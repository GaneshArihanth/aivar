"""SQLAlchemy models — the configuration store and the audit log.

Division of responsibility with Redis:

* PostgreSQL is the **log of record**: configuration, plus one immutable
  ``call_ledger`` row per settled call. ``scripts/reconcile.py`` can rebuild
  every Redis counter from it, so losing Redis is a recoverable incident rather
  than a budget reset.
* Redis holds the **hot counters** that enforcement reads and writes atomically.

Enum-ish columns are ``String`` + ``CheckConstraint`` rather than native
PostgreSQL enums: portable to the SQLite embedded mode, and alterable without
a migration dance.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

JSONVariant = JSON().with_variant(JSONB, "postgresql")


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------- teams


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    # Set while this team is frozen. Redis holds the flag the enforcement path
    # actually reads; these columns are the durable record of when it was
    # frozen and why, so an incident review can see it after the fact.
    frozen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    frozen_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # passive_deletes leaves child removal to the database's ON DELETE CASCADE.
    # Without it SQLAlchemy tries to null out agents.team_id on team deletion,
    # which violates NOT NULL. call_ledger rows carry no foreign key, so spend
    # history outlives the agents it describes.
    agents: Mapped[list[Agent]] = relationship(
        back_populates="team", passive_deletes=True
    )


# -------------------------------------------------------------------- agents

AGENT_STATUSES = ("active", "paused", "blocked", "deleted")


class Agent(Base):
    __tablename__ = "agents"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','paused','blocked','deleted')", name="ck_agent_status"
        ),
        # Names are unique per team among *live* agents only: a partial unique
        # index, so soft-deleting an agent frees its name for reuse.
        Index(
            "uq_agent_team_name_live",
            "team_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int] = mapped_column(
        ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)

    # Only the HMAC-SHA256 of the key is stored. The raw key exists exactly once,
    # in the body of the response that created it.
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # Non-secret leading fragment, so the UI can identify a key it cannot see.
    key_prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    key_created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    preferred_model: Mapped[str] = mapped_column(String(80), nullable=False)
    allow_substitution: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    # Off by default. Crossing providers changes the response schema, the
    # tokenizer and the data-processing agreement covering the request — real
    # consequences that should follow a decision, not a budget threshold.
    allow_cross_provider: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    # Requests and tokens per minute. NULL or 0 means no rate cap. These bound
    # how hard an agent may hammer a provider, which is a different question
    # from how much it may spend — a cheap model can still exhaust a rate limit.
    rpm_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tpm_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    team: Mapped[Team] = relationship(back_populates="agents")
    policy: Mapped[Policy | None] = relationship(
        back_populates="agent", uselist=False, cascade="all, delete-orphan"
    )


# ------------------------------------------------------------------- budgets

BUDGET_SCOPES = ("team", "agent", "session")
BUDGET_PERIODS = ("monthly", "daily", "per_session")


class Budget(Base):
    """A limit attached to a scope.

    ``scope='session'`` rows are a *policy on an agent* ("$2 per session"),
    not a row per session instance — so ``scope_id`` holds the agent id and
    ``period`` is ``per_session``.
    """

    __tablename__ = "budgets"
    __table_args__ = (
        UniqueConstraint("scope", "scope_id", "period", name="uq_budget_scope_period"),
        CheckConstraint(
            "scope IN ('team','agent','session')", name="ck_budget_scope"
        ),
        CheckConstraint(
            "period IN ('monthly','daily','per_session')", name="ck_budget_period"
        ),
        CheckConstraint("limit_micros > 0", name="ck_budget_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(64), nullable=False)
    limit_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    period: Mapped[str] = mapped_column(String(16), nullable=False)

    warn_threshold: Mapped[float] = mapped_column(Float, default=0.80, nullable=False)
    hard_threshold: Mapped[float] = mapped_column(Float, default=1.00, nullable=False)
    substitution_threshold: Mapped[float] = mapped_column(
        Float, default=0.90, nullable=False
    )
    # Fraction of this budget that, burned within one hour, trips the runaway
    # breaker. NULL inherits the global default (0.20). 0 disables detection.
    #
    # Per-agent because a single global fraction misbehaves at the extremes: a
    # batch job that legitimately does its month's work in one nightly window
    # would be paused every night, while for a very small budget 20% is a few
    # cents and any burst looks like a loop.
    runaway_hourly_fraction: Mapped[float | None] = mapped_column(
        Float, nullable=True, default=None
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


# ------------------------------------------------------------- model catalog


class ModelCatalog(Base):
    """Pricing and the substitution ordering.

    ``tier_rank`` ascends with capability/cost, so a fallback chain is simply
    the same-provider models with a lower rank, cheapest last.
    """

    __tablename__ = "model_catalog"

    model_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    input_micros_per_1k: Mapped[int] = mapped_column(BigInteger, nullable=False)
    output_micros_per_1k: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tier_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Wire format, which decides how a request is translated on the way out and
    # how usage is read on the way back. 'openai' covers every OpenAI-compatible
    # server — Azure, Ollama, vLLM, LM Studio, Groq, Together, and Gemini's
    # compatibility endpoint — so most self-hosted deployments need no new code.
    provider_kind: Mapped[str] = mapped_column(
        String(24), default="openai", nullable=False
    )
    # NULL means "wherever UPSTREAM_BASE_URL points" (the mock, by default).
    base_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # The *name* of the environment variable holding the credential — never the
    # credential. Secrets stay in the process environment, so a database dump
    # cannot be used to call the provider, and the UI can show which key a model
    # needs without ever being able to display it.
    api_key_env: Mapped[str | None] = mapped_column(String(80), nullable=True)

    is_custom: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    context_window: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class Policy(Base):
    __tablename__ = "policies"

    agent_id: Mapped[int] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True
    )
    # Ordered list of model ids, preferred first, cheapest last.
    fallback_chain: Mapped[list] = mapped_column(JSONVariant, default=list, nullable=False)
    # True once an operator has edited the chain by hand. Auto-derived chains
    # are regenerated whenever the preferred model changes; a hand-built one
    # must not be silently overwritten by that.
    is_custom: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    agent: Mapped[Agent] = relationship(back_populates="policy")


# ------------------------------------------------------------------ sessions


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_id: Mapped[int] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False)
    limit_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    close_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)


# -------------------------------------------------------------- call ledger


class CallLedger(Base):
    """One immutable row per settled call. The reconciliation source of truth."""

    __tablename__ = "call_ledger"
    __table_args__ = (
        Index("ix_ledger_agent_ts", "agent_id", "created_at"),
        Index("ix_ledger_team_ts", "team_id", "created_at"),
        Index("ix_ledger_period", "period"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # Unique so a retried settle cannot double-charge.
    request_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)

    agent_id: Mapped[int] = mapped_column(Integer, nullable=False)
    team_id: Mapped[int] = mapped_column(Integer, nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    period: Mapped[str] = mapped_column(String(16), nullable=False)

    requested_model: Mapped[str] = mapped_column(String(80), nullable=False)
    served_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    substituted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    estimated_micros: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    actual_micros: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    # allowed | substituted | rejected_budget | rejected_session |
    # rejected_runaway | upstream_error
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


# -------------------------------------------------------------------- events


class BudgetGrant(Base):
    """A one-time budget boost, on top of the baseline monthly limit.

    Kept as its own row rather than by editing the budget: a boost is a
    temporary, attributable exception ("+$10 so this job can finish"), and
    folding it into the limit would erase both the fact that it happened and
    the number to return to afterwards.
    """

    __tablename__ = "budget_grants"
    __table_args__ = (Index("ix_grants_agent_period", "agent_id", "period"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    agent_id: Mapped[int] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    period: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str | None] = mapped_column(String(120), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class BudgetEvent(Base):
    __tablename__ = "budget_events"
    __table_args__ = (Index("ix_events_ts", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    severity: Mapped[str] = mapped_column(String(16), nullable=False)  # info|warning|critical
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(64), nullable=False)
    type: Mapped[str] = mapped_column(String(48), nullable=False)
    actor: Mapped[str | None] = mapped_column(String(120), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONVariant, default=dict, nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
