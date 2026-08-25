# DESIGN: Kalman-Based Cost Estimator for Urgency Options (CG-12)

**Status:** proposed — ready for CG-12 implementation
**Date:** 2026-08-22
**Author:** consultant (glm-5.2), for operator review
**Related:** `DESIGN-urgency-enforcement.md` (CG-11), `src/token_predictor.py` (CG-3),
`~/.hermes/bot/burn_predictor.py` (Kalman), `~/.hermes/bot/kalman_health.py` (--collect)

---

## 0. Problem

CG-11 makes urgency **enforceable** (NULL-gate, price-tier gate, tick watchdog).
But the *ask* itself — "NOW/SOON/DEFER/BATCH?" — presents the operator with a
semantic choice and no economic signal. The operator guesses at cost from
context; the skill's decision matrix shows quota state but not **dollars**.

This design adds one pure function that **surfaces predicted cost + confidence
interval next to each urgency option** in the clarify question. The operator
sees:

```
NOW:   ~$0.25 ± $0.05  (paid failover, ~56K tokens @ $0.47/M)
SOON:  ~$0.00 ± $0.00  (quota resets in ~2h, free dispatch)
DEFER: ~$0.00 ± $0.00  (same, cheapest window)
BATCH: ~$0.00 ± $0.00  (free only)
BUT:   waiting costs ~$1.38 in bleed (2h × $0.69/h)
```

instead of a bare four-way choice. The cost makes the trade-off visible: a
NOW-justified task that's actively bleeding may cost **more** to defer than to
dispatch on paid failover. The estimator makes that implicit arithmetic explicit.

---

## 1. Design principles

- **Composition, not new math.** The innovation is composing three existing
  signals — (a) the production Kalman filter's burn trajectory, (b) the
  `price_observations` table's measured $/M per provider, (c) the
  `api_calls` table's per-model token distribution — into a single per-urgency
  cost number. No new Kalman filter, no ML model, no training step.
- **Pure function.** `estimate_cost(urgency, task_tokens, current_state)` takes
  pre-fetched state and returns a dict. The DB-reading wrapper is separate
  (see §5). The pure core is testable without a live DB.
- **Always answers.** Like `predict_tokens`, the estimator returns a number
  even when data is missing. Degraded signals fall back to conservative
  defaults; the confidence flag surfaces the degradation.
- **Read-only.** The estimator reads `zai_usage.db` and `zai_state.json`
  exclusively via `mode=ro` URI connections. It never writes.
- **~120 lines.** One module, stdlib only, no numpy dependency for the pure
  core (the Kalman math lives in `burn_predictor.py` and is read by reference,
  not re-implemented).

---

## 2. Cost model — three components

The cost of dispatching a task with urgency `u` has three components. Each is
computed independently and summed; any component can be $0 depending on state.

### 2.1 DIRECT — the token cost of the dispatch itself

```
direct_cost = estimated_tokens × effective_price_per_M / 1_000_000
```

- **If free quota is available** (zai flat sub, `friend_token_pct > 0` or
  `friend_available && token_pct > 0`): `effective_price = 0` (flat sub
  covers it). Direct cost = $0.
- **If quota is exhausted** (both keys at 0%): the dispatch falls over to
  paid providers. `effective_price` = the cheapest measured rate from
  `price_observations` (currently OpenRouter at $0.47/M, or PPQ glm-5.2 at
  $0.26/M if that provider is live).
- **Fallback rate** when no measured price exists: the known rate table
  (OpenRouter $0.47/M, PPQ $0.26/M, DeepInfra $1.30/M).

### 2.2 BLEED — the cost of NOT dispatching (waiting while a problem is active)

```
bleed_cost = bleed_rate_per_hour × time_until_dispatch_hours
```

- **Bleed rate** = recent paid spend rate from `api_calls` / `daily_spend`.
  Computed as: `SUM(cost_usd WHERE cost_usd > 0 AND ts > now - 24h) / 24`.
  Current measured: ~$0.69/h ($16.41 / 24h).
- **Time until dispatch** = when the next free/cheap window opens:
  - `NOW`: 0 (dispatching immediately, bleed stops).
  - `SOON`: time until quota resets (from Kalman `exhausts_in_hours`, or
    the zai quota API `friend_reset_ms`, or a conservative default).
  - `DEFER`/`BATCH`: same as SOON but potentially longer (waits for the
    cheapest tier, not just any non-expensive tier).
