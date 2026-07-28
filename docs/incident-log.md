# Incident Log — Proxy Routing Issues (2026-07-09)

## Incident 1: Proxy crashes on 429 (2,345 crashes/day)

**Symptom**: Gateway logs show "Connection error" to localhost:9099. Proxy journal shows `TypeError: _attempt_retry() missing 1 required positional argument: 'key_order'` hundreds of times per day.

**Root cause**: Function `_attempt_retry(e, attempt, name, t0, key_order)` defined with 5 parameters, but called with only 4 at both call sites. The missing `key_order` argument caused `TypeError` on every 429 response.

Additionally, the function body referenced `order` (a local variable from the caller's scope) instead of the `key_order` parameter — a `NameError` would have occurred even if the argument was passed.

**Impact**: 2,345 crashes in one day. Each crash dropped the connection → gateway fell back to OpenRouter directly → burned daily credit limit.

**Fix**:
1. Pass `order` as 5th argument: `_attempt_retry(e, attempt, name, t0, order)`
2. Use `key_order` parameter inside function body
3. Add binary exponential backoff between key switches (was zero)

**Commit**: `496cbd8` in hermes-orchestration

---

## Incident 2: Agent bypassing proxy → OpenRouter credit burn

**Symptom**: OpenRouter daily credit limit ($5) exhausted within hours. Error: "This request requires more credits, or fewer max_tokens."

**Root cause**: `config.yaml fallback_providers` listed OpenRouter first. When proxy had brief connection errors (1,152/hour during Incident 1), gateway bypassed the proxy and called OpenRouter directly. No cost optimization, no Kalman prediction, no model tier routing.

Also: `provider: ppq` wasn't recognized by the agent ("provider not configured") — needed `provider: openai-compatible`.

**Impact**: Entire OpenRouter daily budget consumed. Felix stopped responding when limit hit.

**Fix**: Set `fallback_providers: null`. All requests go through the proxy. Proxy handles failover internally via `_try_external_failover()`.

---

## Incident 3: False key exhaustion on empty content

**Symptom**: Felix returns "Empty response from model — retrying" repeatedly. z.ai "friend" key still has 18-55% quota but isn't being used.

**Root cause**: When z.ai returned `"content":""` (reasoning model used all tokens on thinking), the proxy marked the key as "exhausted" for 5 minutes. But the key wasn't exhausted — it still had quota. The model just didn't produce output for that specific request (reasoning consumed all tokens).

This caused the proxy to skip a perfectly good key and waste money on PPQ/OpenRouter for every subsequent request during the 5-minute cooldown.

**Fix**: Only mark keys as exhausted for actual error responses (quota/auth error JSON with no `choices`). Empty content triggers failover for the current request only — the key stays available for future requests.

---

## Incident 4: Reasoning model returning empty content

**Symptom**: z.ai returns 200 with `"content":""` and `"reasoning_content":"1. Analyze the input..."`. Gateway sees empty content → retries 3x → gives up: "No fallback providers configured."

**Root cause**: glm-5.2 is a reasoning model. With high `max_tokens` (65536, as the gateway requests), the model spends most tokens on internal reasoning and leaves the `content` field empty. The reasoning IS valid output — it's just in the wrong field.

**Fix**: If `content` is empty but `reasoning_content` has data, inject reasoning into the content field:
```python
msg["content"] = reasoning_content
```
The tokens spent on reasoning aren't wasted. No external failover needed.

---

## Incident 5: No provider funding tracker (OpenRouter 402)

**Symptom**: After OpenRouter daily limit hit, proxy kept trying OpenRouter on every failover. Got 402 every time. No mechanism to skip unfunded providers.

**Root cause**: No tracking of provider credit status. The proxy had a hardcoded provider order (OpenRouter first, PPQ second) that didn't account for funding.

**Fix**: Added provider funding tracker:
- On 402: mark provider unfunded for 1 hour
- Failover only tries funded providers
- Dynamic selection: sort by cost, try cheapest funded first
- No hardcoded order — pure cost + funding optimization

---

## Incident 6: LiveRouter returns None on failover (t_2532b185)

**Symptom**: When both z.ai keys are exhausted, `LiveRouter.select_failover()`
returns `(None, None)`. The production proxy silently falls back to the
hardcoded `ollama → ppq → openrouter` chain instead of using Kalman-optimized
provider selection. The Kalman failover path effectively never engages.

**Root cause**: `_do_select_failover` queried the `RoutingOptimizer` at
`difficulty="high"` only. The pay-per-token externals (ppq / openrouter /
deepinfra) are registered as tier `low` (rank 0), which fails the high-tier
gate (required rank 2). So when **both z.ai keys AND `ollama_cloud`** were
unavailable (the realistic 48h-soak scenario where ollama is rate-limited
daily), *no high-tier provider* was viable → `route()` returned
`chosen_provider="fallback"` → `select_failover` mapped that to `None`.

A secondary failure mode: `pace_factor_multi` was called without a
per-provider guard, so one provider's malformed pace-window tuple could raise
and be swallowed into `(None, None)`.

The bug was masked whenever `ollama_cloud` was still up (it is high-tier), so
it only surfaced in the all-high-tier-dead case.

**Impact**: Kalman-optimized failover selection was bypassed during exactly
the outage it was built for. Traffic used the hardcoded chain (worse cost
ordering, no health/quality awareness) instead of the converged-rate routing.

**Fix**:
1. Progressive tier relaxation in `_do_select_failover`: route at
   `high → medium → low`, breaking when a viable provider is found, so the
   low-tier pay-per-token externals are reached when nothing higher is viable.
2. Wrap each `pace_factor_multi` call per-provider so one bad window cannot
   abort the whole failover.

**Invariant restored**: `select_failover` never returns `(None, None)` when a
healthy registered provider exists.

**Regression tests**: `tests/test_live_router.py::TestSelectFailover` —
`test_returns_external_when_all_high_tier_dead` (off-peak + peak variants) and
`test_malformed_pace_window_does_not_break_failover`. All fail pre-fix, pass
post-fix.

**Docs**: `docs/live-router-failover.md`.

---

## Summary

| Incident | Root Cause | Impact | Fix |
|----------|-----------|--------|-----|
| 1 | Missing function argument | 2,345 crashes/day | Pass `order` parameter |
| 2 | Agent bypassing proxy | $5/day OpenRouter waste | Remove fallback_providers |
| 3 | False key exhaustion | Unnecessary PPQ spending | Don't mark empty content as exhausted |
| 4 | Reasoning in wrong field | "No fallback" errors | Inject reasoning as content |
| 5 | No funding tracker | 402 spam on dead provider | Provider funding tracker |
| 6 | Tier gate hid low-tier externals | Kalman failover bypassed | Progressive tier relaxation + pace wrap |
