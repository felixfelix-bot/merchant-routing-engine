# Merchant Routing Engine — Routing Telemetry Dataset Handover

**For:** Data analysis enthusiasts with a Hermes setup
**From:** Felix
**Date:** 2026-08-23
**Repo:** https://github.com/felixfelix-bot/merchant-routing-engine
**Branch:** `wt/glm53-quota-cleanup-t_da1b7c10`

---

## 1. What Is the Merchant Routing Engine?

The Merchant Routing Engine is a **cost-minimizing LLM API reverse proxy** that sits between your applications and multiple LLM providers. Every single API request is routed to the **cheapest viable provider** in real time, using Kalman filters to estimate costs and predict quota exhaustion.

Think of it as an arbitrage engine for LLM tokens. You have multiple API keys and providers with different pricing structures (flat-rate subscriptions, per-token pricing, free promotional credits). The router picks the cheapest one for every request while respecting quality requirements and quota limits.

**Key innovation:** Instead of a hardcoded cascade ("try A first, then B, then C"), the router computes an **effective price** for every provider on every request and picks `argmin(effective_price)`. The effective price incorporates:

- Base cost (from Kalman-smoothed observations)
- Peak-hour surcharge (3× during UTC 06:00–09:59 for z.ai)
- Quota scarcity (ramps up as quota is consumed)
- Health penalties (degrades unreliable providers)
- Pacing factor (slows down before quota exhaustion)

## 2. Repository

- **GitHub:** https://github.com/felixfelix-bot/merchant-routing-engine
- **Dataset location:** `datasets/routing-telemetry/` in the repo root
- **Architecture docs:** `docs/KALMAN-ROUTING-ARCHITECTURE.md` (full technical deep-dive)
- **ADRs:** `docs/adr/` directory (8 Architecture Decision Records)

Clone with:

```bash
git clone https://github.com/felixfelix-bot/merchant-routing-engine.git
cd merchant-routing-engine/datasets/routing-telemetry/
```

## 3. The Dataset

The dataset contains **~11 days of production routing telemetry** (2026-08-12 to 2026-08-23 UTC) exported as 16 CSV files totaling ~74 MB and ~770K rows. See `datasets/routing-telemetry/README.md` for full column descriptions.

### Conventions

- **Timestamps** are Unix epoch seconds (floating-point, UTC) unless otherwise noted
- **Costs** are in **USD**
- In `routing_shadow_decisions.csv`, **`agree=1`** means the live router and the shadow optimizer agreed on the same provider. `agree=0` means they diverged — these are the interesting rows.

### File Summary

| File | Rows | What It Contains |
|------|------|-----------------|
| `api_calls.csv` | 100,249 | Every API call: model, tokens, cost, status, duration, task type |
| `provider_telemetry.csv` | 63,071 | Per-request latency, billed vs actual tokens, error types |
| `routing_shadow_decisions.csv` | 158,859 | Live vs shadow routing decisions, agreement, cost comparison |
| `routing_live_decisions.csv` | 17,060 | Live routing decisions with pace multipliers |
| `routing_profit.csv` | 5,648 | Savings per routing decision vs next-best alternative |
| `key_decisions.csv` | 411,622 | Key selection decisions with quota percentages |
| `kalman_samples.csv` | 939 | Kalman filter state snapshots over time |
| `daily_spend.csv` | 49 | Daily spend per quality tier |
| `price_observations.csv` | 7,346 | Observed provider prices over time (Kalman inputs) |
| `pressure_decisions.csv` | 16,839 | Quota pressure routing decisions |
| `rate_limit_samples.csv` | 2,075 | Rate limit inter-arrival patterns |
| `anomaly_events.csv` | 5,577 | Detected anomalies (rate limits, exhaustion, errors) |
| `key_health.csv` | 6 | Current key health state (snapshot) |
| `measured_rates.csv` | 6 | Measured sats/USD rates |
| `ppq_daily_used.csv` | 6 | PPQ daily spend |
| `token_stats.csv` | 8 | Model token statistics (p50, p90, mean, max) |

