# Universal Pricing — Remaining Steps Plan

**Status:** DRAFT — awaiting consultant review
**Date:** 2026-08-05
**Branch:** converged-rate-replay

## Current State

### DONE (committed + pushed):
- Exponential curve (1/(1-x) asymptote) — `quota_pressure_factor()`
- Superposition (session × weekly × monthly multiplied)
- z.ai pressure (3 windows, onset=0.60, hard_limit=True)
- ollama_cloud pressure (2 windows, onset=0.70, hard_limit=False)
- PPQ pressure (onset=0.80, hard_limit=True, reads used_pct from quota_state)
- Uniform asymptote=1.5 constants for ALL endpoints
- Trailing 365d base rate for z.ai
- Kill switches for all endpoints (default OFF)
- 87 pressure tests passing
- Decision doc (docs/pricing-decisions-2026-08-05.md)

### NOT DONE (from consultant review):
- OpenRouter/DeepInfra pressure = dead code (constants exist, no computation)
- Credit formula `u = 1-(remaining/starting)` not implemented
- Below-onset discount bug (pressure < 1.0 for u < onset)
- Division-by-zero guard (starting_balance=0)
- All-∞ deadlock fallback undefined
- Credit top-up overflow (remaining > starting)
- Production wiring (zai_proxy.py integration)
- Shadow mode validation

---

## Phase 1: Bug Fixes (consultant RED flags)

**Goal:** Fix mathematical correctness issues before any new features.

### 1.1 Below-onset clamping
- **File:** `src/pricing_engine.py` — `_single_window_factor()` 
- **Change:** Add `max(1.0, ...)` or early-return 1.0 when `u <= onset`
- **Why:** Raw formula `1 + K·t/(1-t)` produces 0.70x at u=0% — gives fresh endpoints artificial 30% discount, causes routing oscillation
- **Test:** `test_below_onset_returns_unity` — verify pressure=1.0 at u=0%, 30%, 59%, onset-epsilon
- **Gate:** pytest passes

### 1.2 Division-by-zero guard
- **File:** `src/live_router.py` — credit pressure functions
- **Change:** Guard `u = 1 - (remaining / starting)` when starting=0 → return 1.0 (cold start, no penalty)
- **Why:** Credit-based endpoints with starting_balance=0 would crash
- **Test:** `test_credit_pressure_zero_starting_balance` — returns 1.0, no crash
- **Gate:** pytest passes

### 1.3 Credit top-up overflow
- **File:** `src/live_router.py` — credit pressure functions
- **Change:** Clamp `u = max(0.0, min(1.0, 1 - (remaining/starting)))` — if remaining > starting (topped up), u=0
- **Why:** Prevents negative u from amplifying the below-onset discount
- **Test:** `test_credit_topup_clamps_to_zero` — remaining=$15, starting=$10 → u=0 → pressure=1.0
- **Gate:** pytest passes

---

## Phase 2: Implement Missing Pressure (OpenRouter + DeepInfra)

**Goal:** Wire pressure computation for the two credit-based endpoints that have dead code.

### 2.1 DeepInfra balance tracking
- **Files:** `src/live_router.py`, `src/realtime_pricing.py`
- **Change:** Add `_compute_deepinfra_pressure()`:
  - Query zai_usage.db: `SELECT SUM(cost_usd) FROM api_calls WHERE provider='deepinfra'`
  - `remaining = DEEPINFRA_STARTING_BALANCE - cumulative_spend`
  - `u = clamp(1 - (remaining / DEEPINFRA_STARTING_BALANCE))`
  - `pressure = quota_pressure_factor(u, onset=0.80, asymptote=1.5, hard_limit=True)`
- **Test:** Mock DB returns $2.50 spent → remaining=$2.50 → u=0.5 → pressure=1.0 (below onset)
- **Gate:** pytest passes

### 2.2 OpenRouter balance tracking
- **Files:** `src/live_router.py`
- **Change:** Add `_compute_openrouter_pressure()`:
  - Read balance from balance_snapshots DB (already tracked)
  - `u = clamp(1 - (balance / OPENROUTER_STARTING_BALANCE))`
  - `pressure = quota_pressure_factor(u, onset=0.80, asymptote=1.5, hard_limit=True)`
- **Test:** Balance=$0 → u=1.0 → pressure=∞; Balance=$10 → u=0 → pressure=1.0
- **Gate:** pytest passes

### 2.3 Wire into provider loop
- **File:** `src/live_router.py` — `_do_select_failover()`
- **Change:** Add branches for `openrouter` and `deepinfra` in the pressure application section (same pattern as z.ai/ollama/PPQ)
- **Add to `prov_has_pressure` set** (line ~912)
- **Test:** Integration test — verify all 5 providers get pressure multiplier applied
- **Gate:** pytest passes, verify with debug logging

---

## Phase 3: Deadlock Fallback

**Goal:** Define behavior when ALL endpoints are exhausted.

