# Price-First Kalman Routing — Comprehensive Plan (v2)

**Date**: 2026-07-25
**Author**: Felix (operator) + Hermes
**Status**: AWAITING APPROVAL
**Repo**: `merchant-routing-engine/`

---

## 1. PROBLEM

The live proxy (`zai_proxy.py`) routes API requests through a hardcoded cascade. Price is not the primary input. Peak hours are a hardcoded routing check, not a cost signal. A `route_request()` function that uses price-first routing already exists but is not wired to key selection.

The operator wants all routing decisions driven by price signals, with multiple Kalman filters feeding a routing optimizer. The module must be standalone — others can point their Hermes agents at it. Future: supports Routster nodes (resell LLM access for ecash).

---

## 2. THREE-LAYER ARCHITECTURE

### Layer 1 — Cost Tracking (per key, private to key owner)

**Actor**: Whoever owns the API key.

A Kalman filter estimates the smooth component of the effective cost per million tokens. The filter tracks base_rate and its velocity — the amortized cost that changes slowly as tokens accumulate against a subscription.

**Peak hours are NOT a Kalman input.** Peak multiplier is a deterministic step function of time, applied AFTER the Kalman output. This preserves instant step changes when peak starts/ends. The Kalman is responsible only for the smooth trend underneath.

> **NOTE**: Peak multiplier, scarcity factor, and health factor are DETERMINISTIC multipliers applied OUTSIDE the Kalman filters. They are NOT Kalman state. This preserves instantaneous step changes (e.g., peak hour boundary) without Kalman smoothing lag.

```
                    ┌─────────────────────┐
                    │   Cost Kalman       │
                    │  [base_rate,        │
                    │   rate_velocity]    │
                    └────────┬────────────┘
                             │ base_rate (smooth)
                             ▼
               ┌──────────────────────────────┐
               │  DETERMINISTIC MULTIPLIER    │
               │  LAYER  (pricing_engine.py)  │
               │                              │
               │  × peak_mult(clock)          │  ← step function (instant)
               │  × scarcity_mult(quota)      │  ← ramp function (5min)
               │  × health_mult(circuit)      │  ← instant (breaker trip)
               └──────────────┬───────────────┘
                              │ effective_price
                              ▼
               ┌──────────────────────────────┐
               │  Routing Optimizer           │
               │  (sort by effective_price,   │
               │   filter unhealthy/exhausted)│
               └──────────────────────────────┘
```

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

## 3. EFFECTIVE PRICE SYSTEM

### 3.1 Multi-Kalman Design

> **NOTE**: Peak multiplier, scarcity factor, and health factor are DETERMINISTIC multipliers applied OUTSIDE the Kalman filters. They are NOT Kalman state. This preserves instantaneous step changes (e.g., peak hour boundary) without Kalman smoothing lag.

The system uses two distinct Kalman filter types, each with a narrow state vector:

**Base-Rate Kalman** — estimates the slow amortized cost per provider/key:

```
State: [base_rate, rate_velocity]
  base_rate     = subscription_fee / cumulative_tokens_this_cycle × 1e6
  rate_velocity = d(base_rate)/dt  (cost decreasing as tokens accumulate)

Observes: tokens_this_cycle, billing_cost
Updates:  on each request completion
Output:   base_rate_now (smooth amortized cost per M tokens)
```

**Consumption Kalman** — predicts burn rate and quota exhaustion (extracted from `burn_predictor.py`):

```
State: [burn_rate, burn_acceleration]
  burn_rate        = tokens_per_hour (current)
  burn_acceleration = d(burn_rate)/dt

Observes: tokens consumed per request, timestamps
Retrained: batch retrain from history on each call
Output:   predicted_exhaustion_time, quota_remaining_at_time(t)
```

**Multiplier Layer** (deterministic, in `pricing_engine.py`):

```
peak_mult(t)     = 3.0 if UTC hour in {6,7,8,9} else 1.0     [step function]
scarcity_mult(q) = 1 + max(0, (quota_used_pct - 50) / 50)     [ramp 1.0→2.0]
health_mult(c)   = ∞ if circuit breaker tripped else 1.0       [instant]
```

The multiplier layer sits between Kalman outputs and the routing optimizer. It is NOT part of any Kalman state.

