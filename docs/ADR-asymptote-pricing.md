# ADR: Asymptote Pricing Strategy

## Metadata

| Field | Value |
|---|---|
| **Status** | ACCEPTED |
| **Date** | 2026-08-05 |
| **Decision Maker** | Felix (product owner) |
| **Supersedes** | `docs/asymptote-preference-analysis.md` (all recommendations void) |
| **Superseded by** | — |
| **Code State** | commit `242d1f8` — constants live in `src/pricing_engine.py` |
| **Related** | `docs/asymptote-revised-analysis.md`, `docs/realtime-pricing-design.md`, `src/pricing_engine.py` |

---

## Context

The merchant routing engine serves traffic across **6 AI inference endpoints**, each
with a fundamentally different cost structure. The routing optimizer selects the
endpoint with the lowest *effective price* on every request. For that comparison to
produce sane routing, each endpoint needs a **price model that rises as its quota or
credit balance depletes** — so the optimizer can (a) squeeze cheap endpoints longest
and (b) divert to alternatives before a hard failure (HTTP 429, $0 balance).

### The Endpoints

| Endpoint | Cost Model | Quota / Balance Windows | Marginal Cost |
|---|---|---|---|
| **z.ai (ours)** | $300/yr flat subscription | 5h session + 7d weekly + 30d monthly | **$0** (sunk) |
| **z.ai (friend)** | Shared key, no direct cost | 5h + 7d + 30d (same as ours) | **$0** (sunk) |
| **Ollama Cloud** | $100/mo + $40 prepaid extra | 5h session + 7d weekly | **$0** within quota; $0.10/M above |
| **DeepInfra** | Pay-per-token from balance | Credits (single pool) | **$0.05/M** (real money) |
| **OpenRouter** | Pay-per-token from balance | Credits (single pool) | **$0.135/M** (real money) |
| **PPQ** | Pay-per-token from balance | Credits (single pool) | **$0.14/M** (real money) |

The central tension: **subscription endpoints (z.ai, Ollama) are sunk cost — every
token is "free" — while paid endpoints (PPQ, DeepInfra, OpenRouter) charge real
money per token from a finite wallet.** Felix's explicit directive:

> *"Make the asymptote really low so the keys flee as late as possible and we use
> them as long as possible. I don't want to use OpenRouter or DeepInfra or PPQ
> unless I absolutely have to."*

This ADR records the decision that implements that directive.

### The Pressure Formula (RP-EXP)

All endpoints share one formula — the **rational asymptotic exponential** curve:

```
effective_price = base_rate × pressure(usage, onset, asymptote)

pressure(u) = ┌ 1.0                       if u ≤ onset
              ├ 1 + K·t/(1−t)             if onset < u < 1.0
              └ +∞                        if u ≥ 1.0  (hard_limit=True)
                base_rate × asymptote      if u ≥ 1.0  (hard_limit=False)

  where  t = (usage − onset) / (1 − onset)     [maps (onset, 1.0) → (0, 1)]
         K = asymptote − 1.0
```

The **asymptote** is the multiplier at the *midpoint* of the ramp (halfway between
onset and 100% usage). A low asymptote → price rises gently → endpoint stays
preferred longer. A high asymptote → price spikes steeply → optimizer reroutes
prematurely.

