# Nostr Kind-30315 Kalman Pricing Pipeline

> **Runbook & Architecture** — real-time z.ai pricing dissemination via Nostr
> to the routstrd relay node.  This is not a tutorial; it is a reference for
> operators and contributors.

---

## 1. Architecture Overview

The pipeline moves Kalman-computed z.ai pricing from the T470 (the machine with
live quota visibility) to the VPS2 (the routstrd relay host) without a direct
network tunnel.  Nostr replaceable events are the transport.  A fail-safe on the
consumer side disables the upstream provider if pricing data goes stale.

```
 ┌──────────────────────────────────────────────────────────────────────────┐
 │                         T470  (publisher host)                           │
 │                                                                          │
 │  zai_proxy.py  ──►  z.ai quota API (api.z.ai/api/monitor/usage/...)      │
 │       │                │                                                 │
 │       │          Kalman convergence, scarcity/peak multipliers           │
 │       │                │                                                 │
 │       │          _build_kalman_pricing_json()  ──►  JSON content         │
 │       │                                                     │            │
 │       └────────────►  nak event --kind 30315  ────────────┘            │
 │                          (signs + publishes every 30 s)                  │
 │                                                                          │
 │  NOSTR_SECRET_KEY env  ←  ~/.hermes/bot/kalman_npub.nsec                 │
 └──────────────────────────────┬───────────────────────────────────────────┘
                                │
                    Nostr relays (x3)
          ┌──────────────────────┼──────────────────────┐
          │                      │                      │
  wss://relay.damus.io   wss://nos.lol   wss://relay.primal.net
          │                      │                      │
          └──────────────────────┼──────────────────────┘
                                 │
 ┌───────────────────────────────┼──────────────────────────────────────────┐
 │                          VPS2  (consumer / relay host)                     │
 │                                  │                                         │
 │  cron: */2 * * * *  ──►  kalman-pricing-hook.py                          │
 │                                  │                                         │
 │      ┌───────────────────────────┘                                         │
 │      │                                                                   │
 │      ▼                                                                   │
 │  nak req -k 30315 -d kalman-pricing -a <npub> -l 1 <relay>  (per relay) │
 │      │                                                                   │
 │      ▼                                                                   │
 │  pick_freshest_event()  ──►  age check (< 300 s?)                       │
 │      │                                                                   │
 │      ▼                                                                   │
 │  UPDATE upstream_providers                                               │
 │    SET enabled=1, provider_fee=<fee multiplier>                          │
 │    WHERE slug='zai-coding'                                               │
 │      │                                                                   │
 │      │   (routstrd SQLite DB: keys.db)                                   │
 │      │            │                                                      │
 │      │            ▼                                                      │
 │      │     routstr-public Docker container                               │
 │      │     (serves Lightning-routed LLM calls at the fee)                │
 │      │                                                                   │
 │      ╞══ FAULT PATH ══════════════════════════════════════════════════╡  │
 │      ‖  stale / no events / price ≤ 0                                    ‖ │
 │      ‖  → disable zai-coding (3× retry)                                   ‖ │
 │      ‖  → escalate: docker stop routstr-public                            ‖ │
 │      ╞═══════════════════════════════════════════════════════════════╡  │
 │                                                                          │
 │  Log: /var/log/kalman-pricing-hook.log                                   │
 └──────────────────────────────────────────────────────────────────────────┘
```

### Key design decisions

| Decision | Rationale |
|---|---|
| Nostr replaceable event transport | No direct tunnel needed; any machine on any network can subscribe |
| kind 30315 (app-specific replaceable) | NIP-33 — relays store only the latest event per `d` tag |
| `d=kalman-pricing` d-tag | Deduplicates replaces across the network |
| `t=routstr` tag | Consumer filtering |
| 30 s publish / 2 min poll | Tight enough to disable within 5 min of T470 failure |
| 300 s stale threshold | 5 min — 10× publish interval, generous for relay propagation |
| Fee multiplier, not absolute price | routstrd multiplies by litellm base rate ($0.14/M); we emit ratio |

---

## 2. Publisher (T470)

### What it does

The publisher lives inside `zai_proxy.py` as a daemon thread
(`_nostr_publish_kalman`).  Every 30 seconds it:

