"""Fixtures for the success-criteria tests.

These run against the *live* stack — the proxy on :8000 and the mock provider
on :9000 — rather than an in-process ASGI app. The criteria are statements
about deployed behaviour ("the system hard-blocks at 100%"), and an in-process
harness would quietly skip the parts most likely to break in reality: uvicorn's
concurrency, real Redis, real PostgreSQL, real HTTP between proxy and provider.

Start the stack with `./scripts/devctl.sh start` (or `make demo`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import httpx
import pytest
import pytest_asyncio

PROXY = "http://127.0.0.1:8000"
MOCK = "http://127.0.0.1:9000"


@dataclass
class TestAgent:
    id: int
    team_id: int
    name: str
    api_key: str
    monthly_usd: float
    session_usd: float


@pytest_asyncio.fixture
async def api():
    async with httpx.AsyncClient(base_url=PROXY, timeout=30.0) as client:
        try:
            health = await client.get("/health")
        except httpx.ConnectError:
            pytest.skip("proxy is not running — start it with ./scripts/devctl.sh start")
        if health.status_code != 200:
            pytest.skip(f"proxy is unhealthy: {health.text}")
        yield client


@pytest_asyncio.fixture
async def mock():
    async with httpx.AsyncClient(base_url=MOCK, timeout=30.0) as client:
        try:
            await client.get("/health")
        except httpx.ConnectError:
            pytest.skip("mock provider is not running")
        await client.post("/__mock__/reset")
        await client.post("/__mock__/controls", json={})
        yield client
        await client.post("/__mock__/controls", json={})


@pytest_asyncio.fixture
async def make_agent(api: httpx.AsyncClient):
    """Factory creating a throwaway team + agent with the given budgets.

    Every test gets a fresh team, so counters start at zero and one test's
    spend can never influence another's assertions.
    """
    created: list[int] = []
    created_teams: list[int] = []

    async def _make(
        *,
        monthly_usd: float,
        session_usd: float,
        model: str = "gpt-4o",
        allow_substitution: bool = True,
        team_usd: float | None = None,
        runaway_fraction: float | None = 0.0,
    ) -> TestAgent:
        """Create an isolated team + agent.

        ``runaway_fraction`` defaults to 0 (detector off) so that tests of the
        budget thresholds measure only those thresholds. Test budgets are tiny
        by necessity — a few cents, so a handful of calls exhausts them — and at
        that scale 20% of the month is a fraction of a cent, so the runaway
        breaker would trip first and mask what is being tested. The runaway
        test opts back in explicitly.
        """
        suffix = uuid.uuid4().hex[:8]
        team_response = await api.post(
            "/admin/teams",
            json={
                "name": f"test-team-{suffix}",
                "monthly_budget_usd": team_usd or max(monthly_usd * 100, 1000),
            },
        )
        team_response.raise_for_status()
        team_id = team_response.json()["id"]
        created_teams.append(team_id)

        agent_response = await api.post(
            "/admin/agents",
            json={
                "name": f"probe-{suffix}",
                "team_id": team_id,
                "monthly_budget_usd": monthly_usd,
                "session_budget_usd": session_usd,
                "preferred_model": model,
                "allow_substitution": allow_substitution,
                "runaway_hourly_fraction": runaway_fraction,
            },
        )
        agent_response.raise_for_status()
        payload = agent_response.json()
        created.append(payload["agent"]["id"])

        return TestAgent(
            id=payload["agent"]["id"],
            team_id=team_id,
            name=payload["agent"]["name"],
            api_key=payload["api_key"],
            monthly_usd=monthly_usd,
            session_usd=session_usd,
        )

    yield _make

    # Agents first, then their teams — a team with live agents refuses to be
    # deleted. Without this the throwaway teams accumulate and distort the
    # dashboard's fleet totals.
    for agent_id in created:
        await api.delete(f"/admin/agents/{agent_id}")
    for team_id in created_teams:
        await api.delete(f"/admin/teams/{team_id}")


async def call(
    api: httpx.AsyncClient,
    agent: TestAgent,
    *,
    session_id: str = "s1",
    max_tokens: int = 500,
    model: str | None = None,
    prompt: str = "hello",
) -> httpx.Response:
    payload: dict = {"messages": [{"role": "user", "content": prompt}],
                     "max_tokens": max_tokens}
    if model:
        payload["model"] = model
    return await api.post(
        "/v1/chat/completions",
        json=payload,
        headers={"X-Agent-Key": agent.api_key, "X-Session-Id": session_id},
    )


async def agent_events(api: httpx.AsyncClient, agent_id: int, event_type: str) -> list[dict]:
    response = await api.get("/v1/budget/events")
    response.raise_for_status()
    return [
        e
        for e in response.json()
        if e["type"] == event_type and e["scope_id"] == str(agent_id)
    ]


async def agent_status(api: httpx.AsyncClient, agent: TestAgent) -> dict:
    response = await api.get("/v1/budget/status")
    response.raise_for_status()
    for team in response.json()["teams"]:
        for member in team["agents"]:
            if member["scope_id"] == str(agent.id):
                return member
    raise AssertionError(f"agent {agent.id} not found in status payload")
