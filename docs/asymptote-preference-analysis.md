# Asymptote-as-Preference Analysis: Universal Pressure Curve for All Endpoints

> **Author:** Pricing systems consultant (subagent)
> **Date:** 2026-08-05
> **Audience:** Felix (visual/spatial thinker — wants tables and concrete numbers)
> **Sister docs:**
> - `docs/quota-pressure-design.md` (RP-EXP formula, Ollama-only)
> - `docs/endpoint-universal-pressure.md` (per-provider onset/asymptote table)
> - `docs/realtime-pricing-design.md` (per-endpoint model vision)

---

## 0. TL;DR — The Verdict Up Front

**Yes, asymptote works as a preference knob — but only in the depletion regime (between onset and 100%). Below onset, asymptote has zero effect; preference there is controlled entirely by `base_rate`.** The two parameters compose naturally:

```
base_rate     → "who do I prefer when everyone is fresh?"   (objective)
asymptote     → "how fast do I back off as quota depletes?" (subjective + objective)
```

**Recommendation:** keep them folded into one parameter for now, but document the *cost_ratio* baseline and add a *preference_weight* multiplier on top. Final asymptote = `cost_ratio × preference_weight`. Cost_ratio is auto-updated from billing data; preference_weight is Felix's knob (default 1.0).

---

## 1. The Formula (Refresher)

The RP-EXP curve, one window:

```
                ┌ 1.0                       if u ≤ onset
pressure(u)  =  ┤ 1 + K · t / (1 − t)       if onset < u < 1.0
                └ +∞                        if u ≥ 1.0

   where t = (u − onset) / (1 − onset)        [maps (onset, 1.0) → (0, 1)]
         K = asymptote − 1.0
```

Multiple windows are **multiplied** (superimposed), not maxed.

### 1.1 What asymptote *actually* controls

The curve passes through three landmarks:

| Landmark | Usage `u` (onset=0.70) | Multiplier |
|---|---|---|
| **Onset** | 0.70 | `1.0` (always — independent of asymptote) |
| **Midpoint** | 0.85 | `asymptote` (literally equals the parameter) |
| **Near-wall** | 0.95 | `1 + 5·(asymptote − 1)` = `5·asymptote − 4` |
| **Wall** | 1.00 | `+∞` (always — independent of asymptote) |

So asymptote is **the multiplier at the midpoint of the ramp**. Below the midpoint, lowering asymptote makes the curve gentler; above the midpoint, lowering asymptote makes the divergence to +∞ less steep (but it still diverges).

### 1.2 Worked multipliers at different asymptote values (onset = 0.70)

| Usage `u` | t | A=1.5 | **A=2.0** | A=3.0 | **A=4.17** (Ollama) | A=5.0 | A=10.0 |
|---|---|---|---|---|---|---|---|
| 0.70 (onset) | 0.000 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| 0.75 | 0.167 | 1.20 | 1.33 | 1.60 | 1.86 | 2.00 | 3.50 |
| 0.80 | 0.333 | 1.25 | 1.50 | 2.00 | 2.58 | 3.00 | 6.50 |
| 0.85 (midpoint) | 0.500 | **1.50** | **2.00** | **3.00** | **4.17** | **5.00** | **10.0** |
| 0.90 | 0.667 | 1.75 | 3.00 | 5.00 | 7.33 | 9.00 | 19.0 |
| 0.95 | 0.833 | 2.75 | 6.00 | 11.0 | 16.8 | 21.0 | 46.0 |
| 0.99 | 0.967 | 14.5 | 30.0 | 59.0 | 93.0 | 117 | 262 |
| 1.00 | 1.000 | +∞ | +∞ | +∞ | +∞ | +∞ | +∞ |

**Visual read:** doubling the asymptote roughly doubles the multiplier at every point in the ramp. At the midpoint it's exactly proportional. Near the wall, all curves converge to +∞ regardless.

---

## 2. Base Rate Calculation (Trailing Data)

The base rate is the **objective anchor** — the price the router compares against when no quota pressure is active (u ≤ onset). Get this wrong and the whole comparison is wrong.

### 2.1 Per-endpoint base rate source

