# Architecture Analysis: Free-Tier LLM Endpoint Integration

**Date:** 2026-08-22
**Status:** Design document — no code changes
**Scope:** Compares full Kalman integration (Approach A) vs. pre-proxy filter (Approach B)

---

## Executive Summary

**Recommendation: Approach B (Pre-Proxy Filter).** It captures ~80% of the cost
savings with ~10% of the complexity. The Kalman system's value proposition is
*price-driven selection among paid providers with dynamic cost*. Free-tier
endpoints have a fixed $0 cost — there's nothing to smooth, predict, or
optimize. Inserting them into the Kalman pipeline adds 12 integration points
to model a constant, which is architectural overkill.

The existing `promo_tier.py` (oxalpha) already demonstrates the right pattern:
a pure guard module that sits alongside the Kalman system, not inside it.

---

## Codebase Context (as examined)

| Component | File | Role |
|-----------|------|------|
| Provider config | `config/providers.yaml` | Static provider definitions, model_map, strategy |
| Price Kalman | `src/price_kalman.py` | 2-state filter: [base_rate, velocity] per provider |
| Consumption Kalman | `src/consumption_kalman.py` | 3-state filter: [burn_rate, velocity, acceleration] per provider |
| Routing Optimizer | `src/routing_optimizer.py` | 5-stage filter pipeline: tier → health → exhaustion → scarcity → price |
| Pricing Engine | `src/pricing_engine.py` | Deterministic multipliers: peak, scarcity, health, pace |
| Primary Router | `src/primary_router.py` | Phase 3 singleton; registers providers, computes pace, calls optimizer |
| Promo Tier Guard | `src/promo_tier.py` | Pure guard for free/promo tiers — expiry, spend, 402, allowlist |
| Shadow Logger | `src/shadow_logger.py` | A/B comparison logging |
| Model Mapping | `src/model_mapping.py` | (provider, task_type) → model_name |
| Provider Names | `src/provider_names.py` | Name normalization |

### Existing Provider Registry (primary_router.py)

```
_SEED_COSTS = {ours: 0.31, friend: 0.375, ollama_cloud: 0.024,
               ppq: 0.14, openrouter: 0.135, deepinfra: 1.30}
_QUOTA_TOTALS = {ours: 2M, friend: 2M, ollama_cloud: 500M,
                 ppq: inf, openrouter: inf, deepinfra: inf}
```

### Existing Precedent: oxalpha Promo Tier

The `oxalpha` block in `providers.yaml` + `promo_tier.py` is ALREADY a free-tier
integration — just for a time-limited promo rather than a permanent free tier.
Key design choices already validated:
- Separate provider entry (not a variant of `openrouter`)
- `pricing_model: promo_zero` (not `per_token`)
- Pure guard module (no DB, no network, no clock at import)
- Spend guard: any nonzero charge → immediate disable
- Allowlist: only specific task types may reach the tier
- NOT wired into live routing path (repo-side fixture only)

---

## Question-by-Question Analysis

### Q1: New providers or variants of existing providers?

**Answer: New provider entries (not variants).**

The codebase already answers this. `oxalpha` is a separate provider entry that
happens to share OpenRouter's `base_url`. This pattern works because:

