"""Mock LLM provider — an OpenAI-shaped endpoint for testing the controller
without spending real money or waiting on real latency.

It is deliberately *not* a stub that returns a constant. Three behaviours
matter for exercising the budget controller honestly:

1. **Usage that differs from the estimate.** The proxy reserves a worst-case
   hold (``max_tokens`` of output at the model's output rate) and settles
   against reality. If the mock always returned exactly ``max_tokens``, the
   settle/refund path would never be tested. Completions here land on a jittered
   fraction of the ceiling.
2. **Latency.** Real calls take hundreds of milliseconds, which is what makes
   concurrent reservations overlap and race. Instant responses would hide
   exactly the class of bug this system is built to prevent.
3. **Failure.** Timeouts, 500s and malformed usage payloads all happen in
   production, and each leaves a reservation hanging if the proxy mishandles
   it. They are injectable here.

A request counter is exposed at ``/__mock__/stats``. The test for "hard block
at 100%" asserts on it: a blocked request must never reach the provider, and
the only way to prove that is to ask the provider whether it saw anything.
"""

from __future__ import annotations

import asyncio
import random
import time
import uuid
from collections import defaultdict
from typing import Any

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Mock LLM Provider", version="1.0.0")

# Latency profile per model: (mean_ms, stddev_ms). Bigger models are slower.
LATENCY_PROFILE: dict[str, tuple[float, float]] = {
    "gpt-4o": (420, 120),
    "gpt-4o-mini": (180, 60),
    "gpt-4.1-nano": (90, 30),
    "claude-opus-4": (700, 200),
    "claude-sonnet-4": (350, 100),
    "claude-haiku-4-5": (140, 45),
}
DEFAULT_LATENCY = (200.0, 60.0)

# Fraction of max_tokens the model actually generates. The spread is what
# forces the proxy to refund the difference between hold and actual.
COMPLETION_RATIO_RANGE = (0.25, 0.95)


class MockControls(BaseModel):
    """Failure injection, settable at runtime by the test suite."""

    error_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    timeout_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    malformed_usage_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    latency_multiplier: float = Field(default=1.0, ge=0.0, le=100.0)
    forced_delay_ms: int | None = None


controls = MockControls()

stats: dict[str, Any] = {
    "requests_total": 0,
    "requests_by_model": defaultdict(int),
    "errors_injected": 0,
    "timeouts_injected": 0,
    "prompt_tokens_total": 0,
    "completion_tokens_total": 0,
    "started_at": time.time(),
}


def estimate_prompt_tokens(messages: list[dict]) -> int:
    """~4 characters per token, matching the proxy's heuristic tokenizer.

    Both sides using the same approximation keeps the mock honest: a
    discrepancy between reserved and settled amounts should come from output
    length, which is genuinely unknowable up front, not from the two sides
    counting the *input* differently.
    """
    chars = sum(len(str(m.get("content", ""))) + len(str(m.get("role", ""))) for m in messages)
    return max(1, chars // 4)


async def _simulate_latency(model: str) -> float:
    if controls.forced_delay_ms is not None:
        delay = controls.forced_delay_ms / 1000
    else:
        mean, stddev = LATENCY_PROFILE.get(model, DEFAULT_LATENCY)
        delay = max(0.01, random.gauss(mean, stddev) / 1000)
    delay *= controls.latency_multiplier
    await asyncio.sleep(delay)
    return delay


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    body = await request.json()
    model = body.get("model", "gpt-4o")
    messages = body.get("messages", [])
    max_tokens = int(body.get("max_tokens", 512))
    stream = bool(body.get("stream", False))

    stats["requests_total"] += 1
    stats["requests_by_model"][model] += 1

    if random.random() < controls.timeout_rate:
        stats["timeouts_injected"] += 1
        await asyncio.sleep(300)  # the client's timeout should fire first

    if random.random() < controls.error_rate:
        stats["errors_injected"] += 1
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": {"message": "Injected upstream failure", "type": "server_error"}},
        )

    latency = await _simulate_latency(model)

    prompt_tokens = estimate_prompt_tokens(messages)
    completion_tokens = max(1, int(max_tokens * random.uniform(*COMPLETION_RATIO_RANGE)))
    stats["prompt_tokens_total"] += prompt_tokens
    stats["completion_tokens_total"] += completion_tokens

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:20]}"
    created = int(time.time())
    text = (
        f"[mock:{model}] responded to {len(messages)} message(s) "
        f"in {latency * 1000:.0f}ms with {completion_tokens} tokens."
    )

    usage: dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    # A provider that omits usage is a real failure mode; the proxy must fall
    # back to charging the reservation rather than charging nothing.
    if random.random() < controls.malformed_usage_rate:
        usage = {}

    if stream:
        return StreamingResponse(
            _stream_response(completion_id, created, model, text, usage),
            media_type="text/event-stream",
        )

    return JSONResponse(
        content={
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": usage,
        }
    )


async def _stream_response(
    completion_id: str, created: int, model: str, text: str, usage: dict
):
    """SSE stream ending with a usage-bearing chunk, as OpenAI does with
    ``stream_options.include_usage``."""
    import json

    words = text.split(" ")
    for i, word in enumerate(words):
        chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": ("" if i == 0 else " ") + word},
                    "finish_reason": None,
                }
            ],
        }
        yield f"data: {json.dumps(chunk)}\n\n"
        await asyncio.sleep(0.01)

    final = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": usage,
    }
    yield f"data: {json.dumps(final)}\n\n"
    yield "data: [DONE]\n\n"


# --------------------------------------------------------------- test hooks


@app.get("/__mock__/stats")
async def get_stats() -> dict:
    return {
        **stats,
        "requests_by_model": dict(stats["requests_by_model"]),
        "uptime_seconds": round(time.time() - stats["started_at"], 1),
        "controls": controls.model_dump(),
    }


@app.post("/__mock__/reset")
async def reset_stats() -> dict:
    stats["requests_total"] = 0
    stats["requests_by_model"] = defaultdict(int)
    stats["errors_injected"] = 0
    stats["timeouts_injected"] = 0
    stats["prompt_tokens_total"] = 0
    stats["completion_tokens_total"] = 0
    return {"reset": True}


@app.post("/__mock__/controls")
async def set_controls(new: MockControls) -> MockControls:
    global controls
    controls = new
    return controls


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "mock-llm"}
