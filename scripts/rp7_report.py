#!/usr/bin/env python3
"""
RP-7 7-Day Report Generator — run after 7 days of monitoring.

Produces a markdown report of real rates per provider per model,
cost_usd population trend, and Kalman convergence assessment.
"""
import sqlite3
import os
import statistics
from datetime import datetime, timedelta
from collections import defaultdict

DB = os.path.expanduser("~/.hermes/bot/zai_usage.db")

def pct(part, total):
    return (part / total * 100) if total else 0

def main():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    now = datetime.utcnow()
    report = []
    report.append(f"# RP-7 7-Day Convergence Report")
    report.append(f"Generated: {now.isoformat()}Z\n")

    # ── Summary: cost population over 7 days ──
    report.append("## Cost Population (last 7 days)\n")
    report.append("| Day | Date | Total Calls | Populated | % |")
    report.append("|-----|------|-------------|-----------|---|")

    for days_ago in range(7, 0, -1):
        day_start = (now - timedelta(days=days_ago)).strftime('%s')
        day_end = (now - timedelta(days=days_ago - 1)).strftime('%s')
        c.execute("""
            SELECT COUNT(*),
                   SUM(CASE WHEN live_cost IS NOT NULL AND live_cost > 0 THEN 1 ELSE 0 END)
            FROM routing_shadow_decisions
            WHERE ts >= ? AND ts < ?
        """, (day_start, day_end))
        total, populated = c.fetchone()
        total = total or 0
        populated = populated or 0
        day_date = (now - timedelta(days=days_ago)).strftime('%Y-%m-%d')
        report.append(f"| {7 - days_ago + 1} | {day_date} | {total} | {populated} | {pct(populated, total):.1f}% |")

    # ── Per-provider rates (7d average) ──
    report.append("\n## Real Rates Per Provider (7-day average)\n")
    report.append("| Provider | Calls | Avg $/M | Min $/M | Max $/M | StDev | CV |")
    report.append("|----------|-------|---------|---------|---------|-------|----|")

    c.execute("""
        SELECT live_provider,
               COUNT(*) as n,
               AVG(live_cost) as avg_rate,
               MIN(live_cost) as min_rate,
               MAX(live_cost) as max_rate
        FROM routing_shadow_decisions
        WHERE live_cost IS NOT NULL AND live_cost > 0
          AND ts > strftime('%s','now','-7 day')
        GROUP BY live_provider
        ORDER BY n DESC
    """)
    provider_stats = c.fetchall()
    for prov, n, avg_r, min_r, max_r in provider_stats:
        # Get stdev
        c.execute("""
            SELECT live_cost FROM routing_shadow_decisions
            WHERE live_provider = ? AND live_cost IS NOT NULL AND live_cost > 0
              AND ts > strftime('%s','now','-7 day')
        """, (prov,))
        rates = [r[0] for r in c.fetchall()]
        stdev = statistics.stdev(rates) if len(rates) > 1 else 0
        cv = (stdev / avg_r) if avg_r and avg_r > 0 else 0
        report.append(
            f"| {prov} | {n} | ${avg_r:.6f} | ${min_r:.6f} | ${max_r:.6f} | "
            f"${stdev:.6f} | {cv:.3f} |"
        )

    # ── Kalman convergence assessment ──
    report.append("\n## Kalman Convergence Assessment\n")
    converged = []
    not_converged = []
    for prov, n, avg_r, min_r, max_r in provider_stats:
        c.execute("""
            SELECT live_cost FROM routing_shadow_decisions
            WHERE live_provider = ? AND live_cost IS NOT NULL AND live_cost > 0
              AND ts > strftime('%s','now','-7 day')
        """, (prov,))
        rates = [r[0] for r in c.fetchall()]
        if len(rates) > 1:
            stdev = statistics.stdev(rates)
            cv = stdev / avg_r if avg_r > 0 else float('inf')
            status = "✅ CONVERGED" if cv < 0.30 else ("⚠️ STABILIZING" if cv < 0.50 else "❌ OSCILLATING")
            line = f"- **{prov}**: CV={cv:.3f}, mean=${avg_r:.6f}/M, stdev=${stdev:.6f}/M, n={n} — {status}"
            if cv < 0.30:
                converged.append(line)
            else:
                not_converged.append(line)

    for line in converged + not_converged:
        report.append(line)

    # ── price_observations ──
    report.append("\n## Price Observations (tracker data)\n")
    report.append("| Provider | Rate $/M | Source | Measured | Confidence |")
    report.append("|----------|---------|--------|----------|------------|")
    c.execute("""
        SELECT provider, rate_per_m, source, is_measured, confidence
        FROM price_observations
        WHERE id IN (SELECT MAX(id) FROM price_observations GROUP BY provider)
        ORDER BY provider
    """)
    for prov, rate, source, measured, conf in c.fetchall():
        report.append(
            f"| {prov} | ${rate:.6f} | {source} | {'Yes' if measured else 'No'} | {conf:.2f} |"
        )

    # ── Verdict ──
    report.append("\n## Overall Verdict\n")
    n_converged = len(converged)
    n_total = len(converged) + len(not_converged)
    # Check cost population for last 3 days
    pop_ok = True
    for days_ago in range(3, 0, -1):
        day_start = (now - timedelta(days=days_ago)).strftime('%s')
        day_end = (now - timedelta(days=days_ago - 1)).strftime('%s')
        c.execute("""
            SELECT COUNT(*),
                   SUM(CASE WHEN live_cost IS NOT NULL AND live_cost > 0 THEN 1 ELSE 0 END)
            FROM routing_shadow_decisions
            WHERE ts >= ? AND ts < ?
        """, (day_start, day_end))
        total, populated = c.fetchone()
        if total and pct(populated or 0, total) < 80.0:
            pop_ok = False

    if n_total > 0 and n_converged / n_total >= 0.7 and pop_ok:
        report.append("**✅ SYSTEM HEALTHY** — rates stable, cost tracking operational.")
    elif n_converged > 0:
        report.append("**⚠️ PARTIAL** — some providers converging, monitor continues.")
    else:
        report.append("**❌ NOT CONVERGED** — rates unstable, investigate.")

    conn.close()

    output = "\n".join(report)
    print(output)

    # Save to file
    report_path = os.path.expanduser(
        f"~/merchant-routing-engine/docs/rp7-7day-report-{now.strftime('%Y%m%d')}.md"
    )
    with open(report_path, 'w') as f:
        f.write(output)
    print(f"\n--- Report saved to {report_path} ---", file=__import__('sys').stderr)

if __name__ == "__main__":
    main()
