#!/usr/bin/env bash
# rate_refresh_cron.sh — hourly measured-rate refresh (RP-5b).
#
# Runs src.real_price_tracker (collect_rates via its __main__), which:
#   1. clears the 5-min cost_usd cache + 1-day z.ai amortized cache,
#   2. recomputes the full trailing-rate set {provider: {model: $/M}},
#   3. reports readiness + the T6 gate + any >50% 24h-vs-7d price changes.
#
# This keeps the RP-5a dashboard and the router's base-rate dict warm with fresh
# numbers without waiting for the first request after a cache expiry.
#
# Watchdog semantics (matches the operator's "silent when healthy" preference,
# cf. ppq_balance_cron.sh / openrouter_balance_cron.sh):
#   * refresh ok, no price change      → SILENT (empty stdout)
#   * refresh ok, price change detected → one-line alert naming the providers
#   * refresh FAILED                   → one-line alert
#
# Cron delivers stdout verbatim; empty stdout == silent, so a stable pricing
# picture produces no chat noise. Gate status (cold-start seeds still in effect)
# is NOT alerted on hourly — that's expected during warm-up and is reported in
# the module's own log, not as a watchdog ping.
set -u

REPO="/home/c03rad0r/merchant-routing-engine"

cd "$REPO" || { echo "⚠️ rate-refresh: cannot cd to $REPO"; exit 0; }

out=$(python3 -m src.real_price_tracker 2>/dev/null)
rc=$?

if [ $rc -ne 0 ]; then
    echo "⚠️ Rate refresh FAILED (rc=$rc) — src.real_price_tracker raised. Check ~/.hermes/bot/zai_usage.db / logs. Output: ${out:-(none)}"
    exit 0
fi

# Parse the JSON status line the module prints.
read ok gate_passed n_providers n_models changes_str < <(printf '%s' "$out" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    changes = ",".join(d.get("price_changes") or []) or "-"
    print(int(bool(d.get("ok"))), int(bool(d.get("gate_passed"))),
          d.get("n_providers"), d.get("n_measured_models"), changes)
except Exception:
    print("0 0 0 0 -")
' 2>/dev/null)

if [ "$ok" != "1" ]; then
    echo "⚠️ Rate refresh reported not-ok: ${out:-(no output)}"
    exit 0
fi

# Alert only when a provider's 24h rate moved >50% vs its 7d baseline.
if [ "$changes_str" != "-" ] && [ -n "$changes_str" ]; then
    echo "📊 Price change detected on provider(s): ${changes_str} — 24h rate deviates >50% from 7d baseline (providers=${n_providers}, measured_models=${n_models}, gate=${gate_passed})."
fi
# else: silent (refresh ok, prices stable)
exit 0
