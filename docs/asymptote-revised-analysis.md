# Revised Asymptote Analysis: Low-Asymptote / Use-Subscriptions-Longest Strategy

> **Author:** Pricing systems consultant (subagent)
> **Date:** 2026-08-05
> **Status:** ✅ SUPERSEDES `docs/asymptote-preference-analysis.md`
> **Audience:** Felix (visual/spatial thinker — tables and worked examples)
> **Code state:** commit `49bef24` — constants already updated to uniform A=1.5

---

## 0. TL;DR — What Changed and Why

The previous analysis (`asymptote-preference-analysis.md`) recommended **high** asymptotes (z.ai=3.0, Ollama=4.17, PPQ=2.0) so that endpoints would "flee" to alternatives as quota depleted. Felix slept on it and reversed course:

> *"Make the asymptote really low so the keys flee as late as possible and we use them as long as possible. I don't want to use OpenRouter or DeepInfra or PPQ unless I absolutely have to."*

**The new strategy is the OPPOSITE:**

| | Old (superseded) | New (this doc) |
|---|---|---|
| **z.ai asymptote** | 3.0 (flee at ~87%) | **1.5** (flee at ~98%) |
| **Ollama asymptote** | 4.17 (= cost ratio) | **1.5** (gentle ramp) |
| **PPQ asymptote** | 2.0 | **1.5** (uniform) |
| **Design principle** | "Flee early, stay safe" | **"Squeeze every token from sunk-cost subscriptions"** |

The code is already updated: **ALL endpoints use `asymptote=1.5`** (see `pricing_engine.py` lines 165–233, "FELIX FINAL DECISION Aug 5 19:00"). This document explains WHY that's correct and traces through the math.

---

## 1. Why Low Asymptotes on Subscription Endpoints Make Sense

### 1.1 The Sunk Cost Argument

| Endpoint | Payment Model | Marginal Cost per Token |
|---|---|---|
| **z.ai (ours)** | $300/yr flat subscription | **$0** (already paid) |
| **z.ai (friend)** | Shared key, no cost to us | **$0** |
| **Ollama Cloud** | $100/mo + $40 prepaid extra | **$0** (within quota) |
| **PPQ** | Pay-per-token | **$0.14/M** (real money) |
| **DeepInfra** | Pay-per-token from $5 balance | **$0.05/M** (real money) |
| **OpenRouter** | Pay-per-token from $10 balance | **$0.135/M** (real money) |

Every token on z.ai or Ollama is **effectively free** — the money is already spent. Every token on PPQ/DeepInfra/OpenRouter costs **real dollars from a finite wallet**. The optimizer's job is to maximize free tokens and minimize paid tokens.

### 1.2 How the Asymptote Controls This

The asymptote is the multiplier at the **midpoint** of the pressure ramp (halfway between onset and 100% usage). A low asymptote means the price rises **gently** as quota depletes. A high asymptote means the price spikes **steeply**, causing the optimizer to reroute prematurely.

```
LOW asymptote (1.5):
  Price at 90% usage = base × 2.5     ← gentle, still cheap
  Price at 95% usage = base × 4.5     ← starting to hurt
  Price at 98% usage = base × 10.5    ← only NOW does it flee

HIGH asymptote (5.0):
  Price at 90% usage = base × 13.0    ← already fleeing!
  Price at 95% usage = base × 21.0    ← long gone
```

With a low asymptote, z.ai stays below PPQ's base rate ($0.14/M) until **97.8% usage**. With a high asymptote (5.0), it crosses PPQ at **87.5%** — abandoning 10 percentage points of free quota prematurely.

### 1.3 The Intuition for a Visual Thinker

Imagine each endpoint as a tank of free water (subscriptions) or expensive water (paid). The asymptote is how fast the faucet closes as the tank empties:

- **High asymptote** = faucet slams shut at 85% → you switch to expensive water with 15% still in the tank
- **Low asymptote** = faucet trickle-closes, only fully shutting at 99% → you drink every last drop of free water before paying

---

## 2. The Math: How Base Rate + Asymptote Interact

### 2.1 The Crossover Formula

