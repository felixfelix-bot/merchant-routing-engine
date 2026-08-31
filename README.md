# Merchant Routing Engine

Kalman filter-based routing and failover engine for LLM API providers. Manages cost optimization, key health tracking, provider funding tracking, and intelligent failover across multiple API providers.

## Reproduce the routing engine

Full flat-market routing system reproducible from this repo: [REPRODUCE.md](REPRODUCE.md)

## Quick Start

```bash
cd ~/merchant-routing-engine
python3 -m pytest tests/ -v
```

## Architecture

All API requests flow through a single proxy that makes routing decisions:

```
Request → z.ai (flat rate, always first)
  quota exhausted? → other z.ai key
  both exhausted? → cheapest funded external provider (PPQ/OpenRouter)
  all fail? → 503 to client
```

See `HANDOVER.md` for full context, `docs/architecture.md` for details.

## Modules

| Module | Purpose |
|--------|---------|
| `key_health_tracker.py` | Track z.ai key quota health (exhausted for 5 min on error) |
| `provider_funding_tracker.py` | Track PPQ/OpenRouter credits (unfunded for 1h on 402) |
| `reasoning_handler.py` | Inject reasoning_content as content when model produces empty output |
| `route_request.py` | Kalman-based cost/quality router |
| `backoff.py` | Binary exponential backoff for rate limits |
| `external_failover.py` | Dynamic cheapest-funded failover |

## Status

Phase 1: Standalone module copies extracted from production `zai_proxy.py`. Not yet imported by the proxy — see `docs/migration-plan.md`.
