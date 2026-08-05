# Model-Aware Pricing Plan — Per-(Provider, Model) Base Rates

> **Status:** Design (pre-implementation, pre-decision)
> **Author:** Pricing engine analysis (subagent)
> **Date:** 2026-08-05
> **Branch:** `converged-rate-replay`
> **Sister docs:** `docs/endpoint-universal-pressure.md` (per-PROVIDER pressure),
> `docs/realtime-pricing-design.md` (per-model rate tracking infra),
> `docs/extra-usage-real-data-analysis.md` (measured per-model rates)
> **Felix's directive:** each `(provider, model)` pair should get its own price.
> Right now all models within a provider share one base rate. This doc explains
> the gap, the options, and a careful migration path. **No code changes yet.**

---

## 0. TL;DR — the gap in one table

| Provider | Current rate (1 per provider) | Actual per-model rates | Δ (max) |
|---|---|---|---|
| **z.ai (ours)** | $0.001/M (seed) / ~$0.029/M (amortized) | glm-5.2: ~$0.029/M, glm-4.5-flash: ~$0.018/M (same sub, different token volumes) | ~1.6× |
| **ollama_cloud** | $0.024/M (included) | glm-5.2 included: $0.0155/M, glm-5.2 extra: **$0.46/M**, kimi-k3: **$7.53/M** | **485×** |
| **ppq** | $0.14/M | kimi-k3: ~$0.14/M, deepseek-v4-flash: **$0.09 in / $0.19 out** → ~$0.14/M blended | ~1.0× |
| **deepinfra** | $1.30/M | deepseek-v4-pro: ~$1.30/M, deepseek-v4-flash: **$0.09 in / $0.19 out** → ~$0.14/M | **9.3×** |
| **openrouter** | $0.135/M | deepseek-v4-pro: ~$0.135/M, deepseek-v4-flash: **$0.09 in / $0.18 out** → ~$0.135/M | ~1.0× |

**The problem is clearest on DeepInfra and Ollama.** DeepInfra's converged rate
($1.30/M) is for the *pro* model; the *flash* model costs ~9× less — but the
optimizer sees them as the same price. Ollama's kimi-k3 at $7.53/M is 485× its
blended $0.0155/M included rate. Every routing decision that compares
DeepInfra-flash or Ollama-glm-5.2 against PPQ is wrong by up to an order of
magnitude.

---

## 1. Current Architecture — How Pricing Works Today

### 1.1 The routing flow (live_router.py `_do_select_failover`)

```
┌─────────────────────────────────────────────────────────────────────┐
│  For each provider in [ours, friend, ollama_cloud, ppq,             │
│                        openrouter, deepinfra]:                      │
│                                                                     │
│    1. base_rate = effective_rates[provider]    ← ONE rate per prov  │
│    2. Apply per-PROVIDER pressure (quota_pressure, extra_usage,     │
│       credit depletion, throttle)                                   │
│    3. Create throwaway PriceKalman(initial_rate=adjusted_base_rate) │
│    4. optimizer.add_provider(name, price_kalman,                    │
│           model=HARDCODED,          ← prov_model is fixed!          │
│           model_tier=TIER, ...)                                     │
│                                                                     │
│  optimizer.route(difficulty=...) → cheapest viable provider         │
│                                                                     │
│  model = get_model(chosen_provider, task_type)  ← resolved AFTER    │
└─────────────────────────────────────────────────────────────────────┘
```

**Critical observation:** the `add_provider` call hardcodes the model:
- `ours`/`friend`/`ollama_cloud` → always `"glm-5.2"`
- `ppq`/`openrouter`/`deepinfra` → always `"deepseek/deepseek-v4-flash"`

The model is resolved via `get_model(provider, task_type)` **after** the
optimizer has already picked the winner. The price comparison never sees
which model will actually be used.

### 1.2 The converged rate table (provider-level only)

```python
# live_router.py:196
_DEFAULT_CONVERGED_RATES = {
    "ours":          0.001,    # clamped from -0.000968
    "friend":        0.028983,
    "ollama_cloud":  0.023952,
    "ppq":           0.14,
    "openrouter":    0.135,
    "deepinfra":     1.30,
}
```

This is a `dict[str, float]` — provider name → one rate. Six entries, six
providers. No model dimension.

### 1.3 The effective price formula (pricing_engine.py)

```
effective_price = base_rate × peak × scarcity × health × pace × extra_usage
```

Where `base_rate` is the provider-level converged rate. Every multiplier is
per-provider (peak is z.ai-only, scarcity/pressure from provider quota,
health from provider failure count, pace from provider burn rate). The model
is invisible to this entire formula.

### 1.4 What already supports per-model (the partial infrastructure)

| Component | Per-model? | Where |
|---|---|---|
| `realtime_pricing.py` `by_provider_model` | ✅ `dict[tuple[str, str\|None], RateObservation]` | line 147 |
| `real_price_tracker.get_real_rate(provider, model)` | ✅ model param since T6 | line 409 |
| `providers.yaml` external models | ✅ per-model `cost_per_1m_input/output` | line 44-74 |
| `cpvo_calculator.get_effective_rates_model_aware()` | ✅ `{(prov, model): rate}` API | line 317 |
| `cpvo_calculator._query_aggregates(provider, model)` | ✅ narrows by model column | line 115 |
| `model_mapping.MODEL_MAP` | ✅ `(provider, task_type) → model` | line 99 |
| `live_router._do_select_failover` | ❌ provider-level only | line 913 |
| `routing_optimizer.add_provider` | ❌ single model per registration | line 85 |
| `_DEFAULT_CONVERGED_RATES` | ❌ `dict[str, float]` | line 196 |
| `pricing_engine.compute_effective_price` | ❌ provider-level base_rate | line 709 |

