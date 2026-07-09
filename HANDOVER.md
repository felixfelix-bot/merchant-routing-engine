# HANDOVER — Merchant Routing Engine

**Date**: 2026-07-09
**Context**: This repo formalizes the routing/failover logic currently embedded in `~/.hermes/bot/zai_proxy.py`. The logic was built and debugged during an intensive session fixing Hermes (Felix) agent stability issues.

## Why This Repo Exists

Felix kept crashing, burning paid API credits, and returning empty responses. Each fix revealed a deeper architectural issue. The routing logic that emerged — key health tracking, provider funding tracking, reasoning injection, dynamic cheapest-funded failover, binary exponential backoff — needs to be formalized, tested, and made provider-agnostic. This repo is where that happens.

## TL;DR

The proxy (`zai_proxy.py`) is the sole gatekeeper for all LLM API calls. It:
1. Tries z.ai first (flat rate, already paid for)
2. On quota exhaustion → tries the other z.ai key
3. On both exhausted → fails over to cheapest funded external provider (PPQ or OpenRouter)
4. Uses binary exponential backoff on rate limits
5. Injects reasoning content when the model produces empty output
6. Tracks provider funding (402 = unfunded for 1 hour)

## Issues Encountered and Fixed

### Bug 1: Proxy crashes on 429 (2,345 crashes/day)

**Root cause**: `_attempt_retry()` called with 4 arguments instead of 5. The function signature requires `key_order` but both call sites omitted it. Every z.ai 429 triggered a `TypeError` that crashed the request thread, dropped the connection, and caused the gateway to fall back to OpenRouter (burning credits).

**Fix**: 
- Pass `order` as 5th argument at both call sites
- Use `key_order` parameter inside the function body (was referencing out-of-scope `order`)
- Add binary exponential backoff between key switches (was zero — hammered both keys instantly)

**Code**: `zai_proxy.py` lines ~735, ~1040, ~1050

### Bug 2: Agent bypassed proxy → OpenRouter credit burn

**Root cause**: `fallback_providers` in `config.yaml` listed OpenRouter first. When the proxy had a brief connection hiccup, the gateway skipped the proxy entirely and called OpenRouter directly — bypassing all cost optimization, Kalman prediction, and model tier routing. 1,152 connection errors in one hour = 1,152 direct OpenRouter calls.

**Fix**: Set `fallback_providers: null` in config.yaml. All requests go through the proxy only. The proxy handles failover internally.

**Code**: `config.yaml` line 6

### Bug 3: False key exhaustion on empty content

**Root cause**: When z.ai returned `"content":""` (reasoning model used all tokens on thinking), the proxy marked the key as "exhausted" for 5 minutes. But the key wasn't exhausted — it still had quota. The model just didn't produce output for that specific request. This caused the proxy to skip a perfectly good key and waste money on PPQ/OpenRouter.

**Fix**: Only mark keys as exhausted for actual error responses (quota/auth errors). Empty content triggers failover for the current request only, but the key stays available for future requests.

**Code**: `zai_proxy.py` ~line 1015

### Bug 4: Reasoning model returning empty content

**Root cause**: z.ai's glm-5.2 is a reasoning model. It returns responses in two fields: `content` (actual response) and `reasoning_content` (internal thinking). When max_tokens is high (65536), the model spends most tokens on reasoning and leaves `content` empty. The gateway sees empty content → retries 3x → gives up with "No fallback providers configured."

**Fix**: If `content` is empty but `reasoning_content` has data, inject reasoning into the content field. The reasoning IS a valid response — the tokens aren't wasted. No external failover needed.

**Code**: `zai_proxy.py` ~line 1010

### Bug 5: No provider funding tracker (OpenRouter 402)

**Root cause**: When OpenRouter's daily credit limit was hit (402), the proxy didn't track this. It kept trying OpenRouter on every failover, getting 402 every time. No mechanism to skip unfunded providers.

**Fix**: Added provider funding tracker — same pattern as key health, but for external providers. On 402, mark provider unfunded for 1 hour. Failover only tries funded providers, sorted by cost. Dynamic selection — no hardcoded order.

**Code**: `zai_proxy.py` ~line 86-135, ~line 750-810

## Current Architecture

```
Gateway (hermes-gateway)
    ↓ all API requests
Proxy (zai_proxy.py, localhost:9099)
    ↓
    1. best_key() — Kalman-predicted key selection
       → skip exhausted keys (Phase 4 health check)
       → if both exhausted → return None
    2. Forward to z.ai (UPSTREAM)
       → 200 + content → send to gateway ✓
       → 200 + empty content + reasoning → inject reasoning → send ✓
       → 200 + empty + no reasoning → try external failover (this request only)
       → 200 + error JSON → mark key exhausted → try other key
       → 401/403 → try external failover
       → 429 → mark exhausted + binary exponential backoff → retry
    3. If best_key() returns None → _try_external_failover()
       → collect funded providers with cost
       → sort cheapest first
       → try each until one works
       → on 402 → mark unfunded → try next
    4. If all fail → 503 to gateway
```

