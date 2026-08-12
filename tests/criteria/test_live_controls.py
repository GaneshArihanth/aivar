"""Live controls: freeze kill switches, budget boosts, and rate limits.

All three are evaluated inside ``reserve.lua``. A freeze checked in Python
would not stop requests already in flight between that check and the
increment — precisely the traffic an operator is trying to stop.
"""

from __future__ import annotations

import asyncio
import time

import pytest
import pytest_asyncio

from tests.criteria.conftest import agent_status, call


@pytest_asyncio.fixture(autouse=True)
async def thaw(api):
    """Never leave the system frozen, whatever a test does."""
    yield
    await api.delete("/admin/freeze")


async def in_one_rate_window(margin_seconds: int = 10) -> None:
    """Wait out a minute boundary if one is close.

    The RPM/TPM limiters are fixed windows keyed by minute, so an allowance
    resets on the boundary. A test whose calls straddle it sees the counter
    start again and reads that as "the limit did not apply" — a flake, and a
    misleading one. Waits at most `margin_seconds`.
    """
    into_minute = int(time.time()) % 60
    remaining = 60 - into_minute
    if remaining <= margin_seconds:
        await asyncio.sleep(remaining + 0.2)


async def exhaust(api, agent, *, max_tokens=400, limit=120):
    """Spend until the agent is refused. Returns the refusing response."""
    for index in range(limit):
        response = await call(api, agent, session_id=f"burn-{index}", max_tokens=max_tokens)
        if response.status_code == 402:
            return response
    raise AssertionError("never reached the budget limit")


# ------------------------------------------------------------------ freeze


@pytest.mark.asyncio
async def test_global_freeze_stops_dispatch_immediately(api, mock, make_agent):
    agent = await make_agent(monthly_usd=5, session_usd=5)
    assert (await call(api, agent, session_id="pre")).status_code == 200

    await api.post("/admin/freeze", json={"reason": "provider incident", "actor": "sre"})
    await mock.post("/__mock__/reset")

    response = await call(api, agent, session_id="during")
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["type"] == "dispatch_frozen"
    assert error["frozen"] is True
    # Retryable, not a permanent refusal — nothing is wrong with the request.
    assert response.headers["retry-after"]

    assert (await mock.get("/__mock__/stats")).json()["requests_total"] == 0, (
        "a frozen request still reached the provider"
    )

    status = (await api.get("/admin/freeze")).json()
    assert status["global"]["frozen"] is True
    assert status["global"]["reason"] == "provider incident"

    await api.delete("/admin/freeze")
    assert (await call(api, agent, session_id="after")).status_code == 200


@pytest.mark.asyncio
async def test_freeze_takes_effect_against_sustained_concurrent_traffic(
    api, mock, make_agent
):
    """What a freeze actually guarantees, stated precisely.

    It stops every *reservation* from the instant it is set. It cannot recall a
    request already handed to the provider — nothing can — so the guarantee is
    about admission, not about the calls already in flight upstream.

    That is also why it lives in Lua: a freeze evaluated in Python would leave a
    window between the check and the increment, and under concurrency that
    window is where requests slip through.
    """
    agent = await make_agent(monthly_usd=50, session_usd=50)

    frozen_at_index: dict[str, int] = {}

    async def stream():
        """A steady stream, long enough to still be running when the freeze lands."""
        codes = []
        for index in range(60):
            response = await call(api, agent, session_id=f"stream-{index}")
            codes.append(response.status_code)
            if response.status_code == 503 and "first_503" not in frozen_at_index:
                frozen_at_index["first_503"] = index
        return codes

    task = asyncio.create_task(stream())
    await asyncio.sleep(0.6)
    await api.post("/admin/freeze", json={"reason": "mid-stream"})
    codes = await task

    assert 200 in codes, "nothing was served before the freeze"
    assert 503 in codes, "the freeze never took effect on the running stream"

    # Once frozen, nothing is admitted again — no flapping, no leakage.
    first = frozen_at_index["first_503"]
    assert set(codes[first:]) == {503}, (
        f"requests were admitted after the freeze: {codes[first:]}"
    )

    after = await asyncio.gather(
        *(call(api, agent, session_id=f"post-{i}") for i in range(15))
    )
    assert {r.status_code for r in after} == {503}


@pytest.mark.asyncio
async def test_team_freeze_is_scoped_to_that_team(api, mock, make_agent):
    one = await make_agent(monthly_usd=5, session_usd=5)
    two = await make_agent(monthly_usd=5, session_usd=5)
    assert one.team_id != two.team_id

    await api.post(f"/admin/teams/{one.team_id}/freeze", json={"reason": "runaway product"})
    try:
        frozen = await call(api, one, session_id="frozen")
        assert frozen.status_code == 503
        assert frozen.json()["error"]["scope"] == "team"

        # The other team is untouched.
        assert (await call(api, two, session_id="unaffected")).status_code == 200
    finally:
        await api.delete(f"/admin/teams/{one.team_id}/freeze")

    assert (await call(api, one, session_id="thawed")).status_code == 200


# ------------------------------------------------------------------- boost