**Half the infrastructure exists.** The missing half is the wiring from the
hot path (`_do_select_failover`) through to the optimizer.

---

## 2. The Model Catalogue — Every (Provider, Model) Pair

### 2.1 Full model catalogue from MODEL_MAP + providers.yaml

This is the exhaustive list of `(provider, model)` pairs that exist in the
routing engine. Every pair needs its own base rate.

| Provider | Model | Task types | Cost model | Known $/M |
|---|---|---|---|---|
| zai (ours) | glm-5.2 | coding, reasoning | subscription ($155/mo) | ~$0.029 (amortized) |
| zai (ours) | glm-4.5 | reasoning | subscription | ~$0.025 (est) |
| zai (ours) | glm-4.5-air | chat | subscription | ~$0.020 (est) |
| zai (ours) | glm-4.5-flash | simple, chat | subscription | ~$0.018 (est) |
| zai (friend) | *(same models)* | *(same)* | subscription ($0/mo shared) | ×1.21 premium |
| ollama_cloud | glm-5.2 | coding, reasoning | subscription ($100/mo) | $0.0155 incl / $0.46 extra |
| ollama_cloud | glm-4.5-flash | chat, simple | subscription | $0.0155 incl (est) |
| ollama_cloud | **kimi-k3** | *(exclusive)* | always extra-usage | **$7.53** |
| ollama_cloud | **kimi-k2.7-code** | *(exclusive)* | always extra-usage | **$0.29** |
| ollama_cloud | **gpt-oss:120b** | *(exclusive)* | always extra-usage | unknown (~$1-3 est) |
| ppq | kimi-k3 | coding, reasoning | per-token | ~$0.14 |
| ppq | deepseek-v4-flash | chat, simple | per-token | $0.09 in / $0.19 out |
| openrouter | deepseek-v4-pro | coding, reasoning | per-token | ~$0.135 |
| openrouter | deepseek-v4-flash | chat, simple | per-token | $0.09 in / $0.18 out |
| deepinfra | deepseek-v4-pro | coding, reasoning | per-token | ~$1.30 |
| deepinfra | deepseek-v4-flash | chat, simple | per-token | $0.09 in / $0.19 out |

### 2.2 The three cost-model archetypes

| Archetype | Providers | Cost source | Per-model differentiation |
|---|---|---|---|
| **A. Flat-rate subscription** | zai (ours, friend), ollama_cloud | monthly_fee / total_tokens | Token volume per model differs (glm-5.2 uses more tokens/request than flash) |
| **B. Subscription + extra-usage** | ollama_cloud (above quota) | included rate + extra_rate | **Dramatic** — $0.0155 → $0.46 (glm-5.2), $7.53 (kimi-k3) |
| **C. Per-token (pay-per-use)** | ppq, openrouter, deepinfra | cost_per_1m_input / output | **Inherent** — each model has its own list price |

---

## 3. Design Question Answers

### Q1. ARCHITECTURE: "pick cheapest provider" → "pick cheapest (provider, model)"?

**Yes — but the change is smaller than it sounds.**

The key insight: **for a given `task_type`, `MODEL_MAP` maps each provider to
exactly one model.** The routing decision is still "pick the cheapest provider,"
but now each provider's price reflects *its specific model for this task type*
rather than a provider-wide average.

```
TODAY:                                    TOMORROW:
────────────────────────────              ─────────────────────────────────
task_type = "coding"                      task_type = "coding"

ours:          $0.001  (glm-5.2)          ours:          $0.029  (glm-5.2)
friend:        $0.029  (glm-5.2)          friend:        $0.035  (glm-5.2)
ollama_cloud:  $0.024  (glm-5.2)          ollama_cloud:  $0.0155 (glm-5.2, included)
ppq:           $0.14   (kimi-k3)          ppq:           $0.14   (kimi-k3)
openrouter:    $0.135  (ds-v4-pro)        openrouter:    $0.135  (deepseek-v4-pro)
deepinfra:     $1.30   (ds-v4-pro)        deepinfra:     $1.30   (deepseek-v4-pro)

→ Cheapest: ours ($0.001)                 → Cheapest: ollama_cloud ($0.0155)
                                          (when ours exhausted)
```

Wait — in the "tomorrow" column, zai ours moves from $0.001 to $0.029 because
the per-model amortized rate reflects the real subscription cost, not the
marginal-cost seed. **This is a separate decision** (already gated behind
`LIVE_ROUTER_DYNAMIC_RATES_ENABLED`). Model-aware pricing doesn't require
enabling dynamic rates — it just means the *base rate lookup* changes from
`dict[str, float]` to `dict[tuple[str, str], float]`.

**What changes in the code:**