The optimizer picks the cheapest effective price. A subscription endpoint stops being preferred when its **effective price** (base × pressure) exceeds a competitor's **base rate** (which is flat for paid endpoints below their own onset).

```
effective_price(u) = base_rate × pressure(u)

Crossover happens when:
  base_rate × pressure(u) = competitor_base_rate

Solving for u:
  pressure(u) = competitor_base_rate / base_rate = m   (the price ratio)

  Using pressure(u) = 1 + K·t/(1-t), where K = asymptote − 1, t = (u−onset)/(1−onset):

  t = (m − 1) / (asymptote + m − 2)
  u = onset + t × (1 − onset)
```

This gives us the exact usage percentage at which a subscription becomes more expensive than a paid alternative.

### 2.2 z.ai vs PPQ: When Does Free Become More Expensive Than Paid?

z.ai base = $0.0143/M, PPQ base = $0.14/M → ratio **m = 9.79×**

| Asymptote | Crossover usage | What it means |
|---|---|---|
| **1.5** (current) | **97.8%** | z.ai is preferred until 97.8% of its 5h quota is consumed |
| 2.0 (old PPQ value) | 95.9% | Abandons 2% earlier |
| 4.17 (old Ollama value) | 89.4% | Abandons 8% earlier |
| 5.0 (old theoretical) | 87.5% | Abandons 10% earlier — **wastes 10% of free quota!** |

**The difference between A=1.5 and A=5.0 is 10.3 percentage points of free quota.** On z.ai's 5h window (~2M tokens), that's ~206K tokens per session that we'd be paying PPQ for instead of using for free. Across 6 sessions/day, that's ~1.2M tokens/day wasted — about $0.17/day in unnecessary PPQ charges, or **~$62/year**.

### 2.3 z.ai vs DeepInfra (the cheapest paid endpoint)

DeepInfra at $0.05/M is the cheapest external — the first paid endpoint z.ai would fall to.

| Asymptote | Crossover usage | z.ai still preferred until... |
|---|---|---|
| **1.5** | **93.3%** | 93% of session quota consumed |
| 2.0 | 88.6% | 4.7 points earlier |
| 4.17 | 77.6% | 15.7 points earlier |
| 5.0 | 75.4% | 17.9 points earlier |

### 2.4 Full Crossover Table (onset = 0.60 for z.ai)

| Competitor | Base Rate | m (ratio) | A=1.5 | A=2.0 | A=4.17 | A=5.0 |
|---|---|---|---|---|---|---|
| DeepInfra | $0.05/M | 3.50× | **93.3%** | 88.6% | 77.6% | 75.4% |
| OpenRouter | $0.135/M | 9.44× | **97.8%** | 95.8% | 89.1% | 87.1% |
| PPQ | $0.14/M | 9.79× | **97.8%** | 95.9% | 89.4% | 87.5% |

**Reading the table:** with A=1.5, z.ai stays cheaper than ALL paid endpoints until at least 93% usage. With A=5.0, it starts losing to DeepInfra at just 75%. That's the power of the low asymptote.

### 2.5 Effective Price Curve for z.ai at A=1.5 (onset=0.60)

| Session Usage | Pressure | Effective $/M | vs DeepInfra ($0.05) | vs PPQ ($0.14) |
|---|---|---|---|---|
| 60% (onset) | 1.00× | $0.0143 | 3.5× cheaper | 9.8× cheaper |
| 70% | 1.17× | $0.0167 | 3.0× cheaper | 8.4× cheaper |
| 80% | 1.50× | $0.0214 | 2.3× cheaper | 6.5× cheaper |
| 85% | 1.83× | $0.0262 | 1.9× cheaper | 5.3× cheaper |
| 90% | 2.50× | $0.0357 | 1.4× cheaper | 3.9× cheaper |
| 93% | 3.36× | $0.0480 | ≈ tie | 2.9× cheaper |
| 95% | 4.50× | $0.0643 | **above** | 2.2× cheaper |
| 97% | 7.17× | $0.1025 | **above** | 1.4× cheaper |
| 98% | 10.50× | $0.1502 | **above** | **above** (by a hair) |
| 99% | 20.50× | $0.2932 | **above** | **above** |
| 100% | +∞ | +∞ | — | — |