### 3.2 Why Two Kalman Types (Different Roles, NOT Time Constants)

The two Kalman types are separated because they estimate fundamentally **different quantities**, not because they evolve at different speeds. Price CAN be abrupt (peak hour is a step change) — that is exactly why peak cannot be inside the Kalman.

**Base-Rate Kalman** estimates the slow amortized cost (`subscription_fee / cumulative_tokens`). This IS smooth — it amortizes monotonically as tokens accumulate within a billing cycle. Kalman filtering genuinely reduces noise on this signal.

**Consumption Kalman** predicts burn rate and exhaustion (the existing `burn_predictor.py`, extracted). It is batch-retrained on each call from history. Burn rate is stochastic and bursty, but the Kalman smooths the prediction to avoid overreacting to individual requests.

**Peak, scarcity, and health are NOT Kalman state.** They are deterministic functions applied as multipliers AFTER the Kalman produces `base_rate`. The effective price steps instantly when peak hours begin or a circuit breaker trips — this is by design. The operator wants immediate routing response to these events, not Kalman-lagged gradual transitions.

| Kalman Type | State Vector | What It Tracks | Smooth? | Retraining |
|---|---|---|---|---|
| Base-Rate | `[base_rate, rate_velocity]` | Amortized cost per M tokens | Yes (monotonic) | Online (per request) |
| Consumption | `[burn_rate, burn_acceleration]` | Token burn & exhaustion time | Somewhat (bursty) | Batch (per call) |

### 3.3 Effective Price Formula

```
effective_price(provider, t) =
    base_rate(provider, t)          ← from Base-Rate Kalman (smooth)
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

> **IMPORTANT**: `base_rate` is the ONLY Kalman-smoothed term. `peak_multiplier`, `scarcity_factor`, and `health_factor` are all deterministic functions applied instantaneously. `effective_price` can step-change instantly when peak hours begin/end or when a circuit breaker trips. This is intentional — the operator wants immediate routing response to these events, not Kalman-lagged transitions.

### 3.4 Three Operation Modes — Consumer, Merchant, Arbiter

The module supports three distinct operation modes, because the optimization problem differs by role.

**Mode 1: Consumer** (runs in Hermes proxy):

```
Input:  all owned-provider prices + quotas + health
Pipeline:
  Base-Rate Kalman ──→ base_rate per provider
  Consumption Kalman ──→ exhaustion prediction per provider
  Deterministic Pricing Engine ──→ effective_price per provider
  Routing Optimizer ──→ cheapest viable provider
Output: {provider, key, model, effective_price}
Question: "Which of my own providers do I use?"
```

**Mode 2: Merchant** (runs in Routster sell node):

```
Input:  upstream costs (via Consumer System), competitor prices, demand signal
Pipeline:
  Consumer System ──→ cheapest upstream cost
  Demand Kalman ──→ demand curve estimate
  Profit Optimizer ──→ customer-facing price
Output: {customer_price, upstream_cost, expected_profit}
Question: "What do I charge customers?"
```

**Mode 3: Arbiter** (buys AND sells simultaneously):

```
Input:  own keys + Routster network providers + competitor sell prices + demand
Pipeline:
  Network Scraper ──→ competitor buy/sell prices from Routster
  Provider Whitelist ──→ trusted npubs (web of trust)
  Routing Optimizer ──→ cheapest viable upstream (own key OR network provider)
    IF own_key cheaper: serve internally, sell excess to network
    IF network cheaper: buy from network for internal use
  Profit Optimizer ──→ sell price (based on cheapest upstream + margin)
  Reliability Tracker ──→ per-provider delivery outcomes (roadmap)