- **Only applies to NOW-justified tasks** (active bleed — a task that's
  bleeding money because a fix hasn't landed). A BATCH task by definition
  has no bleed; DEFER only bleeds if the operator marks the urgency as
  NOW-justified in the task note.

### 2.3 OPPORTUNITY — the cost of wasting a free window

```
opportunity_cost = 0  (soft signal, not a dollar amount)
```

If free quota is available NOW but the operator chooses DEFER, the window
may close (Kalman predicts when). This is not a dollar cost — it's a
**timing risk** surfaced as a note ("quota window closes in ~3h") rather
than a number. The operator's judgment handles this; the estimator just
makes the window visible.

---

## 3. The pure function

```python
def estimate_cost(
    urgency: str,           # "now" | "soon" | "defer" | "batch"
    task_tokens: int,       # from token_predictor.predict_tokens p90
    current_state: dict,    # pre-fetched: quota, price, bleed, kalman
) -> dict:
    """
    Returns:
    {
        "urgency": "now",
        "cost_usd": 0.25,          # point estimate
        "confidence_interval": [0.20, 0.30],  # ±1σ band
        "confidence": "medium",     # low | medium | high
        "breakdown": {
            "direct": 0.25,         # token cost
            "bleed": 0.00,          # waiting cost
            "opportunity": 0.00,    # window risk (informational)
        },
        "explanation": "paid failover, ~56K tokens @ $0.47/M",
        "bleed_note": None,         # "waiting costs ~$1.38 in bleed (2h × $0.69/h)"
    }
    """
```

`current_state` is a dict with these keys (all optional — missing → fallback):

| Key | Type | Source | Fallback |
|---|---|---|---|
| `free_quota_available` | bool | `zai_state.json` friend_token_pct > 0 | False |
| `free_price_per_m` | float | `price_observations` provider=friend | 0.001 |
| `paid_price_per_m` | float | `price_observations` cheapest measured | 0.47 |
| `bleed_rate_per_hour` | float | `api_calls` SUM(cost)/24h | 0.0 |
| `quota_resets_in_hours` | float\|None | Kalman `exhausts_in_hours` or `zai_state` reset_ms | None |
| `kalman_uncertainty` | float | Kalman `uncertainty` (tokens) | 0 |
| `token_std` | float | `api_calls` per-model std (sqrt(var)) | 0 |

### 3.1 Per-urgency logic

| Urgency | direct | bleed | notes |
|---|---|---|---|
| `now` | `free ? $0 : tokens × paid_price / 1M` | $0 (dispatch stops bleed) | If free quota → $0 total |
| `soon` | $0 if quota resets before dispatch, else paid | `bleed_rate × resets_in_hours` | Bleed may exceed direct savings |
| `defer` | $0 (waits for cheapest window) | `bleed_rate × resets_in_hours` (or longer) | Bleed is the real cost |
| `batch` | $0 (free only) | $0 (no active bleed by definition) | Cheapest, but may never dispatch if free never opens |

### 3.2 Confidence interval

The ±band is composed from two independent uncertainty sources:

1. **Token estimate uncertainty**: `token_std / task_tokens × direct_cost`.
   If `token_std` is unknown, use a default 30% relative error.
2. **Timing uncertainty** (bleed component): Kalman `uncertainty` on
   `exhausts_in_hours` → bleed_rate × uncertainty_hours. If Kalman is
   unconverged, widen the band and flag `low` confidence.

The interval is `[cost - band, cost + band]` where `band = sqrt(σ_token² + σ_timing²)`.

Confidence levels:
- `high`: token confidence ≥ medium (n ≥ 30) AND Kalman uncertainty < 25% of
  exhausts_in_hours (or no timing component needed).
- `medium`: token confidence ≥ low OR Kalman uncertainty < 50%.
- `low`: cold model (no token history) OR Kalman unconverged OR missing
  price data.

---

## 4. Data sources (all read-only)

```
~/.hermes/bot/zai_usage.db (mode=ro):
  api_calls           → per-model token distribution (mean, var, n)
                        + 24h paid spend for bleed rate
  price_observations  → latest rate_per_m per provider/model
  kalman_samples      → latest burn_rate_tph, exhausts_in_hours, uncertainty
  key_decisions       → latest ours_pct, friend_pct (quota state)
  daily_spend         → today's spend by tier (cross-check)

~/.hermes/bot/zai_state.json (read):
  friend_token_pct, friend_available, friend_reset_ms
  (authoritative current quota state, more real-time than kalman_samples)

src/token_predictor.py (imported, not duplicated):
  predict_tokens() → p50/p90 token estimate + confidence per model
  (already seeded from the same api_calls table)
```

### 4.1 Why not just read the Kalman live?

`burn_predictor.predict_all()` returns the live Kalman state, but it requires
importing the production module and may fail if the proxy is down. The
estimator reads `kalman_samples` (the 5-min-collected snapshot) as the primary
source, and falls back to `zai_state.json` for the current quota percentage.
This makes the estimator self-contained — it doesn't depend on the proxy
process being alive.

---

## 5. Implementation — `src/urgency_cost_estimator.py`

**~120 lines, stdlib only, no numpy.**

### 5.1 Module structure

```python
# src/urgency_cost_estimator.py

# ── constants ──
DEFAULT_PAID_PRICE_PER_M = 0.47   # OpenRouter measured 2026-08-22
DEFAULT_FREE_PRICE_PER_M = 0.001  # zai flat sub (amortized)
DEFAULT_TOKEN_STD_FRACTION = 0.30  # 30% relative error when no history
LOW_CONFIDENCE_THRESHOLD = 0.50  # uncertainty > 50% of timing → low

# ── fetch_current_state(db_path) → dict ──
# Reads zai_usage.db (ro) + zai_state.json. Returns the current_state dict.
# All queries are wrapped in try/except; missing data → None (degraded).

# ── estimate_cost(urgency, task_tokens, current_state) → dict ──
# The pure function. No I/O. Takes pre-fetched state.

# ── format_cost(estimate_dict) → str ──
# Formats the dict as the operator-facing string:
# "NOW:   ~$0.25 ± $0.05  (paid failover, ~56K tokens @ $0.47/M)"
```

### 5.2 `fetch_current_state` — the I/O wrapper

```python
def fetch_current_state(db_path=DEFAULT_DB_PATH):
    """Read zai_usage.db (ro) + zai_state.json. Return current_state dict."""
    state = {}
    # 1. quota state from zai_state.json
    # 2. latest price from price_observations (per provider, pick cheapest paid)
    # 3. latest Kalman sample (burn_rate_tph, exhausts_in_hours, uncertainty)
    # 4. bleed rate: SUM(cost_usd WHERE cost>0 AND ts > now-24h) / 24
    # 5. token stats: per-model mean+var from api_calls (last 7d)
    return state
```

### 5.3 `estimate_cost` — the pure core

```python
def estimate_cost(urgency, task_tokens, current_state):
    """Pure: compute direct + bleed + opportunity for one urgency level."""
    free = current_state.get("free_quota_available", False)
    paid_price = current_state.get("paid_price_per_m", DEFAULT_PAID_PRICE_PER_M)
    bleed_rate = current_state.get("bleed_rate_per_hour", 0.0)
    resets_in = current_state.get("quota_resets_in_hours")

    # DIRECT
    if free:
        direct = 0.0
        reason = "free quota available"
    else:
        direct = task_tokens * paid_price / 1_000_000
        reason = f"paid failover, ~{task_tokens//1000}K tokens @ ${paid_price:.2f}/M"

    # BLEED (only for now-justified tasks that are actively bleeding)
    bleed = 0.0
    bleed_note = None
    if urgency != "now" and bleed_rate > 0 and resets_in is not None:
        bleed = bleed_rate * resets_in
        bleed_note = f"waiting costs ~${bleed:.2f} in bleed ({resets_in:.1f}h × ${bleed_rate:.2f}/h)"

    # SOON/DEFER: if quota will reset before we need it, direct → $0
    if urgency in ("soon", "defer", "batch") and resets_in is not None:
        direct = 0.0
        reason = f"quota resets in ~{resets_in:.1f}h, free dispatch"

    # BATCH: always $0 (free only — if no free, it doesn't dispatch)
    if urgency == "batch":
        direct = 0.0
        reason = "free only"

    # NOW: dispatching now stops the bleed
    if urgency == "now":
        bleed = 0.0
        bleed_note = None

    # Confidence interval
    token_std = current_state.get("token_std", task_tokens * DEFAULT_TOKEN_STD_FRACTION)
    sigma_token = (token_std / max(task_tokens, 1)) * direct
    sigma_timing = bleed_rate * (current_state.get("kalman_uncertainty_hours", 0) or 0)
    band = (sigma_token**2 + sigma_timing**2) ** 0.5

    confidence = _confidence(current_state, band, direct)

    return {
        "urgency": urgency,
        "cost_usd": round(direct + bleed, 4),
        "confidence_interval": [round(direct + bleed - band, 4),
                                round(direct + bleed + band, 4)],
        "confidence": confidence,
        "breakdown": {"direct": round(direct, 4), "bleed": round(bleed, 4)},
        "explanation": reason,
        "bleed_note": bleed_note,
    }
```

### 5.4 `format_all_urgencies` — the operator-facing output

```python
def format_all_urgencies(task_tokens, current_state):
    """Return the multi-line string for the clarify question."""
    lines = []
    for u in ("now", "soon", "defer", "batch"):
        est = estimate_cost(u, task_tokens, current_state)
        ci = est["confidence_interval"]
        lines.append(
            f"{u.upper():5} ~${est['cost_usd']:.2f} ± ${((ci[1]-ci[0])/2):.2f}  ({est['explanation']})"
        )
    # bleed note (from SOON — the first urgency that has one)
    for u in ("soon", "defer"):
        est = estimate_cost(u, task_tokens, current_state)
        if est["bleed_note"]:
            lines.append(f"BUT:  {est['bleed_note']}")
            break
    return "\n".join(lines)
```

---

## 6. Integration with CG-11

| CG-11 layer | How the estimator feeds it |
|---|---|
| **Layer 1** (ask-wrapper, `urgency_gate.py gate`) | The TTY prompt calls `format_all_urgencies()` and prints the result above the input line. The operator sees costs + bleed before choosing. |
| **Layer 2** (dispatch gate, NULL-gate) | **No change.** Layer 2 parks NULL urgency; it doesn't need cost estimates. The estimator is advisory, not gating. |
| **Layer 3** (tick watchdog, price-tier engine) | `price_tier()` can optionally call `estimate_cost("defer", ...)` to compute the bleed cost of holding, and log it alongside the tier evidence. Not required for CG-12 — nice-to-have for the audit trail. |

### 6.1 Wiring in the ask-wrapper

In `urgency_gate.py gate`, the `create` subcommand's TTY prompt (§3 of
CG-11 design) currently prints the skill's decision matrix. The change:

```python
# Before the input() call:
from src.urgency_cost_estimator import fetch_current_state, format_all_urgencies
state = fetch_current_state()
task_tokens = 56_000  # from predict_tokens, or conservative default
print(format_all_urgencies(task_tokens, state))
```

The `task_tokens` value comes from `token_predictor.predict_tokens()` with
the model resolved from the task context (or the fleet default if unknown).

### 6.2 What the estimator does NOT do

- It does **not** gate dispatch. Layer 2 gates; the estimator advises.
- It does **not** block on missing data. Every signal degrades gracefully.
- It does **not** require the proxy to be running. It reads the DB + JSON
  files, not the live HTTP endpoint.
- It does **not** add a new Kalman filter. It *reads* the existing one's
  output from `kalman_samples`.

---

## 7. Concrete example — current state (2026-08-22 17:11 UTC)

Data from the live system:

| Signal | Value | Source |
|---|---|---|
| `friend_token_pct` | 0% | `zai_state.json` |
| `friend_available` | true (but token_pct=0) | `zai_state.json` |
| Kalman `burn_rate_tph` | 150,145 | `kalman_samples` latest |
| Kalman `exhausts_in_hours` | None (under budget, no exhaustion projected) | `kalman_samples` |
| OpenRouter price | $0.4653/M | `price_observations` (measured) |
| PPQ glm-5.2 price | $0.2577/M | `price_observations` (measured) |
| glm-5.2 mean tokens | 56,866 | `api_calls` (29,799 rows, 7d) |
| glm-5.2 token variance | 1.11B (σ ≈ 33,356) | `api_calls` |
| 24h paid spend | $16.41 | `api_calls` |
| Bleed rate | $0.69/h | $16.41 / 24h |