| Location | Current | Proposed |
|---|---|---|
| `_do_select_failover` base_rate lookup | `effective_rates[name]` (provider key) | `_resolve_model_rate(name, get_model(name, task_type))` |
| `optimizer.add_provider(model=...)` | hardcoded `"glm-5.2"` / `"deepseek-v4-flash"` | `get_model(name, task_type)` from MODEL_MAP |
| `_DEFAULT_CONVERGED_RATES` | `dict[str, float]` (6 entries) | `dict[tuple[str, str\|None], float]` (16+ entries) |
| `_resolve_dynamic_base_rates()` | returns `dict[str, float]` | returns `dict[tuple[str, str], float]` |
| `_get_effective_rates()` | calls `cpvo.get_effective_rates(base_rates)` | calls `cpvo.get_effective_rates_model_aware(model_base_rates)` |
| `RoutingOptimizer` candidates | one entry per provider | one entry per (provider, model-for-task-type) — **same count** |

**What breaks:**

1. **Nothing in the optimizer itself.** `RoutingOptimizer.add_provider` already
   takes a `model` param and returns `chosen_model`. The optimizer doesn't care
   whether the model was hardcoded or resolved from the map — it just sorts by
   `effective_price`.

2. **The tier relaxation loop** (`for _difficulty in ("high", "medium", "low")`)
   may need rethinking. Currently it relaxes difficulty to find *any* viable
   provider. With per-model pricing, a provider's model for "coding" might be
   tier-high while its model for "chat" is tier-low. The tier should come from
   the *actual model* (via the quality_tiers config), not from the provider.

3. **`_get_effective_rates()`** returns `dict[str, float]` (provider-level). It
   needs to return `dict[tuple[str, str], float]` (model-level), or the lookup
   needs to happen per-model inside the loop.

4. **The `pace_mults` dict** is keyed by provider. If a provider serves
   different task types with different token volumes, pace should arguably be
   per-model. But pace is driven by *quota windows* (provider-level), so this
   is a non-issue — pace stays per-provider (see Q2).

5. **Telemetry/logging** — `routing_live_decisions` logs the effective rate per
   provider. The schema needs a `model` column (or the log should include the
   model name alongside the provider).

### Q2. PRESSURE MODEL: per-provider or per-model?

**Pressure stays per-PROVIDER. Quota is shared across models.**

| Provider | Quota scope | Per-model quota? | Pressure scope |
|---|---|---|---|
| zai (ours) | 5h/weekly/monthly windows on ONE API key | ❌ all models share | per-KEY |
| zai (friend) | same — separate key, separate windows | ❌ | per-KEY |
| ollama_cloud | 5h session + 7d weekly on ONE account | ❌ all models share | per-ACCOUNT |
| ppq | credit balance — no windows | ❌ all models draw from same credits | per-ACCOUNT |
| openrouter | credit balance | ❌ | per-ACCOUNT |
| deepinfra | credit balance | ❌ | per-ACCOUNT |

**The physical reality:** when z.ai's 5h window is at 90%, glm-5.2 and
glm-4.5-flash are BOTH constrained — they draw from the same quota pool. The
pressure multiplier (`quota_pressure_factor`) correctly applies to the
*provider*, not the model.

**What this means for the formula:**

```
effective_price = model_base_rate × peak × PROVIDER_pressure × health × pace
                  ^^^^^^^^^^^^^^^^         ^^^^^^^^^^^^^^^^^^
                  per-MODEL (NEW)          per-PROVIDER (unchanged)
```

The pressure factor doesn't change — it's still computed from the provider's
quota windows or credit balance. Only the base rate changes.

**The kimi-k3 exception:** kimi-k3 on Ollama is *always* extra-usage. Its
pressure is effectively permanent (the extra-usage rate IS its base rate).
This is handled by per-model base rates, not per-model pressure. See Q5.

### Q3. EFFECTIVE PRICE FORMULA

**Proposed formula:**

```
effective_price = model_base_rate × peak × provider_pressure × health × pace
```

Where:

| Component | Scope | Source | Changes from today? |
|---|---|---|---|
| `model_base_rate` | **per (provider, model)** | `realtime_pricing.get_rate(provider, model)` or `providers.yaml` list price | **YES — was `provider_base_rate`** |
| `peak` | per-provider (zai only) | `peak_multiplier(provider, hour)` | No |
| `provider_pressure` | per-provider | `quota_pressure_factor` / credit depletion | No |
| `health` | per-provider | `health_pricing_factor(failure_count)` | No |
| `pace` | per-provider | `pace_factor_multi(windows)` | No |

**CPVO overlay** (quality adjustment, already exists):

```
cpvo_adjusted_base = model_base_rate / model_success_rate
```

Where `model_success_rate` comes from `cpvo_calculator._query_aggregates(provider,
model=model)`. The `get_effective_rates_model_aware()` method already
implements this. See Q7.

**Worked example — task_type="coding", ollama session at 80%:**

| Provider | Model | Base rate | Peak | Pressure (80%) | Health | Pace | Effective |
|---|---|---|---|---|---|---|---|
| ours | glm-5.2 | $0.029 | 3.0 (peak) | 1.0 | 1.0 | 1.0 | **$0.087** |
| friend | glm-5.2 | $0.035 | 3.0 | 1.0 | 1.0 | 1.0 | **$0.105** |
| ollama_cloud | glm-5.2 | $0.0155 | 1.0 | 2.58× | 1.0 | 1.0 | **$0.040** |
| ppq | kimi-k3 | $0.14 | 1.0 | 1.0 | 1.0 | 1.0 | **$0.140** |
| openrouter | ds-v4-pro | $0.135 | 1.0 | 1.0 | 1.0 | 1.0 | **$0.135** |
| deepinfra | ds-v4-pro | $1.30 | 1.0 | 1.0 | 1.0 | 1.0 | **$1.30** |