## Provider Priority

1. **z.ai "ours"** — flat rate ($155/mo), always first
2. **z.ai "friend"** — flat rate (shared), second
3. **PPQ deepseek-v4-flash** — $0.09/1M, cheapest paid fallback
4. **OpenRouter deepseek-v4-flash** — $0.09/1M, same price, different provider for redundancy
5. **Ollama local** — free, last resort (not currently wired)

## Key Health Tracker

```python
_zai_key_health = {
    "ours": {"healthy": True, "retry_after": 0},
    "friend": {"healthy": True, "retry_after": 0},
}

# On error response (quota/auth): mark exhausted for 5 min
# On empty content: do NOT mark (key works, model just didn't produce output)
# On 429: mark exhausted + binary exponential backoff
# On success with content: mark healthy
# On success with reasoning injected: mark healthy
# best_key() Phase 4: skip unhealthy keys, return None if both exhausted
```

## Provider Funding Tracker

```python
_provider_health = {
    "ppq": {"funded": True, "retry_after": 0},
    "openrouter": {"funded": True, "retry_after": 0},
}

# On 402: mark unfunded for 1 hour
# On success: mark funded
# _try_external_failover: only try funded providers, sorted by cost
```

## Where Code Lives Today

| Component | Location | Lines |
|-----------|----------|-------|
| Key health tracker | `~/.hermes/bot/zai_proxy.py` | ~91-146 |
| Provider funding tracker | `~/.hermes/bot/zai_proxy.py` | ~86-135 |
| Binary exponential backoff | `~/.hermes/bot/zai_proxy.py` | ~700-733 |
| External failover | `~/.hermes/bot/zai_proxy.py` | ~750-880 |
| Reasoning injection | `~/.hermes/bot/zai_proxy.py` | ~985-1030 |
| Empty/error response detection | `~/.hermes/bot/zai_proxy.py` | ~985-1045 |
| Kalman burn predictor | `~/.hermes/bot/burn_predictor.py` | full file |
| Model cost matrix | `~/.hermes/bot/model_matrix.py` | full file |
| route_request() | `~/.hermes/bot/burn_predictor.py` | ~524-691 |

## Migration Plan (Gradual, with Revert)

### Phase 1 (CURRENT — this repo)
- Standalone module copies in `src/` that mirror the logic
- zai_proxy.py is production, untouched
- Tests validate the standalone modules
- Felix works in this repo, documents, improves

### Phase 2 (FUTURE — import bridge)
- zai_proxy.py imports from merchant_routing_engine package
- `try: from merchant_routing_engine import ... except ImportError: <inline fallback>`
- If import fails → inline code runs (automatic revert)
- Deploy: copy new zai_proxy.py, restart proxy, watch `/health`

### Phase 3 (FUTURE — full migration)
- zai_proxy.py is thin HTTP handler + delegation
- All routing logic in this repo
- CI tests validate before each deploy
- Versioned releases with rollback tags

### Revert procedure
```bash
# If proxy breaks after Phase 2/3 import:
cp ~/hermes-orchestration/scripts/engine/zai_proxy.py ~/.hermes/bot/zai_proxy.py
systemctl --user restart zai-proxy
# Verify: curl http://localhost:9099/health
```

## What This Repo Should Do Next

1. **Formalize the trackers** as clean, tested, provider-agnostic classes
2. **Make route_request() reusable** — not tied to z.ai's model names
3. **Add quality/cost ratio** — currently sorts by cost only; add quality-per-dollar
4. **Dashboard integration** — expose routing decisions, cost tracking, provider health
5. **Config-driven providers** — providers.yaml instead of hardcoded dicts
6. **Feedback loop** — track actual model quality per provider over time

## Related Files

- `docs/merchant-module-master-plan.md` — original master plan from Signal session
- `docs/merchant-module-complete-master-plan.md` — comprehensive plan
- `docs/merchant-module-session-bootstrap.md` — session bootstrap context
- `~/hermes-orchestration/docs/PLAN-proxy-backoff-fix.md` — backoff fix plan
- `~/hermes-orchestration/docs/PLAN-zai-key-health-tracker.md` — key health plan
- `~/.hermes/bot/zai_proxy.py` — LIVE production code (reference only)
- `~/.hermes/bot/burn_predictor.py` — Kalman predictor + route_request
- `~/.hermes/bot/model_matrix.py` — pricing matrix with OpenRouter scraper

## Kanban Board

Board: `merchant-module` (already exists)
```bash
hermes kanban --board merchant-module list
```

## Signal Group

Group: `merchant-module` (group ID: `cRHuPBVVZA9WcyRZn7xA6vJjbMEiDMZ+kVpiGT0g+6Y=`)
Working directory: `~/merchant-routing-engine/`

When starting a session in this group:
1. `cd ~/merchant-routing-engine`
2. Read `HANDOVER.md` (this file)
3. Check kanban board for current tasks
4. Read context snapshot if available
5. Run tests: `python3 -m pytest tests/ -v`
