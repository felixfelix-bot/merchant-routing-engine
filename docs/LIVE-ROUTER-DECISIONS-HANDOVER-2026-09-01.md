# The Flat Market-Based Live Router — Purpose, Decisions, Measurements, Logging

**Date:** 2026-09-01 · **From:** c03rad0r (via Felix) · **Audience:** LLM context windows (friends onboarding to the system)
**Repo:** https://github.com/felixfelix-bot/merchant-routing-engine (nostr mirror: https://gitworkshop.dev/npub1xtzgnzzu88yfv9es3evykl3ympjz0gc3umy2e6rs3jazruhjyevqe63edh/relay.ngit.dev/merchant-routing-engine)

> **How to use this file (LLM consumption):** paste this file — or its raw URL, `https://raw.githubusercontent.com/felixfelix-bot/merchant-routing-engine/main/docs/LIVE-ROUTER-DECISIONS-HANDOVER-2026-09-01.md` — into your context. It is self-contained, but every § ends in links into the repo so you can pull the primary sources as you need depth. Read order for full onboarding: this doc (why + how decisions happen) → [`docs/FRIEND-ONBOARDING-merchant-router-2026-09-01.md`](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/FRIEND-ONBOARDING-merchant-router-2026-09-01.md) (run your own copy + the ops/gate layer) → [`REPRODUCE.md`](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/REPRODUCE.md) (hands-on) → [`docs/flat-router-design.md`](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/flat-router-design.md) (the bible).

---

## §1 Purpose — why a market, not a routing table

We run an AI fleet: one manager context window, 22+ worker profiles, ~30 cron jobs — every LLM call they make goes through a single local proxy (`production/zai_proxy.py`, stdlib HTTP server on 127.0.0.1:9099) which fronts **12 upstream providers** with **five fundamentally different billing models**:

- z.ai subscriptions (quota windows: 5-hour / weekly / monthly)
- Ollama Cloud ×2 (tokens included with subscription, session/weekly limits)
- OpenCode Go ($10/mo flat rate)
- NeuralWatt (prepaid balance)
- DeepInfra, PPQ, Telnyx, OpenRouter, routstr ×2 (per-token; the two sats-payable ones are `routstr` and `routstrd` — our self-hosted Cashu/Lightning-metered inference nodes)

A hardcoded cascade over that heterogeneity failed three different ways (all documented, see §7): it picked the wrong fallback (17% measured waste), it never compared *paid* routes against *subscription opportunity cost* (one bad night routed a burst onto a 65×-pricier sats node), and a registry naming gap left one model with a single candidate — when that provider died, 2,305 requests 503'd for 72 hours while 8 cheaper providers sat idle.

So the system was rebuilt around one principle:

**ALL providers are EQUAL market participants. Each request is routed to the cheapest HEALTHY provider that can serve the requested model. Price IS the failover chain.**

Three operator directives follow from it (they are policy, not defaults):

1. **NO CAPS.** No provider is ever capped, blocked, or disabled for spending too much — nothing in the current code path blocks on spend. Abnormal burn is *surfaced* as alerts, never blocked. If a provider is expensive or depleted, the market routes around it on the very next request — pressure re-prices, it does not prohibit. (Accuracy scoping: the FRIEND doc §4.2 documents a designed-but-not-wired $15/day paid-tier DENY backstop in the cost-gate design — it exists on paper only; nothing in production calls it today.)
2. **Price over thresholds.** Decisions are made by comparing effective prices, not by if/else quota rules. Quota pressure exists *inside the price*, as a multiplier (§3).
3. **Separation of concerns.** The MANAGER (or any client) decides *which model* to request — the proxy decides *which provider* serves it. The router never rewrites your model; serving a different model than requested with HTTP 200 is a correctness bug, not a routing choice.

Live proof it works: as of 2026-09-01 our primary z.ai key is weekly-locked (100% used, resets in ~43h). Terminology: a z.ai key's *quota windows* are its three overlapping usage-accounting periods (5-hour / weekly / monthly); "locked" = a window's used_pct crossed its per-key `LOCK_THRESHOLDS` (`production/zai_proxy.py`) — a legacy binary gate and only the weakest promise, since the market's answer to the same state is scarcity pricing (§3). Nothing crashed, nothing was manually reconfigured — the flat router kept serving every request from the next-cheapest healthy providers, automatically, per request.

