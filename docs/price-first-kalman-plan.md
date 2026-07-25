# Price-First Kalman Routing — Comprehensive Plan

**Date**: 2025-07-25
**Author**: Felix (operator) + Hermes
**Status**: AWAITING APPROVAL
**Repo**: `merchant-routing-engine/`

---

## 1. PROBLEM

The live proxy (`zai_proxy.py`) routes API requests through a hardcoded cascade:

```
1. If peak hour → try Ollama Cloud first (hardcoded)
2. best_key() → Kalman predicts ours-vs-friend exhaustion
3. If both exhausted → try Ollama Cloud again
4. If still failing → external failover (PPQ/OpenRouter)
```

**Price is not the primary input.** Peak hours are a hardcoded routing check, not a cost signal. A `route_request()` function that DOES use price-first routing already exists in `burn_predictor.py` (line 524) but is NOT called for key selection — only for model tier downgrade.

The operator wants: **all routing decisions driven by price signals**, with multiple Kalman filters feeding a routing optimizer.

## 2. GOAL

Replace the cascade with a price-first routing engine where:

- Each provider has a **Price Kalman** that estimates effective cost per million tokens
- Each provider has a **Consumption Kalman** that predicts burn rate and exhaustion
- A **Routing Optimizer** (not a Kalman) picks the cheapest viable provider
- The whole thing runs in **shadow mode** first, logging decisions alongside the live system
- The module is **standalone** — others can point their Hermes agents at it
- Future: supports **Routster nodes** (resell LLM access for ecash with margin)

## 3. ARCHITECTURE

### 3.1 Multi-Kalman Design

```
┌──────────────────────────────────────────────────────────────┐
│                         CONFIG                                │
│  providers.yaml — endpoints, pricing models, quota URLs      │
│  strategy.yaml — quality thresholds, peak hours, margin      │
└──────────────┬───────────────────────────────────────────────┘
               │
    ┌──────────┴──────────┐
    ▼                     ▼
┌────────────────┐  ┌──────────────────────┐
│ PRICE KALMAN ×N │  │ CONSUMPTION KALMAN×N │
│ one per provider│  │ one per provider     │
│                │  │                      │
│ STATE:          │  │ STATE:               │
│  effective_rate │  │  tokens_per_call     │
│  rate_velocity  │  │  request_rate        │
│                │  │  burn_acceleration    │
│ OBSERVES:       │  │                      │
│  tokens_this_cycle│ │ OBSERVES:           │
│  billing_cost   │  │  tokens last call    │
│  quota_used_pct │  │  calls last 5min     │
│  time_of_day    │  │  quota_used_pct      │
│                │  │                      │
│ OUTPUTS:        │  │ OUTPUTS:             │
│  rate_now       │  │  predicted_burn_30m  │
│  rate_30min     │  │  exhausts_in_hours   │
│  rate_2h        │  │  will_exhaust_window │
└───────┬────────┘  └──────────┬───────────┘
        │                      │
        └──────────┬───────────┘
                   ▼
        ┌─────────────────────┐
        │  ROUTING OPTIMIZER   │
        │  (deterministic)     │
        │                     │
        │  For each provider:  │
        │   cost = price_kalman│
        │         .rate_now    │
        │   viable = not       │
        │     exhausted AND    │
        │     healthy          │
        │                     │
        │  Pick min(cost)      │
        │    among viable      │
        │                     │
        │  OUTPUT:             │
        │   provider + model   │
        │   + effective_price  │
        │   + reason           │
        └─────────────────────┘
```

### 3.2 Why Two Kalman Layers

A single Kalman filter can't model two systems with different time constants:
- **Price** evolves over hours (subscription amortization, peak transitions)
- **Consumption** evolves over minutes (task bursts, model selection)

Forcing them into one state vector means the filter uses a single process noise model that's wrong for both. Price gets jerked by consumption spikes. Consumption gets smeared by slow price drift.

Separate filters allow:
- Different update rates (price: 5min, consumption: per-call)
- Independent failure (a 429'd provider's consumption Kalman goes dark, but its price Kalman keeps predicting from quota data)
- Clean extensibility (adding a provider = new filter instances)

### 3.3 Effective Price Formula

