# cost_gate — percentile cost gate (CG-1)

Implementation of plan **§5** — `docs/PLAN-cost-gate-reform-v2-2026-08-21.md`
(cost-gate-reform v2).  Felix's Q1 answer: *"run the cron jobs when we are in
the lower 20% of our average cost"* — with the fail-closed posture from Q10.

- **Module:** `src/cost_gate.py` — `evaluate_cost_gate()`, pure, no I/O, no
  global state (same discipline as `src/dispatch_gate.py`)
- **Config:** `config/budget.yaml` — §2.5 backstop defaults ($15/day cap,
  50% WARN, paid-tier list)
- **Tests:** `tests/test_cost_gate.py` — 79 tests covering every §5 matrix
  row + §2.2–§2.5 mechanics + §3 override semantics

## Decision semantics (Q2)

| Verdict | Meaning |
|---|---|
| `ALLOW` | Cheap band, or non-deferrable work under the backstop.  **Snapshot at dispatch: covers the job's entire run** (§2.3 job-burst stickiness) — callers must not re-evaluate mid-job. |
| `DEFER` | Deferrable work outside the band: skip & reschedule.  **No model downgrade, no auto-substitution.** |
| `DENY` | Hard fail-closed row of the §5 matrix. |

## §5 fail-closed matrix (evaluation order)

