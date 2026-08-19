# Plan: Per-Model Pricing — Fixing the kimi-k3 485× Cost Blindspot

## Metadata

| Field | Value |
|---|---|
| **Status** | DRAFT — awaiting Felix review |
| **Date** | 2026-08-05 |
| **Author** | Pricing systems consultant |
| **Related** | `docs/ADR-asymptote-pricing.md`, `src/live_router.py`, `src/pricing_engine.py`, `src/real_price_tracker.py`, `src/realtime_pricing.py` |
| **Supersedes** | — (extends, does not replace, the per-provider pricing model) |

---

## TL;DR

The routing optimizer compares providers by effective price, but every provider
has **one base rate regardless of which model is requested**. A provider serving
both `glm-5.2` ($0.0155/M) and `kimi-k3` ($7.53/M) reports a single blended rate
(~$0.024/M) for both. The optimizer cannot distinguish them — it sees kimi-k3 as
**313× cheaper than reality** and routes accordingly.

Per-model rate data **already exists** in `real_price_tracker.get_real_rate(provider, model)`
and `realtime_pricing.RateSnapshot.by_provider_model`, but the router's hot path
discards the model dimension before seeding the optimizer's PriceKalman filters.

**This plan wires per-model rates into the routing decision with a minimal,
backward-compatible change.** No rewrite, no new tables, no migration — the
infrastructure is already built, it just isn't connected.

---

## 1. Gap Analysis

### 1.1 Where Model Info Enters the System

```
Client request "generate with kimi-k3"
       │
       ▼
Production proxy (zai_proxy.py)
       │
       ├── model name extracted from request body
       │
       ▼
LiveRouter.select_failover(
    quota_state=...,
    health_state=...,
    peak=...,
    model="kimi-k3",      ← ◀ MODEL ENTERS HERE
)
       │
       ▼
_do_select_failover(model="kimi-k3")
       │
       ├── ✅ Used: _OLLAMA_EXCLUSIVE_MODELS short-circuit (line 1037)
       │      If model is kimi-k3:cloud → always route to ollama_cloud
       │
       └── ❌ LOST: everything else
              │
              ▼
         for name in self._provider_names:     ← iterates PROVIDERS, not models
              base_rate = effective_rates.get(name, ...)   ← PER-PROVIDER rate
              │
              ▼
         optimizer.add_provider(
             price_kalman=PriceKalman(
                 initial_rate=base_rate,   ← ◀ THE BLEND: kimi-k3's $7.53 invisible
             ),
             model=prov_model,   ← ◀ hardcoded "glm-5.2" for z.ai, "deepseek/..." for externals
         )
```

### 1.2 The Data Flow Diagram (Current — Per-Provider)

```
┌─────────────────────────────────────────────────────────────────────┐
│                     DATA SOURCES (per-model exists)                   │
│                                                                       │
│  real_price_tracker          realtime_pricing                         │
│  ┌─────────────────┐         ┌──────────────────────────┐            │
│  │ get_real_rate(   │         │ RateSnapshot             │            │
│  │   provider,      │         │   .by_provider_model:    │            │
│  │   model  ← ◀     │         │     (prov, model) → obs  │            │
│  │ )                │         │   .by_provider:          │            │
│  │                  │         │     prov → obs (blended) │            │
│  │ get_all_rates()  │         │                          │            │
│  │ → {prov:{model:  │         │ _kalmans:                │            │
│  │    rate}}        │         │   (prov,model)→ Kalman   │            │
│  └────────┬────────┘         └──────────┬───────────────┘            │
│           │                              │                            │
│           │  ◀ per-model data EXISTS     │  ◀ per-model kalmans EXIST │
│           │    but is NOT called         │    but NOT wired to router │
│           │    with model arg            │                            │
│           ▼                              ▼                            │
│ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ │
│                      THE GAP (model dimension dropped)                │
│ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ │
│           ▼                              ▼                            │
│  ┌────────────────────────────────────────────────────────┐           │
│  │              live_router.py  (PER-PROVIDER)             │           │
│  │                                                        │           │
│  │  _DEFAULT_CONVERGED_RATES = {provider: rate}  ← FLAT   │           │
│  │  _resolve_dynamic_base_rates() → {provider: rate} FLAT │           │
│  │  self._price_kalmans = {provider: PriceKalman}  ← FLAT │           │
│  │  self._base_rates = {provider: rate}            ← FLAT │           │
│  │  effective_rates (CPVO) = {provider: rate}      ← FLAT │           │
│  │                                                        │           │
│  │  _do_select_failover:                                   │           │
│  │    base_rate = effective_rates[name]  ← ONE rate/prov  │           │
│  │    optimizer.add_provider(PriceKalman(base_rate))       │           │
│  └────────────────────────────┬───────────────────────────┘           │
│                               ▼                                       │
│  ┌────────────────────────────────────────────────────────┐           │
│  │           routing_optimizer.py  (PER-PROVIDER)          │           │
│  │                                                        │           │
│  │  add_provider(name, price_kalman, ...)                  │           │
│  │  route() → sort by effective_price → cheapest viable    │           │
│  │                                                        │           │
│  │  CANNOT compare kimi-k3@ours vs kimi-k3@ppq             │           │
│  │  because it sees ours=$0.024 (blend) not $7.53 (kimi)   │           │
│  └────────────────────────────────────────────────────────┘           │
└─────────────────────────────────────────────────────────────────────┘
```

### 1.3 Concrete Example: The kimi-k3 Routing Error

**Scenario:** Client requests `kimi-k3`. Both `ours` (z.ai) and `ppq` serve it.

| What the optimizer sees (per-provider) | Reality (per-model) |
|---|---|
| `ours` base rate: **$0.024/M** (blended average) | `ours/kimi-k3`: **$7.53/M** |
| `ppq` base rate: **$0.14/M** | `ppq/kimi-k3`: **$7.53/M** (same model, same upstream) |
| **Optimizer picks:** `ours` (14× "cheaper") | **Should pick:** `ppq` (no real cost difference; `ours` z.ai doesn't actually serve kimi-k3 via subscription — it's an API passthrough at real cost) |

The optimizer makes a **485× wrong-cost estimation** for kimi-k3 traffic
because the blended rate hides the model-level cost.

### 1.4 What Needs to Change at Each Layer