```
effective_price(provider, t) =
    base_rate(provider, t)
  × peak_multiplier(t)              # 3.0 during UTC 6-9, 1.0 otherwise
  × scarcity_factor(provider, t)    # 1.0 at 0% quota → 3.0 at 100%
  × health_factor(provider)         # 1.0 healthy, ∞ if circuit breaker tripped
```

**Base rate by provider type:**

| Type | Formula | Example |
|------|---------|---------|
| Flat-rate subscription | `subscription_cost / tokens_used_this_cycle` | z.ai ours: €155 / 2.3B = $0.068/M |
| Flat-rate shared | `upstream_rate × (1 + penalty_pct)` | z.ai friend: $0.068 × 1.21 = $0.082/M |
| Per-token | `fixed_rate` (constant) | PPQ: $0.280/M |
| Cloud flat-rate | `monthly_cost / tokens_used` | Ollama: $100 / 1.45B = $0.069/M |
| Local | `electricity_cost` | Ollama local: ~$0 |

**Scarcity factor:**

```
scarcity = 1 + max(0, (quota_used_pct - 50) / 50)
```

At 50% quota: scarcity = 1.0 (no penalty)
At 75% quota: scarcity = 1.5 (50% premium)
At 100% quota: scarcity = 2.0 (double cost — opportunity cost of using remaining quota)

**Health factor:**

```
if circuit_breaker_tripped:
    health = infinity
elif recent_429_count > 3:
    health = 2.0
else:
    health = 1.0
```

**CRITICAL CONSTRAINT: effective_price > 0 always.** Even for "free" providers (local Ollama), base_rate must be a small epsilon (e.g., $0.001/M) to avoid division-by-zero in downstream calculations and to ensure the optimizer always has a valid comparison.

## 4. DATA SOURCES

### 4.1 Existing Data (already collected)

| Data | Source | Used For |
|------|--------|----------|
| API call tokens | `zai_usage.db: api_calls` | Consumption Kalman, price amortization |
| Quota percentages | z.ai quota API (polled every 5min) | Both Kalmans |
| Key health / 429s | `zai_usage.db: key_health` | Health factor |
| System resources | `zai_usage.db: system_readings` | Resource penalty |
| Provider funding | `zai_usage.db: provider_health` | Health factor |
| Model pricing | `model_matrix.json` (646 models) | Base rate lookup |
| Daily spend | `zai_usage.db: daily_spend` | Cost tracking |

### 4.2 New Data (shadow mode)

A new table `routing_shadow_decisions` in `zai_usage.db`:

```sql
CREATE TABLE IF NOT EXISTS routing_shadow_decisions (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    -- What the live system did
    live_provider TEXT,
    live_key TEXT,
    live_model TEXT,
    -- What the shadow engine recommended
    shadow_provider TEXT,
    shadow_key TEXT,
    shadow_model TEXT,
    shadow_effective_price REAL,
    shadow_predicted_cost_usd REAL,
    -- Context
    is_peak_hour INTEGER,
    token_estimate INTEGER,
    -- Comparison
    same_decision INTEGER,          -- 1 if live == shadow
    would_save_usd REAL,            -- cost difference if shadow had routed
    reason TEXT                     -- shadow engine's explanation
);
```

This lets us query: "How often did the shadow engine make a different choice? Would it have been cheaper? Did the live system's choice succeed?"

## 5. IMPLEMENTATION PHASES

### PHASE 1 — SHADOW MODE (safe, no production changes)

**Goal**: Build the price-first engine and validate it against live data.

**Deliverables:**

1. `config/providers.yaml` — clean provider definitions:
   ```yaml
   providers:
     zai_ours:
       type: flat_rate_subscription
       base_url: "https://api.z.ai/api/coding/paas/v4"
       key_env: "ZAI_OUR_KEY"
       monthly_cost_usd: 155  # €144 ≈ $155
       quota_api: "https://api.z.ai/api/monitor/usage/quota/limit"
       peak_hours_utc: [6, 7, 8, 9]
       peak_multiplier: 3.0
     
     zai_friend:
       type: flat_rate_shared
       base_url: "https://api.z.ai/api/coding/paas/v4"
       key_env: "ZAI_API_KEY"
       upstream: zai_ours
       penalty_pct: 21
     
     ollama_cloud:
       type: cloud_flat_rate
       base_url: "https://ollama.com/v1"
       key_env: "OLLAMA_CLOUD_API_KEY"
       monthly_cost_usd: 100
       rate_limit_reset: daily
     
     ppq:
       type: per_token
       base_url: "https://api.ppq.ai/v1"
       key_env: "PPQ_API_KEY"
       cost_per_1m_input: 0.09
       cost_per_1m_output: 0.19
     
     openrouter:
       type: per_token
       base_url: "https://openrouter.ai/api/v1"
       key_env: "OPENROUTER_API_KEY"
       cost_per_1m_input: 0.09
       cost_per_1m_output: 0.18
   
   strategy:
     quality_thresholds:
       simple: 60
       medium: 75
       complex: 85
     min_effective_price: 0.001  # never zero
   ```

