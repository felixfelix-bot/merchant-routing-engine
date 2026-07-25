# ADR-004: Effective Price Is Always Positive

## Status

Proposed

## Date

2025-07-25

## Related

- ADR-001 (price-first routing)
- `model_matrix.json` (Ollama local: `cost_per_1m_offpeak: 0.0` — current zero pricing)

## Context

Several providers in the current system have a cost of zero:

- Ollama local (runs on hardware already owned, no per-token cost)
- z.ai "friend" key (shared, no direct cost to the operator)

The operator identified a critical constraint: **it is never acceptable to have zero price, and it is acceptable to have infinite price (unreachable provider), but both cannot occur simultaneously.** Zero price breaks the routing optimizer's comparison logic and causes division-by-zero in profit calculations. A provider priced at $0/M will ALWAYS win the `argmin` — even if it's a local toy model that produces poor quality output.

## Decision

**Every provider's effective price MUST be > 0. A minimum epsilon ($0.001/M) is enforced for all providers, including "free" ones.**

Implementation:

```python
MIN_EFFECTIVE_PRICE = 0.001  # $/M tokens — floor for all providers

def effective_price(provider: Provider, t: datetime) -> float:
    raw = (
        cost_kalman.base_rate(provider)
        * peak_multiplier(t)
        * scarcity_factor(provider)
        * health_factor(provider)
    )
    return max(raw, MIN_EFFECTIVE_PRICE)
```

For providers with genuinely zero cost (local Ollama, free shared keys):
- Base rate is set to MIN_EFFECTIVE_PRICE ($0.001/M) in config
- Scarcity and health multipliers still apply normally
- If the circuit breaker trips, health_factor = infinity overrides the floor (provider becomes unreachable, not zero-cost)

For per-token providers (PPQ, OpenRouter):
- Base rate is their published price (always > 0 naturally)

For subscription providers (z.ai):
- Base rate = subscription_cost / tokens_used (always > 0 as long as subscription_cost > 0)

## Invariants

1. `effective_price(provider, t) > 0` for ALL providers at ALL times.
2. `MIN_EFFECTIVE_PRICE = 0.001` is the global floor. No provider's base rate may be configured below this.
3. `health_factor = infinity` is the ONLY mechanism that can make a provider unselectable. A provider is NEVER unselectable via zero price.
4. Zero and infinity never appear in the same product: if health_factor = infinity, the product is infinity (unreachable), not zero.
5. The routing optimizer's `argmin(effective_price)` always returns a valid provider (no division-by-zero, no degenerate selection).

## Consequences

### Positive
- Routing optimizer never encounters division-by-zero or degenerate comparisons
- Even "free" providers compete on price with non-free ones (the epsilon is small enough that $0.001/M always beats $0.068/M but doesn't break math)
- Profit calculations (traffic × margin) always produce a finite, positive result
- No special-case handling needed for zero-cost providers in the optimizer

### Costs
- Local Ollama's price is an artificial epsilon rather than truly zero (cosmetic inaccuracy)
- If multiple "free" providers exist, they all have the same floor price — the optimizer must use a secondary sort key (quality score) to break ties
