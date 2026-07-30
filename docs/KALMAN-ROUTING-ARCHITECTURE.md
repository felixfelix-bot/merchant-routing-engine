# Kalman Filter-Based Routing Engine — Technical Architecture

## 1. SYSTEM OVERVIEW

The Merchant Routing Engine is a **cost-minimizing LLM API reverse proxy** that routes every incoming request to the cheapest viable provider in real time. It uses two families of Kalman filters — one estimating each provider's amortized cost per million tokens (`PriceKalman`), another predicting token-burn rate and quota exhaustion (`ConsumptionKalman`) — and then applies deterministic multipliers (peak-hour surcharge, quota scarcity, health penalties) on top of the smoothed base rate. The production proxy (`zai_proxy.py`) runs on **DQ05 at `localhost:9099`** as the primary, with T470 as secondary. It fronts two z.ai flat-rate API keys ("ours" and "friend") as the primary providers, falling back to per-token providers (PPQ, OpenRouter, DeepInfra) only when both z.ai keys are exhausted.

The core design principle (ADR-001) is: **price is the primary routing signal.** Provider selection is always `argmin(effective_price)` among viable providers — no hardcoded cascade, no peak-hour routing directive. Peak hours affect routing *only* through the price multiplier, never through an if-statement.

---

## 2. KALMAN FILTERS

Two completely independent Kalman filter families operate per-provider, each tuned to its physics (ADR-002). They never share state.

### 2.1 PriceKalman — Cost Estimator (`src/price_kalman.py`)

**Purpose:** Estimates the SMOOTH component of effective cost per million tokens ($/M) for one provider. Peak multiplier, scarcity, and health are deterministic functions applied *on top* — they are NOT Kalman inputs (ADR-003).

**State vector (2-state constant-velocity model):**

| Component | Meaning | Units |
|-----------|---------|-------|
| `base_rate` | Current estimated cost per million tokens | $/M |
| `velocity` | Rate of change in $/M per update cycle | $/M/cycle |

**Matrices:**
- Transition: `F = [[1, 1], [0, 1]]` (constant-velocity, dt=1)
- Observation: `H = [[1, 0]]` (we only measure base_rate)
- Process noise: `Q = diag(1e-6)` — cost drifts slowly
- Measurement noise: `R = [[1e-4]]` — observations are fairly precise

**Inputs:** Observed cost/M for each billing cycle snapshot (e.g., monthly_fee / tokens_consumed_this_month × 1e6 for flat-rate providers).

**Outputs:**
- `base_rate` — the smoothed $/M estimate
- `velocity` — trend of cost change
- `effective_price()` — computes `base_rate × peak_mult × scarcity × health × pace_mult`, floored at `MIN_EFFECTIVE_PRICE = $0.001/M` (ADR-004: effective price is ALWAYS positive). Returns `+inf` if any multiplier is infinite (unreachable provider).

**Converged rates** (from historical replay of 50,354 decisions / 527M tokens):

| Provider | Seed $/M | Converged $/M |
|----------|----------|---------------|
| ours | 0.310 | **0.001** |
| friend | 0.375 | **0.028983** |
| ollama_cloud | 0.500 | **0.023952** |
| ppq | 0.140 | 0.140 |
| openrouter | 0.135 | 0.135 |
| deepinfra | 1.300 | 1.300 |

*Per-token providers (ppq, openrouter, deepinfra) have fixed published prices — no Kalman needed.*

### 2.2 ConsumptionKalman — Burn-Rate Predictor (`src/consumption_kalman.py`)

**Purpose:** Provider-agnostic predictor of token consumption rate and quota exhaustion. Knows nothing about price, cost, peak hours, or health.

**State vector (3-state constant-acceleration model):**

| Component | Meaning | Units |
|-----------|---------|-------|
| `burn_rate` | Smoothed tokens consumed per period | tokens/period |
| `velocity` | First derivative — trend of burn_rate | tokens/period² |
| `acceleration` | Second derivative — curvature of trend | tokens/period³ |

**Matrices:**
- Transition: `F = [[1, dt, 0.5·dt²], [0, 1, dt], [0, 0, 1]]` (constant-acceleration)
- Observation: `H = [[1, 0, 0]]` (only burn_rate is directly observed)
- Auto-tuned via `from_history()`: `R` = empirical observation variance, `Q` = `R × 1e-3`