| # | Condition | Verdict | reason_code | Override escape |
|---|---|---|---|---|
| 1 | Freeze marker present | DENY | `freeze_marker` | **never** (immune to all scopes) |
| 2 | Dead/locked z.ai key (z.ai path) | DENY | `dead_or_locked_key` | **never** (immune to all scopes) |
| 3 | Price endpoint unreachable / stale >15 min | DENY (+`reason_json.loud=true`, caller logs loudly) | `infra_down` | scope `infra_down` (Q10 escape) |
| 4 | Effective price unknown | DENY | `price_unknown` | none |
| 5 | Budget config missing/unparsable | DENY | `budget_unconfigured` | scope `budget` |
| 6 | Paid-tier backstop exceeded (spend ≥ cap) | DENY, **paid tiers only** — subscription routes freed | `backstop_exceeded` | scope `budget` |
| 7 | <48 h price history, deferrable | DEFER | `price_history_insufficient` | scope `price_history` |
| 8 | Outside p20 band, deferrable | DEFER | `price_outside_band` | none (deferral isn't denial) |

Precedence note: rows 5/6 are evaluated **before** row 7 so a missing budget
config surfaces as DENY instead of rescheduling into the same broken state
forever (fail-closed, Q10).  WARN at 50% of cap is reported in
`backstop.warn` without changing the verdict.

## Gate mechanics

- **p20 threshold (§2.2):** ALLOW deferrable work iff the cheapest ELIGIBLE
  provider's effective price (CG-2 forecast variant) is ≤ the 20th
  percentile of the trailing-7-day hourly medians (linear interpolation).
- **Hysteresis (§2.3):** enter CHEAP at `price ≤ p20`; exit CHEAP only at
  `price > p20 × 1.20`; minimum 30-min dwell between flips.  The returned
  `hysteresis` block `{cheap, last_flip_ts, dwell_remaining_s}` must be
  passed back as `hysteresis_state` on the next evaluation.
- **Cold start (§2.4):** <48 hourly samples → deferrable crons DEFER
  (`price_history_insufficient`); interactive/urgent work is NEVER deferred
  on price-history grounds — the backstop is its only gate.
- **Backstop (§2.5):** $15/day fleet-wide cap on PAID tiers from
  `config/budget.yaml` (shared with CG-6).  Subscription routes (z.ai keys,
  ollama_cloud included quota) are never blocked by it.

## Overrides (§3)

Marker `~/.hermes/bot/.cost_gate_override` (CG-4), JSON
`{scope, expires_ts, issued_by, reason}` — TTL mandatory, single scope per
grant.  Scopes: `budget`, `price_history`, `infra_down`, `paid_ceiling`
(`paid_ceiling` belongs to CG-6; this module never consumes it).

- An override is **consumed only when it actually rescues a block**; the
  verdict carries `override_consumed = {scope, issued_by, reason,
  expires_ts, consumed_at_ts, would_have_been}` — CG-4 persists that as a
  `cost_gate_overrides` audit row.
- **Scope isolation:** an override lifts only its own row (tests pin all
  cross-scope attempts).
- **Immunity:** no scope, ever, bypasses the freeze marker or the
  dead/locked-key hard block.

## Composition with dispatch_gate

`TASK_PROFILES`, `HARDWARE_SAFETY_MARGIN`, `MIN_EFFECTIVE_PRICE` and
`resolve_task_profile` are imported from `src/dispatch_gate` — the *same
objects*, never copies (asserted by identity in tests).  `task_type`
resolves the model and `budget_mult`; the hardware safety margin scales the
informational `required_headroom_usd` exactly as the dispatch gate scales
quota headroom.  Enforcement follows the plan exactly (backstop DENY only at
spend ≥ cap); `required_headroom_usd` is visibility for CG-9 reporting.

## reason_code reference

`freeze_marker`, `dead_or_locked_key`, `infra_down`, `price_unknown`,
`budget_unconfigured`, `backstop_exceeded`, `price_history_insufficient`,
`price_outside_band` (DEFER), `within_p20_band`,
`within_exit_band_hysteresis`, `not_deferrable_backstop_only` (ALLOW), and
override-rescue codes `infra_down_override`, `budget_unconfigured_override`,
`backstop_override`, `price_history_override`.  When an override rescue
produced an ALLOW, the rescue code is the `reason_code` (the band it ran in
is recorded in `reason_json.band`).

## CG-3 — token predictor (upstream of the gate's cost preview)

- **Module:** `src/token_predictor.py` — `predict_tokens()` pure;
  `compute_model_stats` / `seed_token_stats` / `load_token_stats` are the
  I/O helpers (read-only `mode=ro` source connections — the production DB
  is never written).
- **Script:** `scripts/seed_token_stats.py` — one-shot seed,
  **seed-then-replace**: a re-seed fully swaps the stats table in one
  transaction (drift-friendly — models missing from the new window
  disappear); a source with zero usable rows raises and leaves the old
  stats intact (fail-closed against wiping stats on a broken read).
- **Stats store:** `data/token_stats.db` (gitignored via `*.db`),
  aggregated from `~/.hermes/bot/zai_usage.db` `api_calls`
  (`status_code=200`, `total_tokens>0`, trailing 30 d).
- **Prediction:** per-model p50/p90 (same linear-interpolation
  `percentile` as the gate — imported, not duplicated) × the task
  dimension via `TASK_PROFILES.budget_mult` (A3: `task_type` is all-NULL
  in history, so the `(model, task_type)` join key can't be seeded yet;
  CG-5 wiring replaces this).  Percentiles use
  `src.cost_gate.percentile` by import.
- **Confidence:** `n<30 → low` (plan-pinned), `30–199 → medium`,
  `≥200 → high`; stats older than 7 d are `stale` → forced low.
- **Cold model:** always answers — worst observed per-model p90 × 1.5, or
  `DEFAULT_COLD_TOKENS=163128` (calibrated 2026-08-22: worst per-model
  p90 = 108 752, fleet pooled p90 = 102 303) when no stats exist at all.
  The gate fails closed on *price*, never on token history.
- **Kalman inputs: OFF.** `KALMAN_INPUTS_ENABLED=False` until
  `kalman_health.py --short` reports healthy (verified ✗ unhealthy
  2026-08-22, re-verified at implementation time); the module imports
  nothing from the kalman family (pinned by tests).
- **Gate feed:** `gate_estimated_tokens()` returns the **RAW** per-model
  p90 — `evaluate_cost_gate` applies `budget_mult` itself, so scaling
  happens exactly once (pinned by
  `TestCostGateIntegration.test_gate_feed_applies_budget_mult_exactly_once`).
- **Tests:** `tests/test_token_predictor.py` — 33 tests (percentile
  correctness vs an independent reference, status/window/zero filters,
  seed-then-replace drift + fail-closed, confidence buckets, staleness,
  cold model, task scaling, kalman guard, gate integration).

## CG-13 — cost outlier detection (adaptive EWMA + Kalman composition)

- **Module:** `src/cost_outlier_detector.py` —
  `detect_cost_outliers()` (I/O entry point), pure core
  `compute_ewma`, `compute_baseline`, `detect_outlier`,
  `compute_expected_cost`, `classify_discrepancy`,
  `build_provider_breakdown`.
- **Script integration:** `~/.hermes/profiles/manager/scripts/cost-escalation-check.py`
  — `detect_cost_outliers()` replaces the inline `ewma_outlier_check` as the
  primary outlier detector. The inline function remains as a fallback when
  the module import fails (graceful degradation, same pattern as CG-3's
  kalman guard).
- **Tests:** `tests/test_cost_outlier_detector.py` — 30 tests (EWMA
  convergence, baseline stats, outlier detection + cold-start fallback,
  Kalman composition classification, provider breakdown, integration
  with DB fixtures, cold-start state persistence).

### Design

**Two signals composed:**

1. **EWMA outlier** — the current hour's paid $ spend is compared against
   `threshold = max(3 × EWMA, mean + 2 × std)` over the 7-day baseline
   (168 hourly samples). When `actual > threshold` → flagged as an outlier
   with a ratio (`"16x normal"`).

2. **Kalman composition** — the existing Kalman filter (`kalman_samples`
   table) predicts the token burn rate (`burn_rate_tph`). Composing with
   the effective price gives the *expected* $/h:
   `expected = burn_rate_tph × effective_price / 1M`.
   Comparing `expected` vs `actual` reveals the *cause*:
   - `actual >> expected` → **routing inefficiency** (expensive provider
     picked when cheaper exists, or free quota was available but proxy
     failed over to paid).
   - `actual ≈ expected` but both high → **quota exhaustion** (structural:
     free quota depleted, paid failover is the only option).

**Cold start:** when fewer than `MIN_BASELINE_HOURS` (24) of history exist,
the EWMA and baseline stats are unreliable. The module falls back to the
fixed `COLD_START_THRESHOLD` ($2/h, matching the previous static threshold)
until enough data accumulates. State persists in
`~/.hermes/bot/escalation_ewma_state.json` (samples, EWMA, last-sample hour,
last-alert timestamp with 2h re-arm guard).

**Adaptive thresholds:** the EWMA adapts to baseline shifts — e.g. when a
cheaper provider goes live, the normal $/h drops, and the old fixed $2/h
threshold becomes stale; the EWMA tracks the new normal automatically.

### Verified data (2026-08-22)

- Historical baseline: 97h of paid spend data, mean $0.48/h, std $1.26/h.
- EWMA catches today's outliers: 10:00 $4.84/h (7.2x), 17:00 $10.61/h (16.1x).
- Existing Kalman: 151K tok/h burn rate, uncertainty 46K tokens — tracks
  QUOTA not $.
- Composition: 151K tok/h × $0.47/M = ~$0.07/h predicted vs $10.61/h actual
  = 150x discrepancy. The discrepancy IS the signal: Kalman says "quota
  burning normally" but $ spend says "paid failover active".

### Alert taxonomy

| alert_type | condition | message shape |
|---|---|---|
| `spend_outlier` | `actual > max(3×EWMA, mean+2×std)` | `💸 SPEND OUTLIER: $X/h, Yx normal (EWMA $Z/h)` |
| `kalman_composition` | `actual/expected > 10` | `🔍 KALMAN COMPOSITION: Routing Inefficiency — actual $X/h vs expected $Y/h (Zx discrepancy; Kalman N tok/h × $P/M)` |
| `cold_start_bleed` | `actual > COLD_START_THRESHOLD` and `< MIN_BASELINE_HOURS` | `💸 PAID BLEED (cold start): $X/h (cold-start threshold $2/h)` |

### Constants

| name | value | rationale |
|---|---|---|
| `EWMA_ALPHA` | 0.3 | ~5h effective memory (1/alpha ≈ 3.3h half-life) |
| `COLD_START_THRESHOLD` | 2.0 | matches previous static threshold, seamless transition |
| `MIN_BASELINE_HOURS` | 24 | need >1 day of data for mean+std to be reliable |
| `LOOKBACK_HOURS` | 168 | 7-day trailing window for baseline |
| `DEFAULT_PAID_PRICE_PER_M` | 0.47 | OpenRouter measured 2026-08-22 |
| `EWMA_REARM_SECS` | 7200 | 2h re-arm guard prevents alert flooding |

## Wiring status & rollback

CG-1 is the pure decision core only — **nothing in production calls it
yet.**  Wiring happens in CG-7 (CLI wrapper + exit codes) after CG-2
(forecast pricing) and CG-6 (paid-tier guard / spend reader) land.  CG-3
(`src/token_predictor.py`, `scripts/seed_token_stats.py`,
`tests/test_token_predictor.py`, and its doc section) is likewise inert
until CG-7 wires it in.  Rollback = delete `src/cost_gate.py`,
`config/budget.yaml`, `tests/test_cost_gate.py`, the CG-3 files above, and
this file; no production behavior changes.

*2026-08-21 — CG-1, plan v2 §5/§6.*  
*2026-08-22 — CG-3 token predictor, plan v2 §6.*
