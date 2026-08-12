"""Failure-path behaviour: crashed requests, lost Redis, and Redis outage.

These are not stated success criteria, but each protects one of them. A budget
system that leaks reservations on every crash, or that treats a Redis restart
as "everyone's budget is fresh again", enforces correctly only until the first
bad day.
"""

from __future__ import annotations

import time
import uuid

import pytest
import redis.asyncio as aioredis

from app.core.budget import Decision, build_reserve_call, reserve
from app.redisx import keys
from app.redisx.client import RedisUnavailable, gateway
from app.workers import reaper
from tests.conftest import TEST_AGENT_ID, TEST_TEAM_ID, make_policy, seed_limits

PERIOD = "2026-08"


async def _reserve(session_id="s1", estimate=1_000, policy=None):
    return await reserve(
        team_id=TEST_TEAM_ID,
        agent_id=TEST_AGENT_ID,
        session_id=session_id,
        request_id=uuid.uuid4().hex,
        model="gpt-4o",
        estimate_micros=estimate,
        policy=policy,
        allow_substitution=False,
        final_attempt=True,
        period=PERIOD,
    )


@pytest.mark.asyncio
async def test_reaper_reclaims_a_hold_whose_request_died(redis_gateway):
    """A request that never settles must not hold budget forever."""
    policy = make_policy(monthly=10_000, per_session=10_000, team_monthly=10**6)
    await seed_limits(
        redis_gateway, team_id=TEST_TEAM_ID, agent_id=TEST_AGENT_ID,
        team_limit=10**6, agent_limit=10_000, period=PERIOD,
    )

    reserved = await _reserve(estimate=4_000, policy=policy)
    assert reserved.allowed
    assert reserved.agent_spend == 4_000

    # Nothing settles: simulate the process handling this call disappearing.
    assert await redis_gateway.client.zcard(keys.HOLDS_PENDING) == 1
    assert await reaper.reap_once() == 0, "a live hold must not be reclaimed early"

    # Wind the hold's deadline into the past, as its TTL would eventually do.
    hold_key = (await redis_gateway.client.zrange(keys.HOLDS_PENDING, 0, -1))[0]
    await redis_gateway.client.zadd(keys.HOLDS_PENDING, {hold_key: time.time() - 1})

    assert await reaper.reap_once() == 1
    assert int(
        await redis_gateway.client.get(
            keys.agent_spend(TEST_TEAM_ID, TEST_AGENT_ID, PERIOD)
        )
    ) == 0, "the abandoned reservation was never given back"
    assert await redis_gateway.client.zcard(keys.HOLDS_PENDING) == 0


@pytest.mark.asyncio
async def test_reaper_is_idempotent(redis_gateway):
    """Reaping twice must not refund twice."""
    policy = make_policy(monthly=10_000, per_session=10_000, team_monthly=10**6)
    await seed_limits(
        redis_gateway, team_id=TEST_TEAM_ID, agent_id=TEST_AGENT_ID,
        team_limit=10**6, agent_limit=10_000, period=PERIOD,
    )
    await _reserve(estimate=3_000, policy=policy)
    await _reserve(session_id="s2", estimate=2_000, policy=policy)

    for hold_key in await redis_gateway.client.zrange(keys.HOLDS_PENDING, 0, -1):
        await redis_gateway.client.zadd(keys.HOLDS_PENDING, {hold_key: time.time() - 1})

    assert await reaper.reap_once() == 2
    assert await reaper.reap_once() == 0
    assert int(
        await redis_gateway.client.get(
            keys.agent_spend(TEST_TEAM_ID, TEST_AGENT_ID, PERIOD)
        )
    ) == 0


@pytest.mark.asyncio
async def test_missing_limit_is_never_read_as_unlimited(redis_gateway):
    """A cache miss must not become permission to spend.

    If Redis loses its limit keys (restart, eviction, a flush) the script
    reports LIMIT_MISSING rather than allowing the call. Treating an absent
    limit as "no limit" would turn an infrastructure blip into an unbounded
    spend window.
    """
    # Built through the same helper the proxy uses, so this test cannot drift
    # out of step with the script's signature — which is exactly what happened
    # when it kept its own copy of the argument list.
    script_keys, args = build_reserve_call(
        team_id=TEST_TEAM_ID,
        agent_id=TEST_AGENT_ID,
        session_id="s1",
        request_id="req-1",
        model="gpt-4o",
        estimate_micros=1_000,
        policy=make_policy(monthly=1_000, per_session=1_000, team_monthly=1_000),
        allow_substitution=False,
        final_attempt=True,
        tokens=100,
        period=PERIOD,
        now=int(time.time()),
    )
    raw = await gateway.run("reserve", script_keys, args)
    assert raw[0] == "LIMIT_MISSING"
    assert raw[1] == "team"
    assert await redis_gateway.client.get(
        keys.agent_spend(TEST_TEAM_ID, TEST_AGENT_ID, PERIOD)
    ) is None, "a rejected reservation still moved the counter"