**Inputs:** Per-period token consumption (tokens per API call, per minute, or per hour — unit-agnostic, caller-defined).

**Outputs:**
- `predict_horizon(n)` — projected burn rate for the next `n` periods (non-mutating)
- `predict_cumulative(n)` — total tokens expected over next `n` periods
- `will_exhaust(quota_remaining, n)` → `(bool, fractional_periods_until_exhaustion)` with linear interpolation inside the crossing period
- `uncertainty` — standard deviation of burn_rate estimate (sqrt of P[0,0])

**Live data (from proxy `/quota`):**

| Key | Burn Rate | Uncertainty | Trend | Locked | Used % |
|-----|-----------|-------------|-------|--------|--------|
| ours | 359,281 tph | 380,664 | stable | No | 0% (window unknown) |
| friend (5h) | 19,821,291 tph | 4,864,275 | stable | **Yes (80% threshold)** | **95%** |
| friend (monthly) | 19,821,291 tph | 4,864,275 | stable | No | 2% |

---

## 3. ROUTING DECISION FLOW

End-to-end path for a single request:

```
Signal/Matrix message
    ↓
hermes-gateway (port 9098)
    ↓ POST localhost:9099/v1/chat/completions
zai_proxy (port 9099)
    ↓
    ┌──────────────────────────────────────────────────────┐
    │ STEP 1: best_key()                                   │
    │   Phase 1: Kalman burn prediction                    │
    │     → ConsumptionKalman.will_exhaust() per key       │
    │     → Locks key if ANY window exceeds threshold      │
    │   Phase 2: Reactive lock thresholds                  │
    │     → 5h: ours≥90%, friend≥80%                       │
    │     → weekly: ours≥60%, friend≥80%                   │
    │     → monthly: both≥95%                              │
    │   Phase 3: Recover previously-locked key             │
    │     → Re-check if window reset / cooldown expired    │
    │   Phase 4: Health check (skip exhausted keys)        │
    │     → Skip keys marked dead/exhausted                │
    │     → Cost tie-breaker: ours(1.0x) < friend(1.21x)   │
    │   → Returns "ours", "friend", or None                │
    ├──────────────────────────────────────────────────────┤
    │ STEP 2: If None → LiveRouter.select_failover()       │
    │   (kill switch: ~/.hermes/bot/.enable_live_routing)  │
    │     → Builds RoutingOptimizer with all providers     │
    │     → Each provider's PriceKalman + ConsumptionKalman│
    │     → optimizer.route(difficulty, estimated_tokens)  │
    │     → Returns cheapest viable + next-best fallback   │
    │   If LiveRouter disabled/failed:                     │
    │     → _try_external_failover() (hardcoded chain)     │
    │       Priority: DeepInfra → PPQ → OpenRouter         │
    ├──────────────────────────────────────────────────────┤
    │ STEP 3: Pricing Engine computes effective price       │
    │   effective = base_rate × peak × scarcity × health   │
    │               × pace                                 │
    │   (5 multiplier functions, all pure/deterministic)   │
    ├──────────────────────────────────────────────────────┤
    │ STEP 4: Routing Optimizer evaluates all providers    │
    │   For each provider, runs filter pipeline:           │
    │     a) Quality tier gate (model meets difficulty?)   │
    │     b) Health gate (graduated penalty or +inf)       │
    │     c) Exhaustion gate (will_exhaust + remaining<tks)│
    │     d) Scarcity multiplier (ramp from 50%→100% used) │
    │     e) Effective price = base×peak×scar×health×pace  │
    │   → Sort by effective_price ascending                │
    │   → Return cheapest viable (or fallback model)       │
    ├──────────────────────────────────────────────────────┤
    │ STEP 5: Dispatch Gate (if hardware tasks)            │
    │   Three-dimension gate (see §4 below)                │
    ├──────────────────────────────────────────────────────┤
    │ STEP 6: Forward to chosen provider                   │
    │   → 200 + content → return to gateway ✓              │
    │   → 200 + empty + reasoning → inject reasoning ✓     │
    │   → 200 + empty → failover to next key               │
    │   → 401/403 → mark exhausted → next key              │
    │   → 429 → mark exhausted + backoff → retry           │
    │   → 402 → mark unfunded → next external provider     │
    ├──────────────────────────────────────────────────────┤
    │ STEP 7: Post-request logging                         │
    │   → ShadowLogger: live vs shadow decision            │
    │   → ProfitTracker: savings vs next-best alternative  │
    │   → ConsumptionKalman.update(tokens) for serving key │
    │   → api_calls + key_decisions tables                 │
    └──────────────────────────────────────────────────────┘
```

