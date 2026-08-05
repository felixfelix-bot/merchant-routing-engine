# Code Review: Quota-Pressure Pricing System

**Reviewer:** Consultant subagent (code-correctness focus only — no math theory)
**Date:** 2026-08-05
**Branch:** `converged-rate-replay` @ `49bef24`
**Scope:** `src/pricing_engine.py`, `src/live_router.py`, `src/realtime_pricing.py`, `tests/test_quota_pressure.py`, git state.

Validated against Felix's 5 decisions:
1. ALL endpoints get pressure (z.ai, ollama, PPQ, OpenRouter, DeepInfra)
2. Low asymptote (1.5)
3. Superposition (multiply windows, not max)
4. Credit-based endpoints: `u = 1 - (remaining/starting)`
5. Monthly window included for z.ai

---

## Summary verdict

| # | Felix decision | Code state |
|---|---|---|
| 1 | All 5 endpoints get pressure | **PARTIAL** — z.ai ✅, ollama ✅, PPQ ✅, **OpenRouter ❌**, **DeepInfra ❌** |
| 2 | Low asymptote 1.5 | ✅ DONE in constants (uncommitted diff); ⚠️ function default still 4.17 |
| 3 | Superposition (multiply) | ✅ DONE for z.ai 3-window; legacy 2-window helper also correct |
| 4 | Credit-based `u = 1 - (remaining/starting)` | **NOT DONE** — formula not implemented anywhere |
| 5 | Monthly window for z.ai | ✅ DONE (`_zai_window_usages` reads monthly from API; `quota_pressure_factor` multiplies it) |

**Biggest gaps:** OpenRouter/DeepInfra pressure is not wired (kill switch + constants exist, no application code). The credit-depletion formula `u = 1 - (remaining/starting)` is documented but never computed. All pressure kill switches default OFF (production currently runs NO pressure).

---

## 1. `src/pricing_engine.py` — `quota_pressure_factor`

**Location:** lines 501–581.

### Superposition: ✅ DONE (correct)

```python
# lines 564–581
windows: list[float] = [usage]
if weekly is not None: windows.append(weekly)
if monthly is not None: windows.append(monthly)

if any(u >= 1.0 for u in windows):
    return math.inf if hard_limit else asymptote

result = 1.0
for u in windows:
    result *= _single_window_factor(u, onset, asymptote)
return result
```

- All provided windows are **multiplied** (not max). ✅
- `monthly=` param is collected into the product. ✅ (Decision 5)
- Single-window factor `_single_window_factor` (lines 471–498) implements `1 + K·t/(1-t)` with `K = asymptote - 1`, `t = (u - onset)/(1 - onset)`. Correct.

### Issues

- **⚠️ Misleading default.** The signature default is `asymptote: float = EXTRA_USAGE_MULTIPLIER` (= 4.17), **not** 1.5. The per-provider constants (`OLLAMA_QUOTA_PRESSURE_ASYMPTOTE`, `ZAI_…`, `PPQ_…`) default to 1.5 via env, and `live_router` passes them explicitly — so *current callers* get 1.5. But the bare `quota_pressure_factor(0.9)` call (used heavily in tests) still uses 4.17. Latent bug risk if a new caller forgets the kwarg.
- **Stale docstring.** Line 554 says `default 0.75`; actual onset default is `0.70` (`QUOTA_PRESSURE_ONSET`). Lines 526–544 hardcode "asymptote=4.17" examples that no longer match the per-provider 1.5 usage.
- **Legacy `quota_pressure_factor_superimposed`** (lines 584–629): 2-window only (session × weekly, no monthly). Kept for back-compat. Does NOT forward `hard_limit` — always returns `+inf` at either window ≥ 1.0. Fine for z.ai, wrong shape for ollama.

---

## 2. `src/live_router.py` — Pressure application per provider

### Where pressure is applied (`_do_select_failover`, lines 613–945)

| Provider | Lines | Kill switch (default) | Status |
|---|---|---|---|
| **ollama_cloud** | 852–863 | `_QUOTA_PRESSURE_ENABLED` (OFF) | ✅ `base_rate *= quota_pressure` |
| **z.ai ours/friend** | 878–884 | `_ZAI_QUOTA_PRESSURE_ENABLED` (OFF) | ✅ `_compute_zai_pressure(qs)`, 3-window superposition |
| **ppq** | 890–896 | `_PPQ_QUOTA_PRESSURE_ENABLED` (OFF) | ✅ `_compute_ppq_pressure(qs)` |
| **openrouter** | — | `_OPENROUTER_CREDIT_PRESSURE_ENABLED` (OFF) | ❌ **NO application code** |
| **deepinfra** | — | `_DEEPINFRA_CREDIT_PRESSURE_ENABLED` (OFF) | ❌ **NO application code** |