| Endpoint | Formula | Trailing period | Why this period |
|---|---|---|---|
| **z.ai (ours)** | `$300/yr × (months_covered) / trailing_tokens_M` | **365 days** | Yearly subscription — the natural amortization window. Avoids the monthly-sawtooth (rate spikes at month reset, decays as tokens accumulate). |
| **z.ai (friend)** | `$0` shared key; model as `ours × 1.21` (penalty_pct) | n/a | No direct cost — use the same base as ours with the documented 21% premium (ADR-005). |
| **Ollama Cloud** | `total_prepaid_365d / trailing_tokens_M` (includes the $100/mo subscription *plus* any extra-usage burn) | **90 days** | Subscription renews monthly but usage is bursty. 90 days smooths the bursty days (like 2026-08-05: 216M tokens) without overweighting ancient history. |
| **PPQ** | `SUM(cost_usd) / SUM(tokens) FROM ppq_queries` (billing API) | **30 days** | Pure pay-per-query. 30 days captures recent pricing (DeepSeek kimi-k3 may change rates) without overweighting old data. |
| **DeepInfra** | `SUM(estimated_cost) / SUM(tokens)` (per-call) | **30 days** | Same rationale as PPQ — rate may drift with model mix. |
| **OpenRouter** | `SUM(response.usage.cost) / SUM(tokens)` | **30 days** | OpenRouter returns true cost per call — most accurate of all. |

### 2.2 Cold-start behavior (no trailing data yet)

| Endpoint | Cold-start seed | Source |
|---|---|---|
| z.ai | **$0.014/M** (the folklore value: $300/yr ÷ ~21B tokens/yr) | Existing `_measure_zai_amortized` falls back to month-to-date; add a 365d-default fallback. |
| Ollama | **$0.024/M** (current code constant; the real measured value $0.0155/M is 35% lower and will replace it once the tracker converges) | `EXTRA_USAGE_BASE_RATE` constant. |
| PPQ | **$0.14/M** (published) | Cold-start fallback in `_measure_ppq_ledger`. |
| DeepInfra | **$0.05/M** (estimated from model pricing) | Config default. |
| OpenRouter | **$0.135/M** (published) | Config default. |

**Rule:** cold-start seeds are *intentionally conservative* (slightly high). The Kalman filter converges down as real billing data arrives. This biases the router slightly *away* from unmeasured endpoints during the first hours/days — which is safe.

### 2.3 Update cadence

- **z.ai / Ollama amortized:** recompute hourly (the trailing sum is cheap; the divisor changes every hour). Update the Kalman with the new observation.
- **PPQ / DeepInfra / OpenRouter ledger:** update on every API call (`response.cost` arrives in-band) plus a 5-minute background poll for queries we didn't see the cost of.
- **z.ai base rate at year boundary:** when the yearly subscription renews, the numerator resets to $300 — this is a step change in the base rate. The Kalman absorbs it within ~10 observations (an hour of traffic).

### 2.4 Concrete numbers (current)

| Endpoint | Current measured base rate | Notes |
|---|---|---|
| z.ai (ours, 365d) | $300 / 21,000 M = **$0.0143/M** | Matches the $0.014/M folklore. |
| z.ai (friend) | $0.0143 × 1.21 = **$0.0173/M** | 21% premium applied. |
| Ollama (90d) | **$0.0155/M** (measured $38.51 / 2.3B tokens, 4-week window) | 35% below the $0.024/M code constant. |
| PPQ | **$0.14/M** | From `ppq_queries` ledger; dominated by kimi-k3 calls. |
| DeepInfra | **~$0.05/M** | From `estimated_cost` field. |
| OpenRouter | **$0.135/M** | From `response.usage.cost`. |

---

## 3. Asymptote as Preference Knob — Does It Work?

### 3.1 The mathematical case

**It works — with one important caveat.** Asymptote controls the multiplier *during the ramp* (onset → 1.0). It does NOT control preference below onset. So:

```
Preference = f(base_rate, asymptote)

  base_rate  →  preference at LOW usage    (u ≤ onset)
  asymptote  →  preference at HIGH usage   (onset < u < 1.0)
```

These compose multiplicatively in the final price:

```
effective_price(u) = base_rate × pressure(u, asymptote)
                   = base_rate × 1.0                          (u ≤ onset)
                   = base_rate × [1 + K·t/(1−t)]              (onset < u < 1.0)
                   = +∞                                        (u ≥ 1.0, hard_limit)
                   = base_rate × asymptote                     (u ≥ 1.0, no hard_limit)
```

