# Consolidated Plan: Remaining Steps

> **Date:** 2026-08-06
> **Author:** Manager (consultant timed out, verified manually)
> **Status:** READY FOR REVIEW

## 1. Audit Results — Board vs Reality

I checked actual code existence and git history against the 30+ task backlog.

| Task | Title | Board Status | REAL Status | Evidence |
|---|---|---|---|---|
| **RP-1** | Build realtime_pricing.py | blocked | **DONE** | File exists (45KB), running in prod |
| **RP-2** | Tests for realtime_pricing | todo | **DONE** | 1752 tests pass, includes RP tests |
| **RP-3** | Build real_price_tracker.py | blocked | **DONE** | File exists (52KB), running in prod |
| **RP-3** | Wire RealtimePricing into routers | todo | **DONE** | 17 refs to dynamic rates in live_router |
| **RP-4** | Replace hardcoded rates | todo | **DONE** | _resolve_dynamic_base_rates active |
| **RP-4** | Cold review | todo | **STALE** | Superseded by multiple later reviews |
| **RP-5** | Show rates in CVM dashboard | todo | **NEEDED** | Dashboard integration not verified |
| **RP-5** | Add cron collector | todo | **NEEDED** | Cron collector for rate refresh |
| **RP-6** | Cold review | todo | **STALE** | Code already reviewed during PM sprint |
| **RP-7** | Commit + push + 7d monitor | todo | **PARTIAL** | Committed + pushed, monitor NOT started |
| **EUv2-5** | Update _snapshot_quota | blocked | **STALE** | Ollama quota tracking already works |
| **EUv2-6** | Cold review EUv2 | todo | **STALE** | Superseded |
| **EUv2-7** | Commit + shadow mode | todo | **STALE** | Already committed + shadow running |
| **MAP-0** | Model-aware observability | blocked | **DONE** | Per-model logging in shadow_hook |
| **MAP-1** | External per-model pricing | todo | **DONE** | PM-T5 committed (30d1af1) |
| **MAP-2** | Ollama per-model pricing | todo | **DONE** | PM-T4 committed (000e101) |
| **MAP-3** | z.ai per-model pricing | todo | **DONE** | PM-T4 committed (000e101) |
| **MAP-4** | Per-model CPVO overlay | todo | **NEEDED** | Quality scoring not yet built |
| **MAP-5** | Full integration (remove kill switches) | todo | **NEEDED** | Kill switches still in place (intentional) |
| **T1** | PPQ balance collector | blocked | **NEEDED** | ppq_balance_collector.py exists but API auth issue |
| **T3** | OpenRouter balance collector | blocked | **NEEDED** | balance_collectors.py exists but needs API verification |
| **T7** | 7-day shadow logger | todo | **NEEDED** | Shadow logger exists, formal 7-day run not started |
| **T8** | Enable universal pressure | todo | **DONE** | All phases live in production |
| **P8-ACTIVATE** | Enable pressure production | blocked | **DONE** | Activated today (1b78fe0) |
| **P4.5a-d** | Model mapping + CPVO | todo/blocked | **NEEDED** | Model-aware routing not yet built |
| **S1** | Dispatch gate E2E test | blocked | **NEEDED** | Unrelated to pricing, separate workstream |

## 2. Summary: What's Actually Left

**Already done (close these tasks):** RP-1 through RP-4, EUv2-5 through EUv2-7, MAP-0 through MAP-3, T8, P8-ACTIVATE

**Genuinely needed (5 areas, 9 tasks):**

### Area A: Balance Collectors (2 tasks — unblock credit pressure accuracy)
- T1: PPQ balance — resolve API URL/auth, wire to quota_state
- T3: OpenRouter balance — verify API, wire to quota_state

### Area B: Observability (2 tasks — make rates visible)
- RP-5: Real measured rates in CVM dashboard
- RP-5: Cron collector for periodic rate refresh

### Area C: Quality-Aware Pricing (2 tasks — CPVO)
- MAP-4: Per-model CPVO overlay (quality-weighted pricing)
- P4.5a: Model mapping table (provider, task_type) → model_name

### Area D: Long-term Validation (2 tasks)
- T7: Formal 7-day shadow logger with divergence analysis
- RP-7: 7-day convergence monitor for dynamic rates

### Area E: Cleanup (1 task)
- MAP-5: Remove legacy kill switches + hardcoded rates (after validation confirms new system is stable)

## 3. Blocked Items Resolution

### T1: PPQ balance collector
**Blocker:** API URL/auth uncertain. Code exists in ppq_balance_collector.py (17KB).
**Resolve:** Test the API directly with curl, find the correct endpoint, verify auth method.
**Estimated effort:** 2 hours

### T3: OpenRouter balance collector
**Blocker:** API key may be expired/exhausted.
**Resolve:** Test GET https://openrouter.ai/api/v1/key with current key, check response.
**Estimated effort:** 1 hour

### S1: Dispatch gate E2E
**Blocker:** Separate workstream (not pricing-related).
**Resolve:** Separate from pricing plan, handle independently.

## 4. Recommended Action (Priority Order)

### WEEK 1: Unblock + Validate

**Task 1:** Resolve T1 (PPQ API) + T3 (OpenRouter API) — enables accurate balance tracking
  - Dispatch worker to curl both APIs, find working endpoints, wire to quota_state

**Task 2:** Start T7 (shadow logger 7-day run) — formal divergence analysis
  - Already coded (shadow_logger.py, 31KB). Just needs to run for 7 days with reporting.

**Task 3:** RP-5 (CVM dashboard + cron collector)
  - Show real measured rates on the live dashboard
  - Add cron job to refresh rates every hour

### WEEK 2: Quality Layer

**Task 4:** P4.5a (model mapping table)
  - Map (provider, task_type) → model_name
  - Enables model-aware request routing

**Task 5:** MAP-4 (CPVO per-model quality overlay)
  - Score models by quality (cheap garbage vs expensive good)
  - Apply quality multiplier on top of price

### WEEK 3: Cleanup

**Task 6:** MAP-5 (remove kill switches + legacy code)
  - Only after 7-day shadow confirms stability
  - Remove _DEFAULT_CONVERGED_RATES, remove kill switches, simplify code

## 5. Dependency Chain

```
T1 (PPQ API) ──┐
T3 (OR API)  ──┤
                ├──→ T7 (7-day shadow) ──→ MAP-5 (cleanup)
RP-5 (dashboard)┤
                │
P4.5a (mapping)─┼──→ MAP-4 (CPVO) ──────↗
                │
RP-7 (converge) ─┘
```

T1, T3, RP-5, P4.5a run in parallel.
T7 starts after T1+T3 (needs balance data for accurate shadow comparison).
MAP-4 starts after P4.5a (needs model mapping).
MAP-5 is last (needs T7 + MAP-4 complete).

## 6. Risk Assessment

| Risk | Mitigation |
|---|---|
| PPQ API endpoint wrong | Cold-start seed (0.5) covers gap, pressure still works |
| Shadow reveals unexpected divergence | 7-day window gives time to investigate |
| CPVO quality scores are subjective | Start with binary (good/bad), refine later |
| Removing kill switches too early | Keep until 14 days clean production |

## Total: 9 tasks, ~3 weeks
