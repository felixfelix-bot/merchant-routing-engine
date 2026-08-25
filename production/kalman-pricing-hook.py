#!/usr/bin/env python3
"""Nostr-based Kalman pricing hook for routstrd.

Queries Nostr relays for kind-30315 events (tag d=kalman-pricing) from known
Kalman publisher npubs, picks the freshest event, and updates the zai-coding
upstream provider's fee + enabled flag in the routstrd database.

Runs every 2 minutes via cron.  Uses nak CLI for Nostr queries + stdlib for
everything else.

Fail-safe: if ALL events are stale (>5 min old) or no events at all,
DISABLES zai-coding in routstrd's DB — never sells z.ai at stale prices.
"""
import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone

# ── Configuration ────────────────────────────────────────────────────────────

# Known Kalman publisher npubs (add more machines here later)
KALMAN_PUBLISHER_NPUBS = [
    "npub1q2pk0674pg7yn5et8vhxxp3pe6s74grwpy30qj3wja7dysduqtms0ef294",  # T470 (primary)
    "npub1eguvjasf2zn7xnrvc6aenvjgcem6p2whezltux3t0gwlywexz4rsm7kk83",  # DQ05 (backup)
]

# Nostr relays to query (query all, pick freshest result)
NOSTR_RELAYS = [
    "wss://relay.primal.net",
    "wss://nos.lol",
    "wss://relay.damus.io",
]

# Stale threshold: events older than this are considered dead
STALE_THRESHOLD_SECONDS = 300  # 5 minutes

# Host-side path to the routstrd DB (Docker volume mount)
DB_PATH = "/var/lib/docker/volumes/routstr-public_routstr_public_data/_data/keys.db"
# Fallback: use docker exec if the host path isn't accessible
DB_DOCKER_CMD = ("docker", "exec", "routstr-public", "/.venv/bin/python3")
# The zai-coding provider slug in routstrd
ZAI_SLUG = "zai-coding"
# litellm's published per-token rate for GLM models ($/M tokens).
# This is the baseline that routstrd's litellm pricing uses.
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


def query_nostr_kalman_events() -> list[dict]:
    """Query Nostr relays for kind-30315 kalman-pricing events.
    
    Uses nak CLI: nak req -k 30315 -d kalman-pricing -a <npub> -l 1 <relay>
    Returns a list of parsed event dicts with 'created_at' and 'content' keys.
    """
    events = []
    nak_bin = None
    for candidate in ["/usr/local/bin/nak", "nak"]:
        try:
            r = subprocess.run(["which", candidate], capture_output=True, text=True, timeout=3)
            if r.returncode == 0:
                nak_bin = r.stdout.strip()
                break
        except Exception:
            pass
    
    if not nak_bin:
        log("nak CLI not found — cannot query Nostr relays")
        return events
    
    for npub in KALMAN_PUBLISHER_NPUBS:
        for relay in NOSTR_RELAYS:
            try:
                cmd = [
                    nak_bin, "req",
                    "-k", "30315",
                    "-d", "kalman-pricing",
                    "-a", npub,
                    "-l", "1",
                    relay,
                ]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                if result.returncode != 0:
                    continue
                
                # nak outputs JSON events (one per line), possibly with connection messages
                for line in result.stdout.strip().splitlines():
                    line = line.strip()
                    if not line or not line.startswith("{"):
                        continue
                    try:
                        evt = json.loads(line)
                        if evt.get("kind") == 30315:
                            events.append(evt)
                    except json.JSONDecodeError:
                        continue
            except subprocess.TimeoutExpired:
                continue
            except Exception as e:
                log(f"Error querying {relay} for {npub}: {e}")
                continue
    
    return events


def pick_freshest_event(events: list[dict]) -> dict | None:
    """Pick the freshest event by created_at timestamp."""
    if not events:
        return None
    # Deduplicate by event id
    seen = {}
    for evt in events:
        eid = evt.get("id", "")
        if eid and eid not in seen:
            seen[eid] = evt
    unique = list(seen.values())
    if not unique:
        return None
    # Sort by created_at descending
    unique.sort(key=lambda e: e.get("created_at", 0), reverse=True)
    return unique[0]


