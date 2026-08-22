# LLM Routing Telemetry Dataset

Real-world telemetry from a multi-provider LLM failover router (zai_proxy)
in production, covering 2026-07-27 to 2026-08-23 (~4 weeks).

## What this is

A local reverse proxy (`zai_proxy.py`) fronts multiple LLM providers
(z.ai coding-plan keys, OpenRouter, Telnyx, PPQ, DeepInfra, plus
self-hosted Cashu-metered nodes). It rotates keys, falls over on quota
exhaustion, and records every decision. A Kalman filter predicts
quota exhaustion windows. This dataset is the full telemetry output.

## Provider names kept real

Provider identities (`openrouter`, `telnyx`, `routstrd`, `routstr`,
`ours`, `friend`, `ollama_cloud`, `ppq`, `deepinfra`) are preserved —
they make the data actionable. No full API keys exist in this dataset
(`key_suffix` was last-4 only and has been dropped; error/detail text
scanned clean and categorized to enums).

## Schema

See `SCHEMA.sql` for the full DDL. Key tables:

| Table | Rows | What it contains |
|---|---|---|
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

## Sensitive fields removed

- `api_calls.key_suffix` — dropped (was last 4 chars of API key)
- `api_calls.session_id` — dropped (internal session hashes)
- `api_calls.task_type` — dropped
- `anomaly_events.detail` — dropped (kept `title` + `category`)
- `key_health.last_failure_ts`, `backoff_until`, `backoff_seconds` — dropped
- `error` / `error_type` fields — categorized to enums (`broken_pipe`, `timeout`, `dns_error`, `exhausted`, `none`, etc.)

## Shadow tables (audit trail)

The `daily_spend_inflated_pre_rewrite` and `routing_profit_inflated_pre_rewrite`
tables preserve the ORIGINAL sats-as-USD values before a correction was
applied (routstrd/routstr publish pricing in sats; the code originally
treated sats as USD, inflating recorded spend ~1300x for routstrd). The
main tables now contain the corrected USD values.

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

## License

MIT — use freely.
