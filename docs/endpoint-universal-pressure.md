# Universal Endpoint Pressure: Every Quota-Based Endpoint Gets the Exponential Curve

> **Status:** Design (pre-implementation)
> **Author:** Pricing engine analysis (subagent)
> **Date:** 2026-08-05
> **Sister doc:** `docs/quota-pressure-design.md` (Ollama-only RP-EXP curve)
> **Felix's directive:** each endpoint is a self-contained pricing model (Routstr
> node pattern) — the price the router sees depends on **all** factors: remaining
> quota, peak hours, health, pace, scarcity. Not just ollama_cloud.

---

## 0. TL;DR (visual summary)

```
TODAY                                   TOMORROW (this doc)
────────                                ─────────────────────────────────
ollama_cloud  → exponential pressure    ALL three quota endpoints → exponential
z.ai          → flat $0.014/M           z.ai   → amortized base × pressure(5h,wk)
ppq           → flat $0.14/M            ppq    → per-token base × pressure(credits)
                                          └─ scarcity_factor RETIRED (subsumed)
```

The single curve from `quota-pressure-design.md` (RP-EXP rational asymptote)
becomes **per-provider**: each endpoint gets its own `onset`, `asymptote`, and
window source. The router still does one thing — sort by `effective_price`.

---

## 1. Current State (the asymmetry)

| Provider | Base rate source | Has quota windows? | Pressure today? |
|---|---|---|---|
| **z.ai (ours/friend)** | `_measure_zai_amortized`: `monthly_fee / monthly_tokens` (`realtime_pricing.py:393`) | **YES** — `5h`, `weekly`, `monthly` (`quota_window_extractor.py:47`, `providers.yaml:14`) | ❌ flat — only `peak_multiplier` + `scarcity` apply |
| **ollama_cloud** | `EXTRA_USAGE_BASE_RATE = $0.024/M` + Kalman | YES — `5h session`, `7d weekly` | ✅ **only endpoint with `quota_pressure_factor`** (`live_router.py:525`) |
| **ppq** | `ppq_ledger`: `SUM(cost_usd)/SUM(tokens)` (`realtime_pricing.py:558`); cold-start `$0.14/M` | Credits ($ balance), no time window | ❌ flat — treated as `inf` quota |
| openrouter / deepinfra | per-token, no quota | none | ❌ flat (correct — infinite quota) |

**The bug:** z.ai has *real* rate limits (you get 429s when the 5h window is
full), but its price doesn't reflect depletion. PPQ runs out of credits, but its
price stays flat. Only ollama_cloud has the smart curve.

---

## 2. Per-Provider Pressure Model

The same RP-EXP formula, parameterized per provider:

```
pressure(u) = 1.0                       if u ≤ onset
            = 1 + K · t / (1 − t)       if onset < u < 1.0     (t = (u−onset)/(1−onset))
            = +∞                        if u ≥ 1.0

   K = asymptote − 1.0
```

Where **`u` is the worst-window usage fraction** (priority: never run out) and
`onset`, `asymptote`, and the window source are **per-provider**:

### 2.1 z.ai (ours + friend keys)

