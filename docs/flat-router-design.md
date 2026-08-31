# Flat Router Architecture Design
## Unified Kalman Price Discovery Across All Providers

**Author:** Hermes Agent (manager profile)  
**Date:** 2026-08-24  
**Status:** Phase 1 IMPLEMENTED — select_provider() running in shadow mode  
**Scope:** `~/.hermes/bot/zai_proxy.py` (~6190 lines) + `~/merchant-routing-engine/src/` + `~/.hermes/bot/flat_router.py`

---

## Table of Contents

1. [Current Architecture Analysis](#1-current-architecture-analysis)
2. [Proposed Flat Architecture](#2-proposed-flat-architecture)
3. [Provider Inventory](#3-provider-inventory)
4. [Specific Code Changes Needed](#4-specific-code-changes-needed)
5. [Migration Plan](#5-migration-plan)
6. [Skill Design: adding-api-key-to-live-router](#6-skill-design)

---

## 1. Current Architecture Analysis

### 1.1 Two-Tier System Overview

The proxy currently operates a **two-tier routing hierarchy**:

- **Tier 1 (Primary):** `best_key()` selects between the two z.ai keys (`ours` and `friend`) using Kalman burn-rate predictions and per-window lock thresholds. This is the default path for every request.
- **Tier 2 (Failover):** External providers (ollama_cloud, opencode_go, neuralwatt, deepinfra, ppq, openrouter, telnyx, routstr, routstrd) are only tried when both z.ai keys are exhausted (429/403/empty). The failover chain is hardcoded with a specific ordering.

z.ai keys get preferential treatment in every routing decision. External providers are second-class participants — they only see traffic when z.ai is completely unavailable.

### 1.2 Routing Function Map

#### `best_key()` (line 3733) — Primary z.ai Key Selection
- **Purpose:** Binary choice between `ours` and `friend` z.ai keys.
- **Phase 1 (Proactive):** Fetches Kalman burn-rate predictions via `_get_predictions()` for each key. Uses `_will_exhaust()` to identify which key will exhaust first. Picks the safer key.
- **Phase 2 (Reactive fallback):** When predictions unavailable, calls `_best_unlocked()` which checks per-window lock thresholds (`LOCK_THRESHOLDS` dict, line 480).
- **Phase 3 (Recovery):** If the non-chosen key has recovered below threshold, re-evaluates.
- **Phase 4 (Health gate):** Checks `_is_key_healthy()` for the chosen key. If unhealthy, tries the other. If both unhealthy, `chosen = None`.
- **Phase 5 (LiveRouter failover):** When `chosen is None`, calls `_consult_live_router()` to get an external provider. Kill switch: `.enable_live_routing` file flag.
- **Returns:** String key name ("ours", "friend", or an external provider name), or `None`.

**Tier-1 assumption:** This function ONLY considers z.ai keys in Phases 1-4. External providers only appear in Phase 5 as a last resort.

#### `_best_unlocked()` (line 3668) — Reactive Key Selection
- Operates exclusively on `quota_cache` for `ours` and `friend`.
- Checks `is_key_locked()` against `LOCK_THRESHOLDS` per window (5-hour, weekly, monthly).
- When neither locked, uses `_KEY_COST_MULTIPLIER` as tie-breaker (ours=1.0 vs friend=1.21).
- **Tier-1 assumption:** Hardcoded to only know about `ours` and `friend`.

#### `_consult_live_router()` (line 2170) — External Failover Consultation
- Calls `_LIVE_ROUTER.select_failover()` with quota_state, health_state, peak, pace_windows, failure_counts, task_type, model.
- Kill switch: `.enable_live_routing` file must exist.
- Returns `(provider, model, fallback_provider, fallback_model)` tuple.
- **Tier-2 assumption:** Only called when both z.ai keys are exhausted. Never called as a primary routing path.

#### `_try_external_failover()` (line 4452) — External Provider Chain
- Collects funded providers from `EXTERNAL_PROVIDERS` dict.
- Sorts by `_get_provider_cost()` (cheapest first), ties broken by failure_count.
- Honours a `preferred` parameter (LiveRouter's pick) by moving it to front.
- Iterates through candidates, forwarding requests to each provider's API.
- On 402 (out of credits), marks provider unfunded for 5 min, tries next.
- **Tier-2 assumption:** Only reached after z.ai keys fail. Hardcoded ordering bias.

#### `_try_ollama_cloud_any()` (line 4098) — Ollama Cloud Multi-Key Dispatcher
- Iterates over `_OLLAMA_CLOUD_KEYS` list (ollama_cloud, ollama_cloud_2).
- Each key tried in order; first success returns.

#### `_try_opencode_go()` (line 4114) — OpenCode Go Handler
- Flat-rate $10/mo provider. Direct API call to `opencode.ai/zen/go/v1`.
- Health-gated via `_is_key_healthy("opencode_go")`.
- Records spend, marks healthy on success.

#### `_try_telnyx()` (line 4250) — Telnyx Kimi Handler
- Kimi-model-only failover. Maps model names via `_PROVIDER_MODEL_NAMES["telnyx"]`.
- Uses production API or demo endpoint.

#### Request Handler `_proxy()` (line ~4760) — Main Request Flow
1. Extract model from request body.
2. Check for non-z.ai models (kimi-k3 direct to Telnyx, ollama-only models to ollama_cloud).
3. **Advisor mode** (if `.optimizer_advisor_mode` flag exists): consult `_routing_advisor.decide()` first. If it routes directly to ollama, try that. Otherwise use advisor's key. Fallback to `best_key()` on any failure.
4. **Original cascade** (flag off): peak-hour LiveRouter consultation → peak-hour Ollama pre-check → `best_key()`.
5. Shadow logging: records what the shadow optimizer WOULD have chosen alongside the live pick.
6. If `chosen is None`: consult LiveRouter → try ollama_cloud → try opencode_go → try external failover → 503.
7. If `chosen` is external (LiveRouter pick): route to appropriate handler. On failure, fall through to hardcoded chain.
8. If `chosen` is a z.ai key: build retry order `[chosen] + [other z.ai key]`, enter retry loop.
9. Retry loop: for each z.ai key, proxy to z.ai upstream. On 429/403, mark exhausted, try external failover. On empty response, try external failover.
10. Terminal fallback: after retry loop exhausts all keys, consult LiveRouter again → ollama → external failover → 503.

### 1.3 Hardcoded Tier-1/Tier-2 Assumptions

| Location | Line(s) | Assumption |
|---|---|---|
| `best_key()` Phase 1-4 | 3763-3847 | Only considers `ours` and `friend` keys. All external providers invisible. |
| `_best_unlocked()` | 3668-3730 | Hardcoded to `quota_cache["ours"]` and `quota_cache["friend"]` only. |
| `LOCK_THRESHOLDS` | 480-484 | Only `ours` and `friend` have lock thresholds. No external provider has per-window quota locking. |
| `_KEY_COST_MULTIPLIER` | 497 | Only `ours`, `friend`, `ollama_cloud`, `ollama_cloud_2`, `opencode_go`, `neuralwatt` have multipliers. Missing: deepinfra, ppq, openrouter, telnyx, routstr, routstrd. |
| `KEYS` dict | 476 | Only z.ai keys (`ours`, `friend`). External keys live in separate `_EXTERNAL_KEYS`. |
| Request handler | 5076 | `order = [chosen] + [n for n in KEYS if n != chosen]` — retry loop only tries z.ai keys. |
| Request handler | 4935-4967 | `chosen is None` branch: hardcoded ollama → opencode_go → external chain. Never considers z.ai keys here. |
| Request handler | 4974-4996 | `chosen not in KEYS` branch: external provider from LiveRouter, with hardcoded fallback chain. |
| `_consult_live_router()` | 2170-2221 | Named "failover" — only called when z.ai is exhausted. |
| `.enable_live_routing` flag | 261-263 | Kill switch for external routing. Implies external routing is optional/secondary. |
| Peak-hour pre-check | 4850-4854 | Ollama Cloud tried before `best_key()` during peak hours — but only ollama, not all providers. |
| Shadow optimizer comment | 1571-1576 | `zai_ours` was REMOVED from shadow set; comment says "friend-only policy" but live routing still prefers z.ai. |

### 1.4 Places Where z.ai Keys Get Preferential Treatment

1. **`best_key()` is the primary entry point** (line 3733). Every request starts by trying z.ai keys. External providers are only consulted when `best_key()` returns `None`.
2. **Retry loop only includes z.ai keys** (line 5076): `order = [chosen] + [n for n in KEYS if n != chosen]`. External providers are only tried as failover within the loop, not as retry candidates.
3. **Peak-hour Ollama pre-check** (line 4850): Only ollama_cloud gets a pre-check during peak hours. Other cheap externals (deepinfra at $1.30/M, ppq at $0.80/M) are not considered.
4. **`_KEY_COST_MULTIPLIER` tie-breaker** (line 497): `ours` (1.0) always beats `friend` (1.21) when both unlocked. No external provider costs are considered in this tie-break.
5. **Quota monitoring**: Only z.ai keys have `quota_cache` with real-time quota polling. External providers have balance bridges but these feed into `_snapshot_quota()` which is only consumed by LiveRouter (Tier 2).
6. **Kalman predictions**: Only z.ai keys have `_get_predictions()` / `_will_exhaust()` via the `/quota` endpoint. External providers rely on the shadow optimizer's `ConsumptionKalman` which is read-only.
7. **Shadow optimizer removed `zai_ours`** (line 1571) but kept `zai_friend` — the shadow set is incomplete and doesn't include `zai_ours` for comparison.
8. **Advisor mode** (line 4803): When advisor is enabled, it can route to ollama_cloud directly, but falls back to `best_key()` (z.ai only) on any failure.

### 1.5 Existing Kalman Filter Setup for z.ai Keys

**PriceKalman** (`~/merchant-routing-engine/src/price_kalman.py`):
- State: `[base_rate, velocity]` (2D constant-velocity model)
- Transition: `F = [[1, 1], [0, 1]]` (dt=1)
- Observation: `z = base_rate` (H = `[[1, 0]]`)
- Used in shadow optimizer only. Each provider gets a `PriceKalman` seeded with an initial $/M rate.
- `effective_price()` method combines: `base_rate × peak_mult × scarcity × health × pace_mult`

**ConsumptionKalman** (`~/merchant-routing-engine/src/consumption_kalman.py`):
- State: `[burn_rate, velocity, acceleration]` (3D constant-acceleration model)
- Predicts token burn rate and whether quota will exhaust within a horizon.
- Used in shadow optimizer only. `will_exhaust()` checks if remaining quota < predicted burn.

**z.ai-specific prediction** (in `zai_proxy.py`):
- `_get_predictions(key_name)` (line 1740): Cached wrapper that does a self-HTTP GET to `/quota` to get burn-rate predictions. Only works for z.ai keys (ours, friend).
- `_will_exhaust(predictions)` (line 1764): Returns the first window predicted to exhaust.
- These are **not** Kalman filters — they're heuristic predictions from the `/quota` endpoint's burn-rate data.
- The actual Kalman filters (PriceKalman + ConsumptionKalman) are in the shadow optimizer, which is **read-only**.

### 1.6 Shadow Optimizer (Read-Only, All Providers)

The shadow optimizer (`_shadow_optimizer`, line 1570) is a `RoutingOptimizer` instance that has ALL providers registered with their own `PriceKalman` and `ConsumptionKalman`:

| Provider | Seed $/M | model_tier | quota_remaining | peak_hours | peak_mult |
|---|---|---|---|---|---|
| zai_friend | 0.082 (0.068×1.21) | high | 1,000,000 | (6,10) | 3.0 |
| ollama_cloud | 0.40 | standard | 500,000 | none | 1.0 |
| ollama_cloud_2 | 0.40 | standard | 500,000 | none | 1.0 |
| opencode_go | 0.40 | standard | 500,000 | none | 1.0 |
| neuralwatt | 2.21 | standard | ∞ | none | 1.0 |
| ppq_external | 0.80 | low | 10,000,000 | none | 1.0 |
| deepinfra | 1.30 | low | balance×1M | none | 1.0 |
| telnyx | 5.40 | low | balance×1M | none | 1.0 |

**Note:** `zai_ours` was removed from the shadow set (line 1571) because the key was disabled Aug 15. If re-enabled for flat routing, it would need to be re-added.

**What it does:** `_shadow_optimizer.route(difficulty, estimated_tokens)` returns a dict with `chosen_provider`, `chosen_model`, `effective_cost_per_1m`, `reason`, `candidates`. It evaluates each provider through a pipeline:
1. Quality tier gate (model_tier >= required_rank)
2. Health gate (failure_count → graduated pricing: 1.0x → 1.5x → 3.0x → 10.0x → +inf)
3. Exhaustion gate (ConsumptionKalman.will_exhaust + remaining < estimated_tokens)
4. Scarcity multiplier (quota_used_pct → scarcity_factor)
5. Effective price = base_rate × peak_mult × scarcity × health × pace_mult

**Critical limitation:** The shadow optimizer's results are **logged but never used for routing**. It records what it WOULD have chosen alongside the live `best_key()` pick. The `ShadowLogger` writes to `routing_shadow_decisions` table for offline analysis.

**LiveRouter** (`_LIVE_ROUTER`, line 268-280): A `LiveRouter` instance from `src/live_router.py` that wraps `RoutingOptimizer` for live failover selection. Its `select_failover()` method builds a fresh optimizer with all providers + current quota/health state and returns the cheapest viable provider. However, it's **only called when both z.ai keys are exhausted** and requires the `.enable_live_routing` kill switch.

---

## 2. Proposed Flat Architecture

### 2.1 Core Principle

**All providers are equal.** The router picks the cheapest healthy provider that can serve the requested model. No tiers, no z.ai preference, no failover-only second class. Free-market price discovery across all providers via Kalman filters.

### 2.2 New Unified Routing Function

```python
def select_provider(
    model: str | None,
    task_type: str = "coding",
    estimated_tokens: int = 10000,
    difficulty: str = "medium",
) -> list[ProviderCandidate]:
    """Flat-hierarchy provider selection.

    Returns an ORDERED LIST of viable providers, cheapest first.
    The caller iterates the list: try each provider, on failure
    try the next. This replaces both best_key() and the failover chain.

    Each ProviderCandidate contains:
        - name: str (provider name)
        - model: str (model name to send to this provider)
        - effective_cost: float ($/M effective)
        - dispatch_fn: callable (the _try_* method to invoke)
        - reason: str (why this provider was chosen/ranked)

    Never returns empty list — if no provider is viable, returns
    [ProviderCandidate(name="fallback", model=..., cost=inf, ...)]
    so the caller can send a 503.

    Never raises — all failures produce the fallback candidate.
    """
```

**Key differences from `best_key()`:**
- Returns an **ordered list**, not a single key. The list IS the failover chain.
- Considers ALL providers, not just z.ai keys.
- Model matching is a first-class filter: only providers that can serve the requested model are candidates.
- Health gating excludes unhealthy providers before cost comparison.
- No separate "Tier 1" and "Tier 2" paths — one unified selection.

### 2.3 Per-Provider Kalman Filter

Each provider gets two Kalman filters (both already exist in the MRE codebase):

#### PriceKalman (existing, `src/price_kalman.py`)
- **State:** `[base_rate, velocity]` — smoothed $/M trend
- **Input:** Observed cost per million tokens from `_extract_cost()` after each request
- **Update:** Called after every successful request with the measured $/M
- **Output:** `base_rate` — the smoothed cost estimate used for routing

#### ConsumptionKalman (existing, `src/consumption_kalman.py`)
- **State:** `[burn_rate, velocity, acceleration]` — token consumption prediction
- **Input:** Per-period token consumption (from `_record_spend()`)
- **Update:** Called after every request with tokens consumed
- **Output:** `will_exhaust(remaining, horizon)` — predicts quota exhaustion

#### Per-Provider Kalman Inputs

| Input | Source | Update Frequency |
|---|---|---|
| Base cost ($/M) | `_extract_cost()` after each request | Per-request |
| Token burn rate | `_record_spend()` / `_parse_usage()` | Per-request |
| Quota remaining | `_snapshot_quota()` per provider | Per-request (cached 5 min) |
| Health (failure count) | `_zai_key_health` / `_provider_health` | Per-failure/success |
| Model availability | Static `_PROVIDER_MODEL_NAMES` + per-provider model lists | Startup/config |

### 2.4 Price Computation

```
effective_cost = base_rate × peak_mult × scarcity_factor × health_factor × pace_mult
```

This is the EXISTING formula from `RoutingOptimizer._evaluate_provider()` (line 267-362 of `routing_optimizer.py`). The flat router uses the same formula for ALL providers uniformly:

- **base_rate:** From `PriceKalman.predict()` — smoothed $/M for this provider
- **peak_mult:** 3.0 for z.ai during UTC 6-10, 1.0 for all others (per-provider `peak_hours_utc`)
- **scarcity_factor:** Ramps from 1.0 → ∞ as quota is consumed (from `quota_used_pct`)
- **health_factor:** Graduated pricing: 1.0x (0 failures) → 1.5x (1-2) → 3.0x (3-5) → 10.0x (6-10) → +inf (>10 or breaker)
- **pace_mult:** Per-provider pace multiplier from burn-rate windows (existing `_pace_windows`)

### 2.5 Flat-Rate vs Per-Token Cost Models

The Kalman filter needs a marginal cost signal. Different cost models require different approaches:

#### Per-token providers (z.ai, neuralwatt, deepinfra, ppq, openrouter, telnyx, routstr, routstrd)
- **Marginal cost:** Directly measured from API response (`_extract_cost()` → cost_usd / tokens × 1M)
- **PriceKalman input:** Measured $/M after each request
- **ConsumptionKalman input:** Token count from `_parse_usage()`
- **Scarcity:** Real quota depletion (used_pct from balance bridges)
- **Behavior:** Kalman filter naturally tracks cost changes (e.g., prompt-caching discounts, rate changes)

#### Flat-rate providers (opencode_go $10/mo, ollama_cloud included)
- **Marginal cost:** $0 per token (already paid via subscription)
- **PriceKalman input:** Seed with a small positive value (e.g., 0.001 $/M) to avoid division-by-zero and to give the filter a non-zero starting point. Update with 0.0 after each request — the Kalman will converge toward ~0.
- **ConsumptionKalman input:** Token count (still tracked for quota/session limits)
- **Scarcity:** Session quota usage (ollama_cloud has 500M token session limit; opencode_go has rate limits)
- **Key insight:** Flat-rate providers will ALWAYS be cheapest when their Kalman converges to ~0 $/M. This is CORRECT — they ARE cheapest. The scarcity factor protects them: as session quota fills, scarcity ramps their effective price up. When quota is exhausted (paywall), effective price → +inf.
- **Alternative:** Instead of seeding at 0.001, seed at the subscription-equivalent rate: `($10/mo) / (estimated_monthly_tokens)`. For opencode_go with ~50M tokens/mo: $10/50M = $0.20/M. This gives a more realistic "opportunity cost" signal. The Kalman converges to the true marginal cost ($0) over time as updates come in at $0/request.

**Recommendation:** Use subscription-equivalent seeding. This makes the flat router's price comparison meaningful: a flat-rate provider at $0.20/M seed is genuinely cheaper than ppq at $0.80/M but more expensive than a hypothetical $0.10/M provider. The Kalman converges to true marginal cost ($0) as data accumulates.

#### Included providers (ollama_cloud, ollama_cloud_2)
- Same as flat-rate, but cost is $0 (included with another subscription).
- Seed at $0.10/M (estimated value of the included tier).
- Scarcity from session/weekly quota tracker (`_get_ollama_quota_status()`).
- Paywall flag forces used_pct=100 → scarcity → +inf (existing behavior, preserved).

### 2.6 Model Matching

Each request specifies a model. The router only considers providers that can serve that model.

```python
# Provider model registry (new, replaces ad-hoc _PROVIDER_MODEL_NAMES)
PROVIDER_MODELS: dict[str, set[str]] = {
    "ours":          {"glm-5.2", "glm-5.3", "glm-4.5-flash", ...},
    "friend":        {"glm-5.2", "glm-5.3", "glm-4.5-flash", ...},
    "ollama_cloud":  {"glm-5.2", "kimi-k3:cloud", "kimi-k2.7-code", "gpt-oss:120b", ...},
    "ollama_cloud_2": {"glm-5.2", "kimi-k3:cloud", ...},
    "opencode_go":   {"glm-5.2", "glm-5.3", "kimi-k3", "deepseek-v4", ...},
    "neuralwatt":    {"glm-5.2", "deepseek-v4-flash", ...},
    "deepinfra":     {"deepseek-v4-pro", "deepseek-v4-flash", "glm-5.2", ...},
    "ppq":           {"glm-5.2", "kimi-k3", "deepseek-v4-flash", "deepseek-v4-pro", ...},
    "openrouter":    {"glm-5.2", "kimi-k3", "deepseek-v4-flash", "deepseek-v4-pro", ...},
    "telnyx":        {"kimi-k3", "kimi-k2.5", "gpt-5", "claude-haiku-4-5", ...},
    "routstr":       {"glm-5.2", "kimi-k3", "deepseek-v4-flash", ...},
    "routstrd":      {"glm-5.2", "kimi-k3", "deepseek-v4-flash", ...},
}
```

The router filters: `candidates = [p for p in all_providers if model in PROVIDER_MODELS[p.name]]`

For model name translation (e.g., `glm-5.2` → `deepseek-ai/DeepSeek-V4-Pro` on DeepInfra), the existing `_PROVIDER_MODEL_NAMES` dict is used when dispatching to the provider.

### 2.7 Health Gating

Before cost comparison, unhealthy providers are excluded:

1. **Manual disable:** `~/.hermes/bot/.key_disabled_<name>` file exists → excluded
2. **Ollama paywall:** `_ollama_paywall_active()` → excluded
3. **Circuit breaker:** `consecutive_failures > 10` or breaker tripped → excluded
4. **Backoff active:** `time.time() < retry_after` → excluded (temporary)
5. **Unfunded:** `_is_provider_funded()` returns False → excluded (5-min retry)
6. **NeuralWatt daily cap:** `is_daily_cap_exceeded` → excluded until UTC midnight
7. **Routstrd wallet exhausted:** `used_pct >= 100` → excluded

The existing `_is_key_healthy()` and `_is_provider_funded()` functions already handle most of these. The flat router unifies them into a single gate:

```python
def _is_provider_healthy(name: str) -> bool:
    """Unified health gate for ALL providers (replaces _is_key_healthy + _is_provider_funded)."""
    if _is_manually_disabled(name):
        return False
    if not _is_key_healthy(name):  # covers backoff, paywall, circuit breaker
        return False
    if name in EXTERNAL_PROVIDERS and not _is_provider_funded(name):
        return False
    return True
```

### 2.8 Fallback: Try Next Cheapest

The current failover chain is a hardcoded sequence: ollama → opencode_go → ppq → openrouter → deepinfra → telnyx → routstr → routstrd.

In the flat architecture, the failover chain IS the sorted candidate list from `select_provider()`. If the cheapest provider fails mid-request, the caller tries the next cheapest:

```python
candidates = select_provider(model=original_model, ...)

for candidate in candidates:
    if candidate.dispatch(self, body, candidate.model, response_buffer, t0):
        return  # success
    # failure — try next candidate
    _mark_key_failure(candidate.name, ...)

# all candidates failed
self.send_response(503)
```

This replaces:
- `best_key()` (single pick → retry loop)
- `_try_external_failover()` (external chain)
- `_try_ollama_cloud_any()` (ollama multi-key)
- The hardcoded `chosen is None` branch
- The `chosen not in KEYS` branch
- The terminal fallback chain

### 2.9 Shadow Optimizer Fate

**The shadow optimizer is promoted to the live router.** Here's how:

1. The `RoutingOptimizer` class already implements the flat routing logic: it evaluates all providers through the Kalman pipeline and returns the cheapest viable one. It already has `PriceKalman`, `ConsumptionKalman`, health gating, scarcity, peak hours — everything needed.

2. The shadow optimizer's providers are currently registered with **static seed values** and their Kalman filters are never updated with live data. The promotion involves:
   - **Making Kalman updates live:** After each request, update the chosen provider's `PriceKalman` with the measured $/M and `ConsumptionKalman` with the token count. This is the key change — the shadow optimizer becomes a LIVE optimizer.
   - **Refreshing quota/health per request:** Before each `route()` call, update each provider's `quota_remaining`, `failure_count`, and `breaker_tripped` from the live `_snapshot_quota()` and `_snapshot_health()` data.
   - **Adding model filtering:** The current `route()` uses `difficulty` → `model_tier` gating. The flat router adds explicit model-name filtering on top.
   - **Re-adding `zai_ours`:** If the `ours` key is re-enabled, add it back to the optimizer. If still disabled, leave it out.

3. The `LiveRouter` class (`src/live_router.py`) already wraps `RoutingOptimizer` with live quota/health injection. Its `select_primary()` method (line 1027) does exactly what we need — it calls `_do_select_failover()` which builds a fresh optimizer with live state. **The flat router is essentially `LiveRouter.select_primary()` promoted from canary to default.**

4. **Shadow logging is preserved** for observability: the shadow optimizer can continue logging "what it would have chosen" for comparison, but now it's the same as the live decision (agreement = 100%). More useful: log the full candidate list with prices so operators can see the market.

### 2.10 Architecture Diagram (Text)

```
Request arrives with model="glm-5.2"
    │
    ▼
select_provider(model="glm-5.2", task_type="coding")
    │
    ├── 1. Model filter: which providers can serve glm-5.2?
    │      → ours, friend, ollama_cloud, ollama_cloud_2, opencode_go,
    │        neuralwatt, deepinfra, ppq, openrouter, routstr, routstrd
    │
    ├── 2. Health gate: which are healthy right now?
    │      → (e.g., removes ollama_cloud if paywalled, ours if disabled)
    │
    ├── 3. Cost evaluation (per surviving provider):
    │      For each provider:
    │        effective_cost = PriceKalman.predict()   ← smoothed $/M
    │                       × peak_mult              ← z.ai peak hours only
    │                       × scarcity_factor         ← quota depletion
    │                       × health_factor           ← failure count
    │                       × pace_mult               ← burn rate
    │
    ├── 4. Sort cheapest first
    │      → [opencode_go $0.20, ollama_cloud_2 $0.20, ppq $0.80,
    │         deepinfra $1.30, neuralwatt $2.21, friend $0.082×3.0=$0.246, ...]
    │      (actual order depends on live Kalman state, peak hours, quota)
    │
    └── 5. Return ordered candidate list
           Each candidate has: name, model, dispatch_fn, effective_cost, reason

Caller iterates the list:
    for candidate in candidates:
        if candidate.dispatch(self, body, model, buffer, t0):
            update Kalman filters for this provider (cost, tokens)
            return  # success
        else:
            mark failure, try next
```

---

## 3. Provider Inventory

### 3.1 z.ai ours

| Field | Value |
|---|---|
| **Name** | `ours` |
| **API base URL** | `https://api.z.ai/api/coding/paas/v4` (constant `UPSTREAM`) |
| **Auth method** | Bearer token from `ZAI_OUR_KEY` env var |
| **Cost model** | Per-token (subscription) |
| **Effective $/M** | ~$0.068 (historical, from converged rates). Marginal cost $0 (subscription). |
| **Models available** | glm-5.2, glm-5.3, glm-4.5-flash, and all z.ai models |
| **Quota tracking** | Real-time via `quota_cache` — polls z.ai `/quota/limit` endpoint. 5-hour, weekly, monthly windows. 2M tokens per window. |
| **Health tracking** | `_zai_key_health["ours"]` — exponential backoff on 429/403. Manual disable via `.key_disabled_ours`. |
| **Kalman filter** | **No live Kalman.** Uses heuristic predictions via `_get_predictions("ours")` (self-HTTP to /quota). Shadow optimizer does NOT include `zai_ours` (removed line 1571). |
| **Cost multiplier** | 1.0 (`_KEY_COST_MULTIPLIER["ours"]`) |
| **Peak hours** | UTC 6-10, 3.0x multiplier |
| **Current status** | Key was disabled Aug 15, retired per friend-only policy. May be re-enabled. |

### 3.2 z.ai friend

| Field | Value |
|---|---|
| **Name** | `friend` |
| **API base URL** | `https://api.z.ai/api/coding/paas/v4` (constant `UPSTREAM`) |
| **Auth method** | Bearer token from `ZAI_API_KEY` env var |
| **Cost model** | Per-token (courtesy key, 21% premium) |
| **Effective $/M** | ~$0.082 (0.068 × 1.21). Marginal cost $0 (subscription). |
| **Models available** | Same as ours — all z.ai models |
| **Quota tracking** | Same as ours — `quota_cache["friend"]`, 5-hour/weekly/monthly windows, 2M tokens. |
| **Health tracking** | `_zai_key_health["friend"]` — same backoff system. Manual disable via `.key_disabled_friend`. |
| **Kalman filter** | **Shadow only.** `PriceKalman` seeded at 0.082, `ConsumptionKalman` tracking burn. Never updated with live data. |
| **Cost multiplier** | 1.21 (`_KEY_COST_MULTIPLIER["friend"]`) |
| **Peak hours** | UTC 6-10, 3.0x multiplier |
| **Lock thresholds** | 5h: 80%, weekly: 80%, monthly: 95% |

### 3.3 ollama_cloud

| Field | Value |
|---|---|
| **Name** | `ollama_cloud` |
| **API base URL** | Ollama Cloud API (key-specific endpoint) |
| **Auth method** | Bearer token from `OLLAMA_CLOUD_API_KEY` env var |
| **Cost model** | Included (free with $100/mo Ollama subscription) |
| **Effective $/M** | $0 marginal. Shadow seed: $0.40/M (subscription-equivalent). |
| **Models available** | glm-5.2, kimi-k3:cloud, kimi-k2.7-code, gpt-oss:120b, gemma4:31b, qwen3.5:397b, and all Ollama Cloud models |
| **Quota tracking** | `ollama_quota_tracker` — session limit 500M tokens, weekly limit. `_get_ollama_quota_status("ollama_cloud")`. 403 paywall flag: `.ollama_exhausted_until` (resets Monday UTC). |
| **Health tracking** | `_zai_key_health["ollama_cloud"]` + paywall flag. `_ollama_paywall_active("ollama_cloud")`. |
| **Kalman filter** | **Shadow only.** `PriceKalman` seeded at 0.40, `ConsumptionKalman` tracking. Never updated live. |
| **Cost multiplier** | 1.0 |
| **Peak hours** | None (flat rate) |

### 3.4 ollama_cloud_2

| Field | Value |
|---|---|
| **Name** | `ollama_cloud_2` |
| **API base URL** | Ollama Cloud API (second subscription) |
| **Auth method** | Bearer token from `OLLAMA_CLOUD_API_KEY_2` env var |
| **Cost model** | Included (free with second Ollama subscription) |
| **Effective $/M** | $0 marginal. Shadow seed: $0.40/M. |
| **Models available** | Same as ollama_cloud |
| **Quota tracking** | Same system, per-key: `_get_ollama_quota_status("ollama_cloud_2")`. Separate paywall flag: `.ollama_exhausted_until_2`. |
| **Health tracking** | `_zai_key_health["ollama_cloud_2"]` + per-key paywall flag. |
| **Kalman filter** | **Shadow only.** Seeded at 0.40. Separate `ConsumptionKalman`. |
| **Cost multiplier** | 1.0 |
| **Peak hours** | None |

### 3.5 opencode_go

| Field | Value |
|---|---|
| **Name** | `opencode_go` |
| **API base URL** | `https://opencode.ai/zen/go/v1` |
| **Auth method** | Bearer token from `OPENCODE_GO_API_KEY` env var |
| **Cost model** | Flat-rate $10/mo subscription |
| **Effective $/M** | $0 marginal. Shadow seed: $0.40/M. Subscription-equivalent: ~$0.20/M (if ~50M tokens/mo). |
| **Models available** | glm-5.2, glm-5.3 (native!), kimi-k3, deepseek-v4, and 29 models total |
| **Quota tracking** | No explicit quota. `_snapshot_quota()` returns `used_pct=0, remaining=inf, regime="included"`. Rate limits may apply but aren't tracked. |
| **Health tracking** | `_zai_key_health["opencode_go"]` — 429 marks exhausted, 401/403 marks dead after threshold. |
| **Kalman filter** | **Shadow only.** Seeded at 0.40. `ConsumptionKalman` with quota_remaining=500K (arbitrary). |
| **Cost multiplier** | 1.0 |
| **Peak hours** | None |
| **Special** | Serves glm-5.3 natively (unlike ollama_cloud which downgrades to 5.2). |

### 3.6 neuralwatt

| Field | Value |
|---|---|
| **Name** | `neuralwatt` |
| **API base URL** | `https://api.neuralwatt.com/v1` |
| **Auth method** | Bearer token from `NEURALWATT_API_KEY` env var |
| **Cost model** | Per-token. deepseek-v4-flash $0.14/M, prompt caching at $0.03/M. Blended ~$2.21/M (with glm-5.2). |
| **Effective $/M** | ~$2.21/M blended (shadow seed). Real cost varies by model + caching. |
| **Models available** | glm-5.2, deepseek-v4-flash, and NeuralWatt catalog models. Strips non-OpenAI fields (rejects reasoning/task_type/tier_hint). |
| **Quota tracking** | NeuralWatt balance bridge (`_neuralwatt_quota_entry_fn`). Real `/v1/quota` for energy allowance + lifetime cost. Daily cap guardrail: $10/day default. |
| **Health tracking** | `_is_key_healthy("neuralwatt")` + daily-cap check via `_snapshot_health()`. When `is_daily_cap_exceeded` → marked unhealthy until UTC midnight. |
| **Kalman filter** | **Shadow only.** Seeded at 2.21. `ConsumptionKalman` with quota_remaining=inf. |
| **Cost multiplier** | 1.0 |
| **Peak hours** | None |
| **Incident history** | $258 spend in one day (2026-08-22) — daily cap guardrail added as response. |

### 3.7 deepinfra

| Field | Value |
|---|---|
| **Name** | `deepinfra` |
| **API base URL** | `https://api.deepinfra.com/v1/openai` |
| **Auth method** | Bearer token from `DEEPINFRA_API_KEY` env var |
| **Cost model** | Per-token. ~$1.30/M. Prompt caching reduces effective cost. |
| **Effective $/M** | ~$1.30/M (shadow seed). Real cost from `usage.estimated_cost` in response. |
| **Models available** | deepseek-v4-pro, deepseek-v4-flash, glm-5.2 (via model name translation) |
| **Quota tracking** | Balance-based: `_get_deepinfra_balance()` × 1M tokens at $1.30/M. Starting balance from env var (default $5). `deepinfra_balance` in `_EXTERNAL_KEYS`. |
| **Health tracking** | `_is_provider_funded("deepinfra")` — 402 marks unfunded for 5 min. |
| **Kalman filter** | **Shadow only.** Seeded at 1.30. `ConsumptionKalman` with balance-derived quota. |
| **Cost multiplier** | 1.0 (not in `_KEY_COST_MULTIPLIER` — uses `_get_provider_cost()` directly) |
| **Peak hours** | None |
| **Model name mapping** | `deepseek/deepseek-v4-pro` → `deepseek-ai/DeepSeek-V4-Pro` (case-sensitive) |

### 3.8 ppq

| Field | Value |
|---|---|
| **Name** | `ppq` |
| **API base URL** | `https://api.ppq.ai/v1` |
| **Auth method** | Bearer token from `PPQ_API_KEY` env var |
| **Cost model** | Per-token. ~$0.80/M. |
| **Effective $/M** | ~$0.80/M (shadow seed). Real cost via multi-path probe in `cost_extraction`. |
| **Models available** | glm-5.2, kimi-k3, deepseek-v4-flash, deepseek-v4-pro |
| **Quota tracking** | PPQ credit balance bridge (`_ppq_quota_entry_fn`). Reads from `provider_balances` in `api_burn.db`. Real credit balance via `POST /credits/balance`. |
| **Health tracking** | `_is_key_healthy("ppq")` + PPQ gate: daily cap, hourly cap, retry-storm detection (`_ppq_gate_ok()`). |
| **Kalman filter** | **Shadow only.** Seeded at 0.80. `ConsumptionKalman` with quota_remaining=10M, total=20M. |
| **Cost multiplier** | Not in `_KEY_COST_MULTIPLIER`. Uses `_get_provider_cost()` with per-model rate tables. |
| **Peak hours** | None |
| **Special** | Good-use policy gate (`_ppq_gate_ok`, `_ppq_note_attempt`, `_ppq_hash_body`) prevents retry storms. |

### 3.9 openrouter

| Field | Value |
|---|---|
| **Name** | `openrouter` |
| **API base URL** | `https://openrouter.ai/api/v1` |
| **Auth method** | Bearer token from `OPENROUTER_API_KEY` env var. Extra headers: `HTTP-Referer`, `X-Title`. |
| **Cost model** | Per-token. Rates vary by model. |
| **Effective $/M** | From per-model rate tables (`_OPENROUTER_MODEL_RATES`). |
| **Models available** | glm-5.2, kimi-k3, deepseek-v4-flash, deepseek-v4-pro |
| **Quota tracking** | OpenRouter credit balance bridge (`_openrouter_quota_entry_fn`). Reads from `provider_balances`. |
| **Health tracking** | `_is_key_healthy("openrouter")` — currently hardcoded `True` in `_snapshot_health()` (line 1467). |
| **Kalman filter** | **None.** Not in shadow optimizer. No `ConsumptionKalman`. |
| **Cost multiplier** | Not in `_KEY_COST_MULTIPLIER`. Uses `_get_provider_cost()` with per-model rate tables. |
| **Peak hours** | None |

### 3.10 telnyx

| Field | Value |
|---|---|
| **Name** | `telnyx` |
| **API base URL** | `https://api.telnyx.com/v2/ai` (production) or `https://telnyx.com/api/inference` (demo) |
| **Auth method** | Bearer token (production) or browser-like Origin/Referer headers (demo, 10 req/min) |
| **Cost model** | Per-token. ~$5.40/M blended (kimi-k3: $2.70 input, $13.50 output). |
| **Effective $/M** | ~$5.40/M (shadow seed). Real cost from rate-derived calculation with prompt-caching discounts. |
| **Models available** | kimi-k3, kimi-k2.5, gpt-5, claude-haiku-4-5, minimax-m3 (Kimi-focused by operator decision) |
| **Quota tracking** | Telnyx balance bridge (`_telnyx_quota_entry_fn`). Starting balance from env var (default $10). |
| **Health tracking** | `_is_key_healthy("telnyx")` + `_is_provider_funded("telnyx")`. |
| **Kalman filter** | **Shadow only.** Seeded at 5.40. `ConsumptionKalman` with balance-derived quota. |
| **Cost multiplier** | Not in `_KEY_COST_MULTIPLIER`. Uses `_get_provider_cost()` with per-model rate tables + cache-aware blended rate. |
| **Peak hours** | None |
| **Special** | Kimi-only by operator decision (2026-08-20). Generic failover guard skips Telnyx for unmapped models. |

### 3.11 routstr

| Field | Value |
|---|---|
| **Name** | `routstr` |
| **API base URL** | `_EXTERNAL_KEYS.get("routstr_base", "http://23.182.128.51:8009") + "/v1"` |
| **Auth method** | Bearer token from `ROUTSTR_API_KEY` env var |
| **Cost model** | Per-token, Cashu-metered (sats-based) |
| **Effective $/M** | From measured rates (routstr_probe.py daily at 03:00) or catalog fetch. |
| **Models available** | Same model IDs as the proxy (identity mapping) |
| **Quota tracking** | Routstr sats balance bridge (`_routstr_quota_entry_fn`). |
| **Health tracking** | `_is_key_healthy("routstr")` + endpoint liveness probe (`_endpoint_alive()`). |
| **Kalman filter** | **None.** Not in shadow optimizer. |
| **Cost multiplier** | Not in `_KEY_COST_MULTIPLIER`. Uses `_get_provider_cost()` with measured/catalog rates. |
| **Peak hours** | None |

### 3.12 routstrd

| Field | Value |
|---|---|
| **Name** | `routstrd` |
| **API base URL** | `_EXTERNAL_KEYS.get("routstrd_base", "http://localhost:8008") + "/v1"` |
| **Auth method** | Bearer token from `ROUTSTRD_API_KEY` env var |
| **Cost model** | Per-token, Cashu-metered (buys from cheapest network node) |
| **Effective $/M** | From measured rates or network catalog. |
| **Models available** | Same model IDs as the proxy (network catalog) |
| **Quota tracking** | `_routstrd_balance_snapshot()` — 420s cache + last-known-good. Wallet sats balance. |
| **Health tracking** | `_is_key_healthy("routstrd")` + endpoint liveness probe + balance gate (`used_pct >= 100` → skip). |
| **Kalman filter** | **None.** Not in shadow optimizer. |
| **Cost multiplier** | Not in `_KEY_COST_MULTIPLIER`. Uses `_get_provider_cost()` with measured/catalog rates. |
| **Peak hours** | None |

---

## 4. Specific Code Changes Needed

### 4.1 Functions to Modify

| Function | Line | Action | Description |
|---|---|---|---|
| `best_key()` | 3733 | **Deprecate** | Keep as fallback for rollback, but `select_provider()` becomes the primary. Eventually remove. |
| `_best_unlocked()` | 3668 | **Deprecate** | Only used by `best_key()`. No longer needed in flat architecture. |
| `_consult_live_router()` | 2170 | **Repurpose** | Rename to `_consult_flat_router()`. Remove the "failover" framing. Called as primary, not as last resort. |
| `_try_external_failover()` | 4452 | **Modify** | Becomes a generic "try provider by name" dispatcher. The candidate list from `select_provider()` replaces the internal cost-sorting. |
| `_try_ollama_cloud_any()` | 4098 | **Keep** | Becomes a provider dispatch function, called by the unified loop when ollama_cloud is the candidate. |
| `_try_opencode_go()` | 4114 | **Keep** | Becomes a provider dispatch function. |
| `_try_telnyx()` | 4250 | **Keep** | Becomes a provider dispatch function. |
| `_proxy()` (request handler) | ~4760 | **Major rewrite** | Replace the advisor/peak/best_key/failover chain with a single `select_provider()` call + iteration loop. |
| `_snapshot_quota()` | 1380 | **Extend** | Add missing providers to the snapshot. Currently missing: deepinfra balance (already there), but needs all providers for the flat router. |
| `_snapshot_health()` | 1438 | **Extend** | Add missing providers. Currently `openrouter` is hardcoded True. |
| `_is_key_healthy()` | 956 | **Keep** | Works for all providers already. No change needed. |
| `_mark_key_failure()` | 984 | **Keep** | Works for all providers. No change needed. |
| `_mark_key_healthy()` | 1071 | **Keep** | Works for all providers. |
| `_get_provider_cost()` | 1150 | **Keep** | Still used by the flat router for cost lookups. PriceKalman supplements, not replaces. |
| `_extract_cost()` | 3240 | **Keep** | Feeds PriceKalman updates after each request. |
| `_record_spend()` | 2834 | **Keep** | Feeds ConsumptionKalman updates. |

### 4.2 Functions to Add

```python
# ── Flat Router (replaces best_key + failover chain) ─────────────────────

class ProviderCandidate:
    """One viable provider in the flat routing candidate list."""
    name: str           # provider name (e.g., "ppq", "ours", "ollama_cloud")
    model: str          # model name to send to this provider
    effective_cost: float  # $/M effective cost
    dispatch_fn: callable  # Handler._try_* method to invoke
    reason: str         # why this provider was chosen/ranked

def select_provider(
    model: str | None,
    task_type: str = "coding",
    estimated_tokens: int = 10000,
    difficulty: str = "medium",
) -> list[ProviderCandidate]:
    """Flat-hierarchy provider selection. Returns ordered candidate list."""
    # 1. Model filter
    # 2. Health gate
    # 3. Kalman cost evaluation (using promoted shadow optimizer / LiveRouter)
    # 4. Sort cheapest first
    # 5. Return candidate list with dispatch functions

def _update_kalman_after_request(
    provider: str,
    cost_usd: float | None,
    total_tokens: int,
) -> None:
    """Update the provider's PriceKalman + ConsumptionKalman after a request."""
    # PriceKalman.update(cost_usd / total_tokens * 1_000_000) if both available
    # ConsumptionKalman.update(total_tokens)

def _dispatch_to_provider(
    handler: Handler,
    name: str,
    body: bytes,
    model: str,
    response_buffer: bytearray,
    t0: float,
) -> bool:
    """Unified dispatch: call the right _try_* method for this provider."""
    # Maps provider name to dispatch function:
    #   ours/friend → z.ai upstream proxy
    #   ollama_cloud/ollama_cloud_2 → _try_ollama_cloud_any
    #   opencode_go → _try_opencode_go
    #   telnyx → _try_telnyx
    #   deepinfra/ppq/openrouter/routstr/routstrd → _try_external_failover (single provider)
    #   neuralwatt → _try_external_failover (single provider)
```

### 4.3 Functions to Remove (Eventually)

| Function | Line | Reason |
|---|---|---|
| `_best_unlocked()` | 3668 | Only used by `best_key()`. Flat router doesn't use per-window lock thresholds. |
| `LOCK_THRESHOLDS` | 480 | Per-window lock concept replaced by Kalman scarcity + health. |
| `_KEY_COST_MULTIPLIER` | 497 | Replaced by PriceKalman base_rate. Cost multipliers baked into seed values. |
| `.enable_live_routing` flag | 262 | No longer needed — flat routing is always live. Kill switch replaced by `.disable_flat_router` (revert to `best_key()`). |
| `.optimizer_advisor_mode` flag | 1650 | No longer needed — advisor IS the router now. |

### 4.4 Request Handler Rewrite

The current `_proxy()` method (lines ~4760-5360) has a complex flow:

```
Current: advisor? → peak pre-check → best_key() → shadow log →
         chosen is None? → LiveRouter → ollama → opencode → external → 503
         chosen is external? → route to handler → fallback chain → 503
         chosen is z.ai key? → retry loop [chosen, other] → external failover → 503
```

The flat router simplifies this to:

```
New:     select_provider(model) → candidate list
         shadow log (for observability)
         for candidate in candidates:
             dispatch → success? return
             failure? mark, try next
         503
```

**Estimated lines of change:**
- Request handler: ~600 lines removed, ~80 lines added = **net -520 lines**
- New `select_provider()` + `ProviderCandidate` + helpers: **~200 lines added**
- `_try_external_failover()` simplification: **~50 lines removed**
- Total: **net ~-370 lines** (the flat architecture is significantly simpler)

### 4.5 Shadow Optimizer Promotion

The shadow optimizer (`_shadow_optimizer`, line 1570) and LiveRouter (`_LIVE_ROUTER`, line 268) both already have the infrastructure. The promotion involves:

1. **Unify:** Use a single `RoutingOptimizer` instance (not separate shadow + live). The LiveRouter already wraps this.
2. **Live updates:** After each request, call `_update_kalman_after_request()` to update PriceKalman and ConsumptionKalman.
3. **Refresh state per request:** Before each `route()` call, refresh `quota_remaining`, `failure_count`, `breaker_tripped` from live snapshots.
4. **Add model filtering:** Extend `route()` or add a wrapper that filters by model availability.
5. **Re-add zai_ours:** If the key is re-enabled, add it back with its own Kalman.
6. **Add missing providers:** openrouter, routstr, routstrd are not in the shadow optimizer. Add them.
7. **Shadow logging:** Keep logging for observability, but it now logs agreement (100%) + full candidate list.

---

## 5. Migration Plan

### 5.1 Incremental Migration (Recommended)

The migration CAN be done incrementally. The existing kill switches and feature flags provide natural staging:

#### Phase 1: Add `select_provider()` alongside `best_key()` (no behavior change) — ✅ COMPLETE
- ✅ Implement `select_provider()`, `ProviderCandidate`, `_dispatch_to_provider()`, `_update_kalman_after_request()`.
- ✅ Add all providers to the optimizer (including missing: openrouter, routstr, routstrd).
- ✅ Add model registry (`PROVIDER_MODELS`) covering all 12 providers.
- ✅ Wire up Kalman live updates (available via `_update_kalman_after_request()`, not yet called in routing path).
- ✅ Add `_is_provider_healthy()` unified health gate.
- ✅ Shadow logging to `flat_router_shadow_decisions` table (comparison: best_key vs select_provider).
- ✅ Tests: 28 tests in `test_flat_router.py` — all passing.
- **No routing change:** `best_key()` still drives routing. `select_provider()` runs in shadow, logging its candidate list.
- **Test:** Compare `select_provider()` output vs `best_key()` decisions. Verify cost ordering is sensible.

#### Phase 2: Canary — route X% of traffic via `select_provider()`
- Add a canary flag: `.flat_router_canary` (similar to `.optimizer_advisor_mode`).
- When canary is active, route a percentage of requests through `select_provider()` instead of `best_key()`.
- Start at 1% and increase gradually.
- **Fallback:** Any `select_provider()` failure falls back to `best_key()` (existing path unchanged).
- **Test:** Monitor 503 rates, latency, cost per request, provider distribution.

#### Phase 3: Full cutover
- Remove canary flag logic. `select_provider()` is the primary router for ALL requests.
- `best_key()` becomes the fallback (called only if `select_provider()` returns empty list).
- **Test:** Full load test. Verify all providers are reachable. Check Kalman convergence.

#### Phase 4: Cleanup
- Remove `best_key()`, `_best_unlocked()`, `LOCK_THRESHOLDS`, `_KEY_COST_MULTIPLIER`.
- Remove `.enable_live_routing`, `.optimizer_advisor_mode` flags.
- Remove the hardcoded failover chain from `_proxy()`.
- Remove `best_key()` Phase 5 (LiveRouter consultation).
- **Test:** No regression. All routing through `select_provider()`.

### 5.2 Testing

#### Unit Tests
- `test_select_provider`: Verify model filtering, health gating, cost ordering.
- `test_kalman_live_update`: Verify PriceKalman and ConsumptionKalman update correctly after requests.
- `test_flat_rate_cost`: Verify flat-rate providers (opencode_go, ollama_cloud) have correct effective cost.
- `test_model_matching`: Verify only providers with the requested model are candidates.
- `test_health_exclusion`: Verify unhealthy providers are excluded.
- `test_fallback_chain`: Verify the candidate list serves as the failover chain.
- `test_rollback`: Verify `.disable_flat_router` reverts to `best_key()`.

#### Live Proxy Tests
- Send requests with different models (glm-5.2, kimi-k3, deepseek-v4-flash) and verify routing.
- Disable a provider (`.key_disabled_<name>`) and verify it's excluded from candidates.
- Verify cost ordering: cheapest provider should be tried first.
- Verify Kalman convergence: after N requests, check PriceKalman base_rate is tracking real cost.
- Verify shadow logging shows full candidate list with prices.
- Peak-hour test: during UTC 6-10, verify z.ai providers have 3x peak multiplier applied.

#### Integration Tests
- Full request cycle: model → select_provider → dispatch → response → Kalman update.
- Failover: cheapest provider fails → next cheapest succeeds.
- All providers fail → 503 with diagnostic headers.

### 5.3 Rollback Plan

**Primary rollback:** Create `.disable_flat_router` flag file.
- When this file exists, `_proxy()` uses `best_key()` + the existing failover chain (unchanged from current behavior).
- `select_provider()` continues running in shadow for observability but doesn't drive routing.
- This is a one-line check at the top of `_proxy()`:

```python
if os.path.exists(os.path.expanduser("~/.hermes/bot/.disable_flat_router")):
    chosen = best_key()  # original path
    # ... existing code unchanged
else:
    candidates = select_provider(...)  # flat router
    # ... new code
```

**Secondary rollback:** The code is structured so `select_provider()` is a separate function that can be deleted/short-circuited without affecting `best_key()`. All new code is additive in Phase 1.

**Emergency rollback:** Revert the git commit. The design ensures no data migration is needed — the Kalman filters are in-memory, the quota/health tables are unchanged.

### 5.4 Kill Switch Evolution

| Flag | Current Purpose | Flat Router Purpose |
|---|---|---|
| `.enable_live_routing` | Enable LiveRouter for failover | **Deprecated** — flat router is always live. Remove in Phase 4. |
| `.optimizer_advisor_mode` | Enable RoutingAdvisor | **Deprecated** — advisor IS the router. Remove in Phase 4. |
| `.disable_flat_router` | Does not exist | **New** — emergency rollback to `best_key()`. Created in Phase 2. |
| `.key_disabled_<name>` | Disable individual key | **Unchanged** — still works in flat router (health gate). |

---

## 6. Skill Design: adding-api-key-to-live-router

### Skill Name
`adding-api-key-to-live-router`

### Purpose
Documents the process of adding a new API provider to the flat routing architecture. Ensures the provider is a full equal participant in Kalman-based price discovery.

### The 11 Steps

#### Step 1: Add env vars
Add the provider's API key (and optional base URL / starting balance) to `~/.hermes/profiles/manager/.env`:
```
NEWPROVIDER_API_KEY=sk-xxxx
NEWPROVIDER_BASE=https://api.newprovider.com/v1   # optional, if non-standard
NEWPROVIDER_STARTING_BALANCE=10.0                  # optional, for balance-tracked providers
```

#### Step 2: Load the key in `_load_external_keys()`
Add a loader branch in `_load_external_keys()` (line 505) for the new env var:
```python
elif line.startswith("NEWPROVIDER_API_KEY=") and "newprovider" not in keys:
    keys["newprovider"] = line.split("=",1)[1].split("#")[0].strip().strip("'").strip('"')
```

#### Step 3: Register in `EXTERNAL_PROVIDERS` dict
Add the provider to the `EXTERNAL_PROVIDERS` dict (line 688):
```python
"newprovider": {
    "base_url": NEWPROVIDER_BASE,
    "key": NEWPROVIDER_KEY,
},
```

#### Step 4: Define the cost model
Determine the cost model and set the seed rate for the PriceKalman:
- **Per-token:** Set seed to the published $/M rate. Will converge to real cost via Kalman updates.
- **Flat-rate:** Set seed to subscription-equivalent $/M = `($monthly_cost) / (estimated_monthly_tokens)`.
- **Included:** Set seed to a small positive value (e.g., $0.10/M) representing opportunity cost.

#### Step 5: Register in the optimizer
Add the provider to the `RoutingOptimizer` (or `LiveRouter`) with its Kalman filters:
```python
optimizer.add_provider(
    "newprovider",
    PriceKalman(initial_rate=<seed_rate>),  # from Step 4
    ConsumptionKalman(),
    quota_remaining=<tokens_or_inf>,
    model_tier="<high|standard|low>",       # quality tier
    quota_total=<total_quota_or_None>,
    peak_hours_utc=None,                     # most providers have no peak
    peak_mult=1.0,
)
```

#### Step 6: Add to `PROVIDER_MODELS`
Register which models the provider can serve:
```python
PROVIDER_MODELS["newprovider"] = {"glm-5.2", "kimi-k3", "deepseek-v4-flash", ...}
```

#### Step 7: Add model name translation (if needed)
If the provider uses different model IDs, add to `_PROVIDER_MODEL_NAMES` (line 631):
```python
"newprovider": {
    "glm-5.2": "newprovider/glm-5.2",
    "kimi-k3": "moonshot/kimi-k3",
    ...
},
```

#### Step 8: Add balance tracking (if per-token)
Create a balance bridge (mirror the PPQ/OpenRouter/NeuralWatt pattern):
1. Add a collector entry in `src/balance_collectors.py` for the new provider.
2. Add a bridge import in `zai_proxy.py` (near line 290-350).
3. Add the provider to `_snapshot_quota()` (line 1380) and `_snapshot_health()` (line 1438).

#### Step 9: Add a `_try_*` dispatch function (or reuse existing)
If the provider has a standard OpenAI-compatible API (like most externals), no new function is needed — `_try_external_failover()` handles it via `EXTERNAL_PROVIDERS`. If it has special requirements (like ollama_cloud's paywall or opencode_go's native glm-5.3), create a `_try_newprovider()` method on the Handler class.

#### Step 10: Test
1. **Unit test:** Add the provider to test fixtures. Verify it appears in `select_provider()` output when healthy and is excluded when unhealthy.
2. **Live test:** Send a test request with a model the provider supports. Verify:
   - The provider appears in the candidate list with correct effective cost.
   - If it's the cheapest, the request routes to it.
   - After the request, the PriceKalman and ConsumptionKalman are updated.
   - The `api_calls` table logs the provider name and cost.
3. **Failover test:** Disable the provider (`.key_disabled_newprovider`). Verify it's excluded from candidates. Re-enable and verify it returns.

#### Step 11: Add health tracking
The provider automatically gets health tracking via `_zai_key_health` and `_is_key_healthy()`. Verify:
- 429 response → `_mark_key_exhausted("newprovider")` → exponential backoff.
- 401/403 → `_mark_key_dead("newprovider")` → 1h backoff.
- 402 → `_mark_unfunded("newprovider")` → 5-min retry.
- Success → `_mark_key_healthy("newprovider")` → reset.

### Provider Metadata Summary

Each provider needs:

| Metadata | Source | Example |
|---|---|---|
| API key | `.env` | `NEWPROVIDER_API_KEY=sk-xxx` |
| Base URL | `.env` or constant | `https://api.newprovider.com/v1` |
| Cost model | Manual determination | per-token / flat-rate / included |
| Seed $/M rate | Cost model → calculation | $0.80/M (per-token), $0.20/M (flat-rate equiv) |
| Models available | Provider's model catalog | `{"glm-5.2", "kimi-k3", ...}` |
| Model name mapping | Provider's API docs | `{"glm-5.2": "newprovider/glm-5.2"}` |
| Quota/balance tracking | Balance bridge or hardcoded | `used_pct`, `remaining`, `total` |
| Quality tier | Model quality assessment | high / standard / low |
| Peak hours | Provider's pricing model | None (most), (6,10) for z.ai |
| Health tracking | Automatic via `_zai_key_health` | backoff on 429/403/402 |

### Common Pitfalls

1. **Forgetting to add to `PROVIDER_MODELS`:** If a provider isn't in the model registry, `select_provider()` will never route to it, even if it's the cheapest. Always register the models the provider can serve.

2. **Wrong seed rate for flat-rate providers:** Seeding at $0 makes the Kalman filter numerically unstable (division by zero in scarcity calculations). Always seed at a small positive value (subscription-equivalent rate).

3. **Missing balance bridge for per-token providers:** Without a balance bridge, `_snapshot_quota()` returns `{used_pct: 0.0, remaining: inf}` — the provider looks like it has infinite quota. This means scarcity_factor is always 1.0 and the provider never gets price-penalized for depletion. Always add a balance bridge for per-token providers.

4. **Model name mismatch:** If the provider expects `deepseek-ai/DeepSeek-V4-Pro` but you register `deepseek/deepseek-v4-pro` in `_PROVIDER_MODEL_NAMES`, requests will 404. Test with a real request to verify model name translation.

5. **Not adding to `_snapshot_health()`:** If the provider isn't in the health snapshot, the optimizer assumes it's healthy even when it's not. This can route traffic to a dead provider. Always add to `_snapshot_health()`.

6. **Peak hours on non-z.ai providers:** Only z.ai has peak pricing (UTC 6-10, 3x). Setting peak_hours on other providers makes them artificially expensive during those hours, which is wrong — they don't charge more during z.ai peak.

7. **Forgetting Kalman live updates:** If you add a provider to the optimizer but don't call `_update_kalman_after_request()` after each request, the PriceKalman stays at its seed value forever. The cost estimate never improves. Always wire up the post-request Kalman update.

8. **Quality tier misclassification:** Setting a provider to "low" tier when it serves high-quality models means it won't be considered for "high" difficulty requests. Verify the quality tier matches the models the provider actually serves.

---

## Appendix A: Current Failover Chain (to be replaced)

```
Request → best_key()
  ├── returns "ours" or "friend" → proxy to z.ai → retry loop
  │     ├── z.ai success → return
  │     ├── z.ai 429/403 → _mark_key_exhausted → try next z.ai key
  │     ├── z.ai empty → _try_external_failover → return or try next
  │     └── all z.ai keys fail → _consult_live_router → _try_ollama_cloud_any
  │                            → _try_opencode_go → _try_external_failover → 503
  └── returns None (both exhausted)
        ├── _consult_live_router → route to external → fallback chain → 503
        └── _try_ollama_cloud_any → _try_opencode_go → _try_external_failover → 503
```

## Appendix B: Proposed Flat Router Flow

```
Request → select_provider(model, task_type)
  └── returns [candidate1, candidate2, candidate3, ...] (cheapest first)
        ├── try candidate1 → success? update Kalman, return
        ├── try candidate2 → success? update Kalman, return
        ├── try candidate3 → success? update Kalman, return
        ├── ...
        └── all candidates fail → 503
```

## Appendix C: Cost Comparison (Illustrative, Non-Peak)

| Provider | Seed $/M | Peak Mult | Scarcity (50% quota) | Health (0 failures) | Effective $/M |
|---|---|---|---|---|---|
| opencode_go | 0.20 | 1.0 | 1.0 | 1.0 | **$0.20** |
| ollama_cloud_2 | 0.10 | 1.0 | 1.5 | 1.0 | **$0.15** |
| ollama_cloud | 0.10 | 1.0 | 2.0 (75% quota) | 1.0 | **$0.20** |
| zai_friend | 0.082 | 1.0 | 1.2 | 1.0 | **$0.098** |
| zai_ours | 0.068 | 1.0 | 1.5 | 1.0 | **$0.102** |
| ppq | 0.80 | 1.0 | 1.0 | 1.0 | **$0.80** |
| deepinfra | 1.30 | 1.0 | 1.0 | 1.0 | **$1.30** |
| neuralwatt | 2.21 | 1.0 | 1.0 | 1.0 | **$2.21** |
| telnyx | 5.40 | 1.0 | 1.0 | 1.0 | **$5.40** |

During z.ai peak hours (UTC 6-10), z.ai providers get 3x peak_mult:
| zai_friend (peak) | 0.082 | 3.0 | 1.2 | 1.0 | **$0.295** |
| zai_ours (peak) | 0.068 | 3.0 | 1.5 | 1.0 | **$0.306** |

This is the free market in action: during peak hours, ollama_cloud and opencode_go become cheaper than z.ai, and the router naturally prefers them.

---

**End of Design Document**

Phase 1 implemented 2026-08-24. select_provider() running in shadow mode alongside best_key().