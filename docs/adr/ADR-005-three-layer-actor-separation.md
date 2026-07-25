# ADR-005: Three-Layer Actor Separation

## Status

Proposed

## Date

2025-07-25

## Related

- ADR-001 (price-first routing), ADR-002 (multi-Kalman separation)
- `docs/price-first-kalman-plan.md` §2 (three-layer architecture)

## Context

The routing system serves three operating modes:

1. **Self-hosted consumer** (current Hermes setup): The operator owns API keys, pays subscription fees, and wants to route requests to the cheapest viable provider for their own use.

2. **Merchant/Routster node** (future): The operator owns API keys, pays upstream costs, and resells LLM access to customers for ecash. They must set prices that maximize profit (traffic × margin) while remaining competitive.

3. **Dual-mode arbitrage** (future): The operator simultaneously buys LLM access from Routster (cheapest upstream) AND sells on Routster (profitable resale). They minimize cost when buying and maximize profit when selling — at the same time. Both sides require scraping the Routster marketplace for competitor prices, service offerings, and provider reliability.

These are different actor configurations with different objectives, different information, and different Kalman filters. The cost data (what the merchant pays upstream) is private to the merchant. The price data (what the customer pays) is public. The demand curve (how traffic responds to price changes) is only relevant to the merchant.

In dual mode, the operator is simultaneously the buyer AND the seller. Their Layer 1 tracks costs of their own keys (for resale pricing). Their Layer 3 compares: "Should I use my own key, or is it cheaper to buy from Routster provider X?" Their Layer 2 sets resale prices based on both their own costs AND competitor prices scraped from Routster.

Cramming all use cases into one module creates coupling: a change to pricing logic affects routing and vice versa. It also means a self-hosted consumer who just wants routing would need to carry the pricing/demand code.

### Routster Marketplace Intelligence (all modes)

In modes 2 and 3, the operator needs marketplace awareness:
- **Competitor prices**: Scrape Routster for what other nodes charge per model
- **Service offerings**: What models/throughput other nodes provide
- **Provider reliability**: Track whether purchased responses actually delivered (success rate, latency, quality)
- **Trust scoring**: For now, a whitelist of trusted providers (web of trust). Future: automated quality verification and malicious-provider detection.

This marketplace data feeds both Layer 2 (pricing decisions — "am I cheaper than competitors?") and Layer 3 (routing decisions — "is Routster provider X cheaper than my own key?").

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

For dual mode: All three layers run simultaneously. Layer 1 tracks own-key costs. Layer 2 sets resale prices (scraping Routster for competitor data). Layer 3 makes buy-vs-self-route decisions: "Is my own key cheaper, or should I buy from Routster provider X?" The operator maximizes profit on the sell side while minimizing cost on the buy side.

## Invariants

1. Layer 1 output (cost) is NEVER exposed to customers directly. Only Layer 2's announced_price is public.
2. Layer 3 never sees Layer 1 data. It only sees announced prices.
3. Layer 2 is OPTIONAL. For self-hosted routing, Layer 1 feeds Layer 3 directly.
4. Each layer can be used independently: a customer can use Layer 3 without Layer 1 or 2.
5. The Demand Kalman exists ONLY in Layer 2 (merchant side). It never appears in Layer 3.
6. The Consumption Kalman exists in Layer 3 (buyer side). For the self-hosted case, it also feeds back into Layer 1 (the operator's own consumption affects their cost tracking).
7. In dual mode, Layer 3's provider list includes BOTH own keys (from Layer 1) AND external Routster providers (scraped marketplace). The optimizer picks whichever is cheaper per request.
8. Provider trust is a hard filter in Layer 3. Untrusted Routster providers are never selected, regardless of price. Trust defaults to a whitelist (web of trust).

## Consequences

### Positive
- Merchant can publish Layer 3 (routing module) for others without revealing cost structure
- Self-hosted users skip Layer 2 entirely — simpler deployment
- Clean separation: pricing changes don't affect routing and vice versa
- Routster node support is additive (new layer), not a refactor of existing layers
- Each layer independently testable
- Dual mode enables arbitrage: buy cheap from Routster, sell at profit on Routster, all in one system

### Costs
- Three separate interfaces to maintain (cost, pricing, routing)
- For self-hosted case, Layer 1 → Layer 3 shortcut adds a special path (minor complexity)
- Demand Kalman has cold-start problem for new Routster nodes (no price/traffic history)
- Dual mode adds marketplace scraping dependency (Routster API must be queried regularly)
- Provider reliability tracking requires recording outcomes of every Routster purchase
- Trust whitelist requires manual maintenance until automated quality verification is built
