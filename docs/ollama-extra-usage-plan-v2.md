# Plan v2: Ollama Cloud Extra-Usage Cost Awareness

**Revised:** 2026-08-05 (after consultant review)
**Status:** Approved for implementation

## Consultant-Verified Findings

All file paths, line numbers, and pricing values confirmed against actual codebase:
- `_MODEL_COST_PER_1M` (not `_COST_PER_M_TOKENS`) at zai_proxy.py:1394 — ollama_cloud = $0.024/M ✅
- `_snapshot_quota()` hardcoded at zai_proxy.py:663 ✅
- `_try_ollama_cloud()` at zai_proxy.py:1957 ✅
- `_DEFAULT_CONVERGED_RATES` ollama_cloud = $0.023952 at live_router.py:61 ✅
- `_QUOTA_TOTALS` ollama_cloud = 1,000,000 at live_router.py:71 ✅
- `pricing_engine.py` multipliers: peak(3x), scarcity(50%→100%), health, pace ✅
- `providers.yaml` ollama_cloud: $100/mo, no per-token rate ✅
- `_OLLAMA_ONLY_MODELS` at zai_proxy.py:2200: kimi-k2.7-code, kimi-k3:cloud, gpt-oss:120b, gemma4:31b, qwen3.5:397b ✅
- Zero 429s from ollama_cloud in DB — never hit the limit yet ✅
- 5,510 glm-5.2 calls / 374M tokens on ollama_cloud in 7 days ✅

## Consultant Corrections Applied

1. **BLOCKING → FIXED**: `providers.yaml` model_map lists llama3.3 models, not glm-5.2. live_router.py:357-359 hardcodes glm-5.2. Must reconcile before implementation.
2. **MEDIUM → FIXED**: Extra-usage rate must be $0.15/M (not $0.10/M) — must be ABOVE PPQ ($0.14/M) and OpenRouter ($0.135/M) for optimizer to reroute.
3. **MEDIUM → FIXED**: `providers.yaml` says `rate_limit_resets_every_hours: 24` — actual is 5h session + 7d weekly. Must fix config.
4. **MEDIUM → FIXED**: Variable name is `_MODEL_COST_PER_1M` not `_COST_PER_M_TOKENS`.

## Answers to 4 Open Questions

1. **Extra-usage rate**: Use $0.15/M conservative estimate. Do NOT probe yet — probing burns real money. Kalman converges later.
2. **Quota calibration**: Start with theoretical estimate (500M/5h, 3.5B/week). Build auto-calibration: log cumulative usage if 429 ever occurs. Configurable in providers.yaml.
3. **Model exclusivity**: Keep kimi-k3:cloud + kimi-k2.7-code as Ollama-exclusive. PPQ does NOT serve kimi models (verified from DB). Maintain existing `_OLLAMA_ONLY_MODELS` set.
4. **Shadow mode**: Yes, 48h shadow before go-live. Log quota regime in shadow decisions. Go live if agreement > 90% and no regressions.

## Revised Implementation Order

### Step 1: Fix providers.yaml config
- Update ollama_cloud `rate_limit_resets_every_hours: 24` → `quota_windows: [5h, weekly]`
- Add `included_quota_tokens_session: 500000000` (500M)
- Add `included_quota_tokens_weekly: 3500000000` (3.5B)
- Add `extra_usage_rate_per_m: 0.15`
- Reconcile model_map: add glm-5.2 entries to match what live_router.py hardcodes

### Step 2: Build ollama_quota_tracker.py
- Query zai_usage.db for cumulative ollama_cloud tokens in current 5h session
- Query for cumulative tokens in current 7d weekly window
- Compare against configured limits
- Return: `{"regime": "included"|"extra"|"exhausted", "session_used_pct": float, "weekly_used_pct": float}`
- Test against historical data in zai_usage.db

### Step 3: Add extra-usage multiplier to pricing_engine.py
- New deterministic multiplier alongside peak/scarcity/health/pace
- When regime == "extra": multiply base rate by EXTRA_USAGE_MULTIPLIER (config, default = 6.25x → $0.024 * 6.25 = $0.15/M)
- When regime == "included": multiplier = 1.0 (no change)
- When regime == "exhausted": multiplier = infinity (filter out provider)
- Ensure effective rate > $0.14/M (PPQ) when in extra mode

### Step 4: Wire tracker into live_router.py
- Update `_QUOTA_TOTALS["ollama_cloud"]` from 1,000,000 to configured limit
- Call ollama_quota_tracker on each routing decision
- Pass regime to pricing_engine for multiplier
- Log regime in key_decisions table

### Step 5: Update proxy _snapshot_quota()
- Replace hardcoded `snap["ollama_cloud"] = {"used_pct": 0.0, "remaining": 1_000_000, "total": 1_000_000}`
- Use real data from ollama_quota_tracker
- Surface regime in snapshot for CVM/dashboard

### Step 6: Shadow mode 48h
- Run routing optimizer in parallel (shadow_hook infrastructure exists)
- Log quota regime in routing_shadow_decisions table
- Compare shadow_provider vs live_provider
- Go live if agreement > 90%, no regressions

### Step 7: Go live + monitor
- Enable live routing with extra-usage pricing
- Monitor for 7 days
- Calibrate limits if 429 observed