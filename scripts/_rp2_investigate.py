"""RP-2 investigation: inspect api_calls schema + recent raw responses per provider.

Read-only. Helps us discover the actual cost-field path returned by each
provider so src/cost_extraction.py can document it accurately.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

DB_PATH = os.path.expanduser("~/.hermes/bot/zai_usage.db")


def main() -> None:
    db = sqlite3.connect(DB_PATH)

    print("=== api_calls schema ===")
    for r in db.execute("PRAGMA table_info(api_calls)"):
        print(r)

    print("\n=== daily_spend schema ===")
    for r in db.execute("PRAGMA table_info(daily_spend)"):
        print(r)

    cutoff = time.time() - 86400
    print(f"\n=== rows by tier (last 24h, ts > {cutoff:.0f}) ===")
    for r in db.execute(
        "SELECT tier, COUNT(*), "
        "SUM(CASE WHEN cost_usd IS NOT NULL THEN 1 ELSE 0 END) "
        "FROM api_calls WHERE ts>? GROUP BY tier",
        (cutoff,),
    ):
        print(r)

    print("\n=== recent daily_spend (last 7 days) ===")
    for r in db.execute(
        "SELECT date, tier, spend_usd, call_count, token_count "
        "FROM daily_spend ORDER BY date DESC LIMIT 30"
    ):
        print(r)

    db.close()


if __name__ == "__main__":
    main()
