# Routstr Sell-Lane → Kalman Input Tracking — Design

**Status:** Design for review (design-only; no production files touched)
**Date:** 2026-09-08
**Grounding:** all claims cite the live tree as read this session
(`~/.hermes/bot/flat_router.py`, `zai_proxy.py`, `src/live_router.py`, `src/routing_optimizer.py`,
`src/price_kalman.py`, `src/consumption_kalman.py`, `src/demand_kalman.py`, `src/margin_layer.py`,
`src/real_price_tracker.py`, `scripts/api_burn_collector.py`, `merchant-routing-engine/docs/PLAN-routstr-serving-lane-2026-09-07.md`).

---

## 1. Ground truth: what feeds the Kalman filters today

There are **two Kalman families**, and conflating them is the source of most of the operator's confusion. Only one of them is in the routing path.

### 1a. The routing Kalmans (live engine — the ones that matter)

`src/live_router.py` — `LiveRouter` (the "flat router" primary, imported by `zai_proxy.py` as `_LIVE_ROUTER`) holds **one `PriceKalman` + one `ConsumptionKalman` per provider key** (L756–769). The **only** feed into them is `record_request(provider, tokens, cost_estimate=None)` (L1068–1099):

```python
if provider in self._consumption_kalmans:
    self._consumption_kalmans[provider].update(float(tokens))   # token-count observation
if cost_estimate is not None and provider in self._price_kalmans:
    self._price_kalmans[provider].update(float(cost_estimate))  # $/M observation (rare)
```

Every successful dispatch in `zai_proxy.py` calls it — always with **tokens only, never `cost_estimate`**:
- L5222 `record_request(provider=key_name, tokens=ollama_tokens)` (ollama_cloud path)
- L5540 `... provider="opencode_go", tokens=og_tokens)`
- L5727 `... provider="telnyx", tokens=telnyx_tokens)`
- L5842 / L6060 `... provider=provider_name, tokens=ext_tokens)` (external failover hops)
- L6420–6423 `... provider=_cand.name, tokens=_total_tokens ...` (flat-router candidate dispatch)

⇒ **The live engine's ConsumptionKalman is a *token-burn* filter fed by total routed load per provider key, with no class dimension.** The PriceKalman's live role is small: `record_request` never carries a measured $/M, so the per-provider `base_rate` used by `_get_effective_cost` (`flat_router.py` L770–840, reads `pk.predict()` at L818) stays close to seed until a scheduled refresh (`live_router.py` L908–915 `kalman.update(...)` from fresh rate maps). Measured $/M discovery lives in `real_price_tracker.py` (consumes `api_calls.cost_usd`) and is *not* fed back into routing Kalmans per request.

The second routing structure, `src/routing_optimizer.py` `RoutingOptimizer.route()` (used at `zai_proxy.py` L6561 in shadow logging), is a **read-only evaluator**: it calls `provider["consumption_kalman"].will_exhaust(...)` (L305–318) and `provider["price_kalman"].effective_price(...)` (L332) but never updates them — updates come only from `LiveRouter.record_request`. `flat_router.py`'s own `_update_kalman_after_request` (L981–1017) is **"not yet called in routing path"** per `docs/flat-router-design.md` L767 — it is a second, unused per-provider Kalman set (`_ALL_PROVIDERS`, L352–395) and must **not** be double-wired.

### 1b. The demand/margin Kalmans (ADR-005 — NOT wired)

`src/demand_kalman.py` (`DemandKalman`: slope/intercept demand curve) + `src/margin_layer.py` (`compute_optimal_price`, `optimal_price_linear`) exist as a marketplace Layer-2 stack. Grep shows **no import of `margin_layer` or `DemandKalman` anywhere in `zai_proxy.py`/`flat_router.py`/`live_router.py`** — it is library-only today. Any design that says "the demand Kalman prices routstr" is describing something that does not exist in the live path yet.

### 1c. The exhaust/pressure signal is NOT the routing Kalman

`predict_exhaustion` lives in `burn_predictor` (imported `zai_proxy.py` L2124, cached wrapper `_get_predictions` L2388+); `pressure_fsm.py` reads the `kalman_samples`/DB. `quota_state[key]` (incl. `quota_state['routstr']` from `src.balance_collectors.routstr_quota_entry`, `zai_proxy.py` L462–469) is the live scarcity input: `scarcity_factor(quota_used_pct)` (`price_kalman.py` L45–51) is applied on top of the Kalman base in `_get_effective_cost` (L794–834). So "pressure" = deterministic quota ramp + DB-derived exhaustion, *not* the in-memory token Kalman. This matters enormously for the design (§2/§4).

### 1d. Is there ANY caller-class distinction today? **No.**

