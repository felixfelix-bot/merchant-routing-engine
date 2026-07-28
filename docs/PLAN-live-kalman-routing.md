# PLAN: Live Kalman Price-Driven Routing (v2.1)

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

### Critical Design Principle: Never Break Token Flow

**The optimizer's primary value is FALLBACK SELECTION, not primary routing.**

Normal requests (ours or friend available) → unchanged z.ai path.
When both z.ai keys exhaust → optimizer picks cheapest external provider.
This means NO new request-formatting code on the hot path until Phase 4
(canary-proven). External provider endpoints stay behind the existing
failover boundary. Hermes can ALWAYS burn tokens.

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

**Problem:** `best_key()` picks between ours/friend only. When both exhaust,
failover to external providers is ad-hoc (tries ollama, then ppq, then
openrouter in hardcoded order — no price awareness). Optimizer would pick
ollama_cloud ($0.024/M) over friend ($0.029/M) when both z.ai keys are dead.

---

## Target Architecture (v2.1 — safer sequencing)

```
zai_proxy.py (port 9099)
  ├── best_key()                    ← WRAPPER (backward compat, logging)
  │   ├── Normal path: ours/friend (unchanged z.ai routing)
  │   └── Failover path: calls LiveRouter.select_failover()
  │       when BOTH z.ai keys exhausted
  ├── LiveRouter (LIVE, failover-only initially)
  │   ├── RoutingOptimizer with converged Kalman rates
  │   ├── Considers ollama_cloud, friend, ppq, openrouter, deepinfra
  │   ├── Picks cheapest viable external provider
  │   └── Returns (provider, fallback) pair for retry safety
  ├── External failover (enhanced)  ← optimizer-driven ordering
  └── _refresh_loop()               ← feeds quota state to optimizer

  Later (Phase 4+): optimizer also picks PRIMARY provider when canary-proven.
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

## Phase 1: LiveRouter — Failover Selection Only (safe hot path)

### 1.1 — Create `LiveRouter` class
**Status:** DONE (commit 1b76b7b, branch converged-rate-replay)
**File:** `merchant-routing-engine/src/live_router.py`

```python
class LiveRouter:
    """Bridges zai_proxy.py to RoutingOptimizer for LIVE failover routing.
    
    PHASE 1 SCOPE: Only called when BOTH z.ai keys are exhausted.
    Normal requests (ours or friend available) use unchanged best_key() path.
    
    Thread-safe (called from ThreadingHTTPServer handler threads).
    """
    def __init__(self, db_path, converged_rates):
        self._price_kalmans = {name: PriceKalman(rate) for ...}
        self._consumption_kalmans = {name: ConsumptionKalman() for ...}
        self._lock = threading.Lock()
    
    def select_failover(self, quota_state, health_state, peak,
                        failure_counts, pace_windows) -> tuple[str, str]:
        """Returns (provider, fallback_provider) when z.ai is exhausted.
        
        Considers: ollama_cloud, friend (if recovered), ppq, openrouter, deepinfra.
        Returns cheapest viable + a fallback for retry safety.
        """
        # Build optimizer with all external providers + exhausted z.ai keys
        # Call route()
        # Return (chosen, next_cheapest)
    
    def select_primary(self, model, tokens, quota_state, ...) -> str:
        """PHASE 4 ONLY: Pick primary provider (ours vs friend vs ollama).
        Not called in Phase 1. Exists for future canary rollout."""
    
    def record_request(self, provider, tokens, cost_estimate):
        """Update Kalman filters after request completes."""
