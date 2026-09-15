# ADR-016: Health-Derived Price + Target-Pressure Quota Controller

- **Status:** Accepted (operator-ratified 2026-09-15)
- **Builds on:** ADR-015 (HealthKalman), ADR-004 (effective price > 0),
  ADR-011 (config-driven amortized seed pricing).

## Context

Two requirements from the operator:

1. **Price must depend on health.** As a lane's health deteriorates we **raise its
   routing price**, which (because the router always picks the cheapest lane) sheds
   load away from it and protects its quota. This is the market mechanism for
   backpressure.
2. **Price must target a pressure.** We want each quota window to be consumed
   **just before it resets** — never exhausted early (service failure) and never left
   unused (wasted entitlement). The existing `pace_factor` is a heuristic
   (`pace_ratio²` clamped to `[0.5, 3.0]`); it does not *aim* at a target.

The cost/quality tradeoff is real: large contexts and heavy lanes cost more; routing
too hard away from a lane wastes its flat-rate entitlement.

## Decision

1. **Health-derived price.** The effective routing price becomes:
   `effective = base_rate(PriceKalman) × peak × scarcity × HealthKalman × controller`,
   replacing the deprecated `health_factor` / `health_pricing_factor`. Effective price
   remains **always > 0** (ADR-004); `+inf` still means "do not dial".
2. **Target-pressure controller.** Replace the heuristic pace multiplier with a
   **controller that solves for the multiplier `m`** minimizing
   `|t_exhaust(m) − t_reset|`, subject to `m ≥ cost_floor`. Implement with **numeric
   bisection** each 5–30 min refresh (robust to the shape of the demand response); do
   not assume a closed form.
3. **Demand response `r(m)`** = own-traffic diversion (higher own-price → we route
   elsewhere) + `DemandKalman` elasticity for sold traffic.
4. **Probe gate rule.** A fresh `HTTP 429` / `ok:false` in the probe output marks a
   lane **routing-unhealthy** for the gate (not only `healthy is False`).
5. **Observability.** Publish the chosen `m`, the target pressure, and the projected
   depletion/reset times to the operator digest.

## Consequences

- (+) Quota is used efficiently: aim for ~100% at reset; "use it or lose it" honoured.
- (+) Health backpressure is continuous and price-based (market-consistent), not a
  hard on/off.
- (+) One control point (the multiplier) for pacing, auditable in the digest.
- (−) Requires trustworthy `resets_at` and burn-rate estimates → depends on ADR-017.
- (−) Bisection each cycle is slightly more compute than a closed form; bounded.
- **Risk if ignored:** either premature exhaustion (503s) or wasted quota, and
  reintroduction of the boolean-health exhaustion bug.

## References

- `src/pricing_engine.py` (`compute_effective_price`, `pace_factor`,
  `pace_factor_multi`, `quota_pressure_factor`), `src/price_kalman.py`,
  `src/realtime_pricing.py`, `~/.hermes/bot/flat_router.py`.
- ADR-004, ADR-011, ADR-015, ADR-017; plan §32; hermes `DECISIONS.md` D-142.
