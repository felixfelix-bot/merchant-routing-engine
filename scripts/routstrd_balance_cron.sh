#!/usr/bin/env bash
# routstrd_balance_cron.sh — every-5min routstrd wallet (Cashu sats) collector.
#
# Reads the local routstrd wallet and writes a provider_balances row
# (provider='routstrd') so the proxy's quota_state and the efficiency
# monitor can see the spend-side float. Silent when healthy, one-line alert
# when empty/failing — mirrors the other balance crons.
#
# 2026-09-26 — WALLET MIGRATION (path repoint): the cocod daemon and its unix
# socket "$HOME/.cocod/cocod.sock" are gone; "$HOME/.cocod/coco.db" is a 0-byte
# legacy file (preserved at "$HOME/.cocod/wallet-migrated-*"). The active
# wallet is "$HOME/.routstrd/wallet/coco.db" — 17 tables incl.
# coco_cashu_proofs (mintUrl/amount/state), owned by the routstrd daemon on
# 127.0.0.1:8008.
#
# The wallet is read through scripts/routstrd_funding_guard.py --balance-json
# (daemon CLI first, WAL-safe sqlite fallback) so there is exactly ONE reader
# implementation — no duplicated cocod-era wallet logic. A read failure is an
# EXPLICIT non-zero exit, never a silent "0 sats" row.
#
# Balance recorded = network_sats (every mint except the private testnut mint
# https://mint.orangesync.tech, which network nodes do not accept). The full
# per-mint breakdown is kept in raw_json.
set -u

REPO="${ROUTSTRD_REPO:-/home/c03rad0r/merchant-routing-engine}"
GUARD="$REPO/scripts/routstrd_funding_guard.py"
WALLET_DB="${ROUTSTRD_WALLET_DB:-$HOME/.routstrd/wallet/coco.db}"
DB="${API_BURN_DB:-$HOME/.hermes/bot/api_burn.db}"
BTC_USD_RATE="${BTC_USD_RATE:-100000}"
STARTING_SATS="${ROUTSTRD_STARTING_SATS:-77000}"

[ -f "$GUARD" ] || \
    { echo "⚠️ routstrd balance read FAILED (reader missing: $GUARD)"; exit 1; }

out=$(python3 "$GUARD" --balance-json 2>&1) || \
    { echo "⚠️ routstrd balance read FAILED (wallet: $WALLET_DB) — $out"; exit 1; }

python3 - "$out" "$BTC_USD_RATE" "$STARTING_SATS" "$DB" <<'PYEOF' || \
    { echo "⚠️ routstrd balance store failed"; exit 1; }
import sys, json, sqlite3, time

raw, rate_s, starting_s, db = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
try:
    payload = json.loads(raw)
    sats = int(payload["network_sats"])   # excludes the private testnut mint
    per_mint = payload.get("balances", {})
except Exception as e:
    print(f"⚠️ routstrd balance payload unusable ({e}): {raw[:200]}")
    sys.exit(1)

starting_usd = starting_s / 1e8 * rate_s
usd = sats / 1e8 * rate_s
spent = max(0.0, starting_usd - usd)
frac = (1.0 - usd / starting_usd) if starting_usd > 0 else 1.0

try:
    conn = sqlite3.connect(db)
    conn.execute(
        """INSERT INTO provider_balances
           (provider, collected_at, usage, limit_credits, limit_remaining,
            usage_fraction, is_unlimited, is_free_tier, raw_json)
           VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?)""",
        ("routstrd", time.time(), round(spent, 6), starting_usd, round(usd, 6),
         round(frac, 6),
         json.dumps({"balance_sats": sats, "btc_usd": rate_s,
                     "network_sats": sats, "per_mint": per_mint,
                     "source": payload.get("source"),
                     "wallet_db": payload.get("wallet_db")})),
    )
    conn.commit()
    conn.close()
except Exception as e:
    print(f"⚠️ routstrd balance store failed ({e})")
    sys.exit(1)

if sats <= 0:
    print(f"🪫 routstrd wallet EMPTY ({sats} network sats) — network purchases halted until top-up.")
sys.exit(0)
PYEOF
