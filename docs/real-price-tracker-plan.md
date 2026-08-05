# Plan: Real-Time Price Calculation System (RP-1)

**Goal:** Eliminate ALL hardcoded rate constants. Every provider's $/M calculated from real billing data, continuously updated, fed into the routing optimizer.

## Problem Statement

Currently rates are hardcoded in 5+ locations:

| Location | File | Value | Reality |
|---|---|---|---|
| zai_proxy.py:1467 | `_OLLAMA_CLOUD_BASE_RATE` | $0.024/M | Real: $0.0155/M (35% wrong) |
| zai_proxy.py:1470 | `_MODEL_COST_PER_1M` | Various | All guesses |
| live_router.py:84 | `_DEFAULT_CONVERGED_RATES` | Various | All guesses |
| shadow_hook.py:51 | `_SEED_COSTS` | Various | All guesses |
| providers.yaml | `cost_per_1m_input/output` | Various | Stale |

The `daily_spend` table appears to track costs, but it's computed FROM these hardcoded rates — circular, not real measurement.

## Architecture

```
Provider API Response (real $)
        ↓
Proxy captures cost_usd per request
        ↓
api_calls.cost_usd column (NEW)
        ↓
RealPriceTracker (new module)
  - Rolling 24h/7d $/M per provider per model
  - Weighted by token volume
  - Kalman-smoothed
        ↓
price_kalman.py (existing, but fed REAL observations)
        ↓
routing_optimizer.py → decisions based on real prices
        ↓
CVM snapshot → dashboard shows real rates
```

## Data Sources Per Provider

### Ollama Cloud
- **API:** `https://ollama.com/api/usage` returns `activity.cost` per model over rolling 4 weeks
- **Real rate:** `cost / total_tokens` = measured $/M
- **Already working:** CVM server fetches this. Just need to wire into Python.
- **Current measured rate:** glm-5.2 = $0.0155/M, kimi-k2.7-code = $0.0209/M

### PPQ (api.ppq.ai)
- **Response body:** Check if PPQ returns `usage.cost` or similar in chat completions response
- **Balance API:** `POST /credits/balance` returns remaining balance
- **History API:** `GET /queries/history` returns per-query spend
- **Rate:** `query_cost / query_tokens` = real $/M per call

### OpenRouter
- **Response body:** OpenRouter returns `usage.cost` in every chat completion response
- **Headers:** May include `X-Cost-USD` or similar
- **Rate:** `response.usage.cost / total_tokens` = real $/M per call

### DeepInfra
- **Balance tracking:** `deepinfra_balance` table already tracks real balance changes
- **Response body:** DeepInfra returns `usage.cost` or billable tokens
- **Current data:** Started $5.00, spent $0.0000351 on 9 calls = $0.0000039/call avg

### z.ai (flat-rate)
- **Model:** Subscription = $155/mo (ours) + shared (friend)
- **Real marginal rate:** $0/token (already paid)
- **Amortized rate:** `$155 / monthly_tokens` — changes daily
- **For routing:** Marginal cost = $0 (correct, already implemented as $0 in daily_spend)

## Implementation Plan

### Step 1: Add cost_usd column to api_calls table
- `ALTER TABLE api_calls ADD COLUMN cost_usd REAL DEFAULT NULL`
- Migration script for existing rows (populate from daily_spend as approximation)

### Step 2: Extract real cost from each provider's API response in zai_proxy.py
- Ollama: Parse from `/api/usage` (already have this data, just need to store per-call)
- PPQ: Parse `response.json()` for cost field after each request
- OpenRouter: Parse `response.json().usage.cost` 
- DeepInfra: Parse response or track balance delta
- z.ai: cost_usd = 0 (flat-rate, marginal cost)

### Step 3: Build src/real_price_tracker.py
- `get_real_rate(provider, model, window_hours=168)` → returns measured $/M
- Queries: `SELECT SUM(cost_usd) / SUM(total_tokens) * 1e6 FROM api_calls WHERE key_name=? AND model=? AND ts > ? AND cost_usd IS NOT NULL`
- Caches result 5 min (prices don't change per-second)
- Falls back to Ollama API for Ollama, hardcoded for providers with no data yet
- Returns None if insufficient data (< 100 calls in window)

### Step 4: Feed real rates into price_kalman.py
- Replace `_DEFAULT_CONVERGED_RATES` with a call to `real_price_tracker.get_real_rate()`
- Kalman filter observes real rate as measurement, converges over time
- Seeds only used when no real data available (first startup)

### Step 5: Update CVM snapshot to show real rates
- `pricing` section in /snapshot shows measured $/M per provider
- CVM server fetches from real_price_tracker via DB or proxy endpoint

### Step 6: Remove hardcoded rates
- Replace `_MODEL_COST_PER_1M` with dynamic lookup
- Replace `_SEED_COSTS` with real measurements
- Replace `_DEFAULT_CONVERGED_RATES` with real measurements
- Keep hardcoded values ONLY as last-resort fallbacks (clearly marked as estimates)

### Step 7: Dashboard
- CVM snapshot `pricing.estimated_vs_measured` showing both
- Alert if measured rate deviates > 50% from expected (price change detection)

## Hardcoded Values to Eliminate

| # | Location | Variable | Current | Action |
|---|---|---|---|---|
| 1 | zai_proxy.py:1467 | `_OLLAMA_CLOUD_BASE_RATE` | 0.024 | → real_price_tracker |
| 2 | zai_proxy.py:1468 | `_OLLAMA_CLOUD_EXTRA_RATE` | 0.15 | → real_price_tracker (extra regime) |
| 3 | zai_proxy.py:1470-1477 | `_MODEL_COST_PER_1M` | Various | → real_price_tracker |
| 4 | live_router.py:84-89 | `_DEFAULT_CONVERGED_RATES` | Various | → real_price_tracker |
| 5 | shadow_hook.py:51-55 | `_SEED_COSTS` | Various | → real_price_tracker |
| 6 | providers.yaml | `cost_per_1m_input/output` | Various | → Remove, use tracker |
| 7 | zai_proxy.py:677 | `PPQ_PRICING` | 0.14/0.28 | → real_price_tracker |
| 8 | providers.yaml:41 | `extra_usage_rate_per_m` | 0.10 | → Calculate from real data |

## Task Breakdown (Kanban)

### RP-1: Schema migration — add cost_usd to api_calls
- ALTER TABLE + backfill from daily_spend
- Test: column exists, backfilled values reasonable

### RP-2: Cost extraction in zai_proxy.py
- Parse real cost from each provider's response
- Store in api_calls.cost_usd
- Per-provider parsing logic

### RP-3: Build src/real_price_tracker.py
- Rolling $/M calculation per provider per model
- 5-min cache
- Fallback logic
- Tests with mock DB

### RP-4: Wire real_price_tracker into existing modules
- Replace hardcoded rates in live_router.py, shadow_hook.py, zai_proxy.py
- Feed into price_kalman.py as observations
- Tests: no regressions

### RP-5: CVM snapshot integration
- Show real measured rates in /snapshot pricing section
- Show estimated vs measured comparison
- Dashboard updates

### RP-6: Cold review
- Verify all hardcoded rates replaced
- Verify fallbacks work when no data
- Verify Kalman convergence

### RP-7: Go live + monitor
- Run 7 days, verify rates converge to real values
- Compare against Ollama API activity.cost
- Alert if deviation > 50%
