"""Shared test fixtures.

Redis DB 15 is used throughout so tests can flush freely without touching the
development data in DB 0. The environment is set before any app module is
imported, because settings are read once at import time.
"""

from __future__ import annotations

import os

os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("API_KEY_PEPPER", "test-pepper-do-not-use-in-production")
os.environ.setdefault("ENFORCEMENT_FAIL_MODE", "closed")

import pytest_asyncio  # noqa: E402

from app.core.budget import BudgetPolicy, policy_cache  # noqa: E402
from app.redisx.client import gateway  # noqa: E402

TEST_TEAM_ID = 9001
TEST_AGENT_ID = 9002


@pytest_asyncio.fixture
async def redis_gateway():
    """A connected gateway against a flushed test database."""
    await gateway.connect()
    await gateway.client.flushdb()
    policy_cache.invalidate()
    try:
        yield gateway
    finally:
        await gateway.client.flushdb()
        await gateway.close()


def make_policy(
    *,
    monthly: int,
    per_session: int,
    team_monthly: int,
    warn: float = 0.80,
    hard: float = 1.00,
    substitution: float = 0.90,
    runaway_fraction: float = 0.0,
    agent_id: int = TEST_AGENT_ID,
    team_id: int = TEST_TEAM_ID,
) -> BudgetPolicy:
    return BudgetPolicy(
        agent_id=agent_id,
        team_id=team_id,
        monthly_micros=monthly,
        session_micros=per_session,
        team_monthly_micros=team_monthly,
        warn_threshold=warn,
        hard_threshold=hard,
        substitution_threshold=substitution,
        # Off by default so reservation-level tests are not perturbed by the
        # velocity breaker; the runaway tests set it explicitly.
        runaway_fraction=runaway_fraction,
    )


async def seed_limits(
    gw, *, team_id: int, agent_id: int, team_limit: int, agent_limit: int, period: str
) -> None:
    from app.redisx import keys

    await gw.client.set(keys.team_limit(team_id, period), team_limit)
    await gw.client.set(keys.agent_limit(team_id, agent_id, period), agent_limit)