**Key insight:** even at **95% usage**, z.ai is still 2.2× cheaper than PPQ. The router squeezes z.ai to ~98% before PPQ becomes the cheaper option. That's exactly Felix's intent.

### 2.6 The Superposition Danger (and why A=1.5 is safe)

z.ai has **three superimposed windows** (5h session × 7d weekly × 30d monthly). The pressures multiply, not max. This is where a high asymptote becomes dangerous:

**Scenario: session=90%, weekly=70%, monthly=40%** (realistic mid-month)

| Asymptote | Session factor | Weekly factor | Monthly factor | Combined | Effective $/M | vs PPQ |
|---|---|---|---|---|---|---|
| **1.5** | 2.50 | 1.17 | 1.00 | **2.92** | **$0.0417** | 3.4× cheaper ✅ |
| 2.0 | 4.00 | 1.33 | 1.00 | 5.33 | $0.0763 | 1.8× cheaper ✅ |
| 3.0 | 7.00 | 1.67 | 1.00 | 11.67 | $0.1668 | **above PPQ!** ❌ |
| 5.0 | 13.00 | 2.33 | 1.00 | 30.33 | $0.4338 | **3× above PPQ!** ❌ |

With **A=3.0** (the old recommendation), z.ai at 90%/70%/40% would cost **$0.167/M** — more expensive than PPQ ($0.14/M) with a fresh balance! The router would abandon z.ai and route to PPQ, **paying real money when free quota is still available**.

With **A=1.5**, the same scenario costs $0.042/M — 3.4× cheaper than PPQ. The router correctly stays on z.ai.

> **This is the single strongest argument for the low asymptote.** The superposition of three windows amplifies the asymptote's effect cubically. A "reasonable" asymptote of 3.0 on a single window becomes catastrophic when three windows compound. A=1.5 keeps the compounded effect manageable.

---

## 3. Paid Endpoint Balance Tracking

Felix also wants: **remaining balance visibility on ALL paid endpoints** and **asymptotic price increase based on balance depletion**.

### 3.1 API Endpoints for Balance Queries

| Endpoint | API Call | Returns | Poll Cadence |
|---|---|---|---|
| **PPQ** | `POST /credits/balance` | `remaining` (credits in $) | Every 5 min (existing `api_burn_collector`) |
| **DeepInfra** | Billing API: query accumulated `total_spent` | `total_spent` → `remaining = budget − spent` | Every 5 min |
| **OpenRouter** | `GET /api/v1/key` | `usage` field → compute `remaining` | Every 5 min |

