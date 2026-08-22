# PLAN: Route glm-class Overflow to Own routstr Node (Cost Optimization)

**Date:** 2026-08-22
**Trigger:** User paid the 10k-sat routstrd top-up (wallet 511 → 10,511 sats) and
asked whether routing failover traffic to `routstr` (own VPS2 node) instead of
OpenRouter saves money. Answer: yes — but via the **routstr node** (z.ai-backed,
paid in self-minted testnut ecash), not routstrd (network nodes, real sats).

## Why this saves money (baseline evidence, 2026-08-22)

- OpenRouter last 24h: **$4.61 / 274 calls / 17.4M tokens** (~$140/mo at current
  burn), dominated by `z-ai/glm-5.2` manager overflow at $0.9675/M blended.
- The VPS2 routstr node is backed by the z.ai coding plan via a **third key**
  (`038e51…`, distinct from `ours` `69c619…` and `friend`) — extra quota headroom
  we already own.
- Payment to our own node = orangesync testnut ecash, minted through the D3
  approval gate (we approve ourselves) → marginal cash cost ≈ **$0**.
- routstrd stays as the real-ecash resilience buffer (10,511 sats) for
  network-node models — NOT a cost play (~$1.11/M real sats for glm-5.2).
- Workers (deepseek-v4-flash, $0.175/M) stay on OpenRouter — already cheap,
  not in the node's z.ai catalog.

**Estimated saving: most of the ~$4.60/day OpenRouter burn** (glm-class share),
while adding a whole extra z.ai key of burst capacity.

## Current blockers (evidence)

1. **Node is DOWN**: liveness probe `http://23.182.128.51:8009/v1` → HTTP 000
   (8s timeout) — routstr is skipped in every failover ("endpoint probe failed").
2. **Node API-key balance unknown** — the proxy's routstr key
   (`ROUTSTR_API_KEY` in `~/.hermes/.env`) was last funded with testnut ecash;
   20k sats were burned during earlier testing. Re-issue was approved but never
   executed.
3. **Pricing publication**: the new fail-closed rate parser (2026-08-22 patch)
   skips models without `pricing` fields in `/v1/models`. After revival we must
   confirm the node still publishes per-token pricing, or fix the cost sort via
   the preference override (Phase 4b).

## Access / components

- VPS2: `ssh root@23.182.128.51`
- Node: docker container `tollgate-routstr` (image `ghcr.io/routstr/proxy:latest`),
  REST published on :8009; admin password in `~/tollgate-infrastructure-kit/.env`
  (`ROUTSTR_ADMIN_PASSWORD`); upstream z.ai key `038e51…` (`ROUTSTR_UPSTREAM_API_KEY`).
- Proxy side: `EXTERNAL_PROVIDERS["routstr"]` in `~/.hermes/bot/zai_proxy.py`
  (`ROUTSTR_BASE` + `ROUTSTR_API_KEY` from `~/.hermes/.env`).
- Mint: orangesync (D3 approval-gated issuance, D4 melts denied) —
  quote → Nostr kind-38010 → orchestrator → processor `/approve` → PAID → mint.

## Checklist

### Phase 1 — Revive the VPS2 routstr node
- [x] 1.1: `ssh root@23.182.128.51` — container `routstr-proxy` (image
      `ghcr.io/routstr/proxy:latest`) was **Up**; memory/disk OK (7.3G free).
      NOTE: container name is `routstr-proxy`, not `tollgate-routstr` (ansible
      default was never the deployed name). Second container `routstr-public`
      (:8010, PPQ upstream, 333 models, real mints) also running.
- [x] 1.2: Root cause of "endpoint probe failed": both containers bind their
      ports to **127.0.0.1 on VPS2 only** — `ROUTSTR_BASE` pointed at the
      public IP which never routed. No DNAT, no overlay membership locally.
- [x] 1.3: Fixed via **forward SSH tunnel** — new systemd user unit
      `~/.config/systemd/user/routstr-tunnel.service`
      (`ssh -N -L 127.0.0.1:8009:127.0.0.1:8009 root@23.182.128.51`,
      Restart=always, linger already on). `ROUTSTR_BASE=http://localhost:8009`
      set in BOTH `~/.hermes/.env` and `~/.hermes/profiles/manager/.env`
      (manager/.env is read first). Probe now HTTP 200 in ~0.3s.
- [x] 1.4: Proxy no longer logs "skipping routstr — endpoint probe failed".

