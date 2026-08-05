# Plan: Universal Exponential Pricing — Remaining Implementation

> **Status:** DRAFT — awaiting consultant review before scheduling
> **Date:** 2026-08-05
> **Depends on:** commits through 5bd12a9 (formula, constants, A=1.5)
> **ADR:** docs/ADR-asymptote-pricing.md

---

## What's Done

- ✅ Exponential pressure formula: `1 + K·t/(1-t)` in `quota_pressure_factor()`
- ✅ Superimposed windows: session × weekly × monthly (multiply, not max)
- ✅ Per-endpoint constants: onset, asymptote=1.5, hard_limit
- ✅ `_single_window_factor()` helper
- ✅ `_compute_zai_pressure()` and `_zai_window_usages()` helpers in live_router
- ✅ ADR document (facc977)
- ✅ 1328 tests passing, 4 skipped (pending wiring)

## What's Not Done (4 areas)

### Area 1: Wire Pressure into Live Router
The helper functions exist but aren't called during `select_failover()`. The 4 skipped tests cover this.

### Area 2: Balance Collectors for Paid Endpoints
PPQ, DeepInfra, OpenRouter need real-time balance queries to feed `balance_usage` into the pressure formula.

### Area 3: Trailing Base Rates
All base rates are currently hardcoded. Need to switch to trailing-365d amortized for subscriptions and 30d measured for pay-per-token.