| Knob | Value | Rationale |
|---|---|---|
| **base rate** | **amortized, not flat**: `monthly_fee / (tokens_this_month / 1e6)` — ALREADY implemented in `_measure_zai_amortized`. For a `$300/yr` plan at ~21B tokens/yr → **$0.0143/M** (matches the $0.014/M folklore). | The base *is* the real cost-per-token; it drops as you amortize. Pressure multiplies on top. |
| **windows** | `5h` + `weekly` (+ `monthly` as a soft signal). Source: `quota_window_extractor.extract_quota_windows()` already parses these from the z.ai quota API. | z.ai's 5h window is the live rate-limit; weekly is the soft cap. |
| **`u`** | `max(used_pct_5h, used_pct_weekly) / 100` | Worst window governs (same as ollama). |
| **onset** | **0.60** (earlier than ollama's 0.70) | z.ai's 5h window is much shorter (2M tokens vs 500M), so exhaustion comes faster. Earlier onset = more headroom. |
| **asymptote** | **3.0** (peak multiplier ratio, not an "extra rate") | z.ai has no paid extra-usage tier — once the window is full you get 429s. The asymptote models "how bad is hitting the wall". 3.0 ties to the peak multiplier so off-peak-pressure ≈ peak-price, making rerouting natural. |
| **at u=1.0** | `+∞` → router picks friend key first, then ollama/externals | Same guarantee as ollama: the router always finds a cheaper alternative. |

**Concrete z.ai pressure table** (base $0.014/M, onset=0.60, K=2.0):

| 5h usage | Pressure | ours effective | friend effective ($0.014×1.21) | Cheaper? |
|---|---|---|---|---|
| 50% | 1.00 | $0.014 | $0.017 | **ours** |
| 60% | 1.00 | $0.014 | $0.017 | **ours** (onset) |
| 75% | 1.86 | $0.026 | $0.032 | **ours** (still cheapest) |
| 85% | 3.67 | $0.051 | $0.063 | **friend** if ours is the one depleting |
| 95% | 11.0 | $0.154 | — | **ollama/externals win** |
| 100% | +∞ | unreachable | — | always rerouted |

### 2.2 ollama_cloud (unchanged — already done)

| Knob | Value |
|---|---|
| base | `$0.024/M` (converged: `$0.023952`) |
| windows | `5h session` (500M) + `7d weekly` (3.5B) |
| onset | **0.70** (current `QUOTA_PRESSURE_ONSET`) |
| asymptote | **4.17** (`EXTRA_USAGE_MULTIPLIER` = `$0.10/$0.024`) |
| at u=1.0 | `+∞` |

No change needed — this is the reference implementation.

### 2.3 PPQ (credit-based)

| Knob | Value | Rationale |
|---|---|---|
| **base rate** | per-token measured: `SUM(cost_usd)/SUM(tokens)` from `ppq_queries` (`_measure_ppq_ledger`); cold-start `$0.14/M`. | PPQ is genuinely per-token — no amortization. |
| **window source** | **credits**: `u = 1 − (credits_remaining / credits_start)` | PPQ has no time window; credits are a finite pool that only refills on payment. |
| **`u`** | single fraction (no weekly/session — credits are monotonic until topped up) | Simpler than time-windowed providers. |
| **onset** | **0.80** (later than z.ai/ollama) | Credits deplete slowly and are refilled manually; you don't want PPQ price spiking on day 1 of a fresh top-up. Onset later = use the cheap credits first. |
| **asymptote** | **2.0** (the "cost of acquiring new credits" premium) | When credits are low, the marginal cost of the next token includes the friction of re-funding. 2.0 models "you'll pay ~2× in operational overhead to keep PPQ alive". |
| **at u=1.0** | `+∞` | No credits = no service. Router falls through to openrouter/deepinfra. |

**PPQ pressure table** (base $0.14/M, onset=0.80, K=1.0):

| Credits used | Pressure | PPQ effective | openrouter ($0.135) | Cheaper? |
|---|---|---|---|---|
| 50% | 1.00 | $0.140 | $0.135 | **openrouter** |
| 80% | 1.00 | $0.140 | $0.135 | **openrouter** (onset) |
| 90% | 1.50 | $0.210 | $0.135 | **openrouter** |
| 95% | 2.33 | $0.326 | $0.135 | **openrouter** (PPQ deprioritized) |
| 100% | +∞ | unreachable | — | always rerouted |

> ⚠️ **Note:** PPQ is already more expensive than openrouter at baseline, so
> pressure mainly ensures PPQ gets *fully excluded* as credits deplete, rather
> than competing on a tie. The curve's main value is the smooth exclusion at
> `u→1.0` instead of a hard cliff.

### 2.4 Per-provider config block (proposed `providers.yaml`)

```yaml
zai:
  pressure:
    enabled: true
    windows: [5h, weekly]          # which windows feed `u` (max governs)
    onset: 0.60
    asymptote: 3.0                 # = peak_multiplier (ties to peak pricing)

ollama_cloud:
  pressure:
    enabled: true
    windows: [5h, weekly]
    onset: 0.70
    asymptote: 4.17                # = extra_rate / base_rate

external:
  ppq:
    pressure:
      enabled: true
      source: credits              # not a time window — a balance
      onset: 0.80
      asymptote: 2.0               # re-funding friction premium
  openrouter:
    pressure: {enabled: false}     # infinite quota — no pressure
  deepinfra:
    pressure: {enabled: false}
```

---

## 3. Answering the Six Questions

### Q1: Should z.ai get exponential pressure on 5h+7d? What's its quota structure?

**YES.** z.ai exposes three windows via `/api/coding/paas/v4/quota`
(`providers.yaml:14`, `quota_window_extractor.py:47`):
- **5-hour session** (`"5-hour"`) — the live rate-limit window (~2M tokens)
- **weekly** (`"weekly"`) — 7-day rolling cap
- **monthly** (`"monthly"`) — billing period (soft; drives amortization, not pressure)

Pressure should use **`max(5h, weekly)`** — same "worst window governs" rule as
ollama. The monthly window feeds `base_rate` (amortization), **not** pressure.

### Q2: How does PPQ's credit depletion map to the curve?

**`u = 1 − (credits_remaining / credits_start)`** — exactly as the question
proposes. Credits are a monotonic-depleting pool, so there's a single `u` (no
max-of-windows). The PPQ balance collector already exists conceptually (the
`dq05_monitor` MCP fetches `/credits/balance`); it needs to feed `live_router`.

