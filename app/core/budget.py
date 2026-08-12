"""Orchestration around the Lua enforcement scripts.

This module does no deciding. It assembles arguments, calls a script, and
translates the result — the allow/deny logic lives in Lua because that is the
only place it can be atomic. Keeping Python free of budget arithmetic is
deliberate: any comparison done here would be against a value that another
worker may already have changed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.repositories import budgets as budget_repo
from app.db.session import session_scope
from app.redisx import keys
from app.redisx.client import gateway

log = structlog.get_logger(__name__)

WARN_FLAG_TTL_SECONDS = 45 * 24 * 3600  # outlives a monthly period
HOLD_KEY_TTL_MULTIPLIER = 5  # hold outlives its zset deadline so the reaper can read it
WARN_PCT = 80
# Rate buckets are per-minute; keep them a little longer so a request landing
# on a boundary still sees the count it belongs to.
RATE_BUCKET_TTL_SECONDS = 120


class Decision(StrEnum):
    OK = "OK"
    PRESSURE = "PRESSURE"
    EXHAUSTED = "EXHAUSTED"
    SESSION_EXHAUSTED = "SESSION_EXHAUSTED"
    SESSION_CLOSED = "SESSION_CLOSED"
    BLOCKED = "BLOCKED"
    LIMIT_MISSING = "LIMIT_MISSING"
    FROZEN = "FROZEN"
    RATE_LIMITED = "RATE_LIMITED"


@dataclass
class Reservation:
    status: Decision
    detail: str
    team_spend: int
    team_limit: int
    agent_spend: int
    agent_limit: int
    session_spend: int
    session_limit: int
    warned: list[str] = field(default_factory=list)
    # Populated on RATE_LIMITED: the count already used this minute.
    rate_used: int = 0

    @property
    def allowed(self) -> bool:
        return self.status is Decision.OK

    def pct_for(self, scope: str) -> float:
        spend, limit = {
            "team": (self.team_spend, self.team_limit),
            "agent": (self.agent_spend, self.agent_limit),
            "session": (self.session_spend, self.session_limit),
        }[scope]
        return spend / limit if limit else 0.0


@dataclass
class Settlement:
    status: str
    estimate: int
    actual: int
    delta: int
    team_spend: int
    agent_spend: int
    session_spend: int


@dataclass
class BudgetPolicy:
    """Per-agent limits and thresholds, cached off the hot path."""

    agent_id: int
    team_id: int
    monthly_micros: int
    session_micros: int
    team_monthly_micros: int
    warn_threshold: float
    hard_threshold: float
    substitution_threshold: float
    runaway_fraction: float
    rpm_limit: int = 0
    tpm_limit: int = 0


class PolicyCache:
    def __init__(self, ttl_seconds: float = 30.0) -> None:
        self._ttl = ttl_seconds
        self._data: dict[int, tuple[float, BudgetPolicy]] = {}

    def get(self, agent_id: int) -> BudgetPolicy | None:
        hit = self._data.get(agent_id)
        if hit is None or hit[0] < time.monotonic():
            self._data.pop(agent_id, None)
            return None
        return hit[1]

    def put(self, policy: BudgetPolicy) -> None:
        self._data[policy.agent_id] = (time.monotonic() + self._ttl, policy)

    def invalidate(self, agent_id: int | None = None) -> None:
        if agent_id is None:
            self._data.clear()
        else:
            self._data.pop(agent_id, None)


policy_cache = PolicyCache()


async def load_policy(
    agent_id: int, team_id: int, session: AsyncSession | None = None
) -> BudgetPolicy:
    """Read an agent's limits from PostgreSQL and cache them."""
    cached = policy_cache.get(agent_id)
    if cached is not None:
        return cached

    async def _load(s: AsyncSession) -> BudgetPolicy:
        from sqlalchemy import select

        from app.db.models import Agent

        rates = (
            await s.execute(
                select(Agent.rpm_limit, Agent.tpm_limit).where(Agent.id == agent_id)
            )
        ).first()
        monthly = await budget_repo.get_budget(s, "agent", agent_id, "monthly")
        per_session = await budget_repo.get_budget(s, "session", agent_id, "per_session")
        team = await budget_repo.get_budget(s, "team", team_id, "monthly")
        return BudgetPolicy(
            agent_id=agent_id,
            team_id=team_id,
            monthly_micros=monthly.limit_micros if monthly else 0,
            session_micros=per_session.limit_micros if per_session else 0,
            team_monthly_micros=team.limit_micros if team else 0,
            warn_threshold=monthly.warn_threshold
            if monthly
            else settings.default_warn_threshold,
            hard_threshold=monthly.hard_threshold
            if monthly
            else settings.default_hard_threshold,
            substitution_threshold=monthly.substitution_threshold
            if monthly
            else settings.default_substitution_threshold,
            # NULL inherits the global default; 0 is an explicit "disabled".
            runaway_fraction=(
                monthly.runaway_hourly_fraction
                if monthly is not None and monthly.runaway_hourly_fraction is not None
                else settings.runaway_hourly_fraction
            ),
            rpm_limit=int(rates[0] or 0) if rates else 0,
            tpm_limit=int(rates[1] or 0) if rates else 0,
        )

    if session is not None:
        policy = await _load(session)
    else:
        async with session_scope() as s:
            policy = await _load(s)

    policy_cache.put(policy)
    return policy


