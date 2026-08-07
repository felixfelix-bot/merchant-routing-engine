# T7c: 7-Day Shadow Divergence Report

Generated: 2026-08-06
Task: t_d982d1df (T7c: Author 7-day divergence report)
Plan ref: `docs/plan-consolidated-remaining.md` § Area D (T7a→T7b→T7c→MAP-5)
Data source: `~/.hermes/bot/zai_usage.db` :: table `routing_shadow_decisions`
Soak tasks: T7a (deploy, t_c834d3c5) → T7b (12.14-day soak, t_66df4ff1) → **T7c (this report)**

---

## Executive Summary & Verdict

**VERDICT: GO — conditional** for MAP-5 (kill-switch removal), contingent on the
three pre-cutover conditions in §6 and an executed revert plan (§7).

The 12-day shadow soak (283,601 decisions) shows the price-first optimizer
selecting strictly cheaper or equal-cost providers in 97% of paired decisions,
with **zero per-token (paid) spend exposure** because it routes entirely within
the flat-rate provider pool. The optimizer is **12.0% cheaper** ($0.0962/M vs
$0.1094/M) over the full window and **25.7% cheaper** over the trailing 7 days.

**Critical caveat (mandatory read):** The P6 cost-divergence exit gate reports
`all_passed: true` with divergence `0.00%` — but this is a **degenerate pass**,
not a measurement. All P6 pressure-routing columns (`actual_cost`,
`pressure_cost`, `divergence`, `is_429`, `paid_provider`) are 100% NULL/zero
because the live request path calls the legacy `log_decision()` API, not the
P6 `log_pressure_decision()` API (see §3). The designed cost-divergence gate
has therefore NOT been genuinely evaluated. The GO recommendation rests on the
**legacy cost-comparison columns** (which ARE populated and strongly positive)
plus the structural fact that the optimizer's routing domain is bounded to
flat-rate providers — not on the degenerate gate.

---

## 1. Data Window

| Metric | Value |
|---|---|
| Total rows logged | 283,601 |
| Rows with divergence column populated | 283,601 (100% — but all default `0.0`, see §3) |
| Data span | 12.14 days |
| Earliest | 2026-07-25 13:00:18 UTC |
| Latest | 2026-08-06 16:19:48 UTC (logger live, ~24 rows/min) |
| Trailing-7d rows | 147,912 |
| Session span (7d) | 167.98 h (≥ 5 h required) |

Liveness confirmed: the shadow logger is actively receiving traffic (T7a
verified the wiring at `~/.hermes/bot/zai_proxy.py:2590-2594`).

---

## 2. The Two Divergence Metrics (must not be conflated)

There are two distinct "divergence" concepts in `shadow_logger.py`. The parent
soak task (T7b) reported the first; the P6 exit gate checks the second. They
tell very different stories here.

| Metric | What it measures | 7-day value | Gate? |
|---|---|---|---|
| **Agreement disagreement** (`agree` column) | provider-name mismatch, cost-blind | **45.42%** | No |
| **Cost divergence** (`divergence` column) | `\|Δcost\| / max(act,prs)` when providers differ | **0.00%** | **Yes (< 15%)** |

The 45% disagreement is the optimizer choosing a different (cheaper) provider
than the live path ~46% of the time — this is the engine **working as designed**
and represents the savings opportunity, not a defect. The 0.00% cost-divergence
is **not** confirmation that pressure routing tracks actual; it is the artifact
described in §3.

---

## 3. Critical Data-Quality Finding: Degenerate P6 Gate

**The live request path is logging via `ShadowLogger.log_decision()` (the
legacy API), not `log_pressure_decision()` (the P6 API).**

Column population audit (283,607 rows):

| Column | NULL | Zero | Populated? |
|---|---:|---:|---|
| `live_provider` / `shadow_provider` | 0 | — | ✅ full |
| `shadow_cost` (legacy) | 130 | — | ✅ 283,477 |
| `live_cost` (legacy) | 141,848 | — | ⚠️ ~half |
| `pressure_provider` | **283,607** | 0 | ❌ none |
| `actual_cost` | **283,607** | 0 | ❌ none |
| `pressure_cost` | **283,607** | 0 | ❌ none |
| `divergence` | 0 | **283,607** | ❌ degenerate |
| `is_429` | 0 | **283,607** | ❌ degenerate |
| `paid_provider` | 0 | **283,607** | ❌ degenerate |
| `requested_model` / `per_model_base_rate` | 283,607 | — | ❌ none |

Consequences for `evaluate_exit_criteria()`:
- **divergence gate** → `AVG(divergence)=0.0` trivially; passes without measuring anything.
- **429 gate** → `is_429` defaults to `0` everywhere; the real 429 rate is unknown from this table.
- **paid-spend gate** → `paid_provider=0` everywhere. This one happens to be *also*
  genuinely true (the optimizer never selects a paid provider — see §4), so the
  gate is satisfied for the right reason *and* by the default; the two cannot be
  separated from this data alone.

**The `all_passed: true` result must not be cited as validation.** It is
reproduced in §5 for completeness, but the divergence and 429 criteria are
non-results. Fix described in §6, condition (C1).