| Layer | Current | Required | Change Size |
|---|---|---|---|
| `real_price_tracker.py` | Already supports `get_real_rate(provider, model)` | Add `get_all_trailing_rates_per_model()` — the per-model version of `get_all_trailing_rates()` | **Small** (new function, wraps existing query) |
| `realtime_pricing.py` | Already has `by_provider_model` dict and per-model kalmans | Add z.ai per-model amortization + spend-tier per-model breakdown | **Medium** (extend 2 collectors) |
| `live_router.py` `_resolve_dynamic_base_rates` | Returns `{provider: rate}` | Return `{provider: {model: rate}}` (nested) | **Small** (shape change + lookup helper) |
| `live_router.py` `_do_select_failover` | Seeds throwaway Kalman with `effective_rates[name]` | Seed with `effective_rates[name][model]` when model is known | **Small** (one lookup change) |
| `live_router.py` `self._price_kalmans` | `{provider: PriceKalman}` | Add per-model overlay: `{(provider, model): PriceKalman}` | **Medium** (new dict + refresh logic) |
| `routing_optimizer.py` | Receives one Kalman per provider | **No change needed** — still receives one Kalman per provider, but seeded with the right rate | **Zero** |
| `pricing_engine.py` | `quota_pressure_factor` operates on a scalar base_rate | **No change** — pressure is a multiplier; it applies on top of whatever base_rate it receives | **Zero** |

**Key insight:** The optimizer and pricing engine need **zero changes**. The fix
is entirely in how the router **looks up** the base rate before seeding the
throwaway PriceKalman. The model dimension enters at one point
(`select_failover(model=...)`) and needs to flow through to one lookup
(`base_rate = rates[name][model]`).

---

## 2. Architecture Options

### Option A: Nested Dict `{provider: {model: rate}}` — RECOMMENDED

**Shape:**
```python
converged_rates: dict[str, dict[str, float]] = {
    "ours": {
        "glm-5.2":         0.0143,
        "kimi-k2.7-code":  0.0209,
        "kimi-k3":         7.53,
        "_default":        0.0143,   # provider-level fallback
    },
    "ollama_cloud": {
        "glm-5.2":         0.0155,
        "kimi-k3:cloud":   7.53,
        "_default":        0.0155,
    },
    "ppq": {
        "kimi-k3":         7.53,
        "deepseek-v4-flash": 0.14,
        "_default":        0.14,
    },
    ...
}
```

**Lookup helper:**
```python
def _resolve_model_rate(
    rates: dict[str, dict[str, float]],
    provider: str,
    model: str | None,
) -> float:
    """Resolve (provider, model) → rate. Falls back to provider _default."""
    prov_rates = rates.get(provider, {})
    if model and model in prov_rates:
        return prov_rates[model]
    return prov_rates.get("_default", _DEFAULT_CONVERGED_RATES.get(provider, 0.001))
```

**Pros:**
| Criterion | Rating |
|---|---|
| Backward compatibility | ✅ Excellent — `rates[provider]["_default"]` replaces the old `rates[provider]` |
| Existing infrastructure alignment | ✅ `get_all_rates()` already returns this exact shape |
| Memory | ✅ ~6 providers × ~5 models = ~30 floats |
| Migration effort | ✅ Smallest — one lookup helper, one seed change |
| Debuggability | ✅ Easy to inspect: `print(rates["ours"])` shows all models |
| Testability | ✅ Easy to construct test fixtures |

**Cons:**
- Nested dict lookups are slightly more verbose than flat keys
- The `_default` key is a convention (not enforced by the type system)

**Why this fits:** `real_price_tracker.get_all_rates()` **already returns**
`{provider: {model: rate}}`. `realtime_pricing.RateSnapshot.by_provider_model`
already uses `(provider, model)` tuples. The nested dict is the natural shape of
the existing data. No translation layer needed.

---

### Option B: Flat Dict Keyed by `f"{provider}:{model}"`

**Shape:**
```python
converged_rates: dict[str, float] = {
    "ours:glm-5.2":        0.0143,
    "ours:kimi-k3":        7.53,
    "ours:_default":       0.0143,
    "ollama_cloud:kimi-k3:cloud": 7.53,
    ...
}
```

**Pros:**
| Criterion | Rating |
|---|---|
| Lookup simplicity | ✅ Single dict lookup: `rates[f"{provider}:{model}"]` |
| Memory | ✅ Same as Option A |
| Flat structure | ✅ Familiar to developers used to composite keys |

**Cons:**
| Criterion | Rating |
|---|---|
| Backward compatibility | ❌ Breaks — existing code expects `{provider: rate}` |
| Model names with colons | ⚠️ `kimi-k3:cloud` already contains a colon → ambiguous key |
| Existing infrastructure | ❌ `get_all_rates()` returns nested, would need translation |
| Grouping | ❌ Can't easily ask "what models does 'ours' serve?" without scanning all keys |
| Migration effort | ⚠️ Every consumer must change its key format |

**Verdict:** The colon-in-model-name collision (`kimi-k3:cloud`) is a dealbreaker.
The composite key would need a different separator, adding cognitive overhead.
Rejected.

---

### Option C: Per-Model PriceKalman Instances

**Shape:**
```python
self._price_kalmans: dict[tuple[str, str | None], PriceKalman] = {
    ("ours", "glm-5.2"):        PriceKalman(initial_rate=0.0143, ...),
    ("ours", "kimi-k3"):         PriceKalman(initial_rate=7.53, ...),
    ("ours", None):              PriceKalman(initial_rate=0.0143, ...),  # provider fallback
    ("ollama_cloud", "kimi-k3:cloud"): PriceKalman(initial_rate=7.53, ...),
    ...
}
```

**Pros:**
| Criterion | Rating |
|---|---|
| Accuracy | ✅ Highest — each model's Kalman converges independently |
| Smoothing | ✅ Per-model velocity tracking, per-model trend detection |
| Existing precedent | ✅ `realtime_pricing.RealtimePricing._kalmans` already uses this shape |

**Cons:**
| Criterion | Rating |
|---|---|
| Memory | ⚠️ ~30 PriceKalman instances × numpy arrays = ~30 × 200 bytes ≈ 6 KB (negligible) |
| Cold-start convergence | ⚠️ Each model's Kalman needs ~10 updates to converge; rare models stay noisy longer |
| Refresh complexity | ⚠️ The refresh thread must iterate (provider, model) pairs, not just providers |
| Implementation effort | ⚠️ Larger — changes to `__init__`, `refresh_base_rates`, `record_request`, `_do_select_failover` |

