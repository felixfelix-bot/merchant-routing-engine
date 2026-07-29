# IMPL-SPEC: Kalman-Gated Kanban Dispatch

**Status:** APPROVED by operator (2026-07-29) — REVISED with hardware gate (v2)
**Branch:** converged-rate-replay
**Complexity:** ~60 lines proxy endpoint + ~50 lines daemon update

---

## GOAL

Add a `/v1/dispatch_gate` endpoint to `zai_proxy.py` that the kanban dispatch daemon calls before spawning workers. Uses LIVE Kalman filter state already in memory. Returns whether a task can dispatch, what model to use, and at what cost.

**Why:** Workers dispatched during quota exhaustion timeout at 300s with 3-4 API calls. 4 PCB tasks wasted 20 min on failures a predictive gate prevents. Current gate is binary/reactive; Kalman filters give predictive + price-aware dispatch.

---

## WHAT TO BUILD

### 1. Endpoint: `GET /v1/dispatch_gate`

Add to `zai_proxy.py` HTTP handler. ~40 lines.

**Request:**
```
GET /v1/dispatch_gate?estimated_tokens=200000&task_type=coding
```

**Response (200 OK):**
```json
{
  "can_dispatch": true,
  "reason": "sufficient headroom on primary key",
  "recommended_model": "glm-5.2",
  "effective_price_per_m": 0.003,
  "predicted_cost": 0.0006,
  "hours_until_exhaustion": {
    "ours": 4.5,
    "friend": 8.2
  },
  "quota_used_pct": {
    "ours": 45.0,
    "friend": 30.0
  },
  "burn_rate_pct_per_hour": {
    "ours": 9.2,
    "friend": 6.1
  },
  "is_peak_hour": false,
  "peak_multiplier": 1.0,
  "scarcity_factor": 1.0,
  "downgraded": false
}
```

**Response when holding (200 OK, not error):**
```json
{
  "can_dispatch": false,
  "reason": "both keys will exhaust within task budget even with flash model",
  "recommended_model": null,
  "hours_until_exhaustion": {"ours": 0.3, "friend": 0.5}
}
```

### 2. Decision Logic (inside endpoint)

```python
def handle_dispatch_gate(self, query_params):
    estimated_tokens = int(query_params.get('estimated_tokens', [200000])[0])
    task_type = query_params.get('task_type', ['coding'])[0]
    
    # Get live state
    router = LiveRouter.get_instance()
    state = _read_zai_state()  # already called every request cycle
    
    ours_pct = state.get('token_pct', 0)
    friend_pct = state.get('friend_token_pct', 0)
    ours_remaining = _QUOTA_TOTALS['ours'] * (1 - ours_pct / 100)
    friend_remaining = _QUOTA_TOTALS['friend'] * (1 - friend_pct / 100)
    
    # Task type → model + token budget adjustment
    TASK_PROFILES = {
        'mechanical':  {'model': 'glm-4.5-flash',  'budget_mult': 0.25},
        'coding':      {'model': 'glm-5.2',         'budget_mult': 1.0},
        'research':    {'model': 'glm-5.2',         'budget_mult': 2.5},
        'review':      {'model': 'glm-5.2',         'budget_mult': 0.5},
        'docs':        {'model': 'glm-4.5-flash',   'budget_mult': 0.5},
    }
    profile = TASK_PROFILES.get(task_type, TASK_PROFILES['coding'])
    model = profile['model']
    task_budget = estimated_tokens * profile['budget_mult']
    
    # Check if either key has headroom for this task
    # Use ConsumptionKalman.will_exhaust if initialized, else simple pct check
    def key_has_headroom(remaining, pct):
        if pct >= 95:
            return False
        # Simple check: remaining > 2x task budget (safety margin)
        return remaining > task_budget * 2
    
    ours_ok = key_has_headroom(ours_remaining, ours_pct)
    friend_ok = key_has_headroom(friend_remaining, friend_pct)
    
    downgraded = False
    
    if ours_ok or friend_ok:
        # Dispatch with preferred model
        can_dispatch = True
        reason = f"sufficient headroom ({'ours' if ours_ok else 'friend'} key)"
    else:
        # Try downgrade to flash (uses ~30% tokens)
        flash_budget = task_budget * 0.3
        ours_flash_ok = ours_remaining > flash_budget * 2 and ours_pct < 95
        friend_flash_ok = friend_remaining > flash_budget * 2 and friend_pct < 95
        
        if ours_flash_ok or friend_flash_ok:
            model = 'glm-4.5-flash'
            downgraded = True
            can_dispatch = True
            reason = "downgraded to flash due to quota pressure"
            task_budget = flash_budget
        else:
            can_dispatch = False
            model = None
            reason = "both keys will exhaust within task budget"
    
    # Price calculation (even when holding, for monitoring)
    base_price = _DEFAULT_CONVERGED_RATES.get('ours', 0.001)
    peak_mult = peak_multiplier()
    scarcity = scarcity_factor(max(ours_pct, friend_pct))
    effective_price = max(base_price * peak_mult * scarcity, MIN_EFFECTIVE_PRICE)
    
    # Hours until exhaustion estimate
    ours_burn = state.get('predictions', {}).get('ours', {}).get('burn_rate_pct_per_hour', 0)
    friend_burn = state.get('predictions', {}).get('friend', {}).get('burn_rate_pct_per_hour', 0)
    ours_hours = (100 - ours_pct) / ours_burn if ours_burn > 0 else float('inf')
    friend_hours = (100 - friend_pct) / friend_burn if friend_burn > 0 else float('inf')
    
    return json.dumps({
        'can_dispatch': can_dispatch,
        'reason': reason,
        'recommended_model': model,
        'effective_price_per_m': round(effective_price, 6),
        'predicted_cost': round(effective_price * task_budget / 1_000_000, 6),
        'hours_until_exhaustion': {'ours': round(ours_hours, 1), 'friend': round(friend_hours, 1)},
        'quota_used_pct': {'ours': round(ours_pct, 1), 'friend': round(friend_pct, 1)},
        'burn_rate_pct_per_hour': {'ours': round(ours_burn, 1), 'friend': round(friend_burn, 1)},
        'is_peak_hour': peak_mult > 1.0,
        'peak_multiplier': peak_mult,
        'scarcity_factor': round(scarcity, 2),
        'downgraded': downgraded,
    })
```

