# Pricing Plan — Mathematical Soundness Review

**Reviewer:** Math/Edge-Case Audit  
**Date:** 2025-08-05  
**Scope:** Formula correctness, pressure curves, superposition behavior, edge cases, deadlock

---

## 1. Single-Window Pressure Table

**Formula:** `pressure(u) = 1 + K·t/(1-t)` where `t = (u - onset)/(1 - onset)`, `K = 0.5`

### Computed multipliers (assuming clamping to ≥1.0 below onset):

| Usage (u) | t (onset=0.60) | pressure | t (onset=0.70) | pressure | t (onset=0.80) | pressure |
|-----------|----------------|----------|----------------|----------|----------------|----------|
| 50%       | — (below)      | **1.00** | — (below)      | **1.00** | — (below)      | **1.00** |
| 60%       | 0.000          | **1.00** | — (below)      | **1.00** | — (below)      | **1.00** |
| 70%       | 0.250          | **1.17** | 0.000          | **1.00** | — (below)      | **1.00** |
| 80%       | 0.500          | **1.50** | 0.333          | **1.25** | 0.000          | **1.00** |
| 85%       | 0.625          | **1.83** | 0.500          | **1.50** | 0.250          | **1.17** |
| 90%       | 0.750          | **2.50** | 0.667          | **2.00** | 0.500          | **1.50** |
| 95%       | 0.875          | **4.50** | 0.833          | **3.50** | 0.750          | **2.50** |
| 99%       | 0.975          | **20.50**| 0.967          | **15.50**| 0.950          | **10.50**|
| 99.9%     | 0.9975         | **200.50**| 0.9967        | **150.50**| 0.9950        | **100.50**|

### Verdict: Is asymptote=1.5 "low enough"?

**Yes.** The curve is remarkably gentle through the 80–90% range:

| Usage band | Max pressure (any onset) | Interpretation |
|------------|--------------------------|----------------|
| ≤80%       | 1.50×                    | Negligible — router barely notices |
| 85%        | 1.83×                    | Mild preference shift |
| 90%        | 2.50×                    | Moderate — alternatives start winning if cheaper |
| 95%        | 4.50×                    | Strong but not prohibitive |
| 99%        | 20.50×                   | Effectively "last resort" |

At 90%, a 2.5× multiplier means an alternative endpoint only needs to be within 2.5× of the base preference to win. This keeps keys genuinely usable through 90%+. The steep ramp only kicks in above 95%, which is the intended "panic zone."

**The 99%+ values (20×–200×) are effectively hard cutoffs** — any competing endpoint with finite pressure will be preferred. This is correct behavior.

---

## 2. ⚠️ CRITICAL BUG: Below-Onset Discount

The raw formula `1 + K·t/(1-t)` produces **pressure < 1.0** when `u < onset`:

| u    | onset=0.60 | raw pressure | Expected |
|------|------------|-------------|----------|
| 0%   | t = -1.500 | **0.70**    | 1.00     |
| 30%  | t = -0.750 | **0.80**    | 1.00     |
| 50%  | t = -0.250 | **0.90**    | 1.00     |
| 59%  | t = -0.025 | **0.987**   | 1.00     |

**Impact:** A fresh endpoint at 0% usage gets a **30% artificial discount**. This skews the router toward unused endpoints and can cause oscillation:
1. Fresh endpoint wins (cheap) → traffic flows to it
2. Usage rises past onset → discount disappears → router switches away
3. Usage decays → discount returns → cycle repeats

**Fix required:** The implementation MUST clamp:
```
pressure = max(1.0, 1 + K * t / (1 - t))
```
Or equivalently: `if u < onset: return 1.0`

The tables in §1 assume this clamping is in place. **Without it, every number below onset is wrong.**

---

## 3. Superposition (Multiplicative) Analysis

### 3a. Specified edge cases

