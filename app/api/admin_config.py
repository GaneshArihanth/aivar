"""Teams and model catalog — the reference data behind the modal's dropdowns."""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import schemas
from app.api.errors import http_error
from app.core.money import MICROS_PER_USD, micros_to_float, usd_to_micros
from app.db.models import Agent, Budget, Team
from app.db.repositories import agents as agent_repo
from app.db.repositories import budgets as budget_repo
from app.db.repositories import catalog as catalog_repo
from app.db.session import get_session
from app.redisx import keys
from app.redisx.client import gateway

router = APIRouter(prefix="/admin", tags=["admin:config"])


class TeamCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    monthly_budget_usd: Decimal = Field(gt=0)


@router.get("/teams", response_model=list[schemas.TeamOut])
async def list_teams(
    session: AsyncSession = Depends(get_session),
) -> list[schemas.TeamOut]:
    period = keys.monthly_period()
    out: list[schemas.TeamOut] = []
    for team, count in await agent_repo.list_teams_with_counts(session):
        budget = await budget_repo.get_budget(session, "team", team.id, "monthly")
        out.append(
            schemas.TeamOut(
                id=team.id,
                name=team.name,
                monthly_budget_usd=(
                    micros_to_float(budget.limit_micros) if budget else None
                ),
                agent_count=count,
            )
        )
    _ = period
    return out


@router.post(
    "/teams", response_model=schemas.TeamOut, status_code=status.HTTP_201_CREATED
)
async def create_team(
    body: TeamCreateRequest, session: AsyncSession = Depends(get_session)
) -> schemas.TeamOut:
    existing = await agent_repo.list_teams_with_counts(session)
    if any(t.name.lower() == body.name.lower() for t, _ in existing):
        raise http_error(
            status.HTTP_409_CONFLICT,
            "team_name_taken",
            f"A team named '{body.name}' already exists.",
            field="name",
        )

    team = Team(name=body.name)
    session.add(team)
    await session.flush()

    micros = usd_to_micros(body.monthly_budget_usd)
    await budget_repo.upsert_budget(
        session, scope="team", scope_id=team.id, period="monthly", limit_micros=micros
    )
    await budget_repo.warm_limit_cache(
        team.id, None, keys.monthly_period(), team_limit=micros, agent_limit=None
    )
    await session.commit()
    return schemas.TeamOut(
        id=team.id,
        name=team.name,
        monthly_budget_usd=micros_to_float(micros),
        agent_count=0,
    )


class TeamUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    monthly_budget_usd: Decimal | None = Field(default=None, gt=0)


@router.patch("/teams/{team_id}", response_model=schemas.TeamOut)
async def update_team(
    team_id: int,
    body: TeamUpdateRequest,
    session: AsyncSession = Depends(get_session),
) -> schemas.TeamOut:
    """Rename a team or change its monthly cap.

    Lowering the cap below what the team has already spent is permitted and
    binds immediately — every agent on the team is refused on its next call.
    That is a legitimate thing to want during an incident, so the API allows it
    and the dashboard warns before sending it.
    """
    team = (
        await session.execute(select(Team).where(Team.id == team_id))
    ).scalar_one_or_none()
    if team is None:
        raise http_error(
            status.HTTP_404_NOT_FOUND, "team_not_found", f"No team {team_id}."
        )

    if body.name is not None and body.name != team.name:
        clash = [t for t, _ in await agent_repo.list_teams_with_counts(session)
                 if t.name.lower() == body.name.lower() and t.id != team_id]
        if clash:
            raise http_error(
                status.HTTP_409_CONFLICT,
                "team_name_taken",
                f"A team named '{body.name}' already exists.",
                field="name",
            )
        team.name = body.name

    micros = None
    if body.monthly_budget_usd is not None:
        micros = usd_to_micros(body.monthly_budget_usd)
        await budget_repo.upsert_budget(
            session, scope="team", scope_id=team.id, period="monthly",
            limit_micros=micros,
        )
        await budget_repo.warm_limit_cache(
            team.id, None, keys.monthly_period(), team_limit=micros, agent_limit=None
        )

    await session.flush()
    await session.commit()

    budget_row = await budget_repo.get_budget(session, "team", team.id, "monthly")
    counts = {t.id: c for t, c in await agent_repo.list_teams_with_counts(session)}
    return schemas.TeamOut(
        id=team.id,
        name=team.name,
        monthly_budget_usd=micros_to_float(budget_row.limit_micros) if budget_row else None,
        agent_count=counts.get(team.id, 0),
    )


@router.delete("/teams/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_team(
    team_id: int, session: AsyncSession = Depends(get_session)
) -> Response:
    """Delete an empty team.

    Refuses while live agents remain: removing a team out from under running
    agents would strip the outer budget those agents are enforced against.
    """
    team = (
        await session.execute(select(Team).where(Team.id == team_id))
    ).scalar_one_or_none()
    if team is None:
        raise http_error(
            status.HTTP_404_NOT_FOUND, "team_not_found", f"No team {team_id}."
        )

    remaining = (
        await session.execute(
            select(func.count())
            .select_from(Agent)
            .where(Agent.team_id == team_id, Agent.deleted_at.is_(None))
        )
    ).scalar_one()
    if remaining:
        raise http_error(
            status.HTTP_409_CONFLICT,
            "team_not_empty",
            f"Team '{team.name}' still has {remaining} active agent(s). "
            "Delete or move them first.",
        )

    await session.execute(
        delete(Budget).where(Budget.scope == "team", Budget.scope_id == str(team_id))
    )
    await session.delete(team)
    await session.commit()
    await gateway.client.delete(keys.team_limit(team_id, keys.monthly_period()))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# GET /admin/models now lives in app/api/admin_models.py, alongside the rest of
# the catalog's CRUD, so there is one owner of that prefix.
