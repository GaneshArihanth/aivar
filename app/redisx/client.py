"""Redis client lifecycle and Lua script registration.

Enforcement lives entirely in the Lua scripts loaded here; Python only passes
arguments and translates return values. That is deliberate — a decision split
across a Python read and a Python write is not atomic, and non-atomic budget
enforcement is the bug this project exists to fix.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import redis.asyncio as aioredis
import structlog
from redis.asyncio.client import Redis
from redis.commands.core import AsyncScript

from app.config import settings

log = structlog.get_logger(__name__)

SCRIPT_DIR = Path(__file__).parent / "scripts"
SCRIPT_NAMES = ("reserve", "settle", "release", "velocity", "move_agent")


class RedisUnavailable(RuntimeError):
    """Raised when Redis cannot be reached and fail mode is 'closed'."""


class RedisGateway:
    """Owns the connection and the registered scripts."""

    def __init__(self) -> None:
        self._client: Redis | None = None
        self._scripts: dict[str, AsyncScript] = {}

    # ------------------------------------------------------------- lifecycle

    async def connect(self) -> None:
        if settings.is_embedded:
            import fakeredis.aioredis as fakeaioredis

            self._client = fakeaioredis.FakeRedis(decode_responses=True)
            log.info("redis.connected", mode="embedded(fakeredis)")
        else:
            # A blocking pool, not the default one. redis-py's default pool
            # raises MaxConnectionsError once it is saturated, which under
            # fail-closed turns an ordinary traffic burst into a wall of 503s —
            # the proxy would reject requests that are comfortably within
            # budget purely because it ran out of sockets. Blocking makes a
            # burst queue for a connection instead, which is the behaviour a
            # proxy in front of a fleet of agents actually wants.
            pool = aioredis.BlockingConnectionPool.from_url(
                settings.redis_url,
                decode_responses=True,
                health_check_interval=30,
                max_connections=settings.redis_max_connections,
                timeout=settings.redis_pool_timeout_seconds,
            )
            self._client = aioredis.Redis(connection_pool=pool)
            await self._client.ping()
            log.info(
                "redis.connected",
                mode="services",
                url=settings.redis_url,
                max_connections=settings.redis_max_connections,
            )

        self._register_scripts()

    def _register_scripts(self) -> None:
        assert self._client is not None
        for name in SCRIPT_NAMES:
            path = SCRIPT_DIR / f"{name}.lua"
            self._scripts[name] = self._client.register_script(
                path.read_text(encoding="utf-8")
            )
        log.info("redis.scripts_registered", scripts=list(self._scripts))

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---------------------------------------------------------------- access

    @property
    def client(self) -> Redis:
        if self._client is None:
            raise RedisUnavailable("Redis gateway is not connected")
        return self._client

    def script(self, name: str) -> AsyncScript:
        try:
            return self._scripts[name]
        except KeyError:
            raise RedisUnavailable(f"Lua script {name!r} is not registered") from None

    async def run(self, name: str, keys: list[str], args: list[Any]) -> Any:
        """Execute a registered script, honouring the configured fail mode."""
        try:
            return await self.script(name)(keys=keys, args=args)
        except (aioredis.RedisError, OSError, RedisUnavailable) as exc:
            if settings.enforcement_fail_mode == "open":
                log.error("redis.unavailable.fail_open", script=name, error=str(exc))
                return None
            log.error("redis.unavailable.fail_closed", script=name, error=str(exc))
            raise RedisUnavailable(str(exc)) from exc

    async def healthy(self) -> bool:
        try:
            return bool(await self.client.ping())
        except Exception:
            return False


gateway = RedisGateway()
