"""Runaway agent detection.

The scenario from the brief: an agent enters a recursive loop and makes 50,000
calls overnight. Monthly totals cannot catch that — by the time the total looks
wrong, the money is already spent. What separates a loop from ordinary work is
*rate*, so this watches spend velocity: more than 20% of a monthly budget
burned inside a single hour is not a busy day, it is a bug.

Tripping the breaker pauses the agent and requires a human to release it. That
is deliberate. An automatic cool-off would let a looping agent resume and keep
looping — the pause exists so that a person looks at it.
"""

from __future__ import annotations

import time

import structlog

from app.config import settings
from app.core import events
from app.core.money import format_usd, micros_to_float
from app.redisx import keys
from app.redisx.client import gateway

log = structlog.get_logger(__name__)

# Buckets outlive the window so the sum is never short-changed at the edges.
BUCKET_TTL_SECONDS = (settings.runaway_window_minutes + 30) * 60


def threshold_micros(monthly_limit_micros: int, fraction: float | None = None) -> int:
    """Spend within one hour that constitutes a runaway."""
    if fraction is None:
        fraction = settings.runaway_hourly_fraction
    return int(monthly_limit_micros * fraction)


async def record_spend(
    *,
    team_id: int,
    agent_id: int,
    agent_name: str,
    amount_micros: int,
    monthly_limit_micros: int,
    fraction: float | None = None,
) -> tuple[int, bool]:
    """Add settled spend to the velocity window; trip the breaker if breached.

    Returns ``(hour_spend_micros, tripped)``.
    """
    if monthly_limit_micros <= 0:
        return 0, False

    limit = threshold_micros(monthly_limit_micros, fraction)
    bucket = keys.minute_bucket()
    prefix = keys.velocity_prefix(team_id, agent_id)

    raw = await gateway.run(
        "velocity",
        [keys.blocked(team_id, agent_id)],
        [
            prefix,
            bucket,
            settings.runaway_window_minutes,
            amount_micros,
            limit,
            BUCKET_TTL_SECONDS,
            int(time.time()),
            BUCKET_TTL_SECONDS,
        ],
    )
    if raw is None:
        return 0, False

    values = [v.decode() if isinstance(v, bytes) else v for v in raw]
    hour_spend, tripped = int(values[0]), values[1] == "1"

    if tripped:
        await _announce_trip(
            team_id=team_id,
            agent_id=agent_id,
            agent_name=agent_name,
            hour_spend=hour_spend,
            limit=limit,
            monthly_limit=monthly_limit_micros,
        )

    return hour_spend, tripped


async def _announce_trip(
    *,
    team_id: int,
    agent_id: int,
    agent_name: str,
    hour_spend: int,
    limit: int,
    monthly_limit: int,
) -> None:
    pct = (hour_spend / monthly_limit * 100) if monthly_limit else 0
    log.error(
        "runaway.detected",
        agent_id=agent_id,
        agent_name=agent_name,
        hour_spend_usd=format_usd(hour_spend),
        threshold_usd=format_usd(limit),
        pct_of_monthly=round(pct, 1),
    )

    # Mirror the pause into PostgreSQL so the agent reads as blocked even if
    # Redis is later flushed, and so the dashboard's agent list agrees with the
    # enforcement path.
    try:
        from sqlalchemy import update

        from app.core import security
        from app.db.models import Agent
        from app.db.session import session_scope

        async with session_scope() as session:
            await session.execute(
                update(Agent).where(Agent.id == agent_id).values(status="blocked")
            )
            await events.emit(
                events.Event(
                    type="agent.runaway_blocked",
                    severity="critical",
                    scope="agent",
                    scope_id=str(agent_id),
                    message=(
                        f"Agent '{agent_name}' paused: spent ${format_usd(hour_spend)} "
                        f"in the last hour ({pct:.0f}% of its monthly budget). "
                        "Possible infinite loop — human review required."
                    ),
                    payload={
                        "agent_id": agent_id,
                        "agent_name": agent_name,
                        "team_id": team_id,
                        "hour_spend_usd": micros_to_float(hour_spend),
                        "threshold_usd": micros_to_float(limit),
                        "pct_of_monthly": round(pct, 1),
                        "requires": "human_review",
                    },
                ),
                session=session,
            )
        security.auth_cache.invalidate_agent(agent_id)
    except Exception as exc:  # pragma: no cover
        # The Redis breaker is already set, so enforcement holds regardless.
        log.error("runaway.persist_failed", agent_id=agent_id, error=str(exc))


async def hour_spend(team_id: int, agent_id: int) -> int:
    """Current sliding-hour spend, for the dashboard."""
    bucket = keys.minute_bucket()
    bucket_keys = [
        keys.velocity(team_id, agent_id, bucket - i)
        for i in range(settings.runaway_window_minutes)
    ]
    values = await gateway.client.mget(bucket_keys)
    return sum(int(v) for v in values if v)


async def is_blocked(team_id: int, agent_id: int) -> bool:
    return bool(await gateway.client.exists(keys.blocked(team_id, agent_id)))