### 3. Task Type → Token Budget Table

Hardcode in the endpoint (can move to config later):

| task_type | Model | Budget Multiplier | Typical Tokens | Max Cost |
|-----------|-------|-------------------|----------------|----------|
| mechanical | glm-4.5-flash | 0.25x | ~50K | $0.001 |
| coding | glm-5.2 | 1.0x | ~200K | $0.01 |
| research | glm-5.2 | 2.5x | ~500K | $0.02 |
| review | glm-5.2 | 0.5x | ~100K | $0.005 |
| docs | glm-4.5-flash | 0.5x | ~100K | $0.002 |

Default (unknown type): coding profile.

### 4. Daemon Update: `adaptive-dispatch-daemon.sh`

Replace `api_quota_ok()` function (lines 54-95) with:

```bash
kalman_dispatch_gate() {
    local task_type="${1:-coding}"
    local estimated_tokens="${2:-200000}"
    
    local result
    result=$(curl -s "http://localhost:9099/v1/dispatch_gate?estimated_tokens=${estimated_tokens}&task_type=${task_type}" 2>/dev/null)
    
    if [ $? -ne 0 ] || [ -z "$result" ]; then
        # FALLBACK: use old binary gate if endpoint unreachable
        api_quota_ok_legacy
        return $?
    fi
    
    local can_dispatch model
    can_dispatch=$(echo "$result" | python3 -c "import sys,json; d=json.load(sys.stdin); print('1' if d.get('can_dispatch') else '0')" 2>/dev/null)
    model=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('recommended_model','unknown'))" 2>/dev/null)
    
    if [ "$can_dispatch" = "1" ]; then
        echo "YES:${model}"
        return 0
    else
        local reason
        reason=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('reason','unknown'))" 2>/dev/null)
        echo "HOLD:${reason}"
        return 1
    fi
}

# Keep old function as fallback
api_quota_ok_legacy() {
    # ... existing api_quota_ok body unchanged ...
}
```

### 5. Task Type Inference (daemon side)

The daemon infers task_type from the task title before calling the gate:

```bash
infer_task_type() {
    local title="$1"
    title_lower=$(echo "$title" | tr '[:upper:]' '[:lower:]')
    
    if echo "$title_lower" | grep -qE 'drc|gerber|pcb|config|wire.*up|yaml'; then
        echo "mechanical"
    elif echo "$title_lower" | grep -qE 'review|cold.review|audit'; then
        echo "review"
    elif echo "$title_lower" | grep -qE 'doc|readme|handover|plan'; then
        echo "docs"
    elif echo "$title_lower" | grep -qE 'research|investigate|analyze|analysis'; then
        echo "research"
    else
        echo "coding"
    fi
}
```