→ Cheapest: ollama_cloud ($0.040). Correct! glm-5.2 on Ollama is cheaper
than z.ai during peak, even with 80% session pressure.

**Now the same scenario with task_type="chat":**

| Provider | Model | Base rate | Peak | Pressure (80%) | Effective |
|---|---|---|---|---|---|
| ours | glm-4.5-air | $0.020 | 3.0 (peak) | 1.0 | **$0.060** |
| ollama_cloud | glm-4.5-flash | $0.0155 | 1.0 | 2.58× | **$0.040** |
| ppq | deepseek-v4-flash | $0.14 | 1.0 | 1.0 | **$0.140** |
| openrouter | ds-v4-flash | $0.135 | 1.0 | 1.0 | **$0.135** |
| deepinfra | ds-v4-flash | $0.14 | 1.0 | 1.0 | **$0.140** |

→ Cheapest: ollama_cloud ($0.040). **DeepInfra dropped from $1.30 to $0.14!**
That's the 9.3× correction. Without per-model pricing, the optimizer thinks
DeepInfra costs $1.30 for flash — it actually costs $0.14. This is the
biggest single win of the change.

### Q4. EXTERNAL PROVIDERS: unifying per-token with subscription

**Two resolution strategies, unified by a single interface.**

#### Strategy A: Per-token providers (ppq, openrouter, deepinfra)

These already have per-model pricing in `providers.yaml`. The rates are
published list prices — no Kalman, no measurement needed:

```yaml
# config/providers.yaml — already exists
external:
  ppq:
    models:
      deepseek-v4-flash:
        cost_per_1m_input: 0.09
        cost_per_1m_output: 0.19
```

**Resolution:** look up `providers.yaml[provider][model]`, compute a blended
rate (see Q6), use as `model_base_rate`. No Kalman smoothing needed — the
rate is a constant. This is the **easiest** part of the change.

#### Strategy B: Subscription providers (zai, ollama_cloud)

These don't have per-model list prices — the subscription is flat. Per-model
rates come from **amortization** or **measurement**:

| Provider | Per-model rate source | How |
|---|---|---|
| zai (ours) | `real_price_tracker.get_real_rate("ours", "glm-5.2")` | `SUM(cost_usd)/SUM(tokens) WHERE model='glm-5.2'` — but zai cost_usd ≈ $0 (subscription). Fall back to amortization: `monthly_fee / model_token_share / 1e6`. |
| zai (friend) | same, ×1.21 premium | |
| ollama_cloud | `realtime_pricing.get_rate("ollama_cloud", "glm-5.2")` | Already measured: `$0.0155/M` included, `$0.46/M` extra (from `/api/usage` activity). |
| ollama_cloud (kimi-k3) | `realtime_pricing.get_rate("ollama_cloud", "kimi-k3")` | Already measured: `$7.53/M`. |

**The zai amortization problem:** zai is a flat-rate subscription. There's no
per-model cost_usd in the DB (cost is $0 marginal). The per-model rate is:

```
model_rate = annual_fee × model_token_fraction / model_tokens_in_M
```

Where `model_token_fraction = SUM(tokens WHERE model=X) / SUM(all_tokens)`.

Example for ours ($155/mo = $1860/yr, 21B tokens/yr):
- glm-5.2: 60% of tokens → $1860 × 0.60 / (12.6B/1e6) = $0.089/M
- glm-4.5-flash: 30% of tokens → $1860 × 0.30 / (6.3B/1e6) = $0.089/M
- (Same rate! The subscription cost is proportional to token volume.)

**This is actually the correct answer for flat-rate subscriptions:** the
per-token cost is uniform across models because you pay the same regardless of
which model you use. The differentiation comes from *quality* (CPVO), not
*cost*. The real per-model cost difference on zai is zero — it's a sunk cost.

**Recommendation for zai:** keep provider-level amortized rates for zai
(the per-model rate is the same as the provider rate). The optimizer already
sees zai as cheapest ($0.001 seed or ~$0.029 amortized). Per-model pricing
matters most for **per-token providers** and **Ollama extra-usage**, not for
flat-rate subscriptions.

### Q5. KIMI EXEMPTION

**Per-model pricing does NOT change the kimi exemption. The short-circuit
stays.**

The current logic (`live_router.py:1037`):

```python
if model in _OLLAMA_EXCLUSIVE_MODELS:
    return (("ollama_cloud", model), (None, None))
```

This fires BEFORE the price comparison. kimi-k3 always routes to ollama_cloud
regardless of price. With per-model pricing:

| Before | After |
|---|---|
| kimi-k3 → ollama_cloud (short-circuit, price ignored) | kimi-k3 → ollama_cloud (short-circuit, price ignored) |
| kimi-k3 base rate unknown to optimizer | kimi-k3 base rate = $7.53/M (but optimizer never sees it — short-circuit fires first) |

**What changes:** the kimi-k3 rate ($7.53/M) is now tracked in
`realtime_pricing.by_provider_model[("ollama_cloud", "kimi-k3")]` for
*observability* and *cost tracking*, but it doesn't affect the routing
decision (the short-circuit bypasses the optimizer).

**What DOESN'T change:** kimi-k3 still has no alternative provider. The
short-circuit is correct — there's nothing to compare against.

