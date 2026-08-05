# Universal Pricing — Remaining Steps Plan (v2, consultant-reviewed)

**Status:** READY TO SCHEDULE
**Date:** 2026-08-05
**Branch:** converged-rate-replay
**Consultant review:** docs/plan-review-consultant.md

## Current State (verified by consultant)

### DONE (committed + pushed, 1332 tests pass):
- Exponential curve, superposition, all 5 provider pressure functions wired
- Uniform asymptote=1.5, onset staggering (z.ai=0.60, ollama=0.70, credits=0.80)
- Kill switches for all 5 providers (default OFF)
- Trailing 365d z.ai base rate
- z.ai 3-window superposition (5h × weekly × monthly)

### NOT DONE (from consultant review):
- Stale asymptote comments in 2 locations
- PPQ balance collector (real API integration)
- Cold-start safety (returns 1.0 optimistic, ADR says 0.5 conservative)
- Dynamic base rates (real_price_tracker wiring)
- Shadow mode with divergence metrics
- Full-cascade integration test (all 5 flags ON)
- Production activation

---

## Task 1: Fix stale comments (TRIVIAL)
- **Files:** src/live_router.py:811, src/pricing_engine.py:737
- **Change:** Fix "asymptote 5.0" → "1.5" in 2 stale comments
- **Est:** 2 min

## Task 2: Verify cost_usd logging (VERIFICATION)
- **Action:** Run `SELECT key_name, COUNT(*), SUM(cost_usd) FROM api_calls WHERE key_name IN ('deepinfra','openrouter') GROUP BY key_name`
- **If data exists:** DeepInfra/OpenRouter pressure already works (self-tracked). Task = add test only.
- **If no data:** Need to wire cost_usd logging into the proxy for these providers.
- **Est:** 5 min (verify) or 15 min (fix)

## Task 3: PPQ balance collector (MEDIUM)
- **Resolve first:** API URL (POST /credits/balance or /v1/credits/balance?), auth method, response field names
- **Check:** DQ05 monitor's dq05_ppq tool uses this endpoint — check its implementation
- **Build:** Collector that queries PPQ API every 5 min, writes balance to quota_state["ppq"]["used_pct"]
- **Bridge:** Wire balance into quota_state dict that _compute_ppq_pressure reads
- **Est:** 20 min

## Task 4: Cold-start safety fix (SMALL)
- **Change:** _compute_ppq_pressure and _compute_credit_pressure return 1.0 on cold start → return conservative value
- **Why:** Currently optimistic — dead endpoints (OpenRouter $0) appear cheap until first collector run
- **Fix:** Accept cold_start_seed parameter (default 0.5 per ADR), return that instead of 1.0
- **Test:** Cold start with no data → pressure > 1.0 (conservative bias away from unknown-balance)
- **Est:** 10 min

## Task 5: Wire dynamic base rates (SMALL)
- **Use existing:** real_price_tracker.get_real_rate(provider, model, window_hours)
- **Wire into:** _DEFAULT_CONVERGED_RATES dict (live_router.py:186)
- **Rates:**
  - z.ai: get_real_rate("ours", window_hours=8760) + $300/yr amortization
  - ollama: get_real_rate("ollama_cloud", window_hours=2160)
  - ppq/deepinfra/openrouter: get_real_rate(provider, window_hours=720)
- **Fallback:** If no data, use seed rates (existing hardcoded values)
- **Est:** 10 min

## Task 6: Shadow logger with divergence metrics (MEDIUM)
- **Extend:** src/shadow_logger.py (exists, 9.8KB)
- **Log:** For each routing decision, record what pressure-based routing WOULD have chosen vs actual
- **Metrics to track:**
  - Routing divergence (% of decisions that differ)
  - 429 rate from z.ai (should decrease with pressure)
  - Paid-endpoint spend (should decrease)
  - NaN/inf in effective_price (should be 0)
- **Exit criteria for go-live:**
  - Divergence < 15%
  - 429 rate ≤ baseline
  - Paid spend ≤ baseline
  - ≥ 500 decisions logged
  - ≥ 1 full z.ai session cycle observed
- **Conditional extension:** If weekly window not observed in 48h, extend to 7 days
- **Est:** 20 min

## Task 7: Full-cascade integration test (SMALL)
- **Test:** Enable ALL 5 kill switches simultaneously
- **Verify routing cascade:** z.ai (cheapest) → ollama → friend → DeepInfra → OpenRouter → PPQ
- **Verify scarcity neutralized** for ALL 6 providers when flags ON
- **Test deadlock fallback:** all at ∞ → picks least-bad
- **Gate:** Must pass before Phase 8 activation
- **Est:** 15 min

## Task 8: Activate + monitor (GRADUAL)
- **Sequence:** Enable one kill switch at a time, 24h between each:
  1. OLLAMA_QUOTA_PRESSURE_ENABLED=true (lowest risk)
  2. ZAI_QUOTA_PRESSURE_ENABLED=true
  3. PPQ_QUOTA_PRESSURE_ENABLED=true
  4. OPENROUTER_CREDIT_PRESSURE_ENABLED=true
  5. DEEPINFRA_CREDIT_PRESSURE_ENABLED=true
- **Monitor:** 429 rate, routing divergence, paid spend
- **Rollback:** Set flag back to false + restart service. Document all 5 flag names.
- **Est:** 5 days (24h per flag)

---

## Dependency Graph

```
Task 1 (stale comments) ────────────────────────────────┐
Task 2 (verify cost_usd) ──────────────────────────────┐│
Task 3 (PPQ collector) ──────────────────────────────┐ ││
Task 4 (cold-start fix) ───────────────────────────┐ │││
Task 5 (dynamic rates) ──────────────────────────┐ ││││
                                                  v vvv
Task 6 (shadow logger) ◄───────────────────────────────
  │
  v
Task 7 (cascade test) ◄── Gate 6
  │
  v
Task 8 (activate) ◄── Gate 7 + Felix approval
```

Tasks 1-5 are independent — can run in parallel.
Task 6 depends on 1-5.
Task 7 depends on 6.
Task 8 depends on 7 + explicit Felix go-ahead.
