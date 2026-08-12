"""The single source of truth for Redis key naming.

Every key carries a ``{team:<id>}`` hash tag. On standalone Redis that is
cosmetic, but it means the three scope keys touched by one ``reserve.lua``
call (session, agent, team) always hash to the same slot, so the design stays
correct if this is ever moved onto Redis Cluster — where a multi-key script
spanning slots is rejected outright.

All monetary values stored under these keys are integer micro-dollars.
"""

from __future__ import annotations

from datetime import UTC, datetime

PREFIX = "bc"


# --------------------------------------------------------------------- period


def monthly_period(now: datetime | None = None) -> str:
    """UTC month bucket, e.g. ``2026-08``."""
    now = now or datetime.now(UTC)
    return f"{now.year:04d}-{now.month:02d}"


def period_resets_at(period: str) -> datetime:
    """First instant of the month after ``period``, UTC."""
    year, month = (int(p) for p in period.split("-"))
    return (
        datetime(year + 1, 1, 1, tzinfo=UTC)
        if month == 12
        else datetime(year, month + 1, 1, tzinfo=UTC)
    )


def minute_bucket(now: datetime | None = None) -> int:
    """Minutes since epoch — the granularity of the runaway velocity window."""
    now = now or datetime.now(UTC)
    return int(now.timestamp()) // 60


# ----------------------------------------------------------------------- tag


def _tag(team_id: int) -> str:
    return f"{PREFIX}:{{team:{team_id}}}"


# -------------------------------------------------------------------- spend


def team_spend(team_id: int, period: str) -> str:
    return f"{_tag(team_id)}:spend:team:{period}"


def agent_spend(team_id: int, agent_id: int, period: str) -> str:
    return f"{_tag(team_id)}:spend:agent:{agent_id}:{period}"


def session_spend(team_id: int, session_id: str) -> str:
    return f"{_tag(team_id)}:spend:session:{session_id}"


# -------------------------------------------------------------------- limits


def team_limit(team_id: int, period: str) -> str:
    return f"{_tag(team_id)}:limit:team:{period}"


def agent_limit(team_id: int, agent_id: int, period: str) -> str:
    return f"{_tag(team_id)}:limit:agent:{agent_id}:{period}"


# ------------------------------------------------------------------ sessions


def session_meta(team_id: int, session_id: str) -> str:
    """Hash: status, opened_at, limit_micros, agent_id."""
    return f"{_tag(team_id)}:session:{session_id}"


def agent_sessions(team_id: int, agent_id: int) -> str:
    """ZSET of an agent's session ids, scored by when each opened.

    Sessions otherwise exist only as individually-keyed hashes with a TTL,
    which makes them unlistable — there is no way to ask "what is this agent
    running right now" without scanning the keyspace. Scored by open time so
    the newest are cheap to read and expired ones are cheap to prune.
    """
    return f"{_tag(team_id)}:sessions:agent:{agent_id}"


# --------------------------------------------------------------------- warns

# SETNX on this key is what makes the 80% warning fire exactly once per scope
# per period, rather than on every call after the threshold is crossed.
def warn_flag(team_id: int, scope: str, scope_id: str, period: str, pct: int) -> str:
    return f"{_tag(team_id)}:warned:{scope}:{scope_id}:{period}:{pct}"


# --------------------------------------------------------------------- holds


def hold(team_id: int, request_id: str) -> str:
    """Hash describing one outstanding reservation, TTL'd as a backstop."""
    return f"{_tag(team_id)}:hold:{request_id}"


# Global ZSET (member = hold key, score = expiry epoch seconds) scanned by the
# reaper. Deliberately untagged: the reaper is not atomic with anything, so it
# does not need slot co-location.
HOLDS_PENDING = f"{PREFIX}:holds:pending"


# ------------------------------------------------------------------- runaway


def velocity_prefix(team_id: int, agent_id: int) -> str:
    """Prefix that ``velocity.lua`` appends bucket numbers to.

    The script builds its own bucket keys, so this prefix and :func:`velocity`
    must stay in lockstep — hence both living here rather than being assembled
    at the call site.
    """
    return f"{_tag(team_id)}:vel:agent:{agent_id}:"


def velocity(team_id: int, agent_id: int, bucket: int) -> str:
    return f"{velocity_prefix(team_id, agent_id)}{bucket}"


def blocked(team_id: int, agent_id: int) -> str:
    return f"{_tag(team_id)}:blocked:agent:{agent_id}"


# ------------------------------------------------------- freeze / boost / rate

# Untagged: a global freeze is not owned by any one team.
FREEZE_GLOBAL = f"{PREFIX}:freeze:global"


def team_freeze(team_id: int) -> str:
    return f"{_tag(team_id)}:freeze:team"


def boost(team_id: int, agent_id: int, period: str) -> str:
    """A one-time grant on top of the agent's monthly limit, for this period."""
    return f"{_tag(team_id)}:boost:agent:{agent_id}:{period}"


def rpm(team_id: int, agent_id: int, bucket: int) -> str:
    return f"{_tag(team_id)}:rpm:agent:{agent_id}:{bucket}"


def tpm(team_id: int, agent_id: int, bucket: int) -> str:
    return f"{_tag(team_id)}:tpm:agent:{agent_id}:{bucket}"


# -------------------------------------------------------------------- events

EVENTS_CHANNEL = f"{PREFIX}:events"
