"""Success criterion 5 — model substitution under budget pressure."""

from __future__ import annotations

import pytest

from tests.criteria.conftest import agent_status, call


async def _spend_until_pressure(api, agent, target_pct: float = 0.93) -> float:
    """Put an agent just past its substitution threshold, deterministically.

    Spend a little, then lower the monthly budget so the existing spend lands
    at ``target_pct`` of it. Grinding thousands of small calls upward would be
    slower and would land on an unpredictable percentage — and it happens to
    exercise the "budget lowered below current usage" path at the same time.
    """
    for index in range(3):
        response = await call(
            api, agent, session_id=f"warm-{index}", max_tokens=200, model="gpt-4o"
        )
        assert response.status_code == 200, response.text

    consumed = (await agent_status(api, agent))["consumed_usd"]
    assert consumed > 0
    await api.patch(
        f"/admin/agents/{agent.id}",
        json={"monthly_budget_usd": round(consumed / target_pct, 6)},
    )
    return (await agent_status(api, agent))["pct"]


@pytest.mark.criteria
@pytest.mark.asyncio
async def test_substitution_reroutes_to_a_cheaper_model_in_the_same_provider(
    api, mock, make_agent
):
    """Criterion 5: under pressure, requests reroute to the cheaper model.

    Asserted from the provider's side as well as the proxy's: the mock must
    have actually received `gpt-4o-mini`. A response header claiming
    substitution while the expensive model was still called would be worse
    than no substitution at all.
    """
    agent = await make_agent(monthly_usd=0.05, session_usd=0.05, model="gpt-4o")

    reached = await _spend_until_pressure(api, agent)
    assert reached >= 0.85, f"could not drive the agent into pressure (at {reached:.2%})"

    await mock.post("/__mock__/reset")

    response = await call(
        api, agent, session_id="under-pressure", max_tokens=100, model="gpt-4o"
    )
    assert response.status_code == 200, response.text

    assert response.headers["x-budget-model-requested"] == "gpt-4o"
    served = response.headers["x-budget-model-served"]
    assert served != "gpt-4o", "no substitution happened under pressure"
    assert served in ("gpt-4o-mini", "gpt-4.1-nano")
    assert "budget_pressure" in response.headers.get("x-budget-substitution-reason", "")

    # The provider's own view must agree with the disclosure header.
    by_model = (await mock.get("/__mock__/stats")).json()["requests_by_model"]
    assert by_model.get(served, 0) >= 1
    assert by_model.get("gpt-4o", 0) == 0, (
        "the expensive model was called despite reporting a substitution"
    )

    # The response body reports the model that actually served it.
    assert response.json()["model"] == served


@pytest.mark.criteria
@pytest.mark.asyncio
async def test_substitution_is_disclosed_in_the_ledger(api, mock, make_agent):
    """A silent model swap is a correctness change the agent's owner must see."""
    agent = await make_agent(monthly_usd=0.05, session_usd=0.05, model="gpt-4o")
    await _spend_until_pressure(api, agent)

    response = await call(api, agent, session_id="disclose", max_tokens=100)
    assert response.status_code == 200

    calls = (await api.get("/v1/budget/calls?limit=100")).json()
    mine = [c for c in calls if c["agent_id"] == agent.id and c["substituted"]]
    assert mine, "the substitution was not recorded in the call ledger"

    row = mine[0]
    assert row["requested_model"] == "gpt-4o"
    assert row["served_model"] != "gpt-4o"
    assert row["decision"] == "substituted"
    # The cost recorded must be the cheaper model's, not the one asked for.
    assert row["cost_usd"] < 0.01