### 3.1 Least-bad fallback
- **File:** `src/live_router.py`
- **Change:** When all candidates have pressure=∞:
  - Pick the one closest to finite (lowest actual pressure)
  - If all truly ∞, pick z.ai (ours key) as last resort — subscription is sunk cost, 429s auto-retry
  - Log CRITICAL: "all endpoints exhausted — using z.ai ours as last resort"
- **Why:** Total deadlock should degrade gracefully, not hard-fail
- **Test:** `test_all_exhausted_picks_cheapest` — all at ∞ → picks base_rate cheapest
- **Gate:** pytest passes

---

## Phase 4: Test Coverage (consultant gaps)

**Goal:** Close all test gaps identified in the code review.

### 4.1 Asymptote=1.5 tests
- Update bare `quota_pressure_factor()` tests to use explicit asymptote=1.5
- Test crossover points at 1.5 (not 4.17)
- Verify `test_at_full_usage_caps_at_asymptote` uses the right constant

### 4.2 Per-provider onset tests
- z.ai onset=0.60: verify pressure starts at exactly 60%
- ollama onset=0.70: verify pressure starts at 70%
- Credit onset=0.80: verify pressure starts at 80%

### 4.3 Monthly window (3-window path)
- Test `quota_pressure_factor(u, weekly=w, monthly=m)` directly
- Verify product of 3 independent factors

### 4.4 End-to-end 5-provider test
- Mock all 5 providers with various depletion levels
- Verify router picks correctly based on combined pressure + base_rate

---

## Phase 5: Production Integration

**Goal:** Wire the pricing engine into the production zai_proxy.py.

### 5.1 Shadow mode
- Import pricing modules into zai_proxy.py
- Log pressure computations alongside actual routing decisions
- DO NOT change actual routing — just observe
- Duration: 48h
- Kill switch: `PRESSURE_SHADOW_MODE=true/false`

### 5.2 Shadow analysis
- After 48h, analyze: would pressure-based routing have made different choices?
- Count: how many times would router have diverted vs actual
- Validate: no premature reroutes at low usage

### 5.3 Enable pressure (gradual)
- Turn on kill switches one at a time:
  1. `OLLAMA_QUOTA_PRESSURE_ENABLED=true` (lowest risk — already tested)
  2. `ZAI_QUOTA_PRESSURE_ENABLED=true`
  3. `PPQ_QUOTA_PRESSURE_ENABLED=true`
  4. `OPENROUTER_CREDIT_PRESSURE_ENABLED=true`
  5. `DEEPINFRA_CREDIT_PRESSURE_ENABLED=true`
- Monitor 24h between each enablement

---

## Phase 6: Cleanup

**Goal:** Remove legacy code, retire scarcity_factor.

### 6.1 Retire scarcity_factor
- Once all endpoints have pressure, scarcity is fully subsumed
- Set `scarcity = 1.0` in routing_optimizer
- Mark `scarcity_factor()` as deprecated

### 6.2 Remove legacy extra_usage_multiplier
- Fully superseded by continuous pressure
- Keep function but mark deprecated

### 6.3 Stale docstring cleanup
- Remove all references to old asymptote values (4.17, 5.0, 3.0, 2.0)
- Update all price tables in docstrings to asymptote=1.5 values

---

## Phase 7: Routstr Vision (future, not scheduled)

### 7.1 EndpointPriceModel classes
- Wrap each provider's pricing into a self-contained class
- Each class: base_rate(), pressure(), effective_price()

### 7.2 Nostr price publishing
- Each model → Routstr node, publishes effective price
- Optimizer subscribes to prices, picks cheapest

### 7.3 Scoped JWT spending limits
- DeepInfra: create JWT with spending_limit=$5.00
- Maps to prepaid envelope model

---

## Scheduling

| Phase | Task ID | Assignee | Dependencies | Est. Time |
|---|---|---|---|---|
| 1.1 | P1-BELOW-ONSET | worker-merchant | none | 10 min |
| 1.2 | P1-DIVZERO | worker-merchant | none | 5 min |
| 1.3 | P1-TOPOVERFLOW | worker-merchant | none | 5 min |
| 2.1 | P2-DEEPINFRA | worker-merchant | Phase 1 | 15 min |
| 2.2 | P2-OPENROUTER | worker-merchant | Phase 1 | 15 min |
| 2.3 | P2-WIRE | worker-merchant | 2.1, 2.2 | 15 min |
| 3.1 | P3-DEADLOCK | worker-merchant | Phase 2 | 10 min |
| 4.1-4.4 | P4-TESTS | worker-merchant | Phase 1-3 | 20 min |
| 5.1 | P5-SHADOW | worker-merchant | Phase 1-4, Felix approval | ongoing |
| 5.2 | P5-ANALYZE | worker-merchant | 5.1 (48h) | 15 min |
| 5.3 | P5-ENABLE | worker-merchant | 5.2 | gradual |
| 6.1-6.3 | P6-CLEANUP | worker-merchant | Phase 5 | 15 min |
| 7.x | ROUTSTR | future | Phase 6 | TBD |