### Key Columns to Know

- **`api_calls.csv`**: `model`, `total_tokens`, `cost_usd`, `duration_ms`, `task_type`, `status_code` — this is the main fact table
- **`routing_shadow_decisions.csv`**: `live_provider`, `shadow_provider`, `agree`, `live_cost`, `shadow_cost`, `divergence` — join with `api_calls` on timestamp to see full context
- **`routing_profit.csv`**: `savings_per_1m`, `estimated_savings_usd`, `is_peak_hour` — directly answers "how much money did the router save?"
- **`kalman_samples.csv`**: `burn_rate_tph`, `velocity_tph2`, `uncertainty`, `will_exhaust` — watch the filter converge over time
- **`provider_telemetry.csv`**: `billed_tokens`, `actual_tokens`, `token_mismatch` — are providers billing honestly?
- **`key_decisions.csv`**: `ours_pct`, `friend_pct`, `chosen_key`, `reason` — the key selection battleground

## 4. Current Live Router Setup

### Provider Chain

The router evaluates these providers in order of typical cost (cheapest first):

| # | Provider | Pricing | Notes |
|---|----------|---------|-------|
| 1 | **z.ai "ours"** | $155/mo flat (€155/mo) | Primary. Amortized cost ~$0.001/M tokens. Cheapest by far. |
| 2 | **z.ai "friend"** | Shared flat-rate | Secondary z.ai key. ~$0.029/M after convergence. |
| 3 | **Ollama Cloud** | $100/mo flat | No peak-hour surcharge. ~$0.024/M. |
| 4 | **PPQ** (api.ppq.ai) | $0.14/M tokens | Per-token provider. |
| 5 | **OpenRouter** | $0.135/M tokens | Per-token provider. |
| 6 | **DeepInfra** | $1.30/M tokens | Expensive per-token, last resort. |
| 7 | **oxalpha** | Free promo | Free promotional credits. **Expires 2026-08-28.** |

The router doesn't use this order as a cascade — it computes effective prices for all viable providers and picks the cheapest. But the converged rates mean z.ai "ours" wins almost always when it has quota.

### Two Kalman Filter Families

1. **PriceKalman** (2-state constant-velocity model)
   - State: `[base_rate, velocity]` — smoothed cost per million tokens and its trend
   - Inputs: Observed cost/M from billing cycles
   - Outputs: `base_rate` → used to compute `effective_price = base × peak × scarcity × health × pace`
   - Per-token providers (PPQ, OpenRouter, DeepInfra) have fixed published prices — no Kalman needed for them

2. **ConsumptionKalman** (3-state constant-acceleration model)
   - State: `[burn_rate, velocity, acceleration]` — token consumption rate and its derivatives
   - Inputs: Per-period token consumption
   - Outputs: `predict_horizon(n)`, `will_exhaust(quota, n)` — predicts when quota will run out
   - Provider-agnostic: knows nothing about price, only burn rate

These two filter families are **completely independent** (ADR-002). They never share state.

### Routing Decision Formula

```
effective_price = base_rate × peak_mult × scarcity_mult × health_mult × pace_mult
selected_provider = argmin(effective_price)
```

Where:
- **base_rate**: PriceKalman's smoothed $/M estimate
- **peak_mult**: 3.0× during UTC 06:00–09:59 (z.ai only), 1.0× otherwise — instant step function, NOT smoothed (ADR-003)
- **scarcity_mult**: `1 + max(0, (used_pct - 50) / 50)` — ramps from 1.0× at 50% quota to 2.0× at 100%
- **health_mult**: Graduated penalty based on failure count (see below)
- **pace_mult**: `clamp(ratio², 0.5, 3.0)` where ratio = projected total usage / quota — slows down before exhaustion

**Minimum effective price floor: $0.001/M** (ADR-004). A provider with `+∞` effective price is unreachable.

### Peak Hours

