"""Application settings.

Two run modes:

* ``services``  — real Redis + PostgreSQL (the default; what the demo uses).
* ``embedded``  — fakeredis + SQLite, so the whole system runs with zero
  infrastructure installed. Same code paths, different drivers.
"""

from __future__ import annotations

import secrets
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ------------------------------------------------------------------ core
    app_name: str = "Agent Budget Controller"
    dev_mode: Literal["services", "embedded"] = "services"
    log_level: str = "INFO"
    log_json: bool = False

    # -------------------------------------------------------------- backends
    database_url: str = "postgresql+asyncpg://localhost/budget_controller"
    redis_url: str = "redis://localhost:6379/0"

    # Connection pool sizing. The pool blocks rather than erroring when
    # saturated, so a burst queues instead of being rejected as "Redis down".
    redis_max_connections: int = 128
    redis_pool_timeout_seconds: float = 10.0

    # SQLite path used when dev_mode == "embedded".
    embedded_db_path: str = ".embedded/budget_controller.db"

    # ------------------------------------------------------------- upstreams
    # Base URLs include the version prefix, exactly as every provider documents
    # them (https://api.openai.com/v1, https://api.anthropic.com/v1,
    # http://localhost:11434/v1). The adapters append only "/chat/completions"
    # or "/messages", so one convention covers hosted and local alike.
    upstream_base_url: str = "http://127.0.0.1:9000/v1"
    upstream_timeout_seconds: float = 30.0

    # "mock": every model dispatches to upstream_base_url, whatever endpoint the
    # catalog records. "live": each model uses its own base_url and credential.
    #
    # Deliberately not "go live whenever a key happens to be in the
    # environment" — a tool built to prevent surprise spend must not start
    # spending real money because an unrelated OPENAI_API_KEY was exported.
    upstream_mode: Literal["mock", "live"] = "mock"

    # -------------------------------------------------------------- security
    # HMAC pepper for API-key hashing. MUST be set in production; a random
    # per-process value is generated otherwise, which invalidates existing keys
    # on restart — loud by design rather than silently insecure.
    api_key_pepper: str = Field(default_factory=lambda: secrets.token_hex(32))

    # ----------------------------------------------------------- enforcement
    # "closed": reject when Redis is unreachable. The absence of enforcement is
    # the bug this project exists to fix, so this is the default.
    enforcement_fail_mode: Literal["closed", "open"] = "closed"

    default_warn_threshold: float = 0.80
    default_hard_threshold: float = 1.00
    default_substitution_threshold: float = 0.90

    # How long an unsettled reservation survives before the reaper releases it.
    hold_ttl_seconds: int = 120
    reaper_interval_seconds: int = 30

    # ------------------------------------------------------------- runaway
    # Fraction of the monthly budget that, if burned inside one hour, trips the
    # circuit breaker and pauses the agent for human review.
    runaway_hourly_fraction: float = 0.20
    runaway_window_minutes: int = 60
    runaway_sweep_interval_seconds: int = 30

    # -------------------------------------------------------------- sessions
    session_ttl_seconds: int = 60 * 60 * 12

    # ------------------------------------------------------------ tokenizer
    tokenizer: Literal["heuristic", "tiktoken"] = "heuristic"

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, v: str) -> str:
        if v.startswith("postgresql://"):
            # A sync driver here would silently block the event loop.
            return v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v

    @property
    def is_embedded(self) -> bool:
        return self.dev_mode == "embedded"

    @property
    def effective_database_url(self) -> str:
        if self.is_embedded:
            return f"sqlite+aiosqlite:///{self.embedded_db_path}"
        return self.database_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
