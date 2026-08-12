"""The hold reaper.

A reservation is held from the moment it is granted until the call settles. If
the process handling that call dies — a crash, a kill -9 during a deploy, a
client that vanishes mid-request — nothing ever settles it, and the hold sits
against the budget for the rest of the month. Enough of those and an agent is
throttled by money it never actually spent.

The reaper releases holds whose deadline has passed. Every hold is registered
in a sorted set scored by its expiry, so finding the expired ones is a range
query rather than a scan of the keyspace.

There is deliberately no companion "runaway sweep" task: velocity is evaluated
inline on every settle, which is precisely when an agent's spend changes. An
agent that has stopped spending cannot cross a spend threshold, so a periodic
re-check would have nothing to find.
"""

from __future__ import annotations

import asyncio
import time

import structlog

from app.config import settings
from app.redisx import keys
from app.redisx.client import gateway

log = structlog.get_logger(__name__)


async def reap_once() -> int:
    """Release every hold past its deadline. Returns how many were reclaimed."""
    now = time.time()
    expired = await gateway.client.zrangebyscore(keys.HOLDS_PENDING, 0, now)
    if not expired:
        return 0

    reclaimed = 0
    for hold_key in expired:
        if isinstance(hold_key, bytes):
            hold_key = hold_key.decode()

        fields = await gateway.client.hmget(
            hold_key, "team_key", "agent_key", "session_key", "estimate", "request_id"
        )
        team_key, agent_key, session_key, estimate, request_id = fields

        if not team_key:
            # The hash TTL outran the zset entry; nothing left to give back.
            await gateway.client.zrem(keys.HOLDS_PENDING, hold_key)
            continue

        result = await gateway.run(
            "release",
            [team_key, agent_key, session_key, hold_key, keys.HOLDS_PENDING],
            ["reaped_expired_hold"],
        )
        if result and (result[0] == "RELEASED" or result[0] == b"RELEASED"):
            reclaimed += 1
            log.warning(
                "reaper.hold_reclaimed",
                request_id=request_id,
                released_micros=int(estimate or 0),
                reason="the request never settled — process died or timed out",
            )

    return reclaimed


async def run_forever() -> None:
    """Background loop; cancelled on shutdown."""
    log.info("reaper.started", interval_seconds=settings.reaper_interval_seconds)
    try:
        while True:
            await asyncio.sleep(settings.reaper_interval_seconds)
            try:
                reclaimed = await reap_once()
                if reclaimed:
                    log.info("reaper.pass_complete", reclaimed=reclaimed)
            except Exception as exc:  # pragma: no cover - must never die
                # A reaper that exits on a transient Redis error stops
                # protecting the budget silently, which is worse than a noisy
                # log line every interval.
                log.error("reaper.pass_failed", error=str(exc))
    except asyncio.CancelledError:
        log.info("reaper.stopped")
        raise
