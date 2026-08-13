"""Outbound HTTP to the model providers.

Routing is per model, not global. Each catalog entry can carry its own base
URL, wire format and credential, so one agent's fallback chain can step from a
hosted API to a local Ollama box without the caller noticing.

Which endpoint is actually used is governed by ``UPSTREAM_MODE``:

* ``mock``  (default) — everything goes to the mock provider, whatever the
  catalog says. Registering a real endpoint is then free of consequence.
* ``live``  — dispatch to each model's own ``base_url`` with its credential.

The default is deliberately *not* "use the real endpoint whenever a key happens
to be present in the environment". A tool whose entire purpose is preventing
surprise spend must not start spending real money because an unrelated
``OPENAI_API_KEY`` was exported in the shell. Going live is an explicit choice.

Clients are pooled per host: a fresh client per request would add a TCP and TLS
handshake to every call, which would cost more than the budget check it wraps.
"""

from __future__ import annotations

import httpx
import structlog

from app.config import settings
from app.core import providers
from app.core.pricing import ModelPrice

log = structlog.get_logger(__name__)

_clients: dict[str, httpx.AsyncClient] = {}


class UpstreamError(Exception):
    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"upstream returned {status_code}")


class UpstreamTimeout(Exception):
    pass


class UpstreamNotConfigured(Exception):
    """Live mode, but the model cannot be dispatched as configured."""


async def connect() -> None:
    """Warm the default client. Per-host clients are created on first use."""
    _client_for(settings.upstream_base_url)
    log.info(
        "upstream.connected",
        mode=settings.upstream_mode,
        default_base_url=settings.upstream_base_url,
    )


async def close() -> None:
    for client in list(_clients.values()):
        await client.aclose()
    _clients.clear()


def _client_for(base_url: str) -> httpx.AsyncClient:
    client = _clients.get(base_url)
    if client is None:
        client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(settings.upstream_timeout_seconds, connect=5.0),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        )
        _clients[base_url] = client
    return client


def resolve_route(
    model: ModelPrice, *, force_model_endpoint: bool = False
) -> tuple[str, str, str | None]:
    """Return ``(base_url, provider_kind, credential)`` for a model.

    In mock mode everything collapses onto the mock, which speaks the OpenAI
    schema — so the wire format is forced to ``openai`` regardless of what the
    catalog says. Otherwise an Anthropic-kind model would be translated into
    ``/v1/messages`` and sent to a mock that does not serve it.

    ``force_model_endpoint`` bypasses that collapse. The connectivity probe
    needs it: a "Test" button that answers about the mock while the operator is
    asking about their Ollama box would report success for a machine that is
    switched off, which is worse than having no test at all.
    """
    if settings.upstream_mode == "mock" and not force_model_endpoint:
        return settings.upstream_base_url, "openai", None

    if force_model_endpoint and not model.base_url:
        # Nothing of its own to test; the default upstream is what it uses.
        return settings.upstream_base_url, "openai", None

    if not model.base_url:
        raise UpstreamNotConfigured(
            f"Model '{model.model_id}' has no base URL, and UPSTREAM_MODE=live. "
            "Set one in the model catalog, or switch back to mock mode."
        )
    if not providers.is_dispatchable(model.provider_kind):
        raise UpstreamNotConfigured(
            f"Model '{model.model_id}' is registered as '{model.provider_kind}', which "
            "this proxy cannot dispatch directly. Route it through an "
            "OpenAI-compatible gateway and set its kind accordingly."
        )

    credential = providers.resolve_credential(model.api_key_env)
    if model.api_key_env and not credential:
        raise UpstreamNotConfigured(
            f"Model '{model.model_id}' needs the environment variable "
            f"'{model.api_key_env}', which is not set."
        )
    return model.base_url, model.provider_kind, credential


async def chat_completion(
    model: ModelPrice, payload: dict, *, force_model_endpoint: bool = False
) -> providers.UpstreamResult:
    """Dispatch one completion and return a normalised result.

    Raises :class:`UpstreamTimeout`, :class:`UpstreamError` or
    :class:`UpstreamNotConfigured` — each of which the caller must turn into a
    released hold, so a provider problem never consumes budget.

    ``force_model_endpoint`` sends this one call to the model's own endpoint
    even in mock mode, so the Demo page can exercise a real provider without
    switching the whole system to ``upstream_mode="live"`` and putting every
    load test on a metered connection.
    """
    base_url, kind, credential = resolve_route(
        model, force_model_endpoint=force_model_endpoint
    )
    call = providers.build_request(kind, model.model_id, payload, credential)

    try:
        response = await _client_for(base_url).post(
            call.path, json=call.payload, headers=call.headers
        )
    except (httpx.TimeoutException, httpx.ConnectTimeout) as exc:
        raise UpstreamTimeout(str(exc)) from exc
    except httpx.HTTPError as exc:
        raise UpstreamError(502, {"message": str(exc)}) from exc

    if response.status_code >= 400:
        try:
            body = response.json()
        except ValueError:
            body = {"message": response.text[:500]}
        raise UpstreamError(response.status_code, body)

    return providers.parse_response(kind, response.json())


async def probe(model: ModelPrice) -> dict:
    """Check whether a model is reachable as configured, without spending.

    Used by the catalog's "Test" button: registering a local Ollama box and
    finding out at 3am that the port was wrong is a worse way to learn it.
    """
    try:
        base_url, kind, credential = resolve_route(model, force_model_endpoint=True)
    except UpstreamNotConfigured as exc:
        return {"ok": False, "stage": "configuration", "detail": str(exc)}

    # Say plainly which endpoint was contacted, and whether this reflects the
    # path real traffic currently takes.
    tests_own_endpoint = bool(model.base_url)
    live_path = settings.upstream_mode == "live" and tests_own_endpoint

    payload = {
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 8,
    }
    call = providers.build_request(kind, model.model_id, payload, credential)
    try:
        response = await _client_for(base_url).post(
            call.path, json=call.payload, headers=call.headers, timeout=10.0
        )
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "stage": "connect",
            "base_url": base_url,
            "tests_own_endpoint": tests_own_endpoint,
            "detail": f"Could not reach {base_url} — {exc}",
        }

    if response.status_code >= 400:
        return {
            "ok": False,
            "stage": "response",
            "base_url": base_url,
            "tests_own_endpoint": tests_own_endpoint,
            "status": response.status_code,
            "detail": response.text[:300],
        }

    result = providers.parse_response(kind, response.json())
    return {
        "ok": True,
        "stage": "complete",
        "base_url": base_url,
        "provider_kind": kind,
        "tests_own_endpoint": tests_own_endpoint,
        "is_live_path": live_path,
        "reported_usage": result.has_usage,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
    }