**Verdict:** This is the **long-term target** (the RealtimePricing singleton already
does this), but it's more change than necessary for the immediate fix. The router
currently seeds **throwaway** PriceKalmans for each routing decision anyway
(line 1265: `PriceKalman(initial_rate=base_rate, ...)` is constructed fresh every
call), so persistent per-model Kalman state in the router has limited value until
the router also tracks per-model consumption.

---

### Recommendation: Option A (Immediate) → Option C (Evolution)

```
Phase 1 (this plan):   Option A — nested dict lookup
                        ↓ wires per-model rates into routing decisions
                        ↓ zero optimizer changes, zero engine changes
                        ↓ KILL SWITCH: PER_MODEL_PRICING_ENABLED env var

Phase 2 (future):      Option C — persistent per-model PriceKalmans in the router
                        ↓ enables per-model Kalman convergence (trend, velocity)
                        ↓ unifies router kalmans with RealtimePricing kalmans
                        ↓ requires per-model ConsumptionKalman too (burn rate per model)
```

**Rationale:** The immediate problem is wrong routing decisions caused by the
blended rate. Option A fixes that with the smallest possible blast radius: one
lookup helper, one seed change, one kill switch. The optimizer and pricing engine
don't change at all. Phase 2 can follow when there's a need for per-model trend
detection (e.g., "kimi-k3's cost is rising 2% per week").

---

## 3. Provider-Specific Per-Model Rate Sources

Each provider type has a different cost data pipeline. Here's how per-model rates
are obtained (or computed) for each.

### 3.1 Source Matrix

| Provider | Cost Model | Per-Model Source | Available Now? | Cold-Start Strategy |
|---|---|---|---|---|
| **z.ai (ours)** | $300/yr flat subscription | Amortize by per-model token share | ⚠️ Needs per-model GROUP BY | Provider-level amortized rate × model weight |
| **z.ai (friend)** | Shared key, $0 direct | Same as ours × 1.21 premium | ⚠️ Same | Same × 1.21 |
| **Ollama Cloud** | $100/mo + $0.10/M extra | `activity.{model}.cost / .tokens` from API | ✅ Already parsed | Provider-level billing rate |
| **PPQ** | Pay-per-token | `ppq_queries` table GROUP BY model | ✅ Already parsed | Published list price per model |
| **DeepInfra** | Pay-per-token | `daily_spend` needs per-model breakdown | ❌ Currently provider-level only | Provider-level rate |
| **OpenRouter** | Pay-per-token | `daily_spend` needs per-model breakdown | ❌ Currently provider-level only | Published list price per model |

### 3.2 z.ai: Subscription Amortization Per Model

**Current:** `get_zai_amortized_rate(provider)` computes:
```
rate = $300 / (SUM(total_tokens for all models) / 1e6)
```
This gives a **blended** rate — glm-5.2 and kimi-k3 look the same.

**Per-model approach:** z.ai's subscription is a **shared resource** — all models
draw from the same $300/yr budget. The per-model rate is the subscription cost
allocated by each model's token share:

```
per_model_rate = $300 / (SUM(total_tokens_for_model) / 1e6)
```

Wait — that's wrong. If glm-5.2 uses 90% of tokens and kimi-k3 uses 10%, then:
- glm-5.2 allocated cost: $300 × 0.9 = $270 → $270 / (glm_tokens / 1e6)
- kimi-k3 allocated cost: $300 × 0.1 = $30 → $30 / (kimi_tokens / 1e6)

This gives the **amortized subscription cost per model** — how much of the $300
each model "consumed." But this is NOT the real marginal cost (which is $0 for
all models on a subscription).

**The key question for Felix:** Does the optimizer need the **subscription
amortization** (how much budget each model ate) or the **replacement cost**
(what it would cost to serve that model on a per-token provider)?