At `u ≥ 1.0` (credits = 0) → `+∞`, router falls through to openrouter/deepinfra.

### Q3: Should onset be per-provider configurable?

**YES — mandatory.** The windows have different shapes:
- z.ai 5h is **short and sharp** (2M tokens, burns in <1h of heavy use) → **onset 0.60**
- ollama 5h is **large** (500M tokens, hours of headroom) → **onset 0.70**
- PPQ credits are **slow and monotonic** (no reset pressure) → **onset 0.80**

A single global onset would either trigger z.ai too late (429s) or PPQ too early
(needless price spikes on fresh credits). Per-provider onset in `providers.yaml`
(§2.4) is the clean answer.

### Q4: How should z.ai's base rate be computed from $300/yr?

**Measured amortization, not a fixed flat rate.** The formula is already
implemented (`realtime_pricing.py:393`, `_measure_zai_amortized`):

```
base_rate = fee / (tokens_consumed_in_period / 1e6)
```

For a `$300/yr` plan:
- If the period is **monthly** ($25/mo) and you've used 1.75B tokens this month →
  `$25 / 1750 = $0.0143/M` ✓ (matches the `$0.014/M` folklore).
- As the month progresses and tokens accumulate, the rate **drops** (amortization).
- At month reset, it spikes back up (few tokens, same fee) then decays.

**The pressure multiplier is applied ON TOP of this amortized base.** So z.ai's
effective price = `amortized_base × peak × quota_pressure × health × pace`.

> **Recommendation:** extend the lookback window in `_measure_zai_amortized` from
> "month-to-date" to a configurable period (default: month-to-date, option:
> trailing 30d or trailing 365d). Yearly amortization ($300/365d-tokens) gives a
> smoother base that doesn't reset monthly. This is a **separate, small change**
> — not blocking the pressure work.

### Q5: Does scarcity_factor stay or get subsumed?

**SUBSUMED — retire `scarcity_factor` once all three endpoints have pressure.**

**Why:** `scarcity_factor` (`pricing_engine.py:188`) is a *generic linear ramp*
on `quota_used_pct` (onset 50%, reaches 2.0× at 100%). It applies to **all**
providers via `routing_optimizer._evaluate_provider:329`. But:

1. For providers **with pressure** (z.ai, ollama, ppq): `quota_pressure_factor`
   is a *strictly better* model — it's per-window, exponential, and configurable.
   Applying both double-counts the depletion signal. (Today this is hacked around
   by passing `quota_total=None` for ollama when pressure is on —
   `live_router.py:686-690`.)
2. For providers **without pressure** (openrouter, deepinfra): they have `inf`
   quota, so `scarcity_factor` is always 1.0 anyway — it's a no-op.

Once pressure is universal, `scarcity_factor` is either redundant or inert. **Kill it.**

**Migration:** keep `scarcity_factor` during the transition; once the last
provider (PPQ) gets pressure, set `scarcity = 1.0` everywhere and delete the
function + its call in `routing_optimizer.py:320-329`.

### Q6: Migration path — what changes in `live_router.py`?

The pressure computation is currently **outside** the per-provider loop
(`live_router.py:524-531`) and applied only to ollama_cloud **inside** the loop
(`live_router.py:653-664`). The fix is to **move pressure computation inside the
loop**, keyed by provider. Detailed in §4.

---

## 4. Code Changes (file · line · what)

### 4.1 `config/providers.yaml` — add per-provider pressure blocks

Add the `pressure:` sub-block to each provider (§2.4). No existing keys change.

### 4.2 `src/pricing_engine.py` — generalize `quota_pressure_factor`

**Current** (`pricing_engine.py:395`): hardcoded defaults for ollama
(`onset=QUOTA_PRESSURE_ONSET`, `asymptote=EXTRA_USAGE_MULTIPLIER`).

**Change:** the signature already accepts `onset` and `asymptote` as params —
**no function change needed**. Just add a helper that reads per-provider config:

