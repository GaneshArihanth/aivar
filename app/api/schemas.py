"""Pydantic request/response models for the admin and status APIs."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ------------------------------------------------------------------- shared


class TeamOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    monthly_budget_usd: float | None = None
    agent_count: int = 0


class ModelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    model_id: str
    provider: str
    display_name: str
    input_usd_per_1k: float
    output_usd_per_1k: float
    tier_rank: int
    is_active: bool
    provider_kind: str = "openai"
    base_url: str | None = None
    api_key_env: str | None = None
    # Whether the named environment variable currently resolves. The value
    # itself is never returned by this API.
    credential_present: bool = False
    is_custom: bool = False
    context_window: int | None = None
    notes: str | None = None
    dispatchable: bool = True


class ModelCreateRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    model_id: str = Field(min_length=1, max_length=80)
    provider: str = Field(min_length=1, max_length=40)
    display_name: str | None = Field(default=None, max_length=120)
    input_usd_per_1k: Decimal = Field(ge=0)
    output_usd_per_1k: Decimal = Field(ge=0)
    # Higher rank = more capable/expensive. Fallback chains step downward.
    tier_rank: int = Field(default=20, ge=0, le=100)
    provider_kind: str = "openai"
    base_url: str | None = Field(default=None, max_length=300)
    api_key_env: str | None = Field(default=None, max_length=80)
    context_window: int | None = Field(default=None, ge=1)
    notes: str | None = None
    is_active: bool = True

    @field_validator("model_id", "provider")
    @classmethod
    def _no_blank(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("Cannot be blank")
        return stripped

    @field_validator("provider_kind")
    @classmethod
    def _known_kind(cls, v: str) -> str:
        from app.core.providers import PROVIDER_KINDS

        if v not in PROVIDER_KINDS:
            raise ValueError(f"Unknown provider kind '{v}'")
        return v


class ModelUpdateRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    provider: str | None = Field(default=None, max_length=40)
    display_name: str | None = Field(default=None, max_length=120)
    input_usd_per_1k: Decimal | None = Field(default=None, ge=0)
    output_usd_per_1k: Decimal | None = Field(default=None, ge=0)
    tier_rank: int | None = Field(default=None, ge=0, le=100)
    provider_kind: str | None = None
    base_url: str | None = Field(default=None, max_length=300)
    api_key_env: str | None = Field(default=None, max_length=80)
    context_window: int | None = Field(default=None, ge=1)
    notes: str | None = None
    is_active: bool | None = None

    @field_validator("provider_kind")
    @classmethod
    def _known_kind(cls, v: str | None) -> str | None:
        if v is None:
            return v
        from app.core.providers import PROVIDER_KINDS

        if v not in PROVIDER_KINDS:
            raise ValueError(f"Unknown provider kind '{v}'")
        return v


# ------------------------------------------------------------------- agents


class AgentCreateRequest(BaseModel):
    """Payload for the dashboard's '+ New Agent' modal."""

    name: str = Field(min_length=1, max_length=120)
    team_id: int
    monthly_budget_usd: Decimal = Field(gt=0, le=Decimal("1000000"))
    session_budget_usd: Decimal = Field(gt=0, le=Decimal("1000000"))
    preferred_model: str = Field(min_length=1, max_length=80)
    allow_substitution: bool = True
    # Fraction of the monthly budget that, burned in one hour, trips the
    # runaway breaker. None inherits the default (0.20); 0 disables it.
    runaway_hourly_fraction: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("Agent name cannot be blank")
        return stripped

    @model_validator(mode="after")
    def _session_within_monthly(self) -> AgentCreateRequest:
        if self.session_budget_usd > self.monthly_budget_usd:
            raise ValueError(
                "Session budget cannot exceed the monthly budget — every session "
                "would be capped by the monthly limit first."
            )
        return self


class AgentUpdateRequest(BaseModel):
    """Partial update. Every field optional; omitted fields are untouched."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    monthly_budget_usd: Decimal | None = Field(default=None, gt=0)
    session_budget_usd: Decimal | None = Field(default=None, gt=0)
    preferred_model: str | None = None
    allow_substitution: bool | None = None
    allow_cross_provider: bool | None = None
    status: str | None = None
    # 0 disables the cap. Distinct from budget: pacing, not spend.
    rpm_limit: int | None = Field(default=None, ge=0, le=100_000)
    tpm_limit: int | None = Field(default=None, ge=0, le=100_000_000)
    runaway_hourly_fraction: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("status")
    @classmethod
    def _valid_status(cls, v: str | None) -> str | None:
        # 'blocked' is not settable by hand: it is owned by the runaway
        # detector, and cleared through the unblock endpoint so the release is
        # audited.
        if v is not None and v not in ("active", "paused"):
            raise ValueError("status must be 'active' or 'paused'")
        return v


class AgentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    team_id: int
    team_name: str
    preferred_model: str
    allow_substitution: bool
    allow_cross_provider: bool = False
    status: str
    key_prefix: str
    created_at: datetime
    monthly_budget_usd: float
    session_budget_usd: float
    fallback_chain: list[str] = Field(default_factory=list)
    chain_is_custom: bool = False
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    runaway_hourly_fraction: float | None = None


class AgentCreatedResponse(BaseModel):
    """The only place a raw API key is ever returned."""

    agent: AgentOut
    api_key: str
    api_key_note: str = (
        "Store this now — it is shown once and cannot be retrieved later. "
        "Only a hash is kept on the server."
    )


class KeyRotatedResponse(BaseModel):
    agent_id: int
    api_key: str
    key_prefix: str
    api_key_note: str = (
        "The previous key is now invalid. Store this one — it is shown once."
    )


class UnblockRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
    actor: str | None = Field(default=None, max_length=120)


class AgentMoveRequest(BaseModel):
    """Reassign an agent to another team."""

    team_id: int


class ChainUpdateRequest(BaseModel):
    """Replace an agent's fallback chain.

    The first entry becomes the agent's preferred model, so the two can never
    disagree about where the ladder starts.
    """

    chain: list[str] = Field(min_length=1, max_length=8)


class ChainStep(BaseModel):
    model_id: str
    provider: str
    display_name: str
    input_usd_per_1k: float
    output_usd_per_1k: float
    blended_micros: int


class ChainResponse(BaseModel):
    agent_id: int
    chain: list[str]
    steps: list[ChainStep]
    crosses_providers: bool
    allow_cross_provider: bool
    allow_substitution: bool
    is_custom: bool
    warnings: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ budgets


class BudgetStatus(BaseModel):
    scope: str
    scope_id: str
    name: str
    limit_usd: float
    consumed_usd: float
    pct: float
    state: str  # ok | warning | pressure | exhausted


class AgentStatus(BudgetStatus):
    team_id: int
    team_name: str
    status: str
    preferred_model: str
    allow_substitution: bool
    hour_spend_usd: float = 0.0
    blocked: bool = False
    calls_today: int = 0


class TeamStatus(BudgetStatus):
    agents: list[AgentStatus] = Field(default_factory=list)


class StatusResponse(BaseModel):
    period: str
    resets_at: datetime
    teams: list[TeamStatus]
    generated_at: datetime