```

### 1.2 — Modify failover path in zai_proxy.py
**File:** `~/.hermes/bot/zai_proxy.py`
**Status:** ✅ DONE (2026-07-28)

ONLY the failover section changes. Normal `best_key()` path is untouched.

**What was implemented:**

1. **Startup (after ShadowHook init):** `_LIVE_ROUTER` is created with
   converged rates from `load_historical_rates()`. Wrapped in try/except —
   import failure sets `_LIVE_ROUTER = None` and the proxy continues normally.

2. **`best_key()` Phase 5 (new, after Phase 4 health check):** When
   `chosen is None` (both z.ai keys exhausted) AND `_LIVE_ROUTER` is not None
   AND the kill-switch file `~/.hermes/bot/.enable_live_routing` exists,
   calls `_LIVE_ROUTER.select_failover()` with quota/health/peak snapshots.
   If LiveRouter returns a provider, logs the decision and returns it.
   On any exception, falls through to `return None` and the hardcoded
   failover chain in `_proxy()` runs.

3. **`_proxy()` external provider dispatch (new block after `if chosen is None:`):**
   When `best_key()` returns an external provider (not in `KEYS`), routes
   to `_try_ollama_cloud()` or `_try_external_failover()` as appropriate.
   If the LiveRouter-chosen provider fails, falls through to the same
   hardcoded chain. Hermes always gets tokens.

**What changed:** One try/except block in `best_key()` (Phase 5) + one
dispatch block in `_proxy()`. If LiveRouter fails, the existing hardcoded
ollama→ppq→openrouter chain still runs. Hermes always gets tokens.

**What did NOT change:** The entire ours/friend selection path (Phases 1-4).
Proactive Kalman predictions, reactive thresholds, recovery checks, health
gates — all untouched. `_best_unlocked()` and `_refresh_loop()` untouched.

**Tests:** `tests/test_live_router_wire.py` — 8 tests covering:
  - LiveRouter called when both keys exhausted + kill switch ON
  - LiveRouter NOT called when kill switch OFF (normal routing unchanged)
  - LiveRouter exception falls through to hardcoded failover
  - Normal routing path COMPLETELY UNCHANGED (keys available)
  - `_LIVE_ROUTER = None` (import failure) → normal behavior
  - LiveRouter returns (None, None) → falls through
  - Kill switch path constant check

**Cold review:** APPROVED — all 9 criteria verified (normal routing
untouched, LiveRouter only fires on both-exhausted, kill switch checked,
all calls wrapped in try/except, hardcoded chain preserved).

### 1.3 — Kill switch
```bash
# Enable:  touch ~/.hermes/bot/.enable_live_routing
# Disable: rm ~/.hermes/bot/.enable_live_routing
# No restart needed. Next request falls through to hardcoded failover.
```

**Deliverable:** Optimizer picks cheapest external provider when z.ai is down.
Normal routing untouched. Hermes always has tokens.

---

## Phase 2: Kalman Data Feeds (keep filters fresh)

### 2.1 — Converged rates at startup
**DONE:** `scripts/feed_historical_costs.py` exists. Called at proxy startup
via `_converged_rates = load_historical_rates()` → passed to LiveRouter
constructor.

### 2.2 — Live price observations (daily cron)
**DONE:** `scripts/calibrate_kalman_daily.py` — daily PriceKalman calibration.

Queries `daily_spend` for yesterday's effective $/M per provider, feeds to
PriceKalman via `.update()`, and logs convergence to `kalman_samples` table.

Usage (standalone):

    python3 scripts/calibrate_kalman_daily.py
    python3 scripts/calibrate_kalman_daily.py --db /path/to/zai_usage.db
    python3 scripts/calibrate_kalman_daily.py --days-back 7   # catch up after downtime

Cron setup (daily at 02:00 UTC):

    0 2 * * * cd ~/merchant-routing-engine && python3 scripts/calibrate_kalman_daily.py >> /tmp/kalman_calibration.log 2>&1

Tests: `tests/test_calibration.py` (21 tests — import, query, feed, log,
standalone execution, robustness). Coverage: 85%.

### 2.3 — Live consumption tracking
**DONE:** Wired in `zai_proxy.py` `_proxy()` finally block. After every
completed request, calls `_LIVE_ROUTER.record_request(provider, tokens)`.
This updates the ConsumptionKalman for the serving provider, keeping
burn-rate predictions fresh. Wrapped in try/except — NEVER breaks request
handling.

### 2.4 — Pace window computation
**DONE:** Wired in `zai_proxy.py` `_refresh_loop()`. After quota refresh,
calls `_LIVE_ROUTER.compute_pace_windows(quota_cache)` to compute pace
factor input tuples from quota windows + ConsumptionKalman burn rates.
Stored in `_pace_windows` global, passed to `select_failover()` in
`best_key()` on the next failover call. Wrapped in try/except — NEVER
blocks quota refresh.

`LiveRouter.compute_pace_windows()` (new method in `src/live_router.py`):
converts the proxy's `quota_cache` structure into pace_factor input tuples
for `pace_factor_multi()`. Uses `quota_window_extractor` constants for
known window names and error sentinels. Thread-safe via `self._lock`.

Tests: `tests/test_live_router.py::TestComputePaceWindows` (10 tests —
dict keyed by provider, 5-element tuples, 5h+weekly windows, quota_used
math, burn_rate from ConsumptionKalman, skips unknown/malformed, never
raises on garbage). Coverage: 93%.

**Deliverable:** All 5 Kalman inputs stay fresh without manual intervention.

---

## Phase 2.5: Provider Quality Telemetry (CPVO Foundation)

**WHY:** All providers advertise GLM-5.2. None are guaranteed to actually
serve GLM-5.2. A provider running a quantized model, truncating context,
or inflating token counts would be invisible without measurement. The
Kalman optimizer would see the cheaper provider as lower cost and route
MORE traffic there — exactly the wrong direction.

This phase builds the measurement layer BEFORE the soak test so we can
validate provider quality during real traffic, not just provider cost.

### 2.5.1 — Telemetry table (success/fail/latency per request)
**Status:** ✅ DONE (2026-07-28)

New `provider_telemetry` table added to `~/.hermes/bot/zai_usage.db`:
- `response_received` (bool) — did the API return anything?
- `response_valid` (bool) — did the response parse as valid LLM output?
- `latency_ms` (int) — time from request to first token
- `error_type` (text) — timeout, auth, rate_limit, parse_error, none
- `billed_tokens` (int) — what the provider claimed in usage
- `actual_tokens` (int) — what we measured from response length
- `token_mismatch` (bool) — billed != actual (fraud signal)

One INSERT per request in `_proxy()` finally block. Schema migration via
`CREATE TABLE IF NOT EXISTS` at startup. Telemetry NEVER raises — all paths
wrapped in try/except so telemetry failure is silent.

Tests: `tests/test_provider_telemetry.py` — 12 tests, 85% coverage on new
functions. Cold review: APPROVED (telemetry never blocks request handling).

### 2.5.2 — CPVO calculator
**Status:** ✅ DONE (2026-07-28)

CPVO = SUM(cost) / SUM(success) per provider per time window.

If provider A is $0.001/M with 92% success and provider B is $0.029/M
with 99.9% success:
- A effective: $0.001 / 0.92 = $0.00109/M per successful request
- B effective: $0.029 / 0.999 = $0.029/M per successful request

A is still cheaper. But if A drops to 80% success → $0.00125/M.
The math works until it doesn't — we won't know when it stops without measuring.

Built as `src/cpvo_calculator.py` (`CPVOCalculator` class):
- Queries provider_telemetry table for a configurable time window (default 24h)
- `compute_cpvo(provider, window_hours, base_rate)` — returns cost-per-valid-output
  ($/success when base_rate given, tokens/success when not)
- `get_effective_rates(base_rates)` — adjusts base $/M rates: `base / success_rate`
  when success < 0.95, unchanged otherwise; insufficient data (<100 samples) = no
  penalty; 0% success = massive penalty (avoids the provider)
- `get_quality_score(provider)` — returns success_rate, avg_latency_ms,
  token_mismatch_rate, sample_count, cpvo, effective_rate
- EVERY public method wraps in try/except — never raises, returns base on error
- Critical invariant (cold-reviewed): denominator is **success_count**, not
  **total_count** — verified by independent reviewer + 22 tests

Tests: `tests/test_cpvo_calculator.py` — 22 tests, 88% coverage. Cold review:
APPROVED (CPVO math correct, success_rate penalty verified).

Consumers (not yet wired — Phase 3.x):
- `LiveRouter.select_failover()` — will call `get_effective_rates()` to adjust
  base rates before optimization
- Calibration cron — will feed quality-adjusted rates to PriceKalman

### 2.5.3 — Quality probes (canary prompts)

Periodically send a known prompt to each provider and compare output:
- "What is 2+2?" → should return "4"
- "Write a 3-line Python function" → should produce valid Python
- "Summarize this text in one sentence" → should produce coherent text

If a provider returns garbage, truncated output, or wrong answers,
it's running a different model or a degraded version. This is the
canary in the coal mine — detect silent downgrades BEFORE they affect
production routing.

Build as `scripts/quality_probe.py`:
- Cron job (every 4h)
- Sends 3 probe prompts to each provider
- Scores: valid_response (bool), correct_answer (bool), latency_ms
- Logs to provider_telemetry table
- Alerts if quality drops below threshold

### 2.5.4 — Token count audit

Compare billed tokens vs actual response length. Mismatch = billing fraud.
- `billed_tokens` from API response usage field
- `actual_tokens` from len(response) / 4 (rough char-to-token estimate)
- If mismatch > 20% → flag provider for investigation
- Feed mismatch rate into CPVO calculator as quality penalty

**Deliverable:** Quality-aware routing decisions. Optimizer sees
effective cost (price / success_rate), not just sticker price.
Silent downgrades detected within 4h. Billing fraud detected immediately.

---

## Phase 3: Validation (failover-only soak)

### 3.1 — Shadow comparison (48h)
LiveRouter runs in parallel with hardcoded failover. Log what optimizer
WOULD pick vs what hardcoded chain actually picked.

**Gate:** Divergence rate and cost savings measured. If optimizer never
picks differently → investigate (rates may be too close to matter).

### 3.2 — Live failover (when both z.ai keys exhaust)
Enable LiveRouter for failover only. Since failover is rare (both keys
dead), this tests the optimizer on real traffic with minimal risk.

**Gate:** 10 failover events with zero incidents → proceed to Phase 4.

### 3.3 — Kill switch test
Verify: `rm ~/.hermes/bot/.enable_live_routing` → next request uses
hardcoded failover. No restart, no error.

**Deliverable:** Confidence that optimizer-driven failover is safe.

---

## Phase 4: Primary Routing (canary — only after Phase 3 proven)

### 4.1 — Enable select_primary() on hot path
Now LiveRouter picks BETWEEN ours and friend (not just external failover).
Still wrapped in try/except, still has kill switch.

```python
def best_key() -> str:
    if _LIVE_ROUTER and os.path.exists(ENABLE_FLAG):
        try:
            choice = _LIVE_ROUTER.select_primary(model, tokens, ...)
            if choice:  # optimizer gave us an answer
                _log_key_decision(...)
                return choice
        except Exception:
            pass  # fall through
    
    # --- EXISTING: unchanged fallback ---
    ...
