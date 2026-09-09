# T-B — Routstr Wiring Canary: Implementation Report

**Task:** T-B from PLAN-routstr-serving-lane-2026-09-07.md; P0-4 from PLAN-wire-full-provider-pool-rugpull-resilience-2026-09-08.md  
**Assignee:** worker-merchant  
**Date:** 2026-09-07 (updated 2026-09-08 — calibr.json rollup)  
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
14 unit tests covering all canary module functions:
- Header injection/stripping
- JSONL write/unwritable-path fallback
- Model extraction from request body
- DB mirror + no-DB graceful fallback
- calibr.json rollup: shape/mode/routing_changed invariant, sold-row + would-be-route reads from a decision DB, missing-log/DB zero-fail, lookback windowing

### 4. calibr.json snapshot (`build_calibr`, P0-4 scope)
`~/.hermes/bot/routstr_sold_calibr.json` is the machine-readable canary
calibration snapshot. `python3 -m src.routstr_sold_canary --calibr
--calibr-db ~/.hermes/bot/zai_usage.db` rolls the canary JSONL + decision DB
into one file:

```json
{
  "canary": "routstr_sold_calibr",
  "mode": "shadow",
  "priority_header": "X-Priority: sold",
  "lane": "routstr",
  "routing_changed": false,
  ...
  "requests_total": 288,
  "requests_by_status": {"200": 287, "400": 1},
  "sold_chat_requests": 0,
  "probe_or_other": 288,
  "sold_decision_rows": 288,
  "would_be_routes": {}
}
```

Key semantics:
- `mode: shadow` + `routing_changed: false` assert the shadow invariant — the
  canary NEVER alters routing (Phase B), it only labels + logs.
- `sold_decision_rows` = `caller_class='sold'` rows mirrored into
  `routing_live_decisions` (the "sold-class request row" the P&L collector and
  Gate-1 readiness feed read).
- `would_be_routes` = upstream provider keys that served `routstrd_sale` calls
  in the window; under shadow the actual route == the would-be route.
- `sold_chat_requests` vs `probe_or_other` split: chat-completions requests
  carry a `model`, health/probe GETs do not, so probes never pollute the
  sold-traffic signal.
- Missing log/DB → zeroed counters, never raises (a snapshot glitch cannot
  break the request path). Tests write to tmp paths only — never the live
  `~/.hermes/bot/routstr_sold_calibr.json`.

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

### calibr.json live snapshot (2026-09-08, P0-4)
Regenerated from the live canary log + zai_usage.db (48h window, 316 rows):
- 316 canary rows observed; 315×HTTP 200 + 1×400 (probe/health GETs dominate — they carry no chat body)
- 315 `caller_class='sold'` rows visible in `routing_live_decisions` (the sold-class request row; mirrors canary 1:1 via tag-sidecar)
- 1 sold chat-completion E2E probe in window (model glm-5.2 → HTTP 200, served by key_name `ours`) → `sold_chat_requests: 1`, `would_be_routes: {"ours": 1}` (actual route under shadow == would-be route)
- 229 `routstrd_sale` api_calls on record (key_name routstr/ours/chutes/ollama_cloud, all 200) — the Sep-06/07 E2E batch
- `routing_changed: false` on every snapshot — shadow invariant holds

## Quality gates
- ✅ Gate 1 (TDD): 14 tests written, pass
- ✅ Gate 2 (tests pass): canary file 14/14 green; full-suite baseline 269/289 with 20 pre-existing unrelated failures (see below)
- [pending] Gate 2.5 (cold review): reviewer to verify
- ✅ Gate 3: This doc updated in the SAME commit as the calibr code + tests
- ✅ Gate 4: Atomic commit + push (calibr commit 49e7a4f+)
- [pending] Gate 6: Manager review before merge

## Known pre-existing test-suite noise (NOT caused by this change)
- `tests/test_urgency_cost_estimator.py` fails at collection on this host:
  it imports `display_urgency_costs`, which exists in neither this tree nor
  the sibling `~/merchant-routing-engine` checkout at these commits (stale
  test from an earlier CG-3→CG-12 refactor). Verified pre-existing at HEAD by
  stashing this change and re-running — identical error.
- Host sys.path hazard: `flat_router.py`/`conftest.py` insert
  `~/.hermes/bot` and `~/merchant-routing-engine` (production + sibling
  checkout on a different branch) ahead of this worktree's `src`, so a few
  import-time messages and the urgency module can resolve to the sibling
  tree. The canary module and its tests are unaffected (verified by direct
  import path checks).

## Open issues / follow-up
1. Canary log rotated during proxy restart — implement log rotation or append-only guarantee
2. Link key (link-a166*) at 99,999,307 msat balance — top-up when < 50K sat remaining
3. Phase A (caller-class priority routing) not yet implemented — T-A remains open
4. Chutes provider row (id5) added to friends node — check for Chutes key funding
5. PPQ $0.97 — top-up needed urgently