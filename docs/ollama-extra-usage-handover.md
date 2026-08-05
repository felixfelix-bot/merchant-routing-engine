# Handover: Ollama Cloud Pricing — Extra Usage Cost Awareness for Merchant Routing Engine

**To:** Context window maintaining the merchant-routing-engine (`~/merchant-routing-engine/`)
**From:** Manager profile (c03rad0r #3)
**Date:** 2026-08-05
**Re:** Ensure price-first routing accounts for Ollama Cloud extra-usage billing when included quota runs out

---

## TL;DR

Our Ollama Cloud subscription (Max plan, $100/mo) includes a flat-rate quota that resets every 5 hours (session) and every 7 days (weekly). When that included quota is exhausted, Ollama does NOT return 429 — instead it silently switches to "extra usage" billing at per-token rates. **The merchant routing engine currently prices ollama_cloud at a flat $0.024/M tokens regardless of whether we're burning included quota or paid extra credits.** This means the routing optimizer thinks Ollama Cloud is always cheapest, even when it's actually billing us per-token at rates that may exceed PPQ ($0.14/M) or OpenRouter ($0.135/M).

**What we need:** The routing engine should detect when Ollama Cloud transitions from included quota → extra usage, and bump the effective price accordingly so the optimizer routes GLM-5.2 traffic to cheaper alternatives (z.ai off-peak, PPQ, OpenRouter) instead of burning paid extra credits.

---

## Current Architecture

### Proxy (`~/.hermes/bot/zai_proxy.py`)

The proxy is a Python HTTP server on `localhost:9099` that fronts all LLM API calls. Routing priority:

1. **z.ai keys** (ours + friend) — flat-rate subscription, cheapest off-peak
2. **Ollama Cloud** — flat-rate $100/mo, preferred during z.ai peak hours (UTC 6-10)
3. **External failover** (PPQ → OpenRouter → DeepInfra) — pay-per-token, last resort

**When Ollama Cloud gets a 429:**
- `_mark_key_exhausted("ollama_cloud")` → circuit breaker with `error_type="exhausted"`
- Key becomes unhealthy for `exhausted_retry_seconds` (300s default)
- Proxy falls through to `_try_external_failover()` → PPQ/OpenRouter/DeepInfra

**The problem:** Ollama Cloud does NOT always return 429 when included quota runs out. Per their pricing page:

> "Pro and Max users can add extra usage balance. Ollama uses included plan limits first, then draws from the extra usage balance at the model's token rate."

This means after included quota is consumed, the API continues returning 200 OK but starts billing at per-token rates from the extra usage balance. **There is no API signal, no header, no status code change** — the response is identical. The only way to know is to track cumulative usage against known limits.

### Ollama Cloud Plan Details

| Plan | Price | Concurrency | Usage Level |
|------|-------|-------------|-------------|
| Free | $0 | 1 model | Light |
| Pro | $20/mo | 3 models | 50x Free |
| **Max (ours)** | **$100/mo** | **10 models** | **5x Pro (250x Free)** |
| Team | $25/seat/mo (min 5 seats) | — | Per-seat + shared balance |

**Reset cycles:** Session limits reset every 5 hours. Weekly limits reset every 7 days.

**Extra usage:** Max subscribers can add a prepaid balance. When included quota is exhausted, Ollama draws from this balance at the model's token rate. The API continues returning 200 — no 429, no header change.

**Available models on Ollama Cloud:** glm-5.2, glm-5.1, kimi-k2.7-code, kimi-k3:cloud, deepseek-v4-pro, gpt-oss:120b, gemma4:31b, nemotron-3-super, nemotron-3-ultra, minimax-m2.7, and more.

### Merchant Routing Engine (`~/merchant-routing-engine/`)

**Purpose:** Price-first routing optimizer that selects the cheapest viable provider based on:
- Kalman-smoothed base rates (converges over time from observed spend)
- Deterministic multipliers: peak (3x during UTC 6-10), scarcity (linear ramp 50%→100% quota), health (graduated circuit breaker)
- Quality tier filtering (high/standard/low)

**Key files:**
- `src/routing_optimizer.py` — deterministic cost minimizer, collects prices, filters, sorts
- `src/price_kalman.py` — Kalman filter for base rate smoothing
- `src/consumption_kalman.py` — tracks burn rate per provider
- `src/pricing_engine.py` — deterministic multiplier layer (peak, scarcity, health)
- `src/live_router.py` — production wrapper, persistent Kalman state across calls
- `src/shadow_hook.py` — shadow mode bridge (read-only logging)
- `config/providers.yaml` — provider definitions, costs, quotas, model maps

**Current ollama_cloud pricing in the engine:**

| Source | ollama_cloud rate |
|--------|-------------------|
| `zai_proxy.py` `_COST_PER_M_TOKENS` | $0.024/M |
| `live_router.py` `_DEFAULT_CONVERGED_RATES` | $0.023952/M |
| `shadow_hook.py` `_SEED_COSTS` | $0.50/M (seed, converges down) |
| `providers.yaml` | $100/mo flat (no per-token rate listed) |

All three sources treat ollama_cloud as a **flat-rate, constant-cost provider**. The cost never changes based on whether we're in included quota or extra usage.

### Current Traffic Patterns (7-day data)

| Model | Key | Calls | Tokens | Notes |
|-------|-----|-------|--------|-------|
| glm-5.2 | ollama_cloud | 5,298 | 360M | **Bulk of Ollama traffic** |
| kimi-k2.7-code | ollama_cloud | 173 | 9.5M | Consultant profile |
| kimi-k3:cloud | ollama_cloud | 18 | 123K | New consultant profile |
| glm-5.2 | friend | 12,898 | 881M | z.ai courtesy key |
| glm-4.5-flash | ours | 7,886 | 127M | z.ai our key |
| glm-5.2 | ours | 146 | 4.4M | z.ai our key (low — subscription may be issues) |

**Peak day:** 2026-08-05 — 3,121 Ollama Cloud calls, 215M tokens (z.ai both keys exhausted, all traffic fell to Ollama).

**Ollama Cloud daily volume (14d):**
```
2026-07-22:   14M tokens (glm-5.2)
2026-07-23:  205M tokens (glm-5.2) + 23K (kimi)
2026-07-24:  183M tokens (glm-5.2)
2026-07-25:   28M tokens (glm-5.2) + 23M (kimi)
2026-07-27:   68M tokens (glm-5.2) + 48M (kimi)
2026-07-28:   90M tokens (glm-5.2) + 131M (kimi)
2026-07-29:   50M tokens (glm-5.2) + 47M (kimi)
2026-07-30:   94M tokens (glm-5.2) + 2.5M (kimi)
2026-07-31:   12M tokens
2026-08-01:    3M tokens
2026-08-04:   14M tokens + 44K (kimi-k3)
2026-08-05:  216M tokens + 79K (kimi-k3)
```

**Estimated daily usage:** ~50-200M tokens/day on heavy days. Max plan = 250x Free. If Free ≈ 2M tokens/5h session, Max ≈ 500M tokens/5h session ≈ 2.4B tokens/day theoretical max. We're well under that, but a single busy day (like today) can burn through a significant chunk of the weekly limit.

---

## The Problem In Detail

### 1. Ollama Cloud has two cost regimes, but the routing engine only knows one

**Regime A — Included quota (flat rate):** $100/mo already paid. Effective cost = $0/marginal token. The $0.024/M figure is an amortized average, not a marginal cost. The optimizer should see this as **near-free**.

**Regime B — Extra usage (per-token):** After included quota is exhausted, Ollama bills at the model's token rate from the prepaid balance. This rate is **not published** but is likely comparable to other per-token providers ($0.05-0.15/M for glm-5.2-class models). The optimizer should see this as **expensive — potentially more expensive than PPQ ($0.14/M) or OpenRouter ($0.135/M)**.

**Current behavior:** The routing engine always prices ollama_cloud at $0.024/M regardless of regime. This means:
- During included quota: ollama_cloud is priced HIGHER than its true marginal cost ($0). The optimizer may route to z.ai (correctly priced at ~$0) instead of Ollama during off-peak — this is fine but suboptimal.
- During extra usage: ollama_cloud is priced LOWER than its true cost ($0.05-0.15/M). The optimizer routes ALL traffic to Ollama, burning paid credits, when it should be routing to PPQ/OpenRouter.

### 2. No detection mechanism for quota transition

The proxy has no way to know when Ollama Cloud transitions from included → extra usage:
- Ollama API returns 200 OK in both regimes (no 429, no header change)
- No `X-RateLimit-Remaining` or similar headers in the response
- The `/api/usage` endpoint requires different auth (web session cookie, not API key)
- The only signal is a 429 when ALL quota (included + extra balance) is completely exhausted

### 3. GLM-5.2 is the expensive model we route most

GLM-5.2 is our primary workhorse model (5,298 calls, 360M tokens on Ollama in 7 days). It's also likely the most expensive model on Ollama Cloud's per-token extra usage billing. When we're burning extra credits, we want to:
- Route GLM-5.2 traffic to PPQ ($0.14/M) or OpenRouter ($0.135/M) instead
- Keep Ollama Cloud for kimi-k3:cloud and other Ollama-exclusive models that can't be served elsewhere
- Use z.ai keys (flat-rate) whenever they have quota, even during off-peak

---

## What The Merchant Routing Engine Should Do

### Goal 1: Detect Ollama Cloud quota regime

**Option A — Usage tracking (recommended):**
- Track cumulative Ollama Cloud token usage per 5-hour session window and per 7-day weekly window
- The proxy already logs every call to `zai_usage.db` with `key_name='ollama_cloud'` and token counts
- Add a function `_ollama_cloud_quota_state()` that queries the DB for current window usage
- Compare against estimated limits (see below)
- Feed this into the routing optimizer's `quota_state` snapshot (which already has an `ollama_cloud` slot, currently hardcoded to `used_pct: 0.0, remaining: 1_000_000, total: 1_000_000`)

**Option B — Probe-based detection:**
- Periodically call `ollama.com/api/usage` with web session auth (if we can get a cookie)
- More accurate but fragile (auth method differs from API key)

**Option C — 429 as late signal:**
- Keep current behavior — when Ollama Cloud 429s, mark exhausted and failover
- This is the fallback but doesn't help with the extra-usage billing problem (429 only happens when extra balance is ALSO empty)

**Recommended: Option A.** The proxy already has all the data in `zai_usage.db`. The 5-hour and 7-day windows are known. We just need to sum tokens per window and compare against limits.

### Goal 2: Estimate Ollama Cloud limits

Ollama doesn't publish exact token limits. We need to estimate:

- **Max plan = 250x Free** (per their FAQ: "5x Pro", "Pro = 50x Free")
- **Session window = 5 hours, weekly window = 7 days**
- We can calibrate by observing when 429s occur (if they ever do) and back-calculating
- A reasonable starting estimate: ~500M tokens per 5h session, ~3.5B tokens per week
- Store this as a configurable parameter in `config/providers.yaml`

**Calibration approach:**
- Log daily Ollama Cloud usage (already happening)
- If we ever see a 429, note the cumulative usage at that point — that's the hard limit
- Adjust estimates in config over time

### Goal 3: Bump price when in extra-usage regime

When the routing engine detects Ollama Cloud is in extra-usage mode (included quota exhausted for current window):

1. **Raise ollama_cloud's effective price** from $0.024/M to the estimated per-token rate
   - Start with $0.10/M as a conservative estimate (between PPQ $0.14 and z.ai $0.001)
   - This makes the optimizer prefer z.ai (when available) and PPQ/OpenRouter over Ollama
   - Kalman filter will converge to the true rate over time as we observe extra-usage billing

2. **Apply scarcity multiplier more aggressively**
   - The pricing engine already has a scarcity factor (linear ramp from 50% to 100% quota usage → 1.0x to 2.0x)
   - When in extra usage, the scarcity factor should be > 2.0x (or use a separate "extra usage" multiplier)
   - This naturally makes ollama_cloud more expensive than alternatives

3. **Preserve Ollama for exclusive models**
   - kimi-k3:cloud, kimi-k2.7-code, gpt-oss:120b, gemma4:31b — these are only available on Ollama Cloud
   - The routing engine's model_map should ensure these always route to ollama_cloud regardless of price
   - Only GLM-5.2 and other widely-available models should be rerouted when in extra-usage mode

### Goal 4: Use GLM-5.2 sparingly on extra credits

When Ollama Cloud is in extra-usage mode AND z.ai keys are exhausted (peak hours):

1. **Prefer cheaper models:** Route to `glm-4.5-flash` on Ollama Cloud (if available and cheaper) instead of `glm-5.2`
2. **Prefer cheaper providers:** Route GLM-5.2 to PPQ ($0.14/M) or OpenRouter ($0.135/M) instead of Ollama extra usage
3. **Consider model downgrade:** For low-priority tasks (kanban workers, simple lookups), use `glm-4.5-flash` or `deepseek-v4-flash` on PPQ/OpenRouter instead of `glm-5.2` on Ollama extra credits
4. **Log the regime:** Record whether each call was in included or extra-usage mode for cost tracking

---

## Key Files To Modify

### In the merchant routing engine (`~/merchant-routing-engine/`):

1. **`config/providers.yaml`** — Add `included_quota_tokens` and `extra_usage_rate_per_m` fields for ollama_cloud. Add session/weekly window parameters.

2. **`src/live_router.py`** — Update `_QUOTA_TOTALS["ollama_cloud"]` from `1_000_000` to the estimated real limit. Add logic to detect extra-usage regime from cumulative usage data.

3. **`src/pricing_engine.py`** — Add an "extra usage" multiplier that applies when a provider transitions from included quota to paid per-token billing. This is a new deterministic multiplier alongside peak/scarcity/health.

4. **`src/consumption_kalman.py`** — Ensure the consumption Kalman tracks Ollama Cloud burn rate per 5h session and per 7d weekly window. May need separate Kalman instances per window.

5. **`src/routing_optimizer.py`** — When ollama_cloud is in extra-usage mode, either:
   - Raise its effective price (via the multiplier)
   - Or filter it out for non-exclusive models (kimi-k3:cloud etc. still allowed)

### In the proxy (`~/.hermes/bot/zai_proxy.py`):

6. **`_snapshot_quota()`** (line ~660) — Replace the hardcoded `snap["ollama_cloud"] = {"used_pct": 0.0, "remaining": 1_000_000, "total": 1_000_000}` with a real calculation from `zai_usage.db`.

7. **`_try_ollama_cloud()`** (line ~1957) — After a successful 200 response, check if we're likely in extra-usage mode and log it. Consider adding a response header like `X-Ollama-Quota-Regime: included|extra` for downstream consumers.

8. **`_COST_PER_M_TOKENS`** (line ~1396) — Make ollama_cloud's cost dynamic based on regime. When included: $0.001/M (marginal cost). When extra: $0.10/M (estimated).

### New utility (recommended):

9. **`src/ollama_quota_tracker.py`** — New module that:
   - Queries `zai_usage.db` for cumulative Ollama Cloud tokens in current 5h session window
   - Queries for cumulative tokens in current 7d weekly window
   - Compares against configured limits
   - Returns `{"regime": "included"|"extra"|"exhausted", "session_used_pct": ..., "weekly_used_pct": ...}`
   - Called by both the proxy and the routing engine

---

## Implementation Order

1. **`src/ollama_quota_tracker.py`** — Build the usage tracker first. Test against historical data in `zai_usage.db`.
2. **`config/providers.yaml`** — Add quota limits and extra-usage rate config.
3. **`src/pricing_engine.py`** — Add extra-usage multiplier.
4. **`src/live_router.py`** — Wire the tracker into the routing optimizer.
5. **`~/.hermes/bot/zai_proxy.py`** — Update `_snapshot_quota()` to use real data.
6. **Test in shadow mode** — Run the routing optimizer in parallel (shadow_hook already supports this) and compare decisions with/without extra-usage pricing.
7. **Go live** — Enable live routing with the new pricing.

---

## Constraints

- **NEVER break production routing.** All changes to `zai_proxy.py` must have a revert plan. Shadow mode testing first.
- **z.ai flat rate is always primary** when available. Ollama Cloud is secondary. External providers are last resort.
- **kimi-k3:cloud and other Ollama-exclusive models must always route to ollama_cloud** regardless of pricing regime.
- **The proxy is the source of truth** until the routing engine goes live. All changes must be backward-compatible.
- **Kimi K3 usage is small** (~123K tokens/week) — the cost concern is GLM-5.2 (~360M tokens/week on Ollama).

---

## Data Sources

- **Usage DB:** `~/.hermes/bot/zai_usage.db` — table `api_calls` with columns: `ts, key_name, model, total_tokens, prompt_tokens, completion_tokens, status_code, duration_ms`
- **Key decisions:** `zai_usage.db` — table `key_decisions` with `ts, chosen_key, reason`
- **Proxy code:** `~/.hermes/bot/zai_proxy.py` — all routing logic
- **Routing engine:** `~/merchant-routing-engine/src/` — optimizer, pricing, Kalman filters
- **Provider config:** `~/merchant-routing-engine/config/providers.yaml`
- **Ollama pricing:** https://ollama.com/pricing (Max plan, $100/mo, 250x Free usage)
- **Environment:** `~/.hermes/profiles/manager/.env` — `OLLAMA_CLOUD_API_KEY`

---

## Questions For The Routing Engine Maintainer

1. **Extra-usage rate:** Ollama doesn't publish per-model token rates for extra usage. Should we probe this by adding $5 to the extra balance and observing the burn rate? Or use a conservative estimate ($0.10/M)?

2. **Quota limit calibration:** Should we run the system for 2-3 weeks with logging to observe when 429s happen (if ever) and calibrate the session/weekly limits? Or start with the theoretical estimate (500M/5h, 3.5B/week)?

3. **Model exclusivity:** Should kimi-k2.7-code and kimi-k3:cloud always route to ollama_cloud (they're exclusive), or should we check if PPQ offers kimi-k3 as well (their model_map lists `kimi-k3` for PPQ)?

4. **Shadow mode validation:** Should we run the new pricing in shadow mode for 48h before going live, comparing decisions? The shadow_hook infrastructure already exists for this.