**Consequence:** a low-asymptote endpoint is preferred *only when its quota is depleting*. Below onset, the base_rate does all the work.

### 3.2 Does a low asymptote on a cheap endpoint make it ALWAYS preferred?

**No — and this is the key insight.** Consider PPQ at $0.14/M with asymptote=1.5 (very low, "prefer PPQ"):
- At u=0.50 (well below onset=0.80): pressure=1.0, price = **$0.14/M**.
- z.ai at u=0.50: pressure=1.0, price = **$0.0143/M**.
- **z.ai wins** — 10× cheaper — regardless of PPQ's asymptote.

The asymptote cannot overcome a 10× base-rate disadvantage. **Base rate dominates below onset; asymptote only shapes the depletion regime.**

This is exactly what we want: Felix's preference for z.ai is encoded in z.ai's *low base rate*, not in some tuning knob. The asymptote is a *fine-tuning* knob for the depletion regime.

### 3.3 Trade-offs: asymptote-as-preference vs separate preference multiplier

| Aspect | Asymptote-only (one knob) | Separate preference_weight |
|---|---|---|
| Simplicity | ✅ One parameter per endpoint | ❌ Two parameters per endpoint |
| Auto-update from billing | ❌ Can't — preference is mixed with cost | ✅ cost_ratio auto-updates; preference_weight stays manual |
| Tunability | ⚠️ Have to recompute manually when rates change | ✅ Independent of rate changes |
| Conceptual clarity | ❌ "Asymptote = 4.17" is ambiguous (cost ratio? preference?) | ✅ `cost_ratio=4.17, preference=1.0` is unambiguous |
| Risk of misconfiguration | ⚠️ High — if you lower asymptote to "prefer" an endpoint, you also distort the cost signal | ✅ Low — cost signal is preserved |
| Code change required | None (current code already takes asymptote) | Small: `effective_asymptote = cost_ratio * preference_weight` |

### 3.4 Concrete price curves at three asymptote levels

Showing effective price for three endpoints, each at three asymptote levels, with their real base rates. **Bold = the recommended value.**

#### z.ai (base = $0.0143/M, onset = 0.60)

| Usage | A=2.0 | **A=3.0** (recommended) | A=5.0 |
|---|---|---|---|
| 0.60 (onset) | $0.0143 | $0.0143 | $0.0143 |
| 0.75 | $0.0214 | $0.0250 | $0.0321 |
| 0.80 (midpoint) | $0.0286 | **$0.0429** | $0.0714 |
| 0.90 | $0.0572 | $0.100 | $0.186 |
| 0.95 | $0.114 | $0.186 | $0.315 |
| 1.00 | +∞ | +∞ | +∞ |

**At A=3.0:** z.ai crosses Ollama's $0.0155/M base around u=0.72 (just past onset), and crosses PPQ's $0.14/M around u=0.93. So z.ai remains preferred over PPQ even when z.ai is at 90% — good, we want to squeeze z.ai before falling to PPQ.

#### Ollama (base = $0.0155/M, onset = 0.70)

| Usage | A=2.0 | A=3.0 | **A=4.17** (recommended, = $0.10/$0.024) |
|---|---|---|---|
| 0.70 (onset) | $0.0155 | $0.0155 | $0.0155 |
| 0.80 | $0.0233 | $0.0310 | **$0.0401** |
| 0.85 (midpoint) | $0.0310 | $0.0465 | **$0.0646** |
| 0.90 | $0.0465 | $0.0775 | $0.114 |
| 0.95 | $0.0930 | $0.171 | $0.260 |
| ≥1.00 (hard_limit=False) | $0.0310 (=A×base) | $0.0465 | **$0.0646** (extra-usage rate) |

**At A=4.17:** the midpoint (u=0.85) gives $0.0646/M — between z.ai ($0.0143) and PPQ ($0.14). The crossover with z.ai happens around u=0.74. The crossover with PPQ happens around u=0.94. So the router naturally squeezes Ollama until ~94%, then falls to PPQ. This matches the existing RP-EXP design.

