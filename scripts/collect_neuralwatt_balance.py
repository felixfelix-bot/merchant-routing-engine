#!/usr/bin/env python3
"""NeuralWatt balance collector — refreshes NW kWh state every 5 minutes.

WHY THIS EXISTS
════════════════
The NeuralWatt balance bridge was critically broken: the collector in
src/balance_collectors.py reads NEURALWATT_API_KEY from os.environ, but the
environment has the WRONG key (sk-d843...) while the correct key (sk-76de...)
lives only in ~/.hermes/profiles/manager/.env. This caused every /v1/quota
call to return 401, so no fresh rows were ever written to api_burn.db. The
proxy's _neuralwatt_quota_entry_fn reads from api_burn.db with a 20-minute
max_age — with no fresh data, it returns {} and the proxy falls back to
{used_pct:0.0, remaining:inf}, treating NeuralWatt as free/unlimited when
it's nearly out of kWh (1.93/13.33 remaining, $8.99 credits).

WHAT THIS SCRIPT DOES
══════════════════════
1. Reads NEURALWATT_API_KEY directly from ~/.hermes/profiles/manager/.env
   (same path the proxy's _load_external_keys() uses), BYPASSING os.environ
   so the wrong key never interferes.
2. Calls collect_and_store_neuralwatt() with the key passed explicitly,
   which hits GET https://api.neuralwatt.com/v1/quota (balance + subscription)
   and GET https://api.neuralwatt.com/v1/usage/summary (today's cost_usd).
3. The function stores a fresh row in the provider_balances table of
   ~/.hermes/bot/api_burn.db with method='real-api'.
4. Prints a JSON status line (same pattern as other collectors).
5. Handles 401/network errors gracefully — logs and exits 1, never crashes.

CRON ENTRY (every 5 minutes)
════════════════════════════
*/5 * * * * /home/c03rad0r/merchant-routing-engine/scripts/collect_neuralwatt_balance.py >> /home/c03rad0r/merchant-routing-engine/logs/neuralwatt_balance_collector.log 2>&1

SECURITY
════════
The API key is read from the .env file at runtime — it is NEVER hardcoded,
logged, or printed in the JSON output. Only the first 8 chars are shown in
error messages for debugging (rest masked).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

# ── Resolve repo root so we can import src.balance_collectors ───────────────
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

from src.balance_collectors import (  # noqa: E402
    collect_and_store_neuralwatt,
    default_db_path,
    NEURALWATT_KEY_ENV,
)


def _load_key_from_env_file() -> Optional[str]:
    """Read NEURALWATT_API_KEY from the .env file, bypassing os.environ.

    The proxy's _load_external_keys() reads from ~/.hermes/profiles/manager/.env.
    We do the same here so we get the CORRECT key even when os.environ has a
    stale/wrong one (the known sk-d843... vs sk-76de... mismatch).

    Checks both ~/.hermes/profiles/manager/.env and ~/.hermes/.env for
    resilience (same fallback order as routstr_balance_cron.sh).
    """
    env_candidates = [
        Path.home() / ".hermes" / "profiles" / "manager" / ".env",
        Path.home() / ".hermes" / ".env",
    ]
    for env_path in env_candidates:
        if not env_path.is_file():
            continue
        try:
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith(f"{NEURALWATT_KEY_ENV}="):
                    val = line.split("=", 1)[1]
                    # Strip inline comments, quotes, whitespace
                    val = val.split("#")[0].strip().strip("'").strip('"')
                    if val:
                        return val
        except (OSError, UnicodeDecodeError):
            continue
    return None


def _mask_key(key: str) -> str:
    """Show first 8 chars + mask rest, for safe error logging."""
    if len(key) <= 8:
        return key[:2] + "***"
    return key[:8] + "..." + f"(len={len(key)})"


def main() -> int:
    """Collect NeuralWatt balance from the real API, store in api_burn.db.

    Returns 0 on success, 1 on failure. Never raises.
    """
    db_path = default_db_path()

    # Step 1: Get the CORRECT key from .env (bypassing os.environ)
    key = _load_key_from_env_file()

    if not key:
        # Fallback: try os.environ (may have wrong key, but worth trying)
        key = os.environ.get(NEURALWATT_KEY_ENV, "").strip() or None

    if not key:
        print(json.dumps({
            "provider": "neuralwatt",
            "ok": False,
            "error": f"{NEURALWATT_KEY_ENV} not found in .env or environment",
        }))
        return 1

    # Step 2: Collect + store (pass key explicitly to bypass os.environ lookup)
    try:
        balance = collect_and_store_neuralwatt(
            db_path=db_path,
            api_key=key,  # explicit key from .env — overrides os.environ
        )
    except Exception as exc:
        print(json.dumps({
            "provider": "neuralwatt",
            "ok": False,
            "error": f"unexpected exception: {exc}",
            "key_hint": _mask_key(key),
        }))
        return 1

    if balance is None:
        # Collection failed — could be 401 (wrong key), network, or parse error
        key_hint = _mask_key(key)
        print(json.dumps({
            "provider": "neuralwatt",
            "ok": False,
            "error": f"/v1/quota API call failed (key={key_hint}, check .env file)",
            "db_path": db_path,
        }))
        return 1

    # Step 3: Print success JSON (same pattern as _neuralwatt_main in the module)
    print(json.dumps({
        "provider": "neuralwatt",
        "ok": True,
        "method": "real-api",
        "remaining_usd": balance.remaining_usd,
        "total_credits_usd": balance.total_credits_usd,
        "kwh_used": balance.kwh_used,
        "kwh_remaining": balance.kwh_remaining,
        "kwh_included": balance.kwh_included,
        "usage_fraction": balance.usage_fraction,
        "used_pct": balance.used_pct,
        "cost_usd_lifetime": balance.cost_usd,
        "subscription_status": balance.subscription_status,
        "period_end": balance.period_end,
        "is_exhausted": balance.is_exhausted,
        "daily_spent_usd": balance.daily_spent_usd,
        "daily_cap_usd": balance.daily_cap_usd,
        "is_daily_cap_exceeded": balance.is_daily_cap_exceeded,
        "collected_at": balance.collected_at,
        "remaining": balance.kwh_remaining,
        "total": balance.kwh_included,
        "db_path": db_path,
    }, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())