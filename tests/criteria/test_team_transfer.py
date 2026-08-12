"""Moving an agent between teams.

Every live counter is namespaced by team, so this is the one operation where a
foreign-key update alone would silently corrupt the accounting — and the
corruption would be in the agent's favour.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio

from tests.criteria.conftest import agent_status, call


@pytest_asyncio.fixture
async def two_teams(api):
    created = []

    async def _make(name_hint: str, budget: float = 100.0) -> int:
        response = await api.post(
            "/admin/teams",
            json={
                "name": f"{name_hint}-{uuid.uuid4().hex[:8]}",
                "monthly_budget_usd": budget,
            },
        )
        response.raise_for_status()
        created.append(response.json()["id"])
        return response.json()["id"]

    yield _make

    for team_id in created:
        await api.delete(f"/admin/teams/{team_id}")


def team_spend(status, team_id):
    for team in status["teams"]:
        if team["scope_id"] == str(team_id):
            return team["consumed_usd"]
    raise AssertionError(f"team {team_id} missing from status")


@pytest.mark.asyncio
async def test_move_carries_agent_spend_but_leaves_team_totals(
    api, mock, make_agent, two_teams
):
    """The split that matters:

    · the agent keeps its consumption — otherwise moving an agent between teams
      would be a way to reset its monthly budget;
    · the old team keeps its total — it really did spend that money;
    · the new team is not retro-charged for spend it never incurred.
    """
    destination = await two_teams("dest")
    agent = await make_agent(monthly_usd=5, session_usd=5)

    for index in range(4):
        assert (await call(api, agent, session_id=f"pre-{index}")).status_code == 200

    before = (await api.get("/v1/budget/status")).json()
    agent_before = (await agent_status(api, agent))["consumed_usd"]
    source_before = team_spend(before, agent.team_id)
    dest_before = team_spend(before, destination)
    assert agent_before > 0

    moved = await api.post(f"/admin/agents/{agent.id}/move", json={"team_id": destination})
    assert moved.status_code == 200, moved.text
    assert moved.json()["team_id"] == destination

    after = (await api.get("/v1/budget/status")).json()
    assert (await agent_status(api, agent))["consumed_usd"] == pytest.approx(
        agent_before, abs=1e-9
    ), "the agent's spend was reset by the move — a free budget refill"
    assert team_spend(after, agent.team_id) == pytest.approx(source_before, abs=1e-9), (
        "the old team's total changed; it did incur that spend"
    )
    assert team_spend(after, destination) == pytest.approx(dest_before, abs=1e-9), (
        "the new team was retro-charged for spend it never incurred"
    )

    # And the two stores still agree.
    report = (await api.get("/admin/reconcile")).json()
    row = next(r for r in report["agents"] if r["agent_id"] == agent.id)
    assert row["drift_micros"] == 0


@pytest.mark.asyncio
async def test_enforcement_continues_immediately_after_a_move(
    api, mock, make_agent, two_teams
):
    """The limit must bind under the new namespace on the very next call."""
    destination = await two_teams("dest")
    agent = await make_agent(monthly_usd=0.02, session_usd=0.02, allow_substitution=False)

    await api.post(f"/admin/agents/{agent.id}/move", json={"team_id": destination})

    refused = None
    for index in range(60):
        response = await call(api, agent, session_id=f"post-{index}", max_tokens=400)
        if response.status_code == 402:
            refused = response
            break
    assert refused is not None, "the budget stopped binding after the move"

    state = await agent_status(api, agent)
    assert state["consumed_usd"] <= agent.monthly_usd + 1e-9


@pytest.mark.asyncio
async def test_a_paused_agent_cannot_escape_by_moving(api, mock, make_agent, two_teams):
    """The runaway breaker is team-namespaced too. If it did not travel, a
    reassignment would quietly release an agent that a human had not reviewed."""
    destination = await two_teams("dest")
    agent = await make_agent(
        monthly_usd=1.00, session_usd=1.00, runaway_fraction=0.05, model="gpt-4o"
    )

    for burst in range(30):
        responses = await asyncio.gather(
            *(
                call(api, agent, session_id=f"loop-{burst}-{i}", max_tokens=1000)
                for i in range(5)
            )
        )
        if any(r.status_code == 423 for r in responses):
            break
    else:
        pytest.fail("the runaway detector never fired")

    assert (await agent_status(api, agent))["blocked"] is True

    moved = await api.post(f"/admin/agents/{agent.id}/move", json={"team_id": destination})
    assert moved.status_code == 200

    assert (await agent_status(api, agent))["blocked"] is True, (
        "moving the agent released the runaway pause"
    )
    assert (await call(api, agent, session_id="after-move")).status_code == 423

    # It still takes an audited release, from the new team.
    released = await api.post(
        f"/admin/agents/{agent.id}/unblock",
        json={"reason": "Reviewed after transfer", "actor": "test"},
    )
    assert released.status_code == 200
    assert (await call(api, agent, session_id="released")).status_code == 200


@pytest.mark.asyncio
async def test_move_is_refused_when_the_name_would_collide(
    api, mock, make_agent, two_teams
):
    destination = await two_teams("dest")
    first = await make_agent(monthly_usd=1, session_usd=1)

    # A same-named agent already on the destination team.
    clash = await api.post(
        "/admin/agents",
        json={
            "name": first.name,
            "team_id": destination,
            "monthly_budget_usd": 1,
            "session_budget_usd": 1,
            "preferred_model": "gpt-4o-mini",
        },
    )
    assert clash.status_code == 201

    response = await api.post(
        f"/admin/agents/{first.id}/move", json={"team_id": destination}
    )
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "agent_name_taken"

    await api.delete(f"/admin/agents/{clash.json()['agent']['id']}")


@pytest.mark.asyncio
async def test_team_budget_can_be_edited_and_binds_immediately(
    api, mock, make_agent, two_teams
):
    team_id = await two_teams("cap", budget=100.0)
    agent = await make_agent(monthly_usd=50, session_usd=50, team_usd=100.0)
    await api.post(f"/admin/agents/{agent.id}/move", json={"team_id": team_id})

    assert (await call(api, agent, session_id="ok")).status_code == 200

    # Drop the team cap to effectively nothing; the agent's own budget is ample,
    # so only the team ceiling can refuse it.
    patched = await api.patch(
        f"/admin/teams/{team_id}", json={"monthly_budget_usd": 0.0001}
    )
    assert patched.status_code == 200

    refused = await call(api, agent, session_id="over-team-cap", max_tokens=400)
    assert refused.status_code == 402
    error = refused.json()["error"]
    assert error["type"] == "budget_exhausted"
    assert error["scope"] == "team"