With `free_quota_available = False` (both keys at 0%) and a default task of
~56K tokens (glm-5.2 mean):

```
NOW:   ~$0.03 ± $0.01  (paid failover, ~56K tokens @ $0.47/M)
SOON:  ~$0.00 ± $0.00  (quota resets in ~?h, free dispatch — Kalman has no exhaustion projection)
DEFER: ~$0.00 ± $0.00  (same, cheapest window)
BATCH: ~$0.00 ± $0.00  (free only)
```

**Note:** the current Kalman state shows `exhausts_in_hours = None` (under
budget, quota is at 0% but the filter doesn't project exhaustion because
`used_pct = 0` — the quota window may have just reset). In this state,
`quota_resets_in_hours` falls back to `None`, which means the estimator
cannot compute bleed timing. The bleed note would say:
`"waiting costs ~$? in bleed (timing unknown — Kalman unconverged)"`.

When quota IS being consumed and the Kalman tracks it (normal operation), the
output would look like:

```
NOW:   ~$0.03 ± $0.01  (paid failover, ~56K tokens @ $0.47/M)
SOON:  ~$0.00 ± $0.00  (quota resets in ~4.2h, free dispatch)
DEFER: ~$0.00 ± $0.00  (same, cheapest window)
BATCH: ~$0.00 ± $0.00  (free only)
BUT:   waiting costs ~$2.90 in bleed (4.2h × $0.69/h)
```

