# Routing Telemetry Dataset

Exported from the Merchant Routing Engine's production SQLite databases on **2026-08-23**.

## Overview

This dataset contains ~11 days of routing telemetry from a cost-minimizing LLM API reverse proxy that uses Kalman filters to route requests to the cheapest viable provider. The data covers **2026-08-12 to 2026-08-23 UTC**.

## General Notes

- **Timestamps** are Unix epoch seconds (floating-point, UTC) unless otherwise noted. The `daily_spend` and `ppq_daily_used` tables use `date` columns in `YYYY-MM-DD` format instead.
- **Costs** are in **USD**.
- In `routing_shadow_decisions`, **`agree=1`** means the live router and the shadow optimizer selected the same provider. `agree=0` means they diverged.
- All CSVs are UTF-8 encoded with comma delimiters and minimal quoting (RFC 4180 compliant).

## Files

### api_calls.csv — 100,249 rows

Every API call made through the proxy. The core telemetry table.

| Column | Description |
|--------|-------------|
| `id` | Row ID |
| `ts` | Unix timestamp (epoch seconds) |
| `key_name` | API key used (ours, friend, ppq, openrouter, etc.) |
| `key_suffix` | Last 4 chars of the API key |
| `model` | Model requested (e.g. glm-5.2, glm-4.5-air, glm-4.5-flash) |
| `prompt_tokens` | Input tokens |
| `completion_tokens` | Output tokens |
| `total_tokens` | Sum of prompt + completion |
| `tier` | Quality tier (high, standard, low) |
| `cache_hit` | Whether response was served from cache (0/1) |
| `ollama_hit` | Whether Ollama Cloud was used (0/1) |
| `ppq_hit` | Whether PPQ was used (0/1) |
| `status_code` | HTTP status (200, 429, 402, etc.) |
| `error` | Error message if any |
| `duration_ms` | Request latency in milliseconds |
| `cost_usd` | Cost in USD |
| `cost_source` | How cost was calculated (flat_rate, per_token, etc.) |
| `session_id` | Session identifier |
| `task_type` | Type of task (coding, research, docs, review, mechanical) |

Time range: 2026-08-12 17:02 UTC → 2026-08-22 22:49 UTC

---

### provider_telemetry.csv — 63,071 rows

Per-request telemetry for external providers. Tracks latency, token billing accuracy, and error types.

| Column | Description |
|--------|-------------|
| `id` | Row ID |
| `ts` | Unix timestamp |
| `provider` | Provider name |
| `response_received` | Whether a response was received (0/1) |
| `response_valid` | Whether the response was valid (0/1) |
| `latency_ms` | Response latency in milliseconds |
| `error_type` | Type of error (timeout, empty_content, auth, etc.) |
| `billed_tokens` | Token count reported by provider (billed) |
| `actual_tokens` | Token count measured locally (actual) |
| `token_mismatch` | Difference between billed and actual |
| `model` | Model used |

Time range: 2026-08-14 12:32 UTC → 2026-08-22 22:49 UTC

---

### routing_shadow_decisions.csv — 158,859 rows

Shadow mode comparison: what the live system chose vs what the Kalman optimizer would have chosen. The largest table — every request generates a shadow decision.

| Column | Description |
|--------|-------------|
| `id` | Row ID |
| `ts` | Unix timestamp |
| `live_provider` | Provider the live system selected |
| `live_model` | Model the live system used |
| `shadow_provider` | Provider the shadow optimizer would have chosen |
| `shadow_model` | Model the optimizer would have used |
| `shadow_cost` | Cost the shadow decision would have incurred |
| `live_cost` | Actual cost of the live decision |
| `tokens` | Token count for this request |
| `agree` | **1** = live and shadow agreed; **0** = they diverged |
| `reason` | Reason for the decision |
| `pressure_provider` | Provider chosen under pressure (if applicable) |
| `pressure_model` | Model under pressure routing |
| `pressure_cost` | Cost under pressure routing |
| `actual_cost` | Actual cost incurred |
| `divergence` | Cost difference between live and shadow |
| `is_429` | Whether a 429 (rate limit) was involved (0/1) |
| `paid_provider` | Whether a paid per-token provider was used (0/1) |
| `requested_model` | Model the user originally requested |
| `per_model_base_rate` | Per-model base rate from Kalman |
| `per_model_source` | Source of the rate (converged, seed, measured) |
| `quota_regime` | Current quota regime (normal, pressured, exhausted) |

Time range: 2026-08-12 17:02 UTC → 2026-08-22 22:49 UTC

