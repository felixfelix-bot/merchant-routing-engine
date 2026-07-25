# ADR-002: Multi-Kalman Separation

## Status

Proposed

## Date

2025-07-25

## Related

- `burn_predictor.py` lines 63-120 (`KalmanPredictor` class — existing 2-state filter)
- ADR-001 (price-first routing), ADR-003 (deterministic peak multiplier)
- ADR-005 (three-layer actor separation)

## Context

A single Kalman filter estimates the state of a linear dynamical system. Its state vector must evolve according to one linear model with one process noise profile.

The routing system has dynamics at fundamentally different time scales and with different observability:

1. **Cost/base_rate** — evolves over hours to days. Driven by subscription amortization (tokens accumulate against a fixed monthly fee). Observations: tokens consumed per billing cycle, quota percentages.

2. **Consumption/burn_rate** — evolves over minutes. Driven by task arrival, model selection, operator behavior. Observations: tokens per API call, calls per minute.

3. **Demand curve** (Routster only) — evolves over days. Driven by market behavior, competitor pricing. Observations: (price, traffic) pairs.

Cramming all three into one state vector means the process noise (Q) is wrong for every dimension simultaneously. The cost estimate gets jerked by consumption spikes. The consumption estimate gets smeared by slow cost drift. The filter cannot converge on accurate estimates for any dimension.

Additionally, different providers go dark at different times. When Ollama Cloud 429s, its consumption Kalman stops receiving data (pure prediction mode), but z.ai keys keep updating normally. A coupled filter would let Ollama's growing uncertainty contaminate the z.ai estimates.

## Decision

**Separate Kalman filters per concern, per provider.**

Three filter families:

- **Cost Kalman** (Layer 1): one per provider. State: `[base_rate, rate_velocity]`. Smooth tracking of amortized cost.
- **Consumption Kalman** (Layer 2/3 input): one per provider. State: `[tokens_per_call, request_rate, acceleration]`. Predicts quota exhaustion. This is the existing `burn_predictor.py`, extracted and made provider-agnostic.
- **Demand Kalman** (Layer 2, Routster only): one per Routster node. State: `[demand_intercept, demand_slope]`. Estimates price-traffic relationship.

Each filter:
- Has its own process noise (Q) tuned to its time scale
- Has its own measurement noise (R) tuned to its observation quality
- Updates independently (cost: every 5 min, consumption: per call, demand: per pricing observation)
- Fails independently (a 429'd provider's consumption filter goes dark without affecting others)

## Invariants

1. Cost Kalman state vector is exactly 2D: `[base_rate, rate_velocity]`. Never includes peak multiplier, health, or consumption.
2. Consumption Kalman state vector is exactly 3D: `[tokens_per_call, request_rate, acceleration]`. Never includes price or cost.
3. Demand Kalman state vector is exactly 2D: `[intercept, slope]`. Never includes cost or burn rate.
4. No Kalman filter observes data from another filter's domain. Communication is through the routing/pricing optimizers, not through shared state.
5. Each provider gets its own filter instance. No cross-provider state coupling.

## Consequences

### Positive
- Each filter tuned to its physics (process noise matches time scale)
- Independent failure (one provider going dark doesn't contaminate others)
- Clear separation of concerns (cost, consumption, demand are distinct problems)
- Extensible: adding a provider = new filter instances, same code
- Testable: each filter unit-tested in isolation

### Costs
- More classes/instances to manage (N providers × 3 filter types)
- Slightly higher memory (negligible — each filter is 2-3 floats)
- Cold start for each filter independently (mitigated by seeding from historical data)