```python
# NEW (after quota_pressure_factor, ~line 496)
_PRESSURE_DEFAULTS = {
    "zai":          {"onset": 0.60, "asymptote": 3.0},
    "ollama_cloud": {"onset": 0.70, "asymptote": 4.17},
    "ppq":          {"onset": 0.80, "asymptote": 2.0},
}

def pressure_params(provider: str, config: dict | None = None) -> tuple[float, float]:
    """Resolve (onset, asymptote) for a provider from config or defaults."""
    ...
```

### 4.3 `src/live_router.py` — move pressure into the per-provider loop

This is the **core change**. Three edits:

**Edit A — line 524-531:** Move the ollama-only pressure block. Replace:

```python
quota_pressure = 1.0
if _QUOTA_PRESSURE_ENABLED and session_usage_frac > 0:
    try:
        quota_pressure = quota_pressure_factor(session_usage_frac)
    except Exception:
        quota_pressure = 1.0
self._last_quota_pressure = quota_pressure
```

With a **per-provider pressure dict** computed from each provider's windows:

```python
# Compute pressure per provider (replaces the ollama-only block).
provider_pressures: dict[str, float] = {}
if _UNIVERSAL_PRESSURE_ENABLED:
    # z.ai: from quota_state windows (already parsed by quota_window_extractor)
    for zk in ("ours", "friend"):
        usage_frac = _zai_usage_frac(quota_state.get(zk, {}))  # max(5h, weekly)
        if usage_frac > 0:
            provider_pressures[zk] = quota_pressure_factor(
                usage_frac, onset=0.60, asymptote=3.0)
    # ollama_cloud: from API (unchanged)
    if session_usage_frac > 0:
        provider_pressures["ollama_cloud"] = quota_pressure_factor(
            session_usage_frac, onset=0.70, asymptote=4.17)
    # ppq: from credits balance
    ppq_frac = _ppq_credit_frac()  # 1 - (remaining/start)
    if ppq_frac > 0:
        provider_pressures["ppq"] = quota_pressure_factor(
            ppq_frac, onset=0.80, asymptote=2.0)
self._last_quota_pressure = provider_pressures.get("ollama_cloud", 1.0)
```

**Edit B — line 651-664 (the per-provider base_rate block):** Generalize from
ollama-only to any provider with a pressure entry. Replace:

```python
if name == "ollama_cloud" and _QUOTA_PRESSURE_ENABLED:
    if math.isinf(quota_pressure):
        healthy = False
    else:
        base_rate = base_rate * quota_pressure
elif name == "ollama_cloud" and extra_mult != 1.0:
    ...
```

With:

```python
prov_pressure = provider_pressures.get(name, 1.0)
if prov_pressure != 1.0:
    if math.isinf(prov_pressure):
        healthy = False           # +∞ → unreachable via breaker
    else:
        base_rate = base_rate * prov_pressure
# (legacy ollama extra_mult path stays as fallback when pressure is off)
```

**Edit C — line 686-690 (scarcity neutralization):** Generalize. Replace:

```python
oc_pressure_on = (name == "ollama_cloud" and _QUOTA_PRESSURE_ENABLED)
prov_quota_total = (
    None if oc_pressure_on
    else (total if total != float("inf") else None)
)
```

With:

```python
# Any provider with active pressure has scarcity neutralized (no double-count).
prov_has_pressure = name in provider_pressures and provider_pressures[name] != 1.0
prov_quota_total = (
    None if prov_has_pressure
    else (total if total != float("inf") else None)
)
```

### 4.4 New helpers needed in `live_router.py`

```python
def _zai_usage_frac(quota_dict: dict) -> float:
    """Extract max(5h, weekly) usage fraction for a z.ai key from quota_state."""
    # quota_state[key] = {'used_pct': ..., 'windows': [{'name': '5-hour', ...}, ...]}
    # or parse from the proxy's quota_cache structure.

def _ppq_credit_frac() -> float:
    """1 - (credits_remaining / credits_start) from the PPQ balance collector."""
    # Calls the same /credits/balance endpoint as dq05_monitor's dq05_ppq tool.
    # Cache for 5 min (same TTL as CPVO cache).
```

### 4.5 Kill switches (env vars)

```python
_UNIVERSAL_PRESSURE_ENABLED = (
    os.environ.get("UNIVERSAL_QUOTA_PRESSURE_ENABLED", "false").lower()
    in ("1", "true", "yes")
)
_ZAI_PRESSURE_ENABLED = (
    os.environ.get("ZAI_QUOTA_PRESSURE_ENABLED", "false").lower()
    in ("1", "true", "yes")
)
_PPQ_PRESSURE_ENABLED = (
    os.environ.get("PPQ_QUOTA_PRESSURE_ENABLED", "false").lower()
    in ("1", "true", "yes")
)
# ollama keeps its existing OLLAMA_QUOTA_PRESSURE_ENABLED.
```