### 3.1 Quality Tier System

| Difficulty | Required Tier | Representative Model |
|------------|---------------|---------------------|
| high | high | glm-5.2 |
| medium | standard | glm-4.5-air |
| low | low | glm-4.5-flash |

Providers below the required tier are filtered out (infinite effective price) before the sort.

### 3.2 Per-Provider Evaluation Pipeline (`_evaluate_provider`)

Five sequential gates, each can short-circuit to `+inf`:

1. **Quality tier gate** — model tier must meet difficulty requirement
2. **Health gate** — graduated penalty (1.5x → 3x → 10x → +inf) based on failure_count
3. **Exhaustion gate** — if `will_exhaust()` predicts quota exhaustion AND remaining < estimated_tokens
4. **Scarcity multiplier** — ramps linearly from 1.0x at 50% quota usage to 2.0x at 100%
5. **Effective price** — `base × peak × scarcity × health × pace`

---

## 4. DISPATCH GATE (`src/dispatch_gate.py`)

The dispatch gate is a **pure, side-effect-free** three-dimension decision function exposed at `GET /v1/dispatch_gate`. It governs whether a hardware-dependent task (e.g., flashing a board) should proceed given current quota, burn-rate, and hardware availability.

### Dimension 1: Hardware Availability (binary, checked first)

| Hardware Req | Check | Safety Margin |
|-------------|-------|---------------|
| `none` | Always available | 2.0× budget headroom |
| `board` | `/dev/ttyACM*` present + lock free | 4.0× budget headroom |
| `dual_board` | 2+ boards + lock free | 6.0× budget headroom |
| `dq05` | SSH reachable (3s timeout) | 3.0× budget headroom |

If hardware unavailable → **HOLD** regardless of quota. If available → escalate and relax price gate (scarcity override).

### Dimension 2: Quota Sufficiency (predictive, hardware-scaled margin)

```
required_headroom = task_budget × safety_margin + concurrent_burn_extra
```

Where `concurrent_burn_extra` accounts for the rest of the fleet burning quota during the hardware task's wall-clock duration:
```
concurrent_burn = max(burn_rate_ours, burn_rate_friend) × quota_total × duration_hours
```

Task durations: flash=10min, capture=20min, throughput=15min, handshake=60min.

**Decision logic:**
1. Find keys with `remaining ≥ required_headroom` AND `used_pct < 95%` AND healthy
2. If found → **PROCEED**
3. If not → try **flash downgrade** (glm-4.5-flash, budget × 0.3) before holding
4. If flash also fails → **HOLD**

### Dimension 3: Price Optimization (informational + override)

- Peak-hour 3× multiplier reported but **never blocks on its own**
- `scarcity_override = True` when hardware is present during peak ("a board in hand beats waiting")
- Scarcity factor: `1 + max(0, (max_used_pct - 50) / 50)` — same formula as `price_kalman.scarcity_factor`

### Task Budget Multipliers

| Task Type | Model | Budget Mult |
|-----------|-------|-------------|
| mechanical | glm-4.5-flash | 0.25 |
| coding | glm-5.2 | 1.0 |
| research | glm-5.2 | 2.5 |
| review | glm-5.2 | 0.5 |
| docs | glm-4.5-flash | 0.5 |

### Live Dispatch Gate Response (snapshot)

```json
{
  "can_dispatch": true,
  "reason": "sufficient headroom (friend key) with 2x margin",
  "recommended_model": "glm-5.2",
  "effective_price_per_m": 0.002,
  "scarcity_factor": 2.0,
  "quota_used_pct": {"ours": 100.0, "friend": 94.0},
  "is_peak_hour": false,
  "recommended_provider": "friend"
}
```

