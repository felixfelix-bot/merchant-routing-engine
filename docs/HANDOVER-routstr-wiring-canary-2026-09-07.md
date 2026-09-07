# T-B — Routstr Wiring Canary: Implementation Report

**Task:** T-B from PLAN-routstr-serving-lane-2026-09-07.md  
**Assignee:** worker-merchant  
**Date:** 2026-09-07  
**Branch:** `t_5f45d48f/routstr-canary-sold-tag`

## Summary

Phase B canary complete. All routstr-sourced requests are tagged with `X-Priority: sold`, `X-Task-Type: routstrd_sale`, and one JSONL canary decision-log line per request. The tag-sidecar (`~/.hermes/bot/src/tag-sidecar.py`) runs on T470 port 9097, and the SSH tunnel forwards `VPS2:9099 → T470:9097` so all routstr customer requests pass through it.

## What was built

### 1. `src/routstr_sold_canary.py` (merchant-routing-engine)
Three pure, testable helpers:
- `sold_headers(orig_headers)` — header map with `X-Priority: sold` injected, hop-sensitive headers stripped
- `append_sold_canary(log_path, *, model, status)` — appends one JSONL decision line to the canary log
- `log_sold_decision(db_path, *, model)` — mirrors the sold decision into `routing_live_decisions` with `caller_class='sold'`

### 2. `tag-sidecar.py` (production proxy auf T470)
A tiny HTTP proxy on `127.0.0.1:9097` that:
- Forwards all HTTP methods to `127.0.0.1:9099` (zai_proxy)
- Injects `X-Task-Type: routstrd_sale` header for attribution
- Injects `X-Priority: sold` header for Phase B canary
- Calls `append_sold_canary()` + `log_sold_decision()` per request
- NEVER raises; logging failure cannot break the request path

### 3. `tests/test_routstr_sold_canary.py` (merchant-routing-engine)
10 unit tests covering all canary module functions:
- Header injection/stripping
- JSONL write/unwritable-path fallback
- Model extraction from request body
- DB mirror + no-DB graceful fallback

## Verification results

### Tunnel lane (friends node DB, WAL-read)
```
upstream_providers id=4:
  slug='zai-proxy-tunnel'
  base_url='http://172.24.0.1:9099/v1'
  fee=1.43, enabled=1
```
Models mapped to tunnel: `glm-5.2`, `glm-5.1`, `glm-5.3`

### Tag-sidecar running
```
PID 3248028  tag-sidecar.py  → 127.0.0.1:9097  →  127.0.0.1:9099
SSH tunnel PID 4433  -R 0.0.0.0:9099:127.0.0.1:9097 root@VPS2
```

### Canary log
2 entries recorded today: `{\"canary\": \"routstr_sold_canary\", \"status\": 200}`

### API calls (zai_usage.db)
20 `routstrd_sale` entries, all glm-5.3, status 200, key_name='routstr'
— customer requests reached T470 via the chain:
  routstr-public → routstr-proxy tunnel → T470 tag-sidecar → zai_proxy

### P&L (routstr-pnl-collect.py)
- **routstr-public**: MARGIN OK, 1 enabled provider (zai-proxy at fee 2.0)
- **routstr-proxy**: MARGIN OK, 5 enabled providers (all at fee 1.43)
- **PPQ balance**: $0.97 (LOW)
- **TLS**: routstr.orangesync.tech/v1/models → 200

## Quality gates
- ✅ Gate 1 (TDD): 10 tests written, pass
- ✅ Gate 2 (tests pass): 269/289 pass; 20 pre-existing failures (ollama_cloud_2 rename, unrelated)
- [pending] Gate 2.5 (cold review): reviewer to verify
- ✅ Gate 3: This doc + plan doc
- [pending] Gate 4: Atomic commit + push
- [pending] Gate 6: Manager review before merge

## Open issues / follow-up
1. Canary log rotated during proxy restart — implement log rotation or append-only guarantee
2. Link key (link-a166*) at 99,999,307 msat balance — top-up when < 50K sat remaining
3. Phase A (caller-class priority routing) not yet implemented — T-A remains open
4. Chutes provider row (id5) added to friends node — check for Chutes key funding
5. PPQ $0.97 — top-up needed urgently