### Evidence for the OpenRouter / DeepInfra gap

- Kill switches defined: lines 125–130.
- Constants imported: lines 61–66 (`OPENROUTER_CREDIT_PRESSURE_ONSET/ASYMPTOTE`, `OPENROUTER_STARTING_BALANCE`, same for DeepInfra).
- **But:** no `_compute_openrouter_pressure` / `_compute_deepinfra_pressure` helper functions exist (only `_compute_zai_pressure` @ line 279 and `_compute_ppq_pressure` @ line 308).
- **No `if name == "openrouter"` / `if name == "deepinfra"` branch** in the provider loop (lines 790–945).
- `prov_has_pressure` (lines 912–916) checks only ollama / z.ai / ppq — OpenRouter & DeepInfra are absent, so they still go through the linear `scarcity_factor` path with `quota_total = inf` (i.e., scarcity = 1.0 → **no pressure at all**).
- `OPENROUTER_STARTING_BALANCE` / `DEEPINFRA_STARTING_BALANCE` are imported but **never referenced** anywhere (confirmed via grep).

### z.ai pressure details (✅ correct on decisions 3 & 5)

`_compute_zai_pressure` (lines 279–306) → `_zai_window_usages` (lines 219–276) extracts `(session, weekly, monthly)` fractions from the quota entry's `windows` list (aliases: `5-hour`/`5h`/`session`, `weekly`/`7-day`, `monthly`/`30-day`). All three are passed to `quota_pressure_factor(..., hard_limit=True)`. ✅

### Stale comments (correctness-relevant)

- Line 719: `# FELIX DECISION (Aug 5): uniform asymptote 5.0` — should read 1.5.
- Lines 873, 888: `# asymptote=5.0` — should read 1.5.
- `_compute_zai_pressure` docstring (line 280): "asymptote=5.0" → 1.5.
- `_compute_ppq_pressure` docstring (line 311): "asymptote=5.0" → 1.5.

These are comment-only mismatches — the runtime values come from the env-defaulted constants, which ARE 1.5 in the uncommitted diff. But they will mislead the next reader.

---

## 3. `src/realtime_pricing.py` — `_measure_zai_amortized`

**Location:** lines 393–455.

### Verdict: uses **trailing 365-day data**, NOT month-to-date. ✅ (intentional)

```python
# line 410
trailing_cutoff = now - 365 * 86400   # 365-day trailing window
# lines 416–423
rows = conn.execute(
    "SELECT key_name, COALESCE(SUM(total_tokens), 0), MIN(ts) "
    "FROM api_calls WHERE key_name IN ('ours','friend') AND ts >= ? "
    "GROUP BY key_name", (trailing_cutoff,)).fetchall()
# lines 440–443
trailing_days = max(1.0, (now - min_ts) / 86400.0)
annualized_tokens = tokens * (365.0 / trailing_days)
rate = annual_fee / (annualized_tokens / 1e6)
```

- Docstring (lines 393–407) explicitly states this *replaced* the old month-to-date approach (which reset monthly and was noisy at month boundaries).
- Source tagged `SRC_ZAI_AMORTIZED`, `is_measured=False` (estimated).

### Important distinction

This function computes the **base $/M rate** for z.ai — it is NOT the pressure quota window. The **monthly pressure window** Felix wants (Decision 5) is the *live quota usage fraction* from the z.ai API, which is correctly handled by `_zai_window_usages` in `live_router.py`. The two concerns are separate; both are correctly implemented for z.ai.

---

## 4. `tests/test_quota_pressure.py`

### What's tested

- `TestQuotaPressureFactor` — RP-EXP shape: gate tests at 50/70/99%, onset boundary, monotonicity, custom onset/asymptote, midpoint property.
- `TestCrossoverPoints` — Ollama-vs-z.ai price crossover at ~72% (off-peak) and ~84% (peak).
- `TestComputeEffectivePriceWithPressure` — pressure stacks with peak/scarcity/health/pace; overrides legacy regime.
- `TestQuotaPressureSuperimposed` — 2-window product property, symmetry, exhaustion → +inf, `superimposed ≥ max-based` invariant, monotonicity.

### What's tested in `test_universal_pressure.py` (companion file)

- z.ai 3-window superposition via `_compute_zai_pressure`.
- `_zai_window_usages` extraction (per-window + flat fallback + error sentinel skip).
- PPQ `_compute_ppq_pressure` shape.
- Integration: pressure rises with usage, 100% trips breaker, superposition in routing.

### What's MISSING (gaps vs Felix's decisions)