1. Calls `_build_kalman_pricing_json()` — the same function the HTTP
   `/kalman-pricing` endpoint uses, so on-wire and Nostr data are identical.
2. Signs and publishes a kind-30315 replaceable Nostr event via the **nak CLI**.
3. Publishes to all three configured relays in a single `nak event` invocation.

The thread starts automatically when `zai_proxy.py` is launched (the
`__main__` block spawns it with `threading.Thread(..., daemon=True)`).

### Publish interval

```
_NOSTR_PUBLISH_INTERVAL = 30   # seconds
```

### Event format

| Field | Value |
|---|---|
| `kind` | `30315` (replaceable, NIP-33) |
| `d` tag | `kalman-pricing` |
| `t` tag | `routstr` |
| `created_at` | current unix epoch (nak sets automatically) |
| `pubkey` | `npub1q2pk0674pg7yn5et8vhxxp3pe6s74grwpy30qj3wja7dysduqtms0ef294` |
| `content` | JSON — see below |

### Content JSON structure

```json
{
  "timestamp": 1724428800,
  "source": "T470",
  "providers": {
    "zai_ours": {
      "base_rate_usd_per_m": 0.14,
      "effective_price_usd_per_m": 0.14,
      "peak_multiplier": 1.0,
      "scarcity_multiplier": 1.0,
      "health_multiplier": 1.0,
      "pace_multiplier": 1.0,
      "quota_used_pct": { "5-hour": 42, "weekly": 30, "monthly": 12 },
      "locked": false,
      "locked_window": null,
      "locked_threshold": null,
      "will_exhaust": false,
      "hours_until_exhaustion": null,
      "burn_rate_tph": 0.0,
      "available": true,
      "quota_data_unavailable": false
    },
    "zai_friend": { "..." : "same structure" },
    "ppq": {
      "base_rate_usd_per_m": 0.28,
      "effective_price_usd_per_m": 0.28,
      "available": true
    }
  },
  "zai_effective_price_usd_per_m": 0.14,
  "zai_available": true,
  "zai_locked_reason": null,
  "is_peak_hour": false
}
```

If multiple z.ai keys are available, the pipeline picks the **cheapest** and
publishes its effective price in `zai_effective_price_usd_per_m`.  When no key
is available, `zai_available` is `false`, the price is `null`, and
`zai_locked_reason` explains why.

### Source file

```
~/.hermes/bot/zai_proxy.py              (production source of truth)
~/merchant-routing-engine/production/zai_proxy.py   (mirror in repo)
```

Key functions:

| Function | Location | Purpose |
|---|---|---|
| `_build_kalman_pricing_json()` | ~line 5693 | Computes the pricing payload |
| `_load_nostr_sec()` | ~line 5681 | Reads hex sec from nsec file |
| `_nostr_publish_kalman()` | ~line 5806 | Background daemon thread |

### Environment variables

| Variable | Purpose | Required |
|---|---|---|
| `NOSTR_SECRET_KEY` | Hex-encoded secret key for signing (passed to nak via env, **not** CLI arg) | Yes (set by the thread itself) |

The key is loaded by `_load_nostr_sec()` from
`~/.hermes/bot/kalman_npub.nsec` (64-char hex, file mode 600) and injected into
the `nak event` subprocess as the `NOSTR_SECRET_KEY` environment variable.

---

## 3. VPS2 Hook

### What it does

The hook (`kalman-pricing-hook.py`) runs on the VPS2 / routstrd host and is
responsible for translating published Nostr pricing into a routstrd database
update.

Every invocation:

1. Queries all configured Nostr relays for the latest kind-30315 event from
   each known publisher npub.
2. Deduplicates by event id, sorts by `created_at`, and picks the freshest.
3. Checks staleness (must be younger than 300 s).
4. Parses the JSON content.
5. Computes a fee multiplier: `kalman_fee = zai_effective_price_usd_per_m
   / LITELLM_ZAI_BASE_RATE` (clamped to `[0.01, 10.0]`).
6. Updates the `upstream_providers` table in the routstrd SQLite DB:
   `SET enabled=1, provider_fee=<fee_multiplier> WHERE slug='zai-coding'`.
7. Verifies the update by reading the row back.

### Cron schedule

```
*/2 * * * *  python3  /opt/merchant-routing-engine/production/kalman-pricing-hook.py
```