2. `src/price_kalman.py` — one Kalman per provider:
   - State: `[effective_rate, rate_velocity]`
   - Observes: tokens_this_cycle, billing_cost, quota_pct, time_of_day
   - Outputs: `rate_now`, `rate_30min`, `rate_2h`
   - Uses existing Kalman math from `burn_predictor.py` (2-state filter)
   - Peak multiplier is a deterministic time function, NOT a Kalman input
   - Scarcity factor derived from quota observations

3. `src/consumption_kalman.py` — extracted from `burn_predictor.py`:
   - State: `[tokens_per_call, request_rate, acceleration]`
   - Observes: tokens per call, calls per minute, quota burn
   - Outputs: `predicted_burn_30m`, `exhausts_in_hours`, `will_exhaust`
   - This is the existing burn predictor, cleaned up and made provider-agnostic

4. `src/routing_optimizer.py` — deterministic optimizer:
   - Input: price outputs + consumption outputs from all providers
   - Filters: remove exhausted, unhealthy, quality-too-low
   - Sort: by effective_price ascending
   - Output: `{provider, key, model, effective_price, reason}`
   - **CRITICAL**: `min_effective_price = 0.001` — never return zero

5. `src/shadow_logger.py` — taps into live proxy:
   - Hooks into `zai_proxy.py do_POST` (read-only, no routing changes)
   - For each request: logs live decision + computes shadow decision
   - Writes to `routing_shadow_decisions` table
   - Runs in a thread to not slow down the proxy

6. `tests/test_price_kalman.py` — unit tests:
   - Verify price decreases as tokens consumed increases (amortization)
   - Verify peak multiplier applied correctly
   - Verify scarcity ramps from 1.0 to 2.0
   - Verify health factor returns infinity when circuit breaker tripped
   - Verify effective_price is always > 0

7. `tests/test_routing_optimizer.py` — integration tests:
   - Given mock price + consumption outputs, verify correct provider selected
   - Verify cheapest viable provider wins
   - Verify exhausted providers filtered out
   - Verify quality threshold enforced

**Validation criteria (48h of shadow data):**
- Shadow engine produces a decision for >99% of requests (no crashes)
- Shadow decisions match live decisions >70% of the time (sanity check)
- When shadow disagrees, shadow's effective_price is lower (or same quality at lower cost)
- No regression in live proxy performance (shadow logging adds <1ms)

**Effort**: 1-2 worker sessions. No production risk.

---

### PHASE 2 — ADVISOR MODE (low risk, hot-swappable)

**Goal**: Wire the shadow engine into the proxy as a primary signal with fallback.

**Deliverables:**

1. Modify `zai_proxy.py do_POST` (line ~1446):
   ```python
   # OLD:
   chosen = best_key()
   
   # NEW:
   try:
       decision = routing_optimizer.route(
           estimated_tokens=0,
           difficulty="medium"
       )
       chosen = decision["key"]
       chosen_model = decision["model"]
       chosen_base_url = decision.get("base_url", UPSTREAM)
   except Exception:
       chosen = best_key()  # proven fallback
       chosen_model = None
   ```

2. Remove the hardcoded peak-hour check (line 1439-1443):
   - The price Kalman already accounts for peak hours via peak_multiplier
   - Ollama's lower effective price during peak naturally routes there

3. Remove the hardcoded Ollama-first check (line 1426-1429):
   - Ollama-only models still route to Ollama (no z.ai equivalent)
   - But regular models during peak hours route based on price

4. Keep `best_key()` as the exception fallback
5. Keep the external failover cascade as the ultimate fallback