- **UTC 06:00–09:59** (Beijing 14:00–17:59)
- Price **triples** (3.0× multiplier) for z.ai keys during peak
- Applied as an **instant step function** — no smoothing, no Kalman input (ADR-003)
- External per-token providers (PPQ, OpenRouter, etc.) have no peak window

### Quality Tiers

| Difficulty | Required Tier | Representative Model |
|------------|---------------|---------------------|
| high | high | glm-5.2 |
| medium | standard | glm-4.5-air |
| low | low | glm-4.5-flash |

Providers below the required tier get `+∞` effective price and are filtered out before the sort.

### Shadow Mode

The optimizer runs in **parallel read-only mode** alongside the live router (ADR-006). For every request:

1. The **live router** makes its decision (may use legacy logic or hardcoded cascade)
2. The **shadow optimizer** independently computes what it would have chosen
3. Both decisions are logged to `routing_shadow_decisions` with `agree` flag and cost comparison

This allows validating the optimizer without risking production traffic. The `agree` column tells you when they diverged — those rows are where the optimizer would have done something different.

### Dispatch Gate

For hardware-dependent tasks (e.g., flashing a board), a three-dimension gate decides whether to proceed:

1. **Hardware availability** (binary): Is the required hardware present and accessible?
2. **Quota sufficiency** (predictive): Is there enough quota headroom accounting for concurrent burn?
3. **Price optimization** (informational): Peak-hour cost is reported but doesn't block when hardware is present ("a board in hand beats waiting")

### Graduated Health Penalties

| Failure Count | Multiplier | Effect |
|---------------|------------|--------|
| 0 | 1.0× | No penalty |
| 1–2 | 1.5× | Soft penalty (transient errors) |
| 3–5 | 3.0× | Moderate (problematic provider) |
| 6–10 | 10.0× | Severe (nearly unreachable) |
| >10 or breaker tripped | +∞ | Circuit breaker (fully unreachable) |

## 5. Architecture Diagram (Summary)

```
Signal/Matrix Message
    ↓
hermes-gateway (port 9098)
    ↓ POST localhost:9099/v1/chat/completions
zai_proxy (port 9099) — THE ORCHESTRATOR
    ↓
    ┌──────────────────────────────────────────────────────┐
    │ STEP 1: best_key()                                   │
    │   Kalman burn prediction per key                     │
    │   Reactive lock thresholds (5h/weekly/monthly)       │
    │   Recovery of locked keys                            │
    │   Health filter + cost tie-breaker                   │
    │   → "ours" | "friend" | None                         │
    ├──────────────────────────────────────────────────────┤
    │ STEP 2: If None → LiveRouter.select_failover()       │
    │   PriceKalman + ConsumptionKalman per provider       │
    │   Routing Optimizer: argmin(effective_price)         │
    ├──────────────────────────────────────────────────────┤
    │ STEP 3: Pricing Engine (pure functions)              │
    │   effective = base × peak × scarcity × health × pace │
    ├──────────────────────────────────────────────────────┤
    │ STEP 4: Dispatch Gate (if hardware tasks)            │
    │   Hardware? + Quota? + Price? → can_dispatch         │
    ├──────────────────────────────────────────────────────┤
    │ STEP 5: Forward to chosen provider                   │
    │   200 → return ✓  |  429 → backoff  |  402 → next    │
    ├──────────────────────────────────────────────────────┤
    │ STEP 6: Post-request logging                         │
    │   ShadowLogger → routing_shadow_decisions            │
    │   ProfitTracker → routing_profit                     │
    │   ConsumptionKalman.update(tokens)                   │
    └──────────────────────────────────────────────────────┘
```

**Data flow loops:**
1. **Request flow:** Client → Gateway → Proxy → Provider
2. **Kalman update:** Each request's tokens → ConsumptionKalman → better next prediction
3. **Shadow observation:** Every request → shadow optimizer (read-only) → log comparison
4. **Profit tracking:** Every routing decision → savings calculation → async DB write

## 6. Key Architecture Decision Records (ADRs)

