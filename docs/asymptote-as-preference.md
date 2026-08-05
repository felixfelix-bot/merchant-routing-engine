# Asymptote as Preference Dial — Design Analysis

> **Status:** Design analysis (pre-decision)
> **Author:** Pricing engine analysis (subagent)
> **Date:** 2026-08-05
> **Audience:** Felix (visual thinker — wants tech explained before commit)
> **Question on the table:** Felix's idea — *"the one we want to prefer gets a
> lower asymptote, the one we want to use less gets a higher asymptote."*
> **Sister docs:** `endpoint-universal-pressure.md` (per-provider pressure),
> `quota-pressure-design.md` (RP-EXP curve)

---

## TL;DR (one screen)

| Question | Verdict |
|---|---|
| Is asymptote the right knob for **preference**? | **No — not as the primary knob.** Asymptote only acts near quota exhaustion. At 0% usage it is invisible. Preference should apply *always*, so it belongs on `base_rate`. |
| What should asymptote express? | **Quota urgency** — "how painful is it when this endpoint nears its wall?" Not "do we like this endpoint." |
| What should `base_rate` express? | **Preference** — the actual $/M cost we want the router to feel for every token, at every usage level. |
| Can the two be combined? | Yes, but only as a **layered** model: `base_rate` = preference (always on), `asymptote` = urgency (kicks in near the wall). One knob conflates two concerns. |

**The one-line recommendation:** *base_rate = preference, asymptote = urgency.
Don't make Felix's insight do double duty.*

---

## 1. Is Asymptote the Right Knob for Preference?

### 1.1 What the asymptote actually does

Recall the RP-EXP curve (`pricing_engine.py:395`, `_single_window_factor`):

```
                    ┌─ 1.0                          if u ≤ onset
pressure(u)  =      │
                    │   ⎛  u − onset ⎞
                    ├── 1 + K · ─────────────       if onset < u < 1.0
                    │   ⎝  1 − u      ⎠
                    │
                    └─ +∞                          if u ≥ 1.0

where  K = asymptote − 1.0
```

Two facts jump out:

1. **Below the onset, the curve is identically `1.0`.** The asymptote does *nothing*
   there. Changing asymptote from 2.0 → 10.0 has zero effect at u ≤ onset.
2. **The asymptote defines the value at the ramp midpoint**, `u = onset + ½·(1 − onset)`.
   For onset 0.70 that midpoint is u=0.85. Beyond it, pressure diverges toward +∞
   regardless of asymptote.

So asymptote is a knob that only lives in the **ramp region** — roughly the top
30% of the usage scale.

### 1.2 Why that's the wrong shape for "preference"

Felix's intent: *"prefer A over B always."* Concretely, A should be cheaper than
B at **0%, 50%, 90%, 99%** usage — every usage level.

Let's test whether asymptote expresses that. Two endpoints with the same
`base_rate = $0.024/M` and `onset = 0.70`, but different asymptotes:

| Usage | A: asymptote 2.0 | B: asymptote 6.0 | A cheaper? |
|---|---|---|---|
| 0% | $0.024 (1.0×) | $0.024 (1.0×) | **tie** ❌ |
| 50% | $0.024 (1.0×) | $0.024 (1.0×) | **tie** ❌ |
| 70% (onset) | $0.024 (1.0×) | $0.024 (1.0×) | **tie** ❌ |
| 80% | $0.044 (1.83×) | $0.071 (2.95×) | ✅ A |
| 85% (midpoint) | $0.048 (2.0×) | $0.144 (6.0×) | ✅ A |
| 90% | $0.062 (2.58×) | $0.214 (8.92×) | ✅ A |
| 95% | $0.106 (4.43×) | $0.613 (25.5×) | ✅ A |
| ≥100% | +∞ | +∞ | **tie** (both excluded) |

**The problem is visible at the top of the table.** For 70% of the usage range,
asymptote does literally nothing — A and B are equally priced. Felix's "prefer A"
intent is silently dropped until A is already near its wall.

> ⚠️ **The core mismatch:** preference is a *baseline* property; asymptote is a
> *near-exhaustion* property. Using asymptote for preference means **your
> preference is invisible for the majority of decisions** — exactly when traffic
> is flowing freely and the router is making its most consequential choices.

### 1.3 What asymptote *is* good for

