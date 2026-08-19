#!/usr/bin/env bash
# routstr_balance_cron.sh — every-5min Routstr (VPS2 node) sats-balance collector.
#
# Runs src.balance_collectors --provider routstr, persisting one balance row to
# the shared provider_balances table that _snapshot_quota() (→
# quota_state['routstr']) reads. Mirrors ppq_balance_cron.sh semantics:
#   * healthy + sats remaining   → SILENT (empty stdout)
#   * healthy but wallet EMPTY   → one-line alert
#   * collection failed          → one-line alert
set -u

REPO="/home/c03rad0r/merchant-routing-engine"
ENV_FILE="$HOME/.hermes/.env"
ENV_FILE2="$HOME/.hermes/profiles/manager/.env"

# Load ROUTSTR_API_KEY / ROUTSTR_BASE from env, then ~/.hermes/.env, then manager/.env.
for f in "$ENV_FILE" "$ENV_FILE2"; do
    if [ -f "$f" ]; then
        if [ -z "${ROUTSTR_API_KEY:-}" ]; then
            ROUTSTR_API_KEY=$(grep -E '^ROUTSTR_API_KEY=' "$f" 2>/dev/null | head -1 \
                | sed -E 's/^ROUTSTR_API_KEY=//; s/#.*//; s/^[[:space:]]*//; s/[[:space:]]*$//; s/^["'\'']//; s/["'\'']$//')
            export ROUTSTR_API_KEY
        fi
        if [ -z "${ROUTSTR_BASE:-}" ]; then
            ROUTSTR_BASE=$(grep -E '^ROUTSTR_BASE=' "$f" 2>/dev/null | head -1 \
                | sed -E 's/^ROUTSTR_BASE=//; s/#.*//; s/^[[:space:]]*//; s/[[:space:]]*$//; s/^["'\'']//; s/["'\'']$//')
            export ROUTSTR_BASE
        fi
    fi
done

cd "$REPO" || { echo "⚠️ Routstr collector: cannot cd to $REPO"; exit 0; }

# No key yet (VPS2 key creation pending) → silent skip, not an alert.
if [ -z "${ROUTSTR_API_KEY:-}" ]; then
    exit 0
fi

out=$(python3 -m src.balance_collectors --provider routstr 2>/dev/null)
rc=$?

if [ $rc -ne 0 ]; then
    echo "⚠️ Routstr balance collection FAILED (rc=$rc) — VPS2 node unreachable?"
    exit 0
fi

read ok exhausted sats < <(printf '%s' "$out" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    print(int(bool(d.get("ok"))), int(bool(d.get("is_exhausted"))), d.get("balance_sats"))
except Exception:
    print("0 0 None")
' 2>/dev/null)

if [ "$ok" != "1" ]; then
    echo "⚠️ Routstr balance collection reported not-ok: ${out:-(no output)}"
    exit 0
fi

if [ "$exhausted" = "1" ]; then
    echo "🪫 Routstr wallet EMPTY (sats=$sats) — node failover disabled until top-up."
fi
# else: silent (healthy, sats remaining)
exit 0