| Session | Weekly | Monthly | Product (onset=0.60) | Product (onset=0.80) | Verdict |
|---------|--------|---------|----------------------|----------------------|---------|
| 50%     | 50%    | 50%     | 1.0 × 1.0 × 1.0 = **1.00** | **1.00** | ✓ Correct |
| 90%     | 50%    | 50%     | 2.5 × 1.0 × 1.0 = **2.50** | **1.50** | ✓ Reasonable |
| 90%     | 90%    | 90%     | 2.5³ = **15.63** | 1.5³ = **3.38** | ⚠️ See below |

### 3b. Full aligned-usage product table

When all three windows are at the same usage level:

| All-Windows Usage | Product (onset=0.60) | Product (onset=0.70) | Product (onset=0.80) |
|--------------------|----------------------|----------------------|----------------------|
| 70%                | 1.17³ = **1.59**     | 1.00³ = **1.00**     | 1.00³ = **1.00**     |
| 80%                | 1.50³ = **3.38**     | 1.25³ = **1.95**     | 1.00³ = **1.00**     |
| 85%                | 1.83³ = **6.16**     | 1.50³ = **3.38**     | 1.17³ = **1.59**     |
| 90%                | 2.50³ = **15.63**    | 2.00³ = **8.00**     | 1.50³ = **3.38**     |
| 95%                | 4.50³ = **91.13**    | 3.50³ = **42.88**    | 2.50³ = **15.63**    |

### 3c. Is 90/90/90 = 15.6× too extreme?

**For onset=0.60 (z.ai): borderline.** The 5-hour window resets frequently, so hitting 90% on session is common during heavy use. If weekly and monthly are also at 90%, a 15.6× multiplier effectively kills the endpoint. But this genuinely means all three windows are simultaneously near exhaustion — the amplification is conceptually correct.

**For onset=0.80 (credit-based): fine.** 90/90/90 gives only 3.38×, well within usable range.

### 3d. Pathological case assessment

**Can normal usage trigger extreme pressure?**

| Scenario | Session | Weekly | Monthly | Product (0.60) | Pathological? |
|----------|---------|--------|---------|----------------|---------------|
| Heavy session, normal week | 90% | 60% | 50% | 2.5 × 1.0 × 1.0 = **2.50** | No |
| End of heavy week | 70% | 85% | 70% | 1.17 × 1.83 × 1.0 = **2.14** | No |
| End of billing month | 60% | 70% | 90% | 1.0 × 1.0 × 2.5 = **2.50** | No |
| Sustained heavy use | 90% | 90% | 90% | **15.63** | ⚠️ Yes, but warranted |
| All windows at 95% | 95% | 95% | 95% | **91.13** | Yes, but this is a real emergency |

**Verdict:** Superposition does NOT create pathological pressure during normal mixed-usage patterns. The extreme products only emerge when all three windows genuinely align near exhaustion, which represents a real resource crisis. The multiplicative model is sound.

### 3e. MULTIPLY vs MAX comparison

| Scenario | MULTIPLY | MAX | Ratio |
|----------|----------|-----|-------|
| 90/90/90 (onset=0.60) | 15.63 | 2.50 | 6.3× |
| 95/95/95 (onset=0.60) | 91.13 | 4.50 | 20.3× |

MULTIPLY creates dramatically steeper curves when windows align. This is a deliberate design choice (triple-window alignment = emergency), but it makes the pressure less predictable. **Recommendation:** Document this behavior explicitly — operators should understand that aligned-window exhaustion is treated as exponentially urgent, not linearly.

---

## 4. Credit Depletion Edge Cases

### 4a. PPQ negative balance

```
remaining = -$0.003, starting = $10.00
u = 1 - (-0.003 / 10) = 1 - (-0.0003) = 1.0003
u ≥ 1.0 → pressure = infinity ✓
```

**Correct.** A negative balance means credits are truly exhausted. Treating it as infinite pressure (dead endpoint) is the safe choice.

**Caveat:** -$0.003 could be a rounding artifact from PPQ's billing system. If the API still permits tiny requests, marking the endpoint as permanently dead might cause unnecessary failover. Consider an epsilon buffer (e.g., `u >= 1.001` for infinity, `1.0 ≤ u < 1.001` → very high but finite pressure like 1000×). **Low priority** — the current behavior errs on the side of safety.

