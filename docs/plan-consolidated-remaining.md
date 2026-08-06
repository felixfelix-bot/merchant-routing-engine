# Consolidated Plan: Remaining Steps (v2 — Consultant-Reviewed)

> **Date:** 2026-08-06
> **Author:** Manager (consultant-reviewed, feedback incorporated)
> **Status:** APPROVED FOR SCHEDULING
> **Consultant verdict:** GO-WITH-CONDITIONS → conditions met below

## 0. Consultant Review Summary

The consultant found that **4 of 9 tasks have substantial code already written** — they are "verify/integrate" tasks, not "implement" tasks. The plan was revised to:

1. Rename "implement" tasks to "verify/integrate" (prevent rewriting working code)
2. Add a Day-1 integration spike as the highest-priority task
3. Add missing tasks (collector merge, cron deployment, MAP-5 revert plan)
4. Split T7 into deploy + 7-day soak + report authoring
5. Re-baseline timeline from 3 weeks → ~2 weeks (2 dev days + 7-day soak)

**Key insight:** The biggest unknown is whether standalone modules are actually wired into `zai_proxy.py`. The integration spike resolves this before anyone clocks hours.

## 1. Audit Results — Board vs Reality

Checked actual code existence and git history against the 30+ task backlog.

| Task | Title | Board Status | REAL Status | Evidence |
|---|---|---|---|---|
| **RP-1** | Build realtime_pricing.py | blocked | **DONE** | File exists (1103 lines), running in prod |
| **RP-2** | Tests for realtime_pricing | todo | **DONE** | 1768 tests pass, includes RP tests |
| **RP-3** | Build real_price_tracker.py | blocked | **DONE** | File exists (1254 lines), running in prod |
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
| **MAP-4** | Per-model CPVO overlay | todo | **CODE EXISTS** | cpvo_calculator.py (463 lines) has model-aware extension — needs wiring verification |
| **MAP-5** | Full integration (remove kill switches) | todo | **NEEDED** | Kill switches still in place (intentional) |
| **T1** | PPQ balance collector | blocked | **CODE EXISTS** | ppq_balance_collector.py (448 lines), docstring claims "verified live 2026-08-05" — needs integration check |
| **T3** | OpenRouter balance collector | blocked | **CODE EXISTS** | openrouter_balance_collector.py (442 lines), docstring claims "verified live 2026-08-05" — needs integration check |
| **T7** | 7-day shadow logger | todo | **CODE EXISTS** | shadow_logger.py (782 lines) with evaluate_exit_criteria() + should_extend_to_7days() — needs deploy + 7-day run + report |
| **T8** | Enable universal pressure | todo | **DONE** | All phases live in production |
| **P8-ACTIVATE** | Enable pressure production | blocked | **DONE** | Activated today (1b78fe0) |
| **P4.5a** | Model mapping + CPVO | todo/blocked | **CODE EXISTS** | model_mapping.py (311 lines) with get_model(), load_model_map() — needs wiring verification |
| **S1** | Dispatch gate E2E test | blocked | **NEEDED** | Unrelated to pricing, separate workstream |

## 2. Revised Task List — 11 Tasks (9 original + 3 added by consultant - 1 merged)

### Area 0: Integration Spike (WEEK 1, DAY 1) — NEW

- **T0: Integration spike** — Confirm which standalone modules are actually called by `zai_proxy.py` and `pricing_engine.py`. This single finding reshapes the entire plan. Check wiring of: ppq_balance_collector, openrouter_balance_collector, model_mapping, cpvo_calculator, shadow_logger.
  - **Effort:** 2h
  - **Blocks:** All other tasks (determines real scope)

### Area A: Balance Collectors (WEEK 1)

- **T1+T3 (MERGED): Verify & deploy both balance collectors** — Verify PPQ + OpenRouter collectors are wired to quota_state. Deploy cron entries. Smoke test both APIs.
  - Docstrings claim "verified live 2026-08-05" — reconcile with plan's "API auth issue" claim
  - Install cron: `python3 -m src.ppq_balance_collector` + `python3 -m src.openrouter_balance_collector`
  - **Effort:** 1-2h (verify + cron deploy)
  - **NEW sub-task:** Merge standalone collectors into `balance_collectors.py` (explicit deferred TODO in both docstrings — "lost-update storm" risk)

### Area B: Observability (WEEK 1)

- **RP-5a: CVM dashboard integration** — Show real measured rates on live dashboard
  - **Effort:** 2-3h (genuine new code)
- **RP-5b: Cron rate refresh** — Periodic rate refresh job
  - **Effort:** 1-2h (genuine new code)

### Area C: Quality-Aware Pricing (WEEK 2)

- **P4.5a (RENAMED): Verify model_mapping integration** — Code complete in model_mapping.py. Verify it's called by pricing_engine + live_router. Verify config/providers.yaml model_map is populated.
  - **Effort:** 1h (verify wiring, not implement)
- **MAP-4 (RENAMED): Verify CPVO wiring + provider_telemetry.model column** — Code complete in cpvo_calculator.py including model-aware extension. Verify: (1) called by optimizer, (2) provider_telemetry table has model column populated.
  - **Effort:** 2h (verify wiring + column check)

