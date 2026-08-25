# Free-Tier Endpoint Pricing Analysis: Kalman Model Modifications for Request-Limited Free Endpoints

> **Date:** 2026-08-22
> **Question:** Can the Kalman pricing model maximize output from free endpoints (e.g., OpenRouter free GLM 5.2, ~50 req/day, 256k context cap) before switching to paid versions?
> **Status:** Analysis complete — **NO-GO** for full Kalman integration; **GO** for binary cliff with capacity gate

---

## 0. System Summary (as-found)

The existing pricing model has three layers:

1. **Kalman estimation** — `PriceKalman` smooths the base $/M rate per provider
2. **Deterministic multipliers** — `peak_multiplier`, `scarcity_factor`, `health_pricing_factor`, `pace_factor`, `quota_pressure_factor` — all pure functions composed multiplicatively:
   ```
   effective_price = base_rate × peak × scarcity × health × pace × quota_pressure
   ```
3. **argmin routing** — `RoutingOptimizer` sorts providers by `effective_price`, filters unviable (inf price) ones

The `quota_pressure_factor` uses the **RP-EXP rational asymptotic curve**:

```
pressure(u) = 1.0                          if u ≤ onset
            = 1 + K·t/(1−t)               if onset < u < 1.0    (t = (u−onset)/(1−onset))
            = +∞                           if u ≥ 1.0  (hard_limit=True)

where K = asymptote − 1.0
```

Multiple windows (5h, weekly, monthly) are **superimposed by multiplication**:
```
P_total = P(u_5h) × P(u_weekly) × P(u_monthly)
```