---

### routing_live_decisions.csv — 17,060 rows

Live routing decisions made by the LiveRouter (failover from z.ai keys to external providers). Includes pace multipliers.

| Column | Description |
|--------|-------------|
| `id` | Row ID |
| `ts` | Unix timestamp |
| `live_provider` | Provider selected |
| `live_model` | Model used |
| `shadow_provider` | Shadow optimizer's choice |
| `shadow_model` | Shadow optimizer's model |
| `shadow_cost` | Shadow cost |
| `live_cost` | Live cost |
| `tokens` | Token count |
| `agree` | 1 = agreed, 0 = diverged |
| `reason` | Decision reason |
| `pace_mults` | JSON object of pace multipliers per provider |

Time range: 2026-08-14 12:53 UTC → 2026-08-22 22:49 UTC

---

### routing_profit.csv — 5,648 rows

Savings tracking: for each routing decision, how much was saved by choosing the cheapest provider vs the next-best alternative.

| Column | Description |
|--------|-------------|
| `id` | Row ID |
| `ts` | Unix timestamp |
| `provider_used` | Provider that was selected |
| `effective_price` | Effective price ($/M) of the chosen provider |
| `next_best_price` | Effective price of the next-best provider |
| `savings_per_1m` | Savings per million tokens ($/M) |
| `estimated_tokens` | Estimated token count for the request |
| `estimated_savings_usd` | Estimated savings in USD |
| `is_peak_hour` | Whether this was during peak hours (0/1) |
| `mode` | Routing mode (live, shadow) |

Time range: 2026-08-14 13:03 UTC → 2026-08-22 22:46 UTC

---

### key_decisions.csv — 411,622 rows

Key selection decisions: which z.ai API key was chosen for each request and why. The largest table by row count.

| Column | Description |
|--------|-------------|
| `id` | Row ID |
| `ts` | Unix timestamp |
| `chosen_key` | Key that was selected (ours, friend, none) |
| `reason` | Why this key was chosen |
| `ours_pct` | Quota usage percentage for "ours" key |
| `friend_pct` | Quota usage percentage for "friend" key |
| `ours_available` | Whether "ours" key was available (0/1) |
| `friend_available` | Whether "friend" key was available (0/1) |

Time range: 2026-08-12 13:33 UTC → 2026-08-22 22:49 UTC

---

### kalman_samples.csv — 939 rows

Snapshots of the ConsumptionKalman filter state over time. Shows how the burn-rate predictor converged.

