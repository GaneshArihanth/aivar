"""Call ledger writes and the aggregates used for reconciliation.

Every settled call lands here. This is what makes Redis expendable: the counters
are a fast cache of a sum that can always be recomputed from these rows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CallLedger
from app.db.session import session_scope

log = structlog.get_logger(__name__)


async def record_call(
    *,
    request_id: str,
    agent_id: int,
    team_id: int,
    session_id: str | None,
    period: str,
    requested_model: str,
    served_model: str | None,
    substituted: bool,
    prompt_tokens: int,
    completion_tokens: int,
    estimated_micros: int,
    actual_micros: int,
    decision: str,
    latency_ms: int,
    session: AsyncSession | None = None,
) -> None:
    """Insert one immutable ledger row.

    Uses ON CONFLICT DO NOTHING against the unique request_id: a retry that
    reaches here twice records once. Ledger failures are logged but never
    raised — the spend has already been enforced in Redis, and failing the
    caller's request because an audit insert failed would trade a correct
    outcome for an incorrect one.
    """
    values = dict(
        request_id=request_id,
        agent_id=agent_id,
        team_id=team_id,
        session_id=session_id,
        period=period,
        requested_model=requested_model,
        served_model=served_model,
        substituted=substituted,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        estimated_micros=estimated_micros,
        actual_micros=actual_micros,
        decision=decision,
        latency_ms=latency_ms,
    )

    async def _insert(s: AsyncSession) -> None:
        if s.bind.dialect.name == "postgresql":
            await s.execute(
                pg_insert(CallLedger).values(**values).on_conflict_do_nothing(
                    index_elements=["request_id"]
                )
            )
        else:
            s.add(CallLedger(**values))

    try:
        if session is not None:
            await _insert(session)
        else:
            async with session_scope() as s:
                await _insert(s)
    except Exception as exc:  # pragma: no cover - audit must not break serving
        log.error("ledger.write_failed", request_id=request_id, error=str(exc))


async def spend_by_agent(session: AsyncSession, period: str) -> dict[int, int]:
    """Total settled spend per agent for a period — the reconciliation source."""
    rows = await session.execute(
        select(CallLedger.agent_id, func.sum(CallLedger.actual_micros))
        .where(CallLedger.period == period)
        .group_by(CallLedger.agent_id)
    )
    return {agent_id: int(total or 0) for agent_id, total in rows.all()}


async def spend_by_team(session: AsyncSession, period: str) -> dict[int, int]:
    rows = await session.execute(
        select(CallLedger.team_id, func.sum(CallLedger.actual_micros))
        .where(CallLedger.period == period)
        .group_by(CallLedger.team_id)
    )
    return {team_id: int(total or 0) for team_id, total in rows.all()}


async def calls_today(session: AsyncSession) -> dict[int, int]:
    since = datetime.now(UTC) - timedelta(days=1)
    rows = await session.execute(
        select(CallLedger.agent_id, func.count())
        .where(CallLedger.created_at >= since)
        .group_by(CallLedger.agent_id)
    )
    return {agent_id: int(count) for agent_id, count in rows.all()}


async def spend_history(
    session: AsyncSession, agent_id: int, *, days: int
) -> list[dict]:
    """Bucketed spend for an agent over the last ``days``.

    Hourly for a week, daily beyond that: 90 days of hourly buckets is 2,160
    points, which no chart of this size can render meaningfully and which would
    make the query and the payload larger than the insight.

    Aggregated in SQL rather than by pulling rows into Python — an agent under
    load can produce tens of thousands of ledger rows in a month.
    """
    hourly = days <= 7
    since = datetime.now(UTC) - timedelta(days=days)

    dialect = session.bind.dialect.name
    if dialect == "postgresql":
        # AT TIME ZONE 'UTC' first. date_trunc on a timestamptz truncates in the
        # *session's* time zone, so on a +05:30 host an "hour" boundary lands at
        # :30 past the hour in UTC — buckets that then match nothing on a
        # UTC-aligned axis, and a chart that is silently always empty.
        bucket = func.date_trunc(
            "hour" if hourly else "day",
            func.timezone("UTC", CallLedger.created_at),
        )
    else:
        # SQLite has no date_trunc; strftime truncates to the same granularity.
        bucket = func.strftime(
            "%Y-%m-%d %H:00:00" if hourly else "%Y-%m-%d 00:00:00",
            CallLedger.created_at,
        )

    rows = await session.execute(
        select(
            bucket.label("bucket"),
            func.sum(CallLedger.actual_micros),
            func.count(),
            func.sum(CallLedger.prompt_tokens),
            func.sum(CallLedger.completion_tokens),
        )
        .where(CallLedger.agent_id == agent_id, CallLedger.created_at >= since)
        .group_by("bucket")
        .order_by("bucket")
    )

    return [
        {
            "bucket": bucket_value.isoformat()
            if hasattr(bucket_value, "isoformat")
            else str(bucket_value),
            "micros": int(total or 0),
            "calls": int(calls or 0),
            "prompt_tokens": int(prompt or 0),
            "completion_tokens": int(completion or 0),
        }
        for bucket_value, total, calls, prompt, completion in rows.all()
    ]


async def agent_totals(session: AsyncSession, agent_id: int, *, days: int) -> dict:
    """Token split, latency and decision mix — the agent detail page's summary."""
    since = datetime.now(UTC) - timedelta(days=days)
    base = CallLedger.agent_id == agent_id, CallLedger.created_at >= since

    row = (
        await session.execute(
            select(
                func.count(),
                func.sum(CallLedger.prompt_tokens),
                func.sum(CallLedger.completion_tokens),
                func.sum(CallLedger.actual_micros),
                func.avg(CallLedger.latency_ms),
                func.max(CallLedger.latency_ms),
                func.sum(
                    case((CallLedger.substituted.is_(True), 1), else_=0)
                ),
            ).where(*base)
        )
    ).one()

    decisions = (
        await session.execute(
            select(CallLedger.decision, func.count())
            .where(*base)
            .group_by(CallLedger.decision)
        )
    ).all()

    models = (
        await session.execute(
            select(
                CallLedger.served_model,
                func.count(),
                func.sum(CallLedger.actual_micros),
            )
            .where(*base, CallLedger.served_model.is_not(None))
            .group_by(CallLedger.served_model)
            .order_by(func.sum(CallLedger.actual_micros).desc())
        )
    ).all()

    calls, prompt, completion, micros, avg_latency, max_latency, substituted = row
    return {
        "calls": int(calls or 0),
        "prompt_tokens": int(prompt or 0),
        "completion_tokens": int(completion or 0),
        "micros": int(micros or 0),
        "avg_latency_ms": round(float(avg_latency or 0), 1),
        "max_latency_ms": int(max_latency or 0),
        "substituted": int(substituted or 0),
        "decisions": {decision: int(count) for decision, count in decisions},
        "by_model": [
            {"model": model, "calls": int(count), "micros": int(spend or 0)}
            for model, count, spend in models
        ],
    }


async def recent_calls(session: AsyncSession, limit: int = 50) -> list[CallLedger]:
    rows = await session.execute(
        select(CallLedger).order_by(CallLedger.created_at.desc()).limit(limit)
    )
    return list(rows.scalars())