**One subtle improvement:** with per-model pricing, when ollama_cloud's
session quota is at 99% and glm-5.2 is being rerouted to zai (pressure-driven),
kimi-k3 requests still go to ollama_cloud (correct — they have no alternative).
But the *cost* of that kimi-k3 request ($7.53/M) is now visible in the
accounting, which it wasn't before. This helps with budget tracking and the
Routstr vision (Q8).

### Q6. INPUT VS OUTPUT TOKENS

**The problem:** external providers charge differently for input vs output:

| Provider | Model | Input $/M | Output $/M | Blended (70/30 out ratio) |
|---|---|---|---|---|
| ppq | deepseek-v4-flash | $0.09 | $0.19 | $0.09 × 0.7 + $0.19 × 0.3 = **$0.120/M** |
| openrouter | deepseek-v4-flash | $0.09 | $0.18 | $0.09 × 0.7 + $0.18 × 0.3 = **$0.117/M** |
| deepinfra | deepseek-v4-flash | $0.09 | $0.19 | **$0.120/M** |

zai and ollama are flat-rate (same cost per token regardless of direction).

**Proposed solution: blended rate based on task_type output ratio.**

```python
# Per-task-type output token fraction (configurable)
OUTPUT_TOKEN_RATIO = {
    "coding":    0.30,   # 30% of tokens are output (code generation)
    "reasoning": 0.35,   # slightly more output (analysis)
    "chat":      0.40,   # more conversational output
    "simple":    0.20,   # short answers
}

def blend_rate(input_rate, output_rate, output_ratio):
    return input_rate * (1 - output_ratio) + output_rate * output_ratio
```

**Example — task_type="coding" (30% output):**

| Provider | Model | Input | Output | Blended |
|---|---|---|---|---|
| ppq | deepseek-v4-flash | $0.09 | $0.19 | $0.09 × 0.70 + $0.19 × 0.30 = **$0.120/M** |
| ppq | kimi-k3 | — | — | **$0.14/M** (flat — no input/output split published) |
| openrouter | ds-v4-flash | $0.09 | $0.18 | **$0.117/M** |
| deepinfra | ds-v4-flash | $0.09 | $0.19 | **$0.120/M** |

**For subscription providers (zai, ollama):** the blended rate equals the
flat rate — no input/output distinction exists. This is handled naturally:
if `providers.yaml` has no `cost_per_1m_input/output` for a model, fall back
to the amortized/measured flat rate.

**Migration note:** the `OUTPUT_TOKEN_RATIO` table should be configurable in
`providers.yaml`, not hardcoded. Different workloads (coding vs chat) have
very different ratios, and the ratio affects the ranking between providers
that price input/output differently.

### Q7. CPVO (Cost Per Valid Output)

**Yes — quality should factor into the model-aware price. And the
infrastructure already exists.**

`cpvo_calculator.py` already has `get_effective_rates_model_aware()`:

```python
def get_effective_rates_model_aware(
    self,
    base_rates: dict[tuple[str, str | None], float],
) -> dict[tuple[str, str | None], float]:
```

This method takes `{(provider, model): base_rate}` and returns
`{(provider, model): effective_rate}` where `effective = base / success_rate`
for models with <95% success rate. It even falls back to provider-level
aggregation when per-model samples are insufficient (<100).

**The integration is straightforward:**

```python
# In _do_select_failover (pseudocode):
model_base_rates = {
    (name, get_model(name, task_type)): _resolve_model_rate(name, task_type)
    for name in self._provider_names
}
cpvo_adjusted = self._cpvo.get_effective_rates_model_aware(model_base_rates)

for name in self._provider_names:
    model = get_model(name, task_type)
    base_rate = cpvo_adjusted[(name, model)]
    # ... register with optimizer ...
```

**Why CPVO matters more with per-model pricing:**

Without per-model pricing, CPVO adjusts provider-level rates — but all models
in a provider share the penalty. A glm-4.5-flash failure inflates the rate for
glm-5.2 too (they share the provider key). With per-model CPVO:

| Provider | Model | Base $/M | Success rate | CPVO $/M | Notes |
|---|---|---|---|---|---|
| zai | glm-5.2 | $0.029 | 99.5% | $0.029 | No penalty |
| zai | glm-4.5-flash | $0.029 | 92% | $0.032 | Small penalty |
| ollama | kimi-k3 | $7.53 | 98% | $7.68 | Minimal — already expensive |
| deepinfra | ds-v4-flash | $0.120 | 85% | **$0.141** | Now more expensive than ppq |

**This is the killer feature.** A cheaper model that produces garbage becomes
more expensive per useful token. The CPVO overlay is what makes per-model
pricing actually correct, not just more granular.

**Recommendation:** enable per-model CPVO as part of this change. The method
exists, the telemetry table has a `model` column check, and the fallback to
provider-level is already implemented. It's low-risk.

### Q8. ROUTSTR VISION

**Recommendation: each PROVIDER = one Routstr node that publishes MULTIPLE
model prices.**

Rationale:

| Option | Granularity | Node count | Pros | Cons |
|---|---|---|---|---|
| **A. Each (provider, model) = a node** | 16+ nodes | High | Pure market — each model competes independently | Quota is shared; can't model "key exhaustion" per-model. Pressure is per-provider. |
| **B. Each provider = a node, publishes model menu** | 6 nodes | Low | Correct quota model (shared pool). Matches physical reality. | Model selection is delegated to the node, not the market. |
| **C. Hybrid: provider = node, but exposes per-model prices** | 6 nodes | Low | Best of both — quota pressure per-provider, price per-model | Slightly more complex protocol. |

