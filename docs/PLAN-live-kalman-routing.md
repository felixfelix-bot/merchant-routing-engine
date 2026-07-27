# PLAN: Live Kalman Price-Driven Routing (v2.0)

**Author:** Manager (Hermes)
**Date:** 2026-07-28
**Status:** PROPOSED — awaiting Felix approval

## Executive Summary

Replace the binary `best_key()` quota-threshold router with the price-driven
`RoutingOptimizer` from merchant-routing-engine. The optimizer uses converged
Kalman base rates × peak × scarcity × health × pace multipliers to pick the
cheapest viable provider for every request.

The replay proved: at converged rates, ours is 30x cheaper than friend.
When ours exhausts, optimizer picks ollama_cloud ($0.024/M) over friend
($0.029/M) — 17% savings on 77% of traffic. Current production burns money
on the wrong fallback.

---

## Current Architecture (what we have)

```
zai_proxy.py (port 9099)
  ├── best_key()                    ← CURRENT ROUTER (binary quota thresholds)
  │   ├── Phase 1: Kalman burn-rate prediction (proactive)
  │   ├── Phase 2: _best_unlocked() (reactive: lock thresholds per window)
  │   ├── Phase 3: Recovery re-evaluation
  │   └── Phase 4: Health check (skip exhausted keys)
  ├── ShadowHook (READ-ONLY)        ← logs optimizer decisions, never affects routing
  ├── External failover (ollama_cloud, ppq, openrouter, deepinfra)
  └── _refresh_loop()               ← polls z.ai quota API every 5 min
```

**Problem:** `best_key()` picks between ours/friend only. External providers
(ollama_cloud etc.) are only reached via separate failover paths after a
request fails. The optimizer considers ALL providers simultaneously.

---

## Target Architecture

```
zai_proxy.py (port 9099)
  ├── best_key()                    ← WRAPPER (backward compat, logging)
  │   └── calls RoutingOptimizer.route() when ENABLED
  ├── RoutingOptimizer (LIVE)       ← NEW HOT PATH
  │   ├── PriceKalman (converged from historical data)
  │   ├── ConsumptionKalman (live burn tracking)
  │   ├── scarcity_factor (quota % ramp)
  │   ├── peak_multiplier (3x during 6-10 UTC)
  │   ├── health_pricing_factor (graduated failure penalty)
  │   └── pace_factor (predictive burn-rate adjustment)
  ├── External failover (unchanged) ← emergency fallback only
  └── _refresh_loop()               ← feeds quota state to optimizer
```

---

## Phase 0: Data Infrastructure (foundation)

### 0.1 — Converged rates at startup
**DONE:** `scripts/feed_historical_costs.py` reads daily_spend, feeds to
PriceKalman, returns converged rates. Already importable.

**TODO:** Wire `load_historical_rates()` into zai_proxy.py startup so
converged rates are used on every cold start, not just seed costs.

### 0.2 — Live price observations
**Status:** PriceKalman currently receives NO live updates (shadow_hook
line 195-197: "skip for now — seeds are fine").

**TODO:** Feed daily effective rates to PriceKalman. Source: every 24h,
compute spend_usd / tokens for each provider from api_calls table, call
`pk.update(effective_rate)`. This makes the Kalman self-correcting.

### 0.3 — Live consumption tracking
**Status:** ConsumptionKalman exists but is stubbed in replay.

**TODO:** Wire `consumption_kalman.update(tokens)` on every completed
request (after response tokens are known). This feeds the exhaustion
predictions that gate the optimizer.

### 0.4 — Pace window computation
**Status:** `pace_factor_multi()` exists but needs live window data.

**TODO:** In `_refresh_loop()`, compute pace windows from quota_cache:
- 5h window: (used_pct, total, time_elapsed_pct, burn_rate, 5)
- weekly window: (used_pct, total, time_elapsed_pct, burn_rate, 168)
Pass as `pace_mults` to optimizer.

**Deliverable:** Data pipeline that keeps all 5 Kalman inputs fresh.

---

## Phase 1: Optimizer Integration (the hot path)

### 1.1 — Create `LiveRouter` class (new module)
**File:** `merchant-routing-engine/src/live_router.py`