Current parameters (Felix's final decision, Aug 5):
- **All endpoints: asymptote = 1.5** (uniform — squeeze cheap keys as long as possible)
- **Onset:** z.ai=0.60, ollama=0.70, credit-based=0.80
- **Free providers:** base_rate = ε = $0.001/M (MIN_EFFECTIVE_PRICE, ADR-004)

---

## 1. Can the existing quota_pressure_factor model request-count depletion?

### Answer: YES — trivially. The usage fraction `u` is dimensionless.

The `quota_pressure_factor(usage, weekly, monthly, ...)` function takes **dimensionless depletion fractions** (0.0–1.0+). It does not care whether the numerator is tokens or requests — only that `u = consumed / total`.

For a free endpoint with ~50 req/day:

```
u_req = requests_used_today / 50
```

This is a **daily rolling window** (resets every 24h). It maps directly into the superposition framework as a new window:

```python
# Free endpoint: single window (request count, daily)
pressure = quota_pressure_factor(
    usage=u_req,           # daily request depletion
    weekly=None,           # no weekly window
    monthly=None,          # no monthly window
    onset=FREE_TIER_ONSET,  # see §2 for optimal value
    asymptote=1.5,         # uniform (Felix's decision)
    hard_limit=True,       # 50 req = hard 429, no extra-usage path
)
```

**What changes are needed:**
- A new **request counter** (sqlite table or in-memory dict) tracking daily request count per free endpoint
- A new window source in `live_router.py` that feeds `u_req` instead of `u_tokens`
- No change to `quota_pressure_factor` itself — its signature already accepts arbitrary usage fractions

**What does NOT change:**
- The RP-EXP curve formula
- The superposition multiplication
- The routing optimizer
- The health/circuit-breaker system

**Estimated code change:** ~40 LOC (request counter + window source wiring), consistent with the ~60 LOC estimate in `endpoint-universal-pressure.md` §4.3 for similar per-provider wiring.

---

## 2. At what request-count usage does pressure make the free endpoint more expensive than z.ai?

### Setup

- Free endpoint: `base_rate = $0.001/M`, `asymptote = 1.5`, `onset = θ`
- z.ai: `base_rate = $0.0143/M` (amortized from $155/mo)
- Price ratio: `R = 0.0143 / 0.001 = 14.3×`

The free endpoint's effective price is:

```
P_free(u) = $0.001 × pressure(u)
```

We need the crossover where `P_free(u) = $0.0143`:

```
$0.001 × pressure(u) = $0.0143
pressure(u) = 14.3
```

### Solving the RP-EXP equation

With `K = asymptote − 1 = 0.5`:

```
1 + 0.5 · t/(1−t) = 14.3
0.5 · t/(1−t) = 13.3
t/(1−t) = 26.6
t = 26.6(1 − t)
t = 26.6 − 26.6t
t(1 + 26.6) = 26.6
t = 26.6 / 27.6
t* = 0.9638
```

Converting back to `u`:

```
u* = onset + t* × (1 − onset)
```

**With onset = 0.80 (credit-based default):**

```
u* = 0.80 + 0.9638 × 0.20 = 0.80 + 0.1928 = 0.9928
```

→ **Crossover at 99.28% usage = 49.6 out of 50 requests.**

**With onset = 0.60 (z.ai-style aggressive onset):**

```
u* = 0.60 + 0.9638 × 0.40 = 0.60 + 0.3855 = 0.9855
```

→ **Crossover at 98.55% usage = 49.3 out of 50 requests.**

### Pressure curve visualization (onset=0.80, asymptote=1.5)

```
Requests   u      pressure   P_free($/M)   vs z.ai($0.0143)
─────────  ────   ────────   ───────────   ────────────────
  0/50     0.00   1.000      0.00100       FREE (14.3× cheaper)
 10/50     0.20   1.000      0.00100       FREE
 25/50     0.50   1.000      0.00100       FREE
 40/50     0.80   1.000      0.00100       FREE (onset — no penalty yet)
 44/50     0.88   1.333      0.00133       FREE (10.7× cheaper)
 46/50     0.92   2.125      0.00213       FREE (6.7× cheaper)
 48/50     0.96   5.000      0.00500       FREE (2.9× cheaper)
 49/50     0.98   5.500      0.00550       FREE (2.6× cheaper)  ← see note
 49/50     0.98   13.30      0.01330       FREE (1.07× cheaper)  ← corrected
49.6/50    0.9928 14.30      0.01430       CROSSOVER
 50/50     1.00   +∞         +∞            UNREACHABLE (429)
```

Wait — let me recalculate more carefully. At u=0.98, t = (0.98-0.80)/0.20 = 0.90:

```
pressure(0.98) = 1 + 0.5 × 0.90/(1-0.90) = 1 + 0.5 × 9.0 = 1 + 4.5 = 5.5
```

At u=0.99, t = (0.99-0.80)/0.20 = 0.95:

```
pressure(0.99) = 1 + 0.5 × 0.95/0.05 = 1 + 0.5 × 19.0 = 1 + 9.5 = 10.5
```

At u=0.9928, t = 0.9638:

```
pressure(0.9928) = 1 + 0.5 × 0.9638/0.0362 = 1 + 0.5 × 26.6 = 1 + 13.3 = 14.3  ✓
```

### Corrected pressure table (onset=0.80, asymptote=1.5, K=0.5)

| Requests | u    | t     | pressure | P_free ($/M) | vs z.ai ($0.0143) |
|----------|------|-------|----------|-------------|-------------------|
| 0/50     | 0.00 | —     | 1.000    | 0.00100     | 14.3× cheaper      |
| 25/50    | 0.50 | —     | 1.000    | 0.00100     | 14.3× cheaper      |
| 40/50    | 0.80 | 0.000 | 1.000    | 0.00100     | 14.3× cheaper      |
| 44/50    | 0.88 | 0.400 | 1.333    | 0.00133     | 10.7× cheaper      |
| 46/50    | 0.92 | 0.600 | 1.750    | 0.00175     | 8.2× cheaper       |
| 47/50    | 0.94 | 0.700 | 2.167    | 0.00217     | 6.6× cheaper       |
| 48/50    | 0.96 | 0.800 | 3.000    | 0.00300     | 4.8× cheaper       |
| 49/50    | 0.98 | 0.900 | 5.500    | 0.00550     | 2.6× cheaper       |
| 49.5/50  | 0.99 | 0.950 | 10.500   | 0.01050     | 1.36× cheaper      |
| **49.6/50** | **0.9928** | **0.964** | **14.30** | **0.01430** | **CROSSOVER**    |
| 50/50    | 1.00 | —     | +∞       | +∞          | UNREACHABLE        |

### Key finding

The free endpoint stays cheaper than z.ai until **49.6 out of 50 requests** — that is, until you've used 99.28% of the daily request budget. The pressure curve only causes a switch in the **last 0.4 requests** of the 50-request day.

This is a direct manifestation of **pitfall #24 (SCARCITY IS INERT AT LARGE RATE SPREADS)**: when providers are 14.3× apart in cost, the pressure curve is essentially flat until the very edge of exhaustion.

---

## 3. How should the context window cap (256k vs 1M) be modeled?

### Answer: Capacity gate, NOT health_factor or price multiplier.

The context window cap is a **HARD capacity constraint**: a request needing >256k tokens simply cannot be served by the free endpoint. No price increase can change this — it's a physical limitation of the endpoint, not a reliability issue.

This is the same pattern as the existing **exhaustion gate** in `routing_optimizer._evaluate_provider` (lines 316-329):

```python
# Existing exhaustion gate:
will_exhaust, _eta = ck.will_exhaust(
    provider["quota_remaining"], self._exhaustion_horizon
)
if will_exhaust and provider["quota_remaining"] < estimated_tokens:
    return (float("inf"), False, "quota will exhaust before request")
```

The context cap is analogous:

```python
# NEW capacity gate (proposed):
ctx_cap = provider.get("context_window_cap")
if ctx_cap and estimated_tokens > ctx_cap:
    return (float("inf"), False,
            f"request needs ~{estimated_tokens} tokens but "
            f"context cap is {ctx_cap}")
```

### Why NOT a health_factor

- **health_factor** models *reliability* — a struggling provider that might fail. It's graduated (1.0 → 1.5 → 3.0 → 10.0 → +inf) to progressively deprioritize.
- **Context cap** is *capacity* — the endpoint literally cannot serve the request, regardless of health. It's binary: fits or doesn't.

A health_factor of +inf (circuit breaker) is reserved for "this provider is unreachable." Using it for "this provider can't fit this request" conflates two failure modes and makes debugging harder (was the breaker tripped, or was the context too large?).

### Why NOT a price multiplier

A price multiplier (`inf` for oversized requests) is mathematically equivalent to the capacity gate — both return `inf` and make the provider unviable. But the capacity gate is **cheaper to compute** (no Kalman/multiplier chain) and **more diagnostic** (the reason string says "context cap" vs just "infinite price").

### Implementation

Add `context_window_cap` to the `add_provider()` call:

```python
optimizer.add_provider(
    name="free_glm52",
    price_kalman=pk,
    consumption_kalman=ck,
    quota_remaining=float("inf"),  # request-limited, not token-limited
    context_window_cap=256_000,    # 256k tokens — HARD constraint
    model_tier="high",
    model="glm-5.2",
    ...
)
```

And add the gate in `_evaluate_provider()` before the scarcity/price computation (after the quality tier gate, before the health gate — it's a cheaper check).

**Estimated code change:** ~8 LOC (one `if` block + one parameter).

---

## 4. Should latency variance be a CPVO quality penalty?

### The problem

The free endpoint is shared among thousands of users. Latency ranges from 50ms to 30s+. This is not a reliability problem (the response is valid when it arrives) — it's a **user experience** problem.

### Existing CPVO framework

The `CPVOCalculator` (in `cpvo_calculator.py`) computes:

```
effective_rate = base_rate / success_rate    (when success_rate < 0.95)
effective_rate = base_rate                     (when success_rate ≥ 0.95)
```

Where `success_rate = successful_requests / total_requests`.

A "successful" request is one where `response_valid = True`. If we define "valid" to include a **latency threshold**, the CPVO framework naturally handles erratic latency:

### Proposed formula: Latency as validity

Define a request as **invalid** if:
- `latency_ms > LATENCY_TIMEOUT_MS` (e.g., 25,000 ms = 25 seconds)
- OR the response is empty/malformed (existing definition)

Then:

```
success_rate = COUNT(latency_ms ≤ 25000 AND response_valid) / COUNT(*)
effective_rate = base_rate / success_rate    (when < 0.95)
```

### Worked example

Assume the free endpoint has the following latency distribution:
- 70% of requests complete in <5s (valid)
- 20% take 5-25s (valid but slow — still "valid")
- 10% take >25s (invalid — timed out)

Then `success_rate = 0.90`, and:

```
effective_rate = $0.001 / 0.90 = $0.00111/M
```

This is still 12.9× cheaper than z.ai ($0.0143/M). The penalty barely matters.

If the endpoint degrades further (50% timeout):

```
effective_rate = $0.001 / 0.50 = $0.002/M
```

Still 7.2× cheaper than z.ai.

Only at extreme degradation (success_rate < 7%) does the CPVO penalty make the free endpoint more expensive than z.ai:

```
$0.001 / success_rate > $0.0143
success_rate < 0.001 / 0.0143 = 0.0699
```

→ The free endpoint needs **93%+ failure rate** before CPVO pricing makes z.ai cheaper.

### Alternative: latency-weighted CPVO

A more nuanced formula could weight by latency directly:

```
latency_penalty = max(1.0, avg_latency_ms / target_latency_ms)
effective_rate = base_rate × latency_penalty
```

With `target_latency_ms = 2000` (2s) and `avg_latency_ms = 10000` (10s):

```
latency_penalty = 10000 / 2000 = 5.0
effective_rate = $0.001 × 5.0 = $0.005/M
```

Still 2.9× cheaper than z.ai.

### Recommendation: Use the existing CPVO with latency-as-validity

The existing CPVO framework already handles this. No new formula is needed:

1. Define `response_valid = False` when `latency_ms > TIMEOUT` (e.g., 25s)
2. The CPVO calculator naturally penalizes via `1/success_rate`
3. The graduated `health_pricing_factor` (3-5 consecutive timeouts → 3.0×, 6-10 → 10×, >10 → +inf) provides circuit-breaking for severe degradation

The latency-weighted formula adds complexity for marginal benefit — the 14.3× price spread (pitfall #24 again) makes most latency penalties irrelevant until the endpoint is nearly non-functional.

---

## 5. Is binary cliff (use free until 429, then switch) actually optimal?

### The mathematical case

**Pitfall #24** states: "SCARCITY IS INERT AT LARGE RATE SPREADS" — when providers are 30× apart in cost, binary cliff IS optimal.

Our spread is 14.3× ($0.001 vs $0.0143). Let's formalize the comparison.

**Binary cliff strategy:** Use the free endpoint for all 50 requests. On 429 (request 51), switch to z.ai. No pricing model needed — just the existing backoff/circuit-breaker.

**Kalman pricing strategy:** Use the pressure curve to switch to z.ai at `u* = 0.9928` (49.6/50 requests). The pressure curve causes a smooth transition between request 49 and 50.

### Savings comparison

The Kalman approach uses **0.4 fewer free requests** per day than the binary cliff (49.6 vs 50). The marginal savings are:

```
ΔS = 0.4 requests × avg_tokens_per_request × ($0.0143 − $0.001) / 1M
```

With `avg_tokens_per_request = 50,000`:

```
ΔS = 0.4 × 50,000 × $0.0133 / 1,000,000
ΔS = 0.4 × 0.000665
ΔS = $0.000266/day
ΔS = $0.097/year
```

**The Kalman pricing model saves ~$0.10/year** over the binary cliff. The integration cost (12 integration points per pitfall #62, request counter, pressure wiring, testing) dwarfs this by 3+ orders of magnitude.

### When would Kalman pricing matter?

The pressure curve becomes useful when:
1. **The price spread is small** (e.g., 1.5×) — the crossover happens early in the ramp, giving the smooth transition real room to operate
2. **The quota window is large** (millions of tokens) — the smooth transition prevents premature exhaustion
3. **Multiple windows compound** — the superposition makes the combined pressure steeper

None of these apply to the free endpoint:
- Price spread: 14.3× (huge — pitfall #24 regime)
- Quota window: 50 requests (tiny — the ramp occupies 0.4 requests)
- Single window (no superposition compounding)

### The 429 mid-stream risk

The binary cliff uses all 50 requests, which means the 51st request hits a 429. If the 429 arrives **mid-stream** (after the connection is established), the user gets a truncated response.

The Kalman approach avoids this by switching at request 49.6 — but the 0.4-request margin is so thin that a **burst of concurrent requests** (common in agentic workloads) could easily blow through it:

- If 3 requests arrive simultaneously at u=0.98, all 3 might be dispatched to the free endpoint before any of them updates the counter, consuming requests 49, 50, and 51 → the 51st gets a 429 mid-stream.

The Kalman approach doesn't meaningfully reduce this risk because the margin (0.4 requests) is smaller than typical burst sizes.

### The existing health system already handles the real risks

The existing `_BACKOFF_SEQUENCE` (2→4→8→16→32→60s) and `health_pricing_factor` (1.5→3.0→10.0→+inf) already handle:
- 429 burst protection (exponential backoff)
- Circuit-breaking after 10+ consecutive failures (health = +inf)
- Progressive deprioritization (3-5 failures → 3.0×, making z.ai cheaper even without the pressure curve)

The binary cliff + existing health system captures ~99.2% of the value at ~5% of the complexity.

### Formal optimality argument

**Theorem:** For a free endpoint with base_rate `ε` and daily limit `N` requests, competing against a paid endpoint with base_rate `p >> ε`, the binary cliff strategy (drain free until 429, then switch) is ε-optimal when `p/ε > 1/(1 − u*)` where `u*` is the pressure-curve crossover point.

**Proof sketch:** The pressure curve causes a switch at `u*` where `ε × pressure(u*) = p`. The binary cliff switches at `u = 1.0` (429). The difference is `(1 − u*) × N` requests. When `p/ε >> 1` (here, 14.3×), `u* → 1.0` and the difference → 0. The marginal savings → 0 while the complexity cost is constant. ∎

**Conclusion: Binary cliff is optimal for free tiers.** The 14.3× price spread makes the pressure curve inert (pitfall #24). The marginal savings from the smooth transition are ~$0.10/year. The existing health/circuit-breaker system handles the real risks (429s, latency, mid-stream failures).

---

## 6. GO/NO-GO Assessment

### Economic analysis

| Metric | Value |
|--------|-------|
| Free endpoint daily capacity | 50 requests |
| Avg tokens per request (est.) | 50,000 |
| Daily token capacity | 2.5M tokens |
| z.ai effective rate | $0.0143/M |
| **Daily savings (vs z.ai)** | **$0.036/day** |
| **Annual savings** | **$13.10/year** |
| Kalman pricing marginal advantage over binary cliff | $0.10/year |
| Integration cost (12 points × pitfall #62) | ~2-4 engineering hours |

### Risk analysis

| Risk | Severity | Mitigation | Binary cliff handles? |
|------|----------|------------|----------------------|
| 429 mid-stream truncation | Medium | Backoff + retry on z.ai | ✅ (existing 429 handler) |
| Context window truncation (256k) | Low | Capacity gate | ❌ (needs new gate) |
| Latency variance (50ms-30s) | Medium | CPVO + health_pricing_factor | ✅ (existing health system) |
| Model substitution bait-and-switch | Low | Response validation | ✅ (existing token audit) |
| Rate limit changes without notice | Medium | Circuit breaker (5 fails → +inf) | ✅ (existing breaker) |
| Concurrent burst blows through quota | Medium | Margin too thin regardless | ❌ (neither approach helps) |

### Decision matrix

| Criterion | Binary cliff + capacity gate | Full Kalman pricing |
|-----------|----------------------------|-------------------|
| Savings captured | ~99.2% | 100% |
| Complexity (LOC) | ~20 (capacity gate + provider reg) | ~120 (counter + pressure + tests) |
| Integration points | ~3 (provider reg, context gate, 429 handler) | ~12 (pitfall #62) |
| Risk of regression | Low (additive, no existing path changes) | Medium (pressure superposition changes) |
| Maintenance burden | Low (stateless) | Medium (request counter state) |
| Annual ROI | $13.10 / 2 hours | $13.10 / 4 hours + $0.10 marginal |

### **VERDICT: NO-GO for full Kalman integration. GO for lightweight binary cliff.**

The 14.3× price spread makes the Kalman pressure curve inert (pitfall #24). The marginal value of smooth transition over binary cliff is **$0.10/year** — less than the electricity cost of running the request counter. The free endpoint's 50-request daily limit caps total savings at **$13/year**, which is real but small.

### Recommended implementation (lightweight, ~20 LOC)

1. **Register the free endpoint** in `live_router.py` with `base_rate = $0.001/M` (epsilon, ADR-004)
2. **Add a `context_window_cap` capacity gate** to `routing_optimizer._evaluate_provider()` (~8 LOC) — this is the one piece that's NOT handled by the existing system
3. **Use the existing `_BACKOFF_SEQUENCE`** for 429s (binary cliff: drain until 429, then switch)
4. **Use the existing `health_pricing_factor`** for circuit-breaking after repeated 429s/timeouts
5. **Use the existing CPVO calculator** for latency-based deprioritization (mark >25s responses as invalid)
6. **Do NOT** add request-count tracking to the pressure model — the binary cliff is optimal here

### Conditions for upgrading to full Kalman pricing

Revisit this decision if ALL of the following change:
1. The free endpoint's daily limit increases to **>500 requests/day** (making the smooth transition margin meaningful)
2. The price spread narrows to **<3×** (making the pressure curve non-inert)
3. Multiple free endpoints become available with **different rate limits** (requiring inter-free-tier load balancing)
4. The 50-request limit becomes a **soft limit** with extra-usage pricing (like Ollama Cloud's model — then the RP-EXP curve is genuinely useful)

Until then, the binary cliff + capacity gate captures ~99% of the value at ~15% of the complexity.

---

## 7. Existing precedent: the oxalpha promo tier

The codebase already has a **PromoTierGuard** (`src/promo_tier.py`) for the `oxalpha` OpenRouter free promo tier. This is essentially the same pattern — a $0-cost endpoint with hard limits, spend guards, and circuit-breaking. Key design decisions from `promo_tier.py` that apply here:

- **`budget_usd = 0`**: any nonzero charge → immediate disable (anti-routstrd guard)
- **`rate_limit_backoff_s = [60, 120, 300]`**: 429 backoff sequence specific to free tiers (longer than the 2-60s production sequence, because free-tier 429s mean "try again in a minute" not "key exhausted")
- **`circuit_breaker_threshold = 5`**: 5 consecutive 429s → +inf (unreachable)
- **Task-type allowlist**: only certain task types can reach the free tier (data sensitivity)

This is the binary cliff pattern already implemented for a similar use case. The free GLM 5.2 endpoint should follow the same pattern.

---

## 8. Summary of answers

| # | Question | Answer |
|---|----------|--------|
| 1 | Can quota_pressure_factor model request-count depletion? | **YES** — `u` is dimensionless; `u_req = requests_used/50` is just another window. ~40 LOC to wire. |
| 2 | At what usage does pressure make free > z.ai? | **u* = 99.28%** (49.6/50 requests) with onset=0.80, asymptote=1.5. The 14.3× spread makes the curve inert until the last 0.4 requests. |
| 3 | How to model the 256k context cap? | **Capacity gate** in `_evaluate_provider()` — return `inf` when `estimated_tokens > 256k`. NOT a health_factor (different failure mode). ~8 LOC. |
| 4 | Latency variance as CPVO penalty? | **Existing CPVO handles it** — mark >25s responses as `response_valid=False`. `effective_rate = $0.001/success_rate`. Needs 93%+ failure rate to cross z.ai. |
| 5 | Is binary cliff optimal? | **YES** — the 14.3× spread (pitfall #24) makes the pressure curve save ~$0.10/year over binary cliff. Existing health/circuit-breaker handles the real risks. |

## CG-2: Price Exposure — IMPLEMENTED 2026-08-25

### Module: `src/pricing_exposure.py`
- Baselines: z.ai subscription (fee ÷ entitlement × pressure) + flat-tier (routstrd catalog)
- Denominator: `max(capacity_estimate, trailing_30d_tokens)` — prevents $0.001 floor on cold-start
- Pressure: delegated to `pricing_engine` (u_5h × u_week × u_month)
- Forecast: +5/+15/+60 min via Kalman, `?horizon_min=` query param
- Staleness: marker >15 min
- Source tag: `measured|listed|estimated`

### Collector: `scripts/collect_price_observations.py`
- Hourly cron, writes to `price_observations.json`
- Fixture mode for testing

### Endpoint: `GET /v1/pricing` (in zai_proxy.py)
- Returns JSON: per-provider pricing with model, price_per_million_input/output, source
- Params: `?model=`, `?horizon_min=`
- Graceful degradation — never raises

### Config: `config/providers.yaml`
- `friend.monthly_fee_usd: 80` (was 0)

### Tests: 66/66 pass (`tests/test_pricing_exposure.py`)
