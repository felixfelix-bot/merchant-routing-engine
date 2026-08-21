#!/usr/bin/env python3
"""routstrd_funding_guard.py — keep the routstrd wallet's NETWORK-spendable
ecash (real mints: minibits, cubabitcoin) above a 5,000-sat floor.

Run every 5 min from cron (installed by the tollgate-infrastructure-kit role
`routstrd_funding_guard`). Behavior:

  1. Read the cocod wallet per-mint balance over the unix socket.
  2. network_sats = sats held at every mint EXCEPT mint.orangesync.tech
     (orangesync is our private testnut mint — not accepted by network nodes).
  3. Log a 'routstrd_network' row into api_burn.db provider_balances so the
     efficiency monitor and proxy quota views can see the real float.
  4. If network_sats >= FLOOR (5000): clear the alert transition flag, exit.
  5. Else: reuse a fresh (<2h) top-up invoice or create a fixed 10k-sat
     Lightning invoice at the lower-balance real mint, then surface it:
       - ~/.hermes/bot/routstrd_topup_invoice.txt (invoice + lightning: URI)
       - notify-send persistent desktop notification
       - one-shot espeak voice alert per underfunded transition

Silent (exit 0) when healthy; one-line status when acting. Never raises.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import urllib.request
from pathlib import Path

HOME = Path.home()
# Resolve the routstrd CLI robustly: cron's PATH (/usr/bin:/bin) does not
# include ~/.bun/bin, which made every invoice-creation attempt raise
# FileNotFoundError and log "invoice creation FAILED" even though the mint
# was healthy (2026-08-21). Prefer $ROUTSTRD_BIN, then PATH, then the
# well-known bun install location.
ROUTSTRD_BIN = (os.environ.get("ROUTSTRD_BIN")
                or shutil.which("routstrd")
                or str(HOME / ".bun" / "bin" / "routstrd"))
COCOD_SOCK = HOME / ".cocod" / "cocod.sock"
BOT_DIR = HOME / ".hermes" / "bot"
API_BURN_DB = BOT_DIR / "api_burn.db"
STATE_FILE = BOT_DIR / ".routstrd_funding_guard_state.json"
INVOICE_FILE = BOT_DIR / "routstrd_topup_invoice.txt"

FLOOR_SATS = 5000
TOPUP_SATS = 10_000
INVOICE_FRESH_SECS = 2 * 3600
BTC_USD_RATE = float(os.environ.get("BTC_USD_RATE", "100000"))
TESTNUT_MINT = "https://mint.orangesync.tech"
REAL_MINTS = ["https://mint.minibits.cash/Bitcoin",
              "https://mint.cubabitcoin.org"]


def _read_balance() -> dict[str, int]:
    """Per-mint sat balance from the cocod daemon socket. Raises on failure."""
    r = subprocess.run(
        ["curl", "-s", "-m", "10", "--unix-socket", str(COCOD_SOCK),
         "http://localhost/balance"],
        capture_output=True, text=True, timeout=15)
    data = json.loads(r.stdout)
    out = data.get("output", data)
    return {mint: int(v.get("sats", 0)) for mint, v in out.items()}


def _log_balance(network_sats: int) -> None:
    usd = network_sats / 1e8 * BTC_USD_RATE
    try:
        conn = sqlite3.connect(API_BURN_DB)
        conn.execute(
            """INSERT INTO provider_balances
               (provider, collected_at, usage, limit_credits, limit_remaining,
                usage_fraction, is_unlimited, is_free_tier, raw_json)
               VALUES ('routstrd_network', ?, 0, 0, ?, 0, 0, 0, ?)""",
            (time.time(), round(usd, 6),
             json.dumps({"network_sats": network_sats, "btc_usd": BTC_USD_RATE})),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=1))
    except Exception:
        pass


def _create_invoice(mint: str) -> str | None:
    """Fixed top-up invoice via the routstrd CLI. Returns bolt11 or None."""
    try:
        # routstrd is a bun script with a `#!/usr/bin/env bun` shebang, so
        # BOTH the routstrd path and bun itself must be resolvable. Cron's
        # PATH (/usr/bin:/bin) has neither — augment it (2026-08-21).
        bun_bin = str(HOME / ".bun" / "bin")
        env = dict(os.environ)
        env["PATH"] = f"{bun_bin}:{env.get('PATH', '/usr/bin:/bin')}"
        r = subprocess.run(
            [ROUTSTRD_BIN, "wallet", "receive", "bolt11", str(TOPUP_SATS),
             "--mint-url", mint],
            capture_output=True, text=True, timeout=60, env=env)
        blob = (r.stdout or "") + (r.stderr or "")
        m = re.search(r"(lnbc[a-z0-9]+)", blob)
        return m.group(1) if m else None
    except Exception:
        return None


def _surface(invoice: str, network_sats: int) -> None:
    uri = f"lightning:{invoice}"
    try:
        INVOICE_FILE.write_text(
            f"routstrd wallet low: {network_sats} network sats (< {FLOOR_SATS})\n"
            f"Top up {TOPUP_SATS} sats:\n{invoice}\n{uri}\n"
            f"created: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n")
    except Exception:
        pass
    try:
        subprocess.run(
            ["notify-send", "-u", "critical", "-t", "0",
             "routstrd wallet low on real ecash",
             f"{network_sats} network sats (< {FLOOR_SATS}). "
             f"Top up {TOPUP_SATS} sats: {uri}"],
            timeout=15, check=False)
    except Exception:
        pass


def _espeak(msg: str) -> None:
    try:
        subprocess.run(["espeak-ng", msg], timeout=20, check=False)
    except Exception:
        pass


def main() -> int:
    if not COCOD_SOCK.exists():
        return 0
    try:
        balances = _read_balance()
    except Exception as e:
        print(f"⚠️ funding guard: balance read failed: {e}")
        return 0

    network = {m: s for m, s in balances.items() if m != TESTNUT_MINT}
    network_sats = sum(network.values())
    _log_balance(network_sats)

    if network_sats >= FLOOR_SATS:
        state = _load_state()
        if state.get("alerted"):
            state["alerted"] = False
            state.pop("invoice", None)
            _save_state(state)
        return 0

    state = _load_state()
    invoice = state.get("invoice") or ""
    ts = float(state.get("ts") or 0)
    fresh = invoice and (time.time() - ts) < INVOICE_FRESH_SECS

    if not fresh:
        real = {m: balances.get(m, 0) for m in REAL_MINTS}
        mint = min(real, key=real.get)
        invoice = _create_invoice(mint)
        if not invoice:
            print(f"🪫 funding guard: {network_sats} sats < {FLOOR_SATS} and "
                  f"invoice creation FAILED at {mint}")
            return 0
        state = {"invoice": invoice, "mint": mint, "ts": time.time()}
        print(f"🪫 funding guard: {network_sats} network sats < {FLOOR_SATS} "
              f"→ new {TOPUP_SATS}-sat invoice at {mint}")
    elif not state.get("alerted"):
        print(f"🪫 funding guard: still low ({network_sats} sats), "
              f"reusing invoice from {state.get('mint')}")

    state["alerted"] = True
    _save_state(state)
    _surface(invoice, network_sats)

    if not state.get("spoken"):
        _espeak(f"Warning. routstr wallet below {FLOOR_SATS // 1000} thousand sats. "
                f"Top up invoice created.")
        state["spoken"] = True
        _save_state(state)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
