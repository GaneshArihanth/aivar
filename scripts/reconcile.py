"""Compare — and optionally rebuild — Redis counters from the PostgreSQL ledger.

    python -m scripts.reconcile            # report drift, change nothing
    python -m scripts.reconcile --apply    # rebuild the counters from the ledger

Use after a Redis restart, flush or eviction. Drift reported while traffic is
flowing is normal: reservations are held in Redis before their ledger row
exists. Stop traffic (or check that no holds are outstanding) before treating
drift as a fault.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.core.money import format_usd_precise
from app.db.session import dispose_engine, init_engine
from app.redisx.client import gateway
from app.workers import reconciler


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="overwrite Redis counters with the ledger totals")
    parser.add_argument("--period", default=None, help="YYYY-MM (default: now)")
    args = parser.parse_args()

    init_engine()
    await gateway.connect()

    report = (
        await reconciler.rebuild(args.period)
        if args.apply
        else await reconciler.compute_drift(args.period)
    )

    print(f"\nPeriod {report.period}   ({'rebuilt' if args.apply else 'report only'})")
    print(f"Outstanding holds: {report.outstanding_holds}"
          f"{'  — drift below is expected while these are in flight'
             if report.outstanding_holds else ''}\n")

    header = f"  {'agent':<28} {'redis':>12} {'ledger':>12} {'drift':>12}"
    print(header)
    print("  " + "─" * (len(header) - 2))
    for row in sorted(report.agents, key=lambda d: -abs(d.drift_micros)):
        flag = "" if row.drift_micros == 0 else "  ←"
        print(f"  {row.name[:28]:<28} "
              f"{format_usd_precise(row.redis_micros):>12} "
              f"{format_usd_precise(row.ledger_micros):>12} "
              f"{format_usd_precise(row.drift_micros):>12}{flag}")

    print()
    if report.clean:
        print("  ✓ Redis and the ledger agree exactly.")
    else:
        print(f"  ! total drift {format_usd_precise(report.total_drift_micros)}")
        if not args.apply:
            print("    Re-run with --apply to rebuild the counters from the ledger.")
    print()

    await gateway.close()
    await dispose_engine()
    return 0 if report.clean or args.apply else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
