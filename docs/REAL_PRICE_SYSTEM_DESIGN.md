# Real-Time Price Calculation System — Design & Implementation Plan

> **Goal:** Eliminate ALL hardcoded rate constants from the merchant routing engine
> and replace them with a system that continuously measures actual $/M per provider
> per model from real billing/API data. Every rate the optimizer sees must be an
> *observation*, not a constant.

---

## 1. ARCHITECTURE — How Real-Time Price Calculation Works

### Current State (the problem)

Six locations hardcode rates that disagree with each other AND with reality:

```
live_router.py       _DEFAULT_CONVERGED_RATES  → seeds PriceKalman
shadow_hook.py       _SEED_COSTS               → seeds PriceKalman
pricing_engine.py    EXTRA_USAGE_BASE_RATE     → 0.024 $/M
zai_proxy.py         _MODEL_COST_PER_1M        → lookup table for spend tracking
                     _OLLAMA_CLOUD_BASE_RATE   → 0.024 $/M
providers.yaml       monthly_fee_usd, cost_per_1m_*  → config
cvm-server.ts        flatKeyCostPerM            → 0.02 $/M (dashboard)
                     ollamaMonthlyUsd           → $100 (dashboard)
```

The Kalman filter in `price_kalman.py` is **seeded** with these constants and never
receives real observations — it has no measurement pipeline feeding it real $/M.
`record_request(cost_estimate=...)` exists but nobody calls it with real data.

### Proposed Architecture

```
                          ┌─────────────────────────────────────────┐
                          │        REAL PRICE OBSERVATIONS           │
                          │                                         │
                          │  Ollama billing API (cost/activity)     │
                          │  DeepInfra response (estimated_cost)    │
                          │  PPQ burn ledger (cost_usd / tokens)    │
                          │  OpenRouter response headers/body       │
                          │  z.ai amortization ($sub/tokens)        │
                          │  daily_spend table (cost/tokens)        │
                          └────────────────┬────────────────────────┘
                                           │
                                           ▼
                          ┌─────────────────────────────────────────┐
                          │     RealPriceTracker (NEW MODULE)        │
                          │     src/real_price_tracker.py            │
                          │                                         │
                          │  • Collects observations every N min     │
                          │  • Computes $/M per provider × model     │
                          │  • Writes to price_observations table    │
                          │  • Runs change-detection (CUSUM)         │
                          └────────────────┬────────────────────────┘
                                           │
                                           ▼
                          ┌─────────────────────────────────────────┐
                          │   PriceKalman.update(observed_rate)      │
                          │   (existing filter, now FED real data)   │
                          │                                         │
                          │  One Kalman per provider × model         │
                          │  State: [base_rate, velocity]            │
                          └────────────────┬────────────────────────┘
                                           │
                                           ▼
                          ┌─────────────────────────────────────────┐
                          │   live_router / shadow_hook              │
                          │                                         │
                          │  Reads Kalman base_rate → seeds optimizer│
                          │  NO hardcoded constants in the seed path │
                          └────────────────┬────────────────────────┘
                                           │
                                           ▼
                          ┌─────────────────────────────────────────┐
                          │   RoutingOptimizer → effective_price     │
                          │   × peak × scarcity × health × pace      │
                          │   (deterministic multipliers unchanged)   │
                          └─────────────────────────────────────────┘
                                           │
                                           ▼
                          ┌─────────────────────────────────────────┐
                          │   CVM Dashboard / Snapshot               │
                          │   Shows REAL measured $/M + Kalman trend │
                          └─────────────────────────────────────────┘
```

### Key Design Principles

1. **Observations, not constants.** Every $/M value the optimizer sees comes from
   `PriceKalman.base_rate`, and every `update()` call receives a *measured* rate
   derived from real billing data.

2. **Kalman seeds are cold-start estimates.** Hardcoded seeds are acceptable ONLY
   as the initial `x[0]` before the first real observation arrives. After the first
   observation, the filter's output is independent of the seed. Seeds must be
   clearly labelled as "cold start only" and replaced by `update()` within minutes.

3. **Per-provider × per-model granularity.** Different models on the same provider
   can have different prices. The tracker maintains a `(provider, model)` key.

