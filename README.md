# Merchant Routing Engine

Kalman filter-based routing and failover engine for LLM API providers. Manages cost optimization, key health tracking, provider funding tracking, and intelligent failover across multiple API providers.

## Mirror, CI and releases on Nostr (ngit)

This repository is mirrored to **ngit** — git hosting and CI on Nostr — under the maintainer identity `npub1nng5mx…` (hex `9cd14d9acdbab7ca162b772c91fa514da8e3248f61f924bf3441e6e72c5f0dee`). Clone over Nostr or HTTPS from either grasp:

```bash
git clone nostr://npub1nng5mxkdh2mu593twukfr7j3fk5wxfy0v8ujf0e5g8nwwtzlphhqksqpew/relay.ngit.dev/merchant-routing-engine
git clone https://relay.ngit.dev/npub1nng5mxkdh2mu593twukfr7j3fk5wxfy0v8ujf0e5g8nwwtzlphhqksqpew/merchant-routing-engine.git
git clone https://gitnostr.com/npub1nng5mxkdh2mu593twukfr7j3fk5wxfy0v8ujf0e5g8nwwtzlphhqksqpew/merchant-routing-engine.git
```

- **Browse / open PRs:** https://gitworkshop.dev/npub1nng5mxkdh2mu593twukfr7j3fk5wxfy0v8ujf0e5g8nwwtzlphhqksqpew/relay.ngit.dev/merchant-routing-engine
- **CI (the decision of record):** work that lands in **this public tree** is certified at its exact commit by this repo's Nostr CI lane, `.ngit/act/workflows/python-test.yml` (green kind-9842 results at `2ccbbb91` on `refs/heads/main` and at the lane tip `4e55c8f` on `refs/heads/ci/ngit-workflow-lane`). The ngit mirror is the build of record; GitHub Actions runs nothing here (this repo has no `.github/workflows/`).
- **⚠️ Scope of that certificate:** it covers the files in THIS repo at that commit — **not** the live engine tree, which is ahead of this repo (`~/.hermes/bot/zai_proxy.py`, `flat_router.py` and `garbage_detector.py` are newer/absent here). A change to the live engine must publish its sanitized delta into this repo and re-run the lane before this CI can honestly certify it. Details, per-file line counts and the excluded `tests/` modules: [`.ngit/README.md` § Scope](.ngit/README.md#scope-what-a-green-run-does-and-does-not-certify).
- **Announcement (source of truth for the URLs above):** kind `30617`, `d=merchant-routing-engine`, by `9cd14d9acdbab7ca162b772c91fa514da8e3248f61f924bf3441e6e72c5f0dee` (2026-09-14), relays `wss://relay.ngit.dev wss://gitnostr.com`.

### CI surface (what runs where)

| Surface | Engine | Status |
|---|---|---|
| `.ngit/act/workflows/python-test.yml` | ngit-ci coordinator (Nostr) | **Active lane** — pytest suites + syntax checks (see `.ngit/README.md` for steps, counts, and the measured 24-module ignore list) |
| `.github/workflows/` | GitHub Actions | None exist; deliberately not used |

Fleet plan `hermes-orchestration/docs/PLAN-fleet-governance.md` Phase 12.2 lists this repo as CI-less ("merchant-routing" among the 19 CI-less repos); this lane closes that item for this repo.

### Build artifacts (Nostr, not GitHub releases)

Artifacts are published as NIP-94 **kind `1063`** events, not attached to GitHub releases. Each event carries one `url` tag per Blossom mirror and an `x` tag with the file's **sha256** — download from any mirror and verify the hash.

No kind-`1063` artifact is announced for this repo yet, so here is the exact query:

```bash
nak req -k 1063 -t "A=30617:9cd14d9acdbab7ca162b772c91fa514da8e3248f61f924bf3441e6e72c5f0dee:merchant-routing-engine" wss://relay.ngit.dev   # url + x
```

When one appears, download from any `url` and prove the `x` sha256 before use:

```bash
echo "<x-tag-sha256>  artifact" | sha256sum -c -
```

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
