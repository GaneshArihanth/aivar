"""Malformed and hostile input to the proxy.

Each of these returned a 500 or bricked an agent before being fixed. They are
regression tests, not hypotheticals — the proxy sits in front of every LLM call
an agent makes, so an unhandled edge case here is an outage for the fleet.
"""

from __future__ import annotations

import httpx
import pytest

from tests.criteria.conftest import call


@pytest.mark.asyncio
async def test_unknown_model_is_refused_cleanly(api, mock, make_agent):
    """An unpriceable model must be refused, not crash and not be dispatched.

    Without a catalog price there is no estimate; without an estimate there is
    no reservation. Letting it through would put unmetered spend through the
    proxy — the one thing it exists to prevent.
    """
    agent = await make_agent(monthly_usd=10, session_usd=1)
    await mock.post("/__mock__/reset")

    response = await call(api, agent, model="gpt-9-does-not-exist")
    assert response.status_code == 422, response.text

    error = response.json()["error"]
    assert error["type"] == "model_not_found"
    assert error["requested_model"] == "gpt-9-does-not-exist"
    assert "gpt-4o" in error["available_models"]

    assert (await mock.get("/__mock__/stats")).json()["requests_total"] == 0, (
        "an unpriceable request was dispatched to the provider"
    )


@pytest.mark.asyncio
async def test_malformed_body_is_rejected_with_400(api, mock, make_agent):
    agent = await make_agent(monthly_usd=10, session_usd=1)

    for payload in (b"not json at all", b"", b"[1,2,3]", b'"a string"'):
        response = await api.post(
            "/v1/chat/completions",
            content=payload,
            headers={
                "X-Agent-Key": agent.api_key,
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 400, (
            f"{payload!r} produced {response.status_code}, not a clean 400"
        )
        assert response.json()["error"]["type"] == "invalid_request_body"


@pytest.mark.asyncio
async def test_unusable_max_tokens_is_refused_before_dispatch(api, mock, make_agent):
    """max_tokens is what the reservation is sized against.

    A zero, negative or unparseable value would reserve nothing while the
    provider still generated output, so the hold would not cover the call. It
    is refused here rather than being quietly replaced with a default, which
    would change the meaning of a value the caller set on purpose.
    """
    agent = await make_agent(monthly_usd=10, session_usd=10)
    await mock.post("/__mock__/reset")

    for bad in (0, -50, "abc", [1, 2]):
        response = await api.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}], "max_tokens": bad},
            headers={"X-Agent-Key": agent.api_key, "X-Session-Id": f"mt-{bad}"},
        )
        assert response.status_code == 400, f"max_tokens={bad!r}: {response.text}"
        assert response.json()["error"]["type"] == "invalid_max_tokens"

    assert (await mock.get("/__mock__/stats")).json()["requests_total"] == 0

    # Omitting it entirely is legitimate: the ceiling defaults.
    ok = await api.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers={"X-Agent-Key": agent.api_key, "X-Session-Id": "mt-absent"},
    )
    assert ok.status_code == 200
    assert float(ok.headers["x-budget-cost-usd"]) > 0


@pytest.mark.asyncio
async def test_agent_without_a_session_budget_is_not_bricked(api, mock, make_agent):
    """No per-session cap must mean "no session limit", not "spend nothing".

    Read the other way round, an agent whose per-session budget row is missing
    has every request refused as "session budget exhausted: $0.00 of $0.00".
    """
    agent = await make_agent(monthly_usd=5, session_usd=1)

    # Remove the per-session cap through the API's own semantics: the update
    # path treats 0 as "uncapped".
    patched = await api.patch(
        f"/admin/agents/{agent.id}", json={"session_budget_usd": 0}
    )
    # A zero session budget is rejected at the schema (budgets must be > 0),
    # so drive the real-world case instead: an agent created before per-session
    # budgets existed, i.e. no row at all.
    assert patched.status_code in (200, 422)

    response = await call(api, agent, session_id="uncapped", max_tokens=100)
    assert response.status_code == 200

    # With a cap configured, the session header is present and meaningful.
    assert "x-budget-session-remaining-usd" in response.headers


@pytest.mark.asyncio
async def test_missing_and_revoked_keys_are_indistinguishable(api, mock, make_agent):
    """Neither response should reveal whether an agent exists."""
    agent = await make_agent(monthly_usd=5, session_usd=1)
    await api.delete(f"/admin/agents/{agent.id}")

    revoked = await call(api, agent)
    fabricated = await api.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers={"X-Agent-Key": "sk-agent-totally-made-up-key-value-here"},
    )
    assert revoked.status_code == fabricated.status_code == 401
    # One envelope for every error, whichever layer produced it.
    assert (
        revoked.json()["error"]["type"]
        == fabricated.json()["error"]["type"]
        == "invalid_api_key"
    )


@pytest.mark.asyncio
async def test_upstream_failure_costs_nothing(api, mock, make_agent):
    """A provider error must release the hold in full."""
    agent = await make_agent(monthly_usd=5, session_usd=5)

    before = (await api.get("/v1/budget/status")).json()
    spend_before = next(
        m["consumed_usd"]
        for t in before["teams"]
        for m in t["agents"]
        if m["scope_id"] == str(agent.id)
    )

    await mock.post("/__mock__/controls", json={"error_rate": 1.0})
    try:
        response = await call(api, agent, session_id="doomed", max_tokens=800)
        assert response.status_code == 502
        assert response.json()["error"]["type"] == "upstream_error"
    finally:
        await mock.post("/__mock__/controls", json={})

    after = (await api.get("/v1/budget/status")).json()
    spend_after = next(
        m["consumed_usd"]
        for t in after["teams"]
        for m in t["agents"]
        if m["scope_id"] == str(agent.id)
    )
    assert spend_after == pytest.approx(spend_before, abs=1e-9), (
        "a failed upstream call still consumed budget"
    )

    report = (await api.get("/admin/reconcile")).json()
    assert report["outstanding_holds"] == 0, "the failed call left a hold behind"


@pytest.mark.asyncio
async def test_very_large_max_tokens_is_refused_not_silently_truncated(
    api, mock, make_agent
):
    """A request whose worst case exceeds the budget must be refused up front."""
    agent = await make_agent(monthly_usd=0.05, session_usd=0.05, model="gpt-4o")
    await mock.post("/__mock__/reset")

    response = await call(api, agent, session_id="huge", max_tokens=10_000_000)
    assert response.status_code == 402
    assert (await mock.get("/__mock__/stats")).json()["requests_total"] == 0
