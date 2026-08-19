#!/usr/bin/env bash
# routstrd_balance_cron.sh — every-5min routstrd wallet (Cashu sats) collector.
#
# Reads the local cocod wallet (daemon socket) and writes a provider_balances
# row (provider='routstrd') so the proxy's quota_state and the efficiency
# monitor can see the spend-side float. Silent when healthy, one-line alert
# when empty/failing — mirrors the other balance crons.
set -u

SOCK="$HOME/.cocod/cocod.sock"
DB="$HOME/.hermes/bot/api_burn.db"
BTC_USD_RATE="${BTC_USD_RATE:-100000}"
STARTING_SATS="${ROUTSTRD_STARTING_SATS:-77000}"

[ -S "$SOCK" ] || exit 0  # cocod not installed → silent skip

out=$(curl -s -m 10 --unix-socket "$SOCK" http://localhost/balance 2>/dev/null)
[ -n "$out" ] || { echo "⚠️ routstrd balance read FAILED (socket: $SOCK)"; exit 0; }

python3 - "$out" "$BTC_USD_RATE" "$STARTING_SATS" "$DB" <<'PYEOF' || echo "⚠️ routstrd balance store failed"
import sys, json, sqlite3, time

raw, rate_s, starting_s, db = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
try:
    sats = int(sum(v.get("sats", 0) for v in json.loads(raw)["output"].values()))
except Exception:
    sys.exit(1)

starting_usd = starting_s / 1e8 * rate_s
usd = sats / 1e8 * rate_s
spent = max(0.0, starting_usd - usd)
frac = (1.0 - usd / starting_usd) if starting_usd > 0 else 1.0

conn = sqlite3.connect(db)
conn.execute(
    """INSERT INTO provider_balances
       (provider, collected_at, usage, limit_credits, limit_remaining,
        usage_fraction, is_unlimited, is_free_tier, raw_json)
       VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?)""",
    ("routstrd", time.time(), round(spent, 6), starting_usd, round(usd, 6),
     round(frac, 6), json.dumps({"balance_sats": sats, "btc_usd": rate_s})),
)
conn.commit()
conn.close()
if sats <= 0:
    print(f"🪫 routstrd wallet EMPTY ({sats} sats) — network purchases halted until top-up.")
sys.exit(0)
PYEOF