# ------------------------------------------------------------------- reserve


def build_reserve_call(
    *,
    team_id: int,
    agent_id: int,
    session_id: str,
    request_id: str,
    model: str,
    estimate_micros: int,
    policy: BudgetPolicy,
    allow_substitution: bool,
    final_attempt: bool,
    tokens: int,
    period: str,
    now: int,
) -> tuple[list[str], list]:
    """Assemble the KEYS and ARGV for ``reserve.lua``.

    Extracted so exactly one place knows the script's signature. When the
    script gained freeze, boost and rate-limit arguments, a test that had
    duplicated this list started passing nil into Redis — the duplication was
    the defect, not the test.
    """
    minute = keys.minute_bucket()
    script_keys = [
        keys.team_spend(team_id, period),
        keys.agent_spend(team_id, agent_id, period),
        keys.session_spend(team_id, session_id),
        keys.team_limit(team_id, period),
        keys.agent_limit(team_id, agent_id, period),
        keys.session_meta(team_id, session_id),
        keys.blocked(team_id, agent_id),
        keys.hold(team_id, request_id),
        keys.HOLDS_PENDING,
        keys.warn_flag(team_id, "team", str(team_id), period, WARN_PCT),
        keys.warn_flag(team_id, "agent", str(agent_id), period, WARN_PCT),
        keys.agent_sessions(team_id, agent_id),
        keys.FREEZE_GLOBAL,
        keys.team_freeze(team_id),
        keys.boost(team_id, agent_id, period),
        keys.rpm(team_id, agent_id, minute),
        keys.tpm(team_id, agent_id, minute),
    ]
    args = [
        estimate_micros,
        request_id,
        now,
        now + settings.hold_ttl_seconds,
        policy.session_micros,
        policy.warn_threshold,
        policy.hard_threshold,
        policy.substitution_threshold,
        1 if allow_substitution else 0,
        settings.session_ttl_seconds,
        1 if final_attempt else 0,
        WARN_FLAG_TTL_SECONDS,
        model,
        settings.hold_ttl_seconds * HOLD_KEY_TTL_MULTIPLIER,
        agent_id,
        session_id,
        policy.rpm_limit,
        policy.tpm_limit,
        tokens,
        RATE_BUCKET_TTL_SECONDS,
    ]
    return script_keys, args