1. **One PriceKalman per provider name** — `primary_router.py` creates one
   PriceKalman instance per entry in `_SEED_COSTS`. A "variant" would need either
   two Kalman instances under one provider name (not supported) or a sub-provider
   namespace (doesn't exist).

2. **Different cost structures** — OpenRouter paid is ~$0.135/M (per-token,
   Kalman-smoothed). OpenRouter free is $0.001/M (floor, constant). These need
   different `pricing_model` values and different Kalman initialization.

3. **Different rate-limit regimes** — Free tiers have RPM limits; paid tiers
   don't. The health tracker and circuit breaker need to track these independently.

4. **Different model names** — OpenRouter free models have `:free` suffix
   (e.g., `zai-org/GLM-5.2:free`). The `model_map` needs separate entries.

**Config would look like:**
```yaml
openrouter_free:
  base_url: "https://openrouter.ai/api/v1"
  key_env: "OPENROUTER_API_KEY"  # same key, different model names
  headers: { HTTP-Referer: "https://hermes.local", X-Title: "Hermes Agent" }
  pricing_model: free_tier  # new pricing model — $0 marginal cost
  models:
    glm-5.2-free: "zai-org/GLM-5.2:free"
    glm-4.5-flash-free: "zai-org/GLM-4.5-flash:free"
  rate_limits:
    rpm: 20           # requests per minute
    daily_cap: 1000    # requests per day (if applicable)
  context_window_cap: 256000  # tokens (see Q4)
```

### Q2: How to model request-count rate limits in the token-based quota_pressure framework?

**This is the hardest integration problem.** The existing system is entirely
token-oriented:

- `ConsumptionKalman` tracks token burn_rate (tokens/period)
- `quota_remaining` and `quota_total` are in tokens
- `scarcity_factor` ramps based on `quota_used_pct` (token percentage)
- `pace_factor` predicts quota exhaustion from token burn rate

Free tiers typically limit by **request count** (RPM, RPD), not tokens.
A 500-token request and a 50,000-token request both consume 1 RPM.

**Options:**

| Option | Complexity | Correctness |
|--------|-----------|-------------|
| **A.** Model 1 request = N "virtual tokens" | Low | Wrong — doesn't scale with actual token usage |
| **B.** Add a parallel RequestCountKalman | High | Correct — but doubles the Kalman infrastructure |
| **C.** Use health_pricing_factor (429s → failure_count) | **Zero** | **Good enough** — 429s already increment failure_count, which raises effective price via graduated penalties (1.5x → 3x → 10x → inf) |
| **D.** Pre-proxy filter tracks RPM externally | Low | Correct for the filter approach |

**Option C is already built.** When a free tier returns 429, the existing
circuit breaker logic handles it: failure_count increments, effective price
rises, and after 5 consecutive failures the breaker trips (300s cooldown).
This is sufficient for rate-limit management without any new Kalman work.

The only gap: there's no *proactive* RPM tracking (counting requests before
they hit the limit). But for a free tier that's a nice-to-have, not a need.

### Q3: Should free tiers have their own onset/asymptote?