| Column | Description |
|--------|-------------|
| `id` | Row ID |
| `ts` | Unix timestamp |
| `key` | API key (ours, friend) |
| `window` | Quota window (5h, weekly, monthly) |
| `used_pct_observed` | Observed quota usage percentage |
| `projected_additional_pct` | Projected additional usage before window resets |
| `projected_total_pct` | Projected total usage percentage |
| `burn_rate_tph` | Burn rate (tokens per hour) |
| `velocity_tph2` | Rate of change of burn rate |
| `uncertainty` | Standard deviation of the estimate |
| `exhausts_in_hours` | Hours until quota exhaustion (null if won't exhaust) |
| `will_exhaust` | Whether quota will exhaust before reset (0/1) |
| `note` | Optional note |

Time range: 2026-08-14 12:46 UTC → 2026-08-19 16:21 UTC

---

### daily_spend.csv — 49 rows

Daily spend breakdown by tier.

| Column | Description |
|--------|-------------|
| `date` | Date in YYYY-MM-DD format |
| `tier` | Quality tier (high, standard, low) |
| `spend_usd` | Spend in USD |
| `call_count` | Number of API calls |
| `token_count` | Total tokens consumed |

Time range: 2026-08-12 → 2026-08-23

---

### price_observations.csv — 7,346 rows

Observed provider prices over time. These are the raw inputs to the PriceKalman filter.

| Column | Description |
|--------|-------------|
| `id` | Row ID |
| `ts` | Unix timestamp |
| `provider` | Provider name |
| `model` | Model name |
| `rate_per_m` | Observed rate per million tokens ($/M) |
| `source` | How the rate was determined (converged, seed, measured, published) |
| `is_measured` | Whether this was a directly measured rate (0/1) |
| `confidence` | Confidence level (0-1) |
| `sample_tokens` | Token count in the sample |
| `sample_cost_usd` | Cost of the sample in USD |
| `velocity` | Price velocity (rate of change) |
| `note` | Optional note |

Time range: 2026-08-12 13:45 UTC → 2026-08-17 13:55 UTC

---

### pressure_decisions.csv — 16,839 rows

Quota pressure routing decisions — what happens when z.ai keys are near exhaustion.

| Column | Description |
|--------|-------------|
| `id` | Row ID |
| `ts` | Unix timestamp |
| `state` | Pressure state (normal, pressured, exhausted) |
| `requested_model` | Model the user requested |
| `would_serve_model` | Model that would actually be served |
| `would_provider` | Provider that would be used |
| `interactive` | Whether this was an interactive request (0/1) |
| `reason` | Reason for the pressure decision |

Time range: 2026-08-17 14:42 UTC → 2026-08-22 22:47 UTC

---

### rate_limit_samples.csv — 2,075 rows

Rate limit inter-arrival time samples. Used to characterize rate-limiting behavior.

| Column | Description |
|--------|-------------|
| `id` | Row ID |
| `ts` | Unix timestamp |
| `inter_arrival` | Time between consecutive requests (seconds) |
| `consecutive` | Number of consecutive requests |
| `wait_used` | Whether a wait/retry was used (0/1) |
| `source` | Source of the sample |

Time range: 2026-08-14 12:53 UTC → 2026-08-22 22:48 UTC

---

### anomaly_events.csv — 5,577 rows

Detected anomalies in the routing system (rate limits, exhaustion events, unexpected errors).

| Column | Description |
|--------|-------------|
| `id` | Row ID |
| `ts` | Unix timestamp |
| `severity` | Severity level (info, warning, error, critical) |
| `category` | Category (quota, rate_limit, provider_error, etc.) |
| `title` | Short title |
| `detail` | Detailed description |
| `alerted` | Whether an alert was sent (0/1) |
| `resolved` | Whether the anomaly was resolved (0/1) |

Time range: 2026-08-12 13:30 UTC → 2026-08-22 22:48 UTC

---

### key_health.csv — 6 rows

Current health state of each API key. Snapshot, not time-series.

| Column | Description |
|--------|-------------|
| `key_name` | Key name (ollama_cloud, telnyx, ours, friend, etc.) |
| `healthy` | Whether the key is healthy (0/1) |
| `failure_count` | Number of consecutive failures |
| `last_failure_ts` | Timestamp of last failure (epoch seconds) |
| `last_error_type` | Type of last error |
| `backoff_until` | Timestamp until backoff expires |
| `disabled_manually` | Whether manually disabled (0/1) |
| `backoff_seconds` | Backoff duration in seconds |
| `updated_ts` | When this row was last updated (epoch seconds) |

Snapshot time: 2026-08-22 22:49 UTC

---

### measured_rates.csv — 6 rows

Directly measured sats/USD rates for providers. Used for Bitcoin-denominated cost tracking.

| Column | Description |
|--------|-------------|
| `provider` | Provider name |
| `model` | Model name |
| `sats_per_M` | Cost in satoshis per million tokens |
| `usd_per_M` | Cost in USD per million tokens |
| `btc_usd` | BTC/USD exchange rate at measurement time |
| `sats_spent` | Total satoshis spent |
| `prompt_tokens` | Prompt tokens in the measurement |
| `completion_tokens` | Completion tokens in the measurement |
| `measured_at` | Measurement timestamp (epoch seconds) |
| `method` | Measurement method |
| `error` | Error if measurement failed |

Snapshot time: 2026-08-22 22:21–22:28 UTC

---

### ppq_daily_used.csv — 6 rows

Daily PPQ (api.ppq.ai) spend tracking.

| Column | Description |
|--------|-------------|
| `date` | Date in YYYY-MM-DD format |
| `spend_usd` | Spend in USD |
| `requests` | Number of requests |
| `tokens` | Total tokens |
| `storm_blocked` | Whether storm-blocking was active |
| `hour_requests` | Requests in the current hour |
| `last_ts` | Timestamp of last request (epoch seconds) |

Time range: 2026-08-15 → 2026-08-20

---

### token_stats.csv — 8 rows

Model-level token statistics aggregated over the dataset period.

| Column | Description |
|--------|-------------|
| `model` | Model name |
| `n` | Number of requests |
| `p50` | 50th percentile token count |
| `p90` | 90th percentile token count |
| `mean` | Mean token count |
| `max` | Maximum token count |
| `first_ts` | First request timestamp (epoch seconds) |
| `last_ts` | Last request timestamp (epoch seconds) |

## Dataset Size

Total: ~74 MB across 16 CSV files, ~770K rows combined.