---

## §2 The decision loop — what happens per request

Code: [`flat_router.py`](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/flat_router.py) (`select_provider()`, `compute_effective_price()`, both line-anchored below), dispatched by the proxy's dispatch loop. One pass:

```
request(model, messages)
  │
  1. canonicalize_model()          # short form → canonical slashed form, MODEL_ALIASES
  2. filter candidates              # PROVIDER_MODELS registry: who can serve this model?
  3. phantom guard                  # live-catalog check: skip candidates absent from a
  │                                 # fresh upstream catalog snapshot (fail-open)
  4. health gate                    # backoff windows, paywall flags, circuit breaker,
  │                                 # .key_disabled_<name> markers, funding gate
  5. price each survivor            # compute_effective_price() — 5-tier (§3)
  6. sort cheapest-first            # ordered candidate list
  │
  dispatch loop: try candidate 1 → fail? mark failure, try next → all fail → 503
  success → _update_kalman_after_request()   # learn real cost + token burn (§4)
```

The ordered list IS the failover plan. No rebalancing daemon, no drain-and-switch: if the winner dies mid-flight, the very same request walks to candidate #2. Every provider that *can* serve a model *is* a candidate; exclusion happens only through price (∞), health, manual disable, funding, or capability — a naming mismatch that hides candidates is treated as a bug (it once caused a 72-hour outage, §7).

Kill switch: `touch ~/.hermes/bot/.disable_flat_router` reverts to the legacy failover path instantly. The rollback path is deliberately preserved and tested.

---

## §3 The decision criterion — effective price, where quota pressure lives

Uniform formula (from `src/routing_optimizer.py`, tier-specialized in `flat_router.py` ~L571):

```
effective_cost = base_rate × peak_mult × scarcity_factor × health_factor × pace_mult
```

**Key idea: quota pressure is PRICING, not gating.** A depleting provider does not get switched off — it gets more expensive until the market walks away from it by itself. Scarcity ramps `1.0 → ∞` as the provider depletes; at 100% used (or 429/paywall) the price is ∞, i.e. priced out, and the next candidate serves instead. The provider's quota percentage never blocks anyone; it just moves a number in a sort.

### The five tiers (why "cost" is tier-specialized)

"Dollar price" means different things per billing model — `PROVIDER_TIER` in `flat_router.py` recognizes five (full table: [`REPRODUCE.md`](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/REPRODUCE.md) §"The 5-tier pricing model"):

| Tier | Class | Providers | Effective price logic |
|---|---|---|---|
| T1 | quota | z.ai ours + friend | sunk cost: `$0.001 × max(0.0001, days_to_reset/7) × peak_factor × health_factor` — **price DECAYS toward the weekly reset** (see below); `peak_factor` = the §3 peak multiplier (3.0 in-window / 1.0 otherwise) on the optimizer path, or a 0.5× off-peak discount in the seed-rate fallback variant |
| T2 | balance | NeuralWatt | base rate × (1 + depletion penalty) × NW correction factor (0.2762 = 1/3.6 — fixes a measured 3.6× token-overcount) |
| T3 | flat | OpenCode Go | $0.001/M floor + session-quota scarcity |
| T4 | included | Ollama Cloud ×2 | $0.001/M floor + session/weekly scarcity |
| T5 | per-token | DeepInfra, PPQ, Telnyx, OpenRouter, routstr ×2 | Kalman-measured real $/M — the market price, discovered by measurement |

### Time-aware pricing (the counter-intuitive T1 rule)

z.ai quota is a **sunk cost** — the monthly fee is paid whether or not the quota is used, and unused quota vanishes at the weekly reset. So the *marginal* cost of an in-quota z.ai token is ~$0, and the effective price **decays** as reset approaches: use-it-or-lose-it. At 7 days to reset it ties with other floors; at 1 day to reset it is strongly preferred; at 100% used it is unavailable. The decay applies to the $0.001 floor, never to zero. Design doc: [`docs/quota-pressure-design.md`](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/quota-pressure-design.md) + time-aware pricing design.

### Pressure curves (the scarcity multiplier, from the pricing reform)

