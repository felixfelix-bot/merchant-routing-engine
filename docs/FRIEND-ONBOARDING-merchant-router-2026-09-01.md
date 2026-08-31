# Merchant Router — Friend Onboarding Handover

**Date:** 2026-09-01 · **From:** c03rad0r (via Felix) · **Audience:** friends onboarding to the flat market router
**Repo:** https://github.com/felixfelix-bot/merchant-routing-engine (mirror: https://gitworkshop.dev/npub1xtzgnzzu88yfv9es3evykl3ympjz0gc3umy2e6rs3jazruhjyevqe63edh/relay.ngit.dev/merchant-routing-engine)

> **How to use this file (LLM consumption):** paste this file — or its raw URL, https://raw.githubusercontent.com/felixfelix-bot/merchant-routing-engine/main/docs/FRIEND-ONBOARDING-merchant-router-2026-09-01.md — straight into your agent. §1–§8 are written to be read by an LLM for self-onboarding; §0 is the human-facing summary.

---

## §0 FOR HUMANS — read this, ignore the rest

We built an inference router that treats every LLM API provider as a participant in a free market. Twelve providers (z.ai subscriptions, Ollama Cloud, OpenCode Go, NeuralWatt, DeepInfra, PPQ, Telnyx, OpenRouter, two sats-payable routstr nodes, …) compete per request. Each provider has a Kalman filter that learns its real cost per million tokens from live traffic; the router picks the cheapest healthy provider that can serve the model you asked for. No tiers by default, no favorites, no usage caps — if a provider is expensive, the market routes around it; if quota runs dry on one, the rest absorb the traffic on the very next request (failover is per-request try-next, not instant rebalancing).

What you can do with it:

