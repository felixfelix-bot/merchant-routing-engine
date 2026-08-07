#!/usr/bin/env bash
# openrouter_balance_cron.sh — every-5min OpenRouter credit-balance collector.
#
# Mirrors ppq_balance_cron.sh. Runs src.openrouter_balance_collector, persisting
# one balance row to the shared provider_balances table that _snapshot_quota()
# (→ quota_state['openrouter'] via openrouter_quota_entry) reads.
#
# Watchdog semantics (matches operator's "silent when healthy" preference,
# cf. ppq_balance_cron.sh):
#   * healthy + credits remaining     → SILENT (empty stdout)
#   * healthy but credits EXHAUSTED   → one-line alert (→ +inf pressure)
#   * collection failed               → one-line alert
#
# Cron (no_agent=true) delivers stdout verbatim; empty stdout == silent, so a
# healthy OpenRouter account produces no chat noise. OPENROUTER_API_KEY is read
# from the env, falling back to ~/.hermes/.env (where the proxy loads it).
set -u

REPO="/home/c03rad0r/merchant-routing-engine"
ENV_FILE="$HOME/.hermes/.env"

# Load OPENROUTER_API_KEY from env, then ~/.hermes/.env. NOTE: the live key in
# .env is valid but NOT exported into the default shell env, so the cron MUST
# source it here (the collector's os.environ.get() only sees exported vars).
if [ -z "${OPENROUTER_API_KEY:-}" ] && [ -f "$ENV_FILE" ]; then
    OPENROUTER_API_KEY=$(grep -E '^OPENROUTER_API_KEY=' "$ENV_FILE" 2>/dev/null | grep -v '^#' \
        | head -1 \
        | sed -E 's/^OPENROUTER_API_KEY=//; s/#.*//; s/^[[:space:]]*//; s/[[:space:]]*$//; s/^["'"'"']//; s/["'"'"']$//')
    export OPENROUTER_API_KEY
fi

cd "$REPO" || { echo "⚠️ OpenRouter collector: cannot cd to $REPO"; exit 0; }

out=$(python3 -m src.openrouter_balance_collector 2>/dev/null)
rc=$?

if [ $rc -ne 0 ]; then
    echo "⚠️ OpenRouter balance collection FAILED (rc=$rc) — openrouter.ai/api/v1/key unreachable. Check OPENROUTER_API_KEY / network."
    exit 0
fi

# Parse the JSON status line the module prints.
read ok exhausted unlimited remaining < <(printf '%s' "$out" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    print(int(bool(d.get("ok"))), int(bool(d.get("is_exhausted"))),
          int(bool(d.get("is_unlimited"))), d.get("limit_remaining"))
except Exception:
    print("0 0 0 None")
' 2>/dev/null)

if [ "$ok" != "1" ]; then
    echo "⚠️ OpenRouter balance collection reported not-ok: ${out:-(no output)}"
    exit 0
fi

# Healthy collection — alert only when a FUNDED key is out of credits (→ +inf
# pressure). Unlimited keys never alert (they have no cap to exhaust).
if [ "$unlimited" != "1" ] && [ "$exhausted" = "1" ]; then
    echo "🪫 OpenRouter credits EXHAUSTED (limit_remaining=$remaining) — OpenRouter routing pressure is +inf; traffic diverts to cheaper tiers until reset/top-up."
fi
# else: silent (healthy, credits remaining or unlimited)
exit 0
