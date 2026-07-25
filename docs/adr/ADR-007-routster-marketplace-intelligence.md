# ADR-007: Routster Marketplace Intelligence

## Status

Proposed — Phase 4 (future)

## Date

2025-07-25

## Related

- ADR-005 (three-layer actor separation, dual mode)
- ADR-001 (price-first routing)

## Context

In dual mode (buy + sell on Routster simultaneously), the operator needs real-time marketplace awareness for both sides:

**Buy side (Layer 3 routing)**: Should I route to my own z.ai key ($0.068/M) or buy from Routster provider X who charges $0.045/M? This requires knowing what Routster providers charge, what models they offer, and whether they're reliable.

**Sell side (Layer 2 pricing)**: Am I the cheapest provider for glm-4.5-flash? If I drop my price by 10%, how much more traffic do I get? This requires knowing competitor prices and estimating demand elasticity.

Without marketplace data, both decisions are blind. The operator might overpay on the buy side or underprice on the sell side.

### Routster Protocol

Routster is an ecash-based LLM marketplace. Providers (sellers) announce prices for models. Buyers pay with ecash tokens. The provider processes the request upstream and returns the response plus change.

Key data available from Routster:
- Provider announcements (price per model, supported models, throughput claims)
- Historical transactions (what was actually charged vs announced)
- Response metadata (latency, success/failure, token counts)

### Provider Reliability Problem

Not all providers deliver what they promise. Problems include:
- Announced price differs from charged price (bait and switch)
- Response quality below expected model capability
- Latency much higher than claimed
- Provider goes offline mid-session
- Malicious provider returns garbage to extract payment

For Phase 4, trust is managed via a whitelist (web of trust). The operator manually curates trusted providers. Future phases add automated quality verification.

## Decision

**Marketplace intelligence as a cross-cutting service that feeds both Layer 2 and Layer 3.**

```
┌─────────────────────────────────────────────────────┐
│ MARKETPLACE SCRAPER                                  │
│                                                      │
│  Polls Routster at configurable interval (1-5 min)   │
│  Collects: provider prices, models, metadata         │
│  Stores in: routster_market table                    │
│                                                      │
│  Output:                                             │
│    price_board[model] = [{provider, price, trust}]   │
│                                                      │
└──────────────┬───────────────────┬───────────────────┘
               │                   │
    ┌──────────▼──────┐   ┌───────▼────────────┐
    │ LAYER 2 (SELL)  │   │ LAYER 3 (BUY)      │
    │                 │   │                    │
    │ "Cheapest       │   │ "Cheapest trusted  │
    │ competitor for  │   │ provider for this  │
    │ this model is   │   │ model is X at      │
    │ X at $Y. I      │   │ $Y. My own key     │
    │ should price    │   │ costs $Z. Use      │
    │ at $Y - 5% to   │   │ whichever is       │
    │ capture traffic."│   │ cheaper."          │
    └─────────────────┘   └────────────────────┘
```

### Price Board

The scraper maintains a live price board:

```python
price_board = {
    "glm-4.5-flash": [
        {"provider": "routster_abc", "price_per_1m": 0.045, "trust": "verified"},
        {"provider": "routster_def", "price_per_1m": 0.052, "trust": "verified"},
        {"provider": "routster_xyz", "price_per_1m": 0.038, "trust": "untrusted"},
    ],
    "glm-5.2": [
        {"provider": "routster_abc", "price_per_1m": 0.12, "trust": "verified"},
    ]
}
```

Layer 2 reads this to set competitive resale prices. Layer 3 reads this to make buy-vs-self-route decisions.

### Provider Reliability Tracking

Every Routster purchase is logged:

```sql
CREATE TABLE routster_purchases (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    announced_price REAL,
    charged_price REAL,
    tokens_in INTEGER,
    tokens_out INTEGER,
    latency_ms INTEGER,
    success INTEGER,          -- 1 if response valid, 0 if failed
    quality_score REAL,       -- future: automated quality check, NULL for now
    notes TEXT
);
```

Reliability score per provider:

```
reliability(provider) = SUM(success) / COUNT(*) over last 100 transactions
price_accuracy(provider) = avg(charged_price / announced_price)
```

### Trust System (Phase 4: whitelist)

```yaml
# config/trusted_providers.yaml
trusted:
  - routster_abc     # verified, good reliability
  - routster_def     # verified, decent reliability

untrusted:
  # everything not in trusted list is treated as untrusted
  # Layer 3 never routes to untrusted providers regardless of price
```

Future phases may add:
- Automated quality verification (compare output quality against expected model capability)
- Anomaly detection (price spikes, quality drops, latency patterns)
- Reputation scoring based on transaction history
- Malicious provider detection (consistent under-delivery, bait-and-switch pricing)

## Invariants

1. Layer 3 NEVER routes to an untrusted provider, regardless of price advantage.
2. Marketplace scraper runs at configurable interval (default: 2 min).
3. Price board is a SNAPSHOT — actual prices may differ at transaction time. Provider reliability tracking records the discrepancy.
4. Trust whitelist is explicit (opt-in). New providers start as untrusted until manually verified.
5. Reliability score uses exponential decay (recent transactions weighted higher).
6. Sell-side pricing considers BOTH own cost (Layer 1) AND competitor prices (price board). It prices to maximize profit, not just undercut.

## Consequences

### Positive
- Buy-side optimization: route to cheapest trusted Routster provider instead of own key when cheaper
- Sell-side optimization: price competitively based on real market data, not guesses
- Reliability tracking catches bad providers before they waste significant ecash
- Whitelist approach is simple and safe for initial deployment
- Marketplace data persists in DB — useful for analysis and future Kalman training

### Costs
- Adds Routster API dependency (scraper must handle API changes, rate limits)
- Whitelist maintenance is manual until automated quality verification is built
- Price board snapshots may be stale by the time a routing decision is made
- Reliability tracking requires recording every transaction outcome
- Dual mode complexity: operator must think about both sides simultaneously

### Future Roadmap

| Feature | Phase | Description |
|---------|-------|-------------|
| Whitelist trust | Phase 4 | Manual curation of trusted providers |
| Reliability scoring | Phase 4 | Automated from transaction history |
| Quality verification | Phase 5+ | Compare output quality to expected model capability |
| Malicious detection | Phase 5+ | Statistical anomaly detection on price/quality/latency |
| Reputation protocol | Phase 5+ | Publish/subcribe reputation events on Nostr |
| Demand elasticity Kalman | Phase 4 | Estimate demand curve from price/traffic observations |