| ADR | Title | Core Decision |
|-----|-------|---------------|
| **ADR-001** | Price-First Routing | Price is the primary routing signal. Provider selection = `argmin(effective_price)`. No hardcoded cascade, no if-statements for peak hours. |
| **ADR-002** | Multi-Kalman Separation | Separate Kalman filters per concern (cost vs consumption). They never share state because they model different physics. |
| **ADR-003** | Deterministic Peak Multiplier | Peak hours (3×) are an instant step function applied AFTER the Kalman output, not smoothed into it. The Kalman tracks the smooth trend underneath. |
| **ADR-004** | Effective Price Positivity | Every provider's effective price must be > 0. A $0.001/M floor prevents division-by-zero and free-tier exploitation. +∞ means unreachable. |
| **ADR-005** | Three-Layer Actor Separation | Three layers: (1) Kalman prediction, (2) deterministic multipliers, (3) router argmin. Each layer is independently testable. |
| **ADR-006** | Shadow Mode Validation | The optimizer runs in read-only parallel mode, logging what it WOULD have chosen alongside live decisions. Minimum 48h validation before going live. |
| **ADR-007** | Routster Marketplace Intelligence | Future: marketplace-aware routing for buy/sell on Routster (Phase 4, not yet implemented). |
| **ADR-008** | Deterministic Multipliers Outside Kalman | Generalizes ADR-003: ALL step-change signals (peak, health, scarcity) are deterministic multipliers outside the Kalman. The Kalman only tracks smooth trends. |

Full ADR documents are in `docs/adr/` in the repo.

## 7. Getting Onboarded

### Step 1: Clone the Repo

```bash
git clone https://github.com/felixfelix-bot/merchant-routing-engine.git
cd merchant-routing-engine
```

### Step 2: Explore the Dataset

The CSVs are in `datasets/routing-telemetry/`. Start with the smaller files:

```bash
# Quick overview
head -5 datasets/routing-telemetry/daily_spend.csv
head -5 datasets/routing-telemetry/key_health.csv
head -5 datasets/routing-telemetry/token_stats.csv

# The big ones
wc -l datasets/routing-telemetry/*.csv
```

Read `datasets/routing-telemetry/README.md` for full column descriptions.

### Step 3: Analysis Ideas

Here are some questions the dataset can answer:

#### Routing Decision Agreement Rate
- What percentage of the time did the live router agree with the shadow optimizer?
- Has agreement improved over time? (Plot `agree` over `ts` in `routing_shadow_decisions.csv`)
- What are the most common reasons for disagreement? (Group by `reason` where `agree=0`)

#### Cost Savings Analysis
- Total estimated savings: sum `estimated_savings_usd` in `routing_profit.csv`
- Savings during peak vs off-peak: group by `is_peak_hour`
- Which provider delivered the most savings? Group by `provider_used`
- Plot `effective_price` vs `next_best_price` over time

#### Kalman Convergence Visualization
- Plot `burn_rate_tph` over time in `kalman_samples.csv` — watch it stabilize
- Plot `uncertainty` over time — it should decrease as the filter converges
- Plot `velocity_tph2` — should approach zero at convergence
- Compare convergence speed across different keys and windows

#### Peak-Hour Impact
- How does the 3× multiplier affect routing decisions during UTC 06:00–09:59?
- Does the router shift to non-z.ai providers during peak? (Check `live_provider` distribution by hour)
- Cost comparison: peak vs off-peak in `api_calls.csv` (group by hour of `ts`)

#### Provider Failover Patterns
- When does the router move from z.ai to external providers?
- What triggers failover? (Correlate `key_decisions.reason` with `routing_live_decisions.ts`)
- How quickly does health recover? (Check `key_health.csv` and `anomaly_events.csv`)

#### Token Billing Accuracy
- Are providers billing honestly? Compare `billed_tokens` vs `actual_tokens` in `provider_telemetry.csv`
- Which providers have the largest `token_mismatch`?
- Is the mismatch consistent or sporadic?

#### Quota Pressure Dynamics
- How does the system behave under quota pressure? (Track `state` transitions in `pressure_decisions.csv`)
- What models get served under pressure vs normal? (`requested_model` vs `would_serve_model`)
- How often does interactive traffic get priority?

