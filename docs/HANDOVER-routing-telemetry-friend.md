# Merchant Routing Engine — Routing Telemetry Dataset Handover

**For:** Data analysis enthusiasts with a Hermes setup
**From:** Felix
**Date:** 2026-08-23
**Repo:** https://github.com/felixfelix-bot/merchant-routing-engine/tree/master/datasets/routing-telemetry

---

## 1. What Is the Merchant Routing Engine?

The Merchant Routing Engine is a **cost-minimizing LLM API reverse proxy** that sits between your applications and multiple LLM providers. Every single API request is routed to the **cheapest viable provider** in real time, using Kalman filters to estimate costs and predict quota exhaustion.

Think of it as an arbitrage engine for LLM tokens. You have multiple API keys and providers with different pricing structures (flat-rate subscriptions, per-token pricing, free promotional credits, Cashu-metered self-hosted nodes). The router picks the cheapest one for every request while respecting quality requirements and quota limits.

**Key innovation:** Instead of a hardcoded cascade ("try A first, then B, then C"), the router computes an **effective price** for every provider on every request and picks `argmin(effective_price)`. The effective price incorporates:

- Base cost (from Kalman-smoothed observations)
- Peak-hour surcharge (3× during UTC 06:00–09:59 for z.ai)
- Quota scarcity (ramps up as quota is consumed)
- Health penalties (degrades unreliable providers)
- Pacing factor (slows down before quota exhaustion)

## 2. Repository

- **GitHub:** https://github.com/felixfelix-bot/merchant-routing-engine/tree/master/datasets/routing-telemetry
- **Dataset location:** `datasets/routing-telemetry/` in the repo root
- **Architecture docs:** `docs/KALMAN-ROUTING-ARCHITECTURE.md` (full technical deep-dive)
- **ADRs:** `docs/adr/` directory (8 Architecture Decision Records)

Clone with:

```bash
git clone https://github.com/felixfelix-bot/merchant-routing-engine.git
cd merchant-routing-engine/datasets/routing-telemetry/
```

## 3. The Dataset

The dataset contains **~4 weeks of production routing telemetry** (2026-07-27 to 2026-08-23 UTC) exported as a **gzipped SQLite database** with full DDL. This is a sanitized export — no API keys, session IDs, or free-text error messages are included.

### Format

| File | Description |
|------|-------------|
| `scrubbed.db.gz` | Gzipped SQLite database — the full dataset. Decompress with `gunzip` or open directly with `zcat \| sqlite3` |
| `SCHEMA.sql` | Full DDL (all table definitions, indexes, constraints) |
| `README.md` | Dataset documentation with sample queries |

Load it:

```bash
# Decompress
gunzip -k scrubbed.db.gz    # produces scrubbed.db

# Or open in-memory without decompressing
zcat scrubbed.db.gz | sqlite3 :memory:

# Or use DuckDB / pandas directly
python3 -c "import sqlite3, gzip; conn = sqlite3.connect('scrubbed.db'); print(conn.execute('SELECT COUNT(*) FROM api_calls').fetchone())"
```

### Conventions

- **Timestamps** are Unix epoch seconds (floating-point, UTC) unless otherwise noted
- **Costs** are in **USD** (corrected — see shadow tables note below)
- In `routing_shadow_decisions`, **`agree=1`** means the live router and the shadow optimizer agreed on the same provider. `agree=0` means they diverged — these are the interesting rows.

### Table Summary