**Yes, but they're trivial.** Free tiers have:
- `base_rate` = MIN_EFFECTIVE_PRICE ($0.001/M) — constant, zero velocity
- `scarcity_onset` = N/A (no token quota to deplete)
- `peak_hours` = none (free tiers don't have peak pricing)
- `pace_factor` = 1.0 always (no token-based quota windows)

A PriceKalman initialized with `initial_rate=0.001, process_noise=0` would be
a constant function — the Kalman filter does nothing useful. This is a strong
signal that the free tier doesn't belong in the Kalman pipeline.

### Q4: Context window cap (256k) — provider-level or per-model?

**Per-model, but the system currently has no per-model attribute table.**

The `model_map` in `providers.yaml` maps `(provider, task_type) → model_name`
but carries no metadata about each model (context window, max output tokens,
supports vision, etc.). This is a pre-existing gap.

For free tiers specifically:
- OpenRouter free models often have reduced context windows (e.g., 256k vs 1M)
- This is a **hard constraint**, not a pricing signal
- It should be a filter in the routing pipeline (like quality tier), not a
  price multiplier

**Minimal fix:** Add a `context_window` field to the model_map entries and
check it in `_evaluate_provider()` as a 6th filter stage. This is ~20 lines.

### Q5: Should the manager profile decide to use the free tier?

**No.** The system philosophy is explicit: "manager decides model, proxy
decides provider." Free tier is a provider-level concern.

The manager asks for a model (e.g., "glm-5.2") at a difficulty level. The
proxy's job is to find the cheapest provider that can serve it. If a free-tier
provider offers the same model at $0.001/M vs $0.31/M for z.ai, the optimizer
should select it automatically.

The manager should NOT know about free tiers, promo tiers, or provider
selection internals. This separation is already enforced by the
`difficulty → quality_tier → provider` pipeline.

### Q6: Model substitution risk

OpenRouter free "GLM 5.2" might be a quantized version, a different model
behind the same name, or subject to priority deprioritization.

**CPVO (quality probes) would catch this**, but the existing system doesn't
have active CPVO quality measurement implemented yet (it's referenced in docs
but not in the codebase). The `quality_tiers` in `providers.yaml` are
static model lists, not measured quality.

**Practical assessment:**
- For `low`/`medium` difficulty tasks (chat, simple), substitution risk is
  acceptable — the output quality difference is marginal
- For `high` difficulty (coding, reasoning), substitution risk is real — but
  the quality_tier gate already prevents free tiers from serving these if
  their models aren't in the "high" tier
- Probe cost: 1 probe per model per ~100 requests = ~1% overhead. Worth it
  if free-tier traffic is significant.

**Recommendation:** Don't probe initially. Rely on the quality_tier gate to
keep free tiers on low/medium tasks. Add probes later if traffic volume
justifies it.

### Q7: Pre-proxy filter vs. full integration

This is the core question. Let me design both.

---

## Approach A: Full Integration into Kalman Pricing System

### The 12 Integration Points

For each new free-tier provider (e.g., `openrouter_free`), the following
changes are needed:

| # | Integration Point | File | Change | Effort |
|---|-------------------|------|--------|--------|
| 1 | Provider config block | `config/providers.yaml` | Add `openrouter_free:` block with models, rate_limits, pricing_model | 15 lines |
| 2 | Seed cost | `src/primary_router.py` `_SEED_COSTS` | Add `"openrouter_free": 0.001` | 1 line |
| 3 | Quota total | `src/primary_router.py` `_QUOTA_TOTALS` | Add `"openrouter_free": float("inf")` (no token quota) | 1 line |
| 4 | PriceKalman instance | `src/primary_router.py` `__init__` | Auto-created from _SEED_COSTS loop — no change needed | 0 lines |
| 5 | ConsumptionKalman instance | `src/primary_router.py` `__init__` | Auto-created — no change needed | 0 lines |
| 6 | Provider registration | `src/primary_router.py` `_do_route` | Add elif branch for tier/model/peak config | 5 lines |
| 7 | Pace windows | `src/primary_router.py` `_build_pace_windows` | Add free-tier handling (no windows → pace=1.0) | 3 lines |
| 8 | Model map | `config/providers.yaml` `strategy.model_map` | Add `openrouter_free:` task_type → model entries | 4 lines |
| 9 | Quality tiers | `config/providers.yaml` `strategy.quality_tiers` | Add free model names to appropriate tiers | 2 lines |
| 10 | Provider name normalization | `src/provider_names.py` | Add `openrouter_free` → `openrouter_free` mapping | 1 line |
| 11 | Shadow logger | `src/shadow_logger.py` | Add provider to comparison set | 2 lines |
| 12 | Rate-limit handling | `src/primary_router.py` + new module | Add RPM tracker or rely on 429→failure_count | 0-50 lines |

**Total estimated effort: ~35-85 lines of code across 6-8 files.**

But the real cost isn't line count — it's the ongoing maintenance burden:

- **Kalman state for a constant**: PriceKalman with `initial_rate=0.001` and
  zero velocity is a no-op. The filter adds state, covariance matrices, and
  update cycles that do nothing. It's dead weight in every routing decision.

- **Pace windows for RPM limits**: The pace_factor mechanism predicts token
  quota exhaustion. For RPM-limited free tiers, this dimension doesn't apply.
  You'd need to either skip pace (special case) or add a parallel request-count
  tracking system (new Kalman variant).

- **Scarcity modeling gap**: Free tiers don't have token quotas, so scarcity_factor
  is always 1.0. But they DO have rate limits that deplete. The scarcity ramp
  doesn't model this correctly — you'd be mixing token-scarcity and
  request-scarcity in the same multiplier, which is semantically wrong.

- **Exhaustion gate mismatch**: `ConsumptionKalman.will_exhaust()` predicts
  token quota exhaustion. For free tiers, the relevant exhaustion is RPM/daily
  request limits. The gate would either always pass (wrong — ignores rate limits)
  or need a separate request-count exhaustion model.

### Design: Minimal Viable Full Integration

```python
# In primary_router.py _SEED_COSTS:
_SEED_COSTS = {
    ...existing...,
    "openrouter_free": 0.001,  # ADR-004 floor
}

# In _QUOTA_TOTALS:
_QUOTA_TOTALS = {
    ...existing...,
    "openrouter_free": float("inf"),  # no token quota
}

# In _do_route, provider registration loop:
elif name == "openrouter_free":
    tier = "low"  # only low-difficulty tasks
    mdl = "zai-org/GLM-4.5-flash:free"
    prov_peak = None
    prov_peak_mult = 1.0

# In providers.yaml model_map:
openrouter_free:
  coding: null      # not eligible
  reasoning: null   # not eligible
  chat: "zai-org/GLM-4.5-flash:free"
  simple: "zai-org/GLM-4.5-flash:free"
```

The PriceKalman for `openrouter_free` would be initialized at $0.001/M and
never update (no cost observations to feed it). The ConsumptionKalman would
track token burn but has no quota to deplete (`quota_total = inf`). The
scarcity factor is always 1.0. The pace factor is always 1.0. The peak
multiplier is always 1.0.

**Net effect: the entire Kalman pipeline reduces to a constant $0.001/M
for this provider.** All the machinery does nothing.

---

## Approach B: Pre-Proxy Filter

### Design

A lightweight filter that runs BEFORE the Kalman routing pipeline. It checks
if the incoming request is eligible for free-tier routing and, if so, sends
it directly to the free-tier endpoint. Only if the free tier is unavailable
(rate-limited, down, or request ineligible) does the request fall through to
the normal Kalman routing.

```
Request arrives
    │
    ▼
┌─────────────────────┐     eligible?     ┌──────────────────────┐
│  FreeTierFilter     │──────────────────►│  Free-tier endpoint  │
│  (pre-proxy)        │                   │  (OpenRouter :free)  │
│                     │                   └──────────────────────┘
│  Checks:            │     not eligible
│  - task_type allow  │         or
│  - token count < cap│     rate-limited     │
│  - RPM not exceeded │         │            │
│  - tier not disabled │         ▼            │
└─────────────────────┘  ┌──────────────────┐ │
                          │  Kalman routing  │ │
                          │  (existing)      │ │
                          └──────────────────┘ │
```

### Implementation

```python
# src/free_tier_filter.py — ~150 lines, pure module

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any

# Reuse promo_tier.py patterns: pure guard, no DB/network at import

@dataclass
class FreeTierConfig:
    """Configuration for one free-tier endpoint."""
    provider: str                    # "openrouter_free"
    base_url: str                    # "https://openrouter.ai/api/v1"
    key_env: str                     # "OPENROUTER_API_KEY"
    headers: dict = field(default_factory=dict)

    # Eligibility constraints
    max_input_tokens: int = 8000     # small requests only
    allowed_task_types: frozenset = field(
        default_factory=lambda: frozenset({"chat", "simple"}))
    allowed_models: frozenset = field(
        default_factory=lambda: frozenset({"glm-4.5-flash"}))

    # Rate limits (RPM = requests per minute)
    rpm_limit: int = 20
    rpm_window_s: int = 60

    # Failover
    circuit_breaker_threshold: int = 5
    circuit_breaker_cooldown_s: int = 300


@dataclass
class FreeTierState:
    """Runtime state for one free-tier endpoint."""
    request_timestamps: list = field(default_factory=list)  # sliding window
    consecutive_failures: int = 0
    breaker_tripped: bool = False
    breaker_tripped_at: float | None = None

    def rpm_available(self, config: FreeTierConfig, now: float) -> int:
        """How many requests can we still send this minute?"""
        cutoff = now - config.rpm_window_s
        self.request_timestamps = [t for t in self.request_timestamps if t > cutoff]
        return max(0, config.rpm_limit - len(self.request_timestamps))

    def is_healthy(self, config: FreeTierConfig, now: float) -> bool:
        """Check circuit breaker state."""
        if not self.breaker_tripped:
            return True
        if self.breaker_tripped_at is None:
            return True
        if now - self.breaker_tripped_at > config.circuit_breaker_cooldown_s:
            self.breaker_tripped = False
            self.consecutive_failures = 0
            return True
        return False

    def record_success(self, now: float):
        self.consecutive_failures = 0
        self.breaker_tripped = False
        self.request_timestamps.append(now)

    def record_failure(self, now: float, config: FreeTierConfig):
        self.consecutive_failures += 1
        if self.consecutive_failures >= config.circuit_breaker_threshold:
            self.breaker_tripped = True
            self.breaker_tripped_at = now


class FreeTierFilter:
    """Pre-proxy filter: routes eligible small requests to free-tier endpoints.

    Sits BEFORE the Kalman routing pipeline. If a request is eligible and the
    free tier is available, it short-circuits to the free endpoint. Otherwise,
    it returns None and the request flows through normal Kalman routing.

    This is a PURE module: no DB, no network at import. All state is in-memory.
    Thread safety: callers should hold a lock around route_to_free_tier().
    """

    def __init__(self, configs: list[FreeTierConfig] | None = None):
        self._configs = configs or []
        self._states = {c.provider: FreeTierState() for c in self._configs}

    def route_to_free_tier(
        self,
        model: str | None,
        task_type: str | None,
        estimated_tokens: int,
        now: float | None = None,
    ) -> dict | None:
        """Try to route to a free-tier endpoint.

        Returns dict with {provider, base_url, key_env, headers, model_name}
        if a free tier can serve this request. Returns None if not eligible
        or all free tiers are unavailable (caller falls through to Kalman).
        """
        import time
        now = now if now is not None else time.time()

        for config in self._configs:
            state = self._states[config.provider]

            # 1. Circuit breaker check
            if not state.is_healthy(config, now):
                continue

            # 2. Task type allowlist
            if task_type and task_type not in config.allowed_task_types:
                continue

            # 3. Token count cap
            if estimated_tokens > config.max_input_tokens:
                continue

            # 4. Model eligibility
            if model and model not in config.allowed_models:
                continue

            # 5. RPM availability
            if state.rpm_available(config, now) <= 0:
                continue

            # All checks pass — route to this free tier
            state.request_timestamps.append(now)
            return {
                "provider": config.provider,
                "base_url": config.base_url,
                "key_env": config.key_env,
                "headers": config.headers,
                "model_name": self._pick_model(config, model, task_type),
                "_state": state,  # caller calls record_success/failure
            }

        return None  # fall through to Kalman routing

    @staticmethod
    def _pick_model(config: FreeTierConfig, model: str | None,
                    task_type: str | None) -> str:
        """Map requested model to free-tier model name."""
        # Simple mapping: add :free suffix for OpenRouter
        if model and model.startswith("glm-"):
            return f"zai-org/{model}:free"
        return "zai-org/GLM-4.5-flash:free"  # safe default

    def record_result(self, provider: str, success: bool, now: float | None = None):
        """Feed back the result of a free-tier request."""
        import time
        now = now if now is not None else time.time()
        state = self._states.get(provider)
        if not state:
            return
        config = next((c for c in self._configs if c.provider == provider), None)
        if not config:
            return
        if success:
            state.record_success(now)
        else:
            state.record_failure(now, config)
```

### Config (in providers.yaml)

```yaml
free_tier_filter:
  endpoints:
    - provider: "openrouter_free"
      base_url: "https://openrouter.ai/api/v1"
      key_env: "OPENROUTER_API_KEY"
      headers: { HTTP-Referer: "https://hermes.local", X-Title: "Hermes Agent" }
      max_input_tokens: 8000
      allowed_task_types: [chat, simple]
      allowed_models: ["glm-4.5-flash"]
      rpm_limit: 20
      circuit_breaker_threshold: 5
      circuit_breaker_cooldown_seconds: 300
```

### Proxy Integration (~10 lines)

```python
# In the proxy (zai_proxy.py), before the existing best_key() call:

free_filter = FreeTierFilter.get_instance()  # singleton

free_route = free_filter.route_to_free_tier(
    model=requested_model,
    task_type=task_type,
    estimated_tokens=estimate_tokens(request),
)

if free_route:
    try:
        response = forward_to_provider(free_route, request)
        free_filter.record_result(free_route["provider"], success=True)
        return response
    except Exception:
        free_filter.record_result(free_route["provider"], success=False)
        # Fall through to normal routing

# Normal Kalman routing path (unchanged)
key = best_key(model=requested_model, ...)
```

---

## Comparison

| Dimension | Approach A (Full Kalman) | Approach B (Pre-Proxy Filter) |
|-----------|-------------------------|------------------------------|
| **Lines of code** | ~35-85 across 6-8 files | ~160 in 1 new file + ~10 in proxy |
| **Files touched** | 6-8 (primary_router, providers.yaml, provider_names, shadow_logger, pricing_engine, model_mapping) | 2 (new free_tier_filter.py + proxy hook) |
| **New concepts** | Request-count Kalman, free-tier pricing_model, RPM-aware scarcity | Sliding-window RPM counter (stdlib only) |
| **Kalman system impact** | Adds a constant-value provider to every routing decision | Zero — Kalman system untouched |
| **Rate limit handling** | Requires new RequestCountKalman OR relies on 429→failure_count (reactive) | Proactive sliding-window RPM tracking (built-in) |
| **Value capture** | 100% (optimizer sees $0.001/M, picks it when cheapest) | ~80% (only small/chat/simple requests, but that's where free tiers are useful) |
| **Maintenance burden** | High — 12 integration points to keep in sync, Kalman state for constants, semantic mismatch between token-scarcity and request-scarcity | Low — one isolated module, clear interface, no coupling |
| **Failover** | Built-in (optimizer falls through to next cheapest) | Built-in (filter returns None → Kalman routing) |
| **Observability** | Full shadow logging, A/B comparison | Manual logging (would need adding) |
| **Quality gate** | Uses existing quality_tiers (but free models need to be added) | Uses its own allowed_models/allowed_task_types |
| **CPVO readiness** | Yes — quality probes would measure free-tier output | No — would need separate probe path |
| **Reversibility** | Hard — removing a provider from 12 points is error-prone | Trivial — delete the filter or set endpoints: [] |

### Value Capture Analysis

What savings does each approach capture?

**Traffic profile** (estimated):
- 70% of requests are chat/simple (low difficulty, <8k tokens)
- 20% are coding tasks (high difficulty, variable tokens)
- 10% are reasoning tasks (high difficulty, large context)

**Approach A** captures 100% of free-tier eligible traffic — the optimizer
would route any request where the free tier is viable (correct model, healthy,
not exhausted). But free tiers only offer models at the "low" quality tier,
so the quality_tier gate already excludes them from high/medium difficulty
tasks. **Effective capture: ~70%** (the chat/simple bucket).

**Approach B** explicitly filters for small/chat/simple requests, which is
exactly the same ~70% bucket. The difference is that Approach A lets the
optimizer make the call per-request, while Approach B uses a static filter.

**The 30% difference is illusory.** Free tiers don't serve high-difficulty
tasks (wrong models), large-context requests (context window cap), or
reasoning tasks (quality risk). Both approaches end up routing the same
traffic to free tiers.

### Maintenance Burden Analysis

**Approach A** introduces a semantic mismatch that will cause bugs:
- `ConsumptionKalman.will_exhaust()` will never fire for free tiers (inf quota)
- `scarcity_factor` is always 1.0 (no token scarcity)
- `pace_factor` is always 1.0 (no token quota windows)
- But rate limits ARE a form of scarcity/exhaustion — just in a different unit

This means either:
1. Free tiers are second-class citizens in the Kalman system (their rate limits
   are invisible to the optimizer), OR
2. You build a parallel request-count tracking system (doubling the infrastructure)

Option 1 defeats the purpose of integration. Option 2 is more work than
Approach B for the same outcome.

**Approach B** has one moving part: the sliding-window RPM counter. It's
~20 lines of stdlib code with no dependencies on the Kalman system. If the
free tier changes terms, you update one config block. If you want to remove
it, you delete the filter.

---

## Recommendation

### Use Approach B (Pre-Proxy Filter)

**Rationale:**

1. **The Kalman system optimizes for dynamic cost.** Free tiers have constant
   $0 cost. Integrating a constant into a smoothing filter is architecturally
   wrong — it's like adding a fixed-speed road to a traffic-aware GPS.

2. **The oxalpha promo_tier.py already validated this pattern.** It's a pure
   guard module that sits alongside the Kalman system, not inside it. The free
   tier filter is a generalization of the same approach.

3. **The 12 integration points are not worth it for a $0 provider.** Each
   integration point is a maintenance contract. The Kalman system's value is
   in comparing *paid* providers with *dynamic* costs — adding a constant
   to every comparison adds overhead without information.

4. **Rate limits are request-count-based, not token-based.** The existing
   ConsumptionKalman tracks token burn. Retrofitting it for request counting
   is a semantic mismatch that will cause confusion and bugs.

5. **The pre-proxy filter captures the same ~70% of traffic** that full
   integration would, because free tiers only serve low-difficulty tasks
   with small context — which is exactly what the filter targets.

### Implementation Plan

| Phase | Task | Effort |
|-------|------|--------|
| 1 | Create `src/free_tier_filter.py` (~150 lines) | 2h |
| 2 | Add `free_tier_filter` config block to `providers.yaml` | 15min |
| 3 | Hook into `zai_proxy.py` before `best_key()` call (~10 lines) | 1h |
| 4 | Add basic logging (free-tier hit/miss/failover) | 30min |
| 5 | Tests (RPM sliding window, circuit breaker, eligibility) | 2h |
| **Total** | | **~6h** |

### When to reconsider Approach A

Switch to full Kalman integration if:
- Free tiers start offering high-quality models at scale (unlikely — that's
  the paid tier's business model)
- You need to compare free tiers against each other on quality (CPVO probes
  would need to be built first anyway)
- The number of free-tier endpoints grows to >5 (then the filter becomes
  complex enough that optimizer-based selection adds value)
- Free tiers introduce token-based quotas (then the ConsumptionKalman would
  actually have something to track)

Until any of those conditions are met, **Approach B is the right choice.**