async def reserve(
    *,
    team_id: int,
    agent_id: int,
    session_id: str,
    request_id: str,
    model: str,
    estimate_micros: int,
    policy: BudgetPolicy,
    allow_substitution: bool,
    final_attempt: bool,
    tokens: int = 0,
    period: str | None = None,
) -> Reservation:
    """Atomically check every scope and hold the estimate if all of them pass.

    ``tokens`` is the worst-case token count for this call (prompt + the
    ``max_tokens`` ceiling), used for the tokens-per-minute limiter — the same
    number the cost estimate is built from.
    """
    period = period or keys.monthly_period()
    now = int(time.time())

    script_keys, args = build_reserve_call(
        team_id=team_id,
        agent_id=agent_id,
        session_id=session_id,
        request_id=request_id,
        model=model,
        estimate_micros=estimate_micros,
        policy=policy,
        allow_substitution=allow_substitution,
        final_attempt=final_attempt,
        tokens=tokens,
        period=period,
        now=now,
    )

    raw = await gateway.run("reserve", script_keys, args)
    result = _parse_reservation(raw)

    # A missing limit means Redis lost its cached configuration (restart, flush,
    # or an agent created by another process). Reload from PostgreSQL, warm the
    # cache and retry exactly once — never fall through to "allow".
    if result.status is Decision.LIMIT_MISSING:
        log.warning(
            "budget.limit_cache_miss", scope=result.detail, agent_id=agent_id
        )
        policy_cache.invalidate(agent_id)
        fresh = await load_policy(agent_id, team_id)
        await budget_repo.warm_limit_cache(
            team_id,
            agent_id,
            period,
            team_limit=fresh.team_monthly_micros,
            agent_limit=fresh.monthly_micros,
        )
        args[4] = fresh.session_micros
        raw = await gateway.run("reserve", script_keys, args)
        result = _parse_reservation(raw)

    return result


def _parse_reservation(raw: list) -> Reservation:
    if raw is None:
        # Only reachable with ENFORCEMENT_FAIL_MODE=open, where the operator has
        # explicitly chosen availability over enforcement.
        return Reservation(
            status=Decision.OK,
            detail="fail_open",
            team_spend=0,
            team_limit=0,
            agent_spend=0,
            agent_limit=0,
            session_spend=0,
            session_limit=0,
        )

    values = [v.decode() if isinstance(v, bytes) else v for v in raw]
    status, detail = values[0], values[1]
    nums = [int(v) for v in values[2:8]]
    warned = [w for w in values[8].split(",") if w]
    # On RATE_LIMITED the ninth slot carries the count used this minute
    # rather than a warning list.
    rate_used = int(values[8]) if status == "RATE_LIMITED" and values[8].isdigit() else 0
    return Reservation(
        status=Decision(status),
        detail=detail,
        team_spend=nums[0],
        team_limit=nums[1],
        agent_spend=nums[2],
        agent_limit=nums[3],
        session_spend=nums[4],
        session_limit=nums[5],
        warned=[] if status == "RATE_LIMITED" else warned,
        rate_used=rate_used,
    )


# -------------------------------------------------------------------- settle


def _settle_keys(team_id: int, agent_id: int, session_id: str, request_id: str,
                 period: str) -> list[str]:
    return [
        keys.team_spend(team_id, period),
        keys.agent_spend(team_id, agent_id, period),
        keys.session_spend(team_id, session_id),
        keys.hold(team_id, request_id),
        keys.HOLDS_PENDING,
    ]


async def settle(
    *,
    team_id: int,
    agent_id: int,
    session_id: str,
    request_id: str,
    actual_micros: int,
    period: str | None = None,
) -> Settlement:
    period = period or keys.monthly_period()
    raw = await gateway.run(
        "settle",
        _settle_keys(team_id, agent_id, session_id, request_id, period),
        [actual_micros, int(time.time())],
    )
    if raw is None:
        return Settlement("FAIL_OPEN", 0, actual_micros, 0, 0, 0, 0)

    values = [v.decode() if isinstance(v, bytes) else v for v in raw]
    return Settlement(
        status=values[0],
        estimate=int(values[1]),
        actual=int(values[2]),
        delta=int(values[3]),
        team_spend=int(values[4]),
        agent_spend=int(values[5]),
        session_spend=int(values[6]),
    )


async def release(
    *,
    team_id: int,
    agent_id: int,
    session_id: str,
    request_id: str,
    reason: str,
    period: str | None = None,
) -> int:
    """Give a hold back in full. Returns the amount released."""
    period = period or keys.monthly_period()
    raw = await gateway.run(
        "release",
        _settle_keys(team_id, agent_id, session_id, request_id, period),
        [reason],
    )
    if raw is None:
        return 0
    values = [v.decode() if isinstance(v, bytes) else v for v in raw]
    released = int(values[1])
    if released:
        log.info(
            "budget.hold_released",
            request_id=request_id,
            agent_id=agent_id,
            released_micros=released,
            reason=reason,
        )
    return released