**DeepInfra specifics (from Felix's web search):**
- Has billing endpoints at `https://deepinfra.com`
- Can create scoped JWT tokens with `spending_limit`
- Webhook payloads include `inference_status.cost` (per-call cost)
- Compute: `remaining = DEEPINFRA_STARTING_BALANCE − SUM(cost_usd WHERE key='deepinfra')`

**OpenRouter specifics:**
- `GET /api/v1/key` returns `usage` (cumulative $ spent)
- `remaining = OPENROUTER_STARTING_BALANCE − usage`
- Currently EXHAUSTED ($0 balance) — tracked via `SUM(cost_usd WHERE key='openrouter')`

### 3.2 Balance → Usage Fraction

The `quota_pressure_factor` function takes a `usage` parameter (0.0–1.0). For credit-based endpoints, we derive this from the balance:

```python
balance_usage = 1.0 - (remaining_balance / starting_balance)
```

| State | remaining | starting | balance_usage | Meaning |
|---|---|---|---|---|
| Full balance | $5.00 | $5.00 | 0.00 | Fresh — no pressure |
| Half spent | $2.50 | $5.00 | 0.50 | Below onset — no pressure |
| 80% spent | $1.00 | $5.00 | 0.80 | At onset — pressure begins |
| 90% spent | $0.50 | $5.00 | 0.90 | Midpoint — price × 1.5 |
| 95% spent | $0.25 | $5.00 | 0.95 | Near-empty — price × 2.5 |
| Exhausted | $0.00 | $5.00 | 1.00 | **+∞ (hard limit)** |

### 3.3 Integration with quota_pressure_factor

```python
# Pseudocode for paid endpoint pricing
remaining = query_balance(provider)          # from API or local DB
balance_usage = 1.0 - (remaining / starting)  # 0.0 = full, 1.0 = empty

pressure = quota_pressure_factor(
    usage=balance_usage,
    onset=0.80,           # paid endpoints: later onset (credits deplete slowly)
    asymptote=1.5,        # uniform low
    hard_limit=True,      # $0 balance = unreachable
)

effective_price = base_rate * pressure
# At 50% balance: pressure=1.0, price = base_rate (flat)
# At 90% balance: pressure=1.5, price = base_rate × 1.5
# At 100% balance: pressure=+∞, endpoint unreachable
```

### 3.4 PPQ Price Curve at A=1.5 (onset=0.80)

| Balance Used | Pressure | Effective $/M | Notes |
|---|---|---|---|
| 0–80% | 1.00× | $0.140 | Flat — no pressure |
| 85% | 1.17× | $0.163 | Gentle rise |
| 90% | 1.50× | $0.210 | Midpoint |
| 95% | 2.50× | $0.350 | Clearly expensive |
| 98% | 5.50× | $0.770 | Approaching wall |
| 99% | 10.50× | $1.470 | Emergency territory |
| 100% | +∞ | +∞ | Hard stop |

Even at 95% balance depletion, PPQ costs $0.35/M — more expensive than any fresh endpoint. The pressure mainly ensures a **smooth** approach to the wall rather than a cliff at $0 balance.

---

## 4. Revised Asymptote Table

### 4.1 Master Table (Felix's Direction — Uniform Low)

| Endpoint | Base Rate | Asymptote | Onset | hard_limit | Rationale |
|---|---|---|---|---|---|
| **z.ai (ours)** | $0.0143/M (365d amortized) | **1.5** | 0.60 | **True** | Sunk cost ($300/yr). Squeeze to 98% before falling to PPQ. 3 superimposed windows; low A keeps compounded pressure manageable. Hard 429 at wall. |
| **z.ai (friend)** | $0.0173/M (ours × 1.21) | **1.5** | 0.60 | **True** | Same sunk-cost logic. 21% premium applied via base rate, not asymptote. |
| **Ollama Cloud** | $0.0155/M (90d measured) | **1.5** | 0.70 | **False** | Sunk cost ($100/mo + prepaid). Low A keeps it below DeepInfra until 95%. hard_limit=False — extra-usage path exists (caps at A×base = $0.023/M). |
| **PPQ** | $0.14/M (30d ledger) | **1.5** | 0.80 | **True** | Real money per token. Base rate already 10× z.ai. A=1.5 ensures smooth exclusion as credits → $0. Balance-tracked via `/credits/balance`. |
| **DeepInfra** | $0.05/M (30d measured) | **1.5** | 0.80 | **True** | Cheapest paid endpoint. Balance-tracked. A=1.5 squeezes credits to 98% before hard stop. |
| **OpenRouter** | $0.135/M (per-call measured) | **1.5** | 0.80 | **True** | Middle-priced paid. Balance-tracked via `/api/v1/key`. Currently exhausted ($0). |

### 4.2 Why Uniform 1.5 (Not Differentiated)?

| Design question | Answer |
|---|---|
| Should paid endpoints have HIGHER asymptotes (flee faster)? | **No.** Their base rates already make them last resort (10× more expensive). Once you're forced onto a paid endpoint, you want to squeeze every credit too. |
| Should subscriptions have EVEN LOWER asymptotes (1.2)? | **Possible but risky.** A=1.2 makes the superposition extremely gentle — at 99% usage, pressure is only 6.5×. But the ramp also barely rises, so the "squeeze every last token" signal gets weak near the wall. A=1.5 is the sweet spot. |
| Does onset matter more than asymptote? | **Yes, for the START of pressure.** Onset controls WHEN pressure begins; asymptote controls HOW FAST it escalates. The staggered onsets (0.60 → 0.70 → 0.80) create the cascade order; the uniform asymptote controls the steepness within each cascade step. |

### 4.3 Routing Priority at Low Usage (No Pressure)

```
z.ai ours    $0.0143/M   ← always preferred (cheapest + sunk cost)
Ollama       $0.0155/M   ← second (also sunk cost; 8% more than z.ai)
z.ai friend  $0.0173/M   ← third (21% penalty on ours)
─────────────────────────────────────────────────────────────
DeepInfra    $0.0500/M   ← first paid (3.5× more than z.ai)
OpenRouter   $0.1350/M   ← second paid (9.4× more than z.ai)
PPQ          $0.1400/M   ← last resort (9.8× more than z.ai)
```

The line separates **free** (subscriptions) from **paid** (per-token). The optimizer will exhaust ALL free endpoints before touching any paid one — this is encoded entirely in the **base rates**, independent of asymptote.

---

## 5. What Happens When Everything Is Depleting

### 5.1 Scenario: subscriptions at 90%, PPQ at 50% balance

**Assumptions** (realistic mid-month, moderate traffic):
- z.ai ours: session=90%, weekly=50%, monthly=30%
- z.ai friend: same windows
- Ollama: session=90%, weekly=50%
- PPQ: 50% balance used
- DeepInfra: fresh ($5 balance)
- OpenRouter: fresh ($10 balance)

**All at A=1.5.** Computing effective prices:

| Rank | Endpoint | Session % | Pressure (combined) | Effective $/M | Free? |
|---|---|---|---|---|---|
| **1** | **Ollama** | 90% | 2.50 × 1.00 = 2.00 | **$0.0310** | ✅ free |
| **2** | **z.ai ours** | 90% | 2.50 × 1.00 × 1.00 = 2.50 | **$0.0358** | ✅ free |
| **3** | **z.ai friend** | 90% | 2.50 × 1.00 × 1.00 = 2.50 | **$0.0433** | ✅ free |
| 4 | DeepInfra | fresh | 1.00 | $0.0500 | ❌ paid |
| 5 | OpenRouter | fresh | 1.00 | $0.1350 | ❌ paid |
| 6 | PPQ | 50% bal | 1.00 | $0.1400 | ❌ paid |

**The router picks Ollama at $0.031/M.** All three subscriptions are under 5¢/M. All three paid endpoints are above 5¢/M. **PPQ at 50% balance ($0.14/M) is 4.5× more expensive than the cheapest subscription.**

### 5.2 Does This Match Felix's Intent?

**Yes.** Felix said: *"I don't want to use OpenRouter or DeepInfra or PPQ unless I absolutely have to."* In this scenario, even with every subscription at 90% depletion, the optimizer correctly stays on subscriptions. The paid endpoints are untouched.

### 5.3 When DOES the Router Finally Fall to Paid?

Push z.ai ours session to 95% (with weekly still at 50%):

| Endpoint | Effective $/M |
|---|---|
| Ollama (90%) | $0.0310 |
| z.ai ours (95%) | $0.0643 |
| z.ai friend (95%) | $0.0778 |
| **DeepInfra (fresh)** | **$0.0500** ← now cheaper than z.ai ours/friend! |

At 95%, z.ai ours ($0.0643) is now more expensive than DeepInfra ($0.0500). The router falls to DeepInfra — but ONLY because z.ai is genuinely at 95% of its tiny 5h window. This is the correct behavior: we've squeezed 95% of the free quota before paying.

### 5.4 The Full Cascade (pushing everything to the wall)

| Step | What's happening | Router picks | Effective $/M |
|---|---|---|---|
| 1 | All subscriptions < 60% | z.ai ours | $0.0143 |
| 2 | z.ai ours hits 70%, Ollama still fresh | Ollama | $0.0155 |
| 3 | Ollama hits 70%, z.ai friend still fresh | z.ai friend | $0.0173 |
| 4 | z.ai friend hits 70% | Ollama (cycled back) | $0.0194 |
| 5 | Everything at 90% | Ollama | $0.0310 |
| 6 | Everything at 93% | Ollama | $0.0413 |
| 7 | Everything at 95% | **DeepInfra** (subscriptions now $0.05+) | $0.0500 |
| 8 | DeepInfra depletes to 80% | OpenRouter | $0.1350 |
| 9 | OpenRouter depletes to 80% | PPQ | $0.1400 |
| 10 | Everything exhausted | **Error / queue** | +∞ |

The cascade is: **free subscriptions → cheapest paid → most expensive paid → error**. Exactly right.

---

## 6. Cold Start and Fallback

### 6.1 Before Any Billing Data Is Collected

| Endpoint | Cold start behavior | Seed `balance_usage` | Rationale |
|---|---|---|---|
| **z.ai** (ours/friend) | Quota API called on startup | 0.0 (from API) | z.ai's quota endpoint returns live usage; no cold-start gap |
| **Ollama** | Usage API called on startup | 0.0 (from API) | `ollama.com/api/usage` returns live session/weekly fractions |
| **PPQ** | No balance query yet | **0.5** (conservative) | Assume 50% depleted until first `/credits/balance` call. Below onset (0.80), so no pressure — but conservative enough to not over-commit |
| **DeepInfra** | No billing query yet | **0.0** (from local DB) or **0.5** (no DB) | If local `api_calls` table has records: compute `1 − (remaining/starting)`. If no records: assume 0.5 (conservative) |
| **OpenRouter** | No key query yet | **0.0** (from local DB) or **0.5** (no DB) | Same as DeepInfra. Currently known-exhausted ($0 balance) |

### 6.2 Why 0.5 (Not 0.0) for Unmeasured Paid Endpoints?

Conservative seeding assumes **more depleted than reality**. This biases the router slightly away from endpoints whose balance we haven't verified. It's the safe default:

- If the balance is actually full → we slightly under-use a paid endpoint temporarily (harmless — we have subscriptions)
- If the balance is actually near-empty → we avoid burning the last few credits before the price adjusts (protective)

Once the first balance query completes (within 5 minutes of startup), the real `balance_usage` replaces the seed.

### 6.3 Subscription Cold Start

For z.ai and Ollama, the quota/usage API is called on startup. If the API is temporarily unreachable:

| Endpoint | Fallback | Seed | Safe? |
|---|---|---|---|
| z.ai | Linear `scarcity_factor(quota_pct)` | `quota_pct` from last known value | ✅ Conservative — linear ramp is gentler than RP-EXP |
| Ollama | `extra_usage_multiplier("included")` | 1.0× (no pressure) | ⚠️ Optimistic — but Ollama has hard_limit=False, so over-use just means extra-usage billing, not a crash |

### 6.4 Does preference_weight Interact with Asymptote?

**Currently: no.** The code uses fixed asymptote constants. The previous analysis proposed splitting into `cost_ratio × preference_weight`, but Felix's uniform-1.5 decision makes this unnecessary for v1.

**If Felix later wants per-endpoint tuning**, the path is:

```python
effective_asymptote = BASE_ASYMPTOTE * preference_weight
# Default preference_weight = 1.0 → asymptote stays 1.5
# preference_weight = 0.8 → asymptote = 1.2 (use even longer)
# preference_weight = 1.5 → asymptote = 2.25 (flee sooner)
```

This is a **v2 concern**. For now, the uniform 1.5 + base-rate differentiation is sufficient and matches Felix's stated intent.

---

## 7. What Changed from the Previous Analysis

| Aspect | Previous (`asymptote-preference-analysis.md`) | This Document (Revised) |
|---|---|---|
| **z.ai asymptote** | 3.0 (ties to peak multiplier) | **1.5** (uniform low — squeeze longest) |
| **Ollama asymptote** | 4.17 (= real cost ratio $0.10/$0.024) | **1.5** (abandoned cost-ratio approach) |
| **PPQ asymptote** | 2.0 | **1.5** (uniform) |
| **Design philosophy** | Asymptote = cost signal (objective) | Asymptote = "how gently to approach the wall" (uniform) |
| **Preference encoding** | Via asymptote (mixed with cost) | **Via base rate only** (clean separation) |
| **Cost ratio concept** | `effective_asymptote = cost_ratio × pref_weight` | **Abandoned.** Base rate handles preference; asymptote is uniform. |
| **Superposition risk** | Not analyzed for z.ai (3 windows) | **Key finding:** A=3.0 on 3 superimposed windows → $0.167/M at 90/70/40% (above PPQ!) |
| **Paid endpoint tracking** | Mentioned but not detailed | **Full spec:** balance APIs, usage fraction, integration with `quota_pressure_factor` |

### Why the Previous Recommendation Was Wrong for Felix's Use Case

The previous analysis treated asymptote as an **objective cost signal** — Ollama's 4.17 was its real cost ratio, z.ai's 3.0 tied to the peak multiplier. This is intellectually elegant but **practically dangerous** with superimposed windows:

- At A=3.0, z.ai with session=90% + weekly=70% produces a combined pressure of **11.67×** → $0.167/M
- This is **above PPQ's $0.14/M** with a fresh balance
- The router would abandon free z.ai quota and pay PPQ for tokens — **the exact opposite of Felix's intent**

The uniform A=1.5 sidesteps this entirely: even with three windows compounding, z.ai stays cheap until genuinely close to exhaustion.

---

## 8. Implementation Status

### 8.1 What's Already Done (commit 49bef24)

```python
# pricing_engine.py — constants already updated
OLLAMA_QUOTA_PRESSURE_ASYMPTOTE = 1.5
ZAI_QUOTA_PRESSURE_ASYMPTOTE    = 1.5
PPQ_QUOTA_PRESSURE_ASYMPTOTE    = 1.5
OPENROUTER_CREDIT_PRESSURE_ASYMPTOTE = 1.5
DEEPINFRA_CREDIT_PRESSURE_ASYMPTOTE  = 1.5
```

### 8.2 What's Still Needed

| Item | Status | Notes |
|---|---|---|
| z.ai helper functions (`_measure_zai_amortized`, etc.) | ✅ Exist | Not yet wired into `live_router.select_failover` |
| PPQ balance tracking (`POST /credits/balance`) | ✅ Exists | `api_burn_collector` runs every 5 min; feeds `balance_usage` |
| DeepInfra billing API integration | ❌ Not built | Need: query accumulated spend, compute `remaining = $5 − spent` |
| OpenRouter key usage (`GET /api/v1/key`) | ❌ Not built | Need: parse `usage` field, compute `remaining = $10 − usage` |
| `balance_usage` → `quota_pressure_factor` wiring for paid endpoints | ❌ Not wired | Need: call `quota_pressure_factor(usage=balance_usage, onset=0.80, asymptote=1.5, hard_limit=True)` in `compute_effective_price` for PPQ/DeepInfra/OpenRouter |
| Stale docstring in `compute_effective_price` (line 765) | ⚠️ Minor | Still references "asymptote=2.0" — should say 1.5 |

### 8.3 Code Snippet: Paid Endpoint Pressure (Proposed)

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
        hard_limit=True,   # $0 balance = unreachable
    )