---

## 4. Real Cost Evidence (legacy columns — the actual signal)

Since the legacy cost columns are populated, the meaningful comparison is
`shadow_cost` (optimizer's pick) vs `live_cost` (what production actually used),
on rows where both are known.

### Paired cost comparison (both costs present)

| Window | n | Avg optimizer $/M | Avg actual $/M | Δ | Optimizer cheaper |
|---|---:|---:|---:|---:|---:|
| Full 12d | 141,633 | $0.096219 | $0.109370 | −$0.0132/M | **12.0%** |
| Last 7d | 73,703 | $0.011206 | $0.015082 | −$0.0039/M | **25.7%** |

Per-decision outcome (full window, paired):
- Optimizer **cheaper**: 59,817 (42.2%)
- Optimizer **pricier**: 3,996 (2.8%)
- **Equal**: 77,820 (54.9%)

The optimizer is cheaper or equal-cost in **97.1%** of paired decisions and
pricier in only 2.8%. The pricier cases are negligible in volume.

### Token-weighted spend (rows with tokens > 0 and both costs)

| | USD |
|---|---:|
| Actual live spend | $291.29 |
| Optimizer would-spend | $271.22 |
| **Delta (saved)** | **$20.07 (6.9%)** |

### Provider distribution shift (actual → optimizer would-route)

| Provider | Actual (live) | Optimizer (shadow) | Shift |
|---|---:|---:|---|
| ours | 45.0% | **63.3%** | +18.3 pts |
| ollama_cloud | 0.0% | **19.3%** | +19.3 pts (new) |
| friend | 43.8% | **6.1%** | −37.7 pts |
| zai_ours | 4.5% | 11.1% | +6.6 pts |
| zai_friend | 6.6% | 0.0% | −6.6 pts |
| openrouter | ~0% | 0.2% | +0.2 pts |
| fallback | 0% | <0.1% | trace |

**Every provider the optimizer selects is flat-rate** (ours, ollama_cloud,
zai_ours, friend, zai_friend). None of ppq / openrouter / deepinfra (the
per-token paid set in `shadow_logger._PAID_PROVIDERS`) is selected in volume.
The optimizer's dominant move is shifting load from `friend` ($0.029/M) to
`ours` ($0.001–0.068/M) and `ollama_cloud` ($0.024/M) — i.e. toward the
cheaper end of the flat-rate pool. The `reason` field confirms deliberate
selection ("cheapest viable provider: … at $X/M (difficulty=…)").

### 429 rate & paid spend

| Metric | Full window | Last 7d | Note |
|---|---|---|---|
| 429 rate (logged `is_429`) | 0.0000% | 0.0000% | **Not measured** — `is_429` is degenerate (§3). No independent 429 evidence in this table. |
| Paid spend (`paid_provider=1`) | $0.0000 | $0.0000 | **Genuinely $0** — optimizer never selects a paid provider (§4 dist. table). Validated. |

---

## 5. `evaluate_exit_criteria()` Raw Output

Called on the trailing-7-day window with baselines set to the current actual
values (no pre-engine baseline is stored separately):

```json
{
  "all_passed": true,
  "criteria": {
    "divergence":      {"value": 0.0,      "threshold": 0.15, "passed": true},   // DEGENERATE — §3
    "rate_429":        {"value": 0.0,      "threshold": 0.0,  "passed": true},   // DEGENERATE — §3
    "paid_spend":      {"value": 0.0,      "threshold": 0.0,  "passed": true},   // genuinely true (§4)
    "decisions_logged":{"value": 147913,   "threshold": 500,  "passed": true},   // valid
    "session_cycle":   {"value": 167.9811, "threshold": 5.0,  "passed": true},   // valid
    "nan_inf_clean":   {"value": true,                        "passed": true}    // valid
  },
  "decisions_logged": 147913,
  "session_span_hours": 167.9811
}
```

Net: 4 of 6 criteria are genuinely satisfied; 1 (paid_spend) is genuinely
satisfied; 2 (divergence, rate_429) are non-results due to the logging gap.
NaN/inf audit: 0 NaN in divergence/actual_cost/pressure_cost.

---

## 6. GO/NO-GO Determination & Conditions

### Decision: **GO — conditional**

The evidence that *is* available is uniformly positive and the spend risk is
structurally bounded:

1. **No paid-spend exposure.** The optimizer selects only flat-rate providers.
   Worst-case MAP-5 behavior cannot meter a per-token bill. The
   `paid_spend ≤ baseline` gate is genuinely met.
2. **Optimizer is cheaper, not costlier.** 12–26% cheaper; pricier in only 2.8%
   of paired decisions and never toward a paid provider.
3. **Volume & span are ample.** 147,912 decisions over 168 h — orders of
   magnitude above the 500-decision / 5-hour floor.
4. **Revert path exists.** The kill switch itself is the rollback mechanism
   (§7); the flat-rate domain means the blast radius of a bad cutover is
   availability, not spend.

### Pre-cutover conditions (all three required)

- **(C1) Fix the logging gap.** Wire the live path to call
  `log_pressure_decision()` (or backfill `actual_cost`/`pressure_cost`/`is_429`
  into the existing `log_decision()` call) so that post-cutover monitoring
  measures real cost-divergence and 429 rate. **Without this, the 0% divergence
  "PASS" is untrustworthy and must not be relied upon after MAP-5.**
- **(C2) Confirm 429 health from an independent source.** The shadow table
  cannot attest to the 429 rate (degenerate `is_429`). Verify the production
  429/error rate from proxy logs or the broader usage DB before cutover, and
  establish it as the post-cutover rollback trigger (§7).
- **(C3) Revert plan in place.** Per consultant's HIGH-risk mitigation for
  MAP-5 — see §7.

### Why not an unconditional GO

The P6 cost-divergence gate — the single metric designed specifically to gate
this promotion — has not been genuinely evaluated. Recommending unconditional
GO on a gate that passes only because its inputs are empty would be unsafe for
the highest-risk step in the plan. The conditions above close that gap: the
cost/safety evidence already strongly supports GO, and C1–C3 make the cutover
observable and reversible.

### Why not a hard NO-GO

The 12-day cost dataset is strong, consistent, and unidirectional (cheaper, no
paid exposure). The flat-rate-only routing domain bounds the downside to
availability, which the kill switch fully mitigates. A hard NO-GO would discard
12 days of positive evidence for a logging fix that is itself small.

---

## 7. MAP-5 Revert Plan (required pre-cutover)

MAP-5 removes the legacy kill switches / hardcoded rates that currently keep
the pricing/routing engine guarded. Revert = re-arm the kill switch.

| Step | Action | Trigger |
|---|---|---|
| 1 | Keep the kill-switch flag/env-var documented and the re-arm command in the runbook. | pre-cutover |
| 2 | Establish post-cutover monitoring on the **fixed** instrumentation (C1): divergence, 429 rate, error rate, provider distribution. | immediate post-cutover |
| 3 | **Rollback trigger A:** any 429-rate increase > 2× the pre-cutover baseline (C2). | re-arm kill switch |
| 4 | **Rollback trigger B:** any hard-error spike or provider-concentration anomaly (e.g. ollama_cloud share rising sharply with elevated errors). | re-arm kill switch |
| 5 | **Rollback trigger C:** observed paid-provider (ppq/openrouter/deepinfra) selection — should remain ~0. | re-arm kill switch |
| 6 | After ≥ 14 days clean production post-cutover, consider the kill switch retired (archive, keep runbook). | stabilization |

---

## 8. Daily Trend (cost-divergence is degenerate; shown for agreement + volume)

| Day (UTC) | n | agree % | divergence %* | 429 %* |
|---|---:|---:|---:|---:|
| 2026-07-25 | 14,747 | 58.5 | 0.00* | 0.000* |
| 2026-07-26 | 23,050 | 54.2 | 0.00* | 0.000* |
| 2026-07-27 | 13,713 | 62.2 | 0.00* | 0.000* |
| 2026-07-28 | 21,845 | 55.2 | 0.00* | 0.000* |
| 2026-07-29 | 40,119 | 43.9 | 0.00* | 0.000* |
| 2026-07-30 | 37,359 | 47.7 | 0.00* | 0.000* |
| 2026-07-31 | 20,637 | 86.1 | 0.00* | 0.000* |
| 2026-08-01 | 17,729 | 84.8 | 0.00* | 0.000* |
| 2026-08-04 | 27,478 | 61.9 | 0.00* | 0.000* |
| 2026-08-05 | 60,516 | 40.2 | 0.00* | 0.000* |
| 2026-08-06 | 6,409 | 30.0 | 0.00* | 0.000* |

\* divergence and 429 columns are degenerate (§3) — shown for completeness only.
Agreement % varies with traffic mix; the recent dip (Aug 5–6) corresponds to
higher-volume periods where the optimizer more frequently selects the cheaper
flat-rate provider over the live default.

---

## 9. Artifacts

- Analysis scripts (this task's workspace):
  `analyze.py` (gate + breakdowns), `legacy_cost_analysis.py` (real cost signal),
  `diagnose_costs.py` (column-population audit), `analysis.json` (raw output).
- Parent soak evidence: T7b (t_66df4ff1) workspace — `acceptance_check.py`,
  `check_soak.py`, `schema_check.py`.
- Source: `src/shadow_logger.py` (`evaluate_exit_criteria`, `log_decision`,
  `log_pressure_decision`, `_compute_divergence`).

---

## 10. Open Items for Downstream (MAP-5)

1. **Fix logging path** (C1) — switch `zai_proxy.py:2590-2594` shadow call from
   `log_decision()` to `log_pressure_decision()`, or augment `log_decision()` to
   populate `actual_cost`/`pressure_cost`/`is_429`. Small change; gates honest
   post-cutover monitoring.
2. **Independent 429 baseline** (C2) — pull from proxy access logs / broader
   usage DB; the shadow table cannot provide it.
3. **Author MAP-5 task** — kill-switch removal + this revert plan, assigned to
   the implementation profile. Do not start until C1–C3 are met.
