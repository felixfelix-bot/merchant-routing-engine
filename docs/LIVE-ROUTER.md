# LIVE ROUTER — canonical reference

> **Purpose.** This is the single, LLM-pointable description of how the Hermes
> **live (flat, market-based) router** is meant to work. If the running router is
> clobbered, regressed, or lost, point a fresh session at **this file** first, then
> the ADRs in [`docs/adr/`](adr/) (especially ADR-001…ADR-019). Keep this file
> committed to the repo; it is a recovery artifact, not a wiki page.
>
> **Invariants (do not violate).** Edit the **repo**, never only the live copy;
> deploy via **Ansible role 29** (`live-router`); make changes on a **`pr/…` branch**
> → PR → review; **never force-push**. Effective price is **always > 0** (ADR-004).

---

## 1. What it is

The live router decides, per request, **which provider/model lane to dial**, by
treating each lane as a supplier with a dynamic **price**. It always prefers the
**cheapest healthy** lane, where "price" encodes cost, scarcity, peak demand, and
**health** so that the market clears in a way that (a) keeps working, (b) does not
exhaust any subscription's quota early, and (c) does not waste quota that resets.

Two runtimes, one design:

| Layer | Live path | Canonical repo path |
|-------|-----------|---------------------|
| Proxy / dispatcher | `~/.hermes/bot/zai_proxy.py` | `scripts/engine/zai_proxy.py` |
| Flat router | `~/.hermes/bot/flat_router.py` | `scripts/engine/flat_router.py` |
| Pricing/Kalman modules | imported from `~/merchant-routing-engine/src` | `src/` (this repo) |

> The live `~/.hermes/bot/*.py` copies are **artifacts**. `scripts/engine/` in the
> orchestration repo and `src/` here are the sources of truth; role 29 deploys them.

---

## 2. The three prices (one per endpoint, per LLM)

| # | Price | Purpose | Owner |
|---|-------|---------|-------|
| 1 | **Routing price** | Internal market signal; the router minimizes it. Raised as **health deteriorates** and **×2 at peak**, to shed load and protect quota. | `pricing_engine.compute_effective_price` + `PriceKalman` + `realtime_pricing` (+ **HealthKalman**, ADR-015) |
| 2 | **Accounting cost** | Our true usage cost, for efficiency study & bookkeeping. | `cost_observer` → `real_price_tracker`, `consumption_kalman` |
| 3 | **Sale price** | What we charge customers on the flat router; set to **maximize profit**. | `margin_layer` (ADR-005 Layer 2) + `DemandKalman` + `pricing_exposure` |

**Self-charge ledger (dress rehearsal).** Treat the operator as an **infinite-money
customer** paying the **exposed sale price**: record `actual_tokens × exposed_price`
per request into a `sell_ledger`. This gives a notional-revenue **system-health
metric** and a **profitability** signal (revenue − accounting cost) ahead of selling
tokens on routstr. See **ADR-018**.

**Objective function (sale price).** `maximize Σ demand(p)·(p − c)` where `demand(p)`
comes from `DemandKalman` and `c` is upstream cost (`PriceKalman`). Genuinely **sunk**
costs are excluded; **recurring/amortized** fees (e.g. the z.ai friend key) are
**cost** and are included (see `pricing_exposure`: "z.ai is NOT free").

---

## 3. The Kalman filters

ADR-002 separates filters by concern; ADR-013 adds regime-shift alerting.

| Filter | State | Feeds |
|--------|-------|-------|
| `PriceKalman` | `[base_rate, velocity]` $/M | routing price (base component) |
| `ConsumptionKalman` | token burn rate | pace / target-pressure controller |
| `DemandKalman` | price→volume response | sale-price optimization (Layer 2) |
| **HealthKalman** *(new, ADR-015)* | per (endpoint, LLM) health/`p_error`/`p_exhaust` | routing price (health component) + gate |

