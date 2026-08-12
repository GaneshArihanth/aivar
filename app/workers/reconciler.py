"""Reconciliation between the PostgreSQL ledger and the Redis counters.

Redis holds the hot counters that enforcement reads; PostgreSQL holds one
immutable row per settled call. The ledger is authoritative, so the counters
can always be rebuilt from it — which is what makes a Redis restart, eviction
or flush a recoverable incident rather than a budget reset.

Two operations:

* ``compute_drift`` compares the two without changing anything. Any non-zero
  drift for an agent with no in-flight requests is a bug worth investigating,
  so this doubles as an assertion in the test suite.
* ``rebuild`` overwrites the counters from the ledger.

Drift is *expected* while requests are in flight: a reservation is held in
Redis from the moment it is granted, but the ledger row is only written once
the call settles. Outstanding holds are reported alongside the drift so the two
can be told apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Agent, Budget, Team
from app.db.repositories import ledger as ledger_repo
from app.db.session import session_scope
from app.redisx import keys
from app.redisx.client import gateway

log = structlog.get_logger(__name__)


@dataclass
class ScopeDrift:
    scope: str
    scope_id: int
    name: str
    redis_micros: int
    ledger_micros: int

    @property
    def drift_micros(self) -> int:
        return self.redis_micros - self.ledger_micros


@dataclass
class DriftReport:
    period: str
    agents: list[ScopeDrift] = field(default_factory=list)
    teams: list[ScopeDrift] = field(default_factory=list)
    outstanding_holds: int = 0

    @property
    def total_drift_micros(self) -> int:
        return sum(abs(d.drift_micros) for d in self.agents)

    @property
    def clean(self) -> bool:
        return self.total_drift_micros == 0


async def compute_drift(
    period: str | None = None, session: AsyncSession | None = None
) -> DriftReport:
    period = period or keys.monthly_period()

    async def _run(s: AsyncSession) -> DriftReport:
        agents = list(
            (
                await s.execute(select(Agent).where(Agent.deleted_at.is_(None)))
            ).scalars()
        )
        ledger_agents = await ledger_repo.spend_by_agent(s, period)
        ledger_teams = await ledger_repo.spend_by_team(s, period)

        report = DriftReport(period=period)

        if agents:
            agent_keys = [
                keys.agent_spend(a.team_id, a.id, period) for a in agents
            ]
            values = await gateway.client.mget(agent_keys)
            for agent, value in zip(agents, values):
                report.agents.append(
                    ScopeDrift(
                        scope="agent",
                        scope_id=agent.id,
                        name=agent.name,
                        redis_micros=int(value or 0),
                        ledger_micros=ledger_agents.get(agent.id, 0),
                    )
                )

        # Only teams that still exist. The ledger deliberately outlives deleted
        # teams, but resurrecting counters for them would leave orphan keys
        # that nothing ever reads or clears.
        live_team_ids = set(
            (await s.execute(select(Team.id))).scalars()
        )
        team_ids = sorted(
            ({a.team_id for a in agents} | set(ledger_teams)) & live_team_ids
        )
        if team_ids:
            team_keys = [keys.team_spend(t, period) for t in team_ids]
            values = await gateway.client.mget(team_keys)
            for team_id, value in zip(team_ids, values):
                report.teams.append(
                    ScopeDrift(
                        scope="team",
                        scope_id=team_id,
                        name=f"team:{team_id}",
                        redis_micros=int(value or 0),
                        ledger_micros=ledger_teams.get(team_id, 0),
                    )
                )

        report.outstanding_holds = await gateway.client.zcard(keys.HOLDS_PENDING)
        return report

    if session is not None:
        return await _run(session)
    async with session_scope() as own:
        return await _run(own)


async def rebuild(period: str | None = None) -> DriftReport:
    """Overwrite Redis counters from the ledger.

    Deliberately not automatic: if drift appears during normal operation that
    is a bug to investigate, and silently papering over it on a timer would
    hide the very thing worth seeing. This is an operator action, run after a
    Redis incident.
    """
    period = period or keys.monthly_period()
    report = await compute_drift(period)

    pipe = gateway.client.pipeline()
    async with session_scope() as session:
        agents = list(
            (
                await session.execute(select(Agent).where(Agent.deleted_at.is_(None)))
            ).scalars()
        )
        by_id = {a.id: a for a in agents}
        for drift in report.agents:
            agent = by_id.get(drift.scope_id)
            if agent is None:
                continue
            pipe.set(
                keys.agent_spend(agent.team_id, agent.id, period), drift.ledger_micros
            )
        for drift in report.teams:
            pipe.set(keys.team_spend(drift.scope_id, period), drift.ledger_micros)

        # Restore the cached limits too. The request path self-heals a missing
        # limit (LIMIT_MISSING → reload from PostgreSQL → retry), but the whole
        # point of running this after an incident is to hand back a warm system
        # rather than one that repairs itself one 500-microsecond stumble at a
        # time.
        budgets = list((await session.execute(select(Budget))).scalars())
        for budget in budgets:
            if budget.period != "monthly":
                continue
            if budget.scope == "team":
                pipe.set(keys.team_limit(int(budget.scope_id), period), budget.limit_micros)
            elif budget.scope == "agent":
                agent = by_id.get(int(budget.scope_id))
                if agent is not None:
                    pipe.set(
                        keys.agent_limit(agent.team_id, agent.id, period),
                        budget.limit_micros,
                    )
    await pipe.execute()

    log.info(
        "reconcile.rebuilt",
        period=period,
        agents=len(report.agents),
        teams=len(report.teams),
        drift_micros=report.total_drift_micros,
    )
    return report
