"""RP-2 post-deploy: verify new API calls have cost_usd populated."""
from __future__ import annotations

import os
import sqlite3
import time

DB = os.path.expanduser("~/.hermes/bot/zai_usage.db")

# Timestamp of the proxy restart (approximate — 5 min window covers it)
RESTART_TS = time.time() - 300


def main() -> None:
    db = sqlite3.connect(DB)

    print(f"Checking calls since ts={RESTART_TS:.0f} (post-restart)")
    print()

    # Total new calls since restart
    total = db.execute(
        "SELECT COUNT(*) FROM api_calls WHERE ts > ?", (RESTART_TS,)
    ).fetchone()[0]
    print(f"Total new calls: {total}")

    if total == 0:
        print("(no calls yet — wait for traffic)")
        db.close()
        return

    # Breakdown by tier and cost population
    print("\n=== By tier ===")
    populated = 0
    for tier, count, with_cost in db.execute(
        "SELECT tier, COUNT(*), "
        "SUM(CASE WHEN cost_usd IS NOT NULL THEN 1 ELSE 0 END) "
        "FROM api_calls WHERE ts > ? GROUP BY tier",
        (RESTART_TS,),
    ):
        pct = (with_cost / count * 100) if count else 0
        print(f"  {tier:15s}  {count:5d} calls  {with_cost:5d} with cost ({pct:.0f}%)")
        populated += with_cost

    pct = (populated / total * 100) if total else 0
    print(f"\n  OVERALL: {populated}/{total} ({pct:.1f}%) have cost_usd")

    # Sample the new 'measured' / 'estimated' / 'flat_rate' rows
    print("\n=== Sample rows with cost_source (new vocabulary) ===")
    for source in ("measured", "estimated", "flat_rate"):
        rows = db.execute(
            "SELECT key_name, model, total_tokens, cost_usd, cost_source "
            "FROM api_calls WHERE ts > ? AND cost_source = ? "
            "ORDER BY ts DESC LIMIT 3",
            (RESTART_TS, source),
        ).fetchall()
        if rows:
            print(f"\n  [{source}]")
            for r in rows:
                print(f"    {r}")

    # Check for any errors in recent calls (proxy health)
    errors = db.execute(
        "SELECT COUNT(*) FROM api_calls WHERE ts > ? AND error IS NOT NULL",
        (RESTART_TS,),
    ).fetchone()[0]
    print(f"\n=== Recent errors: {errors} ===")

    db.close()

    # Gate check
    print(f"\n=== GATE: cost_usd populated for >80% of new calls ===")
    if total >= 5:
        if pct > 80:
            print(f"  PASS: {pct:.1f}% populated")
        else:
            print(f"  BELOW TARGET: {pct:.1f}% (need >80%)")
    else:
        print(f"  INSUFFICIENT DATA: only {total} calls (need >=5 for a meaningful check)")


if __name__ == "__main__":
    main()
