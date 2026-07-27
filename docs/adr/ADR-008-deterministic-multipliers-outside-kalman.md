# ADR-008: Deterministic Multipliers Outside Kalman (Generalized)

**Date:** 2026-07-27
**Status:** Accepted
**Supersedes:** Generalizes ADR-003 (peak multiplier) to all step-change signals
**Related:** ADR-002 (Multi-Kalman separation), ADR-003 (Peak multiplier), ADR-004 (Effective price positivity)

---

## Context

ADR-003 established that peak hours are a deterministic step function OUTSIDE the
Kalman filter — the Kalman tracks the smooth cost trend underneath, and the peak
multiplier applies instant 3.0x on top. This preserved instant response to step
changes.

The same principle applies to two additional signals:

1. **Health** — A key going unhealthy (circuit breaker tripped, repeated failures)
   is a step change. If health were a Kalman state dimension, the filter would
   SMOOTH the transition — meaning a key that suddenly dies would still appear
   "cheap" for several observations before the Kalman caught up. During those
   observations, traffic would route to the dying key and break Hermes.

2. **Quota pacing** — The burn-rate-vs-time-remaining calculation is a
   deterministic prediction. Given a smoothed burn rate (from ConsumptionKalman),
   whether we will exhaust quota before reset is arithmetic, not estimation.
   The pace_factor multiplier applies the result instantly.

Both are step changes. Both need instant response. Both belong OUTSIDE the
Kalman as deterministic multipliers.

## Decision

**All step-change signals are deterministic multipliers OUTSIDE the Kalman.
The Kalman only tracks the smooth trend underneath.**

The effective price formula is:

```
effective_price = base_rate × peak_mult × scarcity_mult × health_mult × pace_mult
                  |_________|   |________|   |___________|   |________|
                   Kalman        deterministic multipliers (ADR-003)
                   (smooth)
```

Where:
- `base_rate` — Kalman-smoothed expected cost per million tokens
- `peak_mult` — 3.0x during UTC [6,10), 1.0x otherwise (ADR-003)
- `scarcity_mult` — ramps 1.0x → 2.0x as quota usage goes 50% → 100%
- `health_mult` — graduated: 1.0x (0 failures) → 1.5x → 3.0x → 10.0x → +inf (>10 failures)
- `pace_mult` — 0.5x → 3.0x based on predicted quota exhaustion (pace_ratio²)

### Why Not Health as a Kalman State Dimension?

Considered: State = [base_rate, velocity, availability] where availability ∈ [0,1].

Rejected because:
- Kalman SMOOTHS availability. A key that suddenly dies (0% availability) takes
  N observations before the filter converges. During those N observations, the
  price isn't high enough — traffic still routes to the dying key.
- This is the EXACT failure mode that ADR-003 solved for peak hours: step
  changes inside Kalman get smoothed, which is wrong behavior.
- For our dead z.ai key (120 failures, subscription expired), smoothing would
  mean the key appears viable for several requests after death — breaking Hermes.

### Why Not Pace Factor as a Kalman State?

Considered: State = [burn_rate, will_exhaust_flag].

Rejected because:
- "Will we exhaust?" is not a noisy measurement needing smoothing. Given burn
  rate and time remaining, it's deterministic arithmetic.
- The Kalman's job is to estimate burn_rate (noisy, needs smoothing). The
  prediction of exhaustion is a pure function of the smoothed estimate.
- Two separate problems, two separate mechanisms: Kalman for estimation,
  deterministic math for prediction.

## The Hybrid: Failure Cost as Kalman Observation

While the health MULTIPLIER is deterministic (instant), the Kalman should still
LEARN from failure patterns chronically. This is done through the OBSERVATION,
not the state:

```
On success: kalman.update(actual_cost)                    # spend_usd / tokens
On failure: kalman.update(fallback_cost + retry_penalty)  # true cost of failed attempt
```

A key with 50% failure rate:
- Success cost: $0.03/M
- Fallback cost (PPQ): $0.14/M + retry latency penalty
- Expected cost per attempt: 0.5 × $0.03 + 0.5 × $0.14 = $0.085/M

The Kalman converges to $0.085/M — the TRUE expected cost. A chronically
unhealthy key naturally becomes more expensive in the Kalman without any
external multiplier. The multiplier handles ACUTE failure (instant), the
Kalman handles CHRONIC failure (learned).

This mirrors ADR-003 exactly:
- Peak hours: step OUTSIDE + smooth trend underneath
- Health: step OUTSIDE + smooth expected cost (including failure overhead) underneath

## Observation Frequency: 5-Minute Windowed Aggregation

The ConsumptionKalman (burn rate) should receive observations at a fixed
5-minute interval, NOT per-request. Individual request token counts are
extremely noisy (one request = 500 tokens, next = 50,000). Per-request
updates would require very high measurement noise (R), slowing convergence.

Instead, aggregate at a fixed interval:

```python
every 5 minutes:
    hourly_rate = tokens_in_last_5min × 12
    consumption_kalman.update(hourly_rate)
```

This reduces measurement noise dramatically and lets the Kalman converge
within 30-60 minutes (6-12 observations) instead of thousands of per-request
observations.

For the PriceKalman (cost), observations are naturally lower-frequency:
- On success: one observation per billing cycle (daily or per-request if
  per-token pricing is available)
- On failure: one observation per failed attempt (fallback cost + penalty)

The 5-minute window applies to the ConsumptionKalman only, not PriceKalman.

## Two Timescale Summary

| Time scale  | Mechanism              | Purpose                          |
|-------------|------------------------|----------------------------------|
| Seconds-mins | Graduated multiplier   | INSTANT response to acute failure|
| Hours-days   | Kalman cost adjustment | LEARNS chronic failure pattern   |
| 5-min windows| ConsumptionKalman      | Smoothed burn rate for pacing   |
| Deterministic| pace_factor math        | Predicts quota exhaustion        |

## Consequences

- Multipliers are pure functions — easy to test, no state, no surprises
- Kalman state stays 2-dimensional [base_rate, velocity] — simple, stable
- Step changes get instant response (no Kalman lag)
- Chronic patterns are learned through observations, not state expansion
- The 5-minute aggregation window gives clean burn-rate estimates
- All multipliers are composable — multiply them together, order doesn't matter

## Implementation Notes

- `pricing_engine.py` contains all deterministic multiplier functions
- `price_kalman.py` / `consumption_kalman.py` contain the Kalman filters
- `routing_optimizer.py` composes them: effective_price = base × multipliers
- `primary_router.py` / `shadow_hook.py` wire production data to the optimizers
- The failure-cost-observation path is implemented in `src/cost_observer.py`
  (CostObserver class). PrimaryRouter and ShadowHook can use it to feed
  real cost observations to PriceKalman on success/failure.
- The 5-minute windowed aggregation is NOT YET implemented (cron or proxy interval needed)