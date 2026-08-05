# Real Extra-Usage Rate Analysis — MEASURED, NOT ESTIMATED

## Breakthrough Discovery

The Ollama `/api/usage` endpoint's `activity` section reports ONLY extra-usage-billed requests — NOT all requests. This is proven by the request count discrepancy:

| Model | API activity.requests | DB total calls | Ratio | Interpretation |
|-------|----------------------|----------------|-------|----------------|
| glm-5.2 | 954 | 28,132 | 3.4% | Only 3.4% of calls hit extra usage |
| kimi-k2.7-code | 276 | 3,801 | 7.3% | 7.3% of calls hit extra usage |
| kimi-k3 | 21 | 18 | ~100% | ALL calls are extra usage (exclusive model) |
| deepseek-v4-flash | 39 | 0* | N/A | Model name mismatch in DB |

## REAL Extra-Usage Rates (calculated from billing data)

| Model | Extra cost | Extra tokens (est) | Rate $/M | vs PPQ $0.14 | vs Flat $0.024 |
|-------|-----------|-------------------|----------|-------------|----------------|
| kimi-k3 | $0.93 | 123,463 | **$7.53** | 54x PPQ | 314x flat |
| glm-5.2 | $32.25 | ~70.4M (proportional) | **$0.46** | 3.3x PPQ | 19x flat |
| kimi-k2.7-code | $5.28 | ~18.3M (proportional) | **$0.29** | 2.1x PPQ | 12x flat |

## Key Facts

1. **$38.52 is REAL extra-usage billing** — not notional value. This came from the prepaid balance.
2. **kimi-k3 is ALWAYS extra usage** — it's an exclusive model not covered by included quota
3. **glm-5.2 extra-usage rate ($0.46/M) IS above PPQ ($0.14/M)** — should reroute when extra usage active
4. **kimi-k3 extra-usage rate ($7.53/M)** — extremely expensive but exclusive (no alternative provider)
5. **3.4% of glm-5.2 calls triggered extra usage** — these are calls past the included quota

## Seeding Strategy for RealtimePricing

### Seed values (best available, flagged as estimates)
```
ours:            $0.001/M    (sunk cost, amortized — MIN_EFFECTIVE_PRICE)
friend:          $0.001/M    (sunk cost, amortized — MIN_EFFECTIVE_PRICE)
ollama_cloud:    $0.0155/M   (MEASURED — activity.cost / total tokens, included mode)
ollama_cloud_extra: $0.46/M  (MEASURED — extra-usage rate for glm-5.2)
ollama_cloud_kimi3: $7.53/M  (MEASURED — exclusive model, always extra)
ppq:             $0.14/M     (known rate)
openrouter:      $0.135/M    (known rate)
deepinfra:       $1.30/M     (known rate)
```

### Replacement path
1. Seed with above values on startup
2. Cron refresh every 5 min fetches Ollama API → replaces ollama rates
3. DB query replaces z.ai amortized rates (monthly_fee / tokens)
4. PPQ/OpenRouter/DeepInfra rates from spend DB
5. Within 1-2 cycles (5-10 min), ALL rates are measured, not seeded

### Transition states
- **cold_start** (0-5 min): seed values, `is_measured=False`
- **first_observation** (5-10 min): real data, `is_measured=True` for providers with data
- **converged** (30+ min): Kalman-smoothed, stable, `is_measured=True` for all