Multiple quota windows (e.g., z.ai's 5h × weekly × monthly) are **multiplied**
(superimposed), not maxed. This is critical — see §Rationale argument 2.

---

## Decision

### Uniform Low Asymptote: A = 1.5 for ALL endpoints

Every endpoint uses `asymptote = 1.5` (i.e., `K = 0.5`). The asymptote is **not**
used to encode preference between endpoints. Preference lives entirely in the
**base rate**. The asymptote's sole job is to control how gently each endpoint's
price approaches its wall as quota/balance depletes.

### Per-Endpoint Parameters

| Endpoint | Base Rate | Onset | Asymptote | hard_limit | Windows |
|---|---|---|---|---|---|
| **z.ai (ours)** | $0.0143/M (365d amortized) | 0.60 | **1.5** | True | 5h × weekly × monthly |
| **z.ai (friend)** | $0.0173/M (ours × 1.21) | 0.60 | **1.5** | True | 5h × weekly × monthly |
| **Ollama Cloud** | $0.0155/M (90d measured) | 0.70 | **1.5** | False | 5h × weekly |
| **DeepInfra** | $0.05/M (30d measured) | 0.80 | **1.5** | True | credits (single) |
| **OpenRouter** | $0.135/M (per-call) | 0.80 | **1.5** | True | credits (single) |
| **PPQ** | $0.14/M (30d ledger) | 0.80 | **1.5** | True | credits (single) |

**Onset staggering** creates the cascade order (when pressure *begins*):
- z.ai at 0.60 (earliest — its 5h window is tiny, ~2M tokens, exhausts fast)
- Ollama at 0.70 (next — its 5h window is large, ~500M tokens)
- Credit-based at 0.80 (last — credits deplete slowly, refill manually)

**hard_limit semantics:**
- `True` (z.ai, DeepInfra, OpenRouter, PPQ): at 100% usage → price = +∞ → optimizer
  must divert. No extra-usage path exists.
- `False` (Ollama Cloud): at 100% usage → price caps at `asymptote × base` ($0.023/M).
  Ollama allows paid extra usage, so exclusive models (kimi-k3, gpt-oss) stay reachable.

### Routing Priority at Low Usage (No Pressure)

With all endpoints fresh (below onset), pressure = 1.0 everywhere, so the base rate
alone determines priority:

```
z.ai (ours)    $0.0143/M   ← always preferred (cheapest + sunk cost)
Ollama Cloud   $0.0155/M   ← second (also sunk cost; 8% above z.ai)
z.ai (friend)  $0.0173/M   ← third (21% penalty on ours, ADR-005)
─────────────────────────────────────────────────────────────
DeepInfra      $0.0500/M   ← first paid (3.5× z.ai)
OpenRouter     $0.1350/M   ← second paid (9.4× z.ai)
PPQ            $0.1400/M   ← last resort (9.8× z.ai)
```

The line separates **free** (subscriptions) from **paid** (per-token). The optimizer
exhausts ALL free endpoints before touching any paid one — encoded entirely in base
rates, independent of the asymptote.

---

## Rationale

Three independent arguments converge on A = 1.5.

### 1. Sunk Cost Principle

z.ai ($300/yr) and Ollama ($100/mo + $40 prepaid) are **already paid**. Every token
routed through them costs $0 marginal. PPQ, DeepInfra, and OpenRouter charge **real
dollars per token** from a finite wallet. The optimizer's objective is to maximize
free tokens and minimize paid tokens. A low asymptote keeps subscription endpoints
price-competitive until 97–98% depletion, squeezing every last drop of free quota
before falling to paid alternatives.

A high asymptote (e.g., 5.0) would cause subscriptions to appear "expensive" at
75–87% usage — abandoning 10+ percentage points of free quota and paying real money
for tokens that were available for free.

### 2. Superposition Danger (the decisive argument)

z.ai has **three superimposed quota windows** (5h session × 7d weekly × 30d monthly).
The pressures **multiply**, not max. With a high asymptote, the compounded pressure
becomes catastrophic at moderate per-window depletion:

**Scenario: session=90%, weekly=70%, monthly=40%** (realistic mid-month)

| Asymptote | Session factor | Weekly factor | Monthly factor | Combined | Effective $/M | vs PPQ ($0.14) |
|---|---|---|---|---|---|---|
| **1.5** (chosen) | 2.50 | 1.17 | 1.00 | **2.92** | **$0.042** | 3.4× cheaper ✅ |
| 2.0 | 4.00 | 1.33 | 1.00 | 5.33 | $0.076 | 1.8× cheaper ✅ |
| 3.0 (old rec.) | 7.00 | 1.67 | 1.00 | 11.67 | $0.167 | **above PPQ!** ❌ |
| 5.0 | 13.00 | 2.33 | 1.00 | 30.33 | $0.434 | **3× above PPQ!** ❌ |

With **A=3.0** (the previous recommendation), z.ai at 90%/70%/40% would cost $0.167/M
— **more expensive than PPQ ($0.14/M) with a fresh balance**. The router would
abandon free z.ai quota and pay PPQ for tokens — the exact opposite of Felix's intent.

With **A=1.5**, the same scenario costs $0.042/M — 3.4× cheaper than PPQ. The router
correctly stays on z.ai.

> **This is the single strongest argument.** Three compounding windows amplify the
> asymptote's effect cubically. A "reasonable" single-window asymptote of 3.0 becomes
> catastrophic when three windows multiply. A=1.5 keeps the compounded effect
> manageable.

### 3. Preference Lives in Base Rate (not asymptote)

z.ai at $0.014/M is **10× cheaper** than PPQ at $0.14/M. Paid endpoints are last
resort by base rate alone — no asymptote tuning is needed to encode this preference.
The asymptote cannot overcome a 10× base-rate disadvantage:

- PPQ at 50% balance (below onset): pressure = 1.0, price = **$0.14/M**
- z.ai at 50% usage: pressure = 1.0, price = **$0.014/M**
- **z.ai wins** regardless of either endpoint's asymptote.

Using differentiated asymptotes to encode preference would mix a subjective knob with
an objective cost signal, creating configuration ambiguity and risk. The clean
separation — **base rate = preference, asymptote = wall-approach gentleness** — is
simpler and safer.

---

## Crossover Analysis

With A=1.5, z.ai (base $0.0143/M, onset 0.60) stays cheaper than each paid endpoint
until the listed session-usage threshold:

| Competitor | Competitor Base | Price Ratio (m) | z.ai crossover at A=1.5 | z.ai crossover at A=5.0 (rejected) | Free quota lost |
|---|---|---|---|---|---|
| **DeepInfra** | $0.05/M | 3.50× | **93.3%** | 75.4% | 17.9 points |
| **OpenRouter** | $0.135/M | 9.44× | **97.8%** | 87.1% | 10.7 points |
| **PPQ** | $0.14/M | 9.79× | **97.8%** | 87.5% | 10.3 points |

**Crossover formula** (solving `base × pressure(u) = competitor_base` for u):

```
m = competitor_base / base_rate
t = (m − 1) / (asymptote + m − 2)
u = onset + t × (1 − onset)
```

With A=1.5, z.ai stays cheaper than **all** paid endpoints until at least 93% usage.
With A=5.0, it starts losing to DeepInfra at just 75% — **wasting 10+ percentage
points of free quota**.

### Effective Price Curve for z.ai at A=1.5 (onset=0.60)

| Session Usage | Pressure | Effective $/M | vs DeepInfra ($0.05) | vs PPQ ($0.14) |
|---|---|---|---|---|
| 60% (onset) | 1.00× | $0.0143 | 3.5× cheaper | 9.8× cheaper |
| 70% | 1.17× | $0.0167 | 3.0× cheaper | 8.4× cheaper |
| 80% | 1.50× | $0.0214 | 2.3× cheaper | 6.5× cheaper |
| 90% | 2.50× | $0.0357 | 1.4× cheaper | 3.9× cheaper |
| 93% | 3.36× | $0.0480 | ≈ tie | 2.9× cheaper |
| 95% | 4.50× | $0.0643 | above | 2.2× cheaper |
| 97% | 7.17× | $0.1025 | above | 1.4× cheaper |
| 98% | 10.50× | $0.1502 | above | above (by a hair) |
| 99% | 20.50× | $0.2932 | above | above |
| 100% | +∞ | +∞ | — | — |

Even at **95% usage**, z.ai is still 2.2× cheaper than PPQ. The router squeezes z.ai
to ~98% before PPQ becomes the cheaper option.

### The Full Cascade (pushing everything to the wall)

| Step | State | Router picks | Effective $/M |
|---|---|---|---|
| 1 | All subscriptions < 60% | z.ai (ours) | $0.0143 |
| 2 | z.ai ours hits 70%, Ollama fresh | Ollama | $0.0155 |
| 3 | Ollama hits 70%, z.ai friend fresh | z.ai (friend) | $0.0173 |
| 4 | z.ai friend hits 70% | Ollama (cycled) | $0.0194 |
| 5 | Everything at 90% | Ollama | $0.0310 |
| 6 | Everything at 93% | Ollama | $0.0413 |
| 7 | Everything at 95% | **DeepInfra** (subs now $0.05+) | $0.0500 |
| 8 | DeepInfra depletes to 80% | OpenRouter | $0.1350 |
| 9 | OpenRouter depletes to 80% | PPQ | $0.1400 |
| 10 | Everything exhausted | **Error / queue** | +∞ |

The cascade is: **free subscriptions → cheapest paid → most expensive paid → error**.

---

## Alternatives Considered

### A. HIGH asymptote (A=5.0, all endpoints) — REJECTED

Abandons subscriptions too early. z.ai crosses DeepInfra at 75% and PPQ at 87% —
wasting 10+ percentage points of free quota per session. With three superimposed
windows, A=5.0 produces 30× combined pressure at 90%/70%/40% — 3× above PPQ's base
rate, causing the router to pay real money while free quota remains.

**Estimated waste:** ~$62/year in unnecessary PPQ charges (from ~1.2M tokens/day
diverted to paid endpoints prematurely).

### B. DIFFERENTIATED asymptotes (z.ai=2.0–3.0, PPQ=5.0) — REJECTED

The previous analysis (`asymptote-preference-analysis.md`) recommended z.ai=3.0,
Ollama=4.17, PPQ=2.0. This mixes a subjective preference knob with an objective cost
signal, creating configuration ambiguity. The superposition analysis (§Rationale #2)
shows z.ai=3.0 is actively dangerous with three compounding windows. Furthermore,
differentiated asymptotes add unnecessary complexity — the base rate already encodes
all the preference information needed.

### C. SEPARATE preference_weight parameter — REJECTED (deferred to v2)

The previous analysis proposed `effective_asymptote = cost_ratio × preference_weight`,
splitting objective cost from subjective preference. This is conceptually cleaner but
adds a second parameter per endpoint with no practical benefit at current scale.
Felix's uniform-1.5 decision makes the split unnecessary for v1. The migration path
is documented should per-endpoint tuning become desirable later.

### D. EVEN LOWER asymptote (A=1.2) — REJECTED

Makes the superposition extremely gentle (pressure only 6.5× at 99% usage), but the
ramp also barely rises — the "squeeze every last token" signal gets weak near the
wall. A=1.5 is the sweet spot between gentleness and a meaningful pressure gradient.

---

## Balance Tracking for Credit-Based Endpoints

PPQ, DeepInfra, and OpenRouter have no time-based quota windows. Their usage fraction
is derived from **remaining credit balance**:

```python
balance_usage = 1.0 - (remaining_balance / starting_balance)
```

| State | Remaining | Starting | balance_usage | Meaning |
|---|---|---|---|---|
| Full balance | $5.00 | $5.00 | 0.00 | Fresh — no pressure |
| Half spent | $2.50 | $5.00 | 0.50 | Below onset — no pressure |
| 80% spent | $1.00 | $5.00 | 0.80 | At onset — pressure begins |
| 90% spent | $0.50 | $5.00 | 0.90 | Midpoint — price × 1.5 |
| 95% spent | $0.25 | $5.00 | 0.95 | Near-empty — price × 2.5 |
| Exhausted | $0.00 | $5.00 | 1.00 | **+∞ (hard limit)** |

### API Endpoints for Balance Queries

| Endpoint | API Call | Returns | Poll Cadence |
|---|---|---|---|
| **PPQ** | `POST /credits/balance` | `remaining` (credits in $) | Every 5 min (`api_burn_collector`) |
| **DeepInfra** | Billing API: query accumulated `total_spent` | `remaining = budget − spent` | Every 5 min |
| **OpenRouter** | `GET /api/v1/key` | `usage` field → compute `remaining` | Every 5 min |

### Cold-Start Seeding

For paid endpoints with no balance data yet, seed `balance_usage = 0.5` (conservative —
assumes 50% depleted). This biases the router slightly away from unmeasured endpoints
until the first balance query completes (within 5 minutes). For subscription endpoints
(z.ai, Ollama), the quota/usage API is called on startup — no cold-start gap.

---

## Consequences

### Positive

- **z.ai and Ollama are used to 97–98% capacity** before the optimizer diverts to
  paid endpoints — maximizing sunk-cost value.
- **Superimposed 3-window multiplication** keeps z.ai competitive even when all three
  windows are partially depleted (the decisive superposition scenario yields $0.042/M,
  still 3.4× cheaper than PPQ).
- **Paid endpoints are only reached** when ALL subscription endpoints are
  near-exhausted — matching Felix's explicit directive.
- **Saves ~$62/year** vs the rejected high-asymptote (A=5.0) strategy, from not paying
  PPQ prematurely for ~1.2M tokens/day that would otherwise be free.
- **Simple configuration**: one asymptote value (1.5) across all endpoints. Preference
  is encoded entirely in base rates — no per-endpoint tuning needed.

### Negative / Risks

- **Closer to the wall**: with A=1.5, the optimizer keeps routing to a subscription
  endpoint until ~98% usage. If usage spikes suddenly (burst traffic), the endpoint
  may hit 100% (HTTP 429) before the optimizer reacts. Mitigation: the `pace_factor`
  predictive pacer and `hard_limit=True` (+∞ price) provide a backstop.
- **Stale docstring**: `compute_effective_price` parameter docstring for
  `zai_session_usage` (line ~737) still references "asymptote=2.0" — should say 1.5.
  Minor; does not affect behavior.

---

## Implementation Status

| Item | Status | Location / Notes |
|---|---|---|
| RP-EXP formula (`quota_pressure_factor`) | ✅ DONE | `src/pricing_engine.py`, `_single_window_factor` + `quota_pressure_factor` |
| Per-endpoint constants (all A=1.5) | ✅ DONE | `src/pricing_engine.py` lines 165–233 |
| Superimposed windows (multiply, not max) | ✅ DONE | `quota_pressure_factor` collects windows and multiplies |
| z.ai 3-window wiring in `compute_effective_price` | ✅ DONE | `is_zai` branch with `zai_session/weekly/monthly_usage` params |
| Ollama Cloud pressure curve | ✅ DONE | `quota_pressure` param override path |
| z.ai wiring into `live_router.select_failover` | ⏳ PENDING | 4 tests skipped; helper functions exist but not called in production path |
| PPQ credit balance collector | ⏳ PENDING | `api_burn_collector` exists; `balance_usage` → `quota_pressure_factor` not wired |
| DeepInfra billing API integration | ❌ NOT BUILT | Need: query `total_spent`, compute `remaining = $5 − spent` |
| OpenRouter key usage (`GET /api/v1/key`) | ❌ NOT BUILT | Need: parse `usage` field, compute `remaining = $10 − usage` |
| Trailing-365d base rate calculation | ⏳ PENDING | `_measure_zai_amortized` exists; needs 365d-default fallback |
| Stale docstring (`asymptote=2.0` → 1.5) | ⚠️ MINOR | `compute_effective_price` line ~737 |

### Proposed: Paid Endpoint Pressure Helper

```python
def paid_endpoint_pressure(
    remaining_balance: float,
    starting_balance: float,
    onset: float = 0.80,
    asymptote: float = 1.5,
) -> float:
    """Compute pressure for a credit-based paid endpoint.

    balance_usage = 1.0 - (remaining / starting)
    At $0 balance → +inf (hard limit).
    """
    if starting_balance <= 0:
        return math.inf
    balance_usage = 1.0 - (remaining_balance / starting_balance)
    return quota_pressure_factor(
        usage=balance_usage,
        onset=onset,
        asymptote=asymptote,
        hard_limit=True,
    )
```

---

## v2 Extension Path (Deferred)

If per-endpoint preference tuning becomes desirable, the documented migration is:

```python
effective_asymptote = BASE_ASYMPTOTE * preference_weight
# Default preference_weight = 1.0 → asymptote stays 1.5
# preference_weight = 0.8  → asymptote = 1.2  (use even longer)
# preference_weight = 1.5  → asymptote = 2.25 (flee sooner)
```

This is a **v2 concern**. For v1, the uniform 1.5 + base-rate differentiation is
sufficient and matches the stated intent.

---

## Related Documents

- **`docs/asymptote-revised-analysis.md`** — detailed math, 479 lines (the primary
  analytical basis for this decision)
- **`docs/asymptote-preference-analysis.md`** — earlier analysis, **SUPERSEDED** (all
  recommendations void)
- **`docs/realtime-pricing-design.md`** — overall pricing architecture
- **`src/pricing_engine.py`** — implementation (`quota_pressure_factor`,
  `compute_effective_price`, per-endpoint constants)
- **`config/providers.yaml`** — provider definitions and cost structures