---

## 5. KEY DATA POINTS

### 5.1 Provider Cost Comparison ($/M tokens)

| Provider | Cost Type | Base Rate ($/M) | Notes |
|----------|-----------|-----------------|-------|
| z.ai "ours" | Flat-rate (€155/mo) | **0.001** | Cheapest — amortized over high volume |
| z.ai "friend" | Flat-rate (shared) | **0.029** | 21% penalty over ours |
| Ollama Cloud | Flat-rate ($100/mo) | **0.024** | No peak hours |
| OpenRouter | Per-token | **0.135** | deepseek-v4-flash |
| PPQ | Per-token | **0.140** | deepseek-v4-flash |
| DeepInfra | Per-token | **1.300** | Historical effective rate |

### 5.2 z.ai Quota Windows (per key)

Three overlapping windows:
- **5-hour window** (TOKENS_LIMIT): ~2M tokens, resets every 5 hours
- **Weekly window**: longer-term token budget
- **Monthly window** (TIME_LIMIT): resets monthly

### 5.3 Peak Hours

- **UTC 06:00–09:59** (Beijing 14:00–17:59)
- Price **triples** (3.0× multiplier) during peak
- Applied as an **instant step function** (no smoothing)
- Only z.ai keys are affected; external providers have no peak window

### 5.4 Prediction Accuracy (Converged-Rate Replay)

Replay of 50,354 historical decisions, 527M tokens:

| Metric | Value |
|--------|-------|
| Live cost (actual spend) | $10,743.95 |
| Shadow cost (seed-rate estimate) | $11,996.63 |
| Converged replay cost | **$0.55** |
| Converged vs Seed savings | **99.7%** |
| Live vs Converged agreement | 31.8% |
| Seed vs Converged agreement | 99.1% |
| Routing under converged rates | 100% → "ours" |

### 5.5 Graduated Health Penalty Scale

| Failure Count | Multiplier | Effect |
|---------------|------------|--------|
| 0 | 1.0× | No penalty |
| 1–2 | 1.5× | Soft penalty (transient) |
| 3–5 | 3.0× | Moderate (problematic) |
| 6–10 | 10.0× | Severe (near-unreachable) |
| >10 or breaker tripped | **+∞** | Circuit breaker (fully unreachable) |

### 5.6 Pace (Quota-Pacing) Multiplier

Predictive factor that adjusts price based on burn rate vs time remaining:
```
pace_ratio = (quota_used + burn_rate × time_remaining) / quota_total
pace_factor = clamp(pace_ratio², 0.5, 3.0)
```
- ratio 0.5 (underutilizing) → 0.25× (attract traffic)
- ratio 1.0 (perfect) → 1.0×
- ratio 1.2 (will exhaust) → 1.44× (slow down)
- Priority: **never run out > use everything**

---

## 6. ARCHITECTURE DIAGRAM DESCRIPTION