```

### 4.2 — Canary: 10% traffic
Hash on request ID. 90% uses old best_key(), 10% uses LiveRouter.
Monitor: error rate, latency, provider distribution.

**Gate:** 24h zero incidents → 50%.
**Gate:** 48h zero incidents → 100%.

### 4.3 — 100% primary routing
Remove canary gate. LiveRouter is primary router for ALL requests.
best_key() stays as permanent emergency fallback.

### 4.4 — Multi-provider primary routing (future)
When optimizer picks ollama_cloud as PRIMARY (not just failover), the
request handler needs to format for ollama's API. This is the risky step
— only done after canary proves optimizer picks correctly.

**Gate:** Separate canary for ollama-as-primary. Has its own kill switch.

### 4.5 — Model-aware routing (kimi-k3 on ollama + task-type mapping)

**WHY:** Currently proxy sends same model name to every provider. When
z.ai exhausts, it requests glm-5.2 from ollama — but ollama may serve
a different model or not have glm-5.2 at all. Kimi K3 (2.8T MoE agentic)
is now available on ollama cloud. It may produce better code than
kimi-k2.7-code for coding tasks. But we don't know without measuring.

This phase makes the optimizer model-aware: different models on
different providers, chosen by CPVO per (provider, model, task_type).

#### 4.5.1 — Model mapping table

Build `src/model_mapping.py`:

```python
MODEL_MAP = {
    # (provider, task_type) → model_name
    ("z.ai", "reasoning"): "glm-5.2",
    ("z.ai", "coding"): "kimi-k2.7-code",
    ("z.ai", "simple"): "glm-4.5-flash",
    ("ollama_cloud", "reasoning"): "glm-5.2",      # if available
    ("ollama_cloud", "coding"): "kimi-k3",          # new — MoE agentic
    ("ollama_cloud", "simple"): "gemma4",            # lightweight
    ("friend", "reasoning"): "glm-5.2",
    ("friend", "coding"): "kimi-k2.7-code",          # if friend has it
    ("friend", "simple"): "glm-4.5-flash",
    ("openrouter", "*"): "auto",                      # let OR pick
}