The operator sees: **deferring saves $0.03 in tokens but costs $2.90 in
bleed → dispatch NOW.** That is the entire point of the estimator.

---

## 8. CG-12 implementation plan (paste-ready)

**Order: module → tests → wire to ask-wrapper → commit.**
All new code is stdlib-only Python 3. Never touch other in-flight branches.

### 8.1 Module: `src/urgency_cost_estimator.py`

```python
#!/usr/bin/env python3
"""CG-12: Kalman-based cost estimator for urgency options.

Composes three existing signals into a per-urgency dollar cost:
  1. Kalman burn trajectory (kalman_samples) → when quota resets
  2. price_observations → current $/M per provider
  3. api_calls token distribution → estimated tokens per task

Pure function: estimate_cost(urgency, task_tokens, current_state) → dict.
I/O wrapper: fetch_current_state(db_path) → dict (reads zai_usage.db ro + zai_state.json).
Formatter: format_all_urgencies(task_tokens, current_state) → str.
"""
from __future__ import annotations
import json, os, sqlite3, time
from urllib.request import pathname2url

DEFAULT_DB_PATH = os.path.expanduser("~/.hermes/bot/zai_usage.db")
DEFAULT_STATE_PATH = os.path.expanduser("~/.hermes/bot/zai_state.json")
DEFAULT_PAID_PRICE_PER_M = 0.47
DEFAULT_FREE_PRICE_PER_M = 0.001
DEFAULT_TOKEN_STD_FRACTION = 0.30
DEFAULT_TASK_TOKENS = 56_000

URGENCIES = ("now", "soon", "defer", "batch")


def _ro(db_path):
    uri = "file:" + pathname2url(os.path.abspath(db_path)) + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=10)


def fetch_current_state(db_path=DEFAULT_DB_PATH, state_path=DEFAULT_STATE_PATH):
    """Read zai_usage.db (ro) + zai_state.json. All signals degrade to None."""
    s = {}
    # 1. quota state from zai_state.json
    try:
        st = json.load(open(state_path))
        pct = float(st.get("friend_token_pct", 0) or 0)
        s["free_quota_available"] = pct > 0
        reset_ms = st.get("friend_reset_ms")
        if reset_ms:
            s["quota_resets_in_hours"] = max(0, (reset_ms / 1000 - time.time()) / 3600)
    except Exception:
        s["free_quota_available"] = False
    # 2. latest price from price_observations (cheapest paid)
    try:
        c = _ro(db_path); c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT provider, rate_per_m FROM price_observations "
            "WHERE provider NOT IN ('friend','ours') "
            "ORDER BY ts DESC LIMIT 1").fetchone()
        s["paid_price_per_m"] = float(row["rate_per_m"]) if row else DEFAULT_PAID_PRICE_PER_M
        c.close()
    except Exception:
        s["paid_price_per_m"] = DEFAULT_PAID_PRICE_PER_M
    # 3. latest Kalman sample
    try:
        c = _ro(db_path); c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT burn_rate_tph, exhausts_in_hours, uncertainty "
            "FROM kalman_samples ORDER BY ts DESC LIMIT 1").fetchone()
        if row and row["exhausts_in_hours"]:
            s["quota_resets_in_hours"] = float(row["exhausts_in_hours"])
        s["kalman_uncertainty"] = float(row["uncertainty"] or 0) if row else 0
        c.close()
    except Exception:
        pass
    if "quota_resets_in_hours" not in s:
        s["quota_resets_in_hours"] = None
    # 4. bleed rate: 24h paid spend / 24
    try:
        c = _ro(db_path)
        row = c.execute(
            "SELECT SUM(cost_usd) FROM api_calls "
            "WHERE cost_usd > 0 AND ts > ?", (time.time() - 86400,)).fetchone()
        s["bleed_rate_per_hour"] = float(row[0] or 0) / 24.0
        c.close()
    except Exception:
        s["bleed_rate_per_hour"] = 0.0
    # 5. token stats: glm-5.2 mean + var (7d)
    try:
        c = _ro(db_path); c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT AVG(total_tokens) as mean, "
            "AVG(total_tokens*total_tokens) - AVG(total_tokens)*AVG(total_tokens) as var "
            "FROM api_calls WHERE model='glm-5.2' AND ts > ?",
            (time.time() - 86400 * 7,)).fetchone()
        if row and row["mean"]:
            s["task_tokens"] = int(row["mean"])
            s["token_std"] = (row["var"] or 0) ** 0.5
        c.close()
    except Exception:
        pass
    return s


def _confidence(state, band, cost):
    if cost == 0 and band == 0:
        return "high"
    if state.get("free_quota_available"):
        return "high"
    if band > max(cost * 0.5, 0.01):
        return "low"
    if band > max(cost * 0.2, 0.005):
        return "medium"
    return "high"


def estimate_cost(urgency, task_tokens, state):
    """Pure: compute direct + bleed for one urgency level."""
    free = state.get("free_quota_available", False)
    paid = state.get("paid_price_per_m", DEFAULT_PAID_PRICE_PER_M)
    bleed_rate = state.get("bleed_rate_per_hour", 0.0)
    resets_in = state.get("quota_resets_in_hours")

    # DIRECT
    if free:
        direct, reason = 0.0, "free quota available"
    elif urgency == "batch":
        direct, reason = 0.0, "free only"
    elif urgency in ("soon", "defer") and resets_in is not None and resets_in > 0:
        direct, reason = 0.0, f"quota resets in ~{resets_in:.1f}h, free dispatch"
    else:
        direct = task_tokens * paid / 1_000_000
        reason = f"paid failover, ~{task_tokens // 1000}K tokens @ ${paid:.2f}/M"

    # BLEED
    bleed, bleed_note = 0.0, None
    if urgency != "now" and bleed_rate > 0:
        if resets_in is not None and resets_in > 0:
            bleed = bleed_rate * resets_in
            bleed_note = f"waiting costs ~${bleed:.2f} in bleed ({resets_in:.1f}h × ${bleed_rate:.2f}/h)"
        else:
            bleed_note = "waiting costs ~$? in bleed (timing unknown — Kalman unconverged)"

    # Confidence
    tok_std = state.get("token_std", task_tokens * DEFAULT_TOKEN_STD_FRACTION)
    sigma_tok = (tok_std / max(task_tokens, 1)) * direct if direct > 0 else 0
    band = sigma_tok
    conf = _confidence(state, band, direct)

    # cost_usd = dispatch token cost (direct). Bleed shown separately
    # in the "BUT:" line — operator compares direct vs bleed to decide.
    return {
        "urgency": urgency,
        "cost_usd": round(direct, 4),
        "bleed_usd": round(bleed, 4),
        "confidence_interval": [round(max(0.0, direct - band), 4),
                                round(direct + band, 4)],
        "confidence": conf,
        "breakdown": {"direct": round(direct, 4), "bleed": round(bleed, 4)},
        "explanation": reason,
        "bleed_note": bleed_note,
    }


def format_all_urgencies(task_tokens, state):
    """Multi-line string for the clarify question."""
    lines = []
    for u in URGENCIES:
        e = estimate_cost(u, task_tokens, state)
        ci = e["confidence_interval"]
        w = (ci[1] - ci[0]) / 2
        lines.append(f"{u.upper():5} ~${e['cost_usd']:.2f} ± ${w:.2f}  ({e['explanation']})")
    for u in ("soon", "defer"):
        e = estimate_cost(u, task_tokens, state)
        if e["bleed_note"]:
            lines.append(f"BUT:  {e['bleed_note']}")
            break
    return "\n".join(lines)
```

