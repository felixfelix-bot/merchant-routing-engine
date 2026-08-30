# ops/ — Operational tooling

Operational shell/Python tooling for the merchant-module production estate
(VPS2 23.182.128.51, its hermes tenants, and the routing-engine codebase).

This directory holds long-lived monitors and runbook-adjacent automation that
are committed to this repo so they survive re-provisioning and can be reviewed.

## Contents

| File | Purpose |
|------|---------|
| `uptime-monitor.sh` | Permanent per-endpoint production uptime monitor (RO-1). |
| `tests/test_uptime_monitor.sh` | TDD harness for the uptime monitor state machine. |

---

## uptime-monitor.sh

Permanent uptime monitor for the whole production estate. It generalizes the
2026-08-28 revival-only bootstrap watchdog (`vps51-watchdog.sh`) into a
full per-endpoint monitor that alerts only on *transitions*.

### What it probes (every 5 min via cron)

| Endpoint | Kind | Host:port / URL |
|----------|------|-----------------|
| `vps51-ssh` (box) | TCP | `23.182.128.51:22` |
| `vps219-ssh` | TCP | `23.182.128.219:22` |
| `routstr-info` | HTTPS | `https://routstr.orangesync.tech/v1/info` |
| `ai-web` | HTTPS | `https://ai.orangesync.tech` |
| `friends-web` | HTTPS | `https://friends.orangesync.tech` |
| `relay2-web` | HTTPS | `https://relay2.orangesync.tech` |
| `blossom2-web` | HTTPS | `https://blossom2.orangesync.tech` |

These are hardcoded until the box is back and the `/etc/caddy` route list can
be re-read to discover any additional production routes.

### Alerting semantics (output only on transitions)

The script writes alert text to stdout; the cron job (no_agent, `deliver=origin`)
forwards non-empty stdout to the Signal **merchant-module** group. It is silent
when nothing transitioned.

- **Outage**: ≥2 consecutive failures of an endpoint that was **previously up**
  → one alert. Never repeated while the endpoint stays down (the daily P&L cron
  already reports ongoing outages).
- **Recovery**: endpoint flips down→up → one alert.
- **Isolation**: an endpoint never observed up does **not** alert on failures —
  a box that boots dark is baseline + a revival watch, not an alarm.
- **No repeated alerts while stably down.**

### Kanban auto-unblock (preserved bootstrap behavior)

On box revival (≥2 consecutive up probes of the `box:true` endpoint), the script
auto-unblocks the staged recovery tasks via:

```bash
hermes kanban --board merchant-module unblock <task>
```

Staged tasks (preserved from `vps51-watchdog.sh`): `t_9c25b7d9` (RO-2), `t_5f69c815` (RO-4).

### State

One JSON file per tick, default
`~/.hermes/profiles/manager/cron/state/uptime-monitor.state`:

```json
{
  "endpoints": {
    "vps51-ssh": {
      "status": "down", "fails": 3, "ever_up": true,
      "was_down": true, "last_change": 1790000000, "display": "VPS2 23.182.128.51:22"
    }
  },
  "box_confirm": 0, "box_unblocked": false, "updated": 1790000000
}
```

### Cron swap

The rollout keeps the existing cron schedule (`*/5 * * * *`, no_agent,
deliver=origin, job id `6ab13cf8713a`). To switch from the revival-only
`vps51-watchdog.sh` to this monitor, point that job's `script` field at
`ops/uptime-monitor.sh` (or drop this script into the cron scripts dir). Do not
change the schedule. Coordinates with the manager profile are required before
flipping.

### Env overrides (testability)

| Env var | Default | Meaning |
|---------|---------|---------|
| `UPTIME_ENDPOINTS_JSON` | built-in list | JSON array of endpoint descriptors. |
| `UPTIME_STATE_FILE` | `.../uptime-monitor.state` | State file path. |
| `UPTIME_BOARD` | `merchant-module` | Kanban board for unblock. |
| `UPTIME_STAGED_TASKS` | `t_9c25b7d9 t_5f69c815` | Tasks to unblock on revival. |
| `UPTIME_BOX_ENDPOINT_ID` | `vps51-ssh` | Endpoint id that gates the unblock. |
| `UPTIME_PROBE` | empty | Fake probe `<id>` (exit 0 = up). Used by tests. |

### Tests

```bash
# full TDD suite against the new script (expect 15/15 passing)
bash ops/tests/test_uptime_monitor.sh

# RED demonstration: run the suite against the legacy revival-only watchdog
UPTIME_BIN=$HOME/.hermes/profiles/manager/scripts/vps51-watchdog.sh \
  bash ops/tests/test_uptime_monitor.sh
```

The harness asserts the state-machine transitions: init-up silent, fail-count
threshold, outage alert-once, flap suppression, revival alert, kanban unblock
on box revival, never-up isolation, and JSON state shape.
