"""The reservation core under concurrency.

The headline test is ``test_concurrent_reserves_admit_exactly_the_limit``: it is
the one that distinguishes this design from a read-compare-write middleware.
Run it against a Python implementation that reads the counter, compares, then
increments, and it fails — that is the 50,000-overnight-calls bug, reproduced
in a few hundred milliseconds.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.core.budget import Decision, release, reserve, settle
from app.redisx import keys
from tests.conftest import TEST_AGENT_ID, TEST_TEAM_ID, make_policy, seed_limits

PERIOD = "2026-08"


async def _reserve_one(session_id: str, estimate: int, policy, *, final=True):
    return await reserve(
        team_id=TEST_TEAM_ID,
        agent_id=TEST_AGENT_ID,
        session_id=session_id,
        request_id=uuid.uuid4().hex,
        model="gpt-4o",
        estimate_micros=estimate,
        policy=policy,
        allow_substitution=False,
        final_attempt=final,
        period=PERIOD,
    )


@pytest.mark.asyncio
async def test_concurrent_reserves_admit_exactly_the_limit(redis_gateway):
    """500 simultaneous reservations against room for exactly 100.

    Any interleaving of check and increment lets more than 100 through. The
    assertion is exact — not "roughly 100", not "no more than 105".
    """
    limit = 100
    policy = make_policy(monthly=limit, per_session=10**9, team_monthly=10**9)
    await seed_limits(
        redis_gateway,
        team_id=TEST_TEAM_ID,
        agent_id=TEST_AGENT_ID,
        team_limit=10**9,
        agent_limit=limit,
        period=PERIOD,
    )

    results = await asyncio.gather(
        *(_reserve_one(f"sess-{i}", 1, policy) for i in range(500))
    )

    admitted = [r for r in results if r.allowed]
    rejected = [r for r in results if r.status is Decision.EXHAUSTED]

    assert len(admitted) == limit, (
        f"expected exactly {limit} admitted, got {len(admitted)} — "
        "the check and the increment were not atomic"
    )
    assert len(rejected) == 500 - limit

    final_spend = int(
        await redis_gateway.client.get(
            keys.agent_spend(TEST_TEAM_ID, TEST_AGENT_ID, PERIOD)
        )
    )
    assert final_spend == limit, "counter drifted from the number of admissions"


@pytest.mark.asyncio
async def test_concurrent_reserves_with_varied_costs_never_exceed_limit(redis_gateway):
    """Uneven request sizes must not overshoot, even by one micro-dollar."""
    limit = 10_000
    policy = make_policy(monthly=limit, per_session=10**9, team_monthly=10**9)
    await seed_limits(
        redis_gateway,
        team_id=TEST_TEAM_ID,
        agent_id=TEST_AGENT_ID,
        team_limit=10**9,
        agent_limit=limit,
        period=PERIOD,
    )

    costs = [7, 13, 101, 499, 1_000] * 60  # 300 requests, 124,000 µ$ if all ran
    results = await asyncio.gather(
        *(_reserve_one(f"s-{i}", c, policy) for i, c in enumerate(costs))
    )

    admitted_total = sum(c for c, r in zip(costs, results) if r.allowed)
    stored = int(
        await redis_gateway.client.get(
            keys.agent_spend(TEST_TEAM_ID, TEST_AGENT_ID, PERIOD)
        )
    )
    assert stored == admitted_total
    assert stored <= limit


@pytest.mark.asyncio
async def test_all_or_nothing_across_scopes(redis_gateway):
    """A session breach must not consume agent or team budget."""
    policy = make_policy(monthly=10**6, per_session=50, team_monthly=10**6)
    await seed_limits(
        redis_gateway,
        team_id=TEST_TEAM_ID,
        agent_id=TEST_AGENT_ID,
        team_limit=10**6,
        agent_limit=10**6,
        period=PERIOD,
    )

    ok = await _reserve_one("tight-session", 40, policy)
    assert ok.allowed

    agent_before = int(
        await redis_gateway.client.get(
            keys.agent_spend(TEST_TEAM_ID, TEST_AGENT_ID, PERIOD)
        )
    )

    breach = await _reserve_one("tight-session", 40, policy)  # 80 > 50
    assert breach.status is Decision.SESSION_EXHAUSTED

    agent_after = int(
        await redis_gateway.client.get(
            keys.agent_spend(TEST_TEAM_ID, TEST_AGENT_ID, PERIOD)
        )
    )
    assert agent_after == agent_before, (
        "the rejected request charged the agent anyway — the scopes were not "
        "committed atomically"
    )

    # The session is closed, but a fresh session for the same agent works.
    again = await _reserve_one("tight-session", 1, policy)
    assert again.status is Decision.SESSION_CLOSED
    fresh = await _reserve_one("brand-new-session", 40, policy)
    assert fresh.allowed


@pytest.mark.asyncio
async def test_settle_refunds_the_difference(redis_gateway):
    policy = make_policy(monthly=10_000, per_session=10_000, team_monthly=10**6)
    await seed_limits(
        redis_gateway,
        team_id=TEST_TEAM_ID,
        agent_id=TEST_AGENT_ID,
        team_limit=10**6,
        agent_limit=10_000,
        period=PERIOD,
    )

    request_id = uuid.uuid4().hex
    reserved = await reserve(
        team_id=TEST_TEAM_ID,
        agent_id=TEST_AGENT_ID,
        session_id="s1",
        request_id=request_id,
        model="gpt-4o",
        estimate_micros=1_000,
        policy=policy,
        allow_substitution=False,
        final_attempt=True,
        period=PERIOD,
    )
    assert reserved.allowed
    assert reserved.agent_spend == 1_000

    settled = await settle(
        team_id=TEST_TEAM_ID,
        agent_id=TEST_AGENT_ID,
        session_id="s1",
        request_id=request_id,
        actual_micros=250,
        period=PERIOD,
    )
    assert settled.status == "SETTLED"
    assert settled.delta == -750
    assert settled.agent_spend == 250, "the unused hold was not refunded"


@pytest.mark.asyncio
async def test_settle_is_idempotent(redis_gateway):
    """A retried settle must not charge twice."""
    policy = make_policy(monthly=10_000, per_session=10_000, team_monthly=10**6)
    await seed_limits(
        redis_gateway,
        team_id=TEST_TEAM_ID,
        agent_id=TEST_AGENT_ID,
        team_limit=10**6,
        agent_limit=10_000,
        period=PERIOD,
    )

    request_id = uuid.uuid4().hex
    await reserve(
        team_id=TEST_TEAM_ID,
        agent_id=TEST_AGENT_ID,
        session_id="s1",
        request_id=request_id,
        model="gpt-4o",
        estimate_micros=1_000,
        policy=policy,
        allow_substitution=False,
        final_attempt=True,
        period=PERIOD,
    )
    first = await settle(
        team_id=TEST_TEAM_ID, agent_id=TEST_AGENT_ID, session_id="s1",
        request_id=request_id, actual_micros=400, period=PERIOD,
    )
    second = await settle(
        team_id=TEST_TEAM_ID, agent_id=TEST_AGENT_ID, session_id="s1",
        request_id=request_id, actual_micros=400, period=PERIOD,
    )

    assert first.status == "SETTLED"
    assert second.status == "NOHOLD"
    assert second.agent_spend == 0  # no-op reply
    assert int(
        await redis_gateway.client.get(
            keys.agent_spend(TEST_TEAM_ID, TEST_AGENT_ID, PERIOD)
        )
    ) == 400


@pytest.mark.asyncio
async def test_release_returns_the_whole_hold(redis_gateway):
    """A failed upstream call must cost nothing."""
    policy = make_policy(monthly=10_000, per_session=10_000, team_monthly=10**6)
    await seed_limits(
        redis_gateway,
        team_id=TEST_TEAM_ID,
        agent_id=TEST_AGENT_ID,
        team_limit=10**6,
        agent_limit=10_000,
        period=PERIOD,
    )

    request_id = uuid.uuid4().hex
    await reserve(
        team_id=TEST_TEAM_ID, agent_id=TEST_AGENT_ID, session_id="s1",
        request_id=request_id, model="gpt-4o", estimate_micros=2_500,
        policy=policy, allow_substitution=False, final_attempt=True, period=PERIOD,
    )
    released = await release(
        team_id=TEST_TEAM_ID, agent_id=TEST_AGENT_ID, session_id="s1",
        request_id=request_id, reason="upstream_error", period=PERIOD,
    )

    assert released == 2_500
    assert int(
        await redis_gateway.client.get(
            keys.agent_spend(TEST_TEAM_ID, TEST_AGENT_ID, PERIOD)
        )
    ) == 0


@pytest.mark.asyncio
async def test_concurrent_reserve_and_settle_stay_consistent(redis_gateway):
    """Interleaved holds and refunds must leave the counter exactly right."""
    policy = make_policy(monthly=10**7, per_session=10**7, team_monthly=10**7)
    await seed_limits(
        redis_gateway,
        team_id=TEST_TEAM_ID,
        agent_id=TEST_AGENT_ID,
        team_limit=10**7,
        agent_limit=10**7,
        period=PERIOD,
    )

    async def call(i: int) -> int:
        request_id = uuid.uuid4().hex
        r = await reserve(
            team_id=TEST_TEAM_ID, agent_id=TEST_AGENT_ID, session_id=f"s{i % 7}",
            request_id=request_id, model="gpt-4o", estimate_micros=1_000,
            policy=policy, allow_substitution=False, final_attempt=True,
            period=PERIOD,
        )
        if not r.allowed:
            return 0
        actual = 100 + (i % 400)
        await settle(
            team_id=TEST_TEAM_ID, agent_id=TEST_AGENT_ID, session_id=f"s{i % 7}",
            request_id=request_id, actual_micros=actual, period=PERIOD,
        )
        return actual

    charged = await asyncio.gather(*(call(i) for i in range(300)))

    stored = int(
        await redis_gateway.client.get(
            keys.agent_spend(TEST_TEAM_ID, TEST_AGENT_ID, PERIOD)
        )
    )
    assert stored == sum(charged)

    # Every hold was settled; nothing is left outstanding.
    assert await redis_gateway.client.zcard(keys.HOLDS_PENDING) == 0