**Validation criteria (72h of advisor data):**
- Zero increase in failed requests
- Zero increase in PPQ/OpenRouter spend (the expensive providers)
- Provider distribution shifts toward cheaper providers
- No oscillation (provider switching more than every 5 min)

**Rollback procedure:**
```bash
# If anything goes wrong:
git checkout HEAD~1 -- ~/.hermes/bot/zai_proxy.py
systemctl --user restart zai-proxy
# Or: set fallback_providers back to the old config
```

**Effort**: 1 worker session. Low risk — exception handler falls back.

---

### PHASE 3 — PRIMARY MODE (the goal)

**Goal**: Remove `best_key()`, routing optimizer is the only decision engine.

**Deliverables:**

1. Remove `best_key()` from `zai_proxy.py`
2. Remove the cascade logic (peak-hour, ollama-first, etc.)
3. The proxy's `do_POST` becomes:
   ```python
   decision = routing_optimizer.route(request)
   if decision is None:
       # All providers exhausted
       send_503()
       return
   forward_to(decision["base_url"], decision["key"], decision["model"])
   ```
4. Clean up unused code (key health tracker inline, external failover inline)

**Validation criteria:**
- Same success rate as Phase 2
- Same or lower cost
- All providers accessible
- Dashboard shows price-driven routing decisions

**Effort**: 1 worker session. Medium risk — removing the safety net.

---

### PHASE 4 — STANDALONE MODULE + ROUTSTER SUPPORT

**Goal**: Make the module shareable and add ecash resale.

**Deliverables:**

1. `merchant-routing-engine/` is a pip-installable package
2. Clean config file format — anyone can define their providers
3. `src/margin_layer.py` — for Routster nodes:
   ```python
   def compute_customer_price(
       upstream_price: float,      # from routing optimizer
       margin_pct: float,          # configurable, e.g., 30%
       ecash_fee_per_request: int, # sats
       demand_signal: float,       # from demand Kalman
   ) -> dict:
       base_margin = upstream_price * (1 + margin_pct / 100)
       demand_adjusted = base_margin * demand_multiplier(demand_signal)
       return {
           "price_per_1m": demand_adjusted,
           "ecash_fee": ecash_fee_per_request,
           "total_per_request": demand_adjusted * est_tokens / 1e6 + ecash_fee
       }
   ```
4. Full documentation: README, quickstart, provider config guide
5. Example configs for: Hermes agent, TollGate router, Routster node
6. Dashboard integration — show effective prices per provider in real-time

**Effort**: 2 worker sessions. No production risk — pure documentation + packaging.

## 6. TASK BREAKDOWN FOR KANBAN

### Phase 1 Tasks (shadow mode)

| ID | Task | Worker Model | Est |
|----|------|-------------|-----|
| P1.1 | Write `config/providers.yaml` with all 5 providers + pricing models | glm-4.5-flash | 15min |
| P1.2 | Write `src/price_kalman.py` — 2-state Kalman per provider | glm-5.2 | 45min |
| P1.3 | Write `src/consumption_kalman.py` — extracted from burn_predictor | glm-5.2 | 30min |
| P1.4 | Write `src/routing_optimizer.py` — deterministic cost minimizer | glm-5.2 | 30min |
| P1.5 | Write `src/shadow_logger.py` — tap into proxy, log decisions | glm-5.2 | 30min |
| P1.6 | Write tests: price_kalman + routing_optimizer | glm-5.2 | 30min |
| P1.7 | Wire shadow_logger into zai_proxy.py (read-only hook) | glm-5.2 | 20min |
| P1.8 | Run for 48h, collect shadow data, write validation report | — | 48h |

### Phase 2 Tasks (advisor mode)

| ID | Task | Worker Model | Est |
|----|------|-------------|-----|
| P2.1 | Modify do_POST to call routing_optimizer first, best_key() fallback | glm-5.2 | 30min |
| P2.2 | Remove hardcoded peak-hour check from do_POST | glm-5.2 | 10min |
| P2.3 | Test: verify all providers still reachable, no regressions | glm-5.2 | 30min |
| P2.4 | Run for 72h, collect advisor data, write comparison report | — | 72h |

### Phase 3 Tasks (primary mode)