**Option C is correct.** A Routstr node represents a *physical endpoint*
(API key + account). It has:

- **Shared quota** (one pool for all models) → pressure is per-node
- **Per-model prices** (different models cost differently) → base rates per-model
- **Per-model quality** (CPVO) → effective rates per-model

The node publishes a **price menu**:

```json
{
  "node_id": "ollama_cloud",
  "quota_state": {"session_5h": 0.72, "weekly_7d": 0.45},
  "models": {
    "glm-5.2":     {"base_rate": 0.0155, "extra_rate": 0.46,  "success_rate": 0.99},
    "glm-4.5-flash": {"base_rate": 0.0155, "extra_rate": 0.46, "success_rate": 0.95},
    "kimi-k3":     {"base_rate": 7.53,   "extra_rate": 7.53,  "success_rate": 0.98}
  }
}
```

The router (client) computes the effective price per-model, factoring in the
node's quota pressure (shared across all models on that node). This is
exactly the architecture described in this plan: **per-model base rate ×
per-provider pressure.**

---

## 4. Proposed Implementation — The Per-Model Rate Table

### 4.1 New data structure: `MODEL_BASE_RATES`

Replace `_DEFAULT_CONVERGED_RATES` (6 entries) with a per-model table:

```python
# Seed values — replaced by measured data after first refresh cycle
# Keys: (provider, model) → base $/M
_MODEL_BASE_RATES_SEED: dict[tuple[str, str | None], float] = {
    # ── z.ai (flat-rate subscription — same rate for all models) ──
    ("ours", "glm-5.2"):          0.001,    # seed; amortized ~0.029
    ("ours", "glm-4.5"):          0.001,
    ("ours", "glm-4.5-air"):      0.001,
    ("ours", "glm-4.5-flash"):    0.001,
    ("friend", "glm-5.2"):        0.029,    # ×1.21 premium
    ("friend", "glm-4.5-flash"):  0.029,
    ("friend", None):             0.029,    # provider-level fallback

    # ── Ollama Cloud (measured from /api/usage) ──
    ("ollama_cloud", "glm-5.2"):        0.0155,   # included
    ("ollama_cloud", "glm-4.5-flash"):  0.0155,   # included (est)
    ("ollama_cloud", "kimi-k3"):        7.53,     # always extra
    ("ollama_cloud", "kimi-k2.7-code"): 0.29,     # always extra
    ("ollama_cloud", None):             0.0155,   # provider-level fallback

    # ── PPQ (per-token, from providers.yaml) ──
    ("ppq", "kimi-k3"):            0.14,     # flat
    ("ppq", "deepseek-v4-flash"):  0.120,    # blended: 0.09×0.7 + 0.19×0.3
    ("ppq", None):                 0.14,     # provider-level fallback

    # ── OpenRouter (per-token) ──
    ("openrouter", "deepseek-v4-pro"):   0.135,
    ("openrouter", "deepseek-v4-flash"): 0.117,   # blended: 0.09×0.7 + 0.18×0.3
    ("openrouter", None):                0.135,

    # ── DeepInfra (per-token) ──
    ("deepinfra", "deepseek-v4-pro"):   1.30,
    ("deepinfra", "deepseek-v4-flash"): 0.120,   # blended: 0.09×0.7 + 0.19×0.3
    ("deepinfra", None):                1.30,
}
```

### 4.2 Resolution function: `_resolve_model_rate`

```python
def _resolve_model_rate(
    provider: str,
    task_type: str,
    *,
    effective_rates: dict[tuple[str, str | None], float] | None = None,
) -> float:
    """Resolve the base $/M for a (provider, task_type) pair.

    Resolution order:
    1. Look up the model for this (provider, task_type) via MODEL_MAP.
    2. Check effective_rates (CPVO-adjusted) for (provider, model).
    3. Check realtime_pricing snapshot for (provider, model).
    4. Check real_price_tracker.get_real_rate(provider, model).
    5. Fall back to _MODEL_BASE_RATES_SEED[(provider, model)].
    6. Fall back to provider-level seed _MODEL_BASE_RATES_SEED[(provider, None)].
    7. Fall back to MIN_EFFECTIVE_PRICE.

    Never raises.
    """
```

### 4.3 Changes to `_do_select_failover` (the hot path)

The main loop changes from:

```python
# TODAY (provider-level)
for name in self._provider_names:
    base_rate = effective_rates.get(name, ...)
    prov_model = "glm-5.2" if name in ("ours", "friend", "ollama_cloud") else "deepseek/..."
    # ... pressure, health, etc ...
    optimizer.add_provider(name=name, model=prov_model, ...)
```

To:

```python
# TOMORROW (model-level)
for name in self._provider_names:
    model = get_model(name, task_type)  # resolve from MODEL_MAP
    base_rate = _resolve_model_rate(name, task_type, effective_rates=model_effective_rates)
    # ... per-PROVIDER pressure, health, pace (unchanged) ...
    optimizer.add_provider(name=name, model=model, ...)  # real model!
```

**Lines changed:** ~15 lines in the loop body. The pressure/health/pace code
stays identical — it operates on the *provider*, not the model.

