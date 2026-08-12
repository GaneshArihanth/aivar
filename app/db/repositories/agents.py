"""Agent lifecycle: create, read, update, soft-delete, key rotation.

Creation is the interesting one. An agent is not a single row — it is a row
plus a monthly budget, a per-session budget and a substitution policy. All four
are written in one transaction, because an agent that exists without a budget
would either be unenforced (if missing limits mean "allow") or permanently
broken (if they mean "deny"), and neither is a state worth being able to reach.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import set_committed_value

from app.core import security
from app.db.models import Agent, Budget, Policy, Team
from app.db.repositories import budgets as budget_repo
from app.db.repositories import catalog as catalog_repo


class AgentNameTaken(Exception):
    """Another live agent on the same team already uses this name."""


class TeamNotFound(Exception):
    pass


class ModelNotFound(Exception):
    pass


class AgentNotFound(Exception):
    pass


@dataclass
class CreatedAgent:
    agent: Agent
    raw_api_key: str
    fallback_chain: list[str]
    monthly_micros: int
    session_micros: int


def _live(stmt: Select) -> Select:
    return stmt.where(Agent.deleted_at.is_(None))


async def get_agent(
    session: AsyncSession, agent_id: int, *, include_deleted: bool = False
) -> Agent | None:
    # Both relationships are eager-loaded: under asyncio a lazy load raises
    # MissingGreenlet rather than quietly emitting a query.
    stmt = (
        select(Agent)
        .options(selectinload(Agent.team), selectinload(Agent.policy))
        .where(Agent.id == agent_id)
    )
    if not include_deleted:
        stmt = _live(stmt)
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_agents(session: AsyncSession) -> list[Agent]:
    stmt = _live(
        select(Agent).options(selectinload(Agent.team), selectinload(Agent.policy))
    ).order_by(Agent.team_id, Agent.id)
    return list((await session.execute(stmt)).scalars())


async def name_is_taken(
    session: AsyncSession, team_id: int, name: str, *, exclude_id: int | None = None
) -> bool:
    stmt = _live(
        select(func.count())
        .select_from(Agent)
        .where(Agent.team_id == team_id, Agent.name == name)
    )
    if exclude_id is not None:
        stmt = stmt.where(Agent.id != exclude_id)
    return bool((await session.execute(stmt)).scalar_one())


async def create_agent(
    session: AsyncSession,
    *,
    name: str,
    team_id: int,
    monthly_micros: int,
    session_micros: int,
    preferred_model: str,
    allow_substitution: bool,
    runaway_hourly_fraction: float | None = None,
) -> CreatedAgent:
    """Create an agent with its budgets and policy in a single transaction."""
    team = (
        await session.execute(select(Team).where(Team.id == team_id))
    ).scalar_one_or_none()
    if team is None:
        raise TeamNotFound(f"No team with id {team_id}")

    model = await catalog_repo.get_model(session, preferred_model)
    if model is None or not model.is_active:
        raise ModelNotFound(f"Unknown or inactive model {preferred_model!r}")

    if await name_is_taken(session, team_id, name):
        raise AgentNameTaken(f"Team already has an agent named {name!r}")

    raw_key = security.generate_api_key()
    agent = Agent(
        team_id=team_id,
        name=name,
        # Only the HMAC lands in the database; raw_key is returned to the caller
        # once and then dropped.
        key_hash=security.hash_api_key(raw_key),
        key_prefix=security.key_prefix(raw_key),
        preferred_model=preferred_model,
        allow_substitution=allow_substitution,
        status="active",
    )
    session.add(agent)
    await session.flush()  # assigns agent.id without ending the transaction

    await budget_repo.upsert_budget(
        session,
        scope="agent",
        scope_id=agent.id,
        period="monthly",
        limit_micros=monthly_micros,
        runaway_hourly_fraction=runaway_hourly_fraction,
    )
    await budget_repo.upsert_budget(
        session,
        scope="session",
        scope_id=agent.id,
        period="per_session",
        limit_micros=session_micros,
    )

    chain = await catalog_repo.build_fallback_chain(session, preferred_model)
    policy = Policy(agent_id=agent.id, fallback_chain=chain)
    session.add(policy)
    await session.flush()

    # Populate the relationships without touching the database. A plain
    # `agent.team = team` would cascade into loading `team.agents`, and
    # `agent.policy = policy` into loading the previous policy for
    # delete-orphan bookkeeping — both are lazy loads, which raise
    # MissingGreenlet under asyncio.
    set_committed_value(agent, "team", team)
    set_committed_value(agent, "policy", policy)

    return CreatedAgent(
        agent=agent,
        raw_api_key=raw_key,
        fallback_chain=chain,
        monthly_micros=monthly_micros,
        session_micros=session_micros,
    )


async def rotate_key(session: AsyncSession, agent: Agent) -> str:
    """Issue a new key and invalidate the old one. Returns the raw key once."""
    raw_key = security.generate_api_key()
    old_hash = agent.key_hash
    agent.key_hash = security.hash_api_key(raw_key)
    agent.key_prefix = security.key_prefix(raw_key)
    agent.key_created_at = datetime.now(UTC)
    security.auth_cache.invalidate(old_hash)
    return raw_key


async def soft_delete(session: AsyncSession, agent: Agent) -> None:
    """Retire an agent without destroying its history.

    The ledger rows stay: deleting an agent must not rewrite what a team spent
    last month. The key hash is scrambled so the old key stops authenticating
    even if the row is later restored by hand.
    """
    agent.deleted_at = datetime.now(UTC)
    agent.status = "deleted"
    agent.key_hash = f"revoked:{agent.id}:{security.generate_api_key()[:24]}"
    security.auth_cache.invalidate_agent(agent.id)


async def get_agent_budgets(
    session: AsyncSession, agent_id: int
) -> tuple[Budget | None, Budget | None]:
    """Returns ``(monthly, per_session)``."""
    rows = list(
        (
            await session.execute(
                select(Budget).where(
                    Budget.scope_id == str(agent_id),
                    Budget.scope.in_(("agent", "session")),
                )
            )
        ).scalars()
    )
    monthly = next((b for b in rows if b.scope == "agent"), None)
    per_session = next((b for b in rows if b.scope == "session"), None)
    return monthly, per_session


async def list_teams_with_counts(session: AsyncSession) -> list[tuple[Team, int]]:
    stmt = (
        select(Team, func.count(Agent.id))
        .outerjoin(
            Agent, (Agent.team_id == Team.id) & (Agent.deleted_at.is_(None))
        )
        .group_by(Team.id)
        .order_by(Team.name)
    )
    return [(t, c) for t, c in (await session.execute(stmt)).all()]


async def get_team(session: AsyncSession, team_id: int) -> Team | None:
    return (
        await session.execute(select(Team).where(Team.id == team_id))
    ).scalar_one_or_none()
