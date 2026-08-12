"""Live budget status — the dashboard's data source.

Reads limits and identity from PostgreSQL and live spend from Redis, in two
batched round trips rather than per-agent queries: the dashboard polls this,
and a fan-out of one query per agent would make the monitoring more expensive
than the thing being monitored.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, status as status_module
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api import schemas
from app.api.errors import http_error
from app.config import settings
from app.core.money import micros_to_float, pct
from app.db.models import Agent, Budget, Team
from app.db.repositories import ledger as ledger_repo
from app.db.session import get_session
from app.redisx import keys
from app.redisx.client import gateway

router = APIRouter(tags=["status"])


def _state(fraction: float, *, warn: float, substitution: float) -> str:
    if fraction >= 1.0:
        return "exhausted"
    if fraction >= substitution:
        return "pressure"
    if fraction >= warn:
        return "warning"
    return "ok"


@router.get("/v1/budget/status", response_model=schemas.StatusResponse)
async def budget_status(
    session: AsyncSession = Depends(get_session),
) -> schemas.StatusResponse:
    period = keys.monthly_period()

    teams = list(
        (await session.execute(select(Team).order_by(Team.name))).scalars()
    )
    agents = list(
        (
            await session.execute(
                select(Agent)
                .options(selectinload(Agent.team))
                .where(Agent.deleted_at.is_(None))
                .order_by(Agent.team_id, Agent.name)
            )
        ).scalars()
    )
    budgets = list((await session.execute(select(Budget))).scalars())

    team_limits = {
        int(b.scope_id): b for b in budgets if b.scope == "team" and b.period == "monthly"
    }
    agent_limits = {
        int(b.scope_id): b
        for b in budgets
        if b.scope == "agent" and b.period == "monthly"
    }

    # One MGET for every counter the page needs, plus one for the velocity
    # window, instead of a query per agent.
    spend_keys: list[str] = [keys.team_spend(t.id, period) for t in teams]
    spend_keys += [keys.agent_spend(a.team_id, a.id, period) for a in agents]
    blocked_keys = [keys.blocked(a.team_id, a.id) for a in agents]

    client = gateway.client
    pipe = client.pipeline()
    pipe.mget(spend_keys) if spend_keys else None
    for key in blocked_keys:
        pipe.exists(key)
    bucket = keys.minute_bucket()
    for agent in agents:
        pipe.mget(
            [
                keys.velocity(agent.team_id, agent.id, bucket - i)
                for i in range(settings.runaway_window_minutes)
            ]
        )
    results = await pipe.execute()

    raw_spend = results[0] if spend_keys else []
    spend_map = {k: int(v or 0) for k, v in zip(spend_keys, raw_spend)}
    blocked_flags = results[1 : 1 + len(agents)]
    velocity_rows = results[1 + len(agents) :]

    calls = await ledger_repo.calls_today(session)

    team_status: list[schemas.TeamStatus] = []
    for index_team, team in enumerate(teams):
        t_budget = team_limits.get(team.id)
        t_limit = t_budget.limit_micros if t_budget else 0
        t_spend = spend_map.get(keys.team_spend(team.id, period), 0)
        t_pct = pct(t_spend, t_limit)

        members: list[schemas.AgentStatus] = []
        for index_agent, agent in enumerate(agents):
            if agent.team_id != team.id:
                continue
            a_budget = agent_limits.get(agent.id)
            a_limit = a_budget.limit_micros if a_budget else 0
            a_spend = spend_map.get(keys.agent_spend(agent.team_id, agent.id, period), 0)
            a_pct = pct(a_spend, a_limit)
            hour = sum(int(v) for v in (velocity_rows[index_agent] or []) if v)

            members.append(
                schemas.AgentStatus(
                    scope="agent",
                    scope_id=str(agent.id),
                    name=agent.name,
                    team_id=team.id,
                    team_name=team.name,
                    limit_usd=micros_to_float(a_limit),
                    consumed_usd=micros_to_float(a_spend),
                    pct=round(a_pct, 4),
                    state=_state(
                        a_pct,
                        warn=a_budget.warn_threshold if a_budget else 0.8,
                        substitution=(
                            a_budget.substitution_threshold if a_budget else 0.9
                        ),
                    ),
                    status=agent.status,
                    preferred_model=agent.preferred_model,
                    allow_substitution=agent.allow_substitution,
                    hour_spend_usd=micros_to_float(hour),
                    blocked=bool(blocked_flags[index_agent]),
                    calls_today=calls.get(agent.id, 0),
                )
            )

        team_status.append(
            schemas.TeamStatus(
                scope="team",
                scope_id=str(team.id),
                name=team.name,
                limit_usd=micros_to_float(t_limit),
                consumed_usd=micros_to_float(t_spend),
                pct=round(t_pct, 4),
                state=_state(
                    t_pct,
                    warn=t_budget.warn_threshold if t_budget else 0.8,
                    substitution=t_budget.substitution_threshold if t_budget else 0.9,
                ),
                agents=members,
            )
        )
        _ = index_team

    return schemas.StatusResponse(
        period=period,
        resets_at=keys.period_resets_at(period),
        teams=team_status,
        generated_at=datetime.now(UTC),
    )


@router.get("/admin/reconcile", tags=["ops"])
async def reconcile_report(session: AsyncSession = Depends(get_session)) -> dict:
    """Compare Redis counters against the PostgreSQL ledger.

    Non-zero drift with zero outstanding holds means the two stores disagree,
    which is a bug. Drift *with* holds outstanding is expected — those are
    reservations for calls that have not settled yet.
    """
    from app.workers import reconciler

    report = await reconciler.compute_drift(session=session)
    return {
        "period": report.period,
        "clean": report.clean,
        "total_drift_micros": report.total_drift_micros,
        "outstanding_holds": report.outstanding_holds,
        "agents": [
            {
                "agent_id": d.scope_id,
                "name": d.name,
                "redis_micros": d.redis_micros,
                "ledger_micros": d.ledger_micros,
                "drift_micros": d.drift_micros,
            }
            for d in report.agents
        ],
        "teams": [
            {
                "team_id": d.scope_id,
                "redis_micros": d.redis_micros,
                "ledger_micros": d.ledger_micros,
                "drift_micros": d.drift_micros,
            }
            for d in report.teams
        ],
    }


@router.get("/v1/budget/calls", tags=["status"])
async def recent_calls(
    limit: int = 50, session: AsyncSession = Depends(get_session)
) -> list[dict]:
    """Recent ledger rows — the durable per-call record.

    Substitutions are recorded here rather than as one event per call: under
    sustained pressure *every* call is substituted, which would flood the event
    table and the dashboard feed with a repetition of the same fact. The live
    event stream announces it; this is where it is kept.
    """
    rows = await ledger_repo.recent_calls(session, limit=min(limit, 200))
    return [
        {
            "request_id": row.request_id,
            "agent_id": row.agent_id,
            "team_id": row.team_id,
            "session_id": row.session_id,
            "requested_model": row.requested_model,
            "served_model": row.served_model,
            "substituted": row.substituted,
            "prompt_tokens": row.prompt_tokens,
            "completion_tokens": row.completion_tokens,
            "cost_usd": micros_to_float(row.actual_micros),
            "estimated_usd": micros_to_float(row.estimated_micros),
            "decision": row.decision,
            "latency_ms": row.latency_ms,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]


@router.get("/v1/budget/calls/{request_id}", tags=["status"])
async def call_detail(
    request_id: str, session: AsyncSession = Depends(get_session)
) -> dict:
    """One ledger row by request id — what the event detail overlay opens."""
    from app.db.models import CallLedger

    row = (
        await session.execute(
            select(CallLedger).where(CallLedger.request_id == request_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise http_error(
            status_module.HTTP_404_NOT_FOUND,
            "call_not_found",
            f"No ledger row for request {request_id}.",
        )

    return {
        "request_id": row.request_id,
        "agent_id": row.agent_id,
        "team_id": row.team_id,
        "session_id": row.session_id,
        "period": row.period,
        "requested_model": row.requested_model,
        "served_model": row.served_model,
        "substituted": row.substituted,
        "prompt_tokens": row.prompt_tokens,
        "completion_tokens": row.completion_tokens,
        "total_tokens": row.prompt_tokens + row.completion_tokens,
        "estimated_usd": micros_to_float(row.estimated_micros),
        "cost_usd": micros_to_float(row.actual_micros),
        # What the worst-case hold reserved but the call did not use, and was
        # therefore refunded at settle.
        "refunded_usd": micros_to_float(max(0, row.estimated_micros - row.actual_micros)),
        "decision": row.decision,
        "latency_ms": row.latency_ms,
        "created_at": row.created_at.isoformat(),
    }


@router.get("/v1/budget/events", tags=["status"])
async def recent_events(session: AsyncSession = Depends(get_session)) -> list[dict]:
    """Recent budget events for the dashboard's initial paint.

    The live feed arrives over SSE; this fills the panel on first load so it is
    not blank until the next threshold is crossed.
    """
    from app.db.models import BudgetEvent

    rows = list(
        (
            await session.execute(
                select(BudgetEvent).order_by(BudgetEvent.created_at.desc()).limit(50)
            )
        ).scalars()
    )
    return [
        {
            "id": row.id,
            "type": row.type,
            "severity": row.severity,
            "scope": row.scope,
            "scope_id": row.scope_id,
            "message": row.message,
            # Who performed the action, for events that had a human behind them
            # (unblocking in particular — "released by whom" is the point of
            # recording it at all).
            "actor": row.actor,
            "payload": row.payload,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]
