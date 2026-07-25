# Price-First Kalman Routing — Comprehensive Plan (v2)

**Date**: 2025-07-25
**Author**: Felix (operator) + Hermes
**Status**: AWAITING APPROVAL
**Repo**: `merchant-routing-engine/`

---

## 1. PROBLEM

The live proxy (`zai_proxy.py`) routes API requests through a hardcoded cascade. Price is not the primary input. Peak hours are a hardcoded routing check, not a cost signal. A `route_request()` function that uses price-first routing already exists but is not wired to key selection.

The operator wants all routing decisions driven by price signals, with multiple Kalman filters feeding a routing optimizer. The module must be standalone — others can point their Hermes agents at it. Future: supports Routster nodes (resell LLM access for ecash).

## 2. THREE-LAYER ARCHITECTURE

### Layer 1 — Cost Tracking (per key, private to key owner)

**Actor**: Whoever owns the API key.

A Kalman filter estimates the smooth component of the effective cost per million tokens. The filter tracks base_rate and its velocity — the amortized cost that changes slowly as tokens accumulate against a subscription.

**Peak hours are NOT a Kalman input.** Peak multiplier is a deterministic step function of time, applied AFTER the Kalman output. This preserves instant step changes when peak starts/ends. The Kalman is responsible only for the smooth trend underneath.

```
State: [base_rate, rate_velocity]
Observes: tokens_this_cycle, billing_cost, quota_used_pct
Outputs: base_rate_now, base_rate_30min, base_rate_2h

Effective cost = base_rate × peak_multiplier(clock)  [deterministic]
                          × scarcity_factor(quota)     [deterministic]
                          × health_factor(circuit)     [deterministic]
```

**INVARIANT: effective_cost > 0 always.** Even for "free" providers, base_rate must be a small epsilon ($0.001/M) to avoid division-by-zero.

### Layer 2 — Pricing Decision (merchant / Routster node)

**Actor**: The Routster node operator who resells LLM access.

This layer is NOT a Kalman. It's a constrained optimization: maximize profit = traffic × (price - cost).

A **Demand Kalman** sub-component estimates the demand curve from noisy (price, traffic) observations:

```
State: [demand_intercept, demand_slope]
demand(price) = intercept + slope × price
Observes: (price_offered, traffic_received) pairs
Output: estimated demand curve
```

The profit optimizer uses the estimated demand curve + competitor prices to find the profit-maximizing price:

```
profit(price) = demand(price) × (price - upstream_cost)
optimal_price = argmax(profit)
```

**Constraint**: optimal_price must be competitive. If higher than cheapest competitor, traffic drops to near-zero. The demand Kalman captures this relationship.

**Output**: announced_price — PUBLIC, announced to customers.

### Layer 3 — Routing Decision (customer / Hermes agent)

**Actor**: The buyer who chooses which merchant/provider to use.

This layer is NOT a Kalman. It's a deterministic sort-and-filter:

1. Collect announced prices from all available providers
2. Filter: remove exhausted, unhealthy, quality-too-low
3. Sort: by effective_price ascending
4. Pick cheapest viable

**Output**: chosen provider + model + expected cost.

---

## 3. EFFECTIVE PRICE FORMULA

```
effective_price(provider, t) =
    base_rate(provider, t)          ← from Cost Kalman (smooth)
  × peak_multiplier(t)              ← deterministic step function (instant)
  × scarcity_factor(provider, t)    ← deterministic (updates every 5min with quota)
  × health_factor(provider)         ← deterministic (instant on circuit breaker)
```

**Peak multiplier**: 3.0 during UTC hours [6,7,8,9], 1.0 otherwise. Step change, not smoothed.

**Scarcity factor**: `1 + max(0, (quota_used_pct - 50) / 50)`. Ramps from 1.0 at 50% quota to 2.0 at 100%.

**Health factor**: `∞` if circuit breaker tripped, `1.0` otherwise. Instant change.

**Base rate by provider type:**

| Type | Formula | Example |
|------|---------|---------|
| Flat-rate subscription | `subscription_cost / tokens_used_this_cycle` | z.ai ours: €155 / 2.3B = $0.068/M |
| Flat-rate shared | `upstream_rate × (1 + penalty_pct)` | z.ai friend: $0.068 × 1.21 = $0.082/M |
| Per-token | `fixed_rate` (constant) | PPQ: $0.280/M |
| Cloud flat-rate | `monthly_cost / tokens_used` | Ollama: $100 / 1.45B = $0.069/M |

As tokens increase, subscription rate decreases (amortization). This is the smooth trend the Kalman tracks.

---

## 4. WHY THREE LAYERS (NOT ONE KALMAN)