If Felix lowers Ollama to A=2.0 ("prefer Ollama more"): the curve stays flat much longer. Crossover with z.ai doesn't happen until u=0.80. Crossover with PPQ doesn't happen until u=0.97. **Risk:** we burn Ollama deeper into extra-usage territory before rerouting.

#### PPQ (base = $0.14/M, onset = 0.80, credit-based)

| Credits used | A=1.5 | **A=2.0** (recommended) | A=3.0 |
|---|---|---|---|
| 0.80 (onset) | $0.140 | $0.140 | $0.140 |
| 0.85 | $0.175 | $0.210 | $0.280 |
| 0.90 (midpoint) | $0.210 | **$0.280** | $0.420 |
| 0.95 | $0.315 | $0.560 | $0.980 |
| 1.00 | +∞ | +∞ | +∞ |

**At A=2.0:** PPQ is already more expensive than OpenRouter ($0.135) at every usage level — pressure mainly exists to ensure smooth exclusion (not cliff) as credits → 0.

If Felix sets PPQ to A=1.0 (effectively no pressure): PPQ stays at $0.140 until credits hit zero, then hard-stops. **That's the cliff behavior we're trying to avoid.** Even a small A=1.5 gives a smoother handoff.

### 3.5 Recommendation on the trade-off

**Fold them together for v1, separate for v2.**

**v1 (now):** Set asymptote per endpoint as documented in §5. Document the cost_ratio basis in a comment so future maintainers know the rationale.

**v2 (when billing data is reliable):** Refactor to:

```python
effective_asymptote = cost_ratio × preference_weight
```

