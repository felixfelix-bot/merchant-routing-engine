#!/usr/bin/env python3
"""Probe z.ai cost_usd values + test the dynamic rate resolver."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sqlite3

db = os.path.expanduser("~/.hermes/bot/zai_usage.db")
c = sqlite3.connect(db, timeout=2)

print("== z.ai cost_usd sample (ours/friend) ==")
for k in ("ours", "friend"):
    row = c.execute(
        f"SELECT COUNT(*), AVG(cost_usd), SUM(cost_usd), SUM(total_tokens) "
        f"FROM api_calls WHERE key_name=? AND cost_usd IS NOT NULL", (k,)).fetchone()
    print(f"  {k:8s} n={row[0]} avg_cost={row[1]} sum_cost={row[2]} sum_tok={row[3]}")
c.close()

print("\n== get_real_rate over provider windows ==")
from src.real_price_tracker import (
    get_real_rate, get_zai_amortized_rate, SEED_RATES, PROVIDER_WINDOW_HOURS,
)
for p, w in PROVIDER_WINDOW_HOURS.items():
    r = get_real_rate(p, window_hours=w)
    print(f"  {p:14s} window={int(w):6d}h  get_real_rate={r}")

print("\n== get_zai_amortized_rate ==")
for p in ("ours", "friend"):
    print(f"  {p:8s} amortized={get_zai_amortized_rate(p)}")

print("\n== SEED_RATES ==")
for p, v in SEED_RATES.items():
    print(f"  {p:14s} {v}")