```
┌─────────────────────────────────────────────────────────────────────┐
│                        CLIENT LAYER                                 │
│  Signal / Matrix Message                                           │
│         │                                                           │
│         ▼                                                           │
│  hermes-gateway (port 9098)  ────── POST /v1/chat/completions      │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                   zai_proxy.py (port 9099)                          │
│                   ── THE ORCHESTRATOR ──                            │
│                                                                     │
│  ┌─────────────┐   ┌──────────────┐   ┌──────────────────────┐     │
│  │  KEYS LOADER │   │ HARDWARE     │   │ QUOTA FETCHER        │     │
│  │ ours, friend │   │ PROBE        │   │ (cached 5min TTL)    │     │
│  │ external keys│   │ (udevadm/ssh)│   │                      │     │
│  └──────┬───────┘   └──────┬───────┘   └──────────┬───────────┘     │
│         │                  │                      │                  │
│         ▼                  ▼                      ▼                  │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │                    best_key() (4 phases)                    │   │
│  │  Phase 1: Kalman burn prediction ──────────────┐            │   │
│  │  Phase 2: Reactive lock thresholds             │  per key   │   │
│  │  Phase 3: Recovery of locked keys              │            │   │
│  │  Phase 4: Health filter + cost tie-break       │            │   │
│  │  → "ours" | "friend" | None                    │            │   │
│  └──────────────────────────────────────────────────────────────┘   │
│         │                                                           │
│         ▼ if None                                                   │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │              LiveRouter.select_failover()                    │   │
│  │  (kill switch: .enable_live_routing)                         │   │
│  │                                                              │   │
│  │  ┌──────────────┐  ┌───────────────┐  ┌─────────────────┐  │   │
│  │  │ PriceKalman  │  │ConsumptionKalm│  │  CPVO Calculator│  │   │
│  │  │  (per prov)  │  │   (per prov)  │  │  (effective rate│  │   │
│  │  │  2-state     │  │   3-state     │  │   from DB)      │  │   │
│  │  └──────┬───────┘  └───────┬───────┘  └────────┬────────┘  │   │
│  │         │                  │                   │            │   │
│  │         ▼                  ▼                   ▼            │   │
│  │  ┌──────────────────────────────────────────────────────┐   │   │
│  │  │           ROUTING OPTIMIZER                          │   │   │
│  │  │  For each provider:                                  │   │   │
│  │  │    1. Quality tier gate (difficulty → tier)          │   │   │
│  │  │    2. Health gate (graduated: 1.5x→3x→10x→∞)        │   │   │
│  │  │    3. Exhaustion gate (will_exhaust + remaining<toks)│   │   │
│  │  │    4. Scarcity multiplier (1.0→2.0 ramp at 50%→100%) │   │   │
│  │  │    5. effective_price = base×peak×scar×health×pace   │   │   │
│  │  │  → sort by effective_price → cheapest viable          │   │   │
│  │  └──────────────────────────────────────────────────────┘   │   │
│  └──────────────────────────────────────────────────────────────┘   │
│         │                                                           │
│         ▼                                                           │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │              PRICING ENGINE (pure functions)                │   │
│  │  peak_multiplier()    → 3.0x during UTC 06-09 (z.ai only)  │   │
│  │  scarcity_factor()    → 1 + max(0, (pct-50)/50)            │   │
│  │  health_pricing_factor() → 1.0|1.5|3.0|10.0|+∞            │   │
│  │  pace_factor()        → clamp(ratio², 0.5, 3.0)            │   │
│  │  compute_effective_price() → base × all multipliers        │   │
│  │  (MIN_EFFECTIVE_PRICE = $0.001/M floor — ADR-004)          │   │
│  └──────────────────────────────────────────────────────────────┘   │
│         │                                                           │
│         ▼                                                           │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │              DISPATCH GATE (/v1/dispatch_gate)              │   │
│  │  Dim 1: Hardware available? (board/lock/dq05 probe)        │   │
│  │  Dim 2: Quota sufficient? (budget × margin + concurrent)   │   │
│  │  Dim 3: Price OK? (informational, scarcity_override)       │   │
│  │  → can_dispatch: true/false + recommended_model            │   │
│  └──────────────────────────────────────────────────────────────┘   │
│         │                                                           │
│         ▼                                                           │
│  ┌──────────────┐  ┌──────────┐  ┌────────────┐  ┌────────────┐    │
│  │ z.ai "ours"  │  │z.ai      │  │ DeepInfra  │  │ PPQ / OR   │    │
│  │ api.z.ai     │  │"friend"  │  │            │  │            │    │
│  │ glm-5.2      │  │ glm-5.2  │  │deepseek-v4 │  │deepseek-v4 │    │
│  │ $0.001/M     │  │ $0.029/M │  │ $1.30/M    │  │ $0.14/M    │    │
│  └──────────────┘  └──────────┘  └────────────┘  └────────────┘    │
│         │                                                           │
│         ▼ (response received)                                       │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │                 POST-REQUEST LOGGING                          │   │
│  │                                                               │   │
│  │  ShadowLogger  ──→ routing_shadow_decisions table             │   │
│  │    (live vs shadow decision comparison)                      │   │
│  │    → agreement_rate, cost_comparison                         │   │
│  │                                                               │   │
│  │  ProfitTracker ──→ routing_profit table (daemon thread)       │   │
│  │    (savings_per_1m = next_best - effective, async writes)    │   │
│  │    → get_daily_summary(), get_weekly_trend()                 │   │
│  │                                                               │   │
│  │  ConsumptionKalman.update(tokens) ──→ serving provider only   │   │
│  │                                                               │   │
│  │  token_audit ──→ billed vs actual mismatch check              │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    CVM SERVER (demo/cvm-server)                     │
│                    ── SNAPSHOT PUBLISHER ──                         │
│                                                                     │
│  Reads from:                                                        │
│    • Proxy /quota endpoint (cached 10s)                             │
│    • Proxy /v1/dispatch_gate endpoint                               │
│    • kalman_price_state.json (converged rates)                      │
│    • SQLite DBs (zai_usage.db, demo_ledger.db)                      │
│                                                                     │
│  get_snapshot() tool returns:                                       │
│    • quota (per-key windows + predictions)                          │
│    • pricing (ours/friend/ollama/ppq $/M)                           │
│    • cost_today, cost_hour                                          │
│    • routing_decisions (last 20)                                    │
│    • provider_distribution                                          │
│    • dispatch_gate (can_dispatch, scarcity, headroom)              │
│    • system stats (CPU, mem, disk)                                  │
│    • participants + ledger                                          │
│                                                                     │
│  Exposed via:                                                       │
│    • Nostr NIP-28 tool calls (get_snapshot, send_prompt)           │
│    • HTTP /snapshot endpoint (dashboard display)                    │
│    • HTTP /price-history endpoint                                   │
└─────────────────────────────────────────────────────────────────────┘
```

