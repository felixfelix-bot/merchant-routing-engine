"""Verify backfill integrity: does sum(backfilled cost) reconcile with daily_spend?"""
import sqlite3
import os

db = os.path.expanduser("~/.hermes/bot/zai_usage.db")
con = sqlite3.connect(db)
cur = con.cursor()

print("=== daily_spend totals by tier ===")
dspend_total = 0.0
for tier, tok, spend in cur.execute(
    "SELECT tier, SUM(token_count), SUM(spend_usd) FROM daily_spend GROUP BY tier ORDER BY 3 DESC"
).fetchall():
    print(f"  {tier:15s} tokens={tok:>15,}  spend=${spend:.4f}")
    dspend_total += spend
print(f"  {'TOTAL':15s} {'':>15s}  spend=${dspend_total:.4f}")

print()
print("=== reconciliation: for each matched (date,tier), compare tokens & spend ===")
# matched daily_spend rows joined against the backfilled api_calls aggregates
rows = cur.execute(
    """
    SELECT d.tier, d.date,
           d.token_count      AS ds_tokens,
           COALESCE(SUM(a.total_tokens), 0) AS ac_tokens,
           d.spend_usd        AS ds_spend,
           COALESCE(SUM(a.cost_usd), 0)     AS ac_cost
    FROM daily_spend d
    LEFT JOIN api_calls a
      ON a.key_name = d.tier
     AND date(a.ts, 'unixepoch', 'localtime') = d.date
     AND a.cost_source = 'backfilled'
    GROUP BY d.tier, d.date
    HAVING ds_tokens > 0
    ORDER BY d.spend_usd DESC
    LIMIT 15
    """
).fetchall()
print(f"  {'tier':12s} {'date':12s} {'ds_tok':>14s} {'ac_tok':>14s} {'tok_ratio':>9s} "
      f"{'ds_spend':>10s} {'ac_cost':>10s} {'cost_ratio':>10s}")
grand_ac_cost = 0.0
grand_ds_spend = 0.0
for tier, date, ds_tok, ac_tok, ds_spend, ac_cost in rows:
    tok_ratio = ac_tok / ds_tok if ds_tok else 0
    cost_ratio = ac_cost / ds_spend if ds_spend else 0
    grand_ac_cost += ac_cost
    grand_ds_spend += ds_spend
    print(f"  {tier:12s} {date:12s} {ds_tok:>14,} {ac_tok:>14,} {tok_ratio:>9.3f} "
          f"{ds_spend:>10.4f} {ac_cost:>10.4f} {cost_ratio:>10.3f}")
print(f"  (top-15 shown) cumulative ds_spend=${grand_ds_spend:.4f} ac_cost=${grand_ac_cost:.4f}")

print()
print("=== which daily_spend tiers had ZERO matching api_calls? (spend not allocated) ===")
for tier, n_dates, tok, spend in cur.execute(
    """
    SELECT d.tier, COUNT(*), SUM(d.token_count), SUM(d.spend_usd)
    FROM daily_spend d
    WHERE NOT EXISTS (
      SELECT 1 FROM api_calls a
      WHERE a.key_name = d.tier AND a.cost_source = 'backfilled'
    )
    GROUP BY d.tier
    """
).fetchall():
    print(f"  {tier:12s} dates={n_dates} tokens={tok:,} spend=${spend:.4f}")

con.close()
