# Consolidated Plan v2: Remaining Steps (Consultant-Reviewed)

> **Date:** 2026-08-06
> **Reviewer:** Cold adversarial audit (GLM-5.2 consultant)
> **Status:** READY FOR SCHEDULING
> **Supersedes:** plan-consolidated-remaining.md (v1 — stale audit)

## 0. What Changed from v1

v1 claimed 9 tasks remain. Consultant verified actual code and found **4 were already done**:
- T1 (PPQ balance): API verified live (commit 46a141b), wired to quota_state
- T3 (OpenRouter): collector built (17KB), only needs API key check (5-min ops)
- P4.5a (model mapping): providers.yaml model_map table + model_mapping.py (11KB) built
- MAP-4 (CPVO): cpvo_calculator.py (19KB) built, integrated in live_router.py, 107K rows of telemetry data

Also found **critical operational risk** missed by v1: production proxy running 8-day-old shadow code.

**Real remaining work: 5 tasks, not 9.**

## 1. Day 0 — IMMEDIATE BLOCKERS (before any scheduling)

### BLOCKER-1: Commit + push dirty working tree
- 6 uncommitted files: live_router.py, providers.yaml, real_price_tracker.py, shadow_hook.py, test_proxy_snapshot_quota.py, test_shadow_hook.py
- 1 unpushed commit: cd3cb2b (RP-2 test suite)
- **Action:** Commit dirty files, push all to github/converged-rate-replay
- **Risk of not doing:** Work loss on crash/suspend

### BLOCKER-2: Sync production proxy to repo
- `~/.hermes/bot/src/shadow_hook.py`: 14KB Jul 29 vs repo 22KB Aug 6
- `~/.hermes/bot/src/shadow_logger.py`: 10KB Jul 29 vs repo 32KB Aug 6
- **Action:** Copy repo src/ to ~/.hermes/bot/src/, restart proxy
- **Risk of not doing:** Shadow analysis based on old code; all downstream work contaminated

## 2. Corrected Task List (5 tasks)

### Area A: Observability (2 tasks — parallel, independent)

**A1: RP-5 — CVM Dashboard + Cron Rate Refresh**
- Show real measured rates on live CVM dashboard
- Add cron job to refresh rates hourly
- Files: real_price_tracker.py (exists), dashboard integration needed
- **Effort:** 4 hours
- **Parallelizable:** Yes

**A2: T7 — Shadow Divergence Analysis (data already collected)**
- routing_shadow_decisions table has **279,533 rows** — NOT starting from zero
- Run formal divergence analysis: compare shadow decisions vs actual routing outcomes
- Identify any systematic biases in the pricing optimizer
- **Effort:** 3 hours analysis + report
- **Parallelizable:** Yes (can start Day 1)

### Area B: Quality Calibration (1 task)

**B1: CPVO Threshold Calibration**
- cpvo_calculator.py is BUILT and INTEGRATED (live_router.py line 780)
- 107,886 rows of provider_telemetry data available
- Task: calibrate quality thresholds against real outcomes, validate scores
- Set CPVO cold-start policy for new providers (< 100 samples → base rate)
- **Effort:** 3 hours
- **Depends on:** BLOCKER-2 (need current code in prod)
- **Parallelizable:** Yes with A1, A2

### Area C: Validation + Convergence (1 task)

**C1: RP-7 — 7-Day Convergence Monitor**
- Run 7-day monitoring of dynamic rates vs static fallback rates
- Track Kalman convergence metrics (prediction error, velocity)
- Daily check-in: divergence rate, 429 rate, error rate
- **Effort:** 7 days wall-clock, ~1 hour/day review
- **Depends on:** BLOCKER-1, BLOCKER-2
- **Parallelizable:** Yes (runs in background)

### Area D: Cleanup (1 task — LAST)

**D1: MAP-5 — Remove Kill Switches + Legacy Code**
- Remove _DEFAULT_CONVERGED_RATES, REALTIME_PRICING_ENABLED, PER_MODEL_PRICING_ENABLED
- Remove hardcoded fallback rates from providers.yaml
- **EXIT CRITERIA (must define before starting):**
  - 14 consecutive days clean production (zero pricing-related errors)
  - Shadow divergence rate < 5%
  - Kalman prediction error stable or decreasing
  - 429 rate baseline unchanged
  - CPVO quality scores stable (no oscillation)
- **Effort:** 2 hours code + careful deploy
- **Depends on:** A2, B1, C1 ALL complete + exit criteria met

## 3. Corrected Dependency Chain

```
BLOCKER-1 (commit+push) ──┐
BLOCKER-2 (prod sync) ────┤
                          ├──→ A1 (dashboard)  ──────────────────────┐
                          ├──→ A2 (shadow analysis) ──────────────┐  │
                          ├──→ B1 (CPVO calibration) ──────────┐  │  │
                          └──→ C1 (7-day monitor) ─────────────┤  │  │
                                                             ↓  ↓  ↓
                                                        D1 (cleanup)
                                                    [needs ALL + exit criteria]
```

Everything after blockers is parallelizable. D1 is the only hard-sequential task.

## 4. Risk Assessment (Consultant-Enhanced)

| Risk | Severity | Mitigation |
|---|---|---|
| Production proxy stale code | 🔴 CRITICAL | BLOCKER-2 — sync immediately |
| Uncommitted work loss | 🔴 HIGH | BLOCKER-1 — commit now |
| PER_MODEL_PRICING_ENABLED defaults false in code | 🟡 MEDIUM | External config override; document dependency |
| CPVO cold-start for new providers | 🟡 MEDIUM | < 100 samples → base rate fallback (already coded) |
| OpenRouter key expiry → stale balance data | 🟡 LOW | Add staleness alarm (check balance age > 1h) |
| Shadow DB bloat (279K rows, growing) | 🟡 LOW | Add retention policy (keep 30 days) |
| shadow_mode.enabled: false in config | 🟡 MEDIUM | Flag is off but logger running — configuration drift, fix during prod sync |

## 5. Scheduling

| Phase | Tasks | Wall-clock | Workers |
|---|---|---|---|
| Day 0 | BLOCKER-1 + BLOCKER-2 | 1 hour | Manager (inline) |
| Week 1 | A1 + A2 + B1 (parallel) | 2-3 days | 3 kanban workers |
| Week 1-2 | C1 (background monitor) | 7 days | cron job + daily check |
| Week 3 | D1 (conditional) | 1 day | 1 kanban worker |

## Removed from v1 Plan (already done)
- ~~T1 (PPQ balance collector)~~ — done, API verified
- ~~T3 (OpenRouter balance build)~~ — built, API key check only (fold into BLOCKER-2)
- ~~P4.5a (model mapping table)~~ — done
- ~~MAP-4 (CPVO build)~~ — built + integrated, calibration only (→ B1)
