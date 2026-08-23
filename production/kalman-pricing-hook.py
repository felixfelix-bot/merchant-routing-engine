#!/usr/bin/env python3
"""Kalman pricing hook for routstrd.

Fetches Kalman-aware pricing from DQ05's zai_proxy via the reverse SSH tunnel
(localhost:9098) and updates the zai-coding upstream provider's fee in the
routstrd database. Runs every 2 minutes via cron.

stdlib only — no pip installs required.

Fail-safe: if the tunnel is down or the endpoint errors, the script logs and
exits silently. routstrd keeps its existing litellm-based pricing.
"""
import json
import os
import sqlite3
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

# ── Configuration ────────────────────────────────────────────────────────────
KALMAN_PRICING_URL = "http://127.0.0.1:9098/kalman-pricing"
# Host-side path to the routstrd DB (Docker volume mount)
DB_PATH = "/var/lib/docker/volumes/routstr-public_routstr_public_data/_data/keys.db"
# Fallback: use docker exec if the host path isn't accessible
DB_DOCKER_CMD = ("docker", "exec", "routstr-public", "/.venv/bin/python3")
# The zai-coding provider slug in routstrd
ZAI_SLUG = "zai-coding"
# litellm's published per-token rate for GLM models ($/M tokens).
# This is the baseline that routstrd's litellm pricing uses.
# GLM-4.5 blended rate ≈ $0.07-0.20/M; we use the midpoint.
LITELLM_ZAI_BASE_RATE = 0.14
# Log file
LOG_FILE = "/var/log/kalman-pricing-hook.log"
# Maximum fee multiplier (prevent runaway pricing)
MAX_FEE = 10.0
MIN_FEE = 0.01


def log(msg: str) -> None:
    """Append a timestamped line to the log file."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass  # logging must never break the hook
    print(line, flush=True)


def fetch_kalman_pricing() -> dict | None:
    """Fetch the kalman-pricing JSON from the reverse tunnel endpoint.
    Returns None on any error (tunnel down, parse error, etc.)."""
    try:
        req = urllib.request.Request(KALMAN_PRICING_URL, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                log(f"HTTP {resp.status} from kalman-pricing endpoint")
                return None
            return json.loads(resp.read())
    except urllib.error.URLError as e:
        log(f"Tunnel unreachable: {e}")
        return None
    except Exception as e:
        log(f"fetch_kalman_pricing error: {e}")
        return None


def update_provider_db(enabled: bool, fee: float) -> bool:
    """Update the zai-coding provider in the routstrd DB.
    Tries direct sqlite3 first, falls back to docker exec.
    Returns True on success, False on failure."""
    # Try direct sqlite3 access (host volume mount)
    try:
        if os.path.exists(DB_PATH):
            conn = sqlite3.connect(DB_PATH, timeout=5)
            conn.execute(
                "UPDATE upstream_providers SET enabled=?, provider_fee=? WHERE slug=?",
                (1 if enabled else 0, round(fee, 4), ZAI_SLUG),
            )
            conn.commit()
            conn.close()
            return True
    except Exception as e:
        log(f"Direct DB update failed: {e}")

    # Fallback: docker exec
    try:
        import subprocess
        sql = (
            f"import sqlite3; db=sqlite3.connect('/app/data/keys.db'); "
            f"db.execute('UPDATE upstream_providers SET enabled={1 if enabled else 0}, "
            f"provider_fee={round(fee, 4)} WHERE slug=\"{ZAI_SLUG}\"'); "
            f"db.commit(); print('OK')"
        )
        result = subprocess.run(
            list(DB_DOCKER_CMD) + ["-c", sql],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return True
        else:
            log(f"Docker exec failed: {result.stderr.strip()}")
            return False
    except Exception as e:
        log(f"Docker exec fallback failed: {e}")
        return False


def verify_provider_db() -> dict | None:
    """Read current zai-coding state from the DB for verification."""
    try:
        if os.path.exists(DB_PATH):
            conn = sqlite3.connect(DB_PATH, timeout=5)
            c = conn.cursor()
            c.execute(
                "SELECT slug, provider_fee, enabled FROM upstream_providers WHERE slug=?",
                (ZAI_SLUG,),
            )
            row = c.fetchone()
            conn.close()
            if row:
                return {"slug": row[0], "provider_fee": row[1], "enabled": bool(row[2])}
    except Exception:
        pass
    return None


def main() -> int:
    # 1. Fetch Kalman pricing
    data = fetch_kalman_pricing()
    if data is None:
        # Tunnel down or error — skip silently, routstrd keeps litellm prices
        log("No pricing data (tunnel down?) — skipping, routstrd keeps litellm prices")
        return 0

    zai_available = data.get("zai_available", False)
    zai_price = data.get("zai_effective_price_usd_per_m")
    locked_reason = data.get("zai_locked_reason")

    if not zai_available or zai_price is None:
        # z.ai is unavailable — disable the zai-coding upstream
        log(f"z.ai unavailable (reason: {locked_reason}) — disabling {ZAI_SLUG}")
        if update_provider_db(enabled=False, fee=1.43):
            log(f"Disabled {ZAI_SLUG} in routstrd DB")
        else:
            log(f"Failed to disable {ZAI_SLUG}")
        return 0

    # 2. Calculate the provider_fee multiplier
    # kalman_fee = zai_effective_price / litellm_base_rate
    # This scales the fee UP when quota is scarce (peak hours, near exhaustion)
    # and DOWN when quota is healthy (cheap z.ai attracts volume)
    kalman_fee = zai_price / LITELLM_ZAI_BASE_RATE
    kalman_fee = max(MIN_FEE, min(MAX_FEE, kalman_fee))

    # 3. Update the DB
    log(
        f"z.ai available — effective_price=${zai_price:.6f}/M, "
        f"litellm_base=${LITELLM_ZAI_BASE_RATE}/M, "
        f"fee={kalman_fee:.4f} (peak={data.get('is_peak_hour', False)})"
    )

    if update_provider_db(enabled=True, fee=kalman_fee):
        # Verify
        state = verify_provider_db()
        if state:
            log(f"Updated {ZAI_SLUG}: fee={state['provider_fee']}, enabled={state['enabled']}")
        else:
            log(f"Updated {ZAI_SLUG} (verification skipped)")
    else:
        log(f"Failed to update {ZAI_SLUG}")

    return 0


if __name__ == "__main__":
    exit(main())