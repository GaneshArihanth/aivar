"""API-key generation, hashing and agent authentication.

Why HMAC-SHA256 and not bcrypt/argon2
-------------------------------------
Those are password hashes: deliberately slow, to make guessing a low-entropy
human-chosen secret expensive. An agent key here is 32 bytes from
``secrets.token_urlsafe`` — 256 bits of entropy. There is nothing to brute
force, so the slowness buys no security while costing ~100 ms on *every*
proxied request. Worse, a salted-per-row password hash cannot be looked up by
value: authenticating would mean loading every agent row and comparing one by
one, which is O(n) per request and gets slower as the fleet grows.

HMAC-SHA256 with a server-side pepper is the right tool: deterministic (so the
hash is a unique-indexed lookup key), fast, and — because the pepper lives in
the environment rather than the database — a stolen database dump still cannot
be used to derive or verify keys offline.

The raw key exists in exactly one place: the body of the response that created
it. It is never written to PostgreSQL and never logged (see
``app/logging_setup.py`` for the redaction pass that enforces the latter).
"""

from __future__ import annotations

import hmac
import secrets
import time
from dataclasses import dataclass
from hashlib import sha256

import structlog
from fastapi import Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db.models import Agent
from app.db.session import session_scope

log = structlog.get_logger(__name__)

KEY_PREFIX = "sk-agent-"
KEY_BYTES = 32
PREFIX_DISPLAY_LEN = len(KEY_PREFIX) + 6  # e.g. "sk-agent-a1b2c3"


# ------------------------------------------------------------------ key mint


def generate_api_key() -> str:
    """A fresh raw agent key. Cryptographically random, never stored."""
    return f"{KEY_PREFIX}{secrets.token_urlsafe(KEY_BYTES)}"


def hash_api_key(raw_key: str) -> str:
    """Deterministic HMAC-SHA256 of the key under the server pepper."""
    return hmac.new(
        settings.api_key_pepper.encode("utf-8"),
        raw_key.encode("utf-8"),
        sha256,
    ).hexdigest()


def key_prefix(raw_key: str) -> str:
    """Non-secret fragment retained for display (`sk-agent-a1b2c3…`)."""
    return raw_key[:PREFIX_DISPLAY_LEN]


def keys_match(raw_key: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_api_key(raw_key), stored_hash)


# ---------------------------------------------------------------- auth cache


@dataclass(frozen=True)
class AgentContext:
    """Everything the request path needs about the caller.

    Carries the fallback chain too, so a substitution decision never needs a
    database round trip mid-request.
    """

    agent_id: int
    team_id: int
    agent_name: str
    team_name: str
    preferred_model: str
    allow_substitution: bool
    status: str
    fallback_chain: tuple[str, ...] = ()
    allow_cross_provider: bool = False


class _TTLCache:
    """Tiny per-process cache so authentication is not a DB round trip per call.

    Invalidated explicitly on any agent mutation (see ``invalidate``) and on the
    ``config.invalidate`` event, so a paused/deleted agent stops being served
    immediately rather than after the TTL.
    """

    def __init__(self, ttl_seconds: float = 30.0) -> None:
        self._ttl = ttl_seconds
        self._data: dict[str, tuple[float, AgentContext]] = {}

    def get(self, key: str) -> AgentContext | None:
        hit = self._data.get(key)
        if hit is None:
            return None
        expires_at, value = hit
        if expires_at < time.monotonic():
            self._data.pop(key, None)
            return None
        return value

    def put(self, key: str, value: AgentContext) -> None:
        self._data[key] = (time.monotonic() + self._ttl, value)

    def invalidate(self, key: str | None = None) -> None:
        if key is None:
            self._data.clear()
        else:
            self._data.pop(key, None)

    def invalidate_agent(self, agent_id: int) -> None:
        for k, (_, ctx) in list(self._data.items()):
            if ctx.agent_id == agent_id:
                self._data.pop(k, None)


auth_cache = _TTLCache()


# ------------------------------------------------------------ authentication


def _extract_key(
    authorization: str | None, x_agent_key: str | None
) -> str | None:
    if x_agent_key:
        return x_agent_key.strip()
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


async def authenticate_agent(
    authorization: str | None = Header(default=None),
    x_agent_key: str | None = Header(default=None, alias="X-Agent-Key"),
) -> AgentContext:
    """FastAPI dependency resolving an API key to an :class:`AgentContext`.

    Opens its own short-lived database session on a cache miss rather than
    taking one as a dependency. A dependency-injected session stays open for
    the whole request — which, on the proxy path, includes waiting several
    hundred milliseconds for the upstream provider. Holding a PostgreSQL
    connection for that long would exhaust the pool at even modest concurrency,
    turning an LLM latency spike into a database outage.
    """
    raw_key = _extract_key(authorization, x_agent_key)
    if not raw_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "type": "missing_api_key",
                    "message": "Provide an agent key via 'Authorization: Bearer …' "
                    "or the 'X-Agent-Key' header.",
                }
            },
        )

    hashed = hash_api_key(raw_key)

    cached = auth_cache.get(hashed)
    if cached is not None:
        ctx = cached
    else:
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(Agent)
                    .options(selectinload(Agent.team), selectinload(Agent.policy))
                    .where(Agent.key_hash == hashed)
                    .limit(1)
                )
            ).scalar_one_or_none()

            if row is None or row.deleted_at is not None:
                # Same response for "no such key" and "revoked key" — nothing is
                # leaked about which agents exist.
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail={
                        "error": {
                            "type": "invalid_api_key",
                            "message": "Unrecognised or revoked agent key.",
                        }
                    },
                )

            ctx = AgentContext(
                agent_id=row.id,
                team_id=row.team_id,
                agent_name=row.name,
                team_name=row.team.name,
                preferred_model=row.preferred_model,
                allow_substitution=row.allow_substitution,
                allow_cross_provider=row.allow_cross_provider,
                status=row.status,
                fallback_chain=tuple(
                    row.policy.fallback_chain if row.policy else [row.preferred_model]
                ),
            )
        auth_cache.put(hashed, ctx)

    if ctx.status == "paused":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": {
                    "type": "agent_paused",
                    "agent": ctx.agent_name,
                    "message": "This agent is paused and cannot dispatch requests.",
                }
            },
        )
    return ctx


# ------------------------------------------------------------- admin guard


async def require_admin(
    authorization: str | None = Header(default=None),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> None:
    """Guards ``/admin/*`` when ``ADMIN_TOKEN`` is configured.

    Left open by default so the dashboard works out of the box in a local demo;
    set ``ADMIN_TOKEN`` to lock it down.
    """
    if not settings.admin_token:
        return
    supplied = _extract_key(authorization, x_admin_token)
    if not supplied or not hmac.compare_digest(supplied, settings.admin_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": {"type": "admin_auth_required"}},
        )