```python
class LiveRouter:
    """Bridges zai_proxy.py to RoutingOptimizer for live routing.
    
    Maintains persistent Kalman state across requests.
    Thread-safe (called from ThreadingHTTPServer handler threads).
    """
    def __init__(self, db_path, converged_rates):
        self._price_kalmans = {name: PriceKalman(rate) for ...}
        self._consumption_kalmans = {name: ConsumptionKalman() for ...}
        self._lock = threading.Lock()
    
    def select_key(self, model, tokens, quota_state, health_state, peak, 
                   failure_counts, pace_windows) -> str:
        """Returns provider name ('ours', 'friend', 'ollama_cloud', ...)."""
        # Build optimizer with current state
        # Call route()
        # Return chosen_provider
    
    def record_request(self, provider, tokens, cost_estimate):
        """Update Kalman filters after request completes."""
```

### 1.2 — Modify `best_key()` to call LiveRouter
**File:** `~/.hermes/bot/zai_proxy.py`

```python
def best_key() -> str:
    # --- NEW: price-driven routing (when enabled) ---
    if _LIVE_ROUTER and os.path.exists(ENABLE_FLAG):
        try:
            choice = _LIVE_ROUTER.select_key(
                model=..., tokens=..., quota_state=..., ...
            )
            # Still log to key_decisions for audit trail
            _log_key_decision(...)
            return choice
        except Exception:
            pass  # fall through to existing logic
    
    # --- EXISTING: binary quota routing (fallback) ---
    ... (unchanged) ...
```

**Key design decisions:**
- **Kill switch:** Touch file `~/.hermes/bot/.disable_live_routing` → instant revert
- **Model detection:** Map incoming model name to difficulty tier (already in shadow_hook)
- **Thread safety:** LiveRouter uses internal lock, same pattern as quota_cache
- **Logging:** key_decisions table gets `routing_engine='live_kalman'` tag

### 1.3 — Map optimizer output to zai_proxy key names
The optimizer returns provider names like "ours", "friend", "ollama_cloud".
zai_proxy needs to map these to actual API keys:

```python
PROVIDER_TO_KEY = {
    "ours": KEYS["ours"],
    "friend": KEYS["friend"], 
    "ollama_cloud": OLLAMA_CLOUD_KEY,
    "ppq": _EXTERNAL_KEYS.get("ppq", ""),
    "openrouter": _EXTERNAL_KEYS.get("openrouter", ""),
}
```

And handle the response routing accordingly (zai upstream vs ollama endpoint).

**Deliverable:** LiveRouter that can actually route requests to any provider.

---

## Phase 2: Provider Endpoint Routing (multi-provider)

### 2.1 — z.ai providers (ours, friend)
No change — same upstream URL, different API key header.

### 2.2 — ollama_cloud
Currently handled in separate failover code. Need to route to it as a
PRIMARY choice (not just on failure).

**TODO:** Extract ollama_cloud request handler into a callable that
LiveRouter can invoke when optimizer picks it. This means:
- Building the request with ollama's API format
- Handling ollama's response format
- Logging tokens/cost

### 2.3 — ppq, openrouter (paid providers)
Keep as last-resort failover. Optimizer picks them only when all
free/flat providers are exhausted. Low priority for Phase 2.

**Deliverable:** Any provider the optimizer picks gets correctly routed.

---

## Phase 3: Continuous Kalman Calibration

### 3.1 — Daily rate reconciliation
Every 24h (in a background thread or cron):
1. Query `daily_spend` for yesterday's effective rates
2. Feed to PriceKalman via `.update()`
3. Log convergence state to `kalman_samples` table

