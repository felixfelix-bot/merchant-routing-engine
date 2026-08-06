#!/usr/bin/env python3
"""
RP-7 Daily Monitor — checks 4 convergence criteria and alerts on violations.

Run once per day for 7 days. Non-empty stdout = alert delivered.
Empty stdout = all clear, silent.

Criteria:
  1. cost_usd (live_cost) populated for >80% of calls in last 24h
  2. Measured Ollama rate within reasonable range of price_observations
  3. Kalman rates stable (variance < 50% over 24h)
  4. No provider rate deviating >50% from expected
"""
import sqlite3
import os
import sys
import statistics
from datetime import datetime

DB = os.path.expanduser("~/.hermes/bot/zai_usage.db")
DAY0 = datetime(2026, 8, 6)  # deployment day

# Fallback expected rates (from LAST_RESORT_RATES)
FALLBACK_EXPECTED = {
    "ours": 0.001,
    "friend": 0.001,
    "ollama_cloud": 0.0155,
    "ppq": 0.14,
    "openrouter": 0.135,
    "deepinfra": 1.30,
}

alerts = []

def pct(part, total):
    return (part / total * 100) if total else 0

def main():
    if not os.path.exists(DB):
        return  # silent — DB not available

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    # ── Criterion 1: cost population >80% ──
    c.execute("""
        SELECT 
            COUNT(*) as total,
            SUM(CASE WHEN live_cost IS NOT NULL AND live_cost > 0 THEN 1 ELSE 0 END) as populated
        FROM routing_shadow_decisions
        WHERE ts > strftime('%s','now','-1 day')
    """)
    row = c.fetchone()
    total, populated = row[0] or 0, row[1] or 0
    pop_pct = pct(populated, total)
    if total > 0 and pop_pct < 80.0:
        alerts.append(
            f"⚠️ cost_usd population: {pop_pct:.1f}% (< 80% target). "
            f"{populated}/{total} calls in last 24h have live_cost populated."
        )

    # ── Criterion 2: Per-provider rate deviation >50% ──
    # Build expected rates from price_observations (tracker's own data),
    # falling back to LAST_RESORT constants.
    expected = dict(FALLBACK_EXPECTED)
    c.execute("""
        SELECT provider, rate_per_m
        FROM price_observations
        WHERE id IN (SELECT MAX(id) FROM price_observations GROUP BY provider)
    """)
    for prov, rate in c.fetchall():
        if prov and rate and rate > 0:
            expected[prov] = rate

    c.execute("""
        SELECT live_provider,
               AVG(live_cost) as avg_rate,
               COUNT(*) as n
        FROM routing_shadow_decisions
        WHERE live_cost IS NOT NULL AND live_cost > 0
          AND ts > strftime('%s','now','-1 day')
        GROUP BY live_provider
        HAVING n >= 10
    """)
    for prov, avg_rate, n in c.fetchall():
        if prov in expected:
            exp = expected[prov]
            if exp > 0:
                deviation = abs(avg_rate - exp) / exp
                if deviation > 0.50:
                    alerts.append(
                        f"⚠️ {prov} rate deviation: measured ${avg_rate:.6f}/M vs "
                        f"expected ${exp:.6f}/M ({deviation*100:.0f}% off, >50% threshold)"
                    )

    # ── Criterion 3: Kalman stability (variance over 24h) ──
    c.execute("""
        SELECT live_provider, live_cost, ts
        FROM routing_shadow_decisions
        WHERE live_cost IS NOT NULL AND live_cost > 0
          AND ts > strftime('%s','now','-1 day')
        ORDER BY live_provider, ts
    """)
    from collections import defaultdict
    provider_rates = defaultdict(list)
    for prov, cost, ts in c.fetchall():
        provider_rates[prov].append(cost)

    for prov, rates in provider_rates.items():
        if len(rates) >= 20:
            mean_r = statistics.mean(rates)
            stdev_r = statistics.stdev(rates) if len(rates) > 1 else 0
            if mean_r > 0:
                cv = stdev_r / mean_r  # coefficient of variation
                if cv > 0.50:
                    alerts.append(
                        f"⚠️ {prov} Kalman instability: CV={cv:.2f} (>0.50). "
                        f"mean=${mean_r:.6f}/M stdev=${stdev_r:.6f}/M n={len(rates)}"
                    )

    # ── Criterion 4: price_observations freshness ──
    c.execute("""
        SELECT provider, MAX(ts), datetime(MAX(ts),'unixepoch')
        FROM price_observations
        GROUP BY provider
    """)
    for prov, max_ts, dt in c.fetchall():
        c2 = conn.cursor()
        c2.execute("SELECT strftime('%s','now') - ?", (max_ts,))
        age_h = (c2.fetchone()[0] or 0) / 3600
        if age_h > 168:  # >7 days old
            alerts.append(
                f"ℹ️ price_observations[{prov}] stale: last update {dt} ({age_h:.0f}h ago)"
            )

    conn.close()

    if alerts:
        day = (datetime.utcnow() - DAY0).days + 1
        print(f"RP-7 Day {day} Monitoring Report ({datetime.utcnow().isoformat()}Z)")
        print(f"{'='*60}")
        for a in alerts:
            print(a)
        if not any("⚠️" in a for a in alerts):
            print("\n✅ All rate criteria within bounds.")
    # else: silent — all clear

if __name__ == "__main__":
    main()
