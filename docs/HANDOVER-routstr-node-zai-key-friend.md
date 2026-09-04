# HANDOVER — Running Your Own Routstr Node with a z.ai Key (Docker + Ansible + Kalman + Flat Market Router)

**Audience:** a friend operating his own VPS, holding a z.ai API key with very high daily
token quota (>1B tok/day), who wants to (a) serve cheap inference through a Routstr node,
(b) model user demand and key burn with Kalman filters, and (c) manage routing with a
price-first "flat market" router — the same stack Felix runs in production.

**How to use this document:** it is written to be fed whole into an LLM context window.
Every section is self-contained, every source reference is a public GitHub URL. Your
agent can read top-to-bottom and then follow links for depth. Nothing here requires
access to Felix's machines.

---

## 0. READ FIRST — the one legal/ToS landmine

**z.ai's Terms of Service (§4) prohibit reselling coding-plan / subscription API access.**
Felix's production node had a `zai-coding` upstream lane and **disabled it permanently**
for exactly this reason — public resale risk. Before you point a PUBLIC routstr node at
your z.ai key:

1. Check which z.ai product your key belongs to (pay-per-token API vs coding plan vs
   enterprise). Pay-per-token API keys with resale-permitted terms are generally fine to
   front; flat-rate/coding-plan keys are not.
2. If your key is resale-restricted, your options are: run the node **privately** (your own
   keys, your own agents, friends-only with no payment), or front a resale-friendly
   upstream instead (e.g. ppq.ai, DeepInfra, OpenRouter — all pay-per-token).
3. Even with a permitted key: keep a **key-health circuit breaker + kill switch** (see §4)
   so a quota surprise never turns into serving paid traffic on an exhausted upstream.

Everything else in this document is ToS-neutral infrastructure.

---

## 1. What the stack looks like (production shape, verified 2026-09)

```
customers (sk- key or cashu token, sats)
        │
        ▼
routstr node  (routstrd — Bun daemon, port 8008; or routstr-proxy python, port 8000)
        │  charges sats, marks up upstream cost (fee multiplier)
        ▼
zai_proxy  (price-first flat router + Kalman, port 9099)   ← optional but recommended
        │  picks cheapest HEALTHY provider per request
        ▼
upstream providers (z.ai, ollama cloud, PPQ, DeepInfra, opencode, ...)
```

Two Kalman feedback loops ride on top:

- **Quota/burn Kalman** — models each key's consumption rate and remaining quota windows;
  drives key-health state and failover.
- **Price/demand Kalman** — models provider prices and demand pressure; publishes effective
  prices over Nostr (kind-30315) so the routstr node can price z.ai models on REAL quota
  state instead of static list prices.

Margin shape: customer price = `upstream_cost × exchange_fee × upstream_provider_fee ×
provider_fee`. Felix runs 30% margin on revenue ⇒ `provider_fee = 1.43` (formula
`fee = 1/(1-margin)`).

---

## 2. The Routstr node itself

Two distinct products share the name — do not conflate:

| | `routstrd` (daemon) | `routstr` (python proxy) |
|---|---|---|
| repo | github.com/Routstr/routstrd (Bun/TS) | ghcr.io/routstr/proxy image |
| port | 8008 | 8000 |
| auth | sk- keys + cashu tokens (SHA-256 hashed keys) | sk- keys + cashu |
| role | full node: wallet, network routing, cashu | lighter AI-inference front |

### 2.1 routstrd (recommended — this is what production runs)

- Upstream source: https://github.com/Routstr/routstrd — build with
  `bun build src/index.ts && bun build src/daemon/index.ts` (see `package.json`).