### Data Flow Arrows (Summary)

1. **Request flow:** Client → Gateway (9098) → Proxy (9099) → `best_key()` → Provider
2. **Kalman update loop:** Each request's token count → `ConsumptionKalman.update()` → state evolves → next `best_key()` prediction is more accurate
3. **Shadow observation loop:** Every request → `ShadowHook.compare()` → `RoutingOptimizer.route()` (read-only) → logged to `routing_shadow_decisions` table → validates optimizer against live decisions
4. **Profit tracking loop:** Every routing decision → `ProfitTracker.record_decision()` (async daemon thread) → `routing_profit` table → dashboard savings panel
5. **CVM snapshot loop:** CVM server polls proxy `/quota` + `/dispatch_gate` every 10s → reads Kalman state file → publishes unified snapshot via Nostr + HTTP

---

## 7. KEY ADRs (Architecture Decision Records)

| ADR | Title | Core Decision |
|-----|-------|---------------|
| ADR-001 | Price-First Routing | Price is the primary routing signal; `argmin(effective_price)` |
| ADR-002 | Multi-Kalman Separation | Separate filters per concern (cost, consumption, demand) |
| ADR-003 | Deterministic Peak Multiplier | Peak hours are instant step function, NOT Kalman-smoothed |
| ADR-004 | Effective Price Positivity | Price is always > 0 ($0.001 floor); +∞ = unreachable |
| ADR-005 | Three-Layer Actor Separation | Kalman (predict) → Pricing (deterministic multipliers) → Router (argmin) |
| ADR-006 | Shadow Mode Validation | Run optimizer in read-only parallel before going live |
| ADR-008 | Deterministic Multipliers Outside Kalman | All non-cost factors applied post-Kalman |
| ADR-009 | Scarcity Multiplier | Linear ramp from 50% quota usage |

---

## 8. PRODUCTION DEPLOYMENT

| Node | Role | Port | Details |
|------|------|------|---------|
| **DQ05** | Primary proxy | 9099 | Main routing, both z.ai keys, Kalman convergence tracking |
| **T470** | Secondary proxy | 9099 | Backup proxy, also runs z.ai API keys |
| CVM Server | Dashboard/snapshot | HTTP + Nostr | Reads proxy state, publishes unified snapshots |

**Kill switches (no restart needed):**
- `~/.hermes/bot/.enable_live_routing` — enables/disables LiveRouter failover
- `~/.hermes/bot/.key_disabled_ours` — disables the "ours" z.ai key

**Failure safety:** All shadow/logging paths are wrapped in try/except — they can NEVER break production routing. LiveRouter returns `(None, None)` on failure, falling through to the hardcoded external failover chain.