`pressure(u) = 1 + K·t/(1-t)` where `t = (usage − onset)/(1 − onset)` — asymptotic: as usage → 100%, price → ∞ (router always finds an alternative first, by construction). Superposition: session × weekly × monthly factors MULTIPLY. Onsets are staggered per provider class (quota keys feel pressure at 60–70% used, credit-balance keys at 80%) so providers leave the market in a graceful order instead of all at once. NeuralWatt (T2) DOES carry a depletion penalty in its tier formula — linear in the depleted fraction, capped at 2.0× at zero balance (`NW_MAX_DEPLETION_PENALTY`, with its own `.disable_depletion_penalty` kill switch, `flat_router.py`): a bounded repricing, not the asymptotic curve. The routstrd lesson (§7), correctly cited, is about opportunity cost: a hardcoded chain paid ~65× ollama's measured rate because it never compared *paid* routes against subscription opportunity cost (root cause: [`docs/PLAN-cost-gate-reform-v2-2026-08-21.md`](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/PLAN-cost-gate-reform-v2-2026-08-21.md) §1).

### The other multipliers (all deterministic — see §4 for why they are outside the Kalman)

- **peak_mult** — 3.0× for z.ai keys during peak hours, 1.0 otherwise (z.ai peak = Beijing 14:00–18:00). Authoritative definition: `peak_multiplier()` in [`src/price_kalman.py`](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/src/price_kalman.py) uses `peak_hours_utc=(6, 10)` with an INCLUSIVE end — so UTC 06:00–10:59. Fair warning: this window is (deliberately or not) inconsistent across surfaces — the proxy's `_is_peak_hour()` uses hours `{6,7,8,9,10}` (same wall-clock result as price_kalman), while `src/pricing_engine.py`'s variant uses `{6,7,8,9}` (one hour shorter); when in doubt, `price_kalman.py` is the live routing truth. Deterministic by design ([ADR-003](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/adr/ADR-003-deterministic-peak-multiplier.md)): peak scarcity is a schedule fact, not a measurement.
- **health_factor** — graduated pricing on recent failures: 1.0× (0) → 1.5× (1–2) → 3.0× (3–5) → 10.0× (6–10) → ∞ (circuit breaker, >10). A flaky provider becomes expensive before it becomes excluded.
- **pace_mult** — per-provider burn-rate pacing (T2–T5 paths; pinned on the flat path).

---

## §4 How it measures — two Kalman families, and a strict "observer, not controller" boundary

Code: [`src/price_kalman.py`](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/src/price_kalman.py), [`src/consumption_kalman.py`](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/src/consumption_kalman.py); design: [`docs/KALMAN-ROUTING-ARCHITECTURE.md`](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/KALMAN-ROUTING-ARCHITECTURE.md).

**PriceKalman — "what does this provider really cost per million tokens?"** State: `[base_rate, rate_velocity]`. Input: real per-call cost extracted from provider API responses (`cost_usd / tokens × 1e6`, SSE-aware parsing). Output: the `base_rate` in the effective-price formula. Why a filter instead of a rolling average: single-call costs are noisy (prompt-cache discounts, provider-side rate quirks, occasional parse gaps) and a one-off spike must not reroute the whole fleet — but the velocity term still catches genuine trend changes fast. The flagship convergence case is **ollama_cloud: seeded at $0.40/M (a stale per-token estimate) and measured down to ~$0.0155/M over ~49k real calls** — a ~26× correction the seed could never have known (init comment, `production/zai_proxy.py`). PPQ is the counter-example that keeps seeds honest: its measured rate is **$0.1391/M over 623 calls against a deliberately conservative $0.80 seed** — the filter replaces seed knowledge with market truth only as fast as real traffic arrives.