Where:
- `cost_ratio` is auto-computed from trailing data (e.g., Ollama's `$0.10/$0.024 = 4.17`).
- `preference_weight` defaults to 1.0; Felix sets it to 0.7 (prefer) or 1.5 (deprioritize) per endpoint.

This gives Felix a clean subjective knob that doesn't fight the objective cost signal.

---

## 4. The Asymptote Semantics Problem

### 4.1 The two meanings

| Meaning | Type | Example (Ollama) | Source |
|---|---|---|---|
| **Cost ratio** | Objective | 4.17 = `$0.10/M ÷ $0.024/M` (extra-usage rate ÷ base rate) | Measured from billing API |
| **Preference** | Subjective | "I want to prefer Ollama over PPQ" | Felix's judgment |

These are **different things**. The cost ratio is a fact about the world; the preference is a fact about Felix's priorities.

### 4.2 Why conflating them is dangerous

Suppose Ollama raises its extra-usage rate from $0.10/M to $0.15/M. The cost_ratio becomes 6.25 (= $0.15/$0.024). If asymptote = cost_ratio, then the asymptote auto-updates to 6.25 — but Felix never made a preference decision. The router now treats Ollama as "less preferred" purely because of a price change Felix didn't review.

Conversely: if Felix wants to prefer Ollama more and sets asymptote = 2.0, he has accidentally told the router that Ollama's extra-usage rate is only $0.048/M (= $0.024 × 2.0) — a false cost signal. The router may route traffic to Ollama in extra-usage mode thinking it's cheap, when it's actually billing at $0.10/M.

### 4.3 When conflation is OK

Conflation is acceptable when:
1. The cost_ratio is **stable** (subscription endpoints with no extra-usage path — z.ai, PPQ credits).
2. Felix's preference **happens to align** with the cost_ratio (cheap endpoints are preferred; expensive endpoints are deprioritized — which is the natural outcome anyway).

For Ollama specifically (where the cost_ratio is real and meaningful), conflation is **not** OK. For z.ai (no extra-usage path; the asymptote is purely a "how bad is hitting the wall" model), conflation is fine — there's no objective cost_ratio to corrupt.

### 4.4 Recommendation

**Split into two parameters, with a sensible default that folds them:**

```python
# Per-endpoint config
zai:
  cost_ratio: null          # null = no extra-usage path; use preference_weight directly
  preference_weight: 3.0    # "how bad is hitting the wall" — subjective

ollama_cloud:
  cost_ratio: 4.17          # $0.10 / $0.024 — objective, auto-updatable
  preference_weight: 1.0    # neutral — let cost_ratio govern

ppq:
  cost_ratio: null          # credit-based; no per-token extra rate
  preference_weight: 2.0    # re-funding friction — subjective
```

Resolution:

```python
def resolve_asymptote(cost_ratio, preference_weight):
    if cost_ratio is None:
        return preference_weight
    return cost_ratio * preference_weight
```

This way:
- Ollama's asymptote tracks its real cost_ratio (auto-updated when billing changes).
- Felix's preference_weight is a pure subjective knob that multiplies on top.
- For endpoints without a cost_ratio (z.ai, PPQ), the preference_weight IS the asymptote — exactly the current behavior.

---

## 5. Per-Endpoint Model Specification

### 5.1 Master table

| Endpoint | Base Rate (source) | Trailing | Quota Windows | Asymptote | hard_limit | Peak/Other | Rationale |
|---|---|---|---|---|---|---|---|
| **z.ai (ours)** | $0.0143/M ($300/yr ÷ 21B tokens) | 365d | 5h session + 7d weekly + 30d monthly (all three superimposed) | **3.0** | **True** | Peak 3.0× (UTC 06:00–09:59) | Primary provider. A=3.0 ties to peak multiplier (off-peak-pressure ≈ peak-price). Hard limit = 429s at wall. |
| **z.ai (friend)** | $0.0173/M (ours × 1.21) | 365d | Same as ours | **3.0** | **True** | Peak 3.0× | Same model as ours; 21% premium (ADR-005). |
| **Ollama Cloud** | $0.0155/M (measured $38.51/2.3B tokens) | 90d | 5h session + 7d weekly (superimposed) | **4.17** (= $0.10/$0.024) | **False** | None | Has extra-usage path — stays reachable at extra rate. Asymptote = real cost ratio. |
| **PPQ** | $0.14/M (from `ppq_queries` ledger) | 30d | Credits (single u: `1 − remaining/start`) | **2.0** | **True** | None | Hard stop at $0 balance. A=2.0 models re-funding friction. |
| **DeepInfra** | ~$0.05/M (from `estimated_cost`) | 30d | Credits (single u) | **2.0** | **True** | None | Same model as PPQ. Lower base rate → preferred over PPQ/OpenRouter at low usage. |
| **OpenRouter** | $0.135/M (from `response.usage.cost`) | 30d | Credits (single u) | **2.0** | **True** | None | Same model as PPQ. |

### 5.2 Per-endpoint rationale

#### z.ai (ours + friend) — A=3.0, hard_limit=True

- **Base rate:** trailing-365d amortization. $300/yr ÷ 21B tokens = $0.0143/M. Update hourly.
- **Why 3.0 (not 4.17 like Ollama):** z.ai has NO extra-usage tier. Once the 5h window is full, you get 429s. The asymptote models "how painful is hitting the wall" — not a cost ratio. 3.0 ties to the peak multiplier (3.0×), so off-peak-pressure at the midpoint (u=0.80) makes z.ai cost as much as peak pricing: $0.0143 × 3.0 = $0.043/M. This is the natural crossover signal.
- **Why hard_limit=True:** z.ai returns 429 at quota exhaustion. There's no paid fallback. The router MUST divert to the friend key, then Ollama, then externals.
- **Three windows:** superimpose all three (5h × weekly × monthly). The monthly window is the billing period — it's a soft signal, but including it adds conservatism (a triple-depleted z.ai is genuinely in trouble).
- **Peak multiplier:** 3.0× during UTC 06:00–09:59. Applied as a separate deterministic multiplier (not folded into asymptote).

#### Ollama Cloud — A=4.17, hard_limit=False

- **Base rate:** trailing-90d amortized cost. Currently $0.0155/M (measured). Will drift as usage patterns change.
- **Why 4.17:** this IS the cost ratio. $0.10/M (extra-usage rate) ÷ $0.024/M (base) = 4.17. It's objective. The curve crosses the extra-usage rate at the midpoint (u=0.85) and diverges past it.
- **Why hard_limit=False:** Ollama allows extra usage at the published rate. Exclusive models (kimi-k3, gpt-oss) MUST remain reachable even when quota is full. The `live_router` short-circuits exclusive models before price comparison, so the +∞ never blocks them.
- **Two windows:** 5h session × 7d weekly (superimposed). No monthly window.
- **Special case — kimi models:** always billed at extra rate (never included in subscription). Modeled as a flat `asymptote` multiplier, no ramp.

#### PPQ — A=2.0, hard_limit=True

- **Base rate:** trailing-30d from `ppq_queries` ledger. `SUM(cost_usd) / SUM(tokens)`.
- **Why 2.0 (not higher):** PPQ is already expensive ($0.14/M vs z.ai $0.0143/M). The asymptote's job is just to ensure smooth exclusion as credits deplete — not to make PPQ more expensive than it already is. A=2.0 means at the midpoint (90% credits used) PPQ costs $0.28/M — clearly more expensive than DeepInfra ($0.05/M) and OpenRouter ($0.135/M), so the router falls through naturally.
- **Why hard_limit=True:** at $0 balance, PPQ returns an error. Hard stop.
- **Single window:** credits are a monotonic-depleting pool. `u = 1 − (remaining / start)`. No time windows.

#### DeepInfra — A=2.0, hard_limit=True

- **Base rate:** trailing-30d from `estimated_cost` field per call.
- **Why 2.0:** same model as PPQ. DeepInfra has the lowest base rate of the externals ($0.05/M), so it's the *preferred* external. A=2.0 keeps it preferred until credits are ~90% gone.
- **Why hard_limit=True:** balance-based — hard stop at $0.

#### OpenRouter — A=2.0, hard_limit=True

- **Base rate:** trailing-30d from `response.usage.cost` (most accurate source — OpenRouter returns true cost per call).
- **Why 2.0:** same model. OpenRouter is the middle-priced external ($0.135/M) — between DeepInfra ($0.05) and PPQ ($0.14).
- **Why hard_limit=True:** balance-based.

---

## 6. Concrete Recommendation — Final Table

The single table Felix asked for:

| Endpoint | Base Rate | Quota Windows | Asymptote | hard_limit | Rationale |
|---|---|---|---|---|---|
| **z.ai (ours)** | $0.0143/M (365d amortized) | 5h × weekly × monthly | **3.0** | **True** | Primary. A=peak_mult (off-peak-pressure ≈ peak-price). Hard 429 at wall. |
| **z.ai (friend)** | $0.0173/M (ours × 1.21) | 5h × weekly × monthly | **3.0** | **True** | Same as ours + 21% premium. |
| **Ollama Cloud** | $0.0155/M (90d measured) | 5h × weekly | **4.17** (= $0.10/$0.024) | **False** | Has extra-usage. A = real cost ratio. Stays reachable for exclusive models. |
| **PPQ** | $0.14/M (30d ledger) | credits (single u) | **2.0** | **True** | Already expensive; A just smooths exclusion. Hard stop at $0. |
| **DeepInfra** | $0.05/M (30d measured) | credits (single u) | **2.0** | **True** | Preferred external (cheapest). Hard stop at $0. |
| **OpenRouter** | $0.135/M (30d measured) | credits (single u) | **2.0** | **True** | Middle-priced external. Hard stop at $0. |

### 6.1 Routing priority at a glance (low usage, no pressure)

```
z.ai ours    $0.0143/M   ← always preferred (cheapest + primary)
z.ai friend  $0.0173/M   ← second (21% premium)
Ollama       $0.0155/M   ← between ours and friend (cheaper than friend!)
DeepInfra    $0.0500/M   ← preferred external
OpenRouter   $0.1350/M   ← middle external
PPQ          $0.1400/M   ← last resort external
```

**Note:** Ollama at $0.0155/M is cheaper than z.ai friend ($0.0173/M). This means at low usage, the router prefers: `z.ai ours → Ollama → z.ai friend → DeepInfra → OpenRouter → PPQ`. This is correct — Ollama's measured rate really is lower than friend's penalized rate.

### 6.2 Routing priority as z.ai ours depletes (onset=0.60, A=3.0)

| z.ai ours usage | z.ai ours price | Router picks next |
|---|---|---|
| 0.50 | $0.0143 | **z.ai ours** |
| 0.60 (onset) | $0.0143 | **z.ai ours** |
| 0.70 | $0.0214 | **Ollama** ($0.0155) wins |
| 0.80 (midpoint) | $0.0429 | **Ollama** ($0.0155) wins |
| 0.90 | $0.100 | **DeepInfra** ($0.05) wins |
| 0.95 | $0.186 | **OpenRouter** ($0.135) wins |
| 1.00 | +∞ | Friend key → Ollama → externals |

The cascade is exactly right: squeeze z.ai ours until ~70%, fall to Ollama, then DeepInfra, then OpenRouter, then PPQ. PPQ is never reached unless everything else is exhausted.

---

## 7. Implementation Notes

### 7.1 Config block (proposed `providers.yaml`)

```yaml
zai:
  pressure:
    enabled: true
    windows: [5h, weekly, monthly]
    onset: 0.60
    asymptote: 3.0           # = peak_multiplier (ties to peak pricing)
    hard_limit: true
    cost_ratio: null         # no extra-usage path
    preference_weight: 3.0   # = asymptote when cost_ratio is null

ollama_cloud:
  pressure:
    enabled: true
    windows: [5h, weekly]
    onset: 0.70
    asymptote: 4.17          # = extra_rate / base_rate ($0.10 / $0.024)
    hard_limit: false
    cost_ratio: 4.17         # auto-updatable from billing
    preference_weight: 1.0   # neutral

external:
  ppq:
    pressure:
      enabled: true
      source: credits
      onset: 0.80
      asymptote: 2.0
      hard_limit: true
      cost_ratio: null
      preference_weight: 2.0
  deepinfra:
    pressure:
      enabled: true
      source: credits
      onset: 0.80
      asymptote: 2.0
      hard_limit: true
      cost_ratio: null
      preference_weight: 2.0
  openrouter:
    pressure:
      enabled: true
      source: credits
      onset: 0.80
      asymptote: 2.0
      hard_limit: true
      cost_ratio: null
      preference_weight: 2.0
```

### 7.2 Resolution function

```python
def resolve_asymptote(cost_ratio: float | None, preference_weight: float) -> float:
    """Resolve the effective asymptote from objective cost ratio + subjective preference.

    - If cost_ratio is None (no extra-usage path): asymptote = preference_weight.
    - If cost_ratio is set (Ollama): asymptote = cost_ratio × preference_weight.
    """
    if cost_ratio is None:
        return preference_weight
    return cost_ratio * preference_weight
```

### 7.3 Migration from current single-parameter asymptote

The current code takes `asymptote` as a single parameter. To migrate:

1. **Phase 1 (no behavior change):** add `cost_ratio` and `preference_weight` to config; compute `asymptote = resolve_asymptote(cost_ratio, preference_weight)` in the loader. All existing values stay the same.
2. **Phase 2 (auto-update cost_ratio):** wire Ollama's `cost_ratio` to update from billing data. When the extra-usage rate changes, `cost_ratio` auto-updates and the asymptote follows.
3. **Phase 3 (preference tuning):** Felix adjusts `preference_weight` per endpoint based on observed routing behavior.

---

## 8. Open Questions for Felix

1. **z.ai asymptote = 3.0 or 2.0?** 3.0 ties to the peak multiplier (off-peak-pressure at midpoint ≈ peak price). 2.0 is gentler — z.ai stays preferred longer as it depletes. *Recommendation: start at 3.0, lower to 2.5 if you see premature reroutes to Ollama.*

2. **Ollama preference_weight = 1.0 or 0.85?** At 1.0, Ollama's asymptote tracks its real cost ratio (4.17). At 0.85, the asymptote becomes 3.54 — Ollama is "preferred" (stays cheaper longer). *Recommendation: 1.0 (neutral) until you have data showing the router reroutes too early.*

3. **DeepInfra onset = 0.80 or 0.70?** DeepInfra is the preferred external. An earlier onset (0.70) means pressure starts sooner, which might prematurely push traffic to OpenRouter. *Recommendation: 0.80 (later) — let DeepInfra credits deplete further before pressuring.*

4. **Should the monthly z.ai window feed pressure or only base_rate?** Currently proposed: feeds base_rate only (amortization). Including it in pressure adds a third superimposed window — more conservative but noisier. *Recommendation: exclude from pressure; include in base_rate only.*

5. **v1 (single asymptote) or v2 (split cost_ratio + preference_weight)?** v1 ships faster; v2 is cleaner. *Recommendation: v1 now (the values in §6 are stable); plan v2 for when billing data is reliable enough to auto-update cost_ratio.*