4. **Decoupled collection.** The tracker runs on its own schedule (every 5 min),
   writes to SQLite, and never blocks the routing hot path.

---

## 2. DATA SOURCES — Per-Provider Real Price Data

### 2.1 Ollama Cloud

**Source:** `https://ollama.com/api/usage` (API key auth, already working via
`fetch_ollama_usage()` in `src/ollama_extra_usage.py`)

**What it returns:** JSON with per-model `activity.cost` fields and token counts.

**Calculation:**
```
real_rate_per_m = activity.cost / (activity.total_tokens / 1_000_000)
```

**Alternative (fallback):** Amortize the flat $100/mo fee:
```
daily_spend_cost / (daily_spend_tokens / 1e6)
```
This is what CVM already does: `CFG.ollamaMonthlyUsd / (ollamaTokens / 1e6)`.

**Currently measured:** $0.0240/M (from daily_spend — this is itself computed
from the hardcoded $0.024/M constant in `_estimate_cost_usd`, so it's circular).
**Real rate (from actual Ollama billing):** $0.0155/M — **35% lower**.

**Verdict:** Primary source = Ollama API cost data. Fallback = subscription
amortization from daily_spend.

### 2.2 z.ai (ours + friend keys)

**Source:** Flat-rate subscription. Marginal token cost = $0. Real cost is
*amortized*:

```
amortized_rate_per_m = monthly_fee_usd / (month_to_date_tokens / 1_000_000)
```

**Data available:**
- `api_calls` table: `SUM(total_tokens) WHERE key_name IN ('ours', 'friend')`
  grouped by model
- `providers.yaml`: `monthly_fee_usd: 155` for ours, `0` for friend

**Currently hardcoded:** `ours: 0.001`, `friend: 0.029` — both wrong.
ours should be `155 / (4975268693 / 1e6) = $0.000031/M` (manager tier) — nearly
free at high volume. friend at $0.029 is a 21% premium that has no real billing
basis.

**Calculation:**
```python
month_start = first_day_of_current_month
tokens_mtd = SUM(total_tokens) FROM api_calls
             WHERE key_name = 'ours' AND ts >= month_start
rate = monthly_fee_usd / (tokens_mtd / 1e6)
```
Friend: same formula with `monthly_fee_usd = 0` (shared key, cost = 0 to us),
but apply ADR-005 penalty multiplier (21% premium) on top in the optimizer,
not in the base rate.

### 2.3 DeepInfra

**Source:** Response body contains `estimated_cost` field (already extracted in
the proxy at line 2229: `ext_usage.get("estimated_cost")`).

**Calculation per request:**
```python
real_rate_per_m = estimated_cost / (total_tokens / 1_000_000)
```

**Currently measured:** $1.3000/M all-time average from daily_spend — BUT this
is computed from `actual_cost` values returned by DeepInfra, so this IS real data
(already correct for DeepInfra).

**Storage:** Already logged to `daily_spend` via `_record_spend(actual_cost=...)`.
The tracker reads it back.

### 2.4 PPQ (api.ppq.ai)

**Source:** `api_burn.db` → `ppq_queries` table has `cost_usd` and `total_tokens`
per query (populated by the `api_burn_collector` cron job that hits PPQ's
`/queries/history` endpoint).

**Calculation:**
```python
SELECT SUM(cost_usd) / (SUM(total_tokens) / 1e6)
FROM ppq_queries WHERE ts > week_ago
```

**Currently hardcoded:** `ppq: 0.14` — should be measured from actual query costs.

**Note:** The CVM server already computes this (`ppqCostPerM` from burnDb). The
issue is that the router doesn't use it — it uses the hardcoded constant.

### 2.5 OpenRouter

**Source:** OpenRouter returns cost data in response body (`usage.cost` field)
and `X-OR-Cost-USD` response header.

**Calculation per request:**
```python
real_rate_per_m = float(response_cost) / (total_tokens / 1_000_000)
```

**Currently hardcoded:** `openrouter: 0.135` — should be measured from responses.

**Note:** If OpenRouter doesn't log cost per-request yet, we need to extract it
from the response body (same pattern as DeepInfra's `estimated_cost`).

### 2.6 Subscription Providers (monthly fee amortization)

For any provider with `monthly_fee_usd > 0` and no per-request cost field,
the real rate is:
```
rate = monthly_fee_usd / (month_to_date_tokens / 1e6)
```

This naturally decreases as tokens accumulate (amortization). A provider that
costs $0.06/M at the start of the month drops to $0.02/M by month-end at the
same volume.

---

## 3. CALCULATION — How to Compute $/M per Provider × Model

### 3.1 Per-Request Rate (DeepInfra, OpenRouter)

From each completed request:
```python
observed_rate = actual_cost_usd / (total_tokens / 1_000_000)
```
Aggregate over a time window (e.g., last 1h) using a weighted average:
```python
weighted_rate = SUM(cost) / (SUM(tokens) / 1e6)
```
This naturally weights by token volume.

### 3.2 Amortized Rate (z.ai, Ollama subscription)

```python
observed_rate = monthly_fee_usd / (SUM(total_tokens_this_month) / 1e6)
```
Recompute every 5 min as cumulative tokens grow.

### 3.3 Billing API Rate (Ollama /api/usage, PPQ burn ledger)

```python
observed_rate = SUM(billing_cost) / (SUM(billing_tokens) / 1e6)
```

### 3.4 Feed into Kalman

Each computed `observed_rate` is passed to `PriceKalman.update(observed_rate)`.
The Kalman smooths between observations and tracks velocity (rate of price change).

**Measurement noise tuning:**
- Per-request rates (DeepInfra): high noise (prompt caching varies wildly).
  `measurement_noise = 1e-3`.
- Amortized rates (z.ai): low noise (slowly changing). `measurement_noise = 1e-6`.
- Billing API rates (Ollama): medium noise. `measurement_noise = 1e-4`.

---

## 4. INTEGRATION — How This Feeds Into Existing Systems

### 4.1 PriceKalman (existing, modified)

**Current:** `LiveRouter.__init__` creates PriceKalman instances seeded with
`_DEFAULT_CONVERGED_RATES` (hardcoded). No real observations are ever fed.

**Proposed:**
- Seeds remain as cold-start fallbacks (clearly commented as "cold start only").
- A new `_update_kalman_from_observations()` method runs every 5 min inside
  `LiveRouter`, pulling observations from the `RealPriceTracker` and calling
  `price_kalman.update(observed_rate)`.
- The `_base_rates` dict is replaced by live reads from `price_kalman.base_rate`.

### 4.2 LiveRouter Changes

```python
# BEFORE (hardcoded):
_DEFAULT_CONVERGED_RATES = {"ours": 0.001, "ollama_cloud": 0.024, ...}

# AFTER (cold-start only, replaced by observations within 5 min):
_COLD_START_RATES = {"ours": 0.001, "ollama_cloud": 0.024, ...}  # fallback only

def _refresh_rates(self):
    """Called every 5 min by the tracker callback."""
    observations = self._price_tracker.get_latest_rates()
    for provider_model, rate in observations.items():
        kalman = self._get_kalman(provider_model)
        kalman.update(rate)
```

### 4.3 ShadowHook Changes

Same pattern: replace `_SEED_COSTS` with cold-start fallbacks, add observation
pipeline. The shadow hook already calls `record_request()` — just needs to pass
real cost estimates.

### 4.4 CVM Dashboard Changes

Replace `CFG.flatKeyCostPerM = 0.02` with a query that reads from
`price_observations` or the Kalman state. The `computePricing()` function already
reads real data for Ollama and PPQ — extend it to read from the unified tracker
for all providers.

---

## 5. HARDCODED VALUES TO ELIMINATE — Complete List

### 5.1 `src/live_router.py` (line 84-91)
```python
_DEFAULT_CONVERGED_RATES = {
    "ours":          0.001,    # ❌ should be amortized from $155/mo
    "friend":        0.028983, # ❌ should be 0 or amortized
    "ollama_cloud":  0.023952, # ❌ should be $0.0155 (from billing)
    "ppq":           0.14,     # ❌ should be from burn ledger
    "openrouter":    0.135,    # ❌ should be from responses
    "deepinfra":     1.30,     # ⚠ already real (from actual_cost), but should be dynamic
}
```
**Action:** Convert to `_COLD_START_RATES` (fallback only). Real values from
`RealPriceTracker`.

### 5.2 `src/shadow_hook.py` (line 51-58)
```python
_SEED_COSTS = {
    "ours":          0.31,     # ❌ wildly wrong
    "friend":        0.375,    # ❌ wildly wrong
    "ollama_cloud":  0.024,    # ❌ wrong
    "ppq":           0.14,     # ❌ should be measured
    "openrouter":    0.135,    # ❌ should be measured
    "deepinfra":     1.30,     # ⚠ close to real
}
```
**Action:** Same as live_router.

### 5.3 `src/pricing_engine.py` (lines 109-113)
```python
EXTRA_USAGE_BASE_RATE = 0.024    # ❌
EXTRA_USAGE_TARGET_RATE = 0.10   # ⚠ derived
```
**Action:** Replace with `RealPriceTracker.get_rate('ollama_cloud')` for base.
Target rate becomes `tracker.get_rate('ollama_cloud', regime='extra')`.

### 5.4 `zai_proxy.py` (lines 1467-1498)
```python
_OLLAMA_CLOUD_BASE_RATE = 0.024   # ❌
_OLLAMA_CLOUD_EXTRA_RATE = 0.15   # ❌ should come from providers.yaml + tracker
_MODEL_COST_PER_1M = {
    "ollama_cloud": 0.024,        # ❌
    "friend": 0.029,              # ❌
    "ours": 0.0,                  # ⚠ should be amortized, not zero
    "glm-5.2": 0.0,               # ❌ same as ours
    "glm-4.5": 0.88,              # ❌ guess
    "glm-4.5-air": 0.65,          # ❌ guess
    "glm-4.5-airx": 2.80,         # ❌ guess
    "glm-4.5-x": 5.55,            # ❌ guess
    "deepseek/deepseek-v4-pro": 1.30,   # ⚠ close
    "deepseek/deepseek-v4-flash": 0.09, # ❌ should be measured
    "deepinfra": 1.30,            # ⚠ already real
    ...
}
```
**Action:** Replace `_estimate_cost_usd()` to read from `RealPriceTracker`
for live rates. `_record_spend()` already uses `actual_cost` when available —
that path is correct.

### 5.5 `config/providers.yaml`
```yaml
monthly_fee_usd: 155        # ✅ real config, keep
monthly_fee_usd: 100        # ✅ real config, keep
extra_usage_rate_per_m: 0.10 # ❌ should be measured
cost_per_1m_input: 0.09      # ❌ published prices — verify against real
cost_per_1m_output: 0.19     # ❌ published prices — verify against real
```
**Action:** Keep `monthly_fee_usd` as config (it IS real config). Remove
`cost_per_1m_*` from YAML — these are observations, not config. Keep
`extra_usage_rate_per_m` as config only if Ollama publishes it; otherwise
measure it.

### 5.6 `demo/cvm-server/src/cvm-server.ts` (lines 53-54)
```typescript
flatKeyCostPerM: 0.02,     // ❌ should read from tracker
ollamaMonthlyUsd: 100.0,   // ✅ real config, keep
```
**Action:** Replace `flatKeyCostPerM` with a DB read from `price_observations`.

### 5.7 `src/live_router.py` (lines 94-101) — Quota totals
```python
_QUOTA_TOTALS = {
    "ours":         2_000_000,    # ❌ guess
    "friend":       2_000_000,    # ❌ guess
    "ollama_cloud": 500_000_000,  # ⚠ from Ollama docs, verify
    ...
}
```
**Action:** Read from `ollama_quota_tracker` (already partially done for
Ollama). z.ai quota windows come from the quota API.

---

## 6. NEW MODULES — What to Build

### 6.1 `src/real_price_tracker.py` (core, ~300 lines)

**Responsibility:** Collect, compute, and serve real $/M observations.

```python
class RealPriceTracker:
    """Continuously measures real $/M per provider × model from billing data.

    Runs on a 5-min collection cycle. Writes observations to
    price_observations table. Never blocks the routing path.
    """

    def __init__(self, zai_db_path, burn_db_path):
        self._zai_db = zai_db_path
        self._burn_db = burn_db_path
        self._cache: dict[tuple[str, str], RateObservation] = {}
        self._cache_ts = 0.0
        self._lock = threading.Lock()

    def collect_all(self) -> dict[tuple[str, str], float]:
        """Collect fresh rate observations from all sources.

        Returns {(provider, model): observed_rate_per_m}.
        """
        rates = {}
        rates.update(self._measure_zai_amortized())     # ours, friend
        rates.update(self._measure_ollama_billing())     # ollama_cloud
        rates.update(self._measure_ppq_burn_ledger())    # ppq
        rates.update(self._measure_deepinfra_spend())    # deepinfra
        rates.update(self._measure_openrouter_spend())   # openrouter
        self._write_observations(rates)
        return rates

    def get_latest_rate(self, provider: str, model: str = None) -> float | None:
        """Get the latest measured $/M for a provider (optionally per model).

        Thread-safe, cached for 60s. Returns None if no observation yet.
        """
        ...

    def get_all_rates(self) -> dict[str, float]:
        """Get provider-level aggregate rates (averaged across models)."""
        ...

    # ── Per-provider measurement methods ──────────────────────────

    def _measure_zai_amortized(self) -> dict[tuple[str, str], float]:
        """z.ai: monthly_fee / month_to_date_tokens * 1e6."""
        ...

    def _measure_ollama_billing(self) -> dict[tuple[str, str], float]:
        """Ollama: API usage cost / tokens. Fallback: amortize $100/mo."""
        ...

    def _measure_ppq_burn_ledger(self) -> dict[tuple[str, str], float]:
        """PPQ: SUM(cost_usd) / (SUM(tokens) / 1e6) from api_burn.db."""
        ...

    def _measure_deepinfra_spend(self) -> dict[tuple[str, str], float]:
        """DeepInfra: daily_spend actual_cost / tokens."""
        ...

    def _measure_openrouter_spend(self) -> dict[tuple[str, str], float]:
        """OpenRouter: response cost / tokens (needs proxy logging fix)."""
        ...
```

### 6.2 `src/price_change_detector.py` (~100 lines)

**Responsibility:** Detect price changes using CUSUM or simple threshold rules.

```python
class PriceChangeDetector:
    """Detects rate changes using CUSUM (cumulative sum) control chart.

    Fires an alert when the cumulative deviation from the Kalman-smoothed
    rate exceeds a threshold, indicating a real price change (promotion,
    price increase, new pricing tier).
    """
    def __init__(self, threshold: float = 0.15):  # 15% deviation
        ...
    def check(self, provider: str, observed: float, smoothed: float) -> bool:
        ...
```

### 6.3 New SQLite table: `price_observations`

```sql
CREATE TABLE IF NOT EXISTS price_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    provider TEXT NOT NULL,
    model TEXT,
    observed_rate_per_m REAL NOT NULL,
    source TEXT NOT NULL,          -- 'ollama_api', 'ppq_ledger', 'zai_amortized',
                                   -- 'deepinfra_actual', 'openrouter_actual'
    confidence REAL DEFAULT 1.0,   -- 0-1, based on sample size
    sample_tokens INTEGER,         -- tokens used to compute this rate
    sample_cost_usd REAL,          -- actual cost used to compute this rate
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_price_obs_provider_ts
    ON price_observations(provider, model, ts);
```

### 6.4 Cron job: `collect_real_prices` (5-minute interval)

A Hermes cron job that calls `RealPriceTracker.collect_all()`, feeds the
results into the LiveRouter's Kalman filters, and runs the change detector.

---

## 7. TASK BREAKDOWN — Ordered Implementation Steps

### Phase 1: Foundation (no behavior change)

| # | Task | Effort | Description |
|---|------|--------|-------------|
| **1.1** | Create `price_observations` table schema | S | Add migration to zai_usage.db with CREATE TABLE + index. |
| **1.2** | Build `src/real_price_tracker.py` — skeleton | M | Class structure, thread-safe cache, `get_latest_rate()`, `get_all_rates()`. |
| **1.3** | Implement `_measure_zai_amortized()` | S | Query `api_calls` SUM(total_tokens) for current month, divide by monthly_fee_usd. Test against known data. |
| **1.4** | Implement `_measure_ollama_billing()` | M | Parse `fetch_ollama_usage()` response for per-model cost. Fallback to amortization. |
| **1.5** | Implement `_measure_ppq_burn_ledger()` | S | Query `api_burn.db` → `ppq_queries` for cost_usd / tokens. |
| **1.6** | Implement `_measure_deepinfra_spend()` | S | Query `daily_spend` for deepinfra tier (already has actual_cost). |
| **1.7** | Implement `_measure_openrouter_spend()` | S | Same pattern as DeepInfra; check if cost is logged per-request. |
| **1.8** | Write `collect_all()` + `_write_observations()` | S | Orchestrate all collectors, write to price_observations table. |

### Phase 2: Wire Into Kalman (shadow mode)

| # | Task | Effort | Description |
|---|------|--------|-------------|
| **2.1** | Add `LiveRouter._refresh_rates()` | M | Method that reads tracker observations and calls `price_kalman.update()`. |
| **2.2** | Add `LiveRouter._update_kalman_from_tracker()` | M | Background thread or callback that runs `_refresh_rates()` every 5 min. |
| **2.3** | Replace `_base_rates` reads with Kalman reads | M | In `_do_select_failover()`, read from `self._price_kalmans[name].base_rate` instead of `self._base_rates[name]`. |
| **2.4** | Rename `_DEFAULT_CONVERGED_RATES` → `_COLD_START_RATES` | S | Add comment: "fallback only, replaced by real observations within 5 min." |
| **2.5** | Apply same changes to `ShadowHook` | M | Mirror changes in shadow_hook.py (seeds → cold-start, add observation pipeline). |
| **2.6** | Write unit tests for tracker | M | Test each `_measure_*` method with known data from the DBs. |

### Phase 3: Eliminate Hardcoded Rates (production wiring)

| # | Task | Effort | Description |
|---|------|--------|-------------|
| **3.1** | Update `pricing_engine.py` — dynamic `EXTRA_USAGE_BASE_RATE` | S | Read from tracker instead of constant 0.024. |
| **3.2** | Update `zai_proxy.py` — `_estimate_cost_usd()` reads tracker | M | Replace `_MODEL_COST_PER_1M` lookup with `RealPriceTracker.get_latest_rate()`. Keep table as cold-start fallback. |
| **3.3** | Update `zai_proxy.py` — `_get_ollama_cloud_cost_per_1m()` | S | Read base rate from tracker, compute extra rate as base × multiplier (from config). |
| **3.4** | Update `providers.yaml` — remove `cost_per_1m_*` from externals | S | These are observations, not config. Keep only monthly_fee_usd and structural config. |
| **3.5** | Add Hermes cron job `collect_real_prices` | S | 5-min interval cron that calls `tracker.collect_all()` + feeds Kalman. |

### Phase 4: Change Detection + Alerting

| # | Task | Effort | Description |
|---|------|--------|-------------|
| **4.1** | Build `src/price_change_detector.py` | M | CUSUM detector. Fires on >15% sustained deviation from Kalman. |
| **4.2** | Log price changes to `anomaly_events` table | S | INSERT into existing anomaly_events table with category='price_change'. |
| **4.3** | Add velocity readout to `get_kalman_state()` | S | Expose `price_kalman.velocity` per provider for monitoring. |

### Phase 5: Dashboard Integration

| # | Task | Effort | Description |
|---|------|--------|-------------|
| **5.1** | Update CVM `computePricing()` to read from `price_observations` | M | Replace hardcoded `flatKeyCostPerM` with DB query. |
| **5.2** | Add `get_price_history` CVM tool data source | S | Query price_observations table for time-series. |
| **5.3** | Show real vs hardcoded comparison in snapshot | S | Include `_meta.real_vs_seed_delta` to show how much rates have drifted from cold-start. |
| **5.4** | Add rate-change alerts to snapshot | S | Surface recent anomaly_events with category='price_change'. |

### Phase 6: Validation + Rollout

| # | Task | Effort | Description |
|---|------|--------|-------------|
| **6.1** | Shadow comparison: old vs new rates for 48h | M | Log both hardcoded rate and real rate for each routing decision. |
| **6.2** | Verify Ollama $0.0155/M detection | S | Confirm tracker measures the real billing rate, not the circular daily_spend rate. |
| **6.3** | Validate all 6 providers produce observations | S | Ensure no provider falls back to cold-start for >10 min. |
| **6.4** | Performance test: tracker < 50ms, Kalman update < 1ms | S | Ensure no routing path degradation. |

---

## 8. TESTING — How to Validate Real Rates Are Correct

### 8.1 Per-Provider Validation

| Provider | Test | Expected |
|----------|------|----------|
| Ollama | Fetch `/api/usage`, extract cost/tokens | $0.0155/M (measured) |
| z.ai ours | $155 / (4975268693 tokens / 1e6) | $0.0000311/M |
| z.ai friend | $0 / (any tokens / 1e6) | $0/M (then 21% premium in optimizer) |
| DeepInfra | daily_spend cost/tokens (7-day avg) | ~$1.30/M (already real) |
| PPQ | burn_ledger cost/tokens (7-day avg) | Measured from actual queries |
| OpenRouter | response cost/tokens | Measured from actual responses |

### 8.2 Integration Tests

```python
def test_tracker_produces_all_providers():
    tracker = RealPriceTracker(zai_db, burn_db)
    rates = tracker.collect_all()
    for provider in ["ours", "friend", "ollama_cloud", "ppq", "openrouter", "deepinfra"]:
        assert any(k[0] == provider for k in rates), f"Missing {provider}"

def test_kalman_converges_to_real_rate():
    """After 10 observations, Kalman base_rate within 10% of real rate."""
    tracker = RealPriceTracker(...)
    kalman = PriceKalman(initial_rate=0.024)  # cold start
    for _ in range(10):
        obs = tracker.get_latest_rate("ollama_cloud")
        kalman.update(obs)
    assert abs(kalman.base_rate - 0.0155) / 0.0155 < 0.10

def test_rate_change_detected():
    """Simulate a price increase and verify the detector fires."""
    detector = PriceChangeDetector(threshold=0.15)
    smoothed = 0.0155
    for _ in range(5):
        assert not detector.check("ollama_cloud", 0.020, smoothed)  # small drift
    assert detector.check("ollama_cloud", 0.030, smoothed)  # 93% jump
```

### 8.3 Shadow Mode Validation

Run for 48h with both old (hardcoded) and new (tracked) rates logged in
`routing_shadow_decisions`. Verify:
- No routing decision changes more than expected (Ollama gets cheaper → used more).
- Total cost decreases (better rates → better routing).
- No provider ever shows 0 or infinite rate (NaN guard).

---

## 9. DASHBOARD — How to Surface Real-Time Rates in CVM/Snapshot

### 9.1 CVM Snapshot `pricing` Block (Updated)

```json
{
  "pricing": {
    "ours": {
      "cost_basis": 0.000031,
      "your_price": 0.000039,
      "margin_pct": 21.0,
      "source": "amortized",
      "sample_tokens": 4975268693
    },
    "ollama_cloud": {
      "cost_basis": 0.0155,
      "your_price": 0.0196,
      "margin_pct": 21.0,
      "source": "ollama_billing_api",
      "sample_tokens": 877448651,
      "seed_rate": 0.024,
      "drift_pct": -35.4
    },
    ...
  }
}
```

### 9.2 Price History Tool (`get_price_history`)

Query `price_observations` table for a time-series:
```
GET /price_history?provider=ollama_cloud&hours=168
→ [{ts, rate, source}, ...]
```

### 9.3 Rate Change Alerts

Surface recent `anomaly_events` with `category='price_change'` in the snapshot's
`alerts` block:
```json
{
  "alerts": [
    {
      "severity": "warning",
      "title": "Ollama Cloud rate decreased 35%",
      "detail": "$0.024/M → $0.0155/M (measured from billing API)"
    }
  ]
}
```

---

## 10. RISKS + MITIGATIONS

| Risk | Impact | Mitigation |
|------|--------|------------|
| **Ollama API cost data format changes** | Tracker returns stale rates | Fallback to amortization; 30s cache means we retry quickly. |
| **PPQ burn ledger is empty** (no recent queries) | No PPQ rate available | Fall back to cold-start seed; log warning; retry on next cycle. |
| **z.ai month-start token count is low** → amortized rate very high | Overestimates z.ai cost | Use a minimum sample threshold (1M tokens) before publishing; otherwise use previous rate. |
| **DeepInfra prompt caching skews rate** | Per-request rate varies wildly | Weight by token volume; Kalman smooths; use `measurement_noise=1e-3` for high-variance providers. |
| **Tracker DB write blocks routing** | Latency spike | Use separate connection; WAL mode; writes are INSERT only (no locks). |
| **Cold-start rate used for >10 min** | Routing uses wrong rates | Monitor: alert if no observation for a provider in 15 min. |
| **OpenRouter doesn't return cost** | No per-request rate | Need to add response body parsing in the proxy (like DeepInfra). Alternatively, use published prices from their API as a starting observation. |
| **Rate changes mid-session** | Routing decisions based on stale rate | Kalman velocity term tracks trend; 5-min cycle catches changes within 1-2 cycles. |
| **Multiple models on same provider have different prices** | Aggregating to provider-level loses precision | Track per `(provider, model)` in the tracker; aggregate to provider-level only when the optimizer queries it. |
| **Subscription cancellation** (ours key cancelled → $0/mo) | Amortized rate drops to 0 | This is correct behavior — $0/mo means $0/M. The optimizer handles this via `MIN_EFFECTIVE_PRICE = 0.001`. |

---

## Appendix A: Current State — What We Already Have

### Already Correct (no change needed)
- **DeepInfra actual cost extraction** — proxy already parses `estimated_cost` from
  responses and logs to `daily_spend`. The $1.30/M figure is real.
- **PPQ burn ledger** — `api_burn.db` → `ppq_queries` has `cost_usd` per query.
- **Ollama usage API fetch** — `fetch_ollama_usage()` works, returns usage fractions.
- **CVM PPQ rate** — CVM already computes `ppqCostPerM` from the burn ledger.
- **CVM Ollama amortization** — CVM already amortizes `$100/mo` over 30-day tokens.
- **`providers.yaml` monthly_fee_usd** — real config values, not guesses.

### Already Built but Misused
- **`PriceKalman.update()`** — exists, works correctly, but nobody calls it with real data.
- **`LiveRouter.record_request(cost_estimate=...)`** — exists, but the proxy never
  passes a cost estimate.
- **`daily_spend` table** — already records `actual_cost` for DeepInfra, but
  ollama_cloud/friend costs are circular (computed from the same hardcoded constant).

### Completely Missing
- **RealPriceTracker module** — the observation pipeline.
- **Price change detection** — no alerting on rate changes.
- **`price_observations` table** — no persistent record of measured rates.
- **Per-request OpenRouter cost logging** — not extracted from responses.
- **Ollama billing cost extraction** — `fetch_ollama_usage()` fetches usage
  fractions but does NOT extract per-model cost from the response (the `activity.cost`
  fields). Needs parsing.

---

## Appendix B: Measurement Noise Recommendations

| Provider | measurement_noise | Rationale |
|----------|------------------|-----------|
| z.ai (ours/friend) | 1e-6 | Amortized rate changes slowly (only as tokens accumulate). |
| ollama_cloud (billing API) | 1e-4 | Billing cost is exact but may include promotional periods. |
| ollama_cloud (amortized) | 1e-6 | Same as z.ai — slowly changing. |
| ppq | 1e-4 | Per-query cost is exact but varies by input/output ratio. |
| deepinfra | 1e-3 | Prompt caching causes high per-request variance. |
| openrouter | 1e-3 | Similar variance to DeepInfra. |

These are starting points. After 48h of operation, analyze the Kalman residual
distribution and auto-tune `measurement_noise` to the empirical variance (same
pattern as `ConsumptionKalman.from_history()`).