**ConsumptionKalman — "how fast are we burning this provider, and when does it die?"** State: `[burn_rate, velocity, acceleration]` per provider, per quota window — a 3-state constant-acceleration model (ADR-002 invariant #2) that evolved from the original 2-state `[volume, velocity]` filter. Attribution caveat: the live predictions on the proxy's `/quota` endpoint (`burn_rate_tph`, `hours_left`, `projected_total_pct`, `will_exhaust`) are produced by `burn_predictor.py` — that original 2-state filter, which lives in the live ops dir (`~/.hermes/bot/`) and is NOT in this repo; `src/consumption_kalman.py` is its 3-state successor here (the test suite was extracted from the original's KalmanPredictor). The predictions are what the *scheduling* layer (dispatch gate, §6) consumes; they never block serving.

**The boundary rule — deterministic multipliers stay OUTSIDE the Kalman filters** ([ADR-008](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/adr/ADR-008-deterministic-multipliers-outside-kalman.md), [ADR-002](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/adr/ADR-002-multi-kalman-separation.md)). The Kalman filters *measure* (observer); peak/scarcity/health multipliers are deterministic step functions applied on top of the prediction. Mixing them into the filter state would let a quota spike contaminate the learned base rate — the filter would "forget" the provider's true price after recovery. The one designed exception: an LQG (*linear-quadratic-Gaussian* — closed-loop optimal control, the controller counterpart of the Kalman observer) *controller* for T1 quota exhaustion (drive usage to hit 100% just before reset) exists as design only — not implemented.

**Where rates come from before measurements exist** — the seed-then-replace pattern ([ADR-011](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/adr/ADR-011-config-driven-amortized-seed-pricing.md)): new providers start from conservative config-driven seeds (`_SEED_RATES`, `flat_router.py` ~L232); every real call replaces seed knowledge with measurement. For providers that never return per-call cost (PPQ, OpenRouter), rate is computed from **balance depletion**: `Δbalance / tokens`. Every logged cost carries a `cost_source` tag from the real vocabulary — `measured` / `estimated` / `rate_derived` / `cached_rate_derived` / `flat_rate` / `backfilled` / `rate_derived_fallback` — so downstream consumers never mistake an estimate for money.

**The evidence is published** — the historical telemetry behind those measurements ships as a dataset in this repo: [`datasets/routing-telemetry/`](https://github.com/felixfelix-bot/merchant-routing-engine/tree/main/datasets/routing-telemetry) ([`README.md`](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/datasets/routing-telemetry/README.md), full DDL in `SCHEMA.sql`; ~3 weeks of every logged call, 2026-08-12 → 2026-09-01), with a frozen snapshot attached to the [`routing-telemetry-2026-09-01` release](https://github.com/felixfelix-bot/merchant-routing-engine/releases/tag/routing-telemetry-2026-09-01).

**Regime shifts** (a provider silently changing its price structure) are alertable: filter uncertainty + prediction-error trend are monitored ([ADR-013](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/adr/ADR-013-regime-shift-kalman-alerting.md)).

---

## §5 Why it logs — five jobs the telemetry does, and one bug that inflated it 29%

All telemetry is SQLite, written by the proxy per request. Schema and query recipes: [`docs/HANDOVER-routing-telemetry-friend.md`](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/HANDOVER-routing-telemetry-friend.md), [`datasets/routing-telemetry/`](https://github.com/felixfelix-bot/merchant-routing-engine/tree/main/datasets/routing-telemetry).

| Table | One row per… | Feeds |
|---|---|---|
| `api_calls` | served request (model, provider, tokens, `cost_usd`, `cost_source`, session) | Kalman updates, rate discovery, burn-share, audits |
| `key_decisions` | routing decision (`reason` = top-5 candidate chain, truncated at 120 chars, `chosen_key` = winner) | "why did traffic go there" forensics |
| `routing_shadow_decisions` | requested-vs-served model pair | substitution/divergence detection |
| `daily_spend` | provider×day rollup | cost digest + escalation alerts |
| `provider_telemetry` | attempt (latency, billed vs actual tokens, response validity) | CPVO quality (cost-per-*valid*-output) |

**Job 1 — decisions need evidence.** The whole point of the market is that prices are *measured*, not assumed. Without per-call logging there is no Kalman input, and the router is a random walk wearing a lab coat. This is also why costs are backfilled when extraction fails: an un-logged cost is an un-priced provider.

**Job 2 — invisible burn must stay visible.** A provider whose costs fail to parse logs NULL costs; a NULL cost reads as *free*; a free provider wins every price sort. The detector (6h cron) alerts on any provider with ≥10 calls and >50% NULL costs, and `_log_api_call` backfills estimates at insert time as the final safety net. Historical case: 142 calls / 100% NULL / ~3M tokens invisible on one provider before this net existed.

**Job 3 — incident forensics need the ordering chain.** When someone asks "why did we use paid X while cheap Y sat idle?", `key_decisions.reason` (the top-5 candidate chain, truncated at 120 chars) vs `chosen_key` (the winner) answers it in one query. The answer is occasionally "Y appeared in every chain but never won — because it was health-excluded or dead", which is exactly the distinction a `used_pct` column cannot give you.

**Job 4 — requested vs served must be checkable.** The proxy must never serve a different model than requested without saying so. `routing_shadow_decisions` + the response `model` field make every substitution detectable; the incident lore (§7) has three cases of silent-model-family bugs that only telemetry exposed.

**Job 5 — alerting replaces blocking (the NO CAPS corollary).** Because the operator directive is *surface, never block*, alerts are only as good as the data layers under them. Costs are labeled in layers to prevent false alarms: **L0** fixed subscription fees (sunk), **L1** prepaid balances/quota (real consumables), **L2** rate-card estimates from token counts (explicitly NOT money — never summed into "cash spent"), **L3** revenue from the sats nodes. Conflating L2 with L1 once produced a "$404/day" alert when real paid spend was ~$27/day. Daily digest + anomaly thresholds (invisible burn, EWMA outliers, real-variable spend). Provenance note: the L0–L3 vocabulary, thresholds and digest come from our internal ops script `cost-escalation-check.py` — NOT in this repo; [`docs/cost-gate.md`](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/cost-gate.md) is a different artifact — the designed-but-not-yet-wired percentile cost gate (`src/cost_gate.py`, pure decision core).

**The logging bug worth remembering:** the dispatch loop and the dispatch handlers both logged, creating phantom duplicate `api_calls` rows ~6ms apart (two rows per call: `tier=flat_router` + `tier=<handler>`), inflating cost alerts by 29%. Fix: guard both writes with the same `_spend_recorded` flag. Lesson generalizes: **every state-changing side effect in a retry loop needs an idempotency guard.**

---

## §6 What decides what — the four layers, and the decisions deliberately NOT made by the router

| Layer | Question it owns | Scope | Code |
|---|---|---|---|
| **Proxy + flat router** (this doc) | *which provider serves this request* | per request, milliseconds | `flat_router.py` + `production/zai_proxy.py` |
| **Dispatch gate** (scheduling) | *may workers be dispatched right now?* | fleet admission | [`src/dispatch_gate.py`](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/src/dispatch_gate.py) — advisory `/v1/dispatch_gate`; the operator's shell wrapper checks freeze marker → proxy liveness → friend-lane lock → live 1-token probe. Gate ALLOW ≠ lane ALIVE: probe the specific model lane before pinning workers to it. |
| **Urgency classification** (deferral) | *when may non-urgent work run?* | per task | NOW (money bleeding / hard deadline) dispatches anywhere; SOON waits for deadline; DEFER/BATCH wait for a cheap window (cheapest-eligible price ≤ p20 — the 20th percentile — of trailing 7d hourly medians). Design: [`docs/DESIGN-urgency-enforcement.md`](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/DESIGN-urgency-enforcement.md), [`docs/cost-gate.md`](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/cost-gate.md). |
| **Alerts** | *what should a human know about* | observability | digest + anomaly alerts; surface, never block (§5) |

**Decisions deliberately NOT made by the router:**

- **Which model to run** — the requesting client (manager, worker, cron) owns model choice; the router only picks the provider. Model substitution is forbidden (unknown model → 503, never 200-with-a-different-model).
- **Auto-downgrade on price** — refused by operator decision: downgrading quality can fail quality gates and force re-runs, burning more tokens than the downgrade saved. Cheap windows are chosen by *deferring work*, not by silently cheapening the model.
- **Per-request spend caps** — deactivated permanently by operator decision ("too granular"). Strategic budgets live at the dispatch gate, market pressure lives in the router; the two layers are kept separate on purpose.

This layering is the actual architecture answer to "how does quota pressure control spending": pressure re-prices (router) → expensive moments defer work (urgency layer) → gates admit workers only when some lane has headroom (gate) → nothing anywhere caps a provider. Each mechanism does one thing.

---

## §7 Incident lore — decisions under pressure, from lived failures

Full versions: items 1, 3, 4 in [`docs/FRIEND-ONBOARDING-merchant-router-2026-09-01.md`](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/FRIEND-ONBOARDING-merchant-router-2026-09-01.md) §6 and the ADR lineage; item 2's root-cause writeup lives in [`docs/PLAN-cost-gate-reform-v2-2026-08-21.md`](https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/PLAN-cost-gate-reform-v2-2026-08-21.md) §1. The ones that shaped the decision logic in §3:

1. **Model-name alias mismatch (Aug 25, 72h outage).** Registry listed the same model under different name forms per provider; exact-match filtering gave one model a SINGLE candidate. Provider capped → 2,305 × 503 while 8 cheaper providers sat idle; every worker profile crash-looped. Fix: alias canonicalization at both entry points. Lesson that is now law: **a naming mismatch IS a cap** — the market must contain every provider that can serve, or pressure pricing is meaningless.
2. **The routstrd anomaly (Aug 21).** All cheap lanes failed in the same hour; the old hardcoded chain fell through to a pay-per-token sats node at ~65× ollama's measured rate ($18.81/day vs $0.15–4 normal). Lesson: **a hardcoded failover chain never compares paid routes against subscription opportunity cost** — only a price sort over all candidates does, every request.
3. **Stale capability snapshot (Aug 29).** A provider silently added `glm-5.3` to its catalog; our hardcoded map still rewrote it to `glm-5.2` — wrong model served, HTTP 200, happy-path tests green. Lesson: **capability is discovered, not assumed** — a drift cron diffs live catalogs against all three capability surfaces, and probe evidence older than ~30 days is suspect.
4. **Phantom availability (Aug 15).** A removed key's empty fetch data defaulted to "available" — gate passed on a key that no longer existed. Lesson: availability fails CLOSED on missing data; stale cached quota state is advisory only, the live probe is ground truth.

---

## §8 Link index — build your context from these (all curl-verified 2026-09-01)

**Entry points:**
- Repo root: https://github.com/felixfelix-bot/merchant-routing-engine
- Fresh-box reproduction: https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/REPRODUCE.md
- Friend onboarding (run-your-own + ops layer): https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/FRIEND-ONBOARDING-merchant-router-2026-09-01.md
- Router bible: https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/flat-router-design.md
- Kalman architecture: https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/KALMAN-ROUTING-ARCHITECTURE.md
- Prior LLM-facing overview: https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/SYSTEM-OVERVIEW-LLM-HANDOVER.md

**Decision + measurement core (read in this order for depth):**
- Router code: https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/flat_router.py
- Test suite (77): https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/test_flat_router.py
- Price Kalman: https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/src/price_kalman.py
- Consumption Kalman: https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/src/consumption_kalman.py
- Quota pressure design: https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/quota-pressure-design.md
- Dispatch gate: https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/src/dispatch_gate.py
- Urgency enforcement design: https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/DESIGN-urgency-enforcement.md
- Cost gate + cost layers: https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/cost-gate.md

**Decision records (ADRs):**
- ADR-001 price-first routing: https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/adr/ADR-001-price-first-routing.md
- ADR-002 multi-Kalman separation: https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/adr/ADR-002-multi-kalman-separation.md
- ADR-008 deterministic multipliers outside Kalman: https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/adr/ADR-008-deterministic-multipliers-outside-kalman.md
- ADR-011 config-driven amortized seed pricing: https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/adr/ADR-011-config-driven-amortized-seed-pricing.md
- ADR-013 regime-shift Kalman alerting: https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/adr/ADR-013-regime-shift-kalman-alerting.md
- Full ADR index: https://github.com/felixfelix-bot/merchant-routing-engine/tree/main/docs/adr

**Sell-side of the same market (context, not required):**
- routstr-core (GPL-3.0 sats-payable gateway): https://github.com/Routstr/routstr-core
- Our public node (live): https://ai.orangesync.tech
- Ops runbook: https://github.com/felixfelix-bot/merchant-routing-engine/blob/main/docs/routstr-ops-runbook.md
- Big-picture handover gist (fleet + market + scheduling, §6 links here): https://gist.github.com/felixfelix-bot/63a3af3678ce5a694a01818ec575ff8c

**Live state you can check if you're on the reference machine:** `curl -s http://localhost:9099/quota | python3 -m json.tool` (all provider lanes + burn predictions) · `curl -s http://localhost:9099/v1/models` (served models — note it's a curated stub, not the full registry). Production source of truth is `~/.hermes/bot/`; this repo is the published mirror (kept in sync deliberately — if they drift, the repo wins for docs, the live dir wins for behavior).

Questions go to Felix on Signal.