### Area D: Long-term Validation (WEEK 1-2, passive)

- **T7a: Deploy shadow logger** — Verify shadow_logger.py is wired into live request path (log_decision() called). Deploy.
  - **Effort:** 1h
- **T7b: 7-day shadow soak** — Passive calendar time. No dev labor.
  - **Duration:** 7 days
- **T7c: Author divergence report** — Call evaluate_exit_criteria(), analyze 7-day data, write report.
  - **Effort:** 3h
- **RP-7: 7-day convergence monitor** — Monitor dynamic rates for convergence. Passive.
  - **Duration:** 7 days (parallel with T7b)

### Area E: Cleanup (WEEK 3 — AFTER validation)

- **MAP-5: Remove legacy kill switches + hardcoded rates** — Only after T7b + RP-7 confirm stability (≥14 days clean production).
  - **MUST include revert plan** — how to re-enable kill switches on incident
  - **Effort:** 3h (2h cleanup + 1h revert plan)

## 3. Blocked Items Resolution

### T1+T3: Balance collectors
**Contradiction:** Plan says "API auth issue" but docstrings say "verified live 2026-08-05."
**Resolve:** Integration spike (T0) will determine truth. If already wired + verified → just deploy cron. If not → fix auth + wire.

### S1: Dispatch gate E2E
**Blocker:** Separate workstream (not pricing-related).
**Resolve:** Separate from pricing plan, handle independently.

## 4. Revised Schedule (Priority Order)

### WEEK 1: Spike + Unblock + Deploy

| Day | Task | Effort | Blocks |
|---|---|---|---|
| **Day 1** | T0: Integration spike | 2h | All others |
| **Day 1-2** | T1+T3: Verify/deploy collectors + cron | 1-2h | T7a |
| **Day 1-2** | P4.5a: Verify model_mapping wiring | 1h | MAP-4 |
| **Day 2** | RP-5a: CVM dashboard | 2-3h | — |
| **Day 2** | RP-5b: Cron rate refresh | 1-2h | — |
| **Day 2** | T7a: Deploy shadow logger | 1h | T7b |
| **Day 2** | Merge collectors → balance_collectors.py | 1h | — |
| **Day 2** | MAP-4: Verify CPVO wiring + model column | 2h | — |
| **Day 3-9** | T7b + RP-7: 7-day passive soak | 0h labor | T7c, MAP-5 |

### WEEK 2: Report + Cleanup

| Day | Task | Effort |
|---|---|---|
| **Day 9** | T7c: Author divergence report | 3h |
| **Day 9** | MAP-5: Remove kill switches + revert plan | 3h |

**Total dev labor: ~15-18h (2-3 dev days).** Plus 7 calendar days for soak.
**End-to-end: ~2 weeks.**

## 5. Dependency Chain (Revised)

```
T0 (integration spike) ──┬──→ T1+T3 (verify/deploy) ──┐
                          ├──→ P4.5a (verify mapping) ──→ MAP-4 (verify CPVO)
                          ├──→ T7a (deploy logger) ─────→ T7b (7d soak) ──→ T7c (report) ──┐
                          │                                                              ├──→ MAP-5 (cleanup)
                          ├──→ RP-5a (dashboard) ────────────────────────────────────────┤
                          └──→ RP-5b (cron refresh) ──────────────────────────────────────┘
                                                                                           ↑
                    RP-7 (7d convergence) ─────────────────────────────────────────────────┘
```

T0 blocks everything (determines real scope).
T1+T3, P4.5a, RP-5a/5b, T7a run in parallel after T0.
T7b starts after T7a (needs logger deployed).
MAP-4 starts after P4.5a (needs model mapping).
T7c starts after T7b (needs 7 days of data).
MAP-5 is last (needs T7c + RP-7 + ≥14 days clean).

## 6. Risk Assessment (Consultant-Enhanced)

| Risk | Severity | Mitigation |
|---|---|---|
| **Modules exist but aren't wired into zai_proxy.py** | CRITICAL | T0 integration spike resolves on Day 1 |
| **Devs rewrite working code** (double-charge) | MEDIUM | All "implement" tasks renamed to "verify/integrate" |
| **Collector merge lost-update storm** | MEDIUM | Explicit merge task in Area A |
| No cron = no data = no balance pressure | MEDIUM | Cron deployment task added |
| PPQ API endpoint wrong | LOW | Docstrings claim verified; spike confirms |
| Shadow reveals unexpected divergence | MEDIUM | 7-day window gives time to investigate |
| CPVO quality scores subjective | LOW | Code already has binary start; refine later |
| Removing kill switches too early | HIGH | Keep until 14 days clean production + revert plan |
| **provider_telemetry.model column missing** | MEDIUM | MAP-4 explicitly checks this |
| **No revert plan for MAP-5** | HIGH | MAP-5 now MUST include revert plan |

## 7. Consultant Conditions Met

1. ✅ Day-1 integration spike added (T0)
2. ✅ "Implement" → "verify/integrate" for T1, T3, P4.5a, MAP-4
3. ✅ Missing tasks added: collector merge, cron deployment
4. ✅ MAP-5 includes revert plan requirement
5. ✅ Timeline re-baselined to ~2 weeks

**Status: APPROVED FOR SCHEDULING**
