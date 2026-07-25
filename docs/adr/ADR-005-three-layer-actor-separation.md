# ADR-005: Three-Layer Actor Separation

## Status

Proposed

## Date

2025-07-25

## Related

- ADR-001 (price-first routing), ADR-002 (multi-Kalman separation)
- `docs/price-first-kalman-plan.md` §2 (three-layer architecture)

## Context

The routing system serves two fundamentally different use cases:

1. **Self-hosted consumer** (current Hermes setup): The operator owns API keys, pays subscription fees, and wants to route requests to the cheapest viable provider for their own use.

2. **Merchant/Routster node** (future): The operator owns API keys, pays upstream costs, and resells LLM access to customers for ecash. They must set prices that maximize profit (traffic × margin) while remaining competitive.

These are different actors with different objectives, different information, and different Kalman filters. The cost data (what the merchant pays upstream) is private to the merchant. The price data (what the customer pays) is public. The demand curve (how traffic responds to price changes) is only relevant to the merchant.

Cramming both use cases into one module creates coupling: a change to pricing logic affects routing and vice versa. It also means a self-hosted consumer who just wants routing would need to carry the pricing/demand code.

## Decision

**Three layers with explicit actor ownership:**

```
Layer 1 — COST TRACKING (per key, private to key owner)
  Actor: Key owner
  Kalman: Cost Kalman (base_rate, rate_velocity)
  Output: my_cost(key) — PRIVATE
  Used by: Layer 2

Layer 2 — PRICING DECISION (merchant / Routster node)
  Actor: Routster node operator
  Kalman: Demand Kalman (intercept, slope)
  Input: my_cost from Layer 1, competitor prices, demand observations
  Output: announced_price — PUBLIC
  Objective: maximize profit = traffic × (price - cost)
  Used by: Layer 3 (if merchant), bypassed (if self-hosted)

Layer 3 — ROUTING DECISION (customer / Hermes agent)
  Actor: Buyer / Hermes agent
  Kalman: Consumption Kalman (tokens_per_call, request_rate, acceleration)
  Input: announced_prices from all available providers
  Output: chosen_provider + model
  Objective: minimize cost for required quality
```

For the self-hosted consumer (current setup): Layer 1 feeds directly into Layer 3. Layer 2 is bypassed (no resale). The "announced price" is just the cost from Layer 1.

For the Routster node: all three layers are active. Layer 1 computes cost. Layer 2 adds margin and publishes the price. Layer 3 (running on the customer's side) routes based on published prices.

## Invariants

1. Layer 1 output (cost) is NEVER exposed to customers directly. Only Layer 2's announced_price is public.
2. Layer 3 never sees Layer 1 data. It only sees announced prices.
3. Layer 2 is OPTIONAL. For self-hosted routing, Layer 1 feeds Layer 3 directly.
4. Each layer can be used independently: a customer can use Layer 3 without Layer 1 or 2.
5. The Demand Kalman exists ONLY in Layer 2 (merchant side). It never appears in Layer 3.
6. The Consumption Kalman exists in Layer 3 (buyer side). For the self-hosted case, it also feeds back into Layer 1 (the operator's own consumption affects their cost tracking).

## Consequences

### Positive
- Merchant can publish Layer 3 (routing module) for others without revealing cost structure
- Self-hosted users skip Layer 2 entirely — simpler deployment
- Clean separation: pricing changes don't affect routing and vice versa
- Routster node support is additive (new layer), not a refactor of existing layers
- Each layer independently testable

### Costs
- Three separate interfaces to maintain (cost, pricing, routing)
- For self-hosted case, Layer 1 → Layer 3 shortcut adds a special path (minor complexity)
- Demand Kalman has cold-start problem for new Routster nodes (no price/traffic history)
