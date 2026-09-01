# LLM Routing Telemetry Dataset

Real-world telemetry from a multi-provider LLM failover router (zai_proxy)
in production, covering 2026-08-12 to 2026-09-01 (~3 weeks; `api_calls`
spans 2026-08-12 17:02:56 .. 2026-09-01 18:51:12 UTC).

## What this is

A local reverse proxy (`zai_proxy.py`) fronts multiple LLM providers
(z.ai coding-plan keys, OpenRouter, Telnyx, PPQ, DeepInfra, plus
self-hosted Cashu-metered nodes). It rotates keys, falls over on quota
exhaustion, and records every decision. A Kalman filter predicts
quota exhaustion windows. This dataset is the full telemetry output.

## Provider names kept real

Provider identities (`openrouter`, `telnyx`, `routstrd`, `routstr`,
`ours`, `friend`, `ollama_cloud`, `ppq`, `deepinfra`, `neuralwatt`,
`opencode_go`, `oxalpha`) are preserved — they make the data actionable.
No full API keys exist in this dataset (`key_suffix` was last-4 only and
has been dropped; error/detail text scanned clean and categorized to enums).

## Schema

See `SCHEMA.sql` for the full DDL. Key tables:

| Table | Rows | What it contains |
|---|---|---|
| `api_calls` | 166,640 | Every proxied LLM call: timestamp, provider, model, token counts, cost, status |
| `key_decisions` | 527,960 | Every key-rotation decision: chosen key + reason + quota state |
| `routing_shadow_decisions` | 177,398 | Shadow-mode routing comparisons (what WOULD have been chosen) |
| `routing_live_decisions` | 19,948 | Live vs shadow routing decisions (did the router agree with itself?) |
| `flat_router_shadow_decisions` | 57,460 | FlatRouter shadow comparisons: best-key choice vs flat-router top candidate |
| `pressure_decisions` | 46,414 | Quota-pressure tiering: state (GREEN/AMBER/RED), would-serve provider, downgrade reasons |
| `provider_telemetry` | 65,042 | Per-provider health: response received/valid, latency, token mismatches |
| `anomaly_events` | 55,670 | Cost inefficiency anomalies, routing warnings |
| `provider_balances` | 14,013 | Provider balance/quota snapshots (usage, limit, fraction used) |
| `balance_snapshots` | 16,260 | Per-provider wallet balance time series |
| `ppq_queries` | 730 | Per-query PPQ spend (tokens, cost, query type) |
| `price_observations` | 7,346 | Observed provider pricing snapshots |
| `kalman_samples` | 2,356 | Kalman filter state: burn rate, velocity, exhaustion prediction |
| `routing_profit` | 14,014 | Consumer-mode savings ledger: effective price, next-best, savings |
| `routing_profit_inflated_pre_rewrite` | 1,493 | Savings ledger before the sats-as-USD correction (audit) |
| `daily_spend` | 95 | Daily spend by provider tier |
| `daily_spend_inflated_pre_rewrite` | 4 | Daily spend before the sats-as-USD correction (audit) |
| `api_calls_cost_inflated_pre_rewrite` | 0 | api_calls cost snapshot before the correction (audit; truncated at cutover) |
| `rate_limit_samples` | 2,393 | 429 rate-limit inter-arrival/streak samples |
| `key_health` | 12 | Current per-key health (last error/backoff details dropped) |
| `measured_rates` | 22 | Ground-truth per-token costs via wallet balance deltas |
| `deepinfra_balance` | 1 | DeepInfra account balance |

## Sensitive fields removed

- `api_calls.key_suffix` — dropped (was last 4 chars of API key)
- `api_calls.session_id`, `api_calls.task_type` — dropped (internal session hashes / task labels)
- `api_calls.error` → `api_calls.error_type` — free-text converted to enums
  (`broken_pipe`, `timeout`, `dns_error`, `exhausted`, `auth`, `rate_limit`,
  `parse_error`, `other`, `none`); raw error text dropped
- `api_calls_cost_inflated_pre_rewrite.session_id` — dropped (same convention)
- `anomaly_events.detail` — dropped (kept `title` + `category`)
- `key_health.last_failure_ts`, `backoff_until`, `backoff_seconds`,
  `last_error_type` — dropped
- `provider_balances.raw_json` — dropped (raw API response payloads)
- `balance_snapshots.raw`, `balance_snapshots.error` — dropped
- `ppq_queries.api_key_id` — dropped

All retained text fields passed a fail-loud secret scan at export time
(patterns: `sk-…` API keys, `nsec1…` nostr secrets, `Bearer ` tokens,
email addresses, 64-char hex — **0 hits**).

## Excluded tables

Deliberately NOT included:

- `model_decisions` — empty
- `circuit_breaker_events` — kanban-board operations, not router telemetry
- `resource_metrics` — host monitor data
- `task_duration_samples` — worker task durations
- `ppq_daily_used` — legacy daily rollup (superseded by `ppq_queries` in this release)

## Shadow tables (audit trail)

The `daily_spend_inflated_pre_rewrite`, `routing_profit_inflated_pre_rewrite`
and `api_calls_cost_inflated_pre_rewrite` tables preserve the ORIGINAL
sats-as-USD values before a correction was applied (routstrd/routstr publish
pricing in sats; the code originally treated sats as USD, inflating recorded
spend ~1300x for routstrd). The main tables now contain the corrected USD values.

## Sample queries

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

## Download

The dataset is published as a GitHub Release asset (not committed to git
history to keep clones lean — see ADR-010):

**Release:** https://github.com/felixfelix-bot/merchant-routing-engine/releases/tag/routing-telemetry-2026-09-01

Assets:
- `scrubbed.db.gz` (22.2 MB) — SQLite database with all routing tables
- `*.csv` — per-table CSV exports (22 files)

Prior release (superseded, historical): `routing-telemetry-2026-08-22`

## License

MIT — use freely.