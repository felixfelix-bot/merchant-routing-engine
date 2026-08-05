# Quota-Pressure Pricing: Continuous Ollama Price Function

> **Status:** Design — ready for implementation review
> **Author:** Pricing engine analysis (subagent)
> **Date:** 2026-08-05
> **Replaces:** The step-function `extra_usage_multiplier` (regime → fixed multiplier)
> **Satisfies:** Felix's directive for price-based routing (no quota thresholds)

---

## 1. How the Existing Pricing Engine Works

### 1.1 The core formula

```
effective_price = base_rate × peak × scarcity × health × pace × extra_usage
```

Every multiplier is a **pure deterministic function** (ADR-003) — no Kalman, no
state, no smoothing inside the multipliers. The only Kalman-smoothed component is
`base_rate` itself (from `PriceKalman`, ADR-009).

### 1.2 Each multiplier's purpose

| Multiplier | File / function | Input | Behaviour | Range |
|---|---|---|---|---|
| **base_rate** | `price_kalman.PriceKalman.base_rate` | Observed $/M from billing data | Kalman-smoothed trend (2-state: rate + velocity) | ≥ MIN_EFFECTIVE_PRICE (0.001) |
| **peak** | `pricing_engine.peak_multiplier` | `provider`, `hour_utc` | Instant step: 3.0× during z.ai peak hours {6,7,8,9} UTC, else 1.0 | {1.0, 3.0} |
| **scarcity** | `pricing_engine.scarcity_factor` | `quota_used_pct` (0-100+) | Linear ramp: 1.0 below 50%, reaches 2.0 at 100%, continues past 100% | [1.0, ∞) |
| **health** | `pricing_engine.health_pricing_factor` | `failure_count`, `breaker_tripped` | Graduated tiers: 1.0 → 1.5 → 3.0 → 10.0 → +inf | {1.0, 1.5, 3.0, 10.0, +inf} |
| **pace** | `pricing_engine.pace_factor` | burn rate vs time remaining | Predictive: if burning too fast → price up (up to 3.0×); underutilizing → down (floor 0.5×) | [0.5, 3.0] |
| **extra_usage** | `pricing_engine.extra_usage_multiplier` | `regime` string | **Step function**: included → 1.0, extra → 4.17×, exhausted → +inf | {1.0, 4.17, +inf} |

### 1.3 The problem with `extra_usage_multiplier`

The current `extra_usage_multiplier` is a **three-step cliff**:

```
included (1.0×) ──usage hits 100%──▶ extra (4.17×) ──both windows exhausted──▶ +inf
```

This is exactly the threshold-based approach Felix rejected. Problems:
1. **Discontinuous jump**: Ollama goes from $0.024/M to $0.10/M in a single
   step when session usage crosses 100%. There is no gradual warning.
2. **Late reroute**: The optimizer only sees the price spike AFTER the quota is
   already exhausted. By then, we've already burned extra-usage tokens.
3. **No proactive signal**: At 85% or 95% usage, the price is identical to 0%
   usage — the optimizer has no reason to prefer z.ai until it's too late.

### 1.4 How pricing feeds into routing (the live path)

The actual data flow in `live_router._do_select_failover`:

```
1. Query Ollama API → get session_usage, weekly_usage fractions
2. Classify regime: "included" / "extra" / "exhausted" (step function)
3. extra_mult = extra_usage_multiplier(regime)  → 1.0 / 4.17 / +inf
4. base_rate = effective_rates["ollama_cloud"] * extra_mult  (BAKED INTO BASE)
5. Create throwaway PriceKalman(initial_rate=base_rate)
6. optimizer._evaluate_provider → price_kalman.effective_price(peak, scarcity, health, pace)
7. Optimizer sorts by effective_price; cheapest viable wins
```

> **Note:** `compute_effective_price()` in pricing_engine.py has `extra_usage`
> as a separate top-level multiplier, but the live routing path does NOT call
> that function — it bakes `extra_mult` into `base_rate` before creating a
> throwaway PriceKalman. This inconsistency should be resolved as part of this
> change.

---

## 2. The Right Mechanism: `quota_pressure_factor` (NEW multiplier)

### 2.1 Why not reuse scarcity?

