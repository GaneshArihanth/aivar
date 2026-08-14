"""Provider adapters: request/response translation per wire format.

The proxy speaks one dialect to its callers — OpenAI's ``/v1/chat/completions``
— and whatever each upstream needs on the other side. This module is that
seam, and it is what makes a cross-provider fallback chain possible at all: a
chain that steps from GPT-4o to Claude has to change request shape, header
scheme and usage field names mid-flight, and the caller must never see it.

Two kinds are implemented:

* ``openai`` — the OpenAI Chat Completions schema. This covers far more than
  OpenAI: Azure OpenAI, Ollama, vLLM, LM Studio, Groq, Together, Fireworks and
  Gemini's compatibility endpoint all speak it, so registering a self-hosted
  model needs no new code, just a base URL.
* ``anthropic`` — the native Messages API, which differs in three ways that
  matter: the system prompt is a top-level field rather than a message,
  ``max_tokens`` is required, and usage is reported as
  ``input_tokens``/``output_tokens``.

Bedrock and Vertex are deliberately *not* implemented natively — both need
request signing (SigV4 / Google auth) rather than a bearer token, which is a
different problem from format translation. They can still be registered and
routed through any OpenAI-compatible gateway in front of them; the catalog UI
says so rather than offering a setting that silently fails.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import structlog

from app.config import settings

log = structlog.get_logger(__name__)

# Wire formats this proxy can actually dispatch.
DISPATCHABLE_KINDS = ("openai", "anthropic")

# Everything selectable in the catalog UI. The non-dispatchable ones are
# registerable for pricing and policy, and route through a compatible gateway.
PROVIDER_KINDS = {
    "openai": {
        "label": "OpenAI-compatible",
        "dispatchable": True,
        "hint": "OpenAI, Azure OpenAI, Ollama, vLLM, LM Studio, Groq, Together, "
        "Gemini (compatibility endpoint) — anything serving /v1/chat/completions.",
        "default_base_url": "https://api.openai.com/v1",
    },
    "anthropic": {
        "label": "Anthropic Messages",
        "dispatchable": True,
        "hint": "Anthropic's native /v1/messages API.",
        "default_base_url": "https://api.anthropic.com/v1",
    },
    "bedrock": {
        "label": "AWS Bedrock (via gateway)",
        "dispatchable": False,
        "hint": "Bedrock signs requests with SigV4 rather than a bearer token, which "
        "this proxy does not implement. Point base URL at an OpenAI-compatible "
        "gateway in front of Bedrock and set the kind to OpenAI-compatible.",
        "default_base_url": "",
    },
    "vertex": {
        "label": "Google Vertex (via gateway)",
        "dispatchable": False,
        "hint": "Vertex uses Google service-account auth. Use Gemini's OpenAI "
        "compatibility endpoint, or a gateway, with the OpenAI-compatible kind.",
        "default_base_url": "",
    },
}


class MissingCredential(RuntimeError):
    """The model names an env var for its key, and it is not set."""


@dataclass(frozen=True)
class UpstreamCall:
    """A fully-resolved outbound request."""

    url: str
    path: str
    headers: dict[str, str]
    payload: dict[str, Any]


@dataclass(frozen=True)
class UpstreamResult:
    """Normalised response: the OpenAI-shaped body plus extracted usage."""

    body: dict[str, Any]
    prompt_tokens: int
    completion_tokens: int
    has_usage: bool


# --------------------------------------------------------------------- keys


def resolve_credential(api_key_env: str | None) -> str | None:
    """Resolve a provider key from the variable name recorded in the catalog.

    Checks the process environment first, then settings. The second lookup is
    not redundant: pydantic-settings loads .env into the Settings object and
    never into os.environ, so an environment-only lookup finds keys under
    Docker — where compose injects .env as real environment variables — and
    misses them under `make dev`. The failure that produced was a
    "'GEMINI_API_KEY' is not set" error pointing at a variable sitting in plain
    sight in the operator's .env file.
    """
    if not api_key_env:
        return None
    value = os.environ.get(api_key_env)
    if value:
        return value
    # Same name, lowercased, is the settings field: GEMINI_API_KEY -> gemini_api_key.
    from_settings = getattr(settings, api_key_env.lower(), None)
    if from_settings:
        return from_settings
    # Last: a key set through the dashboard. Deliberately lowest precedence, so
    # a value deployed by an operator through SSM or .env always wins over one
    # submitted through a web form that anyone can reach.
    from app.core import credentials

    return credentials.get(api_key_env)


# ------------------------------------------------------------------ openai


def _openai_request(model_id: str, payload: dict, key: str | None) -> UpstreamCall:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return UpstreamCall(
        url="",
        path="/chat/completions",
        headers=headers,
        payload={**payload, "model": model_id},
    )


def _openai_response(body: dict) -> UpstreamResult:
    """Normalise usage, counting reasoning tokens the caller never sees.

    ``completion_tokens`` is not reliably the whole billable output. Gemini's
    OpenAI-compatible endpoint reports the visible completion only, and bills
    the model's internal reasoning on top:

        prompt_tokens 8 · completion_tokens 3 · total_tokens 119

    Metering the 3 there under-counts real spend roughly thirty-fold, which is
    the one mistake this proxy cannot make — an agent would run far past a
    budget the ledger still believed was healthy.

    So the billable completion is whichever is larger: what the provider called
    completion, or what is left of the total after the prompt. Where the two
    already agree — OpenAI folds reasoning into completion_tokens — this
    changes nothing, and it degrades safely if total_tokens is absent.
    """
    usage = body.get("usage") or {}
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    reported = int(usage.get("completion_tokens") or 0)
    total = int(usage.get("total_tokens") or 0)

    return UpstreamResult(
        body=body,
        prompt_tokens=prompt_tokens,
        completion_tokens=max(reported, total - prompt_tokens),
        has_usage=bool(usage),
    )


# --------------------------------------------------------------- anthropic


def _anthropic_request(model_id: str, payload: dict, key: str | None) -> UpstreamCall:
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }
    if key:
        headers["x-api-key"] = key

    # Anthropic takes the system prompt as a top-level field, not as a message
    # with role "system".
    system_parts: list[str] = []
    messages: list[dict] = []
    for message in payload.get("messages") or []:
        if message.get("role") == "system":
            system_parts.append(str(message.get("content", "")))
        else:
            messages.append({"role": message.get("role"), "content": message.get("content")})

    translated: dict[str, Any] = {
        "model": model_id,
        "messages": messages,
        # Required by Anthropic, unlike OpenAI where it is optional. The proxy
        # always has a value here because the reservation is sized against it.
        "max_tokens": int(payload.get("max_tokens") or 1024),
    }
    if system_parts:
        translated["system"] = "\n\n".join(system_parts)
    for field in ("temperature", "top_p", "stop_sequences", "metadata"):
        if field in payload:
            translated[field] = payload[field]

    return UpstreamCall(url="", path="/messages", headers=headers, payload=translated)


def _anthropic_response(body: dict) -> UpstreamResult:
    usage = body.get("usage") or {}
    prompt_tokens = int(usage.get("input_tokens") or 0)
    completion_tokens = int(usage.get("output_tokens") or 0)

    # Re-shape into the OpenAI schema the caller expects. A client pointed at
    # this proxy must not have to care that its request was answered by a
    # different provider's API.
    text = "".join(
        block.get("text", "")
        for block in (body.get("content") or [])
        if isinstance(block, dict) and block.get("type") == "text"
    )
    normalised = {
        "id": body.get("id", ""),
        "object": "chat.completion",
        "created": 0,
        "model": body.get("model", ""),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": body.get("stop_reason") or "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
    return UpstreamResult(
        body=normalised,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        has_usage=bool(usage),
    )


# ------------------------------------------------------------------ facade

_ADAPTERS = {
    "openai": (_openai_request, _openai_response),
    "anthropic": (_anthropic_request, _anthropic_response),
}


def build_request(provider_kind: str, model_id: str, payload: dict, key: str | None):
    builder, _ = _ADAPTERS.get(provider_kind, _ADAPTERS["openai"])
    return builder(model_id, payload, key)


def parse_response(provider_kind: str, body: dict) -> UpstreamResult:
    _, parser = _ADAPTERS.get(provider_kind, _ADAPTERS["openai"])
    return parser(body)


def is_dispatchable(provider_kind: str) -> bool:
    return provider_kind in DISPATCHABLE_KINDS