Output: buy_decision + sell_price (both continuously updated)
Question: "Do I buy from network or use my own keys? And what do I charge?"
```

The arbiter mode is the natural endpoint: a Routster node operator isn't just a seller. They also consume LLM access for their own Hermes agents, cron jobs, and workers. When network providers offer cheaper rates than their own keys, they buy from the network. When their own keys are cheaper, they serve their own traffic internally and sell excess capacity to the network.

**Near-term trust**: manually-maintained whitelist of trusted provider npubs. No untrusted provider gets traffic.

**Roadmap trust**: automated quality verification — statistical comparison of delivered responses against expected model benchmarks. Detects bait-and-switch (advertising glm-5.2 but serving a cheaper model) via latency distribution, token count distribution, and content quality scoring. Malicious providers get auto-blacklisted.

**Key insight**: all three modes share the same Routing Optimizer core. The difference is what's in the candidate pool (own keys only vs own keys + network providers) and whether a profit layer sits on top.

### 3.5 Profit Optimization (Routster)

Profit is NOT just margin per request. It is:

```
profit = traffic(price) × (price - cost)
```

Being cheaper than competitors increases traffic volume, which increases total profit even at lower per-unit margin. The Demand Kalman captures this elasticity.

**Demand Kalman** — state `[demand_rate, price_elasticity]`, observes request volume at different price points:

```
demand_at_price(P) = demand_rate × (P / reference_price) ^ price_elasticity

State: [demand_rate, price_elasticity]
Observes: (price_offered, traffic_received) pairs per hour
Output:   predicted traffic at any candidate price P
```

**Profit Optimizer** — finds the price that maximizes total profit:

```
customer_price = argmax(demand_at_price(P) × (P - upstream_cost))
                 P
```

The optimizer respects a ceiling (must be below cheapest competitor to capture traffic) and a floor (must exceed upstream_cost).

**Profit Tracking Table** — per-request profit is recorded and aggregatable per-key, per-provider, per-hour:

```sql
CREATE TABLE IF NOT EXISTS profit_log (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    provider TEXT,
    key_id TEXT,
    customer_price REAL,    -- what customer paid (ecash)
    upstream_cost REAL,     -- effective_price paid upstream
    profit REAL,            -- customer_price - upstream_cost
    tokens INTEGER
);
```

**For internal use (no Routster yet)**: "profit" = savings vs next-cheapest alternative. If the engine routes to a provider at $0.069/M when the next option is $0.280/M, the "profit" (savings) is $0.211/M. This validates the engine's value even without external customers.

### 3.6 Entry Points

Three clean entry points for three operation modes:

```python
# ── Mode 1: Consumer entry point (Hermes proxy) ──────────────
from merchant_routing import RoutingOptimizer
router = RoutingOptimizer(config="providers.yaml")
decision = router.route(estimated_tokens=1000, difficulty="medium")
# → {provider: "ollama_cloud", effective_price: 0.069, reason: "cheapest_viable"}

# ── Mode 2: Merchant entry point (Routster sell node) ────────
from merchant_routing import ProfitOptimizer
merchant = ProfitOptimizer(config="providers.yaml", margin_strategy="elastic")
price = merchant.set_price(model="glm-5.2")
# → {customer_price: 0.12, upstream_cost: 0.069, expected_profit: 0.051}

# ── Mode 3: Arbiter entry point (buy + sell simultaneously) ──
from merchant_routing import ArbiterNode
node = ArbiterNode(
    config="providers.yaml",
    routster_config="routster.yaml",
    trusted_npubs=["npub1...", "npub1..."],  # web of trust whitelist
)
# Routes internal traffic to cheapest upstream (own keys OR network)
buy_decision = node.route_internal(estimated_tokens=1000)
# → {source: "network", provider: "npub1abc...", effective_price: 0.05, cheaper_than_own: True}