`caller_class` appears in `zai_proxy.py`/`flat_router.py`/`live_router.py` **zero times** (grep-verified). It exists only in: the kanban task bodies (`scripts/create_routstr_tasks.py` T-A), the Phase-B **shadow canary** (`src/routstr_sold_canary.py`, `tag-sidecar.py` — logs to `routstr_sold_canary.jsonl` with `caller_class: "sold"` but **does not alter routing**), and `routing_gate1.py`'s decision log schema. The T-A caller-class hook (**thread `caller_class` from entry → `select_provider(...)`**) is **not implemented** — `select_provider(model, task_type, estimated_tokens, difficulty)` (L1022) has no such parameter. So today a sold routstr request is *indistinguishable* from internal at every Kalman/admission seam; the canary proves reachability only.

### 1e. The routstr provider rows

`zai_proxy.py` L914–925: `routstr` = "our own VPS2 routstr node — z.ai-backed upstream, Cashu-metered" (`http://23.182.128.51:8009/v1`) and `routstrd` = "local routstrd daemon — buys from cheapest network node via Cashu" (`http://localhost:8008/v1`). **These are buy-side providers in the same ladder as chutes/ppq/deepseek** — i.e., the paths *we* buy from when we are the *buyer*. They carry their own Bearer keys (`ROUTSTR_API_KEY`/`ROUTSTRD_API_KEY`, L688–695). `PROVIDER_MODELS` includes both (L251–259) with seed rates $1.00/M (L284–285).

---

## 2. The Kalman skew-risk model (precisely, per family)

The operator's two horns map to *different* mechanisms:

**(a) Sold traffic invisible → underpriced / exhaustion-blind.** Invisible *to routing Kalmans* it is NOT: sold requests dispatched through the buy-lane DO reach `_LIVE_ROUTER.record_request(provider, tokens)` (L6420) because dispatch is class-agnostic — so per-provider **burn** already counts sold load. What is invisible: (i) sold **revenue** and sold-specific **cost of goods** — nothing in the routing path records which tokens were resold or at what margin; and (ii) anything priced from the DemandKalman/margin layer, which is not wired at all. Underpricing risk is therefore a *merchant-margin* problem, not a routing-Kalman-burn problem.

**(b) Sold mixed into internal → over-priced / misrouted internal.** This is the **real, live hazard**, and it is not even about the Kalman *state*: it is about the shared **pressure inputs**. A routstr-sold request served from, say, the z.ai `ours` key consumes the *same* `quota_state['ours']`, the same `api_calls.key_name='ours'` rows, the same `predict_exhaustion('ours')` horizon. A 5-minute sold spike therefore:
  1. moves `quota_used_pct` up → `scarcity_factor` ramps (`price_kalman.py` L45–51) → `_get_effective_cost` raises **internal** effective cost for that lane (L809, L831–834), and
  2. shortens `predict_exhaustion` → `_apply_exhaust_weight` inflates cost (L1081) and pressure_fsm can trip AMBER/RED for **internal** service.
That is "sold spike trips internal pressure" — by design of a shared quota, not by Kalman cross-contamination. Whether that is *wrong* depends on whether the sold lane shares the physical upstream budget with internal (see §4): **if yes, the pressure response is correct and must NOT be separated; if the sell lane has its own upstream (Chutes PAYGO / routstrd Cashu), mixing is a double-count and must be separated.**

**(c) Does the code distinguish today?** No — neither the Kalman sets, the quota state, nor the api_calls schema (`task_type` exists but routstr requests are not reliably tagged at the router entry; `tag-sidecar` sets it only on the Phase-B hop). `api_burn_collector.py` polls `routstr` balance separately (L245–286) but writes to a *separate* `api_burn.db` `balance_snapshots` table — not into `quota_state` the routing engine uses, and not per-class.

---

## 3. Options evaluated

**(i) Fully separate Kalman instances per caller_class.** For *price/cost discovery* this is wrong: cost-of-goods is a property of the *upstream lane* (chutes vs z.ai vs routstrd), not of who is calling. Two classes buying from the same chutes key would then hold two diverging estimates of the same $/M — pure fragmentation, no information gain. For *burn*, separation is wrong when the budget is shared (see (b) above) because exhaustion must reflect *total* draw on the shared pool.

**(ii) Shared Kalman + class-weighted consumption, class-visible outputs.** This matches how the system already works for the buy-lane: one ConsumptionKalman per *provider key* that counts all routed load (correct when the key is shared) — *plus* a new **class-tagged observation ledger** so the same burn can be split into `internal` vs `sold` for *policy* (admission, margin, delisting) without splitting the *estimate*. Cost discovery stays single-instance per key because $/M is class-independent.