Each provider can be toggled independently for staged rollout.

### 4.6 Summary of changes

| File | Change | LOC |
|---|---|---|
| `config/providers.yaml` | Add `pressure:` block per provider | ~20 |
| `src/pricing_engine.py` | Add `_PRESSURE_DEFAULTS` + `pressure_params()` helper | ~15 |
| `src/live_router.py` | Move pressure into per-provider loop (Edits A/B/C); add `_zai_usage_frac`, `_ppq_credit_frac`; add kill switches | ~60 |
| `src/routing_optimizer.py` | No change (pressure flows via `base_rate` mutation, as today) | 0 |
| `tests/test_universal_pressure.py` | New — z.ai pressure, PPQ pressure, scarcity neutralization | ~150 |
| `tests/test_quota_pressure_routing.py` | Update — ollama tests still pass; add z.ai + ppq scenarios | ~50 |

---

## 5. Migration Path (implementation order)

```
Phase 0 ── Config scaffolding (no behavior change)
  │  • Add pressure: blocks to providers.yaml
  │  • Add _PRESSURE_DEFAULTS + pressure_params() to pricing_engine.py
  │  • Add kill switches (all default OFF)
  │  • All existing tests pass unchanged
  ▼
Phase 1 ── z.ai pressure (FIRST — highest impact)
  │  • Wire _zai_usage_frac() from quota_state windows
  │  • Enable ZAI_QUOTA_PRESSURE_ENABLED=true in shadow mode
  │  • Validate: z.ai price rises as 5h window fills; router picks friend
  │    key or ollama when ours is depleted, BEFORE the 429
  │  • 7-day soak: confirm no premature reroutes (onset tuning)
  ▼
Phase 2 ── PPQ pressure (SECOND — needs credits collector)
  │  • Wire _ppq_credit_frac() from /credits/balance
  │  • Enable PPQ_QUOTA_PRESSURE_ENABLED=true in shadow mode
  │  • Validate: PPQ excluded smoothly as credits → 0
  │  • Lower risk: PPQ is already expensive; pressure just smooths exclusion
  ▼
Phase 3 ── Retire scarcity_factor
  │  • All three providers now have pressure
  │  • Set scarcity = 1.0 in routing_optimizer._evaluate_provider
  │  • Remove scarcity_factor() + SCARCITY_* constants from pricing_engine.py
  │  • Remove the prov_quota_total=None hack (no longer needed)
  ▼
Phase 4 ── Cleanup
  • Remove legacy extra_usage_multiplier (EU-R3) — fully superseded
  • Remove RP-5 throttle (RP-5) — pressure subsumes it
  • Remove _QUOTA_PRESSURE_ENABLED (rename to UNIVERSAL_QUOTA_PRESSURE_ENABLED)
  • Update docs/quota-pressure-design.md to reference this doc
```

**Why z.ai first:** it's the primary provider, has the most traffic, and its
429s are the most disruptive. Getting z.ai pressure right gives the biggest
production win. PPQ is a fallback — lower urgency.

**Why scarcity last:** it's the safety net during the transition. Only remove it
once all three endpoints are validated with pressure.

---

## 6. Open Questions for Felix

1. **z.ai asymptote = 3.0?** This ties pressure to the peak multiplier
   (off-peak-pressure ≈ peak-price). Alternative: use the friend-key premium
   (1.21×) so pressure makes "ours" cost as much as "friend" at the wall.
   *Recommendation: 3.0 (peak parity) — stronger reroute signal.*

2. **PPQ asymptote = 2.0?** Models re-funding friction. Alternative: tie it to
   the deepinfra price ratio ($1.30/$0.14 ≈ 9.3×) so PPQ pressure makes it as
   expensive as the worst alternative. *Recommendation: 2.0 (conservative) — PPQ
   is already deprioritized by baseline price.*

3. **Monthly window for z.ai pressure?** Currently excluded (feeds base_rate
   only). Including it would add a third `u` to the max — more conservative but
   noisier. *Recommendation: exclude — monthly is a billing period, not a
   rate-limit.*

4. **z.ai base rate lookback?** Month-to-date (current) vs trailing-30d vs
   trailing-365d. Trailing-365d gives the smoothest base but masks monthly
   resets. *Recommendation: keep month-to-date for now; revisit after pressure
   is live.*