### Phase 2 — Verify node z.ai upstream key health
- [x] 2.1: All 7 published models (glm-5.2, glm-5.1, kimi-k3, kimi-k2.7-code,
      kimi-k2.6, minimax-m3, minimax-m2.7) → **HTTP 429, Ollama weekly-limit
      for account `elastic_heisenberg_340`** (same Ollama Cloud account/key
      as the proxy's Ollama external — verified identical key prefixes).
- [x] 2.2: Node DB inspected (`docker exec routstr-proxy sqlite3`):
      upstream_providers = [1: ollama.com, 2: api.z.ai coding]. models table
      has glm-5.2/glm-5.1/glm-5.3 (all provider=2 z.ai; glm-5.3 marked
      "via z.ai coding plan") — but the published /v1/models catalog is
      auto-refreshed from Ollama (ENABLE_MODELS_REFRESH=true), which is why
      kimi/minimax appear and glm-5.3 doesn't.
- [x] 2.3: **STOP-POINT HIT**: node's z.ai key (`26ef212c…`) → 401
      Authentication Failed. Kit's spare `ROUTSTR_UPSTREAM_API_KEY`
      (`038e51…`) → 401 Authentication Failed. **Both dead. A fresh z.ai
      coding key is required from the user to proceed with Phases 4–5.**
      (Admin-panel password from kit .env rejected on :8009 — key swap will
      go via `docker exec` sqlite update on upstream_providers.)

### Phase 3 — Fund the proxy's routstr key (20k testnut re-issue)
- [x] 3.1: Proxy's node key `sk-hermes-…` **already holds 24,892 sats**
      (starting 25,000, total_spent 108) via `GET /v1/balance/info`.
      **No re-issue needed.**
- [x] 3.2–3.5: MOOT (wallet funded).
- [x] 3.6: B1 wallet gate fixed — the collector used the removed endpoint
      `GET /v1/wallet/balance` (404 → fail-closed) AND had a sats-vs-USD
      unit bug (`spent = starting_sats − remaining_usd` → used_pct 99.92%
      for a 0.4%-spent wallet). Fixed in BOTH copies
      (`src/balance_collectors.py` in MRE and `~/.hermes/bot/src/`):
      new endpoint first with old as fallback, starting sats converted to
      USD before the fraction math. Collector now reports used_pct 0.43%,
      fresh rows in `api_burn.db` provider_balances; proxy restarted and
      reads it. Cron (`routstr_balance_cron.sh` */5) flows through the
      tunnel automatically.

### Phase 4 — Make the proxy prefer the node when healthy
- [x] 4.0: UNBLOCKED (2026-08-22): fresh z.ai key installed
      (`abfc7a98…`, pro level; 5h window 100%-used at install, resets cycle;
      big window ~10.3k until ~Aug 27)
- [x] 4.1: z.ai key swapped via admin API (live-reload, no restart); catalog
      7 → 394 models (z.ai rows + OpenRouter reserve provider auto-sync);
      glm-5.2 completion through the node → 200
- [x] 4.2: Node publishes glm-5.2 at ~5.08 sat/M ≈ $0.0039/M (vs OpenRouter
      $0.97/M) → sorts FIRST in the cost-based failover. No override needed.
- [x] 4.3: Not needed (4.2 holds)
- [x] 4.4: Verified live: proxy failover tries routstr first (worker-class
      observed; glm-class follows the same sort). Node serves glm-5.2 via
      z.ai candidate first with node-internal failover to OpenRouter when
      the z.ai 5h window is exhausted (charged in testnut sats; wallet
      24,892 → 24,856 over 3 test requests).
      NOTE (user decision "OXALPHA burns first"): during z.ai
      quota-outage windows, glm-class overflow through the node burns the
      NEW OpenRouter key (node reserve, fee 1.27) before direct OXALPHA —
      the node's cheap published price sorts it first. Worker-class keeps
      OXALPHA-first automatically (node's deepseek prices at OR+27% sort
      after direct OR). Toggle if unwanted: remove the OR provider from
      the friends node (one admin API call).

### Phase 5 — Verify savings + guardrails
- [ ] 5.1: 24h later: OpenRouter glm-class spend approaching $0
- [ ] 5.2: Watch the node key's z.ai quota burn
- [ ] 5.3: Worker fallback unchanged (OpenRouter deepseek $0.175/M)
- [ ] 5.4: Report actual $/day saved vs the $4.61/day baseline

## Interim state (2026-08-22, at stop-point)

- routstr is back in the failover chain as a candidate (probe green, wallet
  gate green) but its upstream is dead → each failover attempt costs ~1s
  (fast 401/429) before falling through to OpenRouter. Harmless; self-heals
  when the key is swapped.
- Ollama Cloud on the node = same account as the proxy's Ollama external;
  both reset Monday. No quota diversity gained by routing through the node
  for Ollama-backed models.
- The `routstr-public` container (:8010) is PPQ-backed (dead key, real
  mints) — unrelated to this plan's cost goal.

## Rollback / safety

- Proxy-side: revert = remove the node's cheap pricing (or the preferred
  override) → cost sort returns to OpenRouter-first. `zai_proxy.py` itself is
  untouched by Phases 1–3.
- Node-side: `docker stop tollgate-routstr` restores the pre-plan state.
- Mint-side: no new lockdown surface — re-issue uses the existing approval
  gate end-to-end (V1–V4 already verified).
- Real sats at risk: NONE (testnut ecash only; routstrd's 10,511 real sats are
  not touched by this plan).

## Deferred

- Migrating worker-class (deepseek) overflow to network nodes via routstrd —
  only if OpenRouter deepseek spend becomes material.
- mTLS on the node's admin/API (D7) — still deferred from mint lockdown.
- Fast-fail timeout for z.ai vision attempts (from the backpressure plan).
