"""Live controls: kill switches and one-time budget boosts.

These are incident tools. Everything they touch is read by ``reserve.lua``
inside the same atomic execution as the budget check — a freeze evaluated in
Python would not stop the requests already in flight between the check and the
increment, which is exactly the traffic an operator is trying to stop.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import http_error
from app.config import settings
from app.core import events, providers
from app.core.money import format_usd, micros_to_float, usd_to_micros
from app.db.models import BudgetGrant, ModelCatalog, Team
from app.db.repositories import agents as agent_repo
from app.db.session import get_session
from app.redisx import keys
from app.redisx.client import gateway

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin:controls"])


class FreezeRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
    actor: str | None = Field(default=None, max_length=120)


class BoostRequest(BaseModel):
    """A temporary allowance on top of the baseline monthly budget."""

    amount_usd: float = Field(gt=0, le=10_000)
    reason: str = Field(min_length=3, max_length=500)
    actor: str | None = Field(default=None, max_length=120)
    # How long the grant lasts. It also disappears at period end regardless,
    # because the key it lives under is period-scoped.
    hours: int = Field(default=24, ge=1, le=720)


# ------------------------------------------------------------ global freeze


@router.get("/freeze")
async def freeze_status(session: AsyncSession = Depends(get_session)) -> dict:
    frozen_global = await gateway.client.hgetall(keys.FREEZE_GLOBAL)
    teams = list((await session.execute(select(Team))).scalars())

    pipe = gateway.client.pipeline()
    for team in teams:
        pipe.exists(keys.team_freeze(team.id))
    flags = await pipe.execute() if teams else []

    return {
        "global": {
            "frozen": bool(frozen_global),
            "reason": frozen_global.get("reason") if frozen_global else None,
            "actor": frozen_global.get("actor") if frozen_global else None,
            "since": frozen_global.get("frozen_at") if frozen_global else None,
        },
        "teams": [
            {
                "team_id": team.id,
                "name": team.name,
                "frozen": bool(flag),
                "reason": team.frozen_reason,
                "since": team.frozen_at.isoformat() if team.frozen_at else None,
            }
            for team, flag in zip(teams, flags)
        ],
    }


@router.post("/freeze")
async def freeze_all(
    body: FreezeRequest, session: AsyncSession = Depends(get_session)
) -> dict:
    """Stop every outbound call, immediately and everywhere.

    Deliberately has no expiry: an incident switch that silently un-flips
    itself is worse than none, because the traffic resumes at the moment
    nobody is watching for it.
    """
    now = datetime.now(UTC)
    await gateway.client.hset(
        keys.FREEZE_GLOBAL,
        mapping={
            "reason": body.reason,
            "actor": body.actor or "dashboard",
            "frozen_at": now.isoformat(),
        },
    )
    await events.emit(
        events.Event(
            type="system.frozen",
            severity="critical",
            scope="global",
            scope_id="*",
            actor=body.actor or "dashboard",
            message=f"All dispatch frozen: {body.reason}",
            payload={"reason": body.reason, "frozen_at": now.isoformat()},
        ),
        session=session,
    )
    await session.commit()
    log.error("system.frozen", reason=body.reason, actor=body.actor)
    return {"frozen": True, "scope": "global", "reason": body.reason}


@router.delete("/freeze")
async def unfreeze_all(session: AsyncSession = Depends(get_session)) -> dict:
    await gateway.client.delete(keys.FREEZE_GLOBAL)
    await events.emit(
        events.Event(
            type="system.unfrozen",
            severity="warning",
            scope="global",
            scope_id="*",
            actor="dashboard",
            message="Dispatch resumed across the system",
            payload={},
        ),
        session=session,
    )
    await session.commit()
    return {"frozen": False, "scope": "global"}


# -------------------------------------------------------------- team freeze


@router.post("/teams/{team_id}/freeze")
async def freeze_team(
    team_id: int, body: FreezeRequest, session: AsyncSession = Depends(get_session)
) -> dict:
    team = (
        await session.execute(select(Team).where(Team.id == team_id))
    ).scalar_one_or_none()
    if team is None:
        raise http_error(
            status.HTTP_404_NOT_FOUND, "team_not_found", f"No team {team_id}."
        )

    now = datetime.now(UTC)
    team.frozen_at = now
    team.frozen_reason = body.reason
    await gateway.client.hset(
        keys.team_freeze(team_id),
        mapping={"reason": body.reason, "frozen_at": now.isoformat()},
    )
    await events.emit(
        events.Event(
            type="team.frozen",
            severity="critical",
            scope="team",
            scope_id=str(team_id),
            actor=body.actor or "dashboard",
            message=f"Team '{team.name}' frozen: {body.reason}",
            payload={"team_id": team_id, "team_name": team.name, "reason": body.reason},
        ),
        session=session,
    )
    await session.commit()
    return {"frozen": True, "scope": "team", "team_id": team_id}


@router.delete("/teams/{team_id}/freeze")
async def unfreeze_team(
    team_id: int, session: AsyncSession = Depends(get_session)
) -> dict:
    team = (
        await session.execute(select(Team).where(Team.id == team_id))
    ).scalar_one_or_none()
    if team is None:
        raise http_error(
            status.HTTP_404_NOT_FOUND, "team_not_found", f"No team {team_id}."
        )

    team.frozen_at = None
    team.frozen_reason = None
    await gateway.client.delete(keys.team_freeze(team_id))
    await events.emit(
        events.Event(
            type="team.unfrozen",
            severity="warning",
            scope="team",
            scope_id=str(team_id),
            actor="dashboard",
            message=f"Team '{team.name}' resumed",
            payload={"team_id": team_id, "team_name": team.name},
        ),
        session=session,
    )
    await session.commit()
    return {"frozen": False, "scope": "team", "team_id": team_id}


# --------------------------------------------------------------------- boost


@router.post("/agents/{agent_id}/boost")
async def boost_agent(
    agent_id: int, body: BoostRequest, session: AsyncSession = Depends(get_session)
) -> dict:
    """Grant an agent extra budget for this period without moving its baseline.

    Boosts accumulate rather than replace: two $10 grants give $20, so the
    button can be pressed twice during an incident without the second silently
    undoing the first.
    """
    agent = await agent_repo.get_agent(session, agent_id)
    if agent is None:
        raise http_error(
            status.HTTP_404_NOT_FOUND, "agent_not_found", f"No agent {agent_id}."
        )

    period = keys.monthly_period()
    micros = usd_to_micros(body.amount_usd)
    expires_at = datetime.now(UTC) + timedelta(hours=body.hours)

    key = keys.boost(agent.team_id, agent.id, period)
    total = await gateway.client.incrby(key, micros)
    # The grant cannot outlive the period it applies to.
    ttl = min(
        body.hours * 3600,
        max(60, int((keys.period_resets_at(period) - datetime.now(UTC)).total_seconds())),
    )
    await gateway.client.expire(key, ttl)

    session.add(
        BudgetGrant(
            agent_id=agent.id,
            micros=micros,
            period=period,
            reason=body.reason,
            actor=body.actor or "dashboard",
            expires_at=expires_at,
        )
    )
    await events.emit(
        events.Event(
            type="agent.boosted",
            severity="warning",
            scope="agent",
            scope_id=str(agent.id),
            actor=body.actor or "dashboard",
            message=(
                f"'{agent.name}' granted an extra {format_usd(micros)} for {period} "
                f"({body.reason})"
            ),
            payload={
                "agent_id": agent.id,
                "agent_name": agent.name,
                "granted_usd": micros_to_float(micros),
                "total_boost_usd": micros_to_float(int(total)),
                "reason": body.reason,
                "expires_at": expires_at.isoformat(),
            },
        ),
        session=session,
    )
    await session.commit()

    log.warning(
        "agent.boosted",
        agent_id=agent.id,
        granted=format_usd(micros),
        total=format_usd(int(total)),
        reason=body.reason,
    )
    return {
        "agent_id": agent.id,
        "granted_usd": micros_to_float(micros),
        "total_boost_usd": micros_to_float(int(total)),
        "period": period,
        "expires_at": expires_at.isoformat(),
    }


@router.get("/agents/{agent_id}/boost")
async def boost_status(
    agent_id: int, session: AsyncSession = Depends(get_session)
) -> dict:
    agent = await agent_repo.get_agent(session, agent_id)
    if agent is None:
        raise http_error(
            status.HTTP_404_NOT_FOUND, "agent_not_found", f"No agent {agent_id}."
        )
    period = keys.monthly_period()
    key = keys.boost(agent.team_id, agent.id, period)
    total = int(await gateway.client.get(key) or 0)
    ttl = await gateway.client.ttl(key)

    grants = list(
        (
            await session.execute(
                select(BudgetGrant)
                .where(BudgetGrant.agent_id == agent_id, BudgetGrant.period == period)
                .order_by(BudgetGrant.created_at.desc())
                .limit(10)
            )
        ).scalars()
    )
    return {
        "agent_id": agent_id,
        "period": period,
        "active_boost_usd": micros_to_float(total),
        "expires_in_seconds": ttl if ttl and ttl > 0 else None,
        "grants": [
            {
                "usd": micros_to_float(g.micros),
                "reason": g.reason,
                "actor": g.actor,
                "created_at": g.created_at.isoformat(),
            }
            for g in grants
        ],
    }


@router.delete("/agents/{agent_id}/boost", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_boost(
    agent_id: int, session: AsyncSession = Depends(get_session)
) -> Response:
    agent = await agent_repo.get_agent(session, agent_id)
    if agent is None:
        raise http_error(
            status.HTTP_404_NOT_FOUND, "agent_not_found", f"No agent {agent_id}."
        )
    await gateway.client.delete(
        keys.boost(agent.team_id, agent.id, keys.monthly_period())
    )
    await events.emit(
        events.Event(
            type="agent.boost_revoked",
            severity="warning",
            scope="agent",
            scope_id=str(agent_id),
            actor="dashboard",
            message=f"Budget boost revoked for '{agent.name}'",
            payload={"agent_id": agent_id},
        ),
        session=session,
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------- demo config


@router.get("/demo/config")
async def demo_config(session: AsyncSession = Depends(get_session)) -> dict:
    """What the Demo page needs to know before it offers to spend money.

    The page asks rather than assumes, so it can say "GEMINI_API_KEY is not
    set" up front instead of letting the operator compose a prompt, press send,
    and discover the misconfiguration in an error body.
    """
    rows = list(
        (
            await session.execute(
                select(ModelCatalog).where(ModelCatalog.is_active.is_(True))
            )
        ).scalars()
    )

    providers_ready: dict[str, bool] = {}
    for row in rows:
        if row.api_key_env:
            providers_ready[row.api_key_env] = bool(
                providers.resolve_credential(row.api_key_env)
            )

    return {
        "live_allowed": settings.demo_allow_live,
        "upstream_mode": settings.upstream_mode,
        # Only these can actually be dispatched to; the rest are catalogued for
        # pricing but need auth this proxy does not implement (SigV4, Google IAM).
        "dispatchable_models": [
            {
                "model_id": row.model_id,
                "provider": row.provider,
                "api_key_env": row.api_key_env,
                "ready": not row.api_key_env
                or bool(providers.resolve_credential(row.api_key_env)),
            }
            for row in rows
            if providers.is_dispatchable(row.provider_kind) and row.base_url
        ],
        "providers_ready": providers_ready,
    }