The existing `scarcity_factor` already ramps with quota usage — but it's a
**general-purpose** multiplier applied to ALL providers (z.ai keys included).
Its parameters (onset 50%, reaching 2.0× at 100%) are tuned for gentle
rate-limit preservation, not for Ollama's sharp included→extra cost cliff.

At 100% usage, scarcity = 2.0× → Ollama effective = $0.024 × 2.0 = $0.048/M.
This is still cheaper than z.ai friend off-peak ($0.029/M × scarcity). The
scarcity ramp alone cannot make Ollama more expensive than z.ai because it
applies equally to both.

**Scarcity cannot do this job alone.**

### 2.2 Why not dynamic base_rate?

Changing `base_rate` based on regime violates ADR-003/009: the Kalman filter
tracks the smooth cost trend from billing data; deterministic step changes
belong in the multiplier layer. Baking the extra-usage penalty into base_rate
(also what the live_router currently does) conflates the "what does a token
cost?" signal with the "how much quota is left?" signal.

### 2.3 The answer: a dedicated `quota_pressure_factor`

A new deterministic multiplier that is a **continuous function** of
`session_usage` (and `weekly_usage`), designed specifically for the Ollama
prepaid-card → extra-usage cost transition. It:

- Equals 1.0 when usage is low (no penalty — Ollama is cheapest).
- Rises smoothly as usage → 1.0, crossing z.ai's effective price before the
  cliff.
- Reaches the extra-usage rate (~4.17×) at 100% usage.
- Continues ramping past 100% (over-quota), approaching +inf only when the
  provider is truly unreachable.

**This replaces `extra_usage_multiplier`.** The regime classification
("included"/"extra"/"exhausted") survives for logging/monitoring, but it no
longer drives pricing.

---

## 3. Mathematical Model

### 3.1 The pressure function

For each quota window (session, weekly):

```
              ┌ 1.0                                         if u ≤ onset
pressure(u) = ┤
              └ 1 + (A - 1) × ((u - onset) / (1 - onset))²   if u > onset
```

Where:
- `u` = usage fraction (0.0–1.0+; can exceed 1.0 for over-quota)
- `onset` = usage at which pressure begins (default 0.75 = 75%)
- `A` = asymptotic multiplier at u = 1.0 (default = `EXTRA_USAGE_MULTIPLIER` ≈ 4.17)

The overall `quota_pressure_factor` takes the **MAX** across both windows
(worst case governs — same pattern as `pace_factor_multi`):

```
quota_pressure = max(pressure(session_usage), pressure(weekly_usage))
```

### 3.2 Why quadratic?

A linear ramp (like scarcity) distributes the pressure uniformly across the
onset→full range. A **quadratic** ramp keeps the penalty near 1.0 for most of
the range and concentrates the steep rise near the end — mirroring the real
physics: the probability and cost of hitting the extra-usage cliff accelerate
non-linearly as you approach 100%.

### 3.3 Worked example (default parameters: onset=0.75, A=4.17)

| Session usage | Pressure | Ollama effective ($/M) | z.ai friend off-peak ($/M) | z.ai friend peak ($/M) | Cheaper? |
|---|---|---|---|---|---|
| 50% | 1.00 | $0.024 | $0.029 | $0.087 | **Ollama** |
| 75% | 1.00 | $0.024 | $0.029 | $0.087 | **Ollama** |
| 80% | 1.21 | $0.029 | $0.029 | $0.087 | tie (off-peak) |
| 85% | 1.62 | $0.039 | $0.029 | $0.087 | **z.ai** (off-peak) / **Ollama** (peak) |
| 90% | 2.14 | $0.051 | $0.029 | $0.087 | **z.ai** (off-peak) / **Ollama** (peak) |
| 95% | 3.03 | $0.073 | $0.029 | $0.087 | **z.ai** (off-peak) / **Ollama** (peak) |
| 98% | 3.70 | $0.089 | $0.029 | $0.087 | **z.ai** (both) |
| 100% | 4.17 | $0.100 | $0.029 | $0.087 | **z.ai** (both) |
| 110% | 6.37 | $0.153 | $0.029 | $0.087 | **z.ai** (both) |

