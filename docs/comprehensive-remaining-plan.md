# Comprehensive Remaining Steps Plan

**Date:** 2026-08-06
**Status:** READY FOR SCHEDULING
**Live in production:** Pressure + per-model pricing (all 6 kill switches ON)

---

## TRIAGE: What's Already Done vs Genuinely Remaining

### ALREADY DONE (cancel these stale tasks):

| Task | Status | Evidence |
|---|---|---|
| RP-1: Build realtime_pricing.py | DONE | src/realtime_pricing.py exists (1084 lines) |
| RP-3: Build real_price_tracker.py | DONE | src/real_price_tracker.py exists (1253 lines) |
| RP-3 (wire): Wire into live_router | DONE | live_router.py imports real_price_tracker, has _build_base_rates_from_tracker() |
| RP-4: Replace hardcoded rates | DONE | _DEFAULT_CONVERGED_RATES has comment "serves as fallback when real_price_tracker unavailable" |
| EUv2-5: Real Ollama quota | DONE | zai_proxy.py imports ollama_quota_tracker, uses get_quota_status |
| P4.5a: Model mapping table | DONE | model_mapping.py has full MODEL_MAP |
| EUv2-6: Cold review extra-usage | LIKELY DONE | 1744 tests, ollama_quota_tracker wired |

### GENUINELY REMAINING (4 workstreams, 8 tasks):

---

## Phase 1: Balance Collector Wiring (2 tasks)

**Problem:** PPQ and OpenRouter balance collectors EXIST but are NOT wired into live_router or cron'd.

### Task 1.1: Cron the balance collectors
- Schedule ppq_balance_collector every 5 min (cron or systemd timer)
- Schedule openrouter_balance_collector every 5 min
- Both write to api_burn.db balance_snapshots table
- **Est:** 10 min

### Task 1.2: Wire collector output into live_router quota_state
- live_router.py needs to read balance_snapshots for PPQ/OpenRouter
- Feed into _compute_ppq_pressure and _compute_credit_pressure
- Currently _compute_credit_pressure reads from api_calls SUM(cost_usd) — verify this works
- **Est:** 15 min

---

## Phase 2: CPVO Model-Aware Wiring (2 tasks)

**Problem:** get_effective_rates_model_aware() EXISTS in cpvo_calculator.py but is NOT called in live_router hot path.

### Task 2.1: Wire CPVO model-aware into live_router
- Replace get_effective_rates(base_rates) with get_effective_rates_model_aware(model_base_rates) when PER_MODEL_PRICING_ENABLED
- Kill switch: MODEL_AWARE_CPVO=true (default OFF)
- Falls back to provider-level when per-model samples < 100
- **Est:** 15 min

### Task 2.2: Model-aware request formatting in proxy (P4.5c)
- zai_proxy.py already reads task_type (line 3041)
- Wire task_type → MODEL_MAP → actual model name sent to provider
- Currently: proxy may send "glm-5.2" regardless of task_type
- **Est:** 20 min

---

## Phase 3: CVM Dashboard + Observability (2 tasks)

**Problem:** CVM server doesn't display measured rates or pricing data.

### Task 3.1: Show real measured rates in CVM snapshot
- cvm_server.py: add pricing keys to snapshot response
- Include: per-provider base rates, per-model rates, pressure multipliers, active kill switches
- **Est:** 20 min

### Task 3.2: 7-day shadow divergence report (T7)
- Automated report: routing decisions where live vs shadow disagreed
- Summary: divergence %, model distribution, provider distribution
- Schedule as cron or one-shot script
- **Est:** 15 min

---

## Phase 4: Cleanup + Cold Reviews (2 tasks)

### Task 4.1: Cancel stale tasks + unblock dependencies
- Cancel: RP-1, RP-3, RP-4, EUv2-5, EUv2-6, P4.5a (all done)
- Unblock: T1 (PPQ), T3 (OpenRouter) — work moved to Phase 1
- Clean up MAP-* leftover tasks

### Task 4.2: Cold review on balance collector wiring + CPVO wiring
- worker-inspector reviews Phase 1 + Phase 2 diffs
- Gate 2.5 mandatory for hot-path changes

---

## Dependency Graph

```
Phase 1.1 (cron collectors) ──┐
Phase 1.2 (wire to live_router)┘──┐
                                    ├──→ Phase 3.1 (CVM dashboard)
Phase 2.1 (CPVO model-aware) ──────┤
Phase 2.2 (request formatting) ────┘──→ Phase 3.2 (divergence report)
                                              ↓
                                    Phase 4.2 (cold review)
```

Phase 1 and Phase 2 are independent — can run in parallel.
Phase 3 depends on 1+2.
Phase 4 (cold review) depends on 1+2.

---

## Risk Assessment

| Phase | Risk | Why |
|---|---|---|
| 1.1 (cron) | LOW | New cron job, no routing change |
| 1.2 (wire collectors) | MEDIUM | Changes quota_state input — affects pressure computation |
| 2.1 (CPVO) | MEDIUM | Changes effective price computation in hot path |
| 2.2 (formatting) | LOW | Changes which model name is sent, not routing logic |
| 3.1 (CVM) | ZERO | Read-only dashboard |
| 3.2 (report) | ZERO | Read-only analysis |

All medium-risk tasks have kill switches and can be rolled back instantly.

---

## What This Enables

After all phases complete:
- Balance collectors feed real data every 5 min → accurate PPQ/OpenRouter/DeepInfra pressure
- CPVO model-aware → quality-weighted pricing (cheap garbage penalized)
- Model-aware formatting → correct model sent per task type
- CVM dashboard → operator visibility into all pricing decisions
- Divergence report → automated validation of routing decisions