1. **Buy inference with sats.** Point any OpenAI-compatible client at our public node `https://ai.orangesync.tech` (a [routstr](https://github.com/Routstr/routstr-core) node backed by this router). **How to pay:** base URL is `https://ai.orangesync.tech/v1` — curl-verify it before wiring it in (`curl -s https://ai.orangesync.tech/v1/models`). You pay per token in sats via Cashu ecash, using any Cashu-compatible wallet. No account, no signup.
   **What this is not:** it is not a hosted free API — the sats endpoint is paid — and there is no support SLA.
2. **Run your own copy.** `git clone` the repo and follow [`REPRODUCE.md`](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/REPRODUCE.md) — you get a working router with dummy keys in ~10 minutes, 77/77 tests green, including the failover chain and kill switches. Real keys are just environment variables.
3. **Add your own providers.** Bring any OpenAI-compatible key: add the env-var name to `config/providers.yaml` (env-var names only — never commit key values), then follow the 11-step adding-a-provider procedure in [`docs/flat-router-design.md`](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/flat-router-design.md) §6 (loader branch in `_load_external_keys()`, `EXTERNAL_PROVIDERS` entry, seed rate, optimizer registration, …). Registration is explicit, not automatic — `providers.yaml` alone does not put a provider on the market.
4. **Steal the ops playbook.** We run 20+ autonomous AI workers on this router around the clock, kept inside quota by gate machinery rather than luck. §4 is the full machinery — gates, pressure windows, urgency classification — written so your own LLM can onboard itself.

The important links, up front: [`README.md`](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/README.md) (caveat: the README predates the flat router and still describes the old architecture — z.ai always first, external providers as failover only; start with REPRODUCE.md) · [`REPRODUCE.md`](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/REPRODUCE.md) · [`docs/flat-router-design.md`](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/flat-router-design.md) · [`docs/SYSTEM-OVERVIEW.md`](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/SYSTEM-OVERVIEW.md) · [the big handover gist](https://gist.github.com/felixfelix-bot/63a3af3678ce5a694a01818ec575ff8c) (link-shared secret gist).

Questions go to Felix on Signal.

---

## §1 System in one paragraph (for LLMs)

A single Python proxy (`production/zai_proxy.py`, stdlib HTTP server, port 9099) fronts every LLM call for a [Hermes Agent](https://hermes-agent.nousresearch.com/docs) fleet: one manager context window, 22+ worker profiles, and ~30 cron jobs all send their `/v1/chat/completions` traffic through it. Routing is done by `flat_router.py`'s `select_provider()`: given a model name it returns an ordered candidate list — cheapest healthy provider first — and the proxy walks that list until one serves the request. Costs are learned, not assumed: two Kalman filter families (`src/price_kalman.py`, `src/consumption_kalman.py`) track each provider's $/M and burn velocity from real API responses. The same proxy exposes `/quota` (all provider lanes), `/v1/dispatch_gate` (should we dispatch work right now?), and `/v1/models` (what's served). Full design: [`docs/flat-router-design.md`](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/flat-router-design.md) (the router bible, ~1000 lines) and [`docs/KALMAN-ROUTING-ARCHITECTURE.md`](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/KALMAN-ROUTING-ARCHITECTURE.md).

**Core principle — NO CAPS.** No provider is ever capped, blocked, or disabled for spending too much. Price discovery handles it: expensive or depleted providers lose on effective price and traffic routes elsewhere. Abnormal burn is *surfaced* as alerts, never blocked. This is a first-class operator directive, not a default. (Scoping note: this directive covers the quota/lane gates of §4.1 — the designed-but-not-yet-wired cost gate in §4.2 does contain a DENY backstop; see there.)

## §2 The router core

- **Candidates, not favorites.** Every provider that *can* serve a model *is* a candidate. Exclusion happens via several gates, not just price: price (balance = infinity), health (failure count / circuit breaker), manual disable (a `.key_disabled_<name>` marker file removes one provider from the market), the funding gate (unfunded providers are skipped and retried on a timer), the live-catalog phantom guard (skips candidates absent from a fresh live-catalog snapshot — fail-open), and simple model capability (a provider that doesn't serve the model is never a candidate). A naming mismatch that hides candidates is treated as a bug (see §6 — it caused a 72-hour outage once).
- **Effective price** — the uniform formula (T5 path; tiers specialize it — see table): Kalman-predicted $/M × peak multiplier (3× for z.ai during UTC 6–10) × scarcity factor (ramps as quota depletes) × health factor (1×→∞ with failures) × pace factor (pinned to 1.0 on the flat path). T1 (z.ai quota) does not use this formula — see the tier table below.
- **Five pricing tiers** because "cost" means different things per provider — tier table: [`REPRODUCE.md`](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/REPRODUCE.md) §"The 5-tier pricing model":

| Tier | Providers | Price basis |
|---|---|---|
| T1 quota | z.ai ours + friend | sunk cost, $0.001 floor × time-decay toward weekly reset — unused quota is wasted quota, so it gets *cheaper* as reset approaches |
| T2 balance | NeuralWatt | base rate × (1 + depletion penalty) × NW correction factor (3.6× token-overcounting correction) |
| T3 flat | OpenCode Go $10/mo | $0.001 floor — marginal cost really is ~$0 |
| T4 included | Ollama Cloud ×2 | $0.001 floor, scarcity from session/weekly quota |
| T5 per-token | DeepInfra, PPQ, Telnyx, OpenRouter, routstr×2 | Kalman-measured real $/M |

- **Model names.** Canonical internal form is the slashed form (`deepseek/deepseek-v4-flash`); `MODEL_ALIASES` + `canonicalize_model()` map short forms onto it before candidate selection; dispatch-time translation to provider-native names (`deepseek-ai/DeepSeek-V4-Flash` for DeepInfra; `deepseek-v4-flash:0731` / `deepseek-v4-pro:0813` tagged IDs for Ollama Cloud) lives in a separate layer (`_PROVIDER_MODEL_NAMES` in the proxy). Registry and translation are separate layers — never conflate them.
- **Tests:** `test_flat_router.py` (77 tests) run against the repo layout without any real keys.
- **Kill switches:** `touch ~/.hermes/bot/.disable_flat_router` reverts to the legacy best_key() failover path; `.dispatch_frozen` pauses all worker dispatch. Both verified in the fresh-box E2E.
- **ADRs:** decisions are numbered and durable in [`docs/adr/`](https://github.com/felixfelix-bot/merchant-routing-engine/tree/main/docs/adr) — start with [ADR-001 price-first routing](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/adr/ADR-001-price-first-routing.md), [ADR-008 deterministic multipliers outside Kalman](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/adr/ADR-008-deterministic-multipliers-outside-kalman.md), [ADR-011 config-driven amortized seed pricing](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/adr/ADR-011-config-driven-amortized-seed-pricing.md).

## §3 Running your own copy

Follow [`REPRODUCE.md`](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/REPRODUCE.md) exactly. It was verified end-to-end on a fresh clone with dummy keys (2026-09-01): 77/77 tests pass, the proxy boots, models serve, the full failover chain (z.ai → Ollama Cloud → routstr sats nodes) behaves, and the kill switches work. Real usage needs exactly one thing: real env vars in `.env` (names are listed in [`config/providers.yaml`](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/config/providers.yaml) — the file contains variable *names*, never values). `PORT` is overridable via environment so a second instance can coexist with a live one.

Layout map: `flat_router.py` + `production/zai_proxy.py` = the live system; `src/` = the Kalman/pricing/gate modules (pure, contract-tested); `scripts/` = collectors, probes, migration tools; `docs/` = design docs, ADRs, incident writeups; `eval/` + `tests/` = evidence.

## §4 The ops layer — how 20+ workers share finite quota without starving

This is the part that's hard to reconstruct from code alone. Our fleet ([Hermes Agent](https://hermes-agent.nousresearch.com/docs) — manager orchestrates, workers execute, cron jobs watch) would burn any single provider's quota in hours if unsupervised. Four mechanisms keep it alive; all of them are *market-aligned*, and none of the quota/lane gates are spend caps.

### 4.1 The dispatch gate (can we work right now?)

There are two different things called "the gate", and they are not the same code:

- **The shell wrapper `zai-quota-gate.sh`** (our internal ops script — it is *not* shipped in this repo) answers the operator's nightly question "can workers run right now?" with this 4-step order:
  1. **Freeze marker** `~/.hermes/bot/.dispatch_frozen` exists → BLOCK (manual emergency stop).
  2. **Proxy liveness** — `/v1/models` reachable → if the proxy is dead, nothing else matters: BLOCK.
  3. **Friend-lane state** — `quota_state.friend.locked == false` → ALLOW.
  4. **Live probe fallback** — when the friend lane is locked (or the gate JSON is degraded/unparseable): send a real 1-token completion through the router (model `deepseek-v4-flash`, which routes through the live market to some non-z.ai provider). `choices` = alive (non-z.ai capacity proven), anything else = BLOCK.
- **`src/dispatch_gate.py`** (shipped in this repo) is the *advisory* gate behind the proxy's `/v1/dispatch_gate` endpoint. It answers a different question — *is there real headroom on some lane, with margin?* — in three dimensions: hardware availability (binary, checked first), then quota headroom with a safety margin that scales with hardware blast radius (it skips keys at or above 95% used), then an informational price recommendation (peak-hour 3× multiplier reported, never blocking on its own). No freeze-marker phase, no liveness phase.

**What the quota signals mean.** Usage percentage never *caps spend* — no provider is billed less or throttled for spending too much. But lane *lock state* is quota-derived: a lane is `locked` when any of its per-window used-pct thresholds is met (LOCK_THRESHOLDS in `production/zai_proxy.py`: weekly 60% ours / 80% friend, monthly 95%, 5-hour burst protection), and `src/dispatch_gate.py` itself skips keys at ≥95% used. So lane lock state (derived from per-window thresholds) is the only quota signal; the wrapper checks the friend lane and probes live capacity when it is locked. Unreachability of the gate endpoint is the rarer probe trigger — the main one is the friend lane being LOCKED: the probe exists to prove non-z.ai capacity when z.ai quota is exhausted.

Cached quota state is *advisory only*; the live probe is ground truth. Stale-state files have lied before (see §6).

### 4.2 Pressure windows — when non-urgent work may run

Binary availability isn't enough for work that can wait. Non-urgent work is classified **DEFER** or **BATCH** and held until a cheap window: the pressure logic asks whether the cheapest eligible effective $/M is at or below the 20th percentile of the trailing 7-day hourly medians. Urgent work (**NOW**: active money bleed, hard deadline) dispatches anywhere; **SOON** waits up to its deadline then escalates. The full urgency system — schema, enforcement layers, the price brain — is designed in [`docs/DESIGN-urgency-enforcement.md`](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/DESIGN-urgency-enforcement.md) and [`docs/cost-gate.md`](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/cost-gate.md). Note the tense: per `docs/cost-gate.md`'s wiring status, the cost gate (`src/cost_gate.py`) is the pure decision core only — **designed, not yet wired into production**; nothing in production calls it yet. One design caveat to the no-caps rule: the cost-gate design's $15/day paid-tier backstop is a DENY (i.e. a cap on paid tiers) — though it is not live today.

Two hard rules from lived failures:

- **A hold must be reversible by the holder.** Any watchdog that parks tasks on quota must also un-park them when the gate reopens — otherwise the board deadlocks silently while every cron logs "ok" (5-day stall, Aug 2026). Watchdogs implement a self-revive phase.
- **Never gate work behind the thing it fixes.** A gate that blocks "make the gate alternates-aware" holds its own repair hostage. When detected, release the chain head manually under the new rule's own logic.

### 4.3 Gate ALLOW ≠ lane ALIVE

`can_dispatch: true` means *some* provider can serve *some* model. It does not mean the specific model lane you're about to pin a worker to is serving. Before dispatching onto a specific model, probe that exact lane: 1–8 token completion, `choices` → alive, error → pick another lane. Two checks, two questions: the gate answers **CAN**, the lane probe answers **WHERE**. Workers have died twice on skipping this distinction.

### 4.4 Cost telemetry — surface, never block

Every proxied call is logged to SQLite (`api_calls`: model, provider, tokens, cost, cost_source measured/estimated) and rolled up to `daily_spend`. Cost is layered and labeled, because conflating them produces false alarms (provenance: the L0–L3 vocabulary, thresholds and digest below come from our internal ops script `cost-escalation-check.py` — they are not part of this repo):

- **L0 fixed** — subscription fees (sunk, amortized daily)
- **L1 prepaid** — balances and quota windows (the real consumables)
- **L2 estimate** — rate-card math from token counts (NOT money — never sum it into "cash spent")
- **L3 revenue** — what routstr customers pay us — **GAP: no revenue tracker exists yet**; our own alert machinery flags this in the daily digest

A digest once a day, anomaly alerts on threshold breach (real-variable > $60/day, invisible-burn detector for providers logging >50% NULL costs, EWMA outliers). Alerts tell Felix; nothing self-blocks. See [`docs/cost-gate.md`](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/cost-gate.md) and the invisible-burn safety net in `_log_api_call` (cost is never NULL for a known provider — estimation backfills at insert time).

### 4.5 The full loop, end to end

Operator states intent ("fix X, schedule the work") → manager classifies urgency with the operator (NOW/SOON/DEFER/BATCH — cost of waiting vs cost of tokens, quota state attached) → tasks land on a kanban board with urgency recorded → gate chain decides when each may run → workers execute through the proxy (market routes each call) → Kalman filters learn from every response → cost telemetry surfaces burn → alerts go to the operator. Nobody hand-routes anything; the market does, and the gates only decide *when*.

## §5 Selling surplus — routstr nodes

Surplus capacity is sold, not wasted: [`routstr-core`](https://github.com/Routstr/routstr-core) (GPL-3.0) is a Cashu-paywalled inference gateway; our public instance `https://ai.orangesync.tech` fronts this router. Customers pay sats per request (base URL `https://ai.orangesync.tech/v1`, Cashu ecash, no account/signup); the sell price is real cost × margin (dual pricing surface — internal routing price is sunk-cost-based and NEVER exposed; see [ADR-005 three-layer actor separation (status: Proposed)](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/adr/ADR-005-three-layer-actor-separation.md) and [`docs/REAL_PRICE_SYSTEM_DESIGN.md`](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/REAL_PRICE_SYSTEM_DESIGN.md)). Ops runbook: [`docs/routstr-ops-runbook.md`](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/routstr-ops-runbook.md); PnL collection: `scripts/routstr-pnl-collect.py`.

Note: `api.routstr.com` is the upstream *reference* node, not ours.

## §6 Incident lore — read before touching anything

Each of these is documented in depth in our internal ops log; in-repo traces are in code comments where noted (e.g. the alias-mismatch fix at `flat_router.py` ~L92, the silent-substitution fix at `production/zai_proxy.py` ~L5395). The one-line versions:

1. **Model-name alias mismatch (Aug 25, 72h outage).** Registry listed `deepseek-v4-flash` for one provider, `deepseek/deepseek-v4-flash` for nine others. Exact-match filtering → requests for the short form had ONE candidate → when that provider capped, 2,305 requests 503'd while the other providers sat idle, and all 22 workers crash-looped. Fix: alias canonicalization at both entry points. Lesson: a naming mismatch IS a cap.
2. **Silent substitution (Aug 25).** Unknown models were silently replaced with a fallback model, HTTP 200. A paying customer asked for one model, got another. Fix: unknown model → 503, never 200-with-different-model. This is a correctness principle, not a preference.
3. **Stale capability snapshot (Aug 29).** A provider added `glm-5.3` to its catalog; our hardcoded map still rewrote it to `glm-5.2` — wrong model served with HTTP 200. Lesson: capability is *discovered* (drift cron diffs live catalogs vs all three hardcoded surfaces), never assumed; a PHANTOM/exclusion claim is a point-in-time observation, not a law.
4. **Double-logging (Aug 24).** Flat router loop + dispatch handler both logged → phantom `api_calls` rows inflated cost alerts by 29%. Symptom: two rows per provider (`tier=flat_router` + `tier=<handler>`), ~6ms apart.
5. **Gate-script bug cluster (Aug 27, 5-day board stall).** Four stacked gate bugs: `sys.exit(0)` inside `try:` swallowed by bare `except:`; jq shape mismatch silenced by `|| echo 0`; word-splitting ran the wrong task ID; probe timeout below median latency → ~50% flaky. Lesson: test gates in BOTH directions — a gate whose success path fails is indistinguishable from "quota exhausted".
6. **Phantom availability (Aug 15).** A removed key's empty fetch data defaulted to "available" — the gate passed on a key that no longer existed. Lesson: availability must fail CLOSED on missing data.
7. **Dispatched-model divergence (Aug 30).** A `delegate_task` asking for model X can be served model Y — HTTP 200, no error. Before trusting "N independent reviews" or model diversity, verify the served model (the result JSON carries it; the usage DB records it).

## §7 Dead code & stale names (2026-09-01 sweep)

**Sweep complete 2026-09-01** (full audit of `production/zai_proxy.py` 7,383 L + live copy + `flat_router.py`; full report is an internal ops artifact, not in this repo). Headline: **oxalpha is double-dead, ≈198 lines** — (a) its only routing hook lives inside `_try_external_failover`, reachable only on the old path behind `.disable_flat_router`; the flat router dispatches externals via `_try_external_single`, which has no oxalpha hook; (b) the promo key 401s upstream (live log: `key DEAD … poller + failover DISABLED`). A missing `pyyaml` also silently disables the tier (one log line, fail-closed by design; moot — live venv has pyyaml). Stale: `providers.yaml` still declares `failover.enabled: true` for the dead key.

Removal candidates, safest first (all line refs = repo copy):

| # | Item | Lines | Verdict |
|---|------|-------|---------|
| 1 | `_weekly_pct` (L4257-4263) | 7 | DEAD — 0 callers, superseded by per-window `is_key_locked` |
| 2 | `_check_spend_cap` stub (L3802-3809) | 8 | DEAD — 0 callers, deactivated 2026-08-20 |
| 3 | Legacy backoff aliases `_BACKOFF_BASE/CAP_SECONDS` (L907-909) | 3 | DEAD — 0 external refs |
| 4 | `_clear_ollama_paywall_flag` (L977-984) | 8 | DEAD — 0 callers; paywall flag auto-expires Mondays anyway |
| 5 | oxalpha cluster (L569-613 tier, L5165-5258 serve, L5384-5393 hook, L7115-7156 poller, L7370-7374 starter) + `providers.yaml` oxalpha block | ~198 | DEAD — remove as ONE unit (cross-refs); operator approval; loses free-tier revival path if a new promo key ever appears |
| 6 | Global spend-cap dead branch (L5665-5681 + stub L3811-3818) | ~25 | Dead branch — **recommend KEEP**: re-activatable runaway-loop circuit breaker |

**DO-NOT-REMOVE (all verified against live):** the `best_key()` chain + old path (≈910 L) — rollback safety net behind `.disable_flat_router`, AND pinned live by `/tier` (consumed by `throttled_daemon.sh`), `/quota`, and `_refresh_loop`. `WORKER_FALLBACK_MODEL` (L781) — definition-only safety net since the Aug-25 silent-substitution fix, kept deliberately. **False-dead trap:** `_try_zai_key`, `_try_telnyx`, `_try_external_single`, `shadow_compare`, `_opencode_go_quota_fraction` look uncalled inside `zai_proxy.py` but are dispatched dynamically/cross-module by `flat_router.py` (L816/834/840/673, and zai_proxy L5735 on the live flat path) — removing any of them breaks the live router.

**Sweep corrections:** `_OLLAMA_ONLY_MODELS` is NOT gone — it lives on as a backward-compat alias of `_OLLAMA_EXCLUSIVE_MODELS` at `src/live_router.py` ~L209 and is imported by `tests/test_live_router_quota_regime.py`. Confirmed gone (0 matches): `_MODEL_COSTS`, semantic cache, local-ollama cascade. No commented-out code blocks >10 L. Endpoints `/v1/pricing` and `/spend` have no found consumers (read-only, harmless). `flat_router.py` itself: no dead code.

**Known drift:** repo copy is 2 freshbox-fix hunks ahead of the deployed proxy (`bot/.env` 3rd key path + env-overridable `PORT`, merged as 3b20482). Benign; syncing the deployed copy is a separate decision.

## §8 Link index (all curl-verified 2026-09-01)

**This repo (GitHub, main branch):**
- https://github.com/felixfelix-bot/merchant-routing-engine — repo root
- https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/README.md
- https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/REPRODUCE.md — fresh-box quick start
- https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/flat_router.py — the router
- https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/test_flat_router.py — 77 tests
- https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/production/zai_proxy.py — the live proxy (published copy)
- https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/config/providers.yaml — provider registry (env-var names only)
- https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/src/dispatch_gate.py — the gate implementation
- https://github.com/felixfelix-bot/merchant-routing-engine/tree/main/docs/adr — ADR-001 … ADR-013

**Design & ops docs (same repo, docs/):**
- https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/flat-router-design.md — router bible
- https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/SYSTEM-OVERVIEW.md
- https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/SYSTEM-OVERVIEW-LLM-HANDOVER.md — the prior LLM-facing handover
- https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/KALMAN-ROUTING-ARCHITECTURE.md
- https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/cost-gate.md — cost gate design
- https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/DESIGN-urgency-enforcement.md — NOW/SOON/DEFER/BATCH enforcement
- https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/quota-pressure-design.md
- https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/REAL_PRICE_SYSTEM_DESIGN.md
- https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/routstr-ops-runbook.md
- https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/routstr-friend-onboarding-handover.md — prior friend onboarding (routstr angle)
- https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/HANDOVER-routing-telemetry-friend.md

**Nostr mirror:** https://gitworkshop.dev/npub1xtzgnzzu88yfv9es3evykl3ympjz0gc3umy2e6rs3jazruhjyevqe63edh/relay.ngit.dev/merchant-routing-engine

**External:**
- https://hermes-agent.nousresearch.com/docs — Hermes Agent docs (the agent framework this router feeds)
- https://github.com/Routstr/routstr-core — routstr gateway (GPL-3.0)
- https://ai.orangesync.tech — our public sats-payable inference node (live)
- https://gist.github.com/felixfelix-bot/63a3af3678ce5a694a01818ec575ff8c — the big picture handover gist (links §6 of that doc to this router)