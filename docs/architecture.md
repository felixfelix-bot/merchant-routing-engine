# Architecture — Merchant Routing Engine

## Request Flow

```
Signal/Matrix message
        ↓
hermes-gateway (port 9098)
        ↓ POST localhost:9099/v1/chat/completions
zai_proxy (port 9099)
        ↓
    ┌──────────────────────────────────────────────────────┐
    │  1. best_key()                                       │
    │     Phase 1: Kalman burn prediction                  │
    │     Phase 2: Reactive lock thresholds                │
    │     Phase 3: Recover previously-locked key           │
    │     Phase 4: Health check (skip exhausted keys)      │
    │     → Returns "ours", "friend", or None              │
    ├──────────────────────────────────────────────────────┤
    │  2. If None → _try_external_failover()               │
    │     → Collect funded providers                       │
    │     → Sort by cost                                   │
    │     → Try cheapest first                             │
    │     → On 402 → mark unfunded → next cheapest         │
    ├──────────────────────────────────────────────────────┤
    │  3. Forward to z.ai                                  │
    │     → 200 + content → send to gateway ✓              │
    │     → 200 + empty content + reasoning → inject ✓     │
    │     → 200 + empty, no reasoning → failover           │
    │     → 200 + error JSON → mark exhausted → next key   │
    │     → 401/403 → mark exhausted → failover            │
    │     → 429 → mark exhausted + backoff → retry         │
    ├──────────────────────────────────────────────────────┤
    │  4. After for loop (all keys tried)                  │
    │     → _try_external_failover() if not already tried  │
    │     → 503 if all fail                                │
    └──────────────────────────────────────────────────────┘
```

## Provider Chain (Priority Order)

| # | Provider | Model | Cost | When Used |
|---|----------|-------|------|-----------|
| 1 | z.ai "ours" | glm-5.2 | $0.07-0.20/1M (flat rate derived) | Always first |
| 2 | z.ai "friend" | glm-5.2 | $0.08-0.24/1M (+21% penalty) | When "ours" exhausted |
| 3 | PPQ | deepseek-v4-flash | $0.28/1M combined | When both z.ai keys dead |
| 4 | OpenRouter | deepseek-v4-flash | $0.27/1M combined | When PPQ unfunded/down |
| 5 | Ollama | qwen2.5-coder:3b | $0 (local) | Last resort (not wired yet) |

## Key Design Principle

**z.ai is always first because it's a flat rate ($155/month).** PPQ and OpenRouter are per-token — every call costs real money. The proxy should exhaust all z.ai options before touching paid providers.

## Component Map

```
~/.hermes/bot/
├── zai_proxy.py          ← HTTP proxy server (port 9099)
│   ├── Key health tracker (lines ~91-146)
│   ├── Provider funding tracker (lines ~86-135)
│   ├── Binary exponential backoff (lines ~700-733)
│   ├── External failover (lines ~750-880)
│   ├── Reasoning injection (lines ~985-1030)
│   └── best_key() with Kalman prediction (lines ~641-742)
├── burn_predictor.py     ← Kalman filter + route_request()
├── model_matrix.py       ← Pricing matrix (646 models)
├── model_tier_router.py  ← Dynamic model downgrade
└── rate_limit_predictor.py ← 429 recovery prediction
```

## Why No Agent-Level Fallback

`config.yaml: fallback_providers: null`

Previously, the gateway had `fallback_providers` that bypassed the proxy on connection errors. This caused:
- Direct OpenRouter calls bypassing cost optimization
- No Kalman prediction on fallback calls
- No model tier downgrade
- $5+/day in unnecessary OpenRouter spending from brief proxy hiccups

Now ALL requests go through the proxy. The proxy handles all failover internally.