# Sets sell price based on cheapest upstream + demand-aware margin
sell_price = node.set_sell_price(model="glm-5.2")
# → {sell_price: 0.08, cheapest_upstream: 0.05, expected_margin: 0.03}
```

---

## 4. WHY THREE LAYERS (NOT ONE KALMAN)

1. **Different actors**: Seller owns Layers 1+2. Buyer owns Layer 3. A seller can publish Layer 3 as a routing module without revealing their cost structure.

2. **Different roles, not time scales**: Base-rate and consumption estimate fundamentally different quantities (financial amortization vs physical burn rate). Peak, scarcity, and health are NOT estimation problems at all — they are deterministic functions. Cramming them into one state vector forces a single process model that's wrong for all dimensions.

3. **Step changes must be instant**: Peak hours, circuit breakers, and rate-limit resets are step functions. Kalman filters smooth step changes by design. By keeping these as deterministic multipliers OUTSIDE the Kalman, we preserve instant response. The operator explicitly wants immediate routing response to peak transitions, not gradual shifts.

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

`price_kalman.py` estimates ONLY `base_rate` (amortized cost), NOT the full `effective_price`. The deterministic multipliers (peak, scarcity, health) are in `pricing_engine.py`, which sits between the Kalman and the optimizer.

1. `config/providers.yaml` — all providers with pricing models
2. `src/price_kalman.py` — Base-Rate Kalman: 2-state `[base_rate, rate_velocity]` per provider/key
3. `src/consumption_kalman.py` — Consumption Kalman: `[burn_rate, burn_acceleration]`, extracted from `burn_predictor.py`
4. `src/pricing_engine.py` — deterministic multiplier layer: applies peak/scarcity/health multipliers to Kalman `base_rate` to produce `effective_price`
5. `src/routing_optimizer.py` — deterministic cost minimizer (sorts by `effective_price`)
6. `src/shadow_logger.py` — read-only tap into proxy
7. Tests: price_kalman + pricing_engine + routing_optimizer
8. Wire shadow_logger into zai_proxy.py (read-only)
9. Run 48h, validate

### Phase 2 — Advisor Mode (low risk, hot-swappable)

1. Modify do_POST: call routing_optimizer first, `best_key()` fallback
2. Remove hardcoded peak-hour check
3. Run 72h, compare

### Phase 3 — Primary Mode (the goal)

1. Remove `best_key()` entirely
2. Routing optimizer is the only router
3. Remove cascade logic

### Phase 4 — Standalone Module + Routster

1. `src/demand_kalman.py` — Demand Kalman: `[demand_rate, price_elasticity]`, estimates demand curve from `(price, traffic)` observations
2. `src/profit_tracker.py` — per-request profit logging + aggregation (per-key, per-provider, per-hour)
3. `src/margin_layer.py` — profit optimizer with demand Kalman
4. Pip-installable package
5. Example configs: Hermes, TollGate, Routster
6. Full documentation

---

## 8. FILE MAP

```
merchant-routing-engine/
├── config/
│   ├── providers.yaml
│   └── strategy.yaml
├── src/
│   ├── price_kalman.py             # Base-Rate Kalman (amortized cost)
│   ├── consumption_kalman.py       # Consumption Kalman (burn rate)
│   ├── pricing_engine.py           # DETERMINISTIC multipliers (peak/scarcity/health)
│   ├── routing_optimizer.py        # sort & filter by effective_price
│   ├── shadow_logger.py            # read-only tap for Phase 1
│   ├── config_loader.py
│   ├── demand_kalman.py            # Phase 4: Routster demand estimation
│   ├── profit_tracker.py           # Phase 4: Routster profit tracking per request
│   └── margin_layer.py             # Phase 4: profit optimizer
├── tests/
│   ├── test_price_kalman.py
│   ├── test_consumption_kalman.py
│   ├── test_pricing_engine.py
│   ├── test_routing_optimizer.py
│   └── test_shadow_logger.py
├── docs/
│   ├── price-first-kalman-plan.md  # This document
│   ├── decisions.md                # ADRs
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

---

## 9. RELATION TO EXISTING CODE

| Existing Code | Fate in Price-First Engine | Phase |
|---|---|---|
| `zai_proxy.py route_request()` | Replaced by `routing_optimizer.py` (price-based sort/filter) | Phase 1 (shadow), Phase 3 (primary) |
| `zai_proxy.py best_key()` | Phase 2: fallback when optimizer unavailable. Phase 3: removed entirely | Phase 2 → Phase 3 |
| `zai_proxy.py _is_peak_hour()` | Replaced by `peak_multiplier` in `pricing_engine.py` (NOT in price Kalman) | Phase 1 |
| `zai_proxy.py` hardcoded cascade | Removed. All routing via `effective_price` comparison | Phase 3 |
| `burn_predictor.py` | Extracted into `consumption_kalman.py` | Phase 1 |
| `key_health_tracker.py` | Feeds `health_factor` into `pricing_engine.py` | Phase 1 |
| `provider_funding_tracker.py` | Feeds `scarcity_factor` into `pricing_engine.py` | Phase 1 |
| Cost matrix in `zai_proxy.py` | Replaced by `base_rate` computation in `price_kalman.py` | Phase 1 |
