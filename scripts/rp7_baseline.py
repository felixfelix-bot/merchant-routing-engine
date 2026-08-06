#!/usr/bin/env python3
"""RP-7 Day-0 baseline: cost population, per-provider rates, Kalman health."""
import sqlite3
import os
from datetime import datetime

DB = os.path.expanduser("~/.hermes/bot/zai_usage.db")

def pct(part, total):
    return (part / total * 100) if total else 0

def main():
    conn = sqlite3.connect(DB)
    c = conn.cursor()

    # Tables
    c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [r[0] for r in c.fetchall()]
    print(f"=== DB tables ({len(tables)}) ===\n{', '.join(tables)}\n")

    # ── routing_shadow_decisions: cost population ──
    print("=" * 70)
    print("ROUTING_SHADOW_DECISIONS — cost population")
    print("=" * 70)

    # ts column is the timestamp field
    c.execute("PRAGMA table_info(routing_shadow_decisions)")
    cols = [r[1] for r in c.fetchall()]
    print(f"Columns: {', '.join(cols)}")

    # live_cost population (last 24h)
    print("\n--- live_cost population (last 24h) ---")
    c.execute("""
        SELECT CASE WHEN live_cost IS NOT NULL AND live_cost > 0 THEN 'populated' ELSE 'NULL/zero' END,
               COUNT(*)
        FROM routing_shadow_decisions
        WHERE ts > strftime('%s','now','-1 day')
        GROUP BY 1
    """)
    rows = c.fetchall()
    total_24h = sum(r[1] for r in rows)
    for row in rows:
        print(f"  {row[0]:12s}: {row[1]:6d} ({pct(row[1], total_24h):.1f}%)")
    print(f"  {'TOTAL':12s}: {total_24h}")

    # per_model_base_rate population (last 24h) — the RP feature
    print("\n--- per_model_base_rate population (last 24h) ---")
    c.execute("""
        SELECT CASE WHEN per_model_base_rate IS NOT NULL AND per_model_base_rate > 0 THEN 'populated' ELSE 'NULL/zero' END,
               COUNT(*)
        FROM routing_shadow_decisions
        WHERE ts > strftime('%s','now','-1 day')
        GROUP BY 1
    """)
    rows = c.fetchall()
    for row in rows:
        print(f"  {row[0]:12s}: {row[1]:6d} ({pct(row[1], total_24h):.1f}%)")

    # all-time
    print("\n--- live_cost population (all-time) ---")
    c.execute("""
        SELECT CASE WHEN live_cost IS NOT NULL AND live_cost > 0 THEN 'populated' ELSE 'NULL/zero' END,
               COUNT(*)
        FROM routing_shadow_decisions
        GROUP BY 1
    """)
    rows = c.fetchall()
    total_all = sum(r[1] for r in rows)
    for row in rows:
        print(f"  {row[0]:12s}: {row[1]:6d} ({pct(row[1], total_all):.1f}%)")
    print(f"  {'TOTAL':12s}: {total_all}")

    # per-provider rates (last 7d)
    print("\n--- Per-provider live_cost (last 7d) ---")
    c.execute("""
        SELECT live_provider,
               COUNT(*) as n,
               COUNT(CASE WHEN live_cost > 0 THEN 1 END) as populated,
               ROUND(AVG(CASE WHEN live_cost > 0 THEN live_cost END), 6) as avg_cost,
               ROUND(MIN(CASE WHEN live_cost > 0 THEN live_cost END), 6) as min_cost,
               ROUND(MAX(CASE WHEN live_cost > 0 THEN live_cost END), 6) as max_cost,
               ROUND(AVG(CASE WHEN live_cost > 0 THEN tokens END), 0) as avg_tokens
        FROM routing_shadow_decisions
        WHERE ts > strftime('%s','now','-7 day')
        GROUP BY live_provider
        ORDER BY n DESC
    """)
    rows = c.fetchall()
    if rows:
        print(f"  {'provider':20s} {'n':>6s} {'pop':>6s} {'avg':>10s} {'min':>10s} {'max':>10s} {'avg_tok':>10s}")
        for row in rows:
            print(f"  {str(row[0]):20s} {row[1]:6d} {row[2]:6d} {str(row[3]):>10s} {str(row[4]):>10s} {str(row[5]):>10s} {str(row[6]):>10s}")
    else:
        print("  (no rows in last 7d)")

    # quota_regime distribution
    print("\n--- quota_regime distribution (last 7d) ---")
    c.execute("""
        SELECT COALESCE(quota_regime, 'NULL') as regime, COUNT(*)
        FROM routing_shadow_decisions
        WHERE ts > strftime('%s','now','-7 day')
        GROUP BY regime
        ORDER BY 2 DESC
    """)
    for row in c.fetchall():
        print(f"  {row[0]:20s}: {row[1]}")

    # ── price_observations ──
    if "price_observations" in tables:
        print("\n" + "=" * 70)
        print("PRICE_OBSERVATIONS")
        print("=" * 70)
        c.execute("PRAGMA table_info(price_observations)")
        cols = [r[1] for r in c.fetchall()]
        print(f"Columns: {', '.join(cols)}")
        c.execute("SELECT COUNT(*) FROM price_observations")
        print(f"Total rows: {c.fetchone()[0]}")
        c.execute("""
            SELECT * FROM price_observations
            ORDER BY rowid DESC LIMIT 5
        """)
        for row in c.fetchall():
            print(f"  {row}")

    # ── kalman_samples ──
    if "kalman_samples" in tables:
        print("\n" + "=" * 70)
        print("KALMAN_SAMPLES")
        print("=" * 70)
        c.execute("PRAGMA table_info(kalman_samples)")
        cols = [r[1] for r in c.fetchall()]
        print(f"Columns: {', '.join(cols)}")
        c.execute("SELECT COUNT(*) FROM kalman_samples")
        print(f"Total rows: {c.fetchone()[0]}")
        c.execute("SELECT * FROM kalman_samples ORDER BY rowid DESC LIMIT 5")
        for row in c.fetchall():
            print(f"  {row}")

    # latest entry
    c.execute("SELECT datetime(MAX(ts),'unixepoch') FROM routing_shadow_decisions")
    latest = c.fetchone()
    print(f"\n=== Latest routing entry: {latest[0]} ===")

    conn.close()
    print(f"\n=== Baseline taken at {datetime.utcnow().isoformat()}Z ===")

if __name__ == "__main__":
    main()
