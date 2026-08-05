"""RP-2 gate check: verify cost_usd population for post-restart calls only."""
from __future__ import annotations

import os
import sqlite3

DB = os.path.expanduser("~/.hermes/bot/zai_usage.db")
# The restart was at approximately ts=1785931596 (from the first post-deploy check).
# Use a cutoff just before that to capture only post-restart traffic.
RESTART_TS = 1785931590


def main() -> None:
    db = sqlite3.connect(DB)

    total = 0
    populated = 0
    print(f"Post-restart calls (ts > {RESTART_TS}):")
    print()
    for tier, count, wc in db.execute(
        "SELECT tier, COUNT(*), "
        "SUM(CASE WHEN cost_usd IS NOT NULL THEN 1 ELSE 0 END) "
        "FROM api_calls WHERE ts > ? GROUP BY tier",
        (RESTART_TS,),
    ):
        pct = wc / count * 100 if count else 0
        print(f"  {tier:15s}  {count:5d} calls  {wc:5d} with cost ({pct:.0f}%)")
        total += count
        populated += wc

    pct = populated / total * 100 if total else 0
    print(f"\n  OVERALL: {populated}/{total} ({pct:.1f}%)")

    print("\n  cost_source breakdown:")
    for src, cnt in db.execute(
        "SELECT cost_source, COUNT(*) FROM api_calls WHERE ts > ? GROUP BY cost_source",
        (RESTART_TS,),
    ):
        print(f"    {str(src):15s}  {cnt}")

    # Verify ollama_cloud estimated cost math
    print("\n  ollama_cloud estimated cost verification:")
    for key, model, tokens, cost, src in db.execute(
        "SELECT key_name, model, total_tokens, cost_usd, cost_source "
        "FROM api_calls WHERE ts > ? AND tier='ollama_cloud' "
        "ORDER BY ts DESC LIMIT 3",
        (RESTART_TS,),
    ):
        expected = tokens / 1e6 * 0.024  # included regime rate
        match = abs(cost - expected) < 1e-9 if cost else False
        print(f"    tokens={tokens:6d} cost=${cost:.6f} expected=${expected:.6f} "
              f"{'OK' if match else 'MISMATCH'} src={src}")

    db.close()

    print(f"\n=== GATE: cost_usd populated for >80% of new calls ===")
    if total >= 5:
        if pct > 80:
            print(f"  PASS: {pct:.1f}% populated ({populated}/{total})")
        else:
            print(f"  FAIL: {pct:.1f}% populated ({populated}/{total})")
    else:
        print(f"  INSUFFICIENT DATA: {total} calls (need >=5)")


if __name__ == "__main__":
    main()
