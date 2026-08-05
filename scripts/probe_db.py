#!/usr/bin/env python3
"""Probe production DB: row counts + cost_usd coverage per provider."""
import os, sqlite3

db = os.path.expanduser("~/.hermes/bot/zai_usage.db")
print("DB path:", db, "exists:", os.path.exists(db))
if not os.path.exists(db):
    raise SystemExit(0)

c = sqlite3.connect(db, timeout=2)
print("\n-- per-provider row counts (total, with cost_usd) --")
for row in c.execute(
    "SELECT key_name, COUNT(*), "
    "SUM(CASE WHEN cost_usd IS NOT NULL THEN 1 ELSE 0 END) "
    "FROM api_calls GROUP BY key_name ORDER BY 2 DESC"
):
    print(f"  {row[0]:20s} total={row[1]:8d}  costed={row[2]:8d}")
c.close()
