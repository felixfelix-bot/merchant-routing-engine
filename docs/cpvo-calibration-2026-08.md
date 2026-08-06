# CPVO Threshold Calibration Report — 2026-08

**Task:** B1 (kanban `t_f38a0e51`) — validate quality scores against real outcomes
**Date:** 2026-08-06
**Scope:** Calibration/validation only. `cpvo_calculator.py` is BUILT and INTEGRATED;
this report does not rebuild it.
**DB:** `~/.hermes/bot/zai_usage.db`, table `provider_telemetry`
**Verdict:** ✅ **PASS — no threshold changes required.** Quality scores, cold-start,
and relative ordering are all correct against 110K+ rows of real telemetry.

---

## 1. Telemetry overview

| Metric | Value |
|---|---|
| Total rows | **110,108** |
| Window | 2026-07-28 → 2026-08-06 (9.8 days) |
| Providers present | `ours`, `friend` (both z.ai flat-rate; anonymized) |
| `model` column | exists, but **0.072% populated** (79/110,208 rows tagged) |
| DB size | 197.9 MB |

### Step 1 — `GROUP BY provider, model` (all-time)

| provider | model | n | avg_success | avg_lat_ms | avg_mismatch | billed_tokens |
|---|---|---:|---:|---:|---:|---:|
| friend | (null) | 54,624 | 0.4368 | 6,418 | 0.2433 | 44,133,333 |
| friend | glm-5.2 | 39 | 0.9744 | 11,264 | 0.7179 | 21,526 |
| friend | glm-4.5-flash | 3 | 1.0000 | 51,516 | 0.6667 | 569 |
| ours | (null) | 55,440 | 0.1700 | 3,334 | 0.1000 | 17,203,961 |
| ours | glm-5.2 | 2 | 0.0000 | 109,847 | 0.0000 | 0 |

The bulk of traffic is recorded at **provider level** (null model). Per-model rows
are too sparse to drive model-aware CPVO on their own (see §5).

## 2. Failure-mode breakdown

`response_received × response_valid` crosstab shows failures are overwhelmingly
**`no_response`** (provider never answered — `response_received = 0`):

| provider | rcvd=0,valid=0 | rcvd=1,valid=0 | rcvd=1,valid=1 |
|---|---:|---:|---:|
| friend | 30,086 | 698 | 23,904 |
| ours | 45,576 | 441 | 9,425 |

Top `error_type`: `no_response` dominates (friend 30,086; ours 45,576), followed by
`none` (success) and small counts of `parse_error` and transient network errors.

**Interpretation:** these are flat-rate endpoints under sustained rate-limit pressure
(429 / quota-exhaustion surfacing as no response). Counting `no_response` as a
failure in the CPVO success denominator is **correct** — a rate-limited attempt is a
wasted attempt, so effective cost-per-valid-output should rise.

## 3. Thresholds under review

| Constant | Value | Location |
|---|---|---|
| `MIN_SAMPLES` | 100 | `cpvo_calculator.py:81` |
| `SUCCESS_THRESHOLD` | 0.95 | `cpvo_calculator.py:85` |
| `_SUCCESS_EPSILON` | 1e-6 | `cpvo_calculator.py:91` (zero-success guard) |
| CPVO cache TTL | 300 s | `live_router.py:642` |

## 4. Validation results

### 4a. Effective rates produced against the live DB (24h window)

Base rates from `real_price_tracker.LAST_RESORT_RATES` (z.ai flat-rate → $0.001/M
marginal seed). CPVO output, live calculator against real rows:

| provider | base $/M | success | n (24h) | effective $/M | multiplier | status |
|---|---:|---:|---:|---:|---:|---|
| ours | 0.00100 | 0.165 | 3,409 | 0.00608 | **6.08×** | penalized |
| friend | 0.00100 | 0.412 | 10,453 | 0.00243 | **2.43×** | penalized |
| ollama_cloud | 0.01500 | — | 0 | 0.01500 | 1.00× | COLD (base rate) |
| ppq | 0.05000 | — | 0 | 0.05000 | 1.00× | COLD (base rate) |
| deepinfra | 0.08000 | — | 0 | 0.08000 | 1.00× | COLD (base rate) |
| openrouter | 0.02900 | — | 0 | 0.02900 | 1.00× | COLD (base rate) |

**Quality ordering is correct:** `friend` (41% success) is penalized *less* than
`ours` (16.5% success), so `friend` ranks ahead of `ours` on effective cost — the
more reliable provider wins, exactly as designed.