def update_provider_db(enabled: bool, fee: float) -> bool:
    """Update the zai-coding provider in the routstrd DB.
    Tries direct sqlite3 first, falls back to docker exec.
    Returns True on success, False on failure.
    """
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
        sql = (
            f"import sqlite3; db=sqlite3.connect('/app/data/keys.db'); "
            f"db.execute('UPDATE upstream_providers SET enabled={1 if enabled else 0}, "
            f"provider_fee={round(fee, 4)} WHERE slug=\\\"{ZAI_SLUG}\\\"'); "
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


def disable_provider_safe() -> bool:
    """Disable the zai-coding provider with retries and escalation.

    Tries update_provider_db(enabled=False) up to 3 times with 1-second sleeps.
    If all retries fail, escalates to stopping the routstr-public container
    entirely (docker stop) — better to have no node than one selling at stale
    prices.  Returns True if disabled successfully, False if all attempts fail.
    """
    for attempt in range(1, 4):
        if update_provider_db(enabled=False, fee=1.43):
            log(f"Disabled {ZAI_SLUG} via DB update (attempt {attempt}/3)")
            return True
        log(f"DB disable attempt {attempt}/3 failed")
        if attempt < 3:
            time.sleep(1)

    # ── Escalation: stop the container entirely ────────────────────────
    # DB updates failed 3× — try docker stop as last resort.  This is
    # drastic but better than a node selling z.ai at stale prices.
    log("CRITICAL: All DB disable attempts failed — escalating to docker stop routstr-public")
    try:
        result = subprocess.run(
            ["docker", "stop", "routstr-public"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            log("CRITICAL: Stopped routstr-public container as last-resort disable")
            return True
        else:
            log(f"CRITICAL: docker stop failed: {result.stderr.strip()}")
    except Exception as e:
        log(f"CRITICAL: docker stop exception: {e}")

    return False


def main() -> int:
    # 1. Query Nostr relays for kind-30315 kalman-pricing events
    events = query_nostr_kalman_events()

    if not events:
        # No events from any publisher — DISABLE zai-coding
        log("No Kalman pricing events found on any relay — disabling zai-coding")
        if not disable_provider_safe():
            log(f"CRITICAL: Failed to disable {ZAI_SLUG} — provider may still be serving stale pricing!")
            return 1
        return 0

    # 2. Pick the freshest event
    freshest = pick_freshest_event(events)
    if not freshest:
        log("Could not parse any valid events — disabling zai-coding")
        if not disable_provider_safe():
            log(f"CRITICAL: Failed to disable {ZAI_SLUG} — provider may still be serving stale pricing!")
            return 1
        return 0

    event_age = int(time.time()) - freshest.get("created_at", 0)
    log(f"Freshest event age: {event_age}s (threshold: {STALE_THRESHOLD_SECONDS}s)")

    # 3. Check staleness
    if event_age > STALE_THRESHOLD_SECONDS:
        log(f"No fresh Kalman pricing from any source — disabling zai-coding "
            f"(freshest event {event_age}s old)")
        if not disable_provider_safe():
            log(f"CRITICAL: Failed to disable {ZAI_SLUG} — provider may still be serving stale pricing!")
            return 1
        return 0

    # 4. Parse the event content
    try:
        data = json.loads(freshest["content"])
    except (json.JSONDecodeError, KeyError) as e:
        log(f"Failed to parse event content: {e} — disabling zai-coding")
        if not disable_provider_safe():
            log(f"CRITICAL: Failed to disable {ZAI_SLUG} — provider may still be serving stale pricing!")
            return 1
        return 0

    zai_available = data.get("zai_available", False)
    zai_price = data.get("zai_effective_price_usd_per_m")
    locked_reason = data.get("zai_locked_reason")
    source = data.get("source", "unknown")

    # 5. Update routstrd based on pricing data
    if not zai_available or zai_price is None or zai_price <= 0:
        # z.ai is unavailable or price is invalid — DISABLE the zai-coding upstream
        reason = "unavailable" if not zai_available else ("None" if zai_price is None else f"{zai_price}")
        log(f"z.ai {reason} (reason: {locked_reason}, source: {source}) — disabling {ZAI_SLUG}")
        if not disable_provider_safe():
            log(f"CRITICAL: Failed to disable {ZAI_SLUG} — provider may still be serving stale pricing!")
            return 1
        log(f"Disabled {ZAI_SLUG} in routstrd DB")
        return 0

    # Calculate the provider_fee multiplier
    # kalman_fee = zai_effective_price / litellm_base_rate
    kalman_fee = zai_price / LITELLM_ZAI_BASE_RATE
    kalman_fee = max(MIN_FEE, min(MAX_FEE, kalman_fee))

    log(
        f"z.ai available (source: {source}) — effective_price=${zai_price:.6f}/M, "
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
        log(f"Failed to update {ZAI_SLUG} (enable) — non-critical, will retry next cycle")

    return 0


if __name__ == "__main__":
    exit(main())