1. **Different actors**: Seller owns Layers 1+2. Buyer owns Layer 3. A seller can publish Layer 3 as a routing module without revealing their cost structure.

2. **Different time scales**: Base rate evolves over hours (amortization). Peak/health change instantly. Demand curve evolves over days (market behavior). Cramming all into one state vector forces a single process model that's wrong for all dimensions.

3. **Step changes**: Peak hours, circuit breakers, and rate-limit resets are step functions. Kalman filters smooth step changes. By keeping these as deterministic multipliers OUTSIDE the Kalman, we preserve instant response.

4. **Separation of concerns**: Cost Kalman doesn't need to know about demand. Pricing optimizer doesn't need to know about burn rate. Routing optimizer doesn't need to know about subscription costs.

---

## 5. SHADOW MODE VALIDATION

Before any routing changes, run the price-first engine in parallel:

```
Request → best_key() [LIVE] → z.ai
       → price_engine() [SHADOW] → log decision, don't route
```

New table `routing_shadow_decisions`:

```sql
CREATE TABLE IF NOT EXISTS routing_shadow_decisions (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    live_provider TEXT,
    live_key TEXT,
    live_model TEXT,
    shadow_provider TEXT,
    shadow_key TEXT,
    shadow_model TEXT,
    shadow_effective_price REAL,
    shadow_predicted_cost_usd REAL,
    is_peak_hour INTEGER,
    token_estimate INTEGER,
    same_decision INTEGER,
    would_save_usd REAL,
    reason TEXT
);
```

**Validation criteria (48h):**
- Shadow engine produces decisions for >99% of requests
- When shadow disagrees, its effective_price is lower
- No regression in live proxy performance (<1ms added)

---

## 6. PROFIT TRACKING

For current setup (no Routster): track cost per key.

```
cost_per_key_per_day = subscription_allocation(key)
profit_per_key = 0  (no revenue yet — pure consumer)
```

For Routster setup:

```
profit_per_key = customer_revenue(key) - upstream_cost(key)
customer_revenue = SUM(ecash_received for requests through this key)
upstream_cost = subscription_fee / tokens_used

profit_per_hour = demand(current_price) × (current_price - upstream_cost)
```

Demand Kalman estimates the demand curve. Profit = traffic × margin. Maximizing profit requires knowing both.

---

## 7. IMPLEMENTATION PHASES

### Phase 1 — Shadow Mode (safe, no production changes)

1. `config/providers.yaml` — all providers with pricing models
2. `src/price_kalman.py` — 2-state Kalman per provider (base_rate + velocity)
3. `src/consumption_kalman.py` — extracted from burn_predictor.py
4. `src/routing_optimizer.py` — deterministic cost minimizer
5. `src/shadow_logger.py` — read-only tap into proxy
6. Tests: price_kalman + routing_optimizer
7. Wire shadow_logger into zai_proxy.py (read-only)
8. Run 48h, validate

### Phase 2 — Advisor Mode (low risk, hot-swappable)

1. Modify do_POST: call routing_optimizer first, best_key() fallback
2. Remove hardcoded peak-hour check
3. Run 72h, compare

### Phase 3 — Primary Mode (the goal)

1. Remove best_key() entirely
2. Routing optimizer is the only router
3. Remove cascade logic

### Phase 4 — Standalone Module + Routster

1. `src/margin_layer.py` — profit optimizer with demand Kalman
2. `src/demand_kalman.py` — estimates demand curve from observations
3. Pip-installable package
4. Example configs: Hermes, TollGate, Routster
5. Full documentation

---

## 8. FILE MAP

```
merchant-routing-engine/
├── config/
│   ├── providers.yaml
│   └── strategy.yaml
├── src/
│   ├── price_kalman.py
│   ├── consumption_kalman.py
│   ├── routing_optimizer.py
│   ├── shadow_logger.py
│   ├── demand_kalman.py        # Phase 4
│   ├── margin_layer.py          # Phase 4
│   └── config_loader.py
├── tests/
│   ├── test_price_kalman.py
│   ├── test_consumption_kalman.py
│   ├── test_routing_optimizer.py
│   └── test_shadow_logger.py
├── docs/
│   ├── price-first-kalman-plan.md  # This document
│   ├── architecture.md
│   └── adr/
│       ├── ADR-001-price-first-routing.md
│       ├── ADR-002-multi-kalman-separation.md
│       ├── ADR-003-deterministic-peak-multiplier.md
│       ├── ADR-004-effective-price-positivity.md
│       ├── ADR-005-three-layer-actor-separation.md
│       └── ADR-006-shadow-mode-validation.md
├── examples/
│   ├── hermes-config.yaml
│   ├── tollgate-config.yaml
│   └── routster-config.yaml
├── README.md
└── pyproject.toml
```