@pytest.mark.criteria
@pytest.mark.asyncio
async def test_multi_step_cross_provider_chain_degrades_all_the_way_down(
    api, mock, make_agent
):
    """A four-step ladder that crosses three providers, walked end to end.

    Asserted from the provider's side: the mock must have been asked for each
    model in turn. A chain that only *reports* stepping down would be worse
    than none at all.
    """
    agent = await make_agent(monthly_usd=0.20, session_usd=0.20, model="gpt-4o")

    await api.patch(f"/admin/agents/{agent.id}", json={"allow_cross_provider": True})
    chain = ["gpt-4o", "claude-haiku-4-5", "gpt-4o-mini", "gemini-3.5-flash-lite"]
    saved = await api.put(f"/admin/agents/{agent.id}/chain", json={"chain": chain})
    assert saved.status_code == 200, saved.text
    assert saved.json()["crosses_providers"] is True

    await mock.post("/__mock__/reset")

    # Squeeze the remaining headroom to a chosen size, so which rung can afford
    # the next call is arithmetic rather than luck. Grinding calls until the
    # budget happens to tighten would step through the ladder in jumps and skip
    # rungs, proving only that *some* substitution occurred.
    #
    # At max_tokens=400 a reservation costs roughly:
    #   gpt-4o $0.00404 · claude-haiku $0.00161 · gpt-4o-mini $0.00024 ·
    #   gemini-3.5-flash-lite $0.00012
    async def squeeze(headroom_usd: float) -> None:
        consumed = (await agent_status(api, agent))["consumed_usd"]
        # Pressure engages at 90%, so leave exactly `headroom` below that line.
        await api.patch(
            f"/admin/agents/{agent.id}",
            json={"monthly_budget_usd": round((consumed + headroom_usd) / 0.9, 8)},
        )

    expectations = [
        (0.0030, "claude-haiku-4-5"),        # too tight for gpt-4o
        (0.0005, "gpt-4o-mini"),             # too tight for claude as well
        (0.0002, "gemini-3.5-flash-lite"),   # only the last rung fits
    ]

    first = await call(api, agent, session_id="ladder-head", max_tokens=400)
    assert first.status_code == 200
    assert first.headers["x-budget-model-served"] == "gpt-4o", "should start at the top"

    for headroom, expected in expectations:
        await squeeze(headroom)
        response = await call(api, agent, session_id=f"ladder-{expected}", max_tokens=400)
        assert response.status_code == 200, response.text
        assert response.headers["x-budget-model-served"] == expected, (
            f"with ${headroom} of headroom the ladder should have reached {expected}, "
            f"but served {response.headers['x-budget-model-served']}"
        )
        assert response.headers["x-budget-model-requested"] == "gpt-4o"

    # The provider actually saw each rung — not just the headers claiming so.
    by_model = (await mock.get("/__mock__/stats")).json()["requests_by_model"]
    for model in chain:
        assert by_model.get(model, 0) > 0, f"{model} was reported but never dispatched"


@pytest.mark.criteria
@pytest.mark.asyncio
async def test_cross_provider_is_refused_without_the_opt_in(api, mock, make_agent):
    """The guard is per agent, and it is off unless someone turns it on."""
    agent = await make_agent(monthly_usd=1.0, session_usd=1.0, model="gpt-4o")

    response = await api.put(
        f"/admin/agents/{agent.id}/chain",
        json={"chain": ["gpt-4o", "claude-haiku-4-5"]},
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["type"] == "invalid_chain"
    assert error["position"] == 1

    # The agent's stored chain is untouched by the refusal.
    current = (await api.get(f"/admin/agents/{agent.id}/chain")).json()
    assert current["crosses_providers"] is False


@pytest.mark.criteria
@pytest.mark.asyncio
async def test_chain_can_be_rebuilt_from_the_catalog(api, mock, make_agent):
    agent = await make_agent(monthly_usd=1.0, session_usd=1.0, model="gpt-4o")

    await api.put(f"/admin/agents/{agent.id}/chain", json={"chain": ["gpt-4o"]})
    assert (await api.get(f"/admin/agents/{agent.id}/chain")).json()["is_custom"] is True

    rebuilt = (await api.post(f"/admin/agents/{agent.id}/chain/auto")).json()
    assert rebuilt["is_custom"] is False
    assert rebuilt["chain"][0] == "gpt-4o"
    assert len(rebuilt["chain"]) > 1
    # Auto-derivation stays inside the provider unless crossing is permitted.
    assert rebuilt["crosses_providers"] is False


@pytest.mark.criteria
@pytest.mark.asyncio
async def test_substitution_disabled_hard_blocks_instead_of_downgrading(
    api, mock, make_agent
):
    """An agent that opts out must never be silently downgraded.

    Some outputs feed contract-sensitive flows where a cheaper model is not an
    acceptable substitute — for those, refusing is the correct behaviour.
    """
    agent = await make_agent(
        monthly_usd=0.02, session_usd=0.02, model="gpt-4o", allow_substitution=False
    )
    await mock.post("/__mock__/reset")

    outcome = None
    for index in range(80):
        response = await call(api, agent, session_id=f"n-{index}", max_tokens=400)
        if response.status_code != 200:
            outcome = response
            break
        assert response.headers["x-budget-model-served"] == "gpt-4o", (
            "an agent with allow_substitution=false was downgraded anyway"
        )

    assert outcome is not None and outcome.status_code == 402
    assert outcome.json()["error"]["type"] == "budget_exhausted"

    by_model = (await mock.get("/__mock__/stats")).json()["requests_by_model"]
    assert set(by_model) <= {"gpt-4o"}, f"unexpected models were called: {by_model}"