**(iii) No Kalman for sold traffic; fixed/separate formula.** Viable only for *pricing the sale* (a merchant margin formula over the buy-side Kalman output + fee multiplier 1.43 per the PLAN) — but it must still be *measured* against the shared burn Kalman for admission, otherwise the sell lane oversells shared quota. So (iii) is correct *for the sell price* but cannot replace (ii) *for load tracking*.

**Recommendation: (ii) — one shared, per-upstream-key Kalman family (status quo structure) + a class-observation ledger feeding class-aware admission/margin decisions, with the DemandKalman/margin layer wired ONLY for the routstr sell price.** This preserves the invariant that cost/burn estimates are physical (per upstream key) while policy is class-aware.

---

## 4. The critical design answer

**Q:** When sold traffic comes in, does the Kalman see it such that (a) prices are correct for total load and (b) internal pricing is not skewed by sold load?

**A (precise):** Split the question along the *physical budget* line, because that determines whether "seeing sold load" is correct or skewing:

- **Same upstream key as internal (e.g., sell lane served from the shared z.ai/`ours` key, or from routstrd Cashu pool shared with our own routstr buying):** sold load **should** feed the shared ConsumptionKalman and quota accounting — a sold spike genuinely accelerates depletion of the pool internal also uses; hiding it (option a) causes over-selling → real exhaustion → internal 503s. The correct "internal protection" is the **T-A admission gate** (sold requests 429 when `predict_exhaustion` hours < `SOLD_SAFETY_HOURS`, internal never 429s) — i.e. *admission by class, estimation shared*. Internal price levels are NOT skewed by sold load in the wrong direction: they rise *because the shared pool is genuinely emptier*, which is the correct scarcity signal.
- **Separate upstream for the sell lane (e.g., sell lane buys from Chutes PAYGO or a distinct routstrd key, internal never touches that budget):** then sold burn must be routed to the *separate* provider key's Kalman — which the current per-key structure already does naturally (record_request keys by provider). The failure to avoid is *mixing two different physical budgets under one key_name*; the fix is **one key_name per physical budget** (already true today) and **never aliasing two budgets onto one key**.
- **The true double-count risk (operator's failure mode):** the same physical token draw logged twice — once as the sold customer's upstream spend and once as the internal spend that bought it. With a resale chain (customer → our routstr node → our z.ai key → upstream), the *only* wallet-draining draw is our z.ai key; the customer-facing "spend" is revenue, not burn. Feeding customer *tokens* into the z.ai key's ConsumptionKalman (they ARE the same tokens, count once at the physical key) is correct; feeding *revenue-derived* quantities in is a double count. Rule: **count tokens once, at the physical upstream key; never count a resold request's tokens again under a synthetic "sold" key in the same Kalman family.**
- **predict_exhaustion() interaction:** it keys on `key_name`/quota state of the *physical* upstream (L2388+; pressure_fsm on `kalman_samples`). If sold and internal share the key, exhaustion already sees sold load via api_calls/quota_state — no change needed beyond the T-A gate that consumes it. If the sell lane has its own upstream, its key gets its own exhaustion horizon and the gate reads *that* key for sold admission. Either way: **exhaustion must be evaluated per physical key, then applied as a class-aware admission rule — never per class per key.**

**The recommended end-state wiring (one paragraph):** `record_request` keeps counting tokens once per physical provider key (no change to the Kalman core). A new thin **caller-class ledger** (columns: `ts, key_name, caller_class ∈ {internal,sold}, tokens, cost_usd`) is appended in the same finally-block where `_log_api_call`/`record_request` already fire (zai_proxy L6400–6425), sourced from the T-A `caller_class` value derived at entry from the routstr Bearer keys. The routing Kalmans stay shared per key (no per-class instances). Class-aware decisions — the sold 429 gate (`predict_exhaustion` on the physical key), the sell price (margin_layer `compute_optimal_price` over `DemandKalman` fed from sold-ledger (price, demand) pairs — the only place a per-class Kalman is justified), and the T-D delist trigger (sold-only burn-rate + variance) — read the ledger, never the shared Kalman state. Cancelled/stale calls are handled exactly as today: `_log_api_call` rows are written only on terminal responses; the ledger inherits that property (append-only on completion, `status_code=200` filter identical to `burn_predictor`'s own api_calls query at `burn_predictor_enhanced.py` L161).

---

## 5. Failure modes covered

| Failure mode | Mechanism today | Design response |
|---|---|---|
| 5-min sold spike trips internal pressure | Shared `quota_state` + `predict_exhaustion` on the physical key | **Correct if budget shared** — keep shared. Internal is protected by admission (429 sold, never internal), not by hiding load. If budget separate, sold load never enters internal key (per-key Kalman) — nothing to fix. |
| Same provider key double-counted | Only if one physical budget is aliased under two key_names or one request logs two draws | Rule: one key per physical budget; count tokens once at the physical key; ledger is observational, never fed back into burn Kalman. |
| Sold invisible → underpriced margin | No revenue/cost-of-goods split exists anywhere | Sold ledger + per-class cost aggregation; sell price via margin_layer over a *sold-demand* DemandKalman (only justifiable per-class Kalman) |
| `predict_exhaustion` misreads sold vs internal | It is per-key; class not in schema | Evaluate per physical key; apply as class-aware admission (T-A gate). Exhaustion horizons for a shared key must include sold (they do today via api_calls). |
| `_update_kalman_after_request` double-feed | Second unused Kalman set (`_ALL_PROVIDERS`) exists | Never wire it into routing alongside `LiveRouter.record_request` — one Kalman family per engine; document as dead or delete. |
| Cancelled/stale calls | Only terminal responses logged | Ledger inherits `_log_api_call` semantics; filter `status_code=200` like burn_predictor's own queries. |

---

## 6. Phased implementation plan (owner + gates)

Repo: `~/merchant-routing-engine` (production proxy `production/zai_proxy.py` mirrors `~/.hermes/bot`; router `flat_router.py`). Each task = kanban task with gates embedded at creation.

### Phase 1 — Class ledger (prerequisite for everything)
**Owner: worker-routing.** Extend `api_calls` (or a sibling `caller_class_ledger` table) with `caller_class`; populate at the existing `_log_api_call` finally-block (zai_proxy L6400) from the T-A value. **Depends on T-A caller-class hook landing** (T-A already threads `caller_class` into the decision path per PLAN §5).
**Deliverable:** every 200/logged call carries `caller_class`; zero routing change.
**Gates:** Gate 1 TDD (failing test: sold request row carries `caller_class='sold'`; internal `'internal'`), Gate 2 full `pytest` green + output pasted, Gate 2.5 independent cold review verdict in PR, Gate 3 docs updated same commit, Gate 4 atomic conventional commits **pushed to GitHub (hermes-bot repo) + `git status` empty + remote verified** (`git ls-remote`/`gh` diff check), Gate 5 clean tree, Gate 6 manager review before merge. Circuit breaker: 3 same-signature failures → BLOCKED report.

### Phase 2 — Sell-price DemandKalman + margin layer (wire ADR-005 for routstr only)
**Owner: worker-routing.** Feed `DemandKalman.update(price=charged, traffic=demand)` from the sold ledger per routstr listing; price sales with `margin_layer.compute_optimal_price(upstream_cost=shared-Kalman base_rate of the physical buy key, demand_kalman=...)` honoring the 1.43 fee-multiplier constraint from PLAN §2.
**Deliverable:** routstr listing price reacts to sold demand and to *measured* upstream cost — without touching internal routing Kalmans.
**Gates:** same table; extra Gate 3 doc `ROUTSTR-COMPETITIVE-PRICING.md` updated same commit.

### Phase 3 — Admission gate + delist wiring on the physical key
**Owner: worker-routing + worker-merchant.** T-A step 4 (sold 429 when `predict_exhaustion(physical_key) < SOLD_SAFETY_HOURS`), and T-D delist trigger reading sold-ledger burn-rate/variance on the shared key.
**Deliverable:** sold spikes shed load (429 + Retry-After) instead of silently raising internal scarcity; delist fires <5 min on burn acceleration (per ADR-007 gate 4).
**Gates:** same; stress test (`stress_test.py` already exists per PLAN T-C) must assert internal-never-blocked + sold-429 under 90% quota simulation.

### Phase 4 — Observability + decommissioning the second Kalman set
**Owner: worker-routing.** Reconcile `flat_router._update_kalman_after_request`/`_ALL_PROVIDERS` vs `LiveRouter.record_request`; document single-source-of-truth; class-split reports for margin.

**No fabricated work:** Phases 1–4 each leave a testable artifact (schema row, PR, gate verdict); nothing above rewires the shared per-key Kalman state.

---

## 7. What already exists vs what is missing (honest summary)

**Exists:** per-provider ConsumptionKalman burn tracking fed by all routed load (sold included) via `record_request` (live_router L1068, zai_proxy L6420); per-key quota/scarcity/exhaustion already reflects sold load when budgets are shared; shadow canary logging sold requests (Phase B); routstr/routstrd as distinct providers with own keys; api_burn_collector for routstr Cashu balance.

**Missing:** any `caller_class` in the routing/Kalman path (T-A unbuilt); any revenue/cost-of-goods class split; any wired DemandKalman/margin layer; the sold 429 admission gate; the delist trigger; and the decision (this design) on shared-vs-separate treatment of sold load per physical budget. The Kalman state itself is *not* the skew vector today — the shared quota/pressure inputs are — and the design's core move is: **keep Kalman estimation shared per physical upstream key, add class as an observation dimension, and make admission/margin class-aware.**
