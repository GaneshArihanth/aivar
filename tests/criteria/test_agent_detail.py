"""Agent detail page data: spend history, token economics, live sessions."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.criteria.conftest import call


@pytest.mark.asyncio
async def test_history_buckets_align_to_real_utc_hours(api, mock, make_agent):
    """Buckets must land on the hour in UTC.

    PostgreSQL's date_trunc works in the *session* time zone. On a host at
    +05:30 that puts every "hour" boundary at :30 past the hour in UTC, so the
    buckets match nothing on a UTC axis and the chart is silently always empty
    — everywhere except a UTC server, where the bug is invisible.
    """
    agent = await make_agent(monthly_usd=5, session_usd=5)
    for index in range(3):
        assert (await call(api, agent, session_id=f"hist-{index}")).status_code == 200

    payload = (await api.get(f"/admin/agents/{agent.id}/history?days=7")).json()
    assert payload["granularity"] == "hour"

    non_empty = [point for point in payload["series"] if point["calls"]]
    assert non_empty, "the calls just made are missing from the history"

    for point in non_empty:
        moment = datetime.fromisoformat(point["bucket"]).astimezone(UTC)
        assert (moment.minute, moment.second) == (0, 0), (
            f"bucket {point['bucket']} is not on a UTC hour boundary"
        )

    assert sum(point["calls"] for point in payload["series"]) >= 3


@pytest.mark.asyncio
async def test_history_series_is_dense_across_the_window(api, mock, make_agent):
    """Quiet hours are present as zeros. A sparse series would draw one busy
    hour as a bar spanning the whole chart."""
    agent = await make_agent(monthly_usd=5, session_usd=5)
    await call(api, agent, session_id="dense")

    seven = (await api.get(f"/admin/agents/{agent.id}/history?days=7")).json()
    assert len(seven["series"]) == 168, "7 days of hourly buckets"

    ninety = (await api.get(f"/admin/agents/{agent.id}/history?days=90")).json()
    assert ninety["granularity"] == "day"
    assert len(ninety["series"]) == 90

    buckets = [datetime.fromisoformat(p["bucket"]) for p in seven["series"]]
    gaps = {(b - a).total_seconds() for a, b in zip(buckets, buckets[1:])}
    assert gaps == {3600.0}, f"buckets are not evenly spaced: {gaps}"


@pytest.mark.asyncio
async def test_history_rejects_an_arbitrary_window(api, make_agent):
    agent = await make_agent(monthly_usd=1, session_usd=1)
    response = await api.get(f"/admin/agents/{agent.id}/history?days=13")
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "invalid_window"


@pytest.mark.asyncio
async def test_totals_report_token_split_and_latency(api, mock, make_agent):
    agent = await make_agent(monthly_usd=5, session_usd=5)
    for index in range(4):
        await call(api, agent, session_id=f"tok-{index}", max_tokens=300)

    totals = (await api.get(f"/admin/agents/{agent.id}/history?days=7")).json()["totals"]
    assert totals["calls"] >= 4
    assert totals["prompt_tokens"] > 0
    assert totals["completion_tokens"] > totals["prompt_tokens"], (
        "generation should dominate a short prompt"
    )
    assert totals["avg_latency_ms"] > 0
    assert totals["max_latency_ms"] >= totals["avg_latency_ms"]
    assert totals["decisions"].get("allowed", 0) >= 4
    assert totals["by_model"] and totals["by_model"][0]["usd"] > 0
    # Costs are reported in dollars only; raw micro-dollars are internal.
    assert "micros" not in totals["by_model"][0]


@pytest.mark.asyncio
async def test_sessions_are_listed_and_can_be_terminated(api, mock, make_agent):
    """Sessions previously existed only as TTL'd Redis keys with no index, so
    there was no way to see or stop one."""
    agent = await make_agent(monthly_usd=5, session_usd=5)
    await call(api, agent, session_id="alpha")
    await call(api, agent, session_id="beta")

    listing = (await api.get(f"/admin/agents/{agent.id}/sessions")).json()["sessions"]
    ids = {s["session_id"] for s in listing}
    assert {"alpha", "beta"} <= ids
    assert all(s["status"] == "open" for s in listing if s["session_id"] in ids)
    assert any(s["spend_usd"] > 0 for s in listing)

    terminated = await api.delete(f"/admin/agents/{agent.id}/sessions/alpha")
    assert terminated.status_code == 200

    # The terminated session is refused…
    blocked = await call(api, agent, session_id="alpha")
    assert blocked.status_code == 402
    assert blocked.json()["error"]["type"] == "session_closed"

    # …while the agent carries on everywhere else.
    assert (await call(api, agent, session_id="beta")).status_code == 200
    assert (await call(api, agent, session_id="gamma")).status_code == 200

    after = (await api.get(f"/admin/agents/{agent.id}/sessions")).json()["sessions"]
    alpha = next(s for s in after if s["session_id"] == "alpha")
    assert alpha["status"] == "closed"
    assert alpha["close_reason"] == "terminated_by_operator"


@pytest.mark.asyncio
async def test_terminating_a_session_does_not_touch_the_monthly_budget(
    api, mock, make_agent
):
    agent = await make_agent(monthly_usd=5, session_usd=5)
    await call(api, agent, session_id="doomed")

    def spend_of(payload):
        for team in payload["teams"]:
            for member in team["agents"]:
                if member["scope_id"] == str(agent.id):
                    return member["consumed_usd"]
        raise AssertionError("agent missing from status")

    before = spend_of((await api.get("/v1/budget/status")).json())
    await api.delete(f"/admin/agents/{agent.id}/sessions/doomed")
    after = spend_of((await api.get("/v1/budget/status")).json())

    assert after == pytest.approx(before, abs=1e-9)
