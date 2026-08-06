#!/usr/bin/env python3
"""shadow_daily_divergence.py — Daily P6 divergence report for the shadow soak.

Queries the ``routing_shadow_decisions`` table and prints a one-shot summary
of the pressure-routing divergence dimension (the P6 gate).  Designed to run
as a daily cron job during the 7-day shadow soak (T7).

Exit codes:
    0  — all gates green (or no data yet)
    1  — a gate FAILED (alert-worthy)
    2  — degenerate dataset (pressure columns not populated — C1 not deployed)

Usage::

    python3 scripts/shadow_daily_divergence.py [--hours N] [--db PATH]

    --hours N   Look back N hours (default 24).
    --db PATH   SQLite path (default ~/.hermes/bot/zai_usage.db).

Standalone: stdlib only. Never raises.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from src.shadow_logger import ShadowLogger


def main() -> int:
    ap = argparse.ArgumentParser(description="Daily shadow divergence report")
    ap.add_argument("--hours", type=float, default=24.0,
                    help="Look-back window in hours (default 24)")
    ap.add_argument("--db", default=os.path.expanduser("~/.hermes/bot/zai_usage.db"),
                    help="SQLite DB path")
    args = ap.parse_args()

    since_ts = time.time() - args.hours * 3600.0

    try:
        logger = ShadowLogger(db_path=args.db)
    except Exception as e:
        print(f"[shadow-daily] Cannot open DB {args.db}: {e}", file=sys.stderr)
        return 2

    try:
        summary = logger.get_pressure_divergence_summary(since_ts=since_ts)
        gate = logger.evaluate_exit_criteria(
            baseline_429_rate=0.0,
            baseline_paid_spend=0.0,
            since_ts=since_ts,
        )
        span = logger.get_session_span_hours(since_ts=since_ts)
    except Exception as e:
        print(f"[shadow-daily] Query failed: {e}", file=sys.stderr)
        return 2
    finally:
        logger.close()

    # ── Degenerate dataset check ──
    if summary["pressure_rows"] == 0:
        print("=" * 60)
        print("DEGENERATE: zero rows with pressure_provider populated.")
        print("The C1 fix (log_decision_with_pressure) is not yet live,")
        print("or no traffic has flowed since the fix was deployed.")
        print("Exit criteria cannot be honestly evaluated.")
        print("=" * 60)
        print(f"  total_rows (last {args.hours:.0f}h): {summary['total_rows']}")
        print(f"  pressure_rows: {summary['pressure_rows']}")
        return 2

    low_pressure_pct = summary["pressure_pct"] < 0.50
    print("=" * 60)
    print(f"Shadow Divergence Report — last {args.hours:.0f}h")
    print(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print("=" * 60)

    print(f"\n  Data quality:")
    print(f"    total_rows:       {summary['total_rows']:,}")
    print(f"    pressure_rows:    {summary['pressure_rows']:,} "
          f"({summary['pressure_pct']:.1%})")
    if low_pressure_pct:
        print(f"    ⚠️  LOW pressure coverage — C1 may not be fully live")

    print(f"\n  Divergence (P6 gate: < 15%):")
    print(f"    avg:   {summary['avg_divergence']:.4%}")
    print(f"    max:   {summary['max_divergence']:.4%}")
    print(f"    zero:  {summary['zero_divergence_pct']:.1%} of pressure rows")

    print(f"\n  Costs ($/M):")
    print(f"    actual:   ${summary['avg_actual_cost']:.6f}")
    print(f"    pressure: ${summary['avg_pressure_cost']:.6f}")

    print(f"\n  Safety:")
    print(f"    429 count:         {summary['429_count']}")
    print(f"    paid_provider rows: {summary['paid_provider_count']}")

    if summary["provider_shifts"]:
        print(f"\n  Top provider shifts (actual → pressure):")
        for shift, n in sorted(summary["provider_shifts"].items(),
                               key=lambda x: -x[1])[:5]:
            print(f"    {shift:40s}  {n:,}")

    print(f"\n  Exit criteria:")
    for name, c in gate["criteria"].items():
        status = "✅" if c["passed"] else "❌"
        thresh = c.get("threshold", "—")
        print(f"    {status} {name:20s}  val={c['value']}  threshold={thresh}")
    print(f"    {'✅' if gate['all_passed'] else '❌'} all_passed: {gate['all_passed']}")

    print(f"\n  Session span: {span:.1f}h")

    # Exit code
    if low_pressure_pct:
        print("\n⚠️  pressure coverage < 50% — partial C1 deployment")
        return 2
    if not gate["all_passed"]:
        print("\n❌ GATE FAILED")
        return 1
    print("\n✅ Gates green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