def get_model_for(provider: str, task_type: str) -> str:
    """Return model name for (provider, task_type). Falls back to
    provider's default if no specific mapping exists."""
```

Task type comes from the worker profile making the API call:
- worker-balloon, worker-admin, worker-plebeian → "coding"
- worker-fips, worker-tollgate, treasurer → "reasoning"
- worker-base, worker-dq05 → "simple"

The proxy extracts task_type from the request header or worker
profile config and passes it to LiveRouter.

#### 4.5.2 — CPVO per (provider, model) pair

Extend `src/cpvo_calculator.py`:
- `compute_cpvo(provider, model, window_hours)` — not just per provider
- Quality probes test EACH model on EACH provider independently
- CPVO table: rows = (provider, model), columns = success_rate, avg_latency, cpvo

This tells us: "kimi-k3 on ollama at $0.05/M with 95% success for coding
tasks" vs "kimi-k2.7-code on z.ai at $0.001/M with 99% success for coding
tasks." The optimizer picks the lower CPVO.

#### 4.5.3 — Model-aware request formatting

In `_proxy()`, when LiveRouter returns a provider+model pair:
- If model differs from what z.ai expects → reformat request body
  (change "model" field in OpenAI-compatible request)
- Most providers are OpenAI-compatible → just change the model string
- Log the model swap in provider_telemetry

#### 4.5.4 — Worker profile → task_type mapping

Add `task_type` field to worker profile configs:
```yaml
# ~/.hermes/profiles/worker-balloon/config.yaml
task_type: coding