---

## ROUTING IN zai_proxy.py

The endpoint needs to be added to the HTTP request handler. Find the existing GET handler section and add:

```python
# In do_GET or handler dispatch:
if path == '/v1/dispatch_gate':
    return self.handle_dispatch_gate(query_params)
```

The handler uses:
- `_read_zai_state()` or the in-memory state dict (already updated per-request)
- `_DEFAULT_CONVERGED_RATES` (already defined in live_router.py, import or duplicate)
- `_QUOTA_TOTALS` (already defined in live_router.py)
- `peak_multiplier()` and `scarcity_factor()` from `src/price_kalman.py`
- `MIN_EFFECTIVE_PRICE` from `src/price_kalman.py`

All of these are already imported or available in the proxy process.

---

## SAFETY PROPERTIES

1. **Never breaks the proxy** — endpoint is read-only, no state mutation
2. **Fails open** — if endpoint is unreachable, daemon falls back to old binary gate
3. **Fails open** — if Kalman state is cold/uninitialized, uses simple pct check
4. **Conservative default** — unknown task_type defaults to coding (200K budget)
5. **Safety margin** — requires 2x task budget in headroom before dispatching
6. **Downgrade before hold** — tries flash model before refusing to dispatch

---

## TESTING

```bash
# Mechanical task, plenty of quota
curl "http://localhost:9099/v1/dispatch_gate?estimated_tokens=50000&task_type=mechanical"

# Research task (500K tokens)
curl "http://localhost:9099/v1/dispatch_gate?estimated_tokens=200000&task_type=research"

# Unknown task type (defaults to coding)
curl "http://localhost:9099/v1/dispatch_gate?estimated_tokens=200000"
```

