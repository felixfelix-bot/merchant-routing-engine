#!/usr/bin/env bash
# ppq_balance_cron.sh — every-5min PPQ credit-balance collector (P3-PPQ STEP 2).
#
# Runs src.ppq_balance_collector, persisting one balance row to the shared
# provider_balances table that _snapshot_quota() (→ quota_state['ppq']) reads.
#
# Watchdog semantics (matches the operator's "silent when healthy" preference,
# cf. manager/scripts/dq05-health-check.sh):
#   * healthy + credits remaining  → SILENT (empty stdout)
#   * healthy but credits EXHAUSTED → one-line alert (balance<=0 → +inf pressure)
#   * collection failed            → one-line alert
#
# Cron (no_agent=true) delivers stdout verbatim; empty stdout == silent, so a
# healthy PPQ account produces no chat noise. PPQ_API_KEY is read from the env,
# falling back to ~/.hermes/.env (where the proxy loads it).
set -u

REPO="/home/c03rad0r/merchant-routing-engine"
ENV_FILE="$HOME/.hermes/.env"

# Load PPQ_API_KEY from env, then ~/.hermes/.env.
if [ -z "${PPQ_API_KEY:-}" ] && [ -f "$ENV_FILE" ]; then
    PPQ_API_KEY=$(grep -E '^PPQ_API_KEY=' "$ENV_FILE" 2>/dev/null | head -1 \
        | sed -E 's/^PPQ_API_KEY=//; s/#.*//; s/^[[:space:]]*//; s/[[:space:]]*$//; s/^["'\'']//; s/["'\'']$//')
    export PPQ_API_KEY
fi

cd "$REPO" || { echo "⚠️ PPQ collector: cannot cd to $REPO"; exit 0; }

out=$(python3 -m src.balance_collectors --provider ppq 2>/dev/null)
rc=$?

if [ $rc -ne 0 ]; then
    echo "⚠️ PPQ balance collection FAILED (rc=$rc) — api.ppq.ai/credits/balance unreachable. Check PPQ_API_KEY / network."
    exit 0
fi

# Parse the JSON status line the module prints.
read ok exhausted balance < <(printf '%s' "$out" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    print(int(bool(d.get("ok"))), int(bool(d.get("is_exhausted"))), d.get("balance"))
except Exception:
    print("0 0 None")
' 2>/dev/null)

if [ "$ok" != "1" ]; then
    echo "⚠️ PPQ balance collection reported not-ok: ${out:-(no output)}"
    exit 0
fi

# Healthy collection — alert only when credits are exhausted (→ +inf pressure).
if [ "$exhausted" = "1" ]; then
    echo "🪫 PPQ credits EXHAUSTED (balance=$balance) — PPQ routing pressure is +inf; traffic diverts to cheaper tiers until top-up."
fi
# else: silent (healthy, credits remaining)
exit 0
