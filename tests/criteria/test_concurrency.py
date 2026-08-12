"""Success criterion 1 — accurate tracking across concurrent agents.

The assertion is exact agreement between the two stores: what Redis enforced
against, and what the PostgreSQL ledger recorded. A budget system whose numbers
drift under load is not a budget system, it is an estimate.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.criteria.conftest import agent_status, call


@pytest.mark.criteria
@pytest.mark.asyncio
async def test_three_concurrent_agents_track_accurately(api, mock, make_agent):
    """Criterion 1: budget tracks correctly across 3 agents making rapid calls."""
    agents = [
        await make_agent(monthly_usd=2.00, session_usd=2.00, model="gpt-4o-mini")
        for _ in range(3)
    ]

    calls_each = 40

    async def hammer(agent, tag: str):
        return await asyncio.gather(
            *(
                call(api, agent, session_id=f"{tag}-{i % 4}", max_tokens=200)
                for i in range(calls_each)
            )
        )

    results = await asyncio.gather(
        *(hammer(agent, f"a{index}") for index, agent in enumerate(agents))
    )

    # Everything should have been served: the budgets are ample for this volume.
    for index, responses in enumerate(results):
        codes = {r.status_code for r in responses}
        assert codes == {200}, f"agent {index} saw unexpected statuses: {codes}"

    # Redis and the ledger must agree exactly, per agent and overall.
    report = (await api.get("/admin/reconcile")).json()
    assert report["outstanding_holds"] == 0, (
        f"{report['outstanding_holds']} reservations never settled"
    )

    ours = {str(a.id) for a in agents}
    for row in report["agents"]:
        if str(row["agent_id"]) not in ours:
            continue
        assert row["drift_micros"] == 0, (
            f"agent {row['name']}: Redis says {row['redis_micros']}µ$, "
            f"ledger says {row['ledger_micros']}µ$ — the counters drifted"
        )

    # Each agent's spend is its own: no cross-contamination between agents.
    for agent in agents:
        state = await agent_status(api, agent)
        assert state["consumed_usd"] > 0
        assert state["calls_today"] >= calls_each


@pytest.mark.criteria
@pytest.mark.asyncio
async def test_concurrent_requests_cannot_exceed_the_limit(api, mock, make_agent):
    """The overspend case: far more concurrent demand than budget.

    This is the 50,000-calls-overnight scenario compressed into a second. With
    a read-then-write counter, concurrent requests all observe the same stale
    total and sail past the limit; the ledger below would then exceed it.

    Substitution is off so the arithmetic is exact. With it on, requests the
    budget cannot afford at gpt-4o prices get rerouted down the ladder and
    served anyway — correct behaviour, but it makes "how many were refused" a
    function of how many raced before the ladder engaged, which is timing, not
    enforcement.
    """
    agent = await make_agent(
        monthly_usd=0.05, session_usd=0.05, model="gpt-4o", allow_substitution=False
    )

    responses = await asyncio.gather(
        *(call(api, agent, session_id=f"flood-{i}", max_tokens=500) for i in range(120))
    )

    served = [r for r in responses if r.status_code == 200]
    refused = [r for r in responses if r.status_code == 402]

    # The number admitted is deliberately not asserted exactly. Each call holds
    # its worst case (5,000 µ$) and refunds the unused part on settle, so a
    # request arriving after earlier ones have settled finds budget that was
    # briefly reserved and then given back. How many arrive in that window
    # depends on the client's connection pool, not on the enforcement — and
    # freeing unspent budget promptly is the behaviour we want.
    #
    # What must hold regardless is the invariant below: total spend never
    # exceeds the limit, and demand far above the budget is refused.
    assert served, "nothing was served at all"
    assert refused, "nothing was refused — the limit was never reached"

    state = await agent_status(api, agent)
    assert state["consumed_usd"] <= agent.monthly_usd + 1e-9, (
        f"spent ${state['consumed_usd']:.6f} against a ${agent.monthly_usd} limit — "
        "concurrent requests overshot the budget"
    )

    report = (await api.get("/admin/reconcile")).json()
    row = next(r for r in report["agents"] if str(r["agent_id"]) == str(agent.id))
    assert row["drift_micros"] == 0