Expected behavior:
- When quota < 50%: `can_dispatch: true` with full model
- When quota 50-80%: `can_dispatch: true`, possibly downgraded to flash
- When quota > 80% both keys: `can_dispatch: false` with reason
- During peak hours (6-10 UTC): `peak_multiplier: 3.0` in response (informational, doesn't block)

---

## WHAT THIS UNBLOCKS

| Board | Tasks Waiting | Current Blocker |
|-------|--------------|-----------------|
| balloon (PCB DRC) | 4 tasks | Quota exhaustion timeouts |
| merchant-routing | 4 tasks (Phase 4.5) | P3.4 soak test running |
| nostr-infra | 12 tasks | Various dependencies |
| embeddings | 0 (complete) | — |

Once the Kalman gate is live, the daemon auto-dispatches when quota windows open — no manual intervention needed.

---

## IMPLEMENTATION NOTES FOR THE MERCHANT MODULE CONTEXT

1. **LiveRouter.get_instance()** is already instantiated in the proxy (PID 439891). It holds warm PriceKalman + ConsumptionKalman instances. The endpoint should query these directly rather than re-importing.

2. **_QUOTA_TOTALS** is defined in `live_router.py`. Can be imported or the proxy may already have it in scope.

3. **The `zai_state.json` predictions field** is written by the proxy's quota tracking. It has `burn_rate_pct_per_hour` and `projected_pct` per key. The endpoint can read this OR query the Kalman filters directly for better precision.

4. **model_mapping.py** (P4.5a, not yet built) — the endpoint hardcodes the task_type → model mapping for now. Once P4.5a lands, it can use the dynamic mapping.

5. **Peak hours are 6-10 UTC** — the endpoint reports this but does NOT block on it. The daemon can optionally hold non-urgent tasks during peak if desired (future enhancement).

6. **The daemon currently round-robins all boards**. The gate is called per-spawn, not per-board. Each task gets individually gated.

---

## HARDWARE GATE (v2 addition)

### Problem: Compounding Failure Cost

Software task fails on quota: wastes ~50K tokens (~$0.001). No side effects.

Hardware task fails on quota: wastes ~50K tokens + holds board lock for 300s timeout + blocks all other hardware tasks from using that board + may leave board half-flashed. Cost: ~$0.001 + 300s hardware downtime + recovery work.

**Hardware tasks need LARGER Kalman safety margins because the blast radius is wider.**

### Three-Dimensional Gate

The dispatch gate has three dimensions. They are NOT equal — there's a priority cascade:

```
DIMENSION 1: Hardware availability (binary, instantaneous)
    Board present? DQ05 reachable? Lock free?
    → If NO: HOLD. Re-check next tick.
    → If YES: escalate priority, relax price gate.

DIMENSION 2: Quota sufficiency (Kalman, predictive)
    Will both keys exhaust within task budget?
    Safety margin: 4x for hardware tasks, 2x for software tasks
    → If YES (enough headroom): proceed to price check
    → If NO: try model downgrade → hold if still insufficient

DIMENSION 3: Price optimization (PriceKalman, trend-aware)
    Is it peak hour (3x multiplier)?
    → If hardware present: DISPATCH ANYWAY (scarcity override)
    → If no hardware needed: hold non-urgent tasks until off-peak
```

**Key insight:** When dimensions 1 and 3 conflict (board is here but it's peak hours), hardware WINS. A board physically present is scarcer than quota — quota resets every 5h, the board might be unplugged in 30 min.

### Extended Endpoint

```
GET /v1/dispatch_gate?estimated_tokens=200000&task_type=coding&hardware_req=board
```

New `hardware_req` parameter (default: `none`):
- `none` — software only (compile, edit, docs, review)
- `board` — needs one physical board connected (flash, capture, serial)
- `dual_board` — needs two boards (handshake, phy exchange)
- `dq05` — needs DQ05 reachable via SSH/Netbird (remote builds)

### Extended Response

```json
{
  "can_dispatch": true,
  "reason": "board present, quota sufficient with 4x margin",
  "recommended_model": "glm-5.2",
  "effective_price_per_m": 0.009,
  "predicted_cost": 0.0018,
  "hours_until_exhaustion": {"ours": 4.5, "friend": 8.2},
  "quota_used_pct": {"ours": 45.0, "friend": 30.0},
  "is_peak_hour": true,
  "peak_multiplier": 3.0,
  "scarcity_override": true,
  "hardware": {
    "required": "board",
    "board_present": true,
    "board_id": "F242D",
    "lock_status": "free",
    "queue_depth": 0,
    "estimated_wait_minutes": 0
  }
}
```

### Hardware State Sources

The endpoint reads hardware state from existing infrastructure:

| Signal | Source | Already exists? |
|--------|--------|----------------|
| Board presence | `ls /dev/ttyACM*` + udevadm | Yes — board_watcher.sh |
| Board identity | udevadm serial number (F242D) | Yes |
| Lock status | `~/.hermes/peripheral_locks/board-lock-monitor.json` | Yes (needs refresh cron) |
| DQ05 reachable | `dq05_detect` MCP call or health endpoint | Yes — dq05_monitor MCP |
| Queue depth | Count running/ready kanban tasks with same hardware_req | New (daemon-side) |

### Hardware Kalman Margin Logic

```python
# In the gate decision logic:
SAFETY_MARGIN = {
    'none': 2.0,       # software: 2x budget headroom
    'board': 4.0,      # single board: 4x (flash + test takes time)
    'dual_board': 6.0, # two boards: 6x (harder to coordinate)
    'dq05': 3.0,       # remote: 3x (network adds variance)
}

margin = SAFETY_MARGIN.get(hardware_req, 2.0)
required_headroom = task_budget * margin

if ours_remaining < required_headroom and friend_remaining < required_headroom:
    # Not enough for this task type with hardware margin
    # Try downgrade to flash (0.3x token usage, same margin)
    flash_required = task_budget * 0.3 * margin
    if ours_remaining < flash_required and friend_remaining < flash_required:
        can_dispatch = False
        reason = f"insufficient quota for {hardware_req} task ({margin}x margin)"
    else:
        model = 'glm-4.5-flash'
        downgraded = True
```

### Hardware Task Duration Impact

Hardware tasks consume quota DURING execution (flash + test + capture = more wall-clock). The ConsumptionKalman burn_rate already accounts for aggregate burn, but for a hardware task specifically:

```python
# Estimate wall-clock duration from task type + hardware
DURATION_MINUTES = {
    'flash': 10,      # pio upload + verify
    'capture': 20,    # serial capture window
    'throughput': 15, # tx/rx test cycle
    'handshake': 60,  # two-node integration test
}

# Quota consumed during task = burn_rate * (duration / 60)
task_duration_hours = DURATION_MINUTES.get(task_subtype, 15) / 60
quota_during_task = ours_burn_rate * task_duration_hours
effective_budget = task_budget + quota_during_task  # tokens consumed by task + tokens burned while waiting
```

### Daemon Task Type → Hardware Inference

```bash
infer_hardware_req() {
    local title="$1"
    title_lower=$(echo "$title" | tr '[:upper:]' '[:lower:]')
    
    # Two-board tasks
    if echo "$title_lower" | grep -qE 'handshake|two.node|2.node|phy.exchange|dual'; then
        echo "dual_board"
    # Single-board tasks
    elif echo "$title_lower" | grep -qE 'flash|pio.upload|serial|capture|throughput|f242d|bootsel'; then
        echo "board"
    # DQ05-dependent tasks
    elif echo "$title_lower" | grep -qE 'dq05|remote.build|ssh.*compile'; then
        echo "dq05"
    else
        echo "none"
    fi
}
```

### Hardware Queue Prediction (simple model, not Kalman)

Board is a single-server queue. Predict wait time for new tasks:

```python
def hardware_queue_wait(hardware_req, running_tasks, ready_tasks):
    """Estimate minutes until a board slot opens."""
    if hardware_req == 'none':
        return 0  # no hardware needed
    
    # Count tasks ahead in queue with same hardware_req
    ahead = [t for t in running_tasks if t.hardware_req == hardware_req]
    ready_ahead = [t for t in ready_tasks if t.hardware_req == hardware_req]
    
    if not ahead:
        return 0  # board is free now
    
    # Estimate completion time of running hardware tasks
    wait = sum(t.estimated_remaining_minutes for t in ahead)
    
    return wait
```

This doesn't need Kalman because hardware tasks have known durations (flash=10min, capture=20min) — no noisy measurements to smooth. Simple sum of estimated remaining times.

### What Needs Fixing Before Hardware Gate Works

1. **board-lock-monitor.json is STALE** — last updated 2026-07-24. The board-access-monitor.sh cron needs to run more frequently (every 5 min, not whenever someone remembers).

2. **No hardware_req field on kanban tasks** — daemon infers from title keywords (above). Later: add explicit field to `hermes kanban create`.

3. **DQ05 reachability check** — currently only via MCP tool. Daemon needs a lightweight `curl` or `ssh -o ConnectTimeout=3` check. Don't spawn a full MCP call per dispatch tick.

### Existing Hardware Gate Infrastructure

| Component | Path | Status |
|-----------|------|--------|
| Board lock monitor | `~/.hermes/peripheral_locks/board-lock-monitor.json` | Stale, needs cron refresh |
| Board watcher | `~/.hermes/profiles/manager/scripts/board_watcher.sh` | Works, detects F242D |
| Board access monitor | `~/.hermes/profiles/manager/scripts/board-access-monitor.sh` | Needs frequency increase |
| DQ05 monitor MCP | `/home/c03rad0r/scripts/dq05_monitor_mcp.py` | Running (PID 8657) |
| flock mutex system | Board workspaces + lock files | Works, last-line defense |
| Balloon board-access skill | `skills/balloon-board-access/` | Has lock protocol docs |

### Mutex vs Gate — Complementary, Not Competing

| Layer | When | What | Prevents |
|-------|------|------|----------|
| **Hardware gate** | PRE-dispatch | "Is board present + free?" | Spawning worker that can't succeed |
| **Quota gate** (Kalman) | PRE-dispatch | "Will quota last with Nx margin?" | Spawning during exhaustion |
| **Price gate** (PriceKalman) | PRE-dispatch | "Is now cost-effective?" (overridden by hardware presence) | Wasting money at peak rates |
| **flock mutex** | RUNTIME | "Only one process flashes this board" | Concurrent hardware collision |

Gate is first line of defense (don't spawn). Mutex is last line (if two somehow spawn, only one gets hardware). Both needed.

---

## QUESTIONS FOR MERCHANT MODULE CONTEXT

1. Does the proxy HTTP handler have a clean dispatch table for GET routes, or does it use if/elif chains? (affects where to add the endpoint)
2. Is `_read_zai_state()` the right function to get current quota percentages, or should we read from the in-memory dict directly?
3. Should the gate endpoint use ConsumptionKalman.will_exhaust() directly (better prediction but needs the Kalman instances exposed), or is the simple `remaining > Nx budget` check sufficient for v1?
4. Any concerns about thread safety? The endpoint is read-only but reads shared Kalman state.
5. **NEW:** The hardware gate needs to read board-lock-monitor.json + check /dev/ttyACM*. Should this happen inside the proxy endpoint (proxy reads files directly) or in the daemon (daemon curls a separate hardware health endpoint)?
6. **NEW:** For DQ05 reachability, should the gate use `ssh -o ConnectTimeout=3` (fast, direct) or the existing MCP `dq05_detect` (richer but heavier)?