```

---

## 9. Summary for Felix

1. **All asymptotes are now 1.5** (uniform low). This makes every endpoint's price rise gently as it depletes, so the optimizer squeezes each one as long as physically possible.

2. **z.ai stays preferred until 98% of its 5h quota is consumed** (vs 87% with the old A=5.0). That's 10+ percentage points of extra free usage per session.

3. **The base rates do all the preference work.** z.ai at $0.014/M is naturally 10× cheaper than PPQ at $0.14/M. The asymptote doesn't need to encode preference — it just controls how smoothly we approach each endpoint's wall.

4. **Paid endpoints get balance tracking.** PPQ via `/credits/balance`, DeepInfra via billing API spend accumulation, OpenRouter via `/api/v1/key`. As balance depletes, `balance_usage` feeds into the same `quota_pressure_factor` with `hard_limit=True` (zero balance = unreachable).

5. **When everything is at 90% depletion**, the router still picks subscriptions (under 5¢/M) over paid endpoints (over 5¢/M). Felix's intent — "don't use paid unless absolutely necessary" — is encoded and verified.

---

> **Supersedes:** `docs/asymptote-preference-analysis.md` (all recommendations in that document are void).
> **See also:** `docs/quota-pressure-design.md` (RP-EXP formula spec), `docs/realtime-pricing-design.md` (per-endpoint model vision)