### 4b. DeepInfra half remaining

```
remaining = $2.50, starting = $5.00
u = 1 - (2.50 / 5.00) = 1 - 0.5 = 0.50
onset = 0.80 → u < onset → pressure = 1.0 ✓
```

**Correct.** Half the credits remaining → no pressure applied.

### 4c. Additional credit edge cases not in spec

| Scenario | Computation | Result | Issue? |
|----------|-------------|--------|--------|
| **starting_balance = 0** | `u = 1 - (rem/0)` | **Division by zero** | ⚠️ Must guard |
| **Credits topped up** (remaining > starting) | rem=$15, start=$10 → u = 1 - 1.5 = -0.5 | **u < 0 → pressure < 1.0** | ⚠️ Clamps needed: `u = max(0, min(1, computed))` |
| **starting_balance unknown** (new endpoint) | u undefined | — | ⚠️ Need default: treat as u=0 until first balance snapshot |

---

## 5. Deadlock Scenario Analysis

**State:** z.ai=95%, ollama=95%, DeepInfra u=0.95, PPQ=∞, OpenRouter=∞

### Single-window pressure at 95%:

| Endpoint   | Onset | Pressure (single window) | Status |
|------------|-------|--------------------------|--------|
| z.ai       | 0.60  | 4.50×                    | Alive (pressured) |
| ollama     | 0.70  | 3.50×                    | Alive (pressured) |
| DeepInfra  | 0.80  | 2.50×                    | Alive (pressured) |
| PPQ        | —     | ∞                        | **Dead** |
| OpenRouter | —     | ∞                        | **Dead** |

### Router behavior:

The router picks `argmin(base_preference × pressure)` across all endpoints.

- **PPQ and OpenRouter are eliminated** (infinite cost).
- Among survivors, **DeepInfra wins** (lowest pressure at 2.50×) assuming comparable base preferences.
- If z.ai has a much better base preference, it might still win: `pref_zai × 4.5 < pref_deepinfra × 2.5` when `pref_zai / pref_deepinfra < 0.556`.

**Verdict: The router picks the least-bad option. It does NOT fail.** ✓

### True deadlock (all endpoints at ∞):

If z.ai, ollama, AND DeepInfra all reach u≥1.0 simultaneously (along with PPQ and OpenRouter already dead):

**Every endpoint has infinite cost → the router has no valid target.**

This is an **unhandled case in the spec.** Options:

| Strategy | Behavior | Tradeoff |
|----------|----------|----------|
| **Fail hard** | Return error to caller | Cleanest but blocks all traffic |
| **Pick least-over∞** | Choose endpoint with lowest u≥1.0 | Buys time but serves degraded |
| **Fallback to ollama** | ollama is local, can't truly "run out" | ⚠️ But spec gives ollama an onset too |
| **Reset pressure** | Drop all pressure, route by preference only | Dangerous — masks real exhaustion |

**Recommendation:** Define explicit behavior for the all-∞ case. The most defensible is "pick the endpoint with the lowest u value ≥1.0" (least-over-exhausted), with a logged warning. This avoids total failure while still routing to the least-damaged endpoint.

### Near-deadlock (all at 99%):

| Endpoint   | Pressure (single) | With 99/99/99 superposition |
|------------|-------------------|----------------------------|
| z.ai       | 20.50×            | 20.50³ = **8,615×**         |
| ollama     | 15.50×            | 15.50³ = **3,724×**         |
| DeepInfra  | 10.50×            | 10.50³ = **1,158×**         |

At these levels, the router effectively ranks by onset: **DeepInfra > ollama > z.ai** (later onset = lower pressure = preferred). This is the intended cascading preservation behavior working correctly.

---

## 6. Onset Staggering Significance

### Question: Does 60/70/80 staggering matter with K=0.5?

**Yes, significantly.** The pressure ratios between onsets are substantial at all usage levels:

| Usage | onset=0.60 | onset=0.70 | onset=0.80 | Ratio (0.60 / 0.80) |
|-------|------------|------------|------------|----------------------|
| 85%   | 1.83       | 1.50       | 1.17       | **1.57×**            |
| 90%   | 2.50       | 2.00       | 1.50       | **1.67×**            |
| 95%   | 4.50       | 3.50       | 2.50       | **1.80×**            |
| 99%   | 20.50      | 15.50      | 10.50      | **1.95×**            |

At 95%, the earliest-onset endpoint (z.ai) experiences **80% more pressure** than the latest-onset (credit-based). This creates a clear priority cascade:

1. **z.ai (onset 0.60)** gets pressured first → router conserves it earliest → **preserved longest**
2. **ollama (onset 0.70)** pressured next → intermediate
3. **Credit-based (onset 0.80)** pressured last → used most aggressively → **exhausted first**

### Is this the right priority?

| Endpoint | Resets? | Onset | Strategy |
|----------|---------|-------|----------|
| z.ai     | Auto (5h/weekly/monthly) | 0.60 (earliest) | Conserved first — makes sense, reliable fallback |
| ollama   | N/A (local) | 0.70 | Intermediate |
| Credit-based | Manual top-up only | 0.80 (latest) | Used aggressively — ⚠️ questionable |

**Potential concern:** Credit-based endpoints (DeepInfra, PPQ) don't auto-reset, yet they're used most aggressively (pressure starts latest). This means credits burn down faster than z.ai quota. If the intent is "preserve non-renewable resources," the onset ordering should be **reversed** (credit-based onset=0.60, z.ai onset=0.80).

**However**, if the intent is "z.ai is the most reliable, always keep it available as fallback," then the current ordering is correct — you burn through credits first because z.ai's auto-resetting windows make it the dependable option.

**Verdict:** The staggering is mathematically meaningful (not negligible). The priority direction is a design choice that should be explicitly documented.

---

## 7. Summary of Findings

### 🔴 Must Fix

| # | Issue | Impact |
|---|-------|--------|
| 1 | **Below-onset discount** — raw formula gives pressure < 1.0 for u < onset (down to 0.70× at u=0) | Skews routing toward fresh endpoints, causes oscillation. Fix: `max(1.0, formula)` or early return. |
| 2 | **Division by zero** when `starting_balance = 0` for credit-based endpoints | Runtime crash. Fix: guard with default u=0. |

### 🟡 Should Address

| # | Issue | Impact |
|---|-------|--------|
| 3 | **All-∞ deadlock** behavior undefined | Router behavior unknown when every endpoint is exhausted. Define explicit fallback. |
| 4 | **Credit top-up overflow** — remaining > starting gives u < 0, amplifies below-onset discount | Fix: `u = clamp(computed, 0, 1)` |
| 5 | **Superposition predictability** — MULTIPLY creates steep non-linear spikes when windows align | Document behavior; consider whether operators need a MAX fallback mode. |

### 🟢 Sound As-Is

| # | Item | Verdict |
|---|------|---------|
| 6 | asymptote=1.5 keeps keys usable through 90%+ | ✓ Gentle curve, steep only above 95% |
| 7 | PPQ negative balance → ∞ | ✓ Correct and safe |
| 8 | DeepInfra at 50% → no pressure | ✓ Correct |
| 9 | Onset staggering creates meaningful priority cascade | ✓ 57–95% pressure differential |
| 10 | Superposition doesn't create pathological cases in normal mixed-window usage | ✓ Extreme products only when genuinely warranted |
| 11 | Partial deadlock (some endpoints ∞) resolves correctly to least-bad | ✓ Router picks lowest finite pressure |

---

## Appendix: Formula Reference

```
For u < onset:  pressure = 1.0                    ← MUST be explicit, not derived
For u ≥ 1.0:    pressure = ∞
Otherwise:      t = (u - onset) / (1 - onset)
                pressure = 1 + K · t / (1 - t)
                where K = asymptote - 1 = 0.5

Superposition:  total = session_pressure × weekly_pressure × monthly_pressure
Effective cost: cost = base_preference × total_pressure
Router picks:   argmin(cost) across all endpoints
```
