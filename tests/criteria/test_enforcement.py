"""Success criteria 2, 3 and 4 — warning, hard block, session breach."""

from __future__ import annotations

import pytest

from tests.criteria.conftest import agent_events, agent_status, call


@pytest.mark.criteria
@pytest.mark.asyncio
async def test_warning_fires_exactly_once_at_80_percent(api, mock, make_agent):
    """Criterion 2: a warning is logged exactly at the 80% threshold.

    "Exactly" is read strictly: not merely that a warning appears once the
    agent is past 80%, but that it fires on the crossing and never repeats.
    A warning that re-fires on every subsequent call is noise, and noise is
    what stops people reading alerts.
    """
    agent = await make_agent(monthly_usd=0.05, session_usd=0.05)

    warnings_before = await agent_events(api, agent.id, "budget.warning")
    assert warnings_before == []

    # Drive spend upward until the agent crosses 80%, checking after each call.
    crossed_at = None
    for index in range(60):
        response = await call(api, agent, session_id=f"warm-{index}", max_tokens=200)
        if response.status_code != 200:
            break
        state = await agent_status(api, agent)
        if state["pct"] >= 0.80:
            crossed_at = index
            break

    assert crossed_at is not None, "never reached 80% of the budget"

    warnings = await agent_events(api, agent.id, "budget.warning")
    assert len(warnings) == 1, (
        f"expected exactly one warning at the crossing, got {len(warnings)}"
    )
    assert 0.80 <= warnings[0]["payload"]["pct"] < 1.0

    # Keep spending inside the budget; the warning must not repeat.
    for index in range(5):
        response = await call(api, agent, session_id=f"after-{index}", max_tokens=100)
        if response.status_code != 200:
            break

    warnings_after = await agent_events(api, agent.id, "budget.warning")
    assert len(warnings_after) == 1, (
        f"the 80% warning re-fired {len(warnings_after)} times — it should mark "
        "the crossing, not every call past it"
    )


@pytest.mark.criteria
@pytest.mark.asyncio
async def test_hard_block_at_100_percent_never_reaches_the_provider(api, mock, make_agent):
    """Criterion 3: the system hard-blocks at 100%.

    The important assertion is not the 402 — it is that the mock provider's
    request counter does not move. Enforcement that rejects *after* dispatch
    would still bill the account; the whole premise is that a blocked request
    never leaves the proxy.
    """
    agent = await make_agent(monthly_usd=0.02, session_usd=0.02)

    blocked_response = None
    for index in range(80):
        response = await call(api, agent, session_id=f"burn-{index}", max_tokens=400)
        if response.status_code == 402:
            blocked_response = response
            break

    assert blocked_response is not None, "never hit the hard limit"

    body = blocked_response.json()["error"]
    assert body["type"] == "budget_exhausted"
    assert body["scope"] in ("agent", "team")
    assert "resets_at" in body and "limit_usd" in body and "consumed_usd" in body

    # The provider must not observe the blocked attempts.
    before = (await mock.get("/__mock__/stats")).json()["requests_total"]
    for index in range(10):
        response = await call(api, agent, session_id=f"post-block-{index}")
        assert response.status_code == 402, "a request slipped through after exhaustion"
    after = (await mock.get("/__mock__/stats")).json()["requests_total"]

    assert after == before, (
        f"{after - before} blocked request(s) still reached the provider — "
        "enforcement happened after dispatch, not before it"
    )


@pytest.mark.criteria
@pytest.mark.asyncio
async def test_session_breach_closes_session_without_touching_agent_budget(
    api, mock, make_agent
):
    """Criterion 4: a session limit breach closes that session only.

    The agent's monthly budget must be untouched, and a new session must work
    immediately — otherwise a per-session cap would be indistinguishable from
    an agent-level one.
    """
    # Session cap far below the monthly budget, so the session breaks first.
    #
    # Substitution is disabled deliberately: with it on, a session approaching
    # its cap degrades to a cheaper model rather than breaching (see
    # test_substitution_extends_a_session_instead_of_breaching_it below), and
    # this test would be measuring the substitution ladder instead of the
    # session rule it is named after.
    agent = await make_agent(
        monthly_usd=5.00, session_usd=0.01, allow_substitution=False
    )

    ok = await call(api, agent, session_id="tight", max_tokens=300)
    assert ok.status_code == 200

    # Snapshot immediately before each attempt: the calls that succeed on the
    # way to the breach charge the agent legitimately, so only the spend across
    # the *rejected* call is meaningful here.
    breach = None
    spend_before = 0.0
    for _ in range(40):
        spend_before = (await agent_status(api, agent))["consumed_usd"]
        response = await call(api, agent, session_id="tight", max_tokens=300)
        if response.status_code == 402:
            breach = response
            break
    assert breach is not None, "session limit was never enforced"

    body = breach.json()["error"]
    assert body["type"] == "session_budget_exhausted"
    assert body["session_closed"] is True
    assert body["scope"] == "session"

    # The rejected call must not have charged the agent.
    spend_after = (await agent_status(api, agent))["consumed_usd"]
    assert spend_after == pytest.approx(spend_before, abs=1e-9), (
        "a rejected session request still consumed agent budget"
    )

    # The closed session stays closed…
    again = await call(api, agent, session_id="tight")
    assert again.status_code == 402
    assert again.json()["error"]["type"] in (
        "session_closed",
        "session_budget_exhausted",
    )

    # …but the agent is free to continue in a new one.
    fresh = await call(api, agent, session_id="a-new-session", max_tokens=200)
    assert fresh.status_code == 200, (
        "closing one session blocked the agent entirely — session scope leaked "
        "into agent scope"
    )


@pytest.mark.criteria
@pytest.mark.asyncio
async def test_substitution_extends_a_session_instead_of_breaching_it(
    api, mock, make_agent
):
    """The documented interaction between the session cap and substitution.

    An agent allowed to substitute does not hit its session cap and stop — it
    steps down the model ladder and keeps working within the same allowance.
    That is the brief's "reroute rather than hard-blocking immediately" applied
    at session scope, and it is why the test above disables substitution to
    measure the session rule on its own.
    """
    agent = await make_agent(
        monthly_usd=5.00, session_usd=0.01, model="gpt-4o", allow_substitution=True
    )

    served = []
    for _ in range(12):
        response = await call(api, agent, session_id="degrading", max_tokens=300)
        assert response.status_code == 200, response.text
        served.append(response.headers["x-budget-model-served"])

    assert served[0] == "gpt-4o", "should start on the requested model"
    assert served[-1] != "gpt-4o", (
        "the session filled up but never stepped down to a cheaper model"
    )
    # The session is still capped — it is being spent more slowly, not ignored.
    remaining = float(
        (await call(api, agent, session_id="degrading", max_tokens=300))
        .headers["x-budget-session-remaining-usd"]
    )
    assert remaining <= agent.session_usd


@pytest.mark.criteria
@pytest.mark.asyncio
async def test_rejected_requests_are_recorded_for_audit(api, mock, make_agent):
    """A refusal is itself an event worth keeping: it is the evidence that the
    controller did its job, and the trail an incident review follows."""
    agent = await make_agent(monthly_usd=0.01, session_usd=0.01)

    for index in range(60):
        response = await call(api, agent, session_id=f"x-{index}", max_tokens=400)
        if response.status_code == 402:
            break

    rejections = await agent_events(api, agent.id, "budget.rejected_budget")
    session_rejections = await agent_events(api, agent.id, "budget.rejected_session")
    assert rejections or session_rejections, "no rejection event was recorded"