| Table | Rows | What It Contains |
|-------|------|-----------------|
| `api_calls` | ~99k | Every proxied LLM call: timestamp, provider, model, token counts, cost, status |
| `routing_profit` | ~5.6k | Consumer-mode savings ledger: effective price, next-best, savings |
| `routing_live_decisions` | ~17k | Live vs shadow routing decisions (did the router agree with itself?) |
| `routing_shadow_decisions` | ~158k | Shadow-mode routing comparisons (what WOULD have been chosen) |
| `key_decisions` | ~410k | Every key-rotation decision: chosen key + reason + quota state |
| `provider_telemetry` | ~63k | Per-provider health: response received/valid, latency, token mismatches |
| `kalman_samples` | ~940 | Kalman filter state: burn rate, velocity, exhaustion prediction |
| `daily_spend` | ~47 | Daily spend by provider tier |
| `anomaly_events` | ~5.5k | Cost inefficiency anomalies, routing warnings |
| `measured_rates` | 2 | Ground-truth per-token costs via wallet balance deltas |
| `price_observations` | ~7.3k | Observed provider pricing snapshots |

See `SCHEMA.sql` for the complete DDL including all columns, indexes, and constraints.

### Sensitive Fields Removed

The following fields were scrubbed from the dataset:

- `api_calls.key_suffix` — dropped (was last 4 chars of API key)
- `api_calls.session_id` — dropped (internal session hashes)
- `api_calls.task_type` — dropped
- `anomaly_events.detail` — dropped (kept `title` + `category`)
- `key_health.last_failure_ts`, `backoff_until`, `backoff_seconds` — dropped
- `error` / `error_type` fields — categorized to enums (`broken_pipe`, `timeout`, `dns_error`, `exhausted`, `none`, etc.)

### Shadow Tables (Audit Trail)

The `daily_spend_inflated_pre_rewrite`, `routing_profit_inflated_pre_rewrite`, and `api_calls_cost_inflated_pre_rewrite` tables preserve the **original sats-as-USD values** before a correction was applied. Routstrd and routstr publish pricing in sats (Lightning/Cashu); the code originally treated sats as USD, inflating recorded spend ~1300× for routstrd. The main tables now contain corrected USD values. The shadow tables are kept for auditability.

### Sample Queries

```sql
-- Daily spend by provider (corrected)
SELECT date, tier, ROUND(spend_usd, 4), call_count
FROM daily_spend WHERE tier NOT IN ('ours','friend')
ORDER BY date DESC, spend_usd DESC;

-- When did the Kalman filter predict exhaustion?
SELECT key, window, burn_rate_tph, exhausts_in_hours, will_exhaust, ts
FROM kalman_samples WHERE will_exhaust = 1
ORDER BY ts DESC LIMIT 20;

-- Live vs shadow routing agreement rate
SELECT
  SUM(CASE WHEN agree = 1 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) AS agree_pct,
  COUNT(*) AS total
FROM routing_live_decisions;

-- Provider health (latency + error rate)
SELECT provider,
  ROUND(AVG(latency_ms)) AS avg_latency,
  SUM(CASE WHEN response_received = 1 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) AS response_rate,
  SUM(CASE WHEN token_mismatch = 1 THEN 1 ELSE 0 END) AS mismatch_count
FROM provider_telemetry
GROUP BY provider ORDER BY avg_latency;

-- Cost anomalies
SELECT substr(title, 1, 80), category, COUNT(*)
FROM anomaly_events
GROUP BY category ORDER BY 3 DESC;
```

### License

MIT — use freely.

## 4. Current Live Router Setup

### Provider Chain

The router evaluates these providers — it computes effective prices for all viable providers and picks the cheapest. The converged rates mean z.ai "ours" wins almost always when it has quota.

