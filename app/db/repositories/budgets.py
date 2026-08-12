"""Budget row access, plus the Redis limit-cache warming that keeps the
enforcement path off PostgreSQL.

``reserve.lua`` needs a limit for every scope it checks. Reading that from
PostgreSQL on each call would put a database round trip in front of every LLM
request, so limits are mirrored into Redis and refreshed whenever the
configuration changes.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Budget
from app.redisx import keys
from app.redisx.client import gateway


async def get_budget(
    session: AsyncSession, scope: str, scope_id: str | int, period: str
) -> Budget | None:
    return (
        await session.execute(
            select(Budget).where(
                Budget.scope == scope,
                Budget.scope_id == str(scope_id),
                Budget.period == period,
            )
        )
    ).scalar_one_or_none()


async def upsert_budget(
    session: AsyncSession,
    *,
    scope: str,
    scope_id: str | int,
    period: str,
    limit_micros: int,
    warn_threshold: float | None = None,
    hard_threshold: float | None = None,
    substitution_threshold: float | None = None,
    runaway_hourly_fraction: float | None = None,
) -> Budget:
    existing = await get_budget(session, scope, scope_id, period)
    if existing is not None:
        existing.limit_micros = limit_micros
        if warn_threshold is not None:
            existing.warn_threshold = warn_threshold
        if hard_threshold is not None:
            existing.hard_threshold = hard_threshold
        if substitution_threshold is not None:
            existing.substitution_threshold = substitution_threshold
        if runaway_hourly_fraction is not None:
            existing.runaway_hourly_fraction = runaway_hourly_fraction
        return existing

    budget = Budget(
        scope=scope,
        scope_id=str(scope_id),
        period=period,
        limit_micros=limit_micros,
        warn_threshold=warn_threshold or settings.default_warn_threshold,
        hard_threshold=hard_threshold or settings.default_hard_threshold,
        substitution_threshold=(
            substitution_threshold or settings.default_substitution_threshold
        ),
        runaway_hourly_fraction=runaway_hourly_fraction,
    )
    session.add(budget)
    return budget


async def warm_limit_cache(
    team_id: int, agent_id: int | None, month: str, *, team_limit: int | None,
    agent_limit: int | None,
) -> None:
    """Mirror limits into Redis so enforcement never touches PostgreSQL."""
    client = gateway.client
    pipe = client.pipeline()
    if team_limit is not None:
        pipe.set(keys.team_limit(team_id, month), team_limit)
    if agent_id is not None and agent_limit is not None:
        pipe.set(keys.agent_limit(team_id, agent_id, month), agent_limit)
    await pipe.execute()


async def drop_limit_cache(team_id: int, agent_id: int, month: str) -> None:
    await gateway.client.delete(keys.agent_limit(team_id, agent_id, month))