The exact path depends on deployment; see [Deployment](#6-deployment).

### nak query

The hook shells out to `nak` for each publisher × relay combination:

```bash
nak req -k 30315 -d kalman-pricing -a <npub> -l 1 <relay_url>
```

- `-k 30315` — filter by event kind
- `-d kalman-pricing` — filter by the `d` tag
- `-a <npub>` — filter by author
- `-l 1` — limit to 1 event (the relay's latest replaceable event for this `d`)

### DB update

Primary path — direct SQLite3 access to the Docker volume:

```python
DB_PATH = "/var/lib/docker/volumes/routstr-public_routstr_public_data/_data/keys.db"
```

Fallback — if the host path doesn't exist, execute inside the container:

```python
DB_DOCKER_CMD = ("docker", "exec", "routstr-public", "/.venv/bin/python3")
```

SQL executed:

```sql
UPDATE upstream_providers
  SET enabled=?,
      provider_fee=?
  WHERE slug='zai-coding';
```

After updating, the hook reads back `SELECT slug, provider_fee, enabled FROM
upstream_providers WHERE slug='zai-coding'` to verify the write landed.

### Log output

```
LOG_FILE = "/var/log/kalman-pricing-hook.log"
```

Each run logs:
- Freshest event age and threshold
- Pricing summary (source, effective price, litellm base, fee)
- Disable/enable decisions with reasons
- Verification state

### Source file

```
~/merchant-routing-engine/production/kalman-pricing-hook.py
```

### Known publisher npubs

```python
KALMAN_PUBLISHER_NPUBS = [
    "npub1q2pk0674pg7yn5et8vhxxp3pe6s74grwpy30qj3wja7dysduqtms0ef294",  # T470
]
```

Additional machines can be added to this list; the hook queries all of them and
picks the single freshest event across all publishers and relays.

---

## 4. Fail-safe Logic

The pipeline's core safety invariant: **never sell z.ai at stale prices.**  If
any of the following conditions is true, the hook disables the `zai-coding`
upstream provider in the routstrd DB:

| Condition | Trigger |
|---|---|
| No events found on any relay | Hook couldn't reach relays, or publisher is dead |
| Freshest event older than 300 s | Publisher stopped publishing |
| `zai_available == false` | All z.ai keys unhealthy, locked, or exhausted |
| `zai_effective_price_usd_per_m` is `None` | Computed from `zai_available == false` |
| `zai_effective_price_usd_per_m <= 0` | Zero-price guard — price must be positive |

### Stale threshold

```python
STALE_THRESHOLD_SECONDS = 300   # 5 minutes (10× publish interval)
```

### Disable retry loop

When a failure condition is hit, `disable_provider_safe()` runs:

```python
for attempt in range(1, 4):          # 3 retries
    if update_provider_db(enabled=False, fee=1.43):
        log("Disabled zai-coding via DB update (attempt N/3)")
        return True
    if attempt < 3:
        time.sleep(1)                 # 1-second pause between retries
```

### Escalation: container stop

If all 3 DB-update attempts fail (e.g., DB is locked, Docker is unhealthy), the
hook escalates to stopping the entire routstrd container:

```python
subprocess.run(["docker", "stop", "routstr-public"], ...)
```

This is drastic — it takes the whole Lightning-routed relay offline — but it is
the last-resort guarantee that no stale-priced requests are served.

### Zero-price guard

Even when an event is fresh and parseable, `zai_effective_price_usd_per_m <= 0`
triggers the disable path.  This catches the edge case where the publisher sent
an event with available=false and price=null.

### Log escalation markers

```
[timestamp] Freshest event age: Ns (threshold: 300s)
[timestamp] z.ai unavailable (reason: ..., source: T470) — disabling zai-coding
[timestamp] Disabled zai-coding via DB update (attempt 1/3)
[timestamp] CRITICAL: All DB disable attempts failed — escalating to docker stop routstr-public
[timestamp] CRITICAL: Stopped routstr-public container as last-resort disable
```

All `CRITICAL` lines should trigger operator alerting.

---

## 5. Configuration

### Publisher (T470) — source: `zai_proxy.py`

| Setting | Value | Description |
|---|---|---|
| `_NOSTR_SEC_PATH` | `~/.hermes/bot/kalman_npub.nsec` | Hex-encoded 64-char secret (60-char hex is wrong; must be 64) |
| `_NOSTR_RELAYS` | see relay list below | Nostr relays to publish to |
| `_NOSTR_PUBLISH_INTERVAL` | `30` | Seconds between publishes |
| `_NOSTR_PUBLISHER_NPUB` | `npub1q2pk...0ef294` | Publisher's npub (read-only identity) |

### Hook (VPS2) — source: `kalman-pricing-hook.py`

| Setting | Value | Description |
|---|---|---|
| `KALMAN_PUBLISHER_NPUBS` | list of npubs | Known publishers to query |
| `NOSTR_RELAYS` | see relay list below | Relays to query (all queried, freshest wins) |
| `STALE_THRESHOLD_SECONDS` | `300` | Events older than this → stale |
| `DB_PATH` | `/var/lib/docker/volumes/routstr-public_routstr_public_data/_data/keys.db` | SQLite DB path (host volume mount) |
| `DB_DOCKER_CMD` | `("docker", "exec", "routstr-public", "/.venv/bin/python3")` | Fallback: execute Python inside container |
| `ZAI_SLUG` | `zai-coding` | routstrd upstream provider slug |
| `LITELLM_ZAI_BASE_RATE` | `0.14` | litellm published $/M tokens for GLM models |
| `MAX_FEE` | `10.0` | Upper clamp on fee multiplier |
| `MIN_FEE` | `0.01` | Lower clamp on fee multiplier |
| `LOG_FILE` | `/var/log/kalman-pricing-hook.log` | Hook log on VPS2 |

### Relay list

The hybrid transport uses three public Nostr relays plus an optional local
strfry relay:

| Relay | URL | Role |
|---|---|---|
| Primal | `wss://relay.primal.net` | Public — primary read/write |
| nos.lol | `wss://nos.lol` | Public — secondary read/write |
| Damus | `wss://relay.damus.io` | Public — secondary read/write |
| Strfry (local) | `ws://127.0.0.1:7777` | Self-hosted — local fastest-path (add to both publisher + hook relay lists when deployed) |

The local strfry relay on port 7777 is the zero-latency path for VPS2 consumers
when T470 and VPS2 can reach a shared strfry instance (e.g., on VPS2 itself).
If a strfry instance is running on VPS2, the publisher can connect to it
directly and the hook can query it locally, eliminating public-relay
propagation delay.

### Environment variables (not in code constants)

| Variable | Used by | Purpose |
|---|---|---|
| `NOSTR_SECRET_KEY` | nak CLI (set by publisher thread) | Signs events without leaking key to `ps aux` |
| `NOSTR_SECRET_KEY` | nak CLI (hook uses env-based query only — no signing) | Hook does not sign; only reads |

---

## 6. Deployment

### Prerequisites

**Both machines:**
- `nak` CLI installed and on PATH (publisher: `~/.local/bin/nak`; hook:
  `/usr/local/bin/nak` or `$PATH`)
- Nostr relay connectivity (outbound to the relay URLs)

**T470 (publisher):**
- `zai_proxy.py` running as the main API proxy (systemd service or Hermes
  supervisor)
- `~/.hermes/bot/kalman_npub.nsec` — 64-char hex secret key, mode 600
- `~/.local/bin/nak` — Go binary from [nak releases](https://github.com/fiatjaf/nak)
- Z.ai API keys loaded in `~/.hermes/profiles/manager/.env` (or sibling .env files)
- Python 3.10+ (runtime for zai_proxy.py)

**VPS2 (hook):**
- Python 3.10+ with stdlib only (no pip packages needed)
- Docker CLI accessible to the cron user (for fallback DB path)
- `routstr-public` container running with SQLite DB volume mounted
- `/var/log/` writable by the cron user (or change `LOG_FILE`)

### Deploy the publisher (T470)

1. Ensure `~/.hermes/bot/kalman_npub.nsec` exists and contains a 64-char hex
   private key:
   ```bash
   ls -la ~/.hermes/bot/kalman_npub.nsec    # verify mode 600
   ```
2. Install nak CLI:
   ```bash
   go install github.com/fiatjaf/nak@latest
   # or download prebuilt binary to ~/.local/bin/nak
   ```
3. Start zai_proxy.py (the publisher thread auto-starts at `__main__`):
   ```bash
   python3 ~/.hermes/bot/zai_proxy.py &
   ```
4. Verify the publisher is running:
   ```bash
   # Watch for the startup message
   # [nostr] Kalman publisher thread started — npub=npub1q2pk...
   # [nostr] Published kind-30315 — zai_available=True price=0.14 ts=...
   ```
5. Verify the event is on relays:
   ```bash
   nak req -k 30315 -d kalman-pricing -a npub1q2pk0674pg7yn5et8vhxxp3pe6s74grwpy30qj3wja7dysduqtms0ef294 -l 1 wss://relay.damus.io
   ```

### Deploy the hook (VPS2)

1. Copy the hook script to the server:
   ```bash
   scp ~/merchant-routing-engine/production/kalman-pricing-hook.py \
       vps2:/opt/kalman-pricing-hook.py
   chmod +x /opt/kalman-pricing-hook.py
   ```

2. Install nak CLI on VPS2:
   ```bash
   # Download nak binary
   curl -sL https://github.com/fiatjaf/nak/releases/latest/download/nak-linux-amd64 \
     -o /usr/local/bin/nak && chmod +x /usr/local/bin/nak
   ```

3. Add cron entry:
   ```bash
   crontab -e
   # Append:
   */2 * * * * python3 /opt/kalman-pricing-hook.py >> /var/log/kalman-pricing-hook.log 2>&1
   ```

4. Verify it runs:
   ```bash
   python3 /opt/kalman-pricing-hook.py
   tail -5 /var/log/kalman-pricing-hook.log
   ```

5. Verify the routstrd DB row:
   ```bash
   sqlite3 /var/lib/docker/volumes/routstr-public_routstr_public_data/_data/keys.db \
     "SELECT slug, provider_fee, enabled FROM upstream_providers WHERE slug='zai-coding';"
   ```

---

## 7. Troubleshooting

### Empty quota windows (blind-quota bug)

**Symptom:** z.ai API returns no parseable limits.  The publisher's
`_fetch_quota_windows()` creates a sentinel window:

```python
return [{"name": "unknown", "type": "TOKENS_LIMIT",
         "used_pct": 0, "resets_at": 0, "window_hours": 0}]
```

This looks like the key has 0% usage (healthy), but it actually means the quota
data is missing.  Without a guard, the key would be published as available at a
falsely low price.

**Mitigation (in code):** `_build_kalman_pricing_json()` detects the sentinel
and safely marks the key as unavailable:

```python
quota_data_unavailable = bool(wins and all(w.get("name") == "unknown" for w in wins))
if quota_data_unavailable:
    available = False
    locked = True
    locked_window = "unknown_quota_data"
```

**Impact on published events:** when this bug is active, `zai_available` will be
`false` and `zai_locked_reason` will include `ours:locked(unknown_quota_data)` (or
`friend`).  The VPS2 hook will disable zai-coding in the routstrd DB.

**Diagnosis:**
```bash
# Check what the publisher is publishing
curl http://127.0.0.1:9099/kalman-pricing | python3 -m json.tool

# Look for quota_data_unavailable: true in the provider entries
```

**Resolution:** The bug is in the z.ai monitoring API response format, not in our
code.  When z.ai API stops returning parseable limits, the pipeline correctly
fails safe (disables).  No action needed unless z.ai quota data is actually
visible in the z.ai dashboard but not in the API response — then the window
parser may need updating.

### Stale events / publisher stopped

**Symptom:** Hook log shows:
```
Freshest event age: 310s (threshold: 300s)
z.ai unavailable (reason: ..., source: T470) — disabling zai-coding
```

**Diagnosis:**
1. Check if the publisher thread is alive on T470:
   ```bash
   ps aux | grep zai_proxy
   # Look for [nostr] Published kind-30315 messages in the last 30s
   ```
2. Check if nak CLI is functioning:
   ```bash
   ~/.local/bin/nak event --help
   ```
3. Check if the nsec file exists:
   ```bash
   ls -la ~/.hermes/bot/kalman_npub.nsec
   ```
4. Check relay connectivity from T470:
   ```bash
   nak req -k 30315 -d kalman-pricing -a npub1q2pk... -l 1 wss://relay.damus.io
   ```

**Resolution:** Restart zai_proxy.py.  The publisher thread starts automatically.

### DB update failures

**Symptom:** Hook log shows:
```
DB disable attempt 2/3 failed
Direct DB update failed: ...
Docker exec failed: ...
```

**Diagnosis:**
1. Check if the volume mount path exists:
   ```bash
   ls -la /var/lib/docker/volumes/routstr-public_routstr_public_data/_data/keys.db
   ```
2. Check if the Docker container is running:
   ```bash
   docker ps | grep routstr-public
   ```
3. Check Docker exec permissions:
   ```bash
   docker exec routstr-public /.venv/bin/python3 -c "print('ok')"
   ```
4. Check sqlite3 Python module:
   ```bash
   python3 -c "import sqlite3; print(sqlite3.version)"
   ```

**Resolution:** Fix the volume mount or Docker permissions.  If the DB path is
correct and accessible, the most common issue is the Docker socket not being
available to the cron user — add the cron user to the `docker` group or use
`sudo docker` (adjust `DB_DOCKER_CMD` accordingly).

### Docker stop escalation

**Symptom:** Hook log shows:
```
CRITICAL: All DB disable attempts failed — escalating to docker stop routstr-public
CRITICAL: Stopped routstr-public container as last-resort disable
```

**Impact:** The entire routstrd relay is offline.  No Lightning-routed requests
can be served at all — this is worse than disabling one provider.

**Recovery:**
```bash
# 1. Determine why DB updates failed (see above)
# 2. Restart the container
docker start routstr-public
# 3. Verify the next cron run picks up fresh pricing
tail -f /var/log/kalman-pricing-hook.log
```

### nak CLI not found

**Symptom:** Hook log shows:
```
nak CLI not found — cannot query Nostr relays
```

**Resolution:** Install nak and ensure it's on PATH (`/usr/local/bin/nak` or
system-wide).

---

## 8. Security

### Nostr secret key handling

- The 64-char hex secret key is stored in `~/.hermes/bot/kalman_npub.nsec` with
  file mode `600` (owner read/write only).
- The key is **never** passed as a CLI argument (`--sec`).  Instead the publisher
  thread sets `NOSTR_SECRET_KEY` as an environment variable passed only to the
  `nak event` subprocess.  This prevents key leakage via `ps aux` or
  `/proc/<pid>/cmdline`.
- The nsec file is never committed to git.  It lives in `~/.hermes/bot/` which is
  outside the repository tree.
- The `.env` files (`~/.hermes/profiles/manager/.env`, `~/.hermes/.env`) are in
  `.gitignore` and never committed.

### No secrets in event content

The kind-30315 event `content` field contains only derived pricing data:
effective prices, multipliers, quota percentages, and availability flags.  No
API keys, no nsec, no internal hostnames are present.  The event is public-by
design (kind 30315 is a replaceable public event) and is visible to all Nostr
relay subscribers.

### routstrd DB access

- The hook accesses the routstrd SQLite DB via a host-side Docker volume mount
  or `docker exec`.  No DB credentials are stored — SQLite doesn't require
  authentication, and physical file access is controlled by the host's filesystem
  permissions (root or docker group).
- The DB path (`/var/lib/docker/volumes/...`) is only accessible from the VPS2
  host itself.

### Network exposure

- The publisher (`zai_proxy.py`) binds to `127.0.0.1:9099` — local only.
- The `nak event` command publishes to public Nostr relays over WSS.
- The hook runs cron-local on VPS2 — no inbound connections opened.
- A local strfry relay on `:7777` (if deployed) should be bound to a trusted
  interface only, not `0.0.0.0`.

### Key rotation

If the publishing npub needs to be rotated:

1. Generate a new key pair:
   ```bash
   nak key generate
   # Save the nsec output to ~/.hermes/bot/kalman_npub.nsec (mode 600)
   ```
2. Update `_NOSTR_PUBLISHER_NPUB` in `zai_proxy.py` with the new npub.
3. Add the old npub temporarily to `KALMAN_PUBLISHER_NPUBS` in the hook to ensure
   no coverage gap during transition.
4. Restart `zai_proxy.py`.
5. Remove the old npub from the hook list after confirming the new npub's events
   are being consumed.
