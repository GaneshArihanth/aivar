"""Prompt token counting for the pre-flight estimate.

The estimate does not need to be exact — the settle step corrects it against
the provider's reported usage. It needs to be *cheap* (it runs before every
call) and it must not systematically under-count, because a hold that is too
small lets a burst of concurrent requests reserve less than they eventually
spend.

``heuristic`` uses ~4 characters per token, which is close enough for English
and costs nothing. ``tiktoken`` is exact for OpenAI models but adds a heavy
dependency and ~1 ms per call; it is opt-in via ``TOKENIZER=tiktoken``.
"""

from __future__ import annotations

import structlog

from app.config import settings

log = structlog.get_logger(__name__)

CHARS_PER_TOKEN = 4
# Per-message framing overhead (role, delimiters) in the chat format.
MESSAGE_OVERHEAD_TOKENS = 4

_tiktoken_encoders: dict[str, object] = {}


def _heuristic_count(messages: list[dict]) -> int:
    total = 0
    for message in messages:
        content = message.get("content", "")
        if isinstance(content, list):
            # Multimodal content blocks: count the text parts, ignore images
            # (whose token cost is provider-specific and not modelled here).
            content = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        total += len(str(content)) // CHARS_PER_TOKEN + MESSAGE_OVERHEAD_TOKENS
    return max(1, total)


def _tiktoken_count(messages: list[dict], model: str) -> int:
    try:
        import tiktoken
    except ImportError:
        log.warning("tokenizer.tiktoken_missing", fallback="heuristic")
        return _heuristic_count(messages)

    encoder = _tiktoken_encoders.get(model)
    if encoder is None:
        try:
            encoder = tiktoken.encoding_for_model(model)
        except KeyError:
            encoder = tiktoken.get_encoding("o200k_base")
        _tiktoken_encoders[model] = encoder

    total = 0
    for message in messages:
        content = message.get("content", "")
        if isinstance(content, list):
            content = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        total += len(encoder.encode(str(content))) + MESSAGE_OVERHEAD_TOKENS  # type: ignore[attr-defined]
    return max(1, total)


def count_prompt_tokens(messages: list[dict], model: str = "") -> int:
    if settings.tokenizer == "tiktoken":
        return _tiktoken_count(messages, model)
    return _heuristic_count(messages)