### Area 4: Shadow Mode + Activation
New pricing runs in shadow mode (log decisions, don't route) for 48h before going live.

---

## Phase 1: Live Router Wiring (2 tasks)

### 1A: Wire z.ai pressure into select_failover

**File:** `src/live_router.py`
**What:** In `select_failover()`, when evaluating z.ai keys (ours + friend):
1. Extract session/weekly/monthly usage from `quota_state` dict
2. Call `_compute_zai_pressure()` to get combined pressure
3. Multiply z.ai base rate by pressure factor
4. If pressure = +inf (any window at 100%), mark key as unhealthy (breaker)

**Quota state mapping:**
```python
# Current quota_state has used_pct per key
# z.ai windows come from z.ai API (5h session, 7d weekly, 30d monthly)
# For now: use used_pct / 100 as session_usage (simplest mapping)
# Future: separate fields for each window
```

**Tests to unskip:**
- `test_price_rises_with_usage` — z.ai pressure rises monotonically
- `test_100_pct_trips_breaker` — at 100%, key excluded
- `test_superposition_in_integration` — 3-window multiply in routing

**Gate:** All 4 previously-skipped tests pass.

### 1B: Wire credit-based pressure (PPQ/DeepInfra/OpenRouter)

**File:** `src/live_router.py`
**What:** In `select_failover()`, for credit-based providers:
1. Compute `balance_usage = 1 - (remaining / starting_budget)` from quota_state
2. Call `quota_pressure_factor(usage=balance_usage, onset=0.80, asymptote=1.5, hard_limit=True)`
3. Multiply base rate by pressure
4. If balance = 0, pressure = +inf → key excluded

**Tests to unskip:**
- `test_deepinfra_pressure_rises_with_spend`
- `test_exhausted_openrouter_is_inf`

**Gate:** All skipped tests pass. Full suite 1332+ passing.

---

## Phase 2: Balance Collectors (3 tasks)

### 2A: PPQ balance collector

**File:** `src/balance_collectors.py` (new)
**What:** Cron-job-compatible module that queries PPQ API:
```python
# POST https://api.ppq.ai/credits/balance
# Returns: {"balance": 12.50, "currency": "USD"}
# Store in: ~/.hermes/bot/api_burn.db (existing table)
```
- Poll every 5 minutes (matches existing api_burn_collector cadence)
- Write to SQLite: `provider_balance(provider, balance, starting, ts)`
- Starting balance: known from top-up history ($20 initial)

**Config:** `PPQ_CREDIT_ID`, `PPQ_API_KEY` from env
**Gate:** Returns valid balance from live API.

### 2B: DeepInfra balance collector

**File:** `src/balance_collectors.py`
**What:** Query DeepInfra billing API:
```python
# GET https://api.deepinfra.com/v1/user/usage
# or: query total_spent from billing endpoint
# remaining = DEEPINFRA_STARTING_BALANCE - total_spent
```
- Poll every 5 minutes
- DeepInfra has webhook payloads with `inference_status.cost` — alternative: accumulate from per-request costs
- Starting balance: from `DEEPINFRA_STARTING_BALANCE` env var (default $5)

**Gate:** Returns valid remaining balance.

### 2C: OpenRouter balance collector

**File:** `src/balance_collectors.py`
**What:** Query OpenRouter API:
```python
# GET https://openrouter.ai/api/v1/key
# Returns: {"data": {"usage": 0.50, "limit": 10.00}}
# remaining = limit - usage
```
- Poll every 5 minutes
- Currently exhausted ($0) — collector should report usage_fraction=1.0

**Gate:** Returns valid usage/limit from API.

---

## Phase 3: Trailing Base Rates (3 tasks)

### 3A: z.ai trailing-365d amortized rate

**File:** `src/real_price_tracker.py` (extend existing)
**What:** Calculate z.ai effective rate from actual usage:
```python
# z.ai cost = $300/year (fixed subscription)
# tokens = SUM(total_tokens WHERE provider LIKE 'zai%' AND ts > now - 365d)
# amortized_rate = 300.0 / tokens_in_millions
# Example: 21B tokens/year → $300/21000M = $0.0143/M
```
- Friend key: same formula but with 1.21x premium (Felix's direction)
- Cold start: seed with estimate from prior months, update as data accumulates

**Data source:** `api_burn.db` — existing token tracking table
**Gate:** Calculated rate within 10% of $0.014/M (sanity check).

### 3B: Ollama trailing-90d measured rate

**File:** `src/real_price_tracker.py`
**What:** Calculate Ollama effective rate:
```python
# total_cost = SUM(cost_usd WHERE provider='ollama_cloud' AND ts > now - 90d)
# total_tokens = SUM(total_tokens WHERE provider='ollama_cloud' AND ts > now - 90d)
# measured_rate = total_cost / total_tokens_in_millions
# Expected: ~$0.0155/M (from billing data)
```
- Includes extra-usage costs in numerator
- 90d window (not 365d) because Ollama billing is monthly

**Gate:** Rate within 20% of $0.016/M.

### 3C: Paid endpoints trailing-30d rate

**File:** `src/real_price_tracker.py`
**What:** For PPQ, DeepInfra, OpenRouter:
```python
# total_cost = SUM(cost_usd WHERE provider=X AND ts > now - 30d)
# total_tokens = SUM(total_tokens WHERE provider=X AND ts > now - 30d)
# measured_rate = total_cost / total_tokens_in_millions
```
- 30d window (paid endpoint costs fluctuate)
- If no data: use seed rates (PPQ=$0.14, DeepInfra=$0.05, OpenRouter=$0.135)

**Gate:** Rates non-zero when data exists; fallback to seeds when cold.

---

## Phase 4: Shadow Mode + Activation (2 tasks)

### 4A: Shadow logger for new pricing

**File:** `src/shadow_logger.py` (extend existing)
**What:** When `UNIVERSAL_PRESSURE_SHADOW=true`:
1. Log every routing decision with: old price (no pressure) vs new price (with pressure)
2. Log which endpoint the new pricing WOULD have chosen
3. Log divergence count (how often new pricing disagrees with current)
4. Run for 48 hours

**Gate:** Shadow log captures 100+ routing decisions with divergence analysis.

### 4B: Activate universal pressure

**File:** `src/live_router.py`, `config/providers.yaml`
**What:**
1. Set `_UNIVERSAL_PRESSURE_ENABLED = True`
2. Remove old `scarcity_factor()` calls for z.ai/PPQ (replaced by quota_pressure_factor)
3. Keep `scarcity_factor()` as cold-start fallback only
4. Monitor for 24h: no 429 spikes, no unexpected routing, no cost anomalies

**Gate:** 24h clean run with no regressions. Rollback = set flag to False.

---

## Dependency Chain

```
Phase 1A (z.ai wiring) ─┐
                        ├─→ Phase 4A (shadow) ─→ Phase 4B (activate)
Phase 1B (credit wiring)┘                              │
                                                       │
Phase 2A (PPQ balance) ───────────────────────────────┤
Phase 2B (DeepInfra) ────────────────────────────────┤
Phase 2C (OpenRouter) ───────────────────────────────┤
                                                       │
Phase 3A (z.ai rate) ────────────────────────────────┤
Phase 3B (Ollama rate) ──────────────────────────────┤
Phase 3C (Paid rates) ───────────────────────────────┘
```

Phases 1, 2, 3 can run in PARALLEL (independent code paths).
Phase 4 depends on all of 1-3.

## Risk Mitigation

| Risk | Mitigation |
|---|---|
| z.ai 429s increase | hard_limit=True trips breaker → key excluded before 429 |
| PPQ balance depletes faster | Onset=0.80, price rises before exhaustion |
| New pricing worse than old | 48h shadow mode with divergence logging |
| Cold start with no data | Seed rates from ADR table, update as data arrives |
| Superposition too aggressive | A=1.5 keeps 3-window compound manageable (2.9x at 90/70/40%) |

## Estimated Tasks: 10
- Phase 1: 2 tasks (wiring)
- Phase 2: 3 tasks (collectors)
- Phase 3: 3 tasks (base rates)
- Phase 4: 2 tasks (shadow + activate)
