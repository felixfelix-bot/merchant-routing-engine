#!/usr/bin/env python3
"""seed_token_stats.py — Seed the CG-3 token-predictor stats DB (one-shot).

Runs the seed-then-replace aggregation from the production usage DB into
the repo-local stats store used by :mod:`src.token_predictor` (CG-3, plan
``docs/PLAN-cost-gate-reform-v2-2026-08-21.md`` §6):

  python3 scripts/seed_token_stats.py
  python3 scripts/seed_token_stats.py --source ~/.hermes/bot/zai_usage.db
  python3 scripts/seed_token_stats.py --stats data/token_stats.db
  python3 scripts/seed_token_stats.py --window-days 7

Guarantees:

- **Read-only source:** the production DB is opened via ``mode=ro`` URI
  (:func:`src.token_predictor.compute_model_stats`); this script never
  writes to it.
- **Seed-then-replace:** the stats DB is fully replaced in one transaction
  — models missing from the new window disappear (drift-friendly).  A
  source with zero usable rows raises and leaves the old stats intact.
- **Safe artifact:** ``data/*.db`` is gitignored (``*.db`` in
  ``.gitignore``).

Exit codes: ``0`` seeded, ``1`` failure (no rows / unreadable source).
"""
from __future__ import annotations

import argparse
import os
import sys

# ── Path bootstrap so `from src.token_predictor` works standalone ────────────
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from src.token_predictor import (  # noqa: E402
    DEFAULT_SOURCE_DB,
    DEFAULT_STATS_DB,
    DEFAULT_WINDOW_DAYS,
    seed_token_stats,
)

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Seed the CG-3 token-predictor stats DB "
        "(seed-then-replace, read-only source)."
    )
    parser.add_argument(
        "--source",
        default=DEFAULT_SOURCE_DB,
        help=f"usage DB to aggregate (default: {DEFAULT_SOURCE_DB})",
    )
    parser.add_argument(
        "--stats",
        default=DEFAULT_STATS_DB,
        help=f"stats DB to write (default: {DEFAULT_STATS_DB})",
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=DEFAULT_WINDOW_DAYS,
        help=f"trailing window in days (default: {DEFAULT_WINDOW_DAYS})",
    )
    args = parser.parse_args(argv)

    try:
        stats = seed_token_stats(
            args.source, args.stats, window_days=args.window_days
        )
    except ValueError as exc:
        print(f"seed_token_stats: refusing to replace stats: {exc}", file=sys.stderr)
        return 1
    except (OSError, Exception) as exc:  # unreadable/missing source, IO error
        print(f"seed_token_stats: failed: {exc}", file=sys.stderr)
        return 1

    meta = stats["meta"]
    print(f"seeded {args.stats}")
    print(
        f"  models={meta['n_models']} rows={meta['total_rows']} "
        f"window={meta['window_days']}d source={meta['source']}"
    )
    for model, d in sorted(
        stats["models"].items(), key=lambda kv: -kv[1]["n"]
    ):
        print(
            f"  {model:32s} n={d['n']:6d} p50={d['p50']:9.0f} "
            f"p90={d['p90']:9.0f} max={d['max']:9.0f}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
