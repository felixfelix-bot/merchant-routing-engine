# IMPL-SPEC: Kalman-Gated Kanban Dispatch

**Status:** APPROVED by operator (2026-07-29)
**Branch:** converged-rate-replay
**Complexity:** ~40 lines proxy endpoint + ~30 lines daemon update

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

## QUESTIONS FOR MERCHANT MODULE CONTEXT

1. Does the proxy HTTP handler have a clean dispatch table for GET routes, or does it use if/elif chains? (affects where to add the endpoint)
2. Is `_read_zai_state()` the right function to get current quota percentages, or should we read from the in-memory dict directly?
3. Should the gate endpoint use ConsumptionKalman.will_exhaust() directly (better prediction but needs the Kalman instances exposed), or is the simple `remaining > 2x budget` check sufficient for v1?
4. Any concerns about thread safety? The endpoint is read-only but reads shared Kalman state.
