#!/usr/bin/env bash
# neuralwatt_balance_cron.sh — every-5min NeuralWatt kWh-balance collector.
#
# Wrapper for scripts/collect_neuralwatt_balance.py — the Python script that
# reads NEURALWATT_API_KEY from ~/.hermes/profiles/manager/.env (bypassing
# the wrong key in os.environ), calls the real /v1/quota API, and stores
# fresh data in api_burn.db.
#
# This shell wrapper mirrors the pattern of ppq_balance_cron.sh,
# openrouter_balance_cron.sh, and routstr_balance_cron.sh. It is the entry
# point referenced in the crontab:
#
#   */5 * * * * /home/c03rad0r/merchant-routing-engine/scripts/neuralwatt_balance_cron.sh >> /home/c03rad0r/merchant-routing-engine/logs/neuralwatt_balance_collector.log 2>&1 # neuralwatt-balance-collector
#
# Watchdog semantics (same as other collectors):
#   * healthy + kWh remaining    → SILENT (empty stdout)
#   * healthy but kWh EXHAUSTED  → one-line alert
#   * collection failed          → one-line alert
set -u

REPO="/home/c03rad0r/merchant-routing-engine"

cd "$REPO" || { echo "⚠️ NeuralWatt collector: cannot cd to $REPO"; exit 0; }

out=$(python3 scripts/collect_neuralwatt_balance.py 2>/dev/null)
rc=$?

if [ $rc -ne 0 ]; then
    echo "⚠️ NeuralWatt balance collection FAILED (rc=$rc) — /v1/quota unreachable. Check .env key / network."
    exit 0
fi

# Parse the JSON status line the script prints.
read ok exhausted kwh_rem daily_cap < <(printf '%s' "$out" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    print(int(bool(d.get("ok"))), int(bool(d.get("is_exhausted"))), d.get("kwh_remaining"), int(bool(d.get("is_daily_cap_exceeded"))))
except Exception:
    print("0 0 None 0")
' 2>/dev/null)

if [ "$ok" != "1" ]; then
    echo "⚠️ NeuralWatt balance collection reported not-ok: ${out:-(no output)}"
    exit 0
fi

# Alert when kWh exhausted or daily cap exceeded.
if [ "$exhausted" = "1" ]; then
    echo "🔋 NeuralWatt kWh EXHAUSTED (remaining=$kwh_rem) — routing pressure is +inf; traffic diverts to cheaper tiers until period reset."
elif [ "$daily_cap" = "1" ]; then
    echo "🪫 NeuralWatt daily cap EXCEEDED — router drops neuralwatt from rotation until UTC midnight."
fi
# else: silent (healthy, kWh remaining, under daily cap)
exit 0