| # | Provider | Pricing | Notes |
|---|----------|---------|-------|
| 1 | **z.ai "ours"** | $155/mo flat | Primary. ~$0.001/M amortized |
| 2 | **z.ai "friend"** | Shared flat-rate | ~$0.029/M |
| 3 | **Ollama Cloud** | $100/mo flat | ~$0.024/M, no peak hours |
| 4 | **OpenCode.ai** | $10/mo flat | GLM-5.2/5.3, Kimi, DeepSeek. Very cheap. |
| 5 | **NeuralWatt** | $0.14/M per-token | deepseek-v4-flash, prompt caching $0.03/M |
| 6 | **PPQ** | $0.14/M per-token | deepseek-v4-flash |
| 7 | **OpenRouter** | $0.135/M per-token | deepseek-v4-flash |
| 8 | **Telnyx** | per-token | Kimi K3, GLM-5.2, GPT-5, Claude, Gemini |
| 9 | **DeepInfra** | $1.30/M per-token | Last resort per-token |
| 10 | **routstrd** | Cashu/sats per-token | Self-hosted, Lightning-metered. Pricing in sats. |
| 11 | **routstr** | Cashu/sats per-token | Self-hosted, Lightning-metered. |
| 12 | **oxalpha** | Free promo (expires 2026-08-28) | stealth/ox-alpha on OpenRouter |

### Provider Details (New Upstreams)

**8. OpenCode.ai** — $10/month flat-rate subscription. Base URL: `https://opencode.ai/zen/go/v1`. Models: GLM-5.2, GLM-5.3, Kimi, DeepSeek. Very cheap flat-rate, positioned between z.ai keys and per-token providers. Portal at `https://opencode.ai`.

**9. NeuralWatt** — per-token pricing. Base URL: `https://api.neuralwatt.com/v1`. Models: deepseek-v4-flash at $0.14/M tokens, with prompt caching at $0.03/M. Portal/playground at `https://portal.neuralwatt.com/playground/glm-5.2`.

**10. routstrd** — self-hosted Cashu-metered inference node. Runs on VPS2. Pay-per-sat (Lightning/Cashu), not USD. Pricing in sats; the dataset originally treated sats as USD (inflating recorded spend ~1300×) — corrected in the main tables, original values preserved in `_inflated_pre_rewrite` shadow tables.

### Two Kalman Filter Families

1. **PriceKalman** (2-state constant-velocity model)
   - State: `[base_rate, velocity]` — smoothed cost per million tokens and its trend
   - Inputs: Observed cost/M from billing cycles
   - Outputs: `base_rate` → used to compute `effective_price = base × peak × scarcity × health × pace`
   - Per-token providers (PPQ, OpenRouter, NeuralWatt, DeepInfra) have fixed published prices — no Kalman needed for them

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
- External per-token providers (PPQ, OpenRouter, NeuralWatt, etc.) have no peak window

### Quality Tiers

| Difficulty | Required Tier | Representative Model |
|------------|---------------|----------------------|
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
| **ADR-006** | Shadow Mode Validation | The optimizer runs on read-only parallel mode, logging what it WOULD have chosen alongside live decisions. Minimum 48h validation before going live. |
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

The dataset is a gzipped SQLite database. Load it and start querying:

```bash
# Decompress
gunzip -k datasets/routing-telemetry/scrubbed.db.gz

# Quick overview
sqlite3 datasets/routing-telemetry/scrubbed.db "SELECT COUNT(*) FROM api_calls;"
sqlite3 datasets/routing-telemetry/scrubbed.db ".tables"
sqlite3 datasets/routing-telemetry/scrubbed.db ".schema api_calls"

# Or use DuckDB for fast analytical queries
duckdb -c "SELECT * FROM 'datasets/routing-telemetry/scrubbed.db'.api_calls LIMIT 5;"
```

Read `datasets/routing-telemetry/README.md` for full documentation and `SCHEMA.sql` for the complete DDL.

### Step 3: Analysis Ideas

Here are some questions the dataset can answer:

#### Routing Decision Agreement Rate
- What percentage of the time did the live router agree with the shadow optimizer?
- Has agreement improved over time? (Plot `agree` over `ts` in `routing_shadow_decisions`)
- What are the most common reasons for disagreement? (Group by `reason` where `agree=0`)

#### Cost Savings Analysis
- Total estimated savings: sum `estimated_savings_usd` in `routing_profit`
- Savings during peak vs off-peak: group by `is_peak_hour`
- Which provider delivered the most savings? Group by `provider_used`
- Plot `effective_price` vs `next_best_price` over time