1. **No test with asymptote=1.5.** All `quota_pressure_factor` tests use the bare signature → default 4.17. `test_at_full_usage_caps_at_asymptote` (line 56) asserts `== EXTRA_USAGE_MULTIPLIER` (4.17) — would **fail** if the default were changed to 1.5.
2. **No test for per-provider onsets** (z.ai 0.60, credit-based 0.80). `test_universal_pressure.py` checks the *constants* exist but doesn't exercise the live values through the curve.
3. **No test for `quota_pressure_factor` with `monthly=` param** (the 3-window product path). Only the legacy 2-window `_superimposed` helper is tested. `test_universal_pressure.py` covers the `_compute_zai_pressure` wrapper but not `quota_pressure_factor(u, weekly=w, monthly=m)` directly.
4. **No test for the credit formula `u = 1 - (remaining/starting)`** — because it isn't implemented.
5. **No tests for OpenRouter / DeepInfra pressure** — because it isn't implemented.
6. **No test that `live_router` applies pressure to ALL 5 providers** — only z.ai/PPQ/ollama are exercised.

---

## 5. Git state

```
Branch:  converged-rate-replay  (up to date with github/converged-rate-replay)
HEAD:    49bef24  feat: universal exponential pressure — per-endpoint onset/asymptote constants, z.ai + PPQ curves wired

Uncommitted:
  deleted:   _inspect_db.py
  modified:  src/pricing_engine.py
```

### What the uncommitted `pricing_engine.py` diff actually changes

**Constant/comment changes only — NO logic changes.**

- `OLLAMA_QUOTA_PRESSURE_ASYMPTOTE`: 4.17 → **1.5**
- `ZAI_QUOTA_PRESSURE_ASYMPTOTE`: 2.0 → **1.5**
- `PPQ_QUOTA_PRESSURE_ASYMPTOTE`: 5.0 → **1.5**
- **Added** `OPENROUTER_CREDIT_PRESSURE_ONSET/ASYMPTOTE` (0.80 / 1.5), `OPENROUTER_STARTING_BALANCE` (10.0)
- **Added** `DEEPINFRA_CREDIT_PRESSURE_ONSET/ASYMPTOTE` (0.80 / 1.5), `DEEPINFRA_STARTING_BALANCE` (5.0)
- Comments updated to "FELIX FINAL DECISION (Aug 5 19:00): ALL endpoints asymptote=1.5 (UNIFORM LOW)".

### Important

- `src/live_router.py` is **NOT in the diff** — the kill switches, helper functions, and application logic for z.ai/PPQ/ollama were committed in prior commits (`49bef24`, `765cce5`). The OpenRouter/DeepInfra kill switches and constant imports in `live_router.py` are already committed but are **dead code** (defined, never used).
- The uncommitted pricing_engine diff should be committed to make the 1.5 asymptotes effective; until then the committed constants still say 4.17/2.0/5.0.

---

## DONE vs NOT DONE

### ✅ DONE
- Superposition as multiplication (Decision 3) — `quota_pressure_factor` multiplies all windows.
- z.ai 3-window (session × weekly × monthly) pressure (Decision 5) — `_compute_zai_pressure` + `_zai_window_usages`.
- ollama_cloud pressure (session × weekly, hard_limit=False, extra-usage cap).
- PPQ pressure (single credit fraction, hard_limit=True).
- Per-provider asymptote constants defaulting to 1.5 (Decision 2) — **in the uncommitted diff**.
- `_measure_zai_amortized` uses trailing 365-day data (stable rate, no month-boundary noise).
- Kill-switch env vars for all 5 providers.

### ❌ NOT DONE
- **OpenRouter pressure application** — constants + kill switch exist, no `_compute_openrouter_pressure`, no `if name == "openrouter"` branch, absent from `prov_has_pressure`. (Decision 1)
- **DeepInfra pressure application** — identical gap to OpenRouter. (Decision 1)
- **Credit-depletion formula `u = 1 - (remaining/starting)`** — documented in comments but never computed. `OPENROUTER_STARTING_BALANCE` / `DEEPINFRA_STARTING_BALANCE` imported but unused. PPQ reads a pre-computed `used_pct`, not the formula. (Decision 4)
- **Commit the 1.5 asymptote diff** — currently uncommitted in `pricing_engine.py`.

### ⚠️ Minor issues
- Stale docstrings/comments referencing asymptote=5.0 / 4.17 / onset=0.75 (7 locations listed above).
- `quota_pressure_factor` signature default `asymptote=4.17` mismatches the per-provider 1.5 — latent footgun.
- Tests assert against the old 4.17 default; would break if the default is changed.
- All pressure kill switches default OFF — production currently runs zero pressure (intentional shadow-mode, but worth stating explicitly to Felix).