---

## 5. Migration Path — Phased Rollout

### Phase 0: Observability (no routing change) — 1 day

**Goal:** see what per-model rates WOULD be, without changing routing.

1. Add `_resolve_model_rate()` function (new, additive).
2. Add `_MODEL_BASE_RATES_SEED` table (new constant, unused by router).
3. In `_do_select_failover`, after the routing decision, log what the
   per-model rates WOULD have been alongside the provider-level rates.
4. Run shadow comparison for 24-48h.

**Risk:** Zero. No routing change. Pure logging.

### Phase 1: External providers only (ppq, openrouter, deepinfra) — 2 days

**Goal:** fix the DeepInfra $1.30→$0.14 correction for flash models.

1. For external providers, look up per-model rates from `providers.yaml`.
2. Compute blended rate using `OUTPUT_TOKEN_RATIO`.
3. Use as `base_rate` in `optimizer.add_provider`.
4. zai and ollama keep provider-level rates (unchanged).
5. Kill switch: `MODEL_AWARE_PRICING_EXTERNAL=true` (default OFF).

**Why externals first:** they have published per-model prices — no
measurement or amortization needed. The correction is large (9.3× for
DeepInfra flash) and low-risk (the optimizer just sees cheaper flash rates).

**Expected behavior change:** DeepInfra flash becomes viable as a failover
(it was priced at $1.30, now $0.14). This may shift traffic toward DeepInfra
for chat/simple task types.

### Phase 2: Ollama per-model (included vs extra) — 2 days

**Goal:** use measured per-model Ollama rates from `realtime_pricing`.

1. For ollama_cloud, look up `(provider, model)` from
   `realtime_pricing.get_rate("ollama_cloud", model)`.
2. kimi-k3 gets $7.53/M (but short-circuit still fires — observability only).
3. glm-5.2 included rate ($0.0155) replaces the $0.024 converged seed.
4. Kill switch: `MODEL_AWARE_PRICING_OLLAMA=true` (default OFF).

**Expected behavior change:** ollama_cloud glm-5.2 base rate drops from
$0.024 to $0.0155 (included rate). This makes ollama even cheaper relative
to PPQ/OpenRouter. The extra-usage rate ($0.46) is already handled by the
existing pressure curve — no change needed.

### Phase 3: zai per-model (optional, low value) — 1 day

**Goal:** per-model rates for zai (mostly cosmetic — rates are the same).

1. For zai, compute per-model amortized rates.
2. In practice, all zai models get the same rate (flat subscription).
3. CPVO differentiates by quality (glm-5.2 at 99.5% vs flash at 92%).
4. Kill switch: `MODEL_AWARE_PRICING_ZAI=true` (default OFF).

**Expected behavior change:** minimal. The per-model CPVO overlay may
slightly penalize glm-4.5-flash relative to glm-5.2, but both are on the
same key with the same quota. The change is mostly for correctness and
observability.

### Phase 4: Per-model CPVO overlay — 1 day

**Goal:** enable `get_effective_rates_model_aware()` in the hot path.

1. Replace `self._cpvo.get_effective_rates(base_rates)` with
   `self._cpvo.get_effective_rates_model_aware(model_base_rates)`.
2. The method already falls back to provider-level when per-model samples
   are insufficient (<100).
3. Kill switch: `MODEL_AWARE_CPVO=true` (default OFF).

**Expected behavior change:** providers with per-model quality differences
get differentiated. A 85%-success deepinfra-flash gets penalized more than
a 99%-success deepinfra-pro.

### Phase 5: Full integration — remove kill switches — 1 day

1. Remove all `MODEL_AWARE_PRICING_*` kill switches (hardcode ON).
2. Remove `_DEFAULT_CONVERGED_RATES` (replaced by `_MODEL_BASE_RATES_SEED`).
3. Update `routing_live_decisions` schema to include `model` column.
4. Update all tests.

**Total estimated effort: 7-8 days** (with shadow validation between phases).

---

## 6. Risk Analysis