# --------------------------------------------------------------- session ops


async def close_session(
    team_id: int, session_id: str, reason: str = "closed_by_operator"
) -> None:
    await gateway.client.hset(
        keys.session_meta(team_id, session_id),
        mapping={
            "status": "closed",
            "closed_at": int(time.time()),
            "close_reason": reason,
        },
    )
    await gateway.client.expire(
        keys.session_meta(team_id, session_id), settings.session_ttl_seconds
    )


async def move_agent_counters(
    *,
    agent_id: int,
    from_team: int,
    to_team: int,
    monthly_limit_micros: int,
    period: str | None = None,
) -> dict:
    """Carry an agent's live Redis state to a new team's namespace.

    See ``move_agent.lua`` for what moves and what deliberately stays. In
    short: the agent's own spend, its runaway pause and its velocity window
    follow it; the team totals do not, because the old team genuinely incurred
    that spend.
    """
    period = period or keys.monthly_period()
    raw = await gateway.run(
        "move_agent",
        [
            keys.agent_spend(from_team, agent_id, period),
            keys.agent_spend(to_team, agent_id, period),
            keys.blocked(from_team, agent_id),
            keys.blocked(to_team, agent_id),
            keys.agent_limit(from_team, agent_id, period),
            keys.agent_limit(to_team, agent_id, period),
        ],
        [
            keys.velocity_prefix(from_team, agent_id),
            keys.velocity_prefix(to_team, agent_id),
            keys.minute_bucket(),
            settings.runaway_window_minutes,
            (settings.runaway_window_minutes + 30) * 60,
            monthly_limit_micros,
        ],
    )
    if raw is None:
        return {"moved_micros": 0, "blocked_moved": False, "velocity_buckets": 0}

    values = [v.decode() if isinstance(v, bytes) else v for v in raw]
    return {
        "moved_micros": int(values[0]),
        "blocked_moved": values[1] == "1",
        "velocity_buckets": int(values[2]),
    }


async def list_sessions(team_id: int, agent_id: int, limit: int = 50) -> list[dict]:
    """An agent's recent sessions, newest first.

    Reads the index, then each session's metadata and live spend. Entries whose
    metadata has expired are pruned from the index as they are found — the
    sessions themselves carry a TTL, and without this the index would grow
    without bound while pointing at keys that no longer exist.
    """
    client = gateway.client
    index_key = keys.agent_sessions(team_id, agent_id)

    cutoff = int(time.time()) - settings.session_ttl_seconds
    await client.zremrangebyscore(index_key, 0, cutoff)

    session_ids = await client.zrevrange(index_key, 0, limit - 1)
    if not session_ids:
        return []

    pipe = client.pipeline()
    for session_id in session_ids:
        pipe.hgetall(keys.session_meta(team_id, session_id))
        pipe.get(keys.session_spend(team_id, session_id))
    rows = await pipe.execute()

    sessions: list[dict] = []
    stale: list[str] = []
    for index, session_id in enumerate(session_ids):
        meta = rows[index * 2] or {}
        spend = int(rows[index * 2 + 1] or 0)
        if not meta:
            stale.append(session_id)
            continue
        limit_micros = int(meta.get("limit") or 0)
        sessions.append(
            {
                "session_id": session_id,
                "status": meta.get("status", "open"),
                "opened_at": int(meta.get("opened_at") or 0),
                "closed_at": int(meta.get("closed_at") or 0) or None,
                "close_reason": meta.get("close_reason"),
                "spend_micros": spend,
                "limit_micros": limit_micros,
                "pct": (spend / limit_micros) if limit_micros else 0.0,
            }
        )

    if stale:
        await client.zrem(index_key, *stale)
    return sessions


async def read_spend(team_id: int, agent_id: int, period: str | None = None) -> int:
    period = period or keys.monthly_period()
    value = await gateway.client.get(keys.agent_spend(team_id, agent_id, period))
    return int(value or 0)
