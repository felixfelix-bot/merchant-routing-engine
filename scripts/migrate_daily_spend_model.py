#!/usr/bin/env python3
"""PM-T5 operator CLI: migrate daily_spend to a per-model schema.

Adds a ``model`` column and upgrades the primary key from ``(date, tier)`` to
``(date, tier, model)``, back-filling existing rows to ``model='unknown'``.
Idempotent and safe to re-run. See ``src/realtime_pricing.migrate_daily_spend_add_model``.

Usage::

    python3 scripts/migrate_daily_spend_model.py [--db PATH]

Defaults to the production usage DB (``~/.hermes/bot/zai_usage.db``). Prints a
JSON report and exits non-zero only if the migration failed to apply.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Make ``src`` importable when run from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.realtime_pricing import migrate_daily_spend_add_model  # noqa: E402

DEFAULT_DB = os.path.expanduser("~/.hermes/bot/zai_usage.db")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=DEFAULT_DB,
        help=f"Path to the usage DB (default: {DEFAULT_DB})",
    )
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"DB not found: {args.db} — nothing to migrate.")
        return 0

    report = migrate_daily_spend_add_model(args.db)
    print(json.dumps({"db": args.db, **report}, indent=2))
    # Non-zero only if a table existed, lacked the column, and we failed to migrate.
    if report["table_exists"] and not report["had_model_column"] and not report["migrated"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