### 3.2 — Convergence monitoring
Reuse existing `kalman-convergence-check` skill. Alert if:
- Uncertainty > 30% (Kalman hasn't converged)
- Rate drift > 50% in 7 days (provider pricing changed)

### 3.3 — Adaptive retraining
ConsumptionKalman should retrain from recent burn history when burn
patterns shift (e.g., new cron jobs, worker profile changes).

**Deliverable:** Self-maintaining Kalman system that stays accurate.

---

## Phase 4: Validation & Rollout

### 4.1 — Shadow comparison period (minimum 48h)
Run LiveRouter in PARALLEL mode:
- `best_key()` still makes the real decision
- LiveRouter logs what it WOULD have picked
- Compare divergence rate, cost estimates

**Gate:** If divergence > 30% without cost improvement → investigate.

### 4.2 — Canary mode (10% traffic)
Route 10% of requests through LiveRouter (hash on request ID).
Monitor:
- Error rate (must be ≤ baseline)
- Latency (must be ≤ baseline + 5ms)
- Provider distribution (compare against replay predictions)

**Gate:** 24h with zero incidents → proceed to 50%.

### 4.3 — 50% rollout
Same monitoring. **Gate:** 48h → proceed to 100%.

### 4.4 — 100% rollout
Remove parallel/shadow logging. LiveRouter is the primary router.
Keep `best_key()` fallback alive for emergencies.

**Gate:** 7 days stable → archive fallback code.

### 4.5 — Kill switch
At any point: `rm ~/.hermes/bot/.enable_live_routing` → instant revert
to old `best_key()` logic. No restart needed.

---

## Phase 5: Post-Rollout Optimization

### 5.1 — Scarcity ceiling tuning
Replay showed scarcity (2x) is inert at current rate spreads. Consider:
- Aggressive scarcity (10x at 90%) to pre-shift high-volume traffic
- Or accept binary cliff (drain ours → switch) as optimal

### 5.2 — Quota window expansion
The real bottleneck is quota capacity. Work with z.ai to:
- Understand if 5h quota can be raised
- Add more API keys (3rd, 4th key = more flat-rate capacity)

### 5.3 — Model-aware routing
Currently difficulty is binary (high/medium/low). Add:
- Latency tracking per model per provider
- Quality scoring (response correctness checks)
- Route cheap models (flash) to lower-tier providers

---

## Risk Analysis

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Optimizer picks dead provider | Medium | High | Health gate + breaker in optimizer; 50ms timeout → retry on next provider |
| Kalman rates drift | Low | Medium | Daily reconciliation + convergence monitoring |
| Thread contention | Low | Medium | LiveRouter lock is fine-grained; optimizer builds fresh per call |
| External provider rate limit | Medium | Low | Quota tracking for ollama_cloud; PPQ/OpenRouter are pay-per-token |
| Regression vs binary router | Medium | High | Canary rollout + instant kill switch |

---

## File Inventory

**New files:**
- `src/live_router.py` — LiveRouter class
- `src/provider_endpoint.py` — Multi-provider request routing
- `scripts/calibrate_kalman_daily.py` — Daily rate reconciliation
- `tests/test_live_router.py` — Integration tests

**Modified files:**
- `~/.hermes/bot/zai_proxy.py` — best_key() calls LiveRouter, startup loads converged rates
- `~/.hermes/bot/zai_proxy.py` — _refresh_loop() feeds optimizer state

**Unchanged (already done):**
- `src/routing_optimizer.py` — core optimizer
- `src/price_kalman.py` — pricing filters
- `src/pricing_engine.py` — pace_factor, scarcity, health
- `scripts/feed_historical_costs.py` — converged rates loader

---

## Effort Estimate

| Phase | Description | Time |
|-------|-------------|------|
| 0 | Data infrastructure | 2-3 hours (delegate to worker) |
| 1 | Optimizer integration | 3-4 hours (delegate, manager reviews) |
| 2 | Provider endpoints | 2-3 hours (delegate) |
| 3 | Kalman calibration | 1-2 hours (cron job setup) |
| 4 | Validation & rollout | 5-7 days (soak time, canary gates) |
| 5 | Post-rollout tuning | Ongoing |

**Code work:** ~1 day of focused implementation (split across 3-4 kanban tasks).
**Soak time:** 7-10 days for full canary → 100% rollout.

---

## Success Criteria

1. **Cost reduction:** ≥ 15% reduction in effective $/M on fallback traffic
2. **Stability:** Zero regression in error rate or latency
3. **Self-maintaining:** Kalman filters stay converged without manual intervention
4. **Observable:** Every routing decision logged with full pricing breakdown
5. **Reversible:** Kill switch tested and verified before 100% rollout