### 8.2 Tests: `tests/test_urgency_cost_estimator.py`

```python
"""Tests for CG-12 urgency cost estimator — pure function, no DB."""
from src.urgency_cost_estimator import estimate_cost, format_all_urgencies

def test_now_free_quota():
    s = {"free_quota_available": True}
    e = estimate_cost("now", 56_000, s)
    assert e["cost_usd"] == 0.0
    assert "free" in e["explanation"]

def test_now_paid():
    s = {"free_quota_available": False, "paid_price_per_m": 0.47}
    e = estimate_cost("now", 56_000, s)
    assert abs(e["cost_usd"] - 0.0263) < 0.001
    assert "paid" in e["explanation"]

def test_soon_quota_resets():
    s = {"free_quota_available": False, "paid_price_per_m": 0.47,
         "bleed_rate_per_hour": 0.69, "quota_resets_in_hours": 4.2}
    e = estimate_cost("soon", 56_000, s)
    assert e["cost_usd"] == 0.0  # direct = 0 (quota resets → free)
    assert e["breakdown"]["direct"] == 0.0
    assert e["bleed_note"] is not None
    assert "4.2h" in e["bleed_note"]
    assert "0.69" in e["bleed_note"]
    # bleed = 0.69 * 4.2 = 2.898
    assert abs(e["breakdown"]["bleed"] - 2.898) < 0.01

def test_defer_with_bleed():
    s = {"free_quota_available": False, "paid_price_per_m": 0.47,
         "bleed_rate_per_hour": 0.69, "quota_resets_in_hours": 4.2}
    e = estimate_cost("defer", 56_000, s)
    assert e["cost_usd"] == 0.0  # direct = 0 (quota resets)
    assert e["breakdown"]["bleed"] > 0
    assert e["bleed_note"] is not None

def test_now_stops_bleed():
    s = {"free_quota_available": False, "paid_price_per_m": 0.47,
         "bleed_rate_per_hour": 0.69, "quota_resets_in_hours": 4.2}
    e = estimate_cost("now", 56_000, s)
    assert e["breakdown"]["bleed"] == 0.0
    assert e["bleed_note"] is None

def test_batch_always_free():
    s = {"free_quota_available": False, "paid_price_per_m": 0.47}
    e = estimate_cost("batch", 56_000, s)
    assert e["cost_usd"] == 0.0
```