- **Felix's fork carries production patches** on branch
  [`churn-fixes-2026-08-22`](https://github.com/felixfelix-bot/routstrd/tree/churn-fixes-2026-08-22)
  (tip `f9b7d15`, ahead of upstream `main`):
  - `4f6b67c` host-binding fix — daemon honored config `host` field (was binding `*:8008`)
  - `7fb2264` localhost provider passthrough + staticProviders support (how it fronts zai_proxy)
  - `2480a0d` B7: merchant Kalman pricing integration
  - `f9b7d15` excludeProviderUrls config + SDK churn fixes
  - Patched files: [src/daemon/index.ts](https://github.com/felixfelix-bot/routstrd/blob/churn-fixes-2026-08-22/src/daemon/index.ts),
    [src/utils/config.ts](https://github.com/felixfelix-bot/routstrd/blob/churn-fixes-2026-08-22/src/utils/config.ts)
- **Production Docker image (multi-stage, non-root, healthcheck):**
  [`routstrd-docker/Dockerfile`](https://github.com/felixfelix-bot/tollgate-infrastructure-kit/blob/hermes-v2/routstrd-docker/routstrd-docker/Dockerfile)
  (+ [README](https://github.com/felixfelix-bot/tollgate-infrastructure-kit/blob/hermes-v2/routstrd-docker/routstrd-docker/README.md)
  and [verification script](https://github.com/felixfelix-bot/tollgate-infrastructure-kit/blob/hermes-v2/routstrd-docker/routstrd-docker/test-routstrd-docker.sh))
  on branch `hermes-v2/routstrd-docker` of the infra kit. Note: there is NO official
  public image — `ghcr.io/routstr/*` returns "denied"; build from source.
- Sidecar integration into a hermes-style docker-compose lives on branch
  `hermes-v2/routstrd-sidecar` of the same repo.

### 2.2 routstr (python proxy, Ansible-deployable)

Deployed wholesale by Ansible role `routstr` (see §3) from image `ghcr.io/routstr/proxy:latest`
with a dedicated Cashu mint + Tor. This is the fastest path from zero to a working node.

### 2.3 Auth model (both) — know this before debugging

- Exactly two auth modes: `Authorization: Bearer sk-...` (hashed key lookup) or
  `Bearer cashu...` (ecash token, mint must be in trusted `cashu_mints`).
- **NIP-98 / Nostr auth is NOT supported** by the proxy server — do not waste time on it.
- A cashu token POSTed at balance creation BECOMES the persistent API key.
- `/v1/info` and `/v1/models` are public (no auth) — use for reachability checks.
- Full verified auth research (probe transcripts, endpoint-by-endpoint) and a
  client-consumption guide exist as operator references — ask Felix to share; the
  server-side truth lives in `/app/routstr/auth.py` + `/app/routstr/balance.py`
  inside the routstr repo.

---

## 3. Ansible: VPS + Docker + routstr node, reproducible

All in the public infra kit: **github.com/felixfelix-bot/tollgate-infrastructure-kit**,
branch [`feat/dm-issuer`](https://github.com/felixfelix-bot/tollgate-infrastructure-kit/tree/feat/dm-issuer/ansible)
(everything below is committed + pushed there; secrets are env-var lookups, never in git).

Base URL: `https://github.com/felixfelix-bot/tollgate-infrastructure-kit/blob/feat/dm-issuer/ansible/`

Order of playbooks (a fresh Debian VPS → serving node):

1. `playbooks/00-zram.yml` … `03-cloudflare-dns.yml` — system prep, DNS
2. [`playbooks/02-docker.yml`](https://github.com/felixfelix-bot/tollgate-infrastructure-kit/blob/feat/dm-issuer/ansible/playbooks/02-docker.yml) → role `roles/docker/` — docker-ce + compose plugin + network
3. [`playbooks/04-caddy.yml`](https://github.com/felixfelix-bot/tollgate-infrastructure-kit/blob/feat/dm-issuer/ansible/playbooks/04-caddy.yml) → role `roles/caddy/` — caddy reverse proxy w/ Cloudflare DNS-01 TLS (templates: `Caddyfile.j2`, `docker-compose.yml.j2`)
4. [`playbooks/18-routstr.yml`](https://github.com/felixfelix-bot/tollgate-infrastructure-kit/blob/feat/dm-issuer/ansible/playbooks/18-routstr.yml) → role [`roles/routstr/`](https://github.com/felixfelix-bot/tollgate-infrastructure-kit/tree/feat/dm-issuer/ansible/roles/routstr) — **the "spin up a routstr node on a VPS" playbook**: generates nsec, deploys dedicated Cashu mint + routstr + Tor via [`templates/docker-compose.routstr.yml.j2`](https://github.com/felixfelix-bot/tollgate-infrastructure-kit/blob/feat/dm-issuer/ansible/roles/routstr/templates/docker-compose.routstr.yml.j2), wires admin API + caddy. Env it expects: `ROUTSTR_UPSTREAM_API_KEY`, `ROUTSTR_ADMIN_PASSWORD`, `NSEC` (+ Cloudflare token for DNS).

Operational roles for a routstrd-based node (also in the kit, same branch):

- `playbooks/46-routstrd-funding-guard.yml` — wallet top-up guard (5k-sat floor, LN invoice)
- `playbooks/48-routstr-node-access.yml` — SSH tunnel + balance collector
- `playbooks/49-routstr-node-config.yml` — configure upstreams + payout LNURL on the VPS
- `playbooks/50-cost-escalation-ewma.yml`, `51-cost-probe.yml` — cost telemetry/EWMA probes (`roles/hermes_cost_probe/files/routstr_probe.py`)

Inventory pattern: `inventory/hosts.yml` (vps1/vps2/backup/dq05/t470) with credentials
via `lookup('env', ...)` — copy this pattern, never commit values.

---

## 4. The flat market-based router (zai_proxy + flat_router)

A single-file production router that treats every provider as a lane in a market and picks
the **cheapest healthy** one per request. Public and complete:

- **[zai_proxy.py](https://github.com/felixfelix-bot/hermes-bot/blob/main/zai_proxy.py)** (felixfelix-bot/hermes-bot, main — the live ~7.5k-line production file):
  - flat-router integration (`select_provider`), in-memory key-health circuit breakers with
    exponential backoff (DB is a write-only mirror — restart heals stale breakers),
  - per-provider quota windows, cost accounting, pressure policy,
  - **`GET /kalman-pricing`** endpoint (line ~6826) — effective per-provider prices with all
    Kalman multipliers,
  - **Nostr kind-30315 publisher** (lines ~7304–7515) — publishes the pricing JSON every 30s.
- **[flat_router.py](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/flat_router.py)** + the extracted module set in
  **[src/](https://github.com/felixfelix-bot/merchant-routing-engine/tree/main/src)** — the readable, modular version. Highlights for your use case:
  - `src/key_health_tracker.py` (z.ai quota health), `src/provider_funding_tracker.py` (credit balances)
  - `src/routing_optimizer.py`, `src/live_router.py`, `src/external_failover.py`, `src/backoff.py`
  - `src/pricing_engine.py`, `src/margin_layer.py` (profit-max), `src/cost_gate.py`
- **Reproduction guide:** [REPRODUCE.md](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/REPRODUCE.md)

Wiring it to a routstrd node: routstrd `staticProviders` → point at `http://<host>:9099/v1`
(zai_proxy), exactly like production. **Pitfall (cost us days):** zai_proxy auto-discovers
`~/merchant-routing-engine` via `sys.path.insert` — if the repo is absent it silently
degrades to a dumb key-rotator. Deploy the repo alongside the proxy.

The pricing feedback loop (optional but this is the interesting part): a 2-min cron hook
subscribes to kind-30315 events (`nak req -k 30315 -d kalman-pricing`), picks the freshest
from known publisher npubs, and rewrites the routstr provider_fee. Quota healthy → fee down
(attract volume); scarce → fee up; both keys locked → upstream disabled. Fail-safe rules:
stale >5 min or `price <= 0` → disable, never sell at stale prices.

---

## 5. The Kalman filters (demand + burn modeling)

Three families, all public in [merchant-routing-engine/src](https://github.com/felixfelix-bot/merchant-routing-engine/tree/main/src):

- **Price Kalman** — `src/price_kalman.py` (base-rate estimation; ADRs 001/003/004 in docs/)
- **Consumption/burn Kalman** — `src/consumption_kalman.py` (+ `burn_rate_aggregator.py`): how fast each key burns tokens; feeds key-health + failover
- **Demand Kalman** — `src/demand_kalman.py`, `src/demand_forecast.py`: demand-curve estimation and two-component forecasting — **this is the "model user demand" piece**

Operational cron scripts (public, github.com/felixfelix-bot/hermes-manager-scripts):
`kalman-resource-predict.sh` (flagship predictor), `kalman-collect.sh`,
`kalman-retune.sh`, `adaptive_dispatch_kalman.py`, `kalman_telemetry_publisher.py`.

Known limitation, learned the hard way: the multi-resource Kalman has an identity state
transition (no velocity model) — flat and ramping series both sit inside the static
confidence band until crossing. Distinguish with a slope-from-history discriminator
(committed in `kalman-resource-predict.sh`).

Also know: z.ai quota windows can read as `unknown/resets_at:0/used_pct:0` through some
key types — a blind-quota state. The `price <= 0` and staleness guards exist because a
publisher once advertised availability off empty data.

---

## 6. Economics — margin math that keeps you solvent

- Fee multiplier: `fee = 1/(1-margin)` — 30% margin ⇒ 1.43.
- Total markup = `exchange_fee × upstream_provider_fee × provider_fee` (production: 1.04 × 1.15 × 1.43 = 1.71×).
- **Sats ≠ USD.** You collect sats, pay upstream in USD. Profitability gate:
  `upstream_cost_usd_per_req < sat_revenue_per_req × btc_price_usd`. Check before celebrating sat revenue.
- routstrd prices BTC LIVE (Kraken+Coinbase+Binance, min-of-first-2, ~120s refresh) — no static-rate risk.
- If your z.ai key is flat-rate, your true amortized cost is near $0.001/M tok — static list-price
  resellers charge 70–200× that. That gap is your market: **price on real cost + real quota state
  (Kalman loop, §4) instead of list prices, and you can undercut everyone while holding margin.**
- Units trap: `total_spent`, `reserved_balance`, `accumulated_msats` are **millisats**;
  `cashu_transactions.amount` is **sats**.

---

## 7. Deep-dive documents already public (read next)

1. [HANDOVER-routing-telemetry-friend.md](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/HANDOVER-routing-telemetry-friend.md) — 450-line analyst handover: 12-provider chain, Kalman architecture, ADRs, schema.
2. [HANDOVER-inference-market-pricing-routstr.md](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/HANDOVER-inference-market-pricing-routstr.md) — the three 2026 inference pricing models (hourly GPU / per-token / energy-based), where routstr sits, cache pass-through pricing (the build-worthy idea), and do-NOTs.
3. [routstr-friend-onboarding-handover.md](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/routstr-friend-onboarding-handover.md) — customer-side onboarding (npub → mint → credits → spend).
4. [REPRODUCE.md](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/REPRODUCE.md) — flat-router reproduction.
5. Telemetry dataset schema: [datasets/routing-telemetry/SCHEMA.sql](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/datasets/routing-telemetry/SCHEMA.sql) + README (22 tables, ~770K rows, scrubbed). **CSV extracts are local-only; ask Felix for a snapshot.**

---

## 8. Suggested build order (for your agent)

1. Debian VPS + domain on Cloudflare → run infra-kit playbooks 00–04 (system, docker, caddy).
2. Deploy the routstr role (playbook 18) with your upstream key in env. Verify `/v1/info` + `/v1/models` (public endpoints).
3. Decide the ToS question (§0) before accepting public payment.
4. Add zai_proxy + merchant-routing-engine checkout on the same host; flip routstrd staticProviders to it. Now you have market routing + key-health breakers.
5. Turn on the Kalman loop: consumption/price/demand filters from src/, `/kalman-pricing` + kind-30315 publisher, cron hook to rewrite fees.
6. Wire P&L: usage_tracking (serves), keys.db (earnings, WAL-checkpoint before reading!), upstream cost ledger — margin check `fee >= 1.27`, profitability gate §6.

## 9. Pitfall checklist (each one bit us)

- keys.db live rows sit in `keys.db-wal` — copy db+wal+shm out and open read-write to checkpoint before querying, else near-empty schema.
- Trusted-mints gate: cashu tokens from untrusted mints fail with the MISLEADING "Token value is too small to cover swap fees".
- Wallet funding guard: cron PATH won't find bun-installed CLIs — resolve via `shutil.which` + explicit fallback.
- Zombie proxy: merchant-routing-engine missing on sys.path = silent degrade (§4).
- No public routstr images — build from source (§2.1).
- routstrd host binding: verify `ss -tlnp` shows your configured host, not `*:8008` (fork patch).
- NIP-98 auth rejected by design — don't try.
- BTC price feed: Binance timeout is normal (falls back to Kraken/Coinbase).
- Quota "0%" from z.ai windows may mean unreadable, not free (§5).

---

*Verified public 2026-09-04. Sources: felixfelix-bot/{tollgate-infrastructure-kit, merchant-routing-engine, hermes-bot, hermes-manager-scripts, routstrd} + Routstr/routstrd. MIT-licensed repos. Handover assembled by Felix's agent fleet — corrections welcome via Signal.*
