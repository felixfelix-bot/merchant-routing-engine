# ADR-015: Per-(endpoint, LLM) Predictive Health Kalman

- **Status:** Accepted (operator-ratified 2026-09-15)
- **Supersedes:** ADR-003 and ADR-008 **with respect to health** (their deterministic
  peak/scarcity steps remain valid; their removal of *health* from the filter does not).
- **Amends:** ADR-002 (adds a fourth, health-specific filter to the multi-Kalman set).

## Context

Live routing kept failing with `all providers exhausted` and
`model provider failed after retries` even after D-140. Inspection showed the cause
was structural, not a single bug:

- ADR-003 ("deterministic peak multiplier") and ADR-008 ("deterministic multipliers
  outside Kalman") deliberately moved peak, scarcity **and health** out of the Kalman
  filter into deterministic step/ramp functions. `price_kalman.health_factor()` is
  literally marked *deprecated*; health was reduced to `health_pricing_factor(failure_count)`
  plus a boolean probe flag.
- `flat_router._probe_says_unhealthy()` only trips when the probe says
  `healthy is False`. Under the probe's hysteresis a lane returning a fresh
  `HTTP 429` / `ok:false` stays `healthy:true`, so it remains a candidate; the
  price-competitive lanes get hammered and the pool is exhausted.

Health is a **time-varying, noisy, predictive** quantity ("is this lane *about to*
fail / exhaust?"), which is exactly what a Kalman filter models and what a boolean +
step function cannot.

## Decision

Adopt a **1-D Kalman filter per (endpoint, LLM)** — `HealthKalman` — as the
authoritative health signal for pricing and gating.

1. **State**: a health/`p_error` (and derived `p_exhaust`) estimate per
   (provider, model), with an uncertainty. Optionally a small joint health+price
   state where it reduces bookkeeping.
2. **Observations**: `provider_probe` results, real request outcomes
   (429/5xx/timeouts/latency), and `cost_observer` failure cost. **Error bumps** the
   estimate; **probe success decays** it. No manual thresholds per lane.
3. **Consumption**: the routing price's health component is derived from the Kalman
   estimate (ADR-016), not from `health_pricing_factor`. The gate uses the estimate
   with a soft threshold.
4. **Hard breakers are retained** for **terminal** conditions only: 401/403 (auth),
   paywall, and 402 (unfunded). Those still short-circuit to `+inf`/excluded.
5. **Probe gate fix**: a fresh `HTTP 429` / `ok:false` in `provider_probe.json`
   MUST mark a lane routing-unhealthy, not only `healthy is False`.

## Consequences

- (+) Health becomes predictive and per-lane; 429 storms shed load before they
  exhaust the pool. Fixes the recurring "all providers exhausted".
- (+) Fewer hand-tuned thresholds; the filter self-calibrates from real outcomes.
- (+) Consistent with ADR-004 (`effective price > 0`) and ADR-002 (filters per concern).
- (−) More state to persist and tune; requires per-(provider,model) keys everywhere
  (already the case for the garbage breaker).
- (−) Must reconcile with `cost_observer` (ADR-008) so failure cost is not
  double-counted in both the health estimate and the base rate.
- **Risk if ignored:** reverts to boolean health → the 429-hysteresis exhaustion
  recurs.

## References

- `src/price_kalman.py` (`health_factor` — deprecated), `src/pricing_engine.py`
  (`health_pricing_factor`), `~/.hermes/bot/flat_router.py`
  (`_is_provider_healthy`, `_probe_says_unhealthy`), `scripts/fleet/provider_probe.py`.
- ADR-002, ADR-003, ADR-004, ADR-008, ADR-016; plan §32; hermes `DECISIONS.md` D-141.