#### Kalman Convergence Visualization
- Plot `burn_rate_tph` over time in `kalman_samples` — watch it stabilize
- Plot `uncertainty` over time — it should decrease as the filter converges
- Plot `velocity_tph2` — should approach zero at convergence
- Compare convergence speed across different keys and windows

#### Peak-Hour Impact
- How does the 3× multiplier affect routing decisions during UTC 06:00–09:59?
- Does the router shift to non-z.ai providers during peak? (Check `live_provider` distribution by hour)
- Cost comparison: peak vs off-peak in `api_calls` (group by hour of `ts`)

#### Provider Failover Patterns
- When does the router move from z.ai to external providers?
- What triggers failover? (Correlate `key_decisions.reason` with `routing_live_decisions.ts`)
- How quickly does health recover? (Check `key_health` and `anomaly_events`)

#### Token Billing Accuracy
- Are providers billing honestly? Compare `billed_tokens` vs `actual_tokens` in `provider_telemetry`
- Which providers have the largest `token_mismatch`?
- Is the mismatch consistent or sporadic?

#### Quota Pressure Dynamics
- How does the system behave under quota pressure? (Track `state` transitions in `pressure_decisions`)
- What models get served under pressure vs normal? (`requested_model` vs `would_serve_model`)
- How often does interactive traffic get priority?

#### Anomaly Patterns
- What types of anomalies are most common? (Group by `category` in `anomaly_events`)
- Are anomalies clustered in time? (Plot `ts` by `severity`)
- What's the resolution rate? (`resolved` vs total)

#### Sats-vs-USD Correction
- Compare `daily_spend` with `daily_spend_inflated_pre_rewrite` — how much was the inflation?
- Same for `routing_profit` vs `routing_profit_inflated_pre_rewrite`
- What's the actual sats-to-USD rate that was applied?

### Step 4: Point Your Hermes at the Data

If you have a Hermes setup, you can:

1. Load the SQLite database and query it:
   ```bash
   gunzip -k datasets/routing-telemetry/scrubbed.db.gz
   sqlite3 datasets/routing-telemetry/scrubbed.db "SELECT * FROM daily_spend ORDER BY date DESC LIMIT 10;"
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
| NeuralWatt | 0.140 | 0.140 (fixed) |
| DeepInfra | 1.300 | 1.300 (fixed) |

The flat-rate providers (ours, friend, Ollama Cloud, OpenCode.ai) converge to very low effective rates because their subscription cost is amortized over high token volume. Per-token providers have fixed published prices — no Kalman needed. routstrd and routstr are priced in sats and measured via wallet balance deltas (see `measured_rates` table).

## 9. Production Deployment

| Node | Role | Port |
|------|------|------|
| **DQ05** | Primary proxy | 9099 |
| **T470** | Secondary proxy | 9099 |
| CVM Server | Dashboard/snapshot publisher | HTTP + Nostr |
| **VPS2** | routstrd self-hosted inference node | Cashu-metered |

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
| `datasets/routing-telemetry/scrubbed.db.gz` | This dataset (gzipped SQLite) |
| `datasets/routing-telemetry/SCHEMA.sql` | Full DDL for all tables |
| `datasets/routing-telemetry/README.md` | Dataset documentation + sample queries |

---

## TL;DR

This is a real production system that routes LLM API requests to the cheapest provider using Kalman filters. It's been running for ~4 weeks and generated ~99k API calls with ~410k key decisions. The dataset is a gzipped SQLite database with 11+ tables covering routing decisions, cost savings, Kalman convergence, token billing accuracy, and provider failover patterns. Clone the repo, decompress the DB, and start asking questions.

**No API keys or secrets are included in this dataset.** All costs are in USD (corrected for sats-as-USD inflation). Timestamps are Unix epoch seconds (UTC). MIT licensed.

---

*Questions? Ask Felix on Signal.*