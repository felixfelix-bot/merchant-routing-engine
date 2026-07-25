# ADR-003: Deterministic Peak Multiplier (Not Kalman Input)

## Status

Proposed

## Date

2025-07-25

## Related

- `burn_predictor.py` lines 518-521 (`_is_peak_hour()` — current implementation)
- `model_matrix.json` `cost_model.peak_hours_utc: [6,7,8,9]`
- ADR-001 (price-first routing), ADR-002 (multi-Kalman separation)

## Context

Peak hours create a step function in z.ai pricing: 3x cost during UTC hours 6-9, 1x otherwise. This is a step change, not a gradual transition.

Kalman filters smooth noisy observations. Their process noise (Q) determines tracking speed: low Q produces smooth, lagging estimates; high Q produces responsive but noisy estimates. When a step change occurs (peak hours begin), the filter takes several update cycles to "catch up." During those cycles, it outputs a price between 1x and 3x — wrong in both directions.

The operator explicitly requires abrupt price changes at peak-hour boundaries so that consumption switches away from z.ai keys during peak and switches back when peak ends.

## Decision

**Peak multiplier is a deterministic step function of clock time, applied AFTER the Kalman output. It is NEVER a Kalman input.**

```
effective_price = cost_kalman.base_rate        ← smooth, from Kalman
                × peak_multiplier(clock)        ← instant step, deterministic
                × scarcity_factor(quota)        ← updates with quota, deterministic
                × health_factor(circuit)        ← instant, deterministic
```

Implementation:

```python
def peak_multiplier(t: datetime) -> float:
    """Deterministic step function. No filtering."""
    peak_hours = config.get("peak_hours_utc", [6, 7, 8, 9])
    peak_mult = config.get("peak_multiplier", 3.0)
    return peak_mult if t.hour in peak_hours else 1.0
```

The Kalman tracks ONLY the smooth base rate underneath. When peak begins, the multiplier steps from 1.0 to 3.0 instantly. The routing optimizer sees the new effective price immediately and switches providers.

## Invariants

1. Peak multiplier is computed from `datetime.now(timezone.utc).hour` — a pure function of time. No Kalman, no smoothing, no interpolation.
2. The step from 1.0 to peak_multiplier (and back) is instantaneous at the hour boundary.
3. The Cost Kalman's state vector NEVER includes a peak/peak-ness flag.
4. Peak hours are configurable in `strategy.yaml` (`peak_hours_utc` list).
5. Peak multiplier value is configurable (`peak_multiplier: float`, default 3.0).
6. This same principle applies to ALL deterministic multipliers: scarcity_factor and health_factor are also applied outside the Kalman.

## Consequences

### Positive
- Instant response to peak-hour boundaries (no lag, no smoothing)
- Kalman remains accurate (tracks only smooth dynamics)
- Configurable: change peak hours or multiplier without retraining the filter
- Transparent: the step change is visible and explainable in routing reasons

### Costs
- Peak multiplier must be maintained in config (if z.ai changes their peak window, config must update)
- Does not handle gradual price changes (if a provider slowly raises prices over an hour, the Kalman captures that — the step function only models the peak component)