**Health is predictive, not a boolean.** ADR-003 and ADR-008 originally moved
peak/scarcity/**health** out of Kalman into deterministic step functions; the
deprecated `price_kalman.health_factor` is the artifact. The fleet regression (429s
treated as `healthy`, "all providers exhausted") is fixed by **HealthKalman**: a 1-D
filter per (endpoint, LLM) that **bumps on failure and decays on probe success**.
Hard breakers remain only for **terminal** failures (401/403, paywall, 402).

---

## 4. Health gating & failure semantics

- `flat_router._is_provider_healthy(name)` = **HealthKalman gate** AND
  `_is_key_healthy(name)` (backoff / paywall / circuit breaker, resolved from
  `zai_proxy`).
- **Probe gate rule (ADR-016):** a fresh `HTTP 429` / `ok:false` from
  `provider_probe.json` marks a lane **routing-unhealthy** — *not* only when
  `healthy is False` (the old hysteresis bug let 429 lanes stay candidates).
- `+inf` price ⇒ **do not dial** (D-138). All-∞ is broken by
  `_reset_delivery_latches()` (D-140) so the next attempt dials fresh.
- Model fallback: if no lane for the primary model is deliverable,
  `model_fallbacks.json` selects the first mapped fallback with a finite lane.
- Per-request candidate budget `FLAT_ROUTER_CAND_BUDGET_S` bounds cycling time.

---

## 5. Quota windows, pacing, and the target-pressure controller

- **Windows** (z.ai: 5h × weekly × monthly; Ollama: 5h session + 7d weekly) are
  read as `{used_pct, resets_at, window_hours}` by `quota_window_extractor` /
  `ollama_quota_tracker`. `quota_pressure_factor` superimposes them (product of
  per-window exponential ramps; `hard_limit` ⇒ `+inf`).
- **Phase-sync (ADR-017):** every window must be anchored to its **authoritative
  `resets_at`** (`window_start = resets_at − window_seconds`). The old Ollama
  **rolling** windows (`now−5h`/`now−7d`) are out of phase and must be replaced.
  When the API omits `resets_at`, **infer/observe** the anchor (first request after a
  usage drop) and persist it.
- **Target-pressure controller (ADR-016):** instead of the heuristic `pace_factor`,
  numerically (bisection) solve for the multiplier `m` that makes **predicted
  depletion land at reset** (`minimize |t_exhaust(m) − t_reset|`), subject to
  `m ≥ cost_floor`. This is how we "use quota just before it resets" without
  exhausting early or leaving quota on the table. Publish `m` + rationale.

---

## 6. Request data flow (flat path)

```
request
  → zai_proxy (auth, model resolve, fallbacks)
  → flat_router.select_provider(model)
       ├─ candidate list (seed + measured rates)
       ├─ health gate  (_is_provider_healthy: HealthKalman + key health)
       ├─ price compare (effective_price = base × peak × scarcity × health × pace)
       └─ pick cheapest finite; skip +inf
  → dial; on success: cost_observer.update(actual_cost); realtime_pricing.snapshot
  → record: api_calls, routing_profit (savings), sell_ledger (self-charge)
  → on failure: HealthKalman bump; breaker/backoff; retry next candidate
```

---

## 7. Quality / safety gates

- **Garbage circuit-breaker** (per (provider, model)) and **canary** lane checks
  (ADR-014): a lane without a passing gate never enters candidacy.
- **Shadow mode** (ADR-006) validates changes without affecting live choice.
- **Regime-shift alerting** (ADR-013) flags when a lane's behaviour shifts.

---

## 8. Deployment & config

- Engine: **role 29 `live-router`** (writes `state/fleet/*.json` + engine copies).
- Shared runtime state: **`scripts/engine/router_state.py`** (role 29
  `router_engine_files`) is the single source of truth for provider funding,
  decayed delivery health, key backoff, and recovery counters. It must deploy
  **atomically** with `zai_proxy.py` + `flat_router.py` — a partial deploy makes
  `zai_proxy` `import router_state` fail (crash loop).
- Upstream timeout: **`ROUTER_UPSTREAM_TIMEOUT_S`** (default `60`s) bounds a
  pre-first-byte stall so a large-context prefill fails fast and is re-priced
  instead of hanging ~180s.
- Probe: **role 43 `provider-probe`** → `~/.hermes/bot/provider_probe.json` (the
  canonical path `flat_router._PROBE_PATH` reads).
- Pressure dispatch: **role 44** (planned) — CPU/mem/disk/gateway/LLM pressure.
- Key inputs: `providers.yaml`, `_SEED_RATES`, `measured_rates`,
  `model_fallbacks.json`, `kalman_pricing.json`, `egress_health_state.json`.

---

## 9. Recovery (if the router is clobbered)

1. Read **this file**, then ADR-001…ADR-019.
2. Confirm the source of truth: `git log` on `main`; the live files are artifacts.
3. Redeploy: run **role 29** (and 43) from the orchestration repo.
   - **Make the role current first:** if the node's orchestration checkout is on
     a stale branch, `router_engine_files` may omit `router_state.py` → the new
     `zai_proxy.py` crash-loops with `ModuleNotFoundError: router_state`
     (2026-09-15). Deploy from the canonical checkout (or `git pull` first).
   - **Split-brain check:** `~/.hermes/bot/router_state.py` exists **and**
     `zai_proxy._provider_health is router_state.provider_health` (one shared
     object, not a per-module copy). See fleet ADR-008.
4. Verify: probe fresh at `~/.hermes/bot/provider_probe.json`; `deepseek-flash` → 200;
   no all-∞; `kalman_pricing.json` updating.
5. If history was lost: restore from the ADR commits + `backup/*` tags/bundles
   (`git tag`, `*.bundle`) before re-deploying.

---

## 10. ADR index

- **ADR-001** price-first routing · **ADR-002** multi-Kalman separation ·
  **ADR-003** deterministic peak multiplier (peak only) · **ADR-004** effective-price
  positivity · **ADR-005** three-layer actor separation · **ADR-006** shadow mode ·
  **ADR-007** routster marketplace intelligence · **ADR-008** deterministic
  multipliers outside Kalman · **ADR-009** single authoritative default branch ·
  **ADR-010** binary artifacts via releases · **ADR-011** config-driven amortized seed
  pricing · **ADR-012** runtime learned-state history · **ADR-013** regime-shift
  Kalman alerting · **ADR-014** opportunistic transient lanes ·
  **ADR-015** per-(endpoint,LLM) health Kalman · **ADR-016** health-derived price +
  target-pressure controller · **ADR-017** quota phase-sync · **ADR-018** three-price
  model + self-charge · **ADR-019** compression cost/quality optimizer.

Fleet-side ADRs (dispatch/pressure/Ansible/live-router policy and the shared
router runtime state) live in `hermes-orchestration/docs/adr/` (ADR-001…008);
**fleet ADR-008** = shared router runtime state (single source of truth).