Asymptote's real job: tuning **how sharp the cliff is near the wall** —
i.e. how aggressively the price repels traffic before exhaustion. That's a
*quota-preservation* signal, not a *preference* signal. Examples:

- **High asymptote (e.g. 6.0)** = "warning! back off NOW" — sharp repulsion.
  Good for endpoints with no extra-usage escape valve (z.ai 429s, ppq credits=0).
- **Low asymptote (e.g. 2.0)** = "gentle slope, keep using me a bit longer" —
  good for endpoints that genuinely allow overage (ollama extra-usage tier).

So asymptote has a legitimate meaning — it's just **urgency**, not preference.

---

## 2. All the "Dials" We Have for Expressing Preference

Map every knob that can make the router prefer endpoint A over B:

| Dial | Where in pipeline | Acts at 0% usage? | Acts near wall? | Effect per unit | Cost of changing it |
|---|---|---|---|---|---|
| **`base_rate`** ($/M) | Bottom of the stack — everything multiplies it | ✅ **yes** | ✅ yes | Linear, everywhere | Side effects: amortization math, Kalman convergence, billing truth |
| **`onset`** (0–1) | Start of ramp | ❌ no | ✅ yes (when pressure begins) | Shifts ramp left/right | Side effects: changes *when* pressure kicks in, not how much |
| **`asymptote`** (multiplier) | Midpoint of ramp | ❌ no | ✅ yes (steepness) | Scales K in `1 + K·t/(1−t)` | Side effects: changes *how steep* the wall is |
| **`peak_multiplier`** | Time-of-day step | ❌ no (only during peak hours) | n/a | Discrete 1.0 / 3.0 step | Only meaningful for z.ai |
| **`pace_factor`** | Predictive pacing | ❌ only when burn-rate data exists | ✅ yes | Squared ratio, clamped [0.5, 3.0] | Per-window, orthogonal to preference |
| **`health_pricing_factor`** | Failure-based penalty | ❌ only on failures | n/a | Graduated tiers (1.5 / 3.0 / 10 / ∞) | Reactive — wrong tool for *intentional* preference |
| **NEW: `preference_weight`** (proposed) | Multiplier on base_rate before everything else | ✅ **yes** | ✅ yes | Linear, everywhere | Adds a knob; redundant with `base_rate` |

### 2.1 Which combination is cleanest?

**The base_rate already is the preference knob.** Look at the current values
(`live_router.py:146`, `_DEFAULT_CONVERGED_RATES`):

```
ours:          $0.001/M   ← cheapest (free to us, clamped floor)
friend:        $0.029/M   ← shared resource, 21% premium
ollama_cloud:  $0.024/M   ← prepaid, want to use it
ppq:           $0.140/M   ← per-token fallback
openrouter:    $0.135/M   ← per-token fallback
deepinfra:     $1.300/M   ← last resort
```

The router ALREADY prefers them in cost order — `base_rate` *is* doing the
preference work today. The cleanest architecture is to **leave that job with
base_rate** and reserve asymptote/onset for *quota urgency*.

A separate `preference_weight` knob is technically clean but redundant — it just
multiplies base_rate by a constant. We'd be inventing a new dial to do what
base_rate already does. **Don't add a knob when an existing one expresses the
same thing.**

### 2.2 The minimum sufficient preference model

```
effective_price = base_rate  ×  peak  ×  scarcity  ×  health  ×  pace  ×  pressure
                  ──────────                                              ─────────
                  ↑ PREFERENCE                                            ↑ URGENCY
                  (always on)                                             (near wall)
```

Two layers, two jobs:

- **base_rate** carries *all* baseline preference.
- **pressure** (onset + asymptote) carries *all* quota urgency.

No mixing. No double-duty. No new knob needed.

---

## 3. Per-Endpoint Preference — What Should It Be?

Mapping Felix's strategy onto the base_rate values (in priority order):

| Rank | Endpoint | Current base | Why this rank | Asymptote (urgency) |
|---|---|---|---|---|
| **1** | **z.ai — ours** | **$0.001** | Flat $155/mo, marginal cost ≈ $0. Use what we paid for. | 3.0 (peak parity — 429s hurt, no escape valve) |
| **2** | **ollama_cloud** | $0.024 | Prepaid, want to *use* it; slightly above ours because ours is "free." | 4.17 (allow extra-usage tier) |
| **3** | **z.ai — friend** | $0.029 | Free to us but a *shared* resource — polite to defer. 21% premium already baked in. | 3.0 (same z.ai 429 pain) |
| **4** | **openrouter** | $0.135 | Per-token, cheapest external. | off (∞ quota, no curve) |
| **5** | **ppq** | $0.140 | Per-token, slightly pricier; credits deplete. | 2.0 (gentle; ppq is already deprioritized by base) |
| **6** | **deepinfra** | $1.300 | Per-token, expensive. | off (∞ quota, no curve) |

