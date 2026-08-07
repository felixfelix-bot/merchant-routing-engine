# RP-7 7-Day Convergence Report
Generated: 2026-08-06T14:59:29.569178Z

## Cost Population (last 7 days)

| Day | Date | Total Calls | Populated | % |
|-----|------|-------------|-----------|---|
| 1 | 2026-07-30 | 34121 | 16835 | 49.3% |
| 2 | 2026-07-31 | 20389 | 10195 | 50.0% |
| 3 | 2026-08-01 | 10924 | 5462 | 50.0% |
| 4 | 2026-08-02 | 0 | 0 | 0.0% |
| 5 | 2026-08-03 | 2264 | 1132 | 50.0% |
| 6 | 2026-08-04 | 38998 | 19496 | 50.0% |
| 7 | 2026-08-05 | 46971 | 23467 | 50.0% |

## Real Rates Per Provider (7-day average)

| Provider | Calls | Avg $/M | Min $/M | Max $/M | StDev | CV |
|----------|-------|---------|---------|---------|-------|----|
| ours | 38826 | $0.001549 | $0.001000 | $0.003000 | $0.000893 | 0.576 |
| friend | 33989 | $0.029953 | $0.028989 | $0.086967 | $0.007399 | 0.247 |

## Kalman Convergence Assessment

- **friend**: CV=0.247, mean=$0.029953/M, stdev=$0.007399/M, n=33989 — ✅ CONVERGED
- **ours**: CV=0.576, mean=$0.001549/M, stdev=$0.000893/M, n=38826 — ❌ OSCILLATING

## Price Observations (tracker data)

| Provider | Rate $/M | Source | Measured | Confidence |
|----------|---------|--------|----------|------------|
| deepinfra | $1.299719 | deepinfra_actual | Yes | 0.33 |
| friend | $0.001000 | zai_amortized | No | 0.70 |
| ollama_cloud | $0.223312 | zai_amortized | No | 0.70 |
| openrouter | $0.135000 | published_list | No | 0.30 |
| ours | $0.093425 | zai_amortized | No | 0.70 |
| ppq | $0.140000 | cold_start_fallback | No | 0.00 |

## Overall Verdict

**⚠️ PARTIAL** — some providers converging, monitor continues.