### 8.3 Run

```bash
cd ~/merchant-routing-engine
python3 -m pytest tests/test_urgency_cost_estimator.py -v
python3 -c "
from src.urgency_cost_estimator import fetch_current_state, format_all_urgencies
s = fetch_current_state()
print(format_all_urgencies(s.get('task_tokens', 56_000), s))
"
```

---

## 9. Edge cases + degradation

| Condition | Behavior |
|---|---|
| DB missing/unreadable | `fetch_current_state` returns defaults: `free_quota_available=False`, `paid_price=$0.47/M`, `bleed_rate=0`, `resets_in=None`. Estimator still answers. |
| Kalman unconverged (`exhausts_in_hours=None`) | `resets_in=None` → bleed note says "timing unknown". Direct cost may show paid (conservative). |
| No token history for model | `task_tokens` defaults to `DEFAULT_TASK_TOKENS=56_000` (glm-5.2 fleet mean). Confidence = `low`. |
| Quota just reset (used_pct=0) | `free_quota_available` depends on `friend_token_pct > 0`. If 0% but window just opened, the zai_state may not have refreshed yet → estimator may show paid (conservative). The operator sees the "free" option once the state catches up. |
| Bleed rate = 0 (no paid spend in 24h) | No bleed note. All urgencies show $0 if free quota is available. |

---

## 10. Future extensions (not CG-12)

- **Per-task token prediction:** currently uses fleet mean by model. When
  `task_type` matures (CG-5), use per-(model, task_type) distribution.
- **Provider-specific pricing:** when the proxy routes to a specific
  provider per task, use that provider's measured rate instead of the
  cheapest. Requires routing context in the ask.
- **Bleed attribution:** currently bleed_rate is fleet-wide. If a specific
  task is the bleed source, attribute it. Requires task-level cost tracking.
- **Opportunity cost as a number:** if we model the value of a free token
  (e.g., "each free token saves $0.47/M vs paid"), the opportunity cost of
  wasting a free window becomes computable. Deferred — the operator's
  judgment handles this today.