| ID | Task | Worker Model | Est |
|----|------|-------------|-----|
| P3.1 | Remove best_key(), make routing_optimizer the only router | glm-5.2 | 30min |
| P3.2 | Remove cascade logic from do_POST | glm-5.2 | 20min |
| P3.3 | Full integration test: all request types, all providers | glm-5.2 | 45min |

### Phase 4 Tasks (standalone + Routster)

| ID | Task | Worker Model | Est |
|----|------|-------------|-----|
| P4.1 | Write `src/margin_layer.py` for Routster nodes | glm-5.2 | 30min |
| P4.2 | Write README, quickstart, provider config guide | glm-4.5-flash | 30min |
| P4.3 | Write example configs: Hermes, TollGate, Routster | glm-4.5-flash | 20min |
| P4.4 | Add price visualization to dashboard | glm-5.2 | 30min |

## 7. RISKS AND MITIGATIONS

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Price Kalman produces bad estimates early (cold start) | High | Medium | Shadow mode validates before any routing changes |
| Routing optimizer slower than best_key() | Low | Low | Cache decision per minute; fall back on timeout |
| Provider oscillation (switching every request) | Medium | Medium | Hysteresis: only switch if price difference >10% |
| Ollama Cloud rate limit not modeled correctly | Medium | Low | Health factor = infinity handles this; price goes to ∞ |
| Subscription cost incorrect (€144 vs $155) | Low | Low | Config-driven; correct the value in providers.yaml |

## 8. SUCCESS CRITERIA

After all phases:

1. **Price is the primary routing signal** — no hardcoded cascade checks
2. **All providers compete on effective price** — z.ai, Ollama, PPQ, OpenRouter
3. **Peak hours are a cost input**, not a routing directive
4. **Forward-looking decisions** — Kalman predicts price 30min and 2h ahead
5. **Shadow data validated** — 48h of comparison data proves the engine works
6. **Standalone module** — others can install and configure for their setup
7. **Routster-ready** — margin layer enables ecash LLM resale
8. **Zero downtime** — every phase is hot-swappable with automatic fallback

## 9. FILE MAP

```
merchant-routing-engine/
├── config/
│   ├── providers.yaml          # Provider definitions + pricing
│   └── strategy.yaml           # Quality thresholds, peak hours, margin
├── src/
│   ├── __init__.py
│   ├── price_kalman.py         # 2-state Kalman: rate + velocity per provider
│   ├── consumption_kalman.py   # 3-state Kalman: tokens + rate + accel
│   ├── routing_optimizer.py    # Deterministic cost minimizer
│   ├── shadow_logger.py        # Tap into proxy, log decisions
│   ├── margin_layer.py         # Routster: compute customer price
│   └── config_loader.py        # Load providers.yaml + strategy.yaml
├── tests/
│   ├── test_price_kalman.py
│   ├── test_consumption_kalman.py
│   ├── test_routing_optimizer.py
│   └── test_shadow_logger.py
├── docs/
│   ├── price-first-kalman-plan.md  # This document
│   ├── architecture.md             # Updated architecture
│   └── routster-integration.md     # Future: ecash resale
├── examples/
│   ├── hermes-config.yaml       # Example: Hermes agent setup
│   ├── tollgate-config.yaml     # Example: TollGate router
│   └── routster-config.yaml     # Example: Routster node
├── README.md
└── pyproject.toml
```

## 10. RELATION TO EXISTING CODE

| Existing | Role in new system |
|----------|-------------------|
| `burn_predictor.py KalmanPredictor` | → Base class for both price and consumption Kalman |
| `burn_predictor.py route_request()` | → Inspiration for routing_optimizer.py (already has cost model!) |
| `model_matrix.json` | → Data source for base rates (646 models with pricing) |
| `zai_proxy.py best_key()` | → Phase 2 fallback, Phase 3 removed |
| `zai_proxy.py _try_ollama_cloud()` | → Becomes a provider in the optimizer |
| `zai_proxy.py _try_external_failover()` | → Becomes provider logic in optimizer |
| `zai_proxy.py _is_peak_hour()` | → Replaced by peak_multiplier in price Kalman |
| `zai_usage.db` | → Shared database for both Kalmans + shadow logging |
| `config/providers.yaml` (existing) | → Extended with pricing models |

---

*This plan is designed for incremental, reversible deployment. Each phase validates before the next begins. The live proxy never breaks — every change has an automatic fallback to the proven cascade.*
