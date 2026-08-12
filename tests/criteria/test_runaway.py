"""Success criterion 6 — the runaway agent detector.

This is the scenario from the brief: an agent enters a recursive loop and burns
through budget far faster than intended. The detector must catch it on *rate*,
pause it, and require a human to release it.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.criteria.conftest import agent_events, agent_status, call


@pytest.mark.criteria
@pytest.mark.asyncio
async def test_runaway_detector_pauses_an_agent_burning_20_percent_in_an_hour(
    api, mock, make_agent
):
    """Criterion 6: >20% of the monthly budget inside one hour trips the breaker."""
    # Budget is generous relative to per-call cost, so the *monthly* limit is
    # nowhere near being hit — only the velocity threshold is. That separation
    # is the point: this must fire while the agent still has budget left.
    agent = await make_agent(
        monthly_usd=1.00, session_usd=1.00, runaway_fraction=0.20, model="gpt-4o"
    )

    status_before = await agent_status(api, agent)
    assert status_before["blocked"] is False
    assert status_before["status"] == "active"

    # A recursive loop: fire calls as fast as the proxy will take them.
    tripped = None
    for burst in range(30):
        responses = await asyncio.gather(
            *(
                call(api, agent, session_id=f"loop-{burst}-{i}", max_tokens=1000)
                for i in range(5)
            )
        )
        blocked = [r for r in responses if r.status_code == 423]
        if blocked:
            tripped = blocked[0]
            break

    assert tripped is not None, "the runaway detector never fired"

    body = tripped.json()["error"]
    assert body["type"] == "agent_paused_runaway"
    assert body["requires"] == "human_review"
    assert body["unblock_endpoint"] == f"/admin/agents/{agent.id}/unblock"

    state = await agent_status(api, agent)
    assert state["blocked"] is True
    assert state["hour_spend_usd"] > 0.20 * agent.monthly_usd
    # Crucially, it tripped on rate — not because the month was exhausted.
    assert state["pct"] < 1.0, (
        "the agent was merely out of budget; the velocity detector added nothing"
    )

    critical = await agent_events(api, agent.id, "agent.runaway_blocked")
    assert critical, "no critical event recorded for the runaway"
    assert critical[0]["severity"] == "critical"
    assert critical[0]["payload"]["requires"] == "human_review"


@pytest.mark.criteria
@pytest.mark.asyncio
async def test_blocked_agent_stays_blocked_until_a_human_releases_it(
    api, mock, make_agent
):
    """The pause must not expire on its own.

    A breaker that resets after a cool-off would let a looping agent resume
    looping — the pause exists precisely so that a person looks at it first.
    """
    agent = await make_agent(
        monthly_usd=1.00, session_usd=1.00, runaway_fraction=0.05, model="gpt-4o"
    )

    for burst in range(30):
        responses = await asyncio.gather(
            *(
                call(api, agent, session_id=f"l-{burst}-{i}", max_tokens=1000)
                for i in range(5)
            )
        )
        if any(r.status_code == 423 for r in responses):
            break
    else:
        pytest.fail("the runaway detector never fired")

    await mock.post("/__mock__/reset")

    # Every subsequent call is refused, and none reach the provider.
    for index in range(5):
        response = await call(api, agent, session_id=f"after-{index}")
        assert response.status_code == 423
    assert (await mock.get("/__mock__/stats")).json()["requests_total"] == 0

    # An ordinary PATCH must not be able to quietly undo the pause.
    sneaky = await api.patch(
        f"/admin/agents/{agent.id}", json={"status": "active"}
    )
    assert sneaky.status_code == 409, (
        "a blocked agent was reactivated without the audited unblock path"
    )
    assert (await call(api, agent, session_id="still-blocked")).status_code == 423

    # The unblock endpoint requires a reason…
    no_reason = await api.post(f"/admin/agents/{agent.id}/unblock", json={})
    assert no_reason.status_code == 422

    # …and with one, service resumes and the release is recorded.
    released = await api.post(
        f"/admin/agents/{agent.id}/unblock",
        json={"reason": "Reviewed: retry loop in the caller, patched", "actor": "sre-oncall"},
    )
    assert released.status_code == 200
    assert released.json()["status"] == "active"

    resumed = await call(api, agent, session_id="after-review", max_tokens=100)
    assert resumed.status_code == 200, "unblocking did not restore service"

    audit = await agent_events(api, agent.id, "agent.unblocked")
    assert audit, "the release was not recorded"
    assert audit[0]["payload"]["reason"].startswith("Reviewed:")
    assert audit[0]["actor"] == "sre-oncall"


@pytest.mark.criteria
@pytest.mark.asyncio
async def test_steady_agent_within_its_rate_is_not_flagged(api, mock, make_agent):
    """A busy agent is not a runaway. False positives would make this useless."""
    agent = await make_agent(
        monthly_usd=50.00, session_usd=5.00, runaway_fraction=0.20, model="gpt-4o-mini"
    )

    for index in range(20):
        response = await call(api, agent, session_id=f"steady-{index}", max_tokens=150)
        assert response.status_code == 200

    state = await agent_status(api, agent)
    assert state["blocked"] is False
    assert state["status"] == "active"
    assert state["hour_spend_usd"] < 0.20 * 50.00