> Note: scarcity applies to both providers equally, so it cancels in the
> comparison. The crossover is driven purely by the pressure multiplier.

### 3.4 Key crossover points

| Scenario | Ollama usage at crossover | Why |
|---|---|---|
| Off-peak (z.ai = $0.029) | **~80%** | $0.024 × 1.21 ≈ $0.029 |
| Peak (z.ai = $0.087) | **~98%** | $0.024 × 3.70 ≈ $0.089 |

This is exactly the desired behaviour:
- **Off-peak**: z.ai is cheap ($0.029), so we proactively reroute at 80% Ollama
  usage — plenty of warning before the cliff.
- **Peak**: z.ai is expensive ($0.087), so we squeeze Ollama until ~98% —
  maximizing the value of the cheaper provider during expensive hours.
- The optimizer handles this automatically — **no peak/off-peak special cases**.

### 3.5 Parameters (tunable via env vars)

| Parameter | Env var | Default | Meaning |
|---|---|---|---|
| `onset` | `OLLAMA_PRESSURE_ONSET` | 0.75 | Usage fraction where pressure begins |
| `A` (asymptote) | `OLLAMA_EXTRA_USAGE_MULTIPLIER` (existing) | 4.17 | Multiplier at 100% usage (= extra_rate / base_rate) |

The onset and asymptote are the only two knobs. They are sufficient to tune the
crossover behaviour without changing the function shape.

---

## 4. Concrete Code Changes

### 4.1 `src/pricing_engine.py` — add `quota_pressure_factor()`

```python
# ── Quota-pressure multiplier (continuous, replaces step-function extra_usage) ─
# A continuous function of usage fraction that replaces the discrete
# extra_usage_multiplier. Felix's directive: price-based routing, not thresholds.
#
# onset: usage fraction below which pressure = 1.0 (default 0.75 = 75%)
# asymptote: multiplier at u=1.0 (default EXTRA_USAGE_MULTIPLIER ≈ 4.17)
#
# Formula (per window):
#   u <= onset  → 1.0
#   u >  onset  → 1 + (A - 1) * ((u - onset) / (1 - onset))^2
#
# Multiple windows: MAX governs (worst case, like pace_factor_multi).

QUOTA_PRESSURE_ONSET: float = float(
    os.environ.get("OLLAMA_PRESSURE_ONSET", "0.75")
)


def quota_pressure_factor(
    session_usage: float,
    weekly_usage: float = 0.0,
    onset: float = QUOTA_PRESSURE_ONSET,
    asymptote: float = EXTRA_USAGE_MULTIPLIER,
) -> float:
    """Continuous quota-pressure multiplier for Ollama Cloud.

    Replaces the step-function extra_usage_multiplier with a smooth quadratic
    ramp. As session/weekly usage approaches 1.0 (100%), the multiplier rises
    from 1.0 toward *asymptote* (default ≈4.17×). Past 100% it continues
    ramping (over-quota penalty).

    The worst window (MAX) governs — same pattern as pace_factor_multi.

    Args:
        session_usage: 5h session usage fraction (0.0–1.0+) from Ollama API.
        weekly_usage: 7d weekly usage fraction (0.0–1.0+). Default 0.0.
        onset: Usage fraction at which pressure begins. Default 0.75.
        asymptote: Multiplier at u=1.0. Default EXTRA_USAGE_MULTIPLIER.

    Returns:
        Pressure multiplier ≥ 1.0.
    """
    span = 1.0 - onset
    if span <= 0:
        return asymptote  # degenerate: onset at or past 100%

    def _window(u: float) -> float:
        if u <= onset:
            return 1.0
        ratio = (u - onset) / span
        return 1.0 + (asymptote - 1.0) * ratio * ratio

    return max(_window(session_usage), _window(weekly_usage))
```

### 4.2 `src/pricing_engine.py` — update `compute_effective_price()`

Add `quota_pressure` parameter. Keep `extra_usage_regime` for backward
compatibility but default to using the continuous pressure when provided:

```python
def compute_effective_price(
    base_rate: float,
    provider: str,
    quota_pct: float,
    ...
    extra_usage_regime: str = "included",
    extra_usage_mult: float | None = None,
    # NEW: continuous quota pressure (preferred over regime when provided)
    quota_pressure: float = 1.0,
) -> float:
    ...
    peak = peak_multiplier(provider, hour_utc)
    scarcity = scarcity_factor(quota_pct)
    health = health_pricing_factor(failure_count, breaker_tripped)

    # Continuous pressure takes precedence; fall back to step function
    # only when quota_pressure is not provided (backward compat).
    if quota_pressure != 1.0:
        pressure = quota_pressure
    else:
        pressure = extra_usage_multiplier(extra_usage_regime, extra_usage_mult)

    price = base_rate * peak * scarcity * health * pace_mult * pressure
    ...
```

### 4.3 `src/price_kalman.py` — add `quota_pressure` to `effective_price()`

```python
def effective_price(
    self,
    peak_mult: float = 1.0,
    scarcity: float = 1.0,
    health: float = 1.0,
    pace_mult: float = 1.0,
    quota_pressure: float = 1.0,  # NEW
) -> float:
    raw = self.predict() * peak_mult * scarcity * health * pace_mult * quota_pressure
    if math.isinf(raw) or math.isnan(raw):
        return float("inf")
    return max(raw, MIN_EFFECTIVE_PRICE)
```

### 4.4 `src/routing_optimizer.py` — thread `quota_pressure` through `_evaluate_provider`

Add a `quota_pressure` field to the provider dict in `add_provider()`, then pass
it to `price_kalman.effective_price()`:

```python
def add_provider(
    self,
    ...
    quota_pressure: float = 1.0,  # NEW
) -> None:
    self._providers[name] = {
        ...
        "quota_pressure": float(quota_pressure),  # NEW
    }

def _evaluate_provider(self, ...):
    ...
    effective_price = provider["price_kalman"].effective_price(
        peak_mult=peak_mult,
        scarcity=scarity,
        health=health,
        pace_mult=pace_mult,
        quota_pressure=provider.get("quota_pressure", 1.0),  # NEW
    )
```

### 4.5 `src/live_router.py` — compute `quota_pressure` instead of regime step

In `_do_select_failover`, replace the regime-based extra_mult computation:

```python
# BEFORE (step function):
extra_mult = extra_usage_multiplier(quota_regime)
...
if name == "ollama_cloud" and extra_mult != 1.0:
    if math.isinf(extra_mult):
        healthy = False
    else:
        base_rate = base_rate * extra_mult

# AFTER (continuous):
quota_pressure = 1.0
if extra_usage_status is not None and name == "ollama_cloud":
    quota_pressure = quota_pressure_factor(
        session_usage=extra_usage_status.session_usage,
        weekly_usage=extra_usage_status.weekly_usage,
    )

# Pass to optimizer via add_provider(..., quota_pressure=quota_pressure)
# No more base_rate mutation; no more tier demotion; no more regime branch.
```

The tier demotion (`tier = "low"` in extra regime) is **no longer needed** —
the continuous price increase handles rerouting naturally. Ollama stays "high"
tier; when its price exceeds z.ai's, the optimizer picks z.ai because it's
cheaper at the same tier.

### 4.6 Summary of changes

| File | Change | LOC |
|---|---|---|
| `src/pricing_engine.py` | Add `quota_pressure_factor()` + `QUOTA_PRESSURE_ONSET` constant; add `quota_pressure` param to `compute_effective_price()` | ~40 |
| `src/price_kalman.py` | Add `quota_pressure` param to `effective_price()` | ~3 |
| `src/routing_optimizer.py` | Add `quota_pressure` to provider dict + `_evaluate_provider` | ~5 |
| `src/live_router.py` | Replace regime step with `quota_pressure_factor()` call; remove tier demotion; remove base_rate baking | ~15 |
| `tests/test_quota_pressure.py` | New test file | ~120 |

---

## 5. How the Optimizer Naturally Reroutes

The routing optimizer (`routing_optimizer.py`) does exactly one thing: it sorts
all viable providers by `effective_price` and picks the cheapest. There is no
quota logic, no threshold check, no special-casing.

With the continuous pressure function:

1. **Low usage (0–75%)**: `quota_pressure = 1.0`. Ollama effective = $0.024/M.
   z.ai effective = $0.029/M (off-peak) or $0.087/M (peak).
   → **Ollama wins** (cheapest at high tier). ✓

2. **Moderate usage (75–80%)**: Pressure begins ramping. Ollama effective rises
   from $0.024 toward $0.029.
   → **Ollama still wins** but margin narrowing. ✓

3. **Crossover (~80% off-peak)**: Ollama effective ≈ $0.029 = z.ai off-peak.
   → **z.ai wins** (equal price, optimizer tie-break may favor either). ✓

4. **High usage (85–98%)**: Ollama effective $0.039–$0.089.
   → Off-peak: **z.ai wins** ($0.029 < Ollama). Peak: **Ollama still wins**
   ($0.087 > Ollama until ~98%). ✓

5. **Near-exhaustion (98–100%)**: Ollama effective $0.089–$0.100.
   → **z.ai wins in both peak and off-peak.** ✓

6. **Over-quota (>100%)**: Pressure continues ramping past 4.17×. Ollama
   effective > $0.10/M.
   → **z.ai wins decisively.** If Ollama is the only provider for exclusive
   models, the `_OLLAMA_EXCLUSIVE_MODELS` short-circuit still routes to it. ✓

**No thresholds. No regime strings in the routing path. No tier demotion.
Just price comparison.**

---

## 6. Updated Kanban Task Description

```
Title: Replace extra_usage step function with continuous quota_pressure_factor

Priority: High
Epic: Price-Based Routing (Felix directive)

Description:
Replace the three-step extra_usage_multiplier (included→extra→exhausted)
with a continuous quadratic quota_pressure_factor that is a pure function
of session_usage and weekly_usage from the Ollama API.

The pressure multiplier:
- Equals 1.0 below 75% usage (Ollama cheapest, no penalty)
- Rises quadratically, crossing z.ai's off-peak price (~$0.029/M) at ~80%
- Reaches 4.17× at 100% usage (maps to the real extra-usage rate $0.10/M)
- Continues past 100% (over-quota), approaching +inf only on hard exhaustion
- Takes MAX(session, weekly) — worst window governs

The optimizer then reroutes to z.ai automatically when Ollama's effective
price exceeds z.ai's — no thresholds, no regime strings, no tier demotion.

Files to change:
- pricing_engine.py: add quota_pressure_factor(), update compute_effective_price()
- price_kalman.py: add quota_pressure param to effective_price()
- routing_optimizer.py: thread quota_pressure through _evaluate_provider
- live_router.py: replace regime step with continuous pressure; remove
  tier demotion and base_rate baking
- tests/: new test_quota_pressure.py

Acceptance criteria:
- At 50% usage: Ollama effective price == base_rate (no penalty)
- At 80% usage off-peak: optimizer chooses z.ai over Ollama
- At 80% usage peak: optimizer still chooses Ollama (z.ai too expensive)
- At 100% usage: Ollama effective == base_rate × 4.17 (matches old "extra")
- Over 100%: price continues rising (no cliff)
- All existing tests pass (backward compat via default quota_pressure=1.0)
- Kill switch: OLLAMA_EXTRA_USAGE_ENABLED=false → quota_pressure stays 1.0
```

---

## 7. Migration Strategy

### Phase 1: Add alongside (zero behaviour change)
- Implement `quota_pressure_factor()` in pricing_engine.py
- Add `quota_pressure=1.0` parameter everywhere (default = no-op)
- All existing tests pass unchanged

### Phase 2: Shadow mode (parallel comparison)
- Compute `quota_pressure` in live_router but don't apply it (kill switch)
- Log both the old regime-based decision and the new pressure-based decision
- Verify the pressure-based decision reroutes at the right usage levels

### Phase 3: Go live
- Set `OLLAMA_EXTRA_USAGE_ENABLED=true` (or rename to `OLLAMA_QUOTA_PRESSURE_ENABLED`)
- The continuous pressure replaces the step function
- Monitor for 7 days; tune `onset` if crossover is too early/late

### Phase 4: Deprecate regime strings
- Remove `extra_usage_multiplier()`, `extra_usage_regime` parameter
- Keep regime classification in `ollama_quota_tracker` for monitoring/logging only