# ~/.hermes/profiles/worker-fips/config.yaml
task_type: reasoning

# ~/.hermes/profiles/worker-base/config.yaml
task_type: simple
```

Proxy reads this from the profile config and includes it in the
routing decision context.

**Gate:** Kimi-k3 CPVO must be measured for ≥ 7 days before routing
coding tasks to it. If CPVO > kimi-k2.7-code CPVO → don't switch.

**Gate:** Model-aware routing has its own kill switch:
`~/.hermes/bot/.enable_model_aware_routing`

**Deliverable:** Optimizer picks the best (provider, model) pair per
task type. Kimi-k3 used for coding ONLY if CPVO proves it's worth it.
Silent model substitution on any provider detected by quality probes.

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
| Optimizer picks dead failover provider | Medium | Low | Returns (provider, fallback) pair; handler retries with fallback. Hardcoded chain still runs on exception. |
| Kalman rates drift | Low | Medium | Daily reconciliation cron + convergence monitoring |
| Thread contention | Low | Medium | LiveRouter lock is fine-grained; optimizer builds fresh per call |
| External provider rate limit | Medium | Low | Quota tracking for ollama_cloud; PPQ/OpenRouter are pay-per-token |
| LiveRouter exception breaks failover | Low | HIGH | try/except wraps ALL LiveRouter calls. Exception → hardcoded chain. Hermes always gets tokens. |
| Normal routing regression | LOW (Phase 1-3) | N/A | Normal path UNTOUCHED until Phase 4 canary. Phase 1-3 only changes failover section. |

---

## File Inventory

**New files:**
- `src/live_router.py` — LiveRouter class (select_failover + select_primary)
- `scripts/calibrate_kalman_daily.py` — Daily rate reconciliation cron
- `tests/test_live_router.py` — Integration tests

**Modified files:**
- `~/.hermes/bot/zai_proxy.py` — failover section calls LiveRouter (Phase 1); startup loads converged rates (Phase 2)
- `~/.hermes/bot/zai_proxy.py` — _refresh_loop() computes pace windows (Phase 2)
- `~/.hermes/bot/zai_proxy.py` — best_key() calls select_primary() (Phase 4 only, canary-gated)

**Unchanged (already done):**
- `src/routing_optimizer.py` — core optimizer
- `src/price_kalman.py` — pricing filters
- `src/pricing_engine.py` — pace_factor, scarcity, health
- `scripts/feed_historical_costs.py` — converged rates loader

---

## Effort Estimate

| Phase | Description | Time | Risk |
|-------|-------------|------|------|
| 0 | Data infrastructure | 2-3 hours | None — read-only feeds |
| 1 | LiveRouter (failover only) | 3-4 hours | LOW — normal path untouched |
| 2 | Kalman data feeds | 1-2 hours | None — background only |
| 3 | Failover soak validation | 3-5 days | LOW — failover is rare |
| 4 | Primary routing canary | 5-7 days | MEDIUM — canary-gated |
| 5 | Post-rollout tuning | Ongoing | Low |

**Phase 1-2 code:** ~1 day (split across 3 kanban tasks).
**Phase 3-4 soak:** 8-12 days total (canary gates, no shortcuts).
**Hermes never loses token access at any point.**

---

## Success Criteria

1. **Cost reduction:** ≥ 15% reduction in effective $/M on fallback traffic
2. **Stability:** Zero regression in error rate or latency
3. **Self-maintaining:** Kalman filters stay converged without manual intervention
4. **Observable:** Every routing decision logged with full pricing breakdown
5. **Reversible:** Kill switch tested and verified before 100% rollout
