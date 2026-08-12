"""Traffic generator for the Agent Budget Controller.

Drives the proxy with realistic-ish agent traffic so the enforcement paths can
be watched on the dashboard and asserted in tests.

Scenarios
---------
steady      Constant, well-behaved traffic. Nothing should trip.
burst       High concurrency against one agent — the race-condition scenario.
runaway     A recursive loop, as in the brief: as fast as possible until the
            velocity breaker pauses the agent.
session     Many calls inside one session, to exercise the session cap.
mixed       All twelve seeded agents at once, which is what the dashboard is
            most interesting to watch.

Examples
--------
    python -m loadgen.main --scenario mixed --calls 200
    python -m loadgen.main --scenario runaway --api-key sk-agent-…
    python -m loadgen.main --scenario burst --agents 3 --concurrency 50
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field

import httpx

PROMPTS = [
    "Summarise this transaction for the fraud review queue.",
    "Rewrite the customer's search query to improve recall.",
    "Draft a reply to the support ticket below, polite and specific.",
    "Explain the schema mismatch between these two tables.",
    "Classify this log line as benign, suspicious or malicious.",
    "Extract the line items and totals from this invoice text.",
]


@dataclass
class Stats:
    ok: int = 0
    substituted: int = 0
    budget_blocked: int = 0
    session_blocked: int = 0
    runaway_blocked: int = 0
    errors: int = 0
    spend_usd: float = 0.0
    latencies: list[float] = field(default_factory=list)
    by_status: Counter = field(default_factory=Counter)

    def record(self, response: httpx.Response, elapsed: float) -> None:
        self.latencies.append(elapsed)
        self.by_status[response.status_code] += 1
        if response.status_code == 200:
            self.ok += 1
            self.spend_usd += float(response.headers.get("x-budget-cost-usd", 0) or 0)
            if (
                response.headers.get("x-budget-model-served")
                != response.headers.get("x-budget-model-requested")
            ):
                self.substituted += 1
        elif response.status_code == 402:
            kind = (response.json().get("error", {}) or {}).get("type", "")
            if kind.startswith("session"):
                self.session_blocked += 1
            else:
                self.budget_blocked += 1
        elif response.status_code == 423:
            self.runaway_blocked += 1
        else:
            self.errors += 1

    def report(self, title: str, wall_seconds: float) -> None:
        total = sum(self.by_status.values())
        latencies = sorted(self.latencies)
        p50 = latencies[len(latencies) // 2] * 1000 if latencies else 0
        p95 = latencies[int(len(latencies) * 0.95)] * 1000 if latencies else 0

        print(f"\n── {title}")
        print(f"   requests        {total} in {wall_seconds:.1f}s "
              f"({total / max(wall_seconds, 0.001):.0f}/s)")
        print(f"   served          {self.ok}"
              f"{f' ({self.substituted} substituted)' if self.substituted else ''}")
        print(f"   budget blocked  {self.budget_blocked}")
        print(f"   session closed  {self.session_blocked}")
        print(f"   runaway paused  {self.runaway_blocked}")
        if self.errors:
            print(f"   errors          {self.errors}")
        print(f"   spend           ${self.spend_usd:.4f}")
        print(f"   latency         p50 {p50:.0f}ms · p95 {p95:.0f}ms")


async def one_call(
    client: httpx.AsyncClient,
    api_key: str,
    stats: Stats,
    *,
    session_id: str,
    max_tokens: int = 400,
    model: str | None = None,
) -> httpx.Response:
    payload: dict = {
        "messages": [{"role": "user", "content": random.choice(PROMPTS)}],
        "max_tokens": max_tokens,
    }
    if model:
        payload["model"] = model

    started = time.perf_counter()
    try:
        response = await client.post(
            "/v1/chat/completions",
            json=payload,
            headers={"X-Agent-Key": api_key, "X-Session-Id": session_id},
        )
    except httpx.HTTPError as exc:
        stats.errors += 1
        print(f"   ! transport error: {exc}", file=sys.stderr)
        raise
    stats.record(response, time.perf_counter() - started)
    return response


# ---------------------------------------------------------------- discovery


@dataclass(frozen=True)
class Profile:
    """Budget shape for a scenario's throwaway agents.

    Each scenario needs a different one. A budget generous enough for the
    steady scenario would never trip the session cap or the velocity breaker
    inside a demo-length run — the limits have to be scaled to the traffic the
    scenario actually produces, or the scenario proves nothing.
    """

    monthly_usd: float
    session_usd: float
    runaway_fraction: float
    model: str = "gpt-4o-mini"


PROFILES: dict[str, Profile] = {
    "steady":  Profile(monthly_usd=5.00, session_usd=0.50, runaway_fraction=0.0),
    "burst":   Profile(monthly_usd=5.00, session_usd=0.50, runaway_fraction=0.0),
    # ~$0.0005 a call, so the $0.06 hourly threshold lands after ~110 calls.
    "runaway": Profile(monthly_usd=0.30, session_usd=0.30, runaway_fraction=0.20),
    # A session cap a couple of calls wide, so the close is quick to observe.
    "session": Profile(monthly_usd=5.00, session_usd=0.002, runaway_fraction=0.0),
    # Tight enough that the greedy agent hits a wall while the polite ones run on.
    "mixed":   Profile(monthly_usd=0.75, session_usd=0.10, runaway_fraction=0.20,
                       model="gpt-4o"),
}


async def fetch_agent_keys(
    client: httpx.AsyncClient, count: int, profile: Profile
) -> list[tuple[str, str]]:
    """Create throwaway agents and return ``(name, api_key)`` pairs.

    The seeded agents' keys were printed once at seed time and are not
    retrievable, so the generator mints its own.
    """
    teams = (await client.get("/admin/teams")).json()
    if not teams:
        raise SystemExit("No teams exist — run `make seed` first.")

    created: list[tuple[str, str]] = []
    for index in range(count):
        team = teams[index % len(teams)]
        response = await client.post(
            "/admin/agents",
            json={
                "name": f"loadgen-{uuid.uuid4().hex[:8]}",
                "team_id": team["id"],
                "monthly_budget_usd": profile.monthly_usd,
                "session_budget_usd": profile.session_usd,
                "preferred_model": profile.model,
                "allow_substitution": True,
                "runaway_hourly_fraction": profile.runaway_fraction,
            },
        )
        response.raise_for_status()
        body = response.json()
        created.append((body["agent"]["name"], body["api_key"]))
    return created


# ---------------------------------------------------------------- scenarios


async def scenario_steady(client, keys_, stats, args):
    async def worker(name: str, key: str) -> None:
        for index in range(args.calls):
            await one_call(client, key, stats, session_id=f"{name}-s{index // 10}",
                           max_tokens=random.randint(120, 400))
            await asyncio.sleep(args.delay)

    await asyncio.gather(*(worker(n, k) for n, k in keys_))


async def scenario_burst(client, keys_, stats, args):
    """Maximum concurrency — the condition a read-then-write counter fails."""
    semaphore = asyncio.Semaphore(args.concurrency)

    async def worker(name: str, key: str, index: int) -> None:
        async with semaphore:
            await one_call(client, key, stats, session_id=f"{name}-burst-{index % 5}",
                           max_tokens=random.randint(200, 600))

    tasks = [
        worker(name, key, i)
        for name, key in keys_
        for i in range(args.calls)
    ]
    await asyncio.gather(*tasks, return_exceptions=True)


async def scenario_runaway(client, keys_, stats, args):
    """The incident from the brief, in miniature."""
    name, key = keys_[0]
    print(f"   looping on {name} until the detector pauses it…")
    for wave in range(args.calls):
        responses = await asyncio.gather(
            *(
                one_call(client, key, stats, session_id=f"loop-{wave}-{i}",
                         max_tokens=1000)
                for i in range(10)
            ),
            return_exceptions=True,
        )
        if any(
            isinstance(r, httpx.Response) and r.status_code == 423 for r in responses
        ):
            print(f"   paused after {(wave + 1) * 10} calls "
                  f"(${stats.spend_usd:.4f} in under a minute)")
            return
    print(f"   detector did not fire after {args.calls * 10} calls "
          f"(${stats.spend_usd:.4f} spent). Raise --calls, or check that the "
          f"agent's runaway_hourly_fraction is not 0.")


async def scenario_session(client, keys_, stats, args):
    name, key = keys_[0]
    session_id = f"{name}-single-session"
    for _ in range(args.calls):
        response = await one_call(client, key, stats, session_id=session_id,
                                  max_tokens=600)
        if response.status_code == 402:
            print(f"   session closed after {stats.ok} calls; opening a new one")
            session_id = f"{name}-session-{uuid.uuid4().hex[:6]}"


async def scenario_mixed(client, keys_, stats, args):
    """Everything at once, with one agent deliberately misbehaving."""
    async def polite(name: str, key: str) -> None:
        for index in range(args.calls):
            await one_call(client, key, stats, session_id=f"{name}-{index // 8}",
                           max_tokens=random.randint(100, 500))
            await asyncio.sleep(random.uniform(0.02, 0.2))

    async def greedy(name: str, key: str) -> None:
        for index in range(args.calls * 3):
            response = await one_call(client, key, stats,
                                      session_id=f"{name}-hot-{index}", max_tokens=900)
            if response.status_code in (402, 423):
                return

    workers = [polite(n, k) for n, k in keys_[:-1]]
    if keys_:
        workers.append(greedy(*keys_[-1]))
    await asyncio.gather(*workers, return_exceptions=True)


SCENARIOS = {
    "steady": scenario_steady,
    "burst": scenario_burst,
    "runaway": scenario_runaway,
    "session": scenario_session,
    "mixed": scenario_mixed,
}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="mixed")
    parser.add_argument("--proxy", default="http://127.0.0.1:8000")
    parser.add_argument("--agents", type=int, default=3)
    parser.add_argument("--calls", type=int, default=20,
                        help="calls per agent (waves of 10, for runaway)")
    parser.add_argument("--concurrency", type=int, default=25)
    parser.add_argument("--delay", type=float, default=0.05)
    parser.add_argument("--api-key", action="append", dest="api_keys",
                        help="use an existing agent key; repeatable")
    parser.add_argument("--keep", action="store_true",
                        help="do not delete agents created by this run")
    args = parser.parse_args()

    async with httpx.AsyncClient(base_url=args.proxy, timeout=60.0) as client:
        try:
            await client.get("/health")
        except httpx.ConnectError:
            raise SystemExit(f"Cannot reach the proxy at {args.proxy}") from None

        created_here = False
        profile = PROFILES[args.scenario]
        if args.api_keys:
            keys_ = [(f"agent-{i + 1}", k) for i, k in enumerate(args.api_keys)]
        else:
            count = 12 if args.scenario == "mixed" else args.agents
            print(
                f"Creating {count} throwaway agent(s) "
                f"(${profile.monthly_usd}/month, ${profile.session_usd}/session, "
                f"{profile.model})…"
            )
            keys_ = await fetch_agent_keys(client, count, profile)
            created_here = True

        stats = Stats()
        started = time.perf_counter()
        print(f"Running '{args.scenario}' with {len(keys_)} agent(s)…")
        await SCENARIOS[args.scenario](client, keys_, stats, args)
        wall = time.perf_counter() - started
        stats.report(f"scenario: {args.scenario}", wall)

        drift = (await client.get("/admin/reconcile")).json()
        marker = "clean" if drift["clean"] else f"DRIFT {drift['total_drift_micros']}µ$"
        print(f"   reconciliation  {marker} "
              f"({drift['outstanding_holds']} holds outstanding)")

        if created_here and not args.keep:
            for name, _ in keys_:
                agents = (await client.get("/admin/agents")).json()
                for agent in agents:
                    if agent["name"] == name:
                        await client.delete(f"/admin/agents/{agent['id']}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