@pytest.mark.asyncio
async def test_boost_lifts_an_exhausted_agent_without_moving_its_baseline(
    api, mock, make_agent
):
    """The emergency case: a critical job needs to finish, and the monthly
    limit must not be quietly rewritten to let it."""
    agent = await make_agent(monthly_usd=0.02, session_usd=0.02, allow_substitution=False)
    await exhaust(api, agent)

    baseline = (await api.get(f"/admin/agents/{agent.id}")).json()["monthly_budget_usd"]
    assert baseline == pytest.approx(0.02)

    granted = await api.post(
        f"/admin/agents/{agent.id}/boost",
        json={"amount_usd": 0.05, "reason": "finish the nightly reconciliation", "hours": 2},
    )
    assert granted.status_code == 200
    assert granted.json()["granted_usd"] == pytest.approx(0.05)

    resumed = await call(api, agent, session_id="after-boost")
    assert resumed.status_code == 200, "the boost did not restore service"

    # The baseline is exactly as it was — the boost is an exception, not an edit.
    after = (await api.get(f"/admin/agents/{agent.id}")).json()
    assert after["monthly_budget_usd"] == pytest.approx(baseline)

    state = (await api.get(f"/admin/agents/{agent.id}/boost")).json()
    assert state["active_boost_usd"] == pytest.approx(0.05)
    assert state["expires_in_seconds"] > 0
    assert state["grants"][0]["reason"].startswith("finish the nightly")


@pytest.mark.asyncio
async def test_boosts_accumulate_rather_than_replace(api, mock, make_agent):
    """Pressing the button twice during an incident must not undo the first."""
    agent = await make_agent(monthly_usd=0.02, session_usd=0.02)
    for amount in (0.01, 0.02):
        await api.post(
            f"/admin/agents/{agent.id}/boost",
            json={"amount_usd": amount, "reason": "incident"},
        )
    state = (await api.get(f"/admin/agents/{agent.id}/boost")).json()
    assert state["active_boost_usd"] == pytest.approx(0.03)
    assert len(state["grants"]) == 2


@pytest.mark.asyncio
async def test_boost_can_be_revoked(api, mock, make_agent):
    agent = await make_agent(monthly_usd=0.02, session_usd=0.02, allow_substitution=False)
    await exhaust(api, agent)
    await api.post(
        f"/admin/agents/{agent.id}/boost", json={"amount_usd": 0.05, "reason": "oops"}
    )
    assert (await call(api, agent, session_id="boosted")).status_code == 200

    await api.delete(f"/admin/agents/{agent.id}/boost")
    assert (await call(api, agent, session_id="revoked")).status_code == 402


# ------------------------------------------------------------- rate limits


@pytest.mark.asyncio
async def test_rpm_limit_admits_exactly_the_allowance(api, mock, make_agent):
    """The same atomicity requirement as the budget: concurrent requests must
    not all read the same stale count and each conclude they have room."""
    agent = await make_agent(monthly_usd=50, session_usd=50)
    await api.patch(f"/admin/agents/{agent.id}", json={"rpm_limit": 5})
    await in_one_rate_window()

    results = await asyncio.gather(
        *(call(api, agent, session_id=f"rpm-{i}") for i in range(40))
    )
    served = [r for r in results if r.status_code == 200]
    limited = [r for r in results if r.status_code == 429]

    assert len(served) == 5, f"expected exactly 5 admitted, got {len(served)}"
    assert len(limited) == 35

    error = limited[0].json()["error"]
    assert error["type"] == "rate_limited"
    assert error["scope"] == "rpm"
    assert limited[0].headers["retry-after"]


@pytest.mark.asyncio
async def test_rate_limiting_does_not_consume_budget(api, mock, make_agent):
    """A pacing refusal is not a spend. The budget must be untouched by it."""
    agent = await make_agent(monthly_usd=50, session_usd=50)
    await api.patch(f"/admin/agents/{agent.id}", json={"rpm_limit": 2})
    await in_one_rate_window()

    await call(api, agent, session_id="one")
    await call(api, agent, session_id="two")
    before = (await agent_status(api, agent))["consumed_usd"]

    refused = await call(api, agent, session_id="three")
    assert refused.status_code == 429

    after = (await agent_status(api, agent))["consumed_usd"]
    assert after == pytest.approx(before, abs=1e-9)

    report = (await api.get("/admin/reconcile")).json()
    assert report["outstanding_holds"] == 0, "a rate-limited request left a hold"


@pytest.mark.asyncio
async def test_tpm_limit_counts_tokens_not_requests(api, mock, make_agent):
    agent = await make_agent(monthly_usd=50, session_usd=50)
    # Room for roughly one 400-token call, not two.
    await api.patch(f"/admin/agents/{agent.id}", json={"tpm_limit": 500})
    await in_one_rate_window()

    first = await call(api, agent, session_id="tpm-1", max_tokens=400)
    assert first.status_code == 200

    second = await call(api, agent, session_id="tpm-2", max_tokens=400)
    assert second.status_code == 429
    assert second.json()["error"]["scope"] == "tpm"


@pytest.mark.asyncio
async def test_rate_limits_are_off_by_default(api, mock, make_agent):
    agent = await make_agent(monthly_usd=50, session_usd=50)
    results = await asyncio.gather(
        *(call(api, agent, session_id=f"free-{i}") for i in range(15))
    )
    assert {r.status_code for r in results} == {200}


@pytest.mark.asyncio
async def test_budget_refusal_is_reported_before_a_rate_refusal(api, mock, make_agent):
    """When both would refuse, the budget is the more fundamental problem and
    the one worth surfacing."""
    agent = await make_agent(monthly_usd=0.02, session_usd=0.02, allow_substitution=False)
    await api.patch(f"/admin/agents/{agent.id}", json={"rpm_limit": 1000})
    response = await exhaust(api, agent)
    assert response.json()["error"]["type"] == "budget_exhausted"