#### Anomaly Patterns
- What types of anomalies are most common? (Group by `category` in `anomaly_events.csv`)
- Are anomalies clustered in time? (Plot `ts` by `severity`)
- What's the resolution rate? (`resolved` vs total)

### Step 4: Point Your Hermes at the Data

If you have a Hermes setup, you can:

1. Load the CSVs into a SQLite database for easy querying:
   ```bash
   # Create a SQLite DB from the CSVs
   python3 -c "
   import sqlite3, csv, glob
   conn = sqlite3.connect('routing_telemetry.db')
   for csv_file in glob.glob('datasets/routing-telemetry/*.csv'):
       table = csv_file.split('/')[-1].replace('.csv', '')
       with open(csv_file) as f:
           reader = csv.reader(f)
           headers = next(reader)
           cols = ', '.join([f'\"{h}\" TEXT' for h in headers])
           conn.execute(f'CREATE TABLE IF NOT EXISTS {table} ({cols})')
           conn.execute(f'DELETE FROM {table}')
           placeholders = ', '.join(['?'] * len(headers))
           conn.executemany(f'INSERT INTO {table} VALUES ({placeholders})', reader)
           conn.commit()
           print(f'Loaded {table}')
   conn.close()
   "
   ```

2. Ask Hermes questions about the data:
   - "What's the total cost savings from routing?"
   - "Show me the Kalman convergence trajectory"
   - "When did the router disagree with the shadow optimizer most?"
   - "Which provider had the most token billing mismatches?"

3. Or use any data analysis tool you prefer — pandas, R, Jupyter, DuckDB, etc.

## 8. Converged Price Rates

From historical replay of 50,354 decisions / 527M tokens, the PriceKalman converged to these rates:

| Provider | Seed $/M | Converged $/M |
|----------|----------|---------------|
| z.ai "ours" | 0.310 | **0.001** |
| z.ai "friend" | 0.375 | **0.029** |
| Ollama Cloud | 0.500 | **0.024** |
| PPQ | 0.140 | 0.140 (fixed) |
| OpenRouter | 0.135 | 0.135 (fixed) |
| DeepInfra | 1.300 | 1.300 (fixed) |

The flat-rate providers (ours, friend, Ollama Cloud) converge to very low effective rates because their subscription cost is amortized over high token volume. Per-token providers have fixed published prices — no Kalman needed.

## 9. Production Deployment

| Node | Role | Port |
|------|------|------|
| **DQ05** | Primary proxy | 9099 |
| **T470** | Secondary proxy | 9099 |
| CVM Server | Dashboard/snapshot publisher | HTTP + Nostr |

The proxy runs as a persistent service on DQ05, with T470 as backup. All shadow/logging paths are wrapped in try/except and can never break production routing.

## 10. Key Files in the Repo

| File | What It Contains |
|------|-----------------|
| `docs/KALMAN-ROUTING-ARCHITECTURE.md` | Full 480-line technical architecture |
| `docs/adr/` | 8 Architecture Decision Records |
| `config/providers.yaml` | Provider definitions with pricing |
| `src/price_kalman.py` | PriceKalman implementation |
| `src/consumption_kalman.py` | ConsumptionKalman implementation |
| `src/dispatch_gate.py` | Three-dimension dispatch gate |
| `datasets/routing-telemetry/` | This dataset |
| `datasets/routing-telemetry/README.md` | Full column-level dataset documentation |

---

## TL;DR

This is a real production system that routes LLM API requests to the cheapest provider using Kalman filters. It's been running for ~11 days and generated ~770K rows of telemetry. The dataset lets you explore routing decisions, cost savings, Kalman convergence, token billing accuracy, and provider failover patterns. Clone the repo, load the CSVs, and start asking questions.

**No API keys or secrets are included in this dataset.** All costs are in USD. Timestamps are Unix epoch seconds (UTC).

---

*Questions? Ask Felix on Signal.*