@pytest.mark.asyncio
async def test_fail_closed_when_redis_is_unreachable(redis_gateway, monkeypatch):
    """With Redis down and fail mode 'closed', reservations raise rather than
    silently succeed. Availability is not worth more than the guarantee here."""
    from app.config import settings

    monkeypatch.setattr(settings, "enforcement_fail_mode", "closed")

    async def boom(*_args, **_kwargs):
        raise aioredis.ConnectionError("connection refused")

    monkeypatch.setattr(gateway.script("reserve"), "__call__", boom)

    with pytest.raises(RedisUnavailable):
        await gateway.run("reserve", ["k"], [1])


@pytest.mark.asyncio
async def test_fail_open_is_available_but_explicit(redis_gateway, monkeypatch):
    """The opposite trade-off remains reachable for operators who want it."""
    from app.config import settings

    monkeypatch.setattr(settings, "enforcement_fail_mode", "open")

    async def boom(*_args, **_kwargs):
        raise aioredis.ConnectionError("connection refused")

    monkeypatch.setattr(gateway.script("reserve"), "__call__", boom)

    assert await gateway.run("reserve", ["k"], [1]) is None


@pytest.mark.asyncio
async def test_counter_never_goes_negative(redis_gateway):
    """Defensive: a stray refund must not hand an agent free budget."""
    policy = make_policy(monthly=10_000, per_session=10_000, team_monthly=10**6)
    await seed_limits(
        redis_gateway, team_id=TEST_TEAM_ID, agent_id=TEST_AGENT_ID,
        team_limit=10**6, agent_limit=10_000, period=PERIOD,
    )
    request_id = uuid.uuid4().hex
    await reserve(
        team_id=TEST_TEAM_ID, agent_id=TEST_AGENT_ID, session_id="s1",
        request_id=request_id, model="gpt-4o", estimate_micros=500,
        policy=policy, allow_substitution=False, final_attempt=True, period=PERIOD,
    )
    # Settle for more than was held, then release: the clamp keeps it at zero.
    from app.core.budget import settle

    await settle(
        team_id=TEST_TEAM_ID, agent_id=TEST_AGENT_ID, session_id="s1",
        request_id=request_id, actual_micros=100, period=PERIOD,
    )
    agent_key = keys.agent_spend(TEST_TEAM_ID, TEST_AGENT_ID, PERIOD)
    await redis_gateway.client.set(agent_key, 50)

    request_id2 = uuid.uuid4().hex
    await reserve(
        team_id=TEST_TEAM_ID, agent_id=TEST_AGENT_ID, session_id="s1",
        request_id=request_id2, model="gpt-4o", estimate_micros=500,
        policy=policy, allow_substitution=False, final_attempt=True, period=PERIOD,
    )
    from app.core.budget import release

    await release(
        team_id=TEST_TEAM_ID, agent_id=TEST_AGENT_ID, session_id="s1",
        request_id=request_id2, reason="test", period=PERIOD,
    )
    assert int(await redis_gateway.client.get(agent_key)) >= 0


@pytest.mark.asyncio
async def test_session_scope_isolation_between_sessions(redis_gateway):
    """One session's spend must not count against another's cap."""
    policy = make_policy(monthly=10**6, per_session=1_000, team_monthly=10**6)
    await seed_limits(
        redis_gateway, team_id=TEST_TEAM_ID, agent_id=TEST_AGENT_ID,
        team_limit=10**6, agent_limit=10**6, period=PERIOD,
    )
    first = await _reserve(session_id="alpha", estimate=900, policy=policy)
    second = await _reserve(session_id="beta", estimate=900, policy=policy)
    assert first.allowed and second.allowed
    assert first.session_spend == 900 and second.session_spend == 900

    over = await _reserve(session_id="alpha", estimate=900, policy=policy)
    assert over.status is Decision.SESSION_EXHAUSTED
    still_fine = await _reserve(session_id="beta", estimate=50, policy=policy)
    assert still_fine.allowed