> Note the order flips at ollama vs. friend: ollama ($0.024) is *cheaper* than
> friend ($0.029) by base_rate. If Felix wants friend preferred over ollama,
> that's a base_rate change (lower friend's rate or bump ollama's), not an
> asymptote change. **This is exactly the kind of question asymptote can't
> answer.**

### 3.1 How these preferences map to asymptote values — IF we tried

Just to show what happens if we follow Felix's idea literally. Suppose we want
the asymptote itself to express the rank. Holding base_rate constant at $0.024:

| Endpoint | "Rank" | Proposed asymptote | Price at u=0 | Price at u=85% | Match intent? |
|---|---|---|---|---|---|
| ours | 1 | 1.5 | $0.024 | $0.036 | At u=0 still tied with everyone — **no** ❌ |
| friend | 2 | 2.0 | $0.024 | $0.048 | Same problem ❌ |
| ppq | 5 | 5.0 | $0.024 | $0.144 | Cheaper than ppq's real $0.14 at u=0! **Wrong** ❌ |
| deepinfra | 6 | 10.0 | $0.024 | $0.288 | Cheaper than deepinfra's real $1.30 at u=0! **Wrong** ❌ |

You can't fix ppq/deepinfra being cheaper than they really are by tweaking the
asymptote, because **at u=0 the asymptote is invisible**. You'd have to lie about
base_rate — which means base_rate is *already* doing the preference job, and the
asymptote layer is just noise.

---

## 4. The Danger of Conflating Preference with Quota Pressure

This is the strongest argument against Felix's idea. Consider **ollama_cloud**:

> *"We prepay for ollama. We want to USE what we paid for — so ollama should be
> PREFERRED. But we also don't want to run out — so pressure near exhaustion
> should be STEEP."*

Translated into dials:

| Goal | Required dial value | Knob |
|---|---|---|
| Prefer ollama at low usage | LOW base_rate | `base_rate` |
| Steep pressure near wall | HIGH asymptote | `asymptote` |

Felix's rule ("preferred = lower asymptote") forces these into direct
contradiction:

- To *prefer* ollama → lower its asymptote.
- To *protect* ollama from exhaustion → raise its asymptote.

**You can't do both with one knob.** You'd have to pick: either ollama gets used
up too aggressively, or it never gets used at all. The exact same conflict
appears for:

- **ppq** (want to spend down credits, but want smooth exclusion as they run out)
- **z.ai friend** (shared — want to lean on it as backup, but back off hard if
  *its* 5h window fills)

The pattern is universal: **every quota'd endpoint has a "use me" intent AND a
"don't exhaust me" intent**, and those intents pull in opposite directions on
any single knob.

### 4.1 Why the two-knob split solves it cleanly

With `base_rate` = preference and `asymptote` = urgency:

| Endpoint | base_rate (preference) | asymptote (urgency) | Combined behaviour |
|---|---|---|---|
| **ollama_cloud** | $0.024 (preferred) | 4.17 (steep near wall) | Used aggressively at low usage; cleanly rerouted near exhaustion ✅ |
| **z.ai ours** | $0.001 (most preferred) | 3.0 (sharp wall — no overage) | Always cheapest until 5h fills, then 429 pressure kicks in ✅ |
| **ppq** | $0.140 (deprioritized) | 2.0 (gentle — already expensive) | Naturally avoided; smooth exclusion as credits deplete ✅ |

**Each intent gets its own dial. No compromise required.**

### 4.2 A visual: the conflation trap

```
Felix's one-knob model:
                                    ┌─────────────────────────┐
   PREFER  ────────────────────────▶│ asymptote               │
   PROTECT ────────────────────────▶│ (must serve BOTH goals) │ ← impossible
                                    └─────────────────────────┘

Two-knob model:
                                    ┌─────────────────────────┐
   PREFER  ────────────────────────▶│ base_rate               │ ✅
                                    ├─────────────────────────┤
   PROTECT ────────────────────────▶│ asymptote (+ onset)     │ ✅
                                    └─────────────────────────┘
```

