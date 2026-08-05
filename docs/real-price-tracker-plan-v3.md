# Plan v3: Real-Time Price Tracking with Extra-Usage Awareness

**Revised:** 2026-08-05 (after Felix's corrections)
**Key insight:** Kimi K3 = always extra usage. GLM-5.2 = extra after 5h limit. Both burn prepaid balance.

## Corrected Understanding

| Model | Included? | When Extra? | Volume | Cost Risk |
|---|---|---|---|---|
| GLM-5.2 | YES (within 5h/7d limits) | When session.usage >= 1.0 | 440M tok/wk | HIGH — heavy volume in extra mode |
| kimi-k3:cloud | NO — always extra | Always | 123K tok/wk | LOW — small volume |
| kimi-k2.7-code | NO — always extra | Always | 9.5M tok/wk | MEDIUM |
| deepseek-v4-flash | YES (within limits) | When limits hit | Low | LOW |

**activity.cost ($38.51/4wk) = REAL prepaid balance burn** (not list value), because:
- Kimi models are always metered (never included)
- GLM-5.2 occasionally crosses into extra mode
- This is actual money spent from prepaid balance

**Measured rates (from real billing):**
- GLM-5.2 blended: $0.0155/M (includes both included + extra periods)
- kimi-k2.7-code: $0.0209/M
- Blended all: $0.0166/M

**The rates are LOW because most GLM-5.2 usage is in included mode.** Extra-mode rate is likely higher but diluted by the volume of included-mode calls.

## Strategy (Felix's seed → replace approach)

### Routing Priority (proactive, not reactive)

1. **GLM-5.2 before 5h limit (usage < 0.85):** Route to Ollama (cheapest, included)
2. **GLM-5.2 approaching limit (usage 0.85-0.99):** Start throttling, prefer z.ai keys
3. **GLM-5.2 at limit (usage >= 1.0):** STOP routing to Ollama. Use z.ai/PPQ only.
4. **Kimi K3:** Always Ollama (no alternative). Accept the extra-usage cost. Track separately.
5. **Kimi K2.7-code:** Always Ollama (exclusive). Track cost.

### Seed Values (immediate)

```python
# Seed rates — replaced by real measurements as cost_usd data accumulates
SEED_RATES = {
    "ollama_cloud": {
        "glm-5.2": {"included": 0.0155, "extra": 0.05},   # extra is estimate
        "kimi-k3:cloud": {"extra": 0.05},                   # always extra
        "kimi-k2.7-code": {"extra": 0.021},                 # measured
        "deepseek-v4-flash": {"included": 0.001, "extra": 0.05},
    },
    "ours": {"all": 0.0},          # flat-rate, $0 marginal
    "friend": {"all": 0.0},        # flat-rate, $0 marginal
    "ppq": {"all": 0.14},          # will be replaced by measured
    "openrouter": {"all": 0.135},  # will be replaced by measured
    "deepinfra": {"all": 0.05},    # will be replaced by measured
}
```

### Replace Path (automatic)

1. **Day 1-2:** Wire _extract_cost() into hot path for OpenRouter/DeepInfra/PPQ. Real measured costs start flowing. Ollama uses seed + aggregate recalibration.
2. **Day 3-7:** Enough data accumulates. real_price_tracker replaces seeds with rolling 7d measured rates.
3. **Ongoing:** Daily recalibration of Ollama aggregate from /api/usage activity.cost.

### Extra-Usage Rate Measurement

We can measure the extra-usage rate for GLM-5.2 by:
1. Splitting calls into "included" (session.usage < 1.0) and "extra" (session.usage >= 1.0)
2. Comparing cost_usd/token between the two groups
3. The difference = extra-usage surcharge

Problem: Ollama doesn't return per-call cost. So we use the aggregate:
- Total activity.cost / total tokens = blended rate
- If we know what fraction of tokens were "extra", we can solve for the extra rate

Example: If 90% of tokens are included (rate ~$0/M, subscription covers) and 10% are extra:
- $38.51 = 0.9 × 2.07B × $0 + 0.1 × 2.07B × extra_rate
- $38.51 = 207M × extra_rate
- extra_rate = $0.000186/M ← way too low, this model is wrong

Alternative: ALL tokens are billed at list price, included quota is a "credit":
- activity.cost = ALL tokens × list_rate
- $38.51 / 2.327B = $0.0166/M (consistent)
- Extra usage = same rate, just billed from prepaid instead of subscription

This means extra-usage rate ≈ $0.0166/M — SAME as included. The "extra" cost is just that you're paying OUT OF POCKET (prepaid) instead of from your subscription.

**If this is correct:** The routing concern isn't the RATE, it's the BALANCE. We need to track prepaid balance burn rate and alert when it's running low.

## Felix's Question: Can we calculate from topup + burn?

YES — if Felix tells us:
1. How much prepaid balance was added (topup amount)
2. Over what period
3. Current remaining balance

Then: burn_rate = (topup - remaining) / time_period
And: effective_rate = burn / tokens_during_period

## Implementation (7 tasks, revised)

### RP-1 ✅ DONE — cost_usd/cost_source columns added + backfilled
### RP-2: Wire _extract_cost() into ALL hot paths (Ollama, OpenRouter, DeepInfra, PPQ)
### RP-3: Build real_price_tracker.py with seed → replace logic
### RP-4: Replace hardcoded rates with tracker calls
### RP-5: Proactive GLM-5.2 throttling at usage >= 0.85 (prevent expensive crossover)
### RP-6: Dashboard shows measured rates + balance tracking
### RP-7: Cold review + go live