### 6.1 What could go wrong

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Per-model rates are wrong (bad seed/measurement) | Medium | High — wrong routing | Shadow comparison in Phase 0; kill switches per phase |
| DeepInfra flash suddenly gets too much traffic | Medium | Medium — credit burn | DeepInfra already has credit pressure; the $5 balance limits exposure |
| Ollama glm-5.2 rate drops to $0.0155, attracts more traffic | Low | Low — more usage of cheaper endpoint | Pressure curve still applies; session quota still limits |
| CPVO penalizes new models unfairly (cold start) | Low | Medium — new model looks bad | Fallback to provider-level (<100 samples); MIN_SAMPLES guard |
| Tier relaxation breaks (model doesn't match difficulty) | Medium | Medium — wrong model for task | Map tier from the MODEL_MAP model, not from the provider |
| Telemetry `model` column missing in old DBs | Low | Low — CPVO falls back to provider-level | Already handled (`PRAGMA table_info` check) |

### 6.2 What does NOT change

- **Pressure model:** still per-provider (quota is shared). No change.
- **Health model:** still per-provider (failure count per key). No change.
- **Pace model:** still per-provider (quota windows per key). No change.
- **Peak multiplier:** still per-provider (zai only). No change.
- **Kimi short-circuit:** still fires before price comparison. No change.
- **Scarcity factor:** still per-provider (quota consumption). No change.
- **RoutingOptimizer:** still sorts by effective_price. No change to the
  optimizer class itself — only the inputs change.

### 6.3 Backward compatibility

Every phase has a kill switch (default OFF). When OFF, the router behaves
exactly as today (provider-level rates, hardcoded models). The migration is
strictly additive — no existing behavior changes unless an operator flips a
switch.

---

## 7. Comparison: Provider-Level vs Model-Level Pricing

### 7.1 Task type "coding" — full comparison

| Provider | Model | Current $/M (provider) | Proposed $/M (model) | Δ | Why different |
|---|---|---|---|---|---|
| ours | glm-5.2 | $0.001 | $0.001 | 0× | Same (flat sub) |
| friend | glm-5.2 | $0.029 | $0.029 | 0× | Same (flat sub) |
| ollama_cloud | glm-5.2 | $0.024 | $0.0155 | **0.65×** | Measured included rate < converged seed |
| ppq | kimi-k3 | $0.14 | $0.14 | 0× | Same (already per-token) |
| openrouter | ds-v4-pro | $0.135 | $0.135 | 0× | Same (already per-token) |
| deepinfra | ds-v4-pro | $1.30 | $1.30 | 0× | Same (pro model matches converged) |

### 7.2 Task type "chat" — where it matters most

| Provider | Model | Current $/M (provider) | Proposed $/M (model) | Δ | Why different |
|---|---|---|---|---|---|
| ours | glm-4.5-air | $0.001 | $0.001 | 0× | Same |
| ollama_cloud | glm-4.5-flash | $0.024 | $0.0155 | **0.65×** | Measured |
| ppq | ds-v4-flash | $0.14 | **$0.120** | **0.86×** | Blended input/output |
| openrouter | ds-v4-flash | $0.135 | **$0.117** | **0.87×** | Blended input/output |
| deepinfra | ds-v4-flash | **$1.30** | **$0.120** | **0.092×** | **Flash ≠ Pro!** |

**DeepInfra for chat goes from $1.30 to $0.12 — a 10.8× correction.** This
is the single biggest routing error in the current system. Without per-model
pricing, the optimizer treats DeepInfra as the most expensive provider for
ALL task types, when in reality its flash model is competitively priced for
chat/simple tasks.

### 7.3 Task type "simple" — same pattern

Same as chat — all externals use flash models. DeepInfra goes from $1.30 to
$0.12. PPQ goes from $0.14 to $0.12. The ranking between PPQ and DeepInfra
for simple tasks flips entirely.

---

## 8. Open Questions for Felix

1. **zai per-model rates:** Since zai is a flat subscription, the per-model
   cost is the same (~$0.029 amortized). Should we bother with per-model zai
   rates, or keep them provider-level? **Recommendation: provider-level for zai,
   per-model for everything else.**

2. **Output token ratio:** The `OUTPUT_TOKEN_RATIO` table (0.30 for coding,
   0.40 for chat, etc.) is an estimate. Should we measure actual ratios from
   the telemetry DB? **Recommendation: start with estimates, replace with
   measured averages after 7 days.**

3. **Ollama extra-usage model selection:** When ollama_cloud's session is in
   extra-usage mode, glm-5.2's rate jumps to $0.46/M. Should the optimizer
   consider switching to a *cheaper model on the same provider* (e.g.,
   glm-4.5-flash at a lower extra rate)? Currently MODEL_MAP fixes one model
   per task_type. **Recommendation: out of scope for this plan — the task_type
   determines the model, and the optimizer compares across providers, not
   within.**

4. **DeepInfra deepseek-v4-pro pricing:** The converged rate ($1.30/M) seems
   high for deepseek-v4-pro. Is this measured or estimated? providers.yaml
   only lists flash pricing. **Recommendation: verify the pro rate against
   DeepInfra's actual billing.**

5. **Routstr node publication:** Should the per-model price menu (Q8) be
   published as a Nostr event (NIP-XX)? This affects the Routstr marketplace
   design. **Recommendation: separate design doc — this plan focuses on the
   internal routing engine.**

---

## 9. Summary

| Aspect | Current (provider-level) | Proposed (model-level) | Impact |
|---|---|---|---|
| Base rate lookup | `_DEFAULT_CONVERGED_RATES[provider]` | `_resolve_model_rate(provider, task_type)` | Per-model accuracy |
| Biggest correction | — | DeepInfra flash: $1.30 → $0.12 (10.8×) | Flash becomes viable failover |
| Pressure | per-provider | per-provider (unchanged) | No change — quota is shared |
| CPVO | per-provider | **per-model** (already coded) | Quality differentiation within provider |
| Kimi short-circuit | bypasses optimizer | bypasses optimizer (unchanged) | No change |
| RoutingOptimizer | sort by effective_price | sort by effective_price (unchanged) | No change to optimizer class |
| Migration | — | 5 phases, each with kill switch | Zero-risk rollout |
| Infrastructure ready | 50% | 90% after wiring | Most code exists; ~15 lines in hot path |

**The change is smaller than it sounds.** The infrastructure (per-model rate
tracking, per-model CPVO, model mapping, per-model provider.yaml pricing) is
mostly built. The gap is a ~15-line change in `_do_select_failover`'s main
loop: resolve the model from `MODEL_MAP` and look up its rate instead of the
provider-level rate. Everything else — pressure, health, pace, the optimizer,
the kimi short-circuit — stays the same.