---

## 5. Recommendation

### 5.1 Should asymptote express preference?

**No — not as a primary signal. Partial yes only as a *secondary* effect.**

- **Primary preference = `base_rate`.** Always-on, linear, applies at every
  usage level. It already encodes Felix's strategy correctly today.
- **Asymptote = quota urgency.** Steepness of the wall. Distinct concern.
- **A *tiny* secondary preference effect is fine** — e.g. setting asymptote
  lower for endpoints with no escape valve (z.ai 429s vs. ollama extra-usage).
  But that's *urgency tuning that happens to correlate with preference*, not
  preference itself.

### 5.2 The cleanest architecture

```
                  ┌──────────────────────────────────────────────┐
                  │           EFFECTIVE PRICE                    │
                  │                                              │
                  │   = base_rate   ←── PREFERENCE (always on)   │
                  │   ×  peak       ←── time-of-day (z.ai only)  │
                  │   ×  scarcity    ←── legacy, retire after     │
                  │   ×  health     ←── failures / breaker       │
                  │   ×  pace       ←── burn-rate pacing          │
                  │   ×  pressure   ←── URGENCY (onset+asymptote)│
                  │                                              │
                  └──────────────────────────────────────────────┘
```

Two intentional layers:

1. **Preference layer (`base_rate`).** Set this once per endpoint to express the
   strategic order. Doesn't move unless strategy changes.
2. **Urgency layer (`onset`, `asymptote`).** Set per endpoint to express *quota
   shape* — how short the window is, whether overage is allowed, how bad the
   wall is.

This is what `endpoint-universal-pressure.md` already proposes (§2.1–2.3). The
doc is correct; Felix's asymptote-as-preference idea would be a step *away* from
that clean separation.

### 5.3 Concrete per-endpoint config (final recommendation)

```yaml
# PREFERENCE lives here — sets the order at every usage level.
zai:
  keys:
    ours:   { base_rate: 0.001 }    # 1st — flat-fee, marginal $0
    friend: { base_rate: 0.029 }    # 3rd — shared resource
ollama_cloud: { base_rate: 0.024 }  # 2nd — prepaid, want to use
ppq:          { base_rate: 0.140 }  # 5th — per-token
openrouter:   { base_rate: 0.135 }  # 4th — cheapest external
deepinfra:    { base_rate: 1.300 }  # 6th — last resort

# URGENCY lives here — onset/asymptote per provider, NOT a preference signal.
zai:          { onset: 0.60, asymptote: 3.0 }   # sharp wall, no overage
ollama_cloud: { onset: 0.70, asymptote: 4.17 }  # extra-usage available
ppq:          { onset: 0.80, asymptote: 2.0 }   # gentle, already deprioritized
openrouter:   { pressure: off }                 # infinite quota
deepinfra:    { pressure: off }                 # infinite quota
```

### 5.4 How this maps to the Routstr node/client vision

ADR-007 (`routster-marketplace-intelligence.md`) describes the endgame:
**each Routstr node publishes a price; the client picks the cheapest.** In that
model:

| Concept | Routstr node/client | Our engine |
|---|---|---|
| What the client sees | Published price | `effective_price` after all multipliers |
| Node's preference for itself | The price it *chooses to publish* | `base_rate` |
| Node's response to its own load | How it raises its published price under demand | `pressure` (onset + asymptote) |
| Client-side preference | "Pick cheapest trusted" | Sort by `effective_price` |

The asymptote is **internal to a node** — it's how the node scales *its own*
price as *its own* quota depletes. It is **not** a lever the buyer pulls to say
"I prefer you." The buyer expresses preference by *choosing* — and chooses by
*price*, which is dominated by `base_rate`.

**If we ship Felix's asymptote-as-preference idea**, we'd be teaching our own
router a habit that doesn't survive contact with the Routstr marketplace: out
there, no one lets you tune their asymptote. You just see their price and pick.
Our internal model should mirror that — **preference = price = base_rate**, and
asymptote stays private to each provider's own depletion math.

---

## 6. One-Sentence Answer for Felix

> *"The asymptote is the shape of the cliff, not the height of the ground —
> preference is the ground (base_rate), urgency is the cliff (asymptote).
> Don't try to make one knob express both."*