| Perspective | glm-5.2 | kimi-k3 | Use Case |
|---|---|---|---|
| **Subscription amortization** | $0.014/M (90% of $300 over 19B tok) | $0.014/M (same — it's shared) | "How much budget did each model consume?" |
| **Replacement cost** | $0.05–$0.14/M (DeepInfra/PPQ price) | $7.53/M (PPQ kimi-k3 price) | "What would we pay if we couldn't use the subscription?" |

**Recommendation:** Use **subscription amortization for the subscription
provider's own base rate** (it IS the cost of using that provider), but add a
**replacement-cost cross-check** so the optimizer knows when a subscription
model's real-world cost exceeds a paid alternative.

**Implementation:** Extend `_query_zai_window()` to GROUP BY model:

```sql
-- Current (provider-level):
SELECT COUNT(*), SUM(total_tokens), MIN(ts)
FROM api_calls WHERE key_name IN ('ours','friend') AND ts > ?

-- Per-model:
SELECT model, COUNT(*), SUM(total_tokens), MIN(ts)
FROM api_calls WHERE key_name IN ('ours','friend') AND ts > ?
GROUP BY key_name, model
```

Then compute per-model amortized rate:
```python
for model, count, tokens, min_ts in rows:
    model_share = tokens / total_provider_tokens   # fraction of provider's tokens
    model_budget = annual_budget * model_share      # allocated $ for this model
    model_rate = model_budget / (tokens / 1e6)      # $/M for this model
    rates[provider][model] = model_rate
```

**Note:** For a flat subscription, all models end up with the **same amortized
rate** (because `model_budget / model_tokens = (budget × share) / (total × share)
= budget / total`). The per-model rate is only different if models have different
real per-token costs — which is the case for kimi-k3 (API passthrough, not
subscription-served).

**Special case — kimi-k3 on z.ai:** kimi-k3 is served via z.ai's API but is NOT
part of the flat subscription — it's a metered model billed at real cost. The
`cost_usd` column in `api_calls` captures this. So `get_real_rate("ours",
"kimi-k3")` returns the **real per-token cost** ($7.53/M), not the amortized
subscription rate. This is the correct behavior — and it already works; we just
need to call it with the model argument.

### 3.3 Ollama Cloud: Per-Model from Billing API

**Already implemented.** `realtime_pricing._measure_ollama_billing()` (line 457)
parses the Ollama `/api/usage` response and produces per-model `RateObservation`
entries:

```python
for model_name, entry in activity.items():
    cost = entry.get("cost")
    tokens = entry.get("total_tokens")
    if req_count > 0 and cost > 0:   # extra-usage model
        extra_rate = cost / (tokens / 1e6)
        result[("ollama_cloud", model_name)] = RateObservation(
            rate_per_m=extra_rate, ...
        )
```

Similarly, `real_price_tracker._ollama_api_rate(model)` already supports
per-model lookup.

**What's missing:** The router never calls these with a model argument. The fix
is to call `get_trailing_rate("ollama_cloud", model)` instead of
`get_trailing_rate("ollama_cloud")`.

### 3.4 PPQ: Per-Model from Ledger

**Already implemented.** `realtime_pricing._measure_ppq_ledger()` (line 572)
groups by model:

```sql
SELECT model, SUM(cost_usd), SUM(total_tokens)
FROM ppq_queries WHERE ts > ?
GROUP BY model
```

Produces per-model `RateObservation` entries. The `real_price_tracker` layer
also supports per-model via `get_real_rate("ppq", model)`.

### 3.5 DeepInfra / OpenRouter: Per-Model from Spend Tables

**Currently provider-level only.** The `_measure_spend_tier()` function (line
658) queries `daily_spend` without a model breakdown:

```sql
-- Current:
SELECT SUM(spend_usd), SUM(token_count)
FROM daily_spend WHERE tier = ? AND date >= ?

-- Per-model (needs schema change):
SELECT model, SUM(spend_usd), SUM(token_count)
FROM daily_spend WHERE tier = ? AND date >= ?
GROUP BY model
```

**Blocker:** The `daily_spend` table may not have a `model` column. This needs
verification. If it doesn't, per-model rates for DeepInfra/OpenRouter require:
1. Adding a `model` column to `daily_spend` (migration)
2. Updating the spend collector to record per-model rows
3. Updating the query to GROUP BY model

**Fallback:** Until the schema supports per-model, use the published list price
per model from `config/providers.yaml` (which already has per-model
`cost_per_1m_input` / `cost_per_1m_output` for external providers).

### 3.6 Per-Model Rate Resolution Priority

For each `(provider, model)` pair, resolve the rate in this order:

```
1. Measured per-model rate
   (get_real_rate(provider, model) → cost_usd / tokens for this specific model)

2. API-sourced per-model rate
   (Ollama billing API, PPQ ledger — model-specific but not from our cost_usd)

3. Published list price per model
   (config/providers.yaml → models.{model}.cost_per_1m_input + output blended)

4. Provider-level measured rate
   (get_trailing_rate(provider) — the current behavior, blended across models)

5. Provider-level seed rate
   (SEED_RATES[provider] — cold-start fallback)

6. Conservative unknown fallback
   (UNKNOWN_PROVIDER_FALLBACK = $1.0/M — make unknown models look expensive)
```

---

## 4. Impact on Existing Systems

### 4.1 Quota Pressure Factor (RP-EXP)

**Current behavior:**
```python
base_rate = effective_rates[name]              # per-provider scalar
base_rate = base_rate * quota_pressure          # pressure multiplier
optimizer.add_provider(PriceKalman(initial_rate=base_rate))
```

**With per-model pricing:**
```python
model_rate = _resolve_model_rate(rates, name, model)  # per-model scalar
model_rate = model_rate * quota_pressure               # same multiplier
optimizer.add_provider(PriceKalman(initial_rate=model_rate))
```

**Impact:** **None.** The pressure factor is a **multiplier** — it operates on
whatever scalar base_rate it receives. Whether that scalar is per-provider or
per-model is transparent to the pressure formula. The RP-EXP curve
(`1 + K·t/(1-t)`) is model-agnostic.

**One subtlety:** The asymptote (1.5×) is the same for all models on a provider.
This is correct — the asymptote controls wall-approach gentleness, not cost
level. kimi-k3 at $7.53 × 1.5 = $11.30/M near the wall, which is still correctly
more expensive than glm-5.2 at $0.014 × 1.5 = $0.022/M. The relative ordering is
preserved.

### 4.2 Optimizer Endpoint Comparison

**Current behavior:** The optimizer registers one entry per provider, each with
a single base rate. It sorts by effective price and picks the cheapest viable.

```
ours:       $0.024/M  ← cheapest (blended)
ollama:     $0.024/M
ppq:        $0.14/M
```

**With per-model pricing:** The optimizer still registers one entry per provider,
but the base rate is now **the requested model's rate on that provider**.

```
Request: kimi-k3
ours:       $7.53/M   ← kimi-k3 on z.ai (API passthrough)
ollama:     $7.53/M   ← kimi-k3:cloud on Ollama (always extra-usage)
ppq:        $7.53/M   ← kimi-k3 on PPQ
deepinfra:  inf       ← doesn't serve kimi-k3 (filtered by tier or model availability)
```

**Impact:** The optimizer's comparison becomes **meaningful** — it compares
the same model's cost across providers, not a blended average. The optimizer
code itself (`route()`, `_evaluate_provider()`) needs **zero changes**.

**New requirement:** The router must know **which providers serve which models**.
Currently, `config/providers.yaml` lists models per provider, but the router
ignores this (it hardcodes `prov_model = "glm-5.2"` for z.ai providers). With
per-model pricing, the router should:
- Look up the model's rate for each provider
- If a provider doesn't serve the requested model, either skip it or set its
  rate to `inf` (unreachable for this model)

**Model availability matrix** (from `config/providers.yaml` model_map):

| Model | ours | friend | ollama_cloud | ppq | openrouter | deepinfra |
|---|---|---|---|---|---|---|
| glm-5.2 | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ |
| kimi-k2.7-code | ❌ | ❌ | ✅ (exclusive) | ❌ | ❌ | ❌ |
| kimi-k3 | ✅ (API) | ✅ (API) | ✅ (exclusive) | ✅ | ❌ | ❌ |
| deepseek-v4-flash | ❌ | ❌ | ❌ | ✅ | ✅ | ✅ |
| deepseek-v4-pro | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ |

### 4.3 Shadow Mode

**Current behavior:** `shadow_hook.py` logs routing decisions to
`routing_shadow_decisions` for comparison against the live proxy's key-rotation
logic.

**With per-model pricing:** Shadow decisions must log:
- The **requested model** (not just the provider)
- The **per-model base rate** used in the decision
- The **per-model effective price** for each candidate

**Schema addition** (to `routing_shadow_decisions` and `routing_live_decisions`):

```sql
ALTER TABLE routing_live_decisions ADD COLUMN requested_model TEXT;
ALTER TABLE routing_live_decisions ADD COLUMN per_model_base_rate REAL;
ALTER TABLE routing_live_decisions ADD COLUMN per_model_source TEXT;  -- 'measured'|'seed'|'fallback'
```

**Impact:** Logging changes only. The shadow comparison logic (divergence rate)
works the same — it just has richer data to explain *why* decisions diverge.

### 4.4 CPVO (Cost-Per-Viable-Output)

**Current:** `CPVOCalculator.get_effective_rates(base_rates)` takes
`{provider: rate}` and returns `{provider: rate}` adjusted by success rate.

**With per-model:** CPVO should adjust the per-model rate, not the provider-level
rate. A model with a low success rate on a specific provider should see its
per-model rate inflated.

**Change:** Extend CPVO to accept and return per-model rates:
```python
# Current:
def get_effective_rates(self, base_rates: dict[str, float]) -> dict[str, float]:

# Per-model:
def get_effective_rates_per_model(
    self, base_rates: dict[str, dict[str, float]]
) -> dict[str, dict[str, float]]:
```

**Impact:** Medium — CPVO queries `provider_telemetry` which would need a
`model` column (or a per-model telemetry table). This can be deferred to Phase 2;
Phase 1 applies CPVO at the provider level and then looks up the per-model rate.

### 4.5 Dynamic Rate Refresh Thread

**Current:** The background thread calls `refresh_base_rates()` every 30 minutes,
which calls `_resolve_dynamic_base_rates()` → `{provider: rate}` and feeds each
into the per-provider PriceKalman.

**With per-model:** The refresh must resolve rates for **every (provider, model)
pair**, not just every provider. The iteration changes from:

```python
# Current:
for name, rate in fresh.items():          # 6 providers
    self._base_rates[name] = rate
    self._price_kalmans[name].update(rate)

# Per-model:
for name, model_rates in fresh.items():   # 6 providers × N models
    self._base_rates_per_model[name] = model_rates
    for model, rate in model_rates.items():
        if model != "_default":
            self._price_kalmans_per_model[(name, model)].update(rate)
```

**Impact:** Small code change, but the refresh query count increases from 6 to
~30 (one per provider-model pair). With 5-minute caching this is negligible
(~30 indexed aggregate queries every 30 minutes).

---

## 5. Special Cases

### 5.1 New Model / Cold Start

**Problem:** A new model (e.g., `glm-6` just released) has no cost history.
`get_real_rate(provider, "glm-6")` returns `None` (insufficient data).

**Resolution chain:**
```
1. get_real_rate(provider, "glm-6")           → None (no data)
2. Published list price (providers.yaml)       → $X.XX/M if listed
3. Provider-level trailing rate                → blended rate (conservative)
4. Provider seed rate                          → SEED_RATES[provider]
5. UNKNOWN_MODEL_FALLBACK = $1.0/M             → make it look expensive
```

**Behavioral choice:** For a new model, **default to expensive** ($1.0/M) rather
than the provider blend. This prevents the optimizer from flooding traffic to an
unmeasured model that might be costly. As cost_usd accumulates, the rate
converges to the real value within ~100 calls (MIN_CALLS_FOR_RATE).

**Rationale:** The kimi-k3 bug happened precisely because an expensive model was
priced at the cheap provider blend. For new models, err on the side of
expensive-until-proven-cheap.

### 5.2 Models That Share Quota (z.ai)

**Problem:** z.ai's subscription quota (5h × weekly × monthly windows) is shared
across ALL models. The quota pressure factor operates on the **provider's** usage
fraction, not per-model. If glm-5.2 traffic exhausts the 5h window, kimi-k3
traffic is also blocked — they share the same pool.

**Solution:** The quota pressure factor remains **per-provider** (it reflects the
shared resource's depletion). The per-model change only affects the **base rate**.
The effective price is:

```
effective = per_model_base_rate × per_provider_quota_pressure × peak × health × pace
```

This is correct: the base rate reflects the model's cost, the pressure reflects
the shared quota's depletion. They are orthogonal dimensions.

**Visualization:**

```
                     Base Rate (per-model)     Quota Pressure (per-provider)
                     ─────────────────────     ────────────────────────────
glm-5.2 on ours:     $0.014/M                × 1.0 (fresh)     = $0.014/M
kimi-k3 on ours:     $7.53/M                 × 1.0 (fresh)     = $7.53/M
glm-5.2 on ours:     $0.014/M                × 2.5 (90% used)  = $0.035/M
kimi-k3 on ours:     $7.53/M                 × 2.5 (90% used)  = $18.83/M
```

The pressure multiplier is the same for both models (shared quota), but the
effective price correctly reflects each model's cost level.

### 5.3 Ollama-Exclusive Models

**Problem:** kimi-k3:cloud, gpt-oss:120b, etc. are ONLY served by ollama_cloud.
The current code short-circuits them to ollama_cloud regardless of price
(line 1037). With per-model pricing, this is still correct — no other provider
serves them.

**No change needed.** The exclusive-model short-circuit fires before the price
comparison. Per-model pricing doesn't affect it.

### 5.4 Fallback When Per-Model Data Is Insufficient

**Resolution:** The `_resolve_model_rate()` helper implements a strict fallback
chain (§3.6). If per-model data is insufficient, it falls back to:

1. Provider-level measured rate (the current behavior — safe, just less precise)
2. Provider seed rate
3. Conservative unknown-model fallback ($1.0/M)

**Logging:** Every fallback should be logged at INFO level so operators can see
which models are still running on imprecise rates:

```python
_log.info(
    "per-model-pricing: %s/%s using %s rate $%.6g/M "
    "(no per-model measured data; falling back to %s)",
    provider, model, source, rate, fallback_source,
)
```

### 5.5 Models with Different Names Across Providers

**Problem:** The same underlying model may have different names:
- `kimi-k3` on z.ai / PPQ
- `kimi-k3:cloud` on Ollama

**Solution:** Maintain a **model alias map** in `config/providers.yaml` or a
dedicated `src/model_aliases.py`:

```yaml
model_aliases:
  kimi-k3:
    - kimi-k3
    - kimi-k3:cloud
    - moonshot/kimi-k3        # OpenRouter naming
```

The rate lookup normalizes the model name through this map before querying.
**Phase 1 can skip this** (handle exact-name matching) and add it in Phase 2
when cross-provider model comparison becomes important.

---

## 6. Task Breakdown

### Dependency Graph

```
                    ┌─────────────────────────┐
                    │  T1: Per-Model Rate     │
                    │  Resolver Helper        │
                    │  (real_price_tracker)   │
                    └────────────┬────────────┘
                                 │
                    ┌────────────▼────────────┐
                    │  T2: Nested Rate Dict   │
                    │  in LiveRouter          │
                    │  (_base_rates_per_model)│
                    └────────────┬────────────┘
                                 │
                 ┌───────────────┼───────────────┐
                 │               │               │
    ┌────────────▼───┐  ┌───────▼────────┐  ┌──▼──────────────┐
    │ T3: Wire Model │  │ T4: z.ai Per-  │  │ T5: DeepInfra/  │
    │ into Failover  │  │ Model Amort.   │  │ OpenRouter Per- │
    │ (the key fix)  │  │ Query          │  │ Model Spend     │
    └────────────┬───┘  └───────┬────────┘  └──┬──────────────┘
                 │              │              │
                 └──────────────┼──────────────┘
                                │
                   ┌────────────▼────────────┐
                   │  T6: Shadow Mode        │
                   │  Per-Model Logging      │
                   └────────────┬────────────┘
                                │
                   ┌────────────▼────────────┐
                   │  T7: Integration Test   │
                   │  kimi-k3 Routing Fix    │
                   └────────────┬────────────┘
                                │
                   ┌────────────▼────────────┐
                   │  T8: Kill Switch +      │
                   │  Shadow Validation Gate │
                   └─────────────────────────┘
```

### Task Details

#### T1: Per-Model Rate Resolver Helper
**Depends on:** Nothing
**Files:** `src/real_price_tracker.py`
**Changes:**
- Add `get_all_trailing_rates_per_model()` — returns `dict[str, dict[str, float]]`
- Wraps the existing `get_all_rates()` query (already groups by provider+model)
- Adds seed/fallback logic per the §3.6 resolution chain
- Adds `_default` key per provider for the fallback rate

**Code sketch:**
```python
def get_all_trailing_rates_per_model(
    *, db_path: str | None = None, _now: float | None = None,
) -> dict[str, dict[str, float]]:
    """{provider: {model: $/M, '_default': $/M}} for every provider+model."""
    measured = get_all_rates(window_hours=168, db_path=db_path, _now=_now)  # already per-model
    result: dict[str, dict[str, float]] = {}
    for prov in PROVIDER_WINDOW_HOURS:
        prov_measured = measured.get(prov, {})
        prov_seed = SEED_RATES.get(prov, UNKNOWN_PROVIDER_FALLBACK)
        model_rates = {}
        for model, rate in prov_measured.items():
            if rate and rate > 0:
                model_rates[model] = rate
        model_rates["_default"] = prov_seed
        # Ensure at least the provider-level trailing rate is present
        provider_rate = get_trailing_rate(prov, db_path=db_path, _now=_now)
        if provider_rate and provider_rate > 0:
            model_rates["_default"] = provider_rate
        result[prov] = model_rates
    return result
```

**Gate:** Unit test: `get_all_trailing_rates_per_model()` returns nested dict with `_default` key for every provider. Cold-start providers return seed as `_default`.

---

#### T2: Nested Rate Dict in LiveRouter
**Depends on:** T1
**Files:** `src/live_router.py`
**Changes:**
- Add `self._base_rates_per_model: dict[str, dict[str, float]]` alongside existing `self._base_rates`
- Modify `_resolve_dynamic_base_rates()` to return nested dict when `PER_MODEL_PRICING_ENABLED` is on
- Add `_resolve_model_rate(rates, provider, model)` helper (§2, Option A)
- Modify `refresh_base_rates()` to refresh per-model rates

**Gate:** Unit test: `_resolve_model_rate()` returns per-model rate when available, `_default` when not, conservative fallback when provider unknown.

---

#### T3: Wire Model into Failover Path (THE KEY FIX)
**Depends on:** T2
**Files:** `src/live_router.py` (`_do_select_failover`)
**Changes:**
- When `model` is not None and `PER_MODEL_PRICING_ENABLED` is on:
  - Replace `base_rate = effective_rates.get(name, ...)` with
    `base_rate = _resolve_model_rate(self._base_rates_per_model, name, model)`
  - If the model is not served by this provider (not in rate dict, not `_default`-able),
    set `healthy = False` or use `inf` base rate (unreachable for this model)
- When `model` is None or kill switch is off: current behavior (backward compatible)

**Code sketch:**
```python
# In _do_select_failover, inside the provider loop:
if model and _PER_MODEL_PRICING_ENABLED:
    model_rate = _resolve_model_rate(
        self._base_rates_per_model, name, model
    )
    if model_rate >= UNKNOWN_MODEL_FALLBACK:
        # Model not served by this provider → make it unreachable
        healthy = False
    else:
        base_rate = model_rate
else:
    base_rate = float(effective_rates.get(name, ...))
```

**Gate:** Integration test: `select_failover(model="kimi-k3")` with per-model rates seeded — the optimizer sees $7.53/M for kimi-k3, not $0.024/M.

---

#### T4: z.ai Per-Model Amortization Query
**Depends on:** T1
**Files:** `src/real_price_tracker.py`, `src/realtime_pricing.py`
**Changes:**
- Add `_query_zai_window_per_model()` — GROUP BY `(key_name, model)`
- Compute per-model amortized rate for each z.ai model
- For metered models (kimi-k3 on z.ai): use `cost_usd`-based rate (already correct)
- For subscription models (glm-5.2 etc.): use amortized rate (shared budget × token share)

**Gate:** Unit test: `get_real_rate("ours", "kimi-k3")` returns ~$7.53/M (from cost_usd), while `get_real_rate("ours", "glm-5.2")` returns ~$0.014/M (amortized). The two rates differ by >100×.

---

#### T5: DeepInfra / OpenRouter Per-Model Spend
**Depends on:** T1
**Files:** `src/realtime_pricing.py`, schema migration
**Changes:**
- Check if `daily_spend` table has a `model` column
- If not: migration to add `model TEXT` column (backfill NULL → 'unknown')
- Update spend collector to record per-model rows
- Update `_measure_spend_tier()` to GROUP BY model
- **Fallback until migrated:** use published list prices from `providers.yaml`

**Gate:** Unit test: `get_real_rate("deepinfra", "deepseek-v4-flash")` returns per-model rate, not provider blend.

---

#### T6: Shadow Mode Per-Model Logging
**Depends on:** T3
**Files:** `src/shadow_hook.py`, `src/live_router.py` (properties)
**Changes:**
- Add `requested_model`, `per_model_base_rate`, `per_model_source` columns to decision tables
- Expose `last_requested_model`, `last_per_model_rates` properties from LiveRouter
- Log per-model rates for each candidate in the routing decision

**Gate:** Unit test: after `select_failover(model="kimi-k3")`, the decision log contains `requested_model="kimi-k3"` and `per_model_base_rate=7.53` for the chosen provider.

---

#### T7: Integration Test — kimi-k3 Routing Fix
**Depends on:** T3, T4
**Files:** `tests/test_per_model_pricing.py` (new)
**Test cases:**
1. **kimi-k3 on ours vs ppq:** both serve kimi-k3 at ~$7.53/M. With per-model pricing, the optimizer sees the real cost on both and picks based on pressure/health, not a fake 14× cost difference.
2. **glm-5.2 on ours vs ollama:** glm-5.2 at $0.014/M (ours) vs $0.0155/M (ollama). The optimizer still picks ours (cheapest) — no regression.
3. **Unknown model cold start:** `select_failover(model="new-model-x")` — rate resolves to $1.0/M (conservative), optimizer treats it as expensive.
4. **Kill switch off:** with `PER_MODEL_PRICING_ENABLED=false`, behavior is identical to current (per-provider rates, kimi-k3 at blended $0.024/M).

**Gate:** All 4 test cases pass. The kimi-k3 case shows the optimizer comparing $7.53 vs $7.53 (correct), not $0.024 vs $0.14 (wrong).

---

#### T8: Kill Switch + Shadow Validation Gate
**Depends on:** T7
**Files:** `src/live_router.py`
**Changes:**
- Add `_PER_MODEL_PRICING_ENABLED` env var (default: `false`)
- Shadow validation: run per-model pricing in shadow mode for 48h, compare routing decisions against current per-provider pricing
- Exit criteria: <5% divergence on glm-5.2 traffic (should be ~0%), >50% divergence on kimi-k3 traffic (expected — this is the fix)

**Gate:** Shadow mode runs for 48h. Divergence report shows kimi-k3 decisions changing (expected) and glm-5.2 decisions stable (regression check).

---

### Summary Table

| Task | Depends On | Est. Effort | Risk | Gate |
|---|---|---|---|---|
| T1: Per-model resolver | — | 2h | Low | Nested dict returned with `_default` |
| T2: Nested dict in router | T1 | 3h | Low | Lookup helper resolves correctly |
| T3: Wire model into failover | T2 | 4h | **Medium** (hot path change) | kimi-k3 sees $7.53/M in optimizer |
| T4: z.ai per-model query | T1 | 3h | Low | kimi-k3@ours = $7.53, glm-5.2@ours = $0.014 |
| T5: DeepInfra/OpenRouter per-model | T1 | 4h | Medium (schema) | Per-model rate returned |
| T6: Shadow mode logging | T3 | 2h | Low | Decision log has model column |
| T7: Integration test | T3, T4 | 3h | Low | 4 test cases pass |
| T8: Kill switch + shadow gate | T7 | 2h | Low | 48h shadow run, divergence report |

**Total estimated effort:** ~23 hours (3 focused days)

---

## 7. The kimi-k3 Cost Table (The Why)

This table makes the problem visceral. These are real measured rates from the
production system:

| Model | Real $/M | Current Router Sees | Error Factor |
|---|---|---|---|
| glm-5.2 | $0.0155 | $0.024 (blend) | 1.5× overpriced (minor) |
| kimi-k2.7-code | $0.0209 | $0.024 (blend) | 1.1× overpriced (minor) |
| **kimi-k3** | **$7.53** | **$0.024 (blend)** | **313× underpriced** |
| deepseek-v4-flash | $0.14 | $0.14 (list) | ✅ correct |
| gpt-oss:120b | $0.46 (extra-usage) | $0.024 (blend) | 19× underpriced |

The kimi-k3 row is the bug. At $7.53/M, routing 1M tokens of kimi-k3 to a
"cheap" provider (because the optimizer thinks it's $0.024/M) costs **$7.50 in
real money** that the optimizer didn't account for. Over a month of heavy
kimi-k3 usage, this could be **hundreds of dollars in unexpected charges**.

### The 485× in the Title

The task says "485× cost blindspot." The 485× comes from comparing kimi-k3's
real rate ($7.53/M) against glm-5.2's real rate ($0.0155/M):

```
7.53 / 0.0155 = 485.8×
```

The optimizer currently can't see this difference — both models show up as
$0.024/M on the same provider. Per-model pricing makes the 485× cost spread
visible to the routing decision.

---

## 8. Migration & Rollback

### Rollout Strategy

1. **Kill switch off (default):** `PER_MODEL_PRICING_ENABLED=false`. Zero behavior
   change. Ship T1–T3 behind the switch.

2. **Shadow mode (48h):** Enable per-model pricing in shadow mode only. Log
   decisions. Compare against live (per-provider) decisions. Verify kimi-k3
   decisions change (expected) and glm-5.2 decisions don't (regression check).

3. **Live enable:** `PER_MODEL_PRICING_ENABLED=true`. Monitor for 24h. The
   routing_live_decisions table shows per-model base rates for every decision.

### Rollback

Set `PER_MODEL_PRICING_ENABLED=false`. Instant revert to per-provider pricing.
No data migration needed — the per-model rate dict is built fresh on each
refresh; disabling the switch just stops using it.

---

## Appendix A: File Change Impact Summary

| File | Change Type | Lines Changed (est.) | Risk |
|---|---|---|---|
| `src/real_price_tracker.py` | Add `get_all_trailing_rates_per_model()` | +40 | Low (new function) |
| `src/live_router.py` | Add per-model dict, resolver, wire into failover | +60, ~10 modified | Medium (hot path) |
| `src/realtime_pricing.py` | Extend z.ai + spend collectors for per-model | +50 | Low (collector changes) |
| `src/routing_optimizer.py` | **No changes** | 0 | — |
| `src/pricing_engine.py` | **No changes** | 0 | — |
| `src/price_kalman.py` | **No changes** | 0 | — |
| `config/providers.yaml` | Optional: add per-model seed rates | +20 | Low |
| `tests/test_per_model_pricing.py` | New test file | +200 | — |

**Total:** ~370 lines added/modified across 3–4 files. Zero changes to the
optimizer, pricing engine, or Kalman filter.

---

## Appendix B: The Lookup Helper (Reference Implementation)

```python
#: Conservative rate for a model with no data at all. Set HIGH so the
#: optimizer never preferentially routes to an unknown "cheap" model.
UNKNOWN_MODEL_FALLBACK: float = 1.0  # $/M


def _resolve_model_rate(
    rates: dict[str, dict[str, float]],
    provider: str,
    model: str | None,
    provider_default: float = 0.001,
) -> float:
    """Resolve (provider, model) → $/M rate with full fallback chain.

    Resolution order (§3.6):
        1. Per-model measured rate (if model is in rates[provider])
        2. Provider-level default (rates[provider]["_default"])
        3. Hardcoded provider default (provider_default arg)
        4. UNKNOWN_MODEL_FALLBACK ($1.0/M — expensive for unknowns)

    When model is None, returns the provider-level default directly
    (backward-compatible with the per-provider pricing path).

    Never raises. Always returns a positive float.
    """
    try:
        prov_rates = rates.get(provider, {})
        if model is not None and model in prov_rates:
            rate = prov_rates[model]
        else:
            rate = prov_rates.get("_default", provider_default)
        if rate is None or rate != rate or rate <= 0:  # None/NaN/non-pos
            return UNKNOWN_MODEL_FALLBACK
        return float(rate)
    except Exception:
        return UNKNOWN_MODEL_FALLBACK
```

---

## Appendix C: Decision Flow Diagram (With Per-Model Pricing)

```
select_failover(model="kimi-k3")
       │
       ├── model in _OLLAMA_EXCLUSIVE_MODELS?
       │     ├── YES → short-circuit to ollama_cloud (unchanged)
       │     └── NO  → continue
       │
       ▼
   for each provider:
       │
       ├── PER_MODEL_PRICING_ENABLED?
       │     ├── YES → base_rate = _resolve_model_rate(
       │     │              self._base_rates_per_model,
       │     │              provider_name,
       │     │              "kimi-k3"          ← ◀ PER-MODEL LOOKUP
       │     │          )
       │     │
       │     │          if base_rate >= UNKNOWN_MODEL_FALLBACK:
       │     │              healthy = False    ← provider doesn't serve this model
       │     │
       │     └── NO  → base_rate = effective_rates[name]  (current behavior)
       │
       ├── base_rate *= quota_pressure        (per-provider, shared quota)
       ├── base_rate *= extra_usage / throttle (per-provider, if applicable)
       │
       ▼
   optimizer.add_provider(
       price_kalman=PriceKalman(initial_rate=base_rate),
       model="kimi-k3",                       ← ◀ actual model, not hardcoded
   )
       │
       ▼
   optimizer.route() → cheapest viable
       │
       ▼
   ((chosen_provider, "kimi-k3"), (fallback, fallback_model))
```

---

## Appendix D: GLM-5.3 Integration — Z.ai Preference Weights

### D1. Context

GLM-5.3 is z.ai's premium reasoning model. Its per-model pricing differs
from pay-per-token providers because z.ai is a flat-rate subscription:

| Dimension | z.ai (ours/friend) | Ollama Cloud | PPQ / OpenRouter / DeepInfra |
|-----------|-------------------|-------------|------------------------------|
| Pricing model | Flat subscription ($300/yr) | Subscription with quota | Pay-per-token |
| Marginal cost per model | **$0** (same pool) | Blended in quota | Varied per model |
| Per-model rate | PREFERENCE WEIGHT (routing preference) | Real cost (measured) | Real cost (measured) |

**Key insight:** Every model on a z.ai key costs the same marginal amount
(nothing extra), but the optimizer needs **non-zero rates for every model**
to compare providers fairly. Without an entry, GLM-5.3 on z.ai gets the
conservative `UNKNOWN_MODEL_FALLBACK ($1.0/M)`, which kills z.ai eligibility
despite being the cheapest option.

### D2. Preference Weight Convention

All z.ai per-model entries use **$0.001/M** — same as the provider-level
`LAST_RESORT_RATES` foundation. This is **not a real cost**. It is a
routing-preference weight that:

1. Keeps z.ai eligible for GLM-5.3 requests (avoids `UNKNOWN_MODEL_FALLBACK`).
2. Gives z.ai the *same* weight for GLM-5.3 as for GLM-5.2 (same subscription).
3. Prevents the provider-level `_default` from being the *only* viable z.ai
   entry (which would skew comparisons).

### D3. Crossover Math: z.ai GLM-5.3 vs Ollama GLM-5.2

The optimizer applies **quota pressure** to each candidate. The crossover
determines when GLM-5.2 on Ollama ($0.0155/M) becomes cheaper than GLM-5.3
on z.ai ($0.001/M + pressure).

**Variables:**

| Symbol | Meaning |
|--------|---------|
| `P_zai` | Quota pressure on z.ai key (1.0 = no pressure) |
| `P_oll` | Quota pressure on Ollama (1.0 = no pressure) |
| `R_zai` | z.ai GLM-5.3 rate ($0.001/M — preference weight) |
| `R_oll` | Ollama GLM-5.2 rate ($0.0155/M — real measured) |
| `E_zai` | GLM-5.3 effective price = R_zai × P_zai |
| `E_oll` | GLM-5.2 effective price = R_oll × P_oll |

**Crossover condition:** `E_zai < E_oll` → z.ai wins.

Substituting values:

    R_zai × P_zai < R_oll × P_oll
    0.001 × P_zai < 0.0155 × P_oll
    P_zai < 15.5 × P_oll

**Meaning:** z.ai GLM-5.3 wins as long as its pressure is less than **15.5×**
Ollama's pressure. Since both keys share similar pressure profiles (both
start at ~1.0 and rise together as quota burns), z.ai GLM-5.3 is
overwhelmingly preferred for premium work.

**Extreme case (friend key saturated):**

If `P_zai = 50.0` (friend heavily saturated) and `P_oll = 1.0` (Ollama quiet):

    0.001 × 50.0 < 0.0155 × 1.0
    0.05 < 0.0155 → **FALSE**

At 50× pressure on z.ai, Ollama GLM-5.2 becomes cheaper. This is the
intended behavior: when the friend key is under heavy load, defer standard
work to Ollama's slower, cheaper pool.

### D4. Crossover Diagram

```
Pressure on Ollama (P_oll)
  10.0 │
       │   z.ai GLM-5.3 WINS
       │   (friend preferred)
   5.0 │
       │   ╲
       │    ╲  Crossover: P_zai = 15.5 × P_oll
   2.0 │     ╲
       │      ╲
   1.0 │       ╲────────────────────── Ollama GLM-5.2 WINS
       │        ╲                    (defer to cheaper pool)
       └────────────────────────────────── Pressure on z.ai (P_zai)
       1.0   5.0   10.0   15.5   20.0
```

Note: This crossover only applies when BOTH keys have quota. If a provider
has zero quota, it is excluded regardless of price (see `_select_failover`
quota check).