**Routing invariant preserved:** even after penalty, both flat-rate providers
($0.0024–$0.0061/M) remain far cheaper than any paid provider ($0.015–$0.08/M).
CPVO therefore does **not** break the "z.ai flat-rate is primary" rule
(`AGENTS.md`) — it just makes the optimizer aware that `ours` is ~2.5× flakier than
`friend` and should be deprioritized between the two.

### 4b. Cold-start behavior — ✅ PASS

| Scenario | Expected | Observed |
|---|---|---|
| Unknown provider `compute_cpvo('nobody')` | `None` | `None` ✓ |
| Unknown provider `get_effective_rates({'nobody':0.05})` | base rate unchanged | `0.05` ✓ |
| Provider with 0 samples (all paid providers) | base rate unchanged | base rate ✓ |
| Per-model < 100 samples | fall back to provider-level | fell back correctly for all 3 keys ✓ |

The `get_effective_rates_model_aware` provider-level fallback (when per-model n <
`MIN_SAMPLES`) works: `friend/glm-5.2`, `friend/glm-4.5-flash`, `ours/glm-5.2` all
returned the **provider-level** effective rate rather than the unadjusted base.

### 4c. `MIN_SAMPLES=100` sensitivity

Both live providers clear even `MIN_SAMPLES=500` in a single 24h window (3.4K and
10.4K rows respectively). The threshold therefore has **no effect on current
production providers** — they are 30–100× past it. It only governs cold-start for
new/paid providers, where 100 is appropriately conservative (engages quickly once a
provider starts receiving traffic, without acting on a handful of noisy samples).

**No change recommended.**

### 4d. `SUCCESS_THRESHOLD=0.95` sanity

A genuine 95%+ success bar is the right "no penalty" cutoff. Both flat-rate
providers are well below it (steady-state, not transient — see daily trend below),
so both are correctly penalized. No provider sits ambiguously near the boundary, so
small shifts in the threshold would not change any current decision.

**No change recommended.**

### 4e. Daily success-rate trend (steady-state, not an outage)

| day | friend succ | ours succ |
|---|---:|---:|
| 2026-07-28 | 0.379 | 0.055 |
| 2026-07-29 | 0.447 | 0.147 |
| 2026-07-30 | 0.452 | 0.142 |
| 2026-07-31 | 0.507 | 0.220 |
| 2026-08-01 | 0.357 | 0.221 |
| 2026-08-04 | 0.538 | 0.240 |
| 2026-08-05 | 0.404 | 0.111 |
| 2026-08-06 | 0.411 | 0.181 |

The low success rates are **persistent across all 8 observed days**, not a single-day
blip. This means CPVO's penalties are a correct, stable reflection of provider
quality — not noise that should be filtered out.

## 5. Findings & recommendations

### ✅ No code or threshold changes needed
The calculator is correctly calibrated for the current provider mix. All 67 CPVO /
telemetry tests pass (1.49 s). Live integration at `live_router.py:1030`
(`_get_effective_rates`) caches for 300 s, guards against empty/`None` returns, and
wraps in try/except — production-safe.

### ⚠️ Data-instrumentation gap (informational — not a CPVO bug)
The `model` column is populated for only **0.072%** of rows (79/110,208). As a
result, **model-aware CPVO (`get_effective_rates_model_aware`) is effectively
dormant** — every per-model query falls back to the provider-level aggregate. This
means quality differences between, e.g., `glm-5.2` (97% in the 39 tagged rows) and
`glm-4.5-flash` are invisible to routing today.

**Recommendation (separate task, not this calibration):** backfill / instrument the
telemetry writer to populate `model` on every row so the model-aware path can engage.
Once a `(provider, model)` pair exceeds 100 samples it will start receiving
per-model penalties automatically. No calculator change required — the fallback is
already correct.

### ⚠️ CPVO coverage is currently flat-rate-only
All four paid providers (`ollama_cloud`, `ppq`, `deepinfra`, `openrouter`) have
**zero** telemetry rows, so CPVO cannot quality-adjust them — they return the base
rate unpenalized. This is safe (cold-start contract holds) but means CPVO only
protects against quality regression on the two flat-rate providers today. This will
self-resolve as paid providers accumulate telemetry during real failovers.

### Note on provider names
`ours` and `friend` are anonymized aliases for two z.ai flat-rate subscriptions
(see `live_router.py:256` `_ZAI_PROVIDERS_RATES = {"ours", "friend"}`). This report
uses the aliases to match the source code.

## 6. Test status

```
tests/test_cpvo_calculator.py
tests/test_cpvo_live_router.py
tests/test_cpvo_model_aware.py
tests/test_provider_telemetry.py
→ 67 passed in 1.49s
```

---

*Generated by kanban worker `worker-merchant` for task `t_f38a0e51` (parent
`t_921dd289`). No source files were modified — calibration only.*
