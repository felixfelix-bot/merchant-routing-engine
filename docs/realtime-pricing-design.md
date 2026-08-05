# Real-Time Price Calculation System — Design

> **One source of truth for $/M.** Replace every hardcoded rate constant with a
> value the system *measures* from real billing data, smoothed by `PriceKalman`,
> refreshed on a 5–30 min batch cycle, and served to all consumers from a single
> thread-safe module: `src/realtime_pricing.py`.
>
> **Companion doc:** `docs/REAL_PRICE_SYSTEM_DESIGN.md` contains the original
> long-form analysis (per-provider data-source walkthroughs, CVM JSON shapes,
> appendices). This document is the **actionable engineering spec**: module
> signatures, data flow, phased migration, file-by-file changes, test plan, and
> risk register. Where the two differ on the module name, **this doc wins** —
> the module is `src/realtime_pricing.py` (singular), not
> `real_price_tracker.py`.

---

## 1. Architecture

### 1.1 The problem — hardcoded rates diverge from reality

A grep of the codebase finds **7 files** holding hardcoded $/M constants that
disagree with each other *and* with measured billing data. The existing
`PriceKalman` is seeded from these constants and **never receives a real
observation** — `record_request(cost_estimate=…)` exists but no caller passes
real data. Concrete example: Ollama Cloud's real billing rate is **$0.0155/M**,
but it is hardcoded as **$0.024/M** in four places (1.55× over).

| # | File | Symbol | Lines | Value(s) | Status |
|---|------|--------|-------|----------|--------|
| 1 | `src/live_router.py` | `_DEFAULT_CONVERGED_RATES` | 84–91 | ours 0.001, friend 0.028983, ollama 0.023952, ppq 0.14, openrouter 0.135, deepinfra 1.30 | seeds Kalman, never fed real data |
| 1 | `src/live_router.py` | `_QUOTA_TOTALS` | 94–101 | ours/friend 2M, ollama 500M, rest `inf` | guesses |
| 2 | `src/shadow_hook.py` | `_SEED_COSTS` | 51–58 | ours **0.31**, friend 0.375, ollama 0.024, … | wildly wrong (ours 10000× too high) |
| 2 | `src/shadow_hook.py` | `_QUOTA_TOTALS` | 61–68 | (same as live_router) | guesses |
| 3 | `src/primary_router.py` | `_SEED_COSTS` | 51–58 | ours **0.31**, friend 0.375, ollama 0.024, … | duplicated, divergent from live_router |
| 3 | `src/primary_router.py` | `_QUOTA_TOTALS` | 60–67 | (same) | guesses |
| 4 | `src/pricing_engine.py` | `EXTRA_USAGE_BASE_RATE`, `EXTRA_USAGE_TARGET_RATE`, `EXTRA_USAGE_MULTIPLIER` | 109–116 | 0.024, 0.10, ≈4.17 | derived from the wrong 0.024 |
| 5 | `~/.hermes/bot/zai_proxy.py` | `_OLLAMA_CLOUD_BASE_RATE`, `_OLLAMA_CLOUD_EXTRA_RATE` | 1467–1468 | 0.024, 0.15 | wrong base |
| 5 | `~/.hermes/bot/zai_proxy.py` | `_MODEL_COST_PER_1M` | 1470–1498 | 14 entries incl. glm-4.5-x 5.55, glm-4.5-airx 2.80 (guesses) | lookup table for spend tracking |
| 6 | `config/providers.yaml` | `monthly_fee_usd`, `extra_usage_rate_per_m`, `cost_per_1m_*` | 10, 17, 31, 41, 52–53, 63–64, 72–73 | mixed | fees are real config; per-token prices are guesses |
| 7 | `demo/cvm-server/src/cvm-server.ts` | `flatKeyCostPerM`, `ollamaMonthlyUsd` | 53–54 | 0.02, 100.0 | dashboard shows wrong number |

The four routers (`live_router`, `shadow_hook`, `primary_router`) each seed their
*own* `PriceKalman` instances from *different* constants, so they disagree even
before any real data arrives.

### 1.2 Target architecture

```
                         ┌──────────────────────────────────────────────┐
                         │            REAL BILLING DATA SOURCES           │
                         │                                              │
                         │  Ollama /api/usage → activity.cost / tokens   │
                         │  zai_usage.db api_calls → SUM(tokens)         │
                         │  api_burn.db ppq_queries → cost_usd / tokens  │
                         │  daily_spend → actual_cost / tokens (DI/OR)   │
                         │  providers.yaml → monthly_fee_usd (amortize)  │
                         └─────────────────────┬────────────────────────┘
                                               │  every 5–30 min (batch)
                                               ▼
                         ┌──────────────────────────────────────────────┐
                         │   RealtimePricing  (src/realtime_pricing.py)   │
                         │   singleton · thread-safe (RLock)             │
                         │                                              │
                         │   refresh()  → collect · write DB · feed       │
                         │                 PriceKalman.update()          │
                         │   snapshot() → O(1) cached read, per-request   │
                         │                                              │
                         │   rate provenance flag: measured | estimated   │
                         └─────────────────────┬────────────────────────┘
                                               │  frozen RateSnapshot
              ┌────────────────┬───────────────┼───────────────┬────────────────┐
              ▼                ▼               ▼               ▼                ▼
        LiveRouter        ShadowHook     PrimaryRouter     zai_proxy        CVM dashboard
        (Kalman base_rate,  (Kalman seeds,  (Kalman seeds,  _estimate_      computePricing
         failover)           shadow)         primary)        cost_usd()      reads DB
```

### 1.3 Design principles

1. **Observations, not constants.** Every $/M the optimizer sees comes from
   `PriceKalman.base_rate`, and every `update()` receives a *measured* rate.
2. **Cold-start constants are fallbacks, labelled as such.** The current
   constants survive ONLY as the initial `x[0]` before the first observation,
   renamed `_COLD_START_RATES` with a clear comment. After the first observation
   the filter's output is independent of the seed.
3. **Batch, never per-request.** `refresh()` runs on a cron cycle. The hot
   routing path calls `snapshot()`, an O(1) cached read. No DB or HTTP call is
   ever made inside a request handler.
4. **Provenance is first-class.** Every rate carries `source` and `is_measured`
   so consumers (and the dashboard) can show "measured from billing API" vs
   "estimated by amortization" vs "cold-start fallback". No silent guesses.
5. **One singleton, many consumers.** `LiveRouter`, `ShadowHook`,
   `PrimaryRouter`, the proxy, and the CVM dashboard all read from the same
   `RealtimePricing` instance — eliminating the four-way divergence above.
6. **Thread-safe by construction.** The proxy is a `ThreadingHTTPServer`
   (confirmed in `live_router.py` docstring: "Thread-safe via a
   `threading.Lock` — called from `ThreadingHTTPServer` handler threads").
   `refresh()` mutates state under an `RLock`; `snapshot()` returns an immutable
   `RateSnapshot` that handler threads read without locking.
7. **Fail-safe.** The module NEVER raises. Any collector failure leaves the
   previous snapshot in place and flags the affected provider as
   `cold_start_fallback` for that cycle. Routing must not break on a billing API
   hiccup.

---

## 2. Module Design — `src/realtime_pricing.py`

### 2.1 Public data structures

```python
from __future__ import annotations
from dataclasses import dataclass
from typing import Final
import threading, time

# Provenance strings (enum-like; kept as plain strings for SQLite ergonomics)
SRC_OLLAMA_BILLING: Final = "ollama_billing_api"     # measured
SRC_PQQ_LEDGER:     Final = "ppq_ledger"             # measured
SRC_DEEPINFRA_ACTUAL: Final = "deepinfra_actual"     # measured
SRC_OPENROUTER_ACTUAL: Final = "openrouter_actual"   # measured
SRC_ZAI_AMORTIZED:  Final = "zai_amortized"          # estimated (config / tokens)
SRC_COLD_START:     Final = "cold_start_fallback"    # estimated (no data yet)

MEASURED_SOURCES: Final[frozenset[str]] = frozenset({
    SRC_OLLAMA_BILLING, SRC_PQQ_LEDGER,
    SRC_DEEPINFRA_ACTUAL, SRC_OPENROUTER_ACTUAL,
})


@dataclass(frozen=True, slots=True)
class RateObservation:
    """A single measured-or-estimated $/M for a (provider, model) pair."""
    provider: str
    model: str | None                 # None = provider-level aggregate
    rate_per_m: float                 # $ per 1M tokens, >= MIN_EFFECTIVE_PRICE
    source: str                       # one of the SRC_* constants
    is_measured: bool                 # True iff source in MEASURED_SOURCES
    confidence: float                 # 0.0–1.0, from sample size (see §2.5)
    sample_tokens: int                # tokens behind this observation
    sample_cost_usd: float            # cost behind this observation (0 if amortized)
    ts: float                         # observation timestamp (unix)
    velocity: float = 0.0             # Kalman velocity (Δ$/M per cycle)

    @property
    def is_stale(self) -> bool:
        """True if no observation in the last 30 min (configurable)."""
        return (time.time() - self.ts) > 1800.0


@dataclass(frozen=True, slots=True)
class RateSnapshot:
    """Immutable point-in-time view of all rates. Safe to share across threads."""
    ts: float
    by_provider_model: dict[tuple[str, str | None], RateObservation]
    by_provider: dict[str, RateObservation]      # token-weighted aggregate per provider
    any_cold_start: bool                         # True if any provider lacks a real obs
    refresh_count: int
```

### 2.2 The `RealtimePricing` class

```python
class RealtimePricing:
    """Single source of truth for measured $/M. Thread-safe singleton.

    Lifecycle:
      • get_instance()   — lazy singleton (matches LiveRouter/ShadowHook pattern)
      • refresh()        — batch collector; called by cron every 5–30 min
      • snapshot()       — O(1) cached read; called per-request from hot paths
      • get_rate(...)    — convenience single-provider lookup
      • feed_kalman(...) — push the latest observation into a caller's PriceKalman

    Guarantees:
      • NEVER raises from public methods (logs + returns last snapshot).
      • snapshot() is lock-free for readers (returns immutable RateSnapshot).
      • refresh() is serialised by an RLock; concurrent refresh() calls no-op.
      • A failed collector leaves the previous observation in place and marks
        the provider source='cold_start_fallback' for that cycle.
    """

    _instance: "RealtimePricing | None" = None
    _instance_lock = threading.Lock()

    DEFAULT_REFRESH_SECONDS = 300.0     # 5 min; tunable up to 1800 (30 min)
    STALE_THRESHOLD_SECONDS = 1800.0    # 30 min → is_stale
    MIN_SAMPLE_TOKENS = 1_000_000       # below this, amortized rate is unreliable

    @classmethod
    def get_instance(
        cls,
        *,
        zai_db_path: str = "~/.hermes/bot/zai_usage.db",
        burn_db_path: str = "~/.hermes/bot/api_burn.db",
        providers_yaml: str | None = None,
        cold_start_rates: dict[str, float] | None = None,
    ) -> "RealtimePricing":
        """Get or create the singleton. Thread-safe."""
        ...

    @classmethod
    def reset_instance(cls) -> None:
        """Drop the singleton (for tests)."""
        ...

    # ── Batch collector (cron-driven) ──────────────────────────────

    def refresh(self) -> RateSnapshot:
        """Collect fresh observations from all sources, feed the internal
        PriceKalman per (provider, model), persist to price_observations,
        and return the new snapshot.

        Idempotent under concurrent calls (RLock-guarded). Designed to be
        invoked by a Hermes cron job, NOT from a request handler.
        """
        ...

    # ── Hot-path readers (per-request) ─────────────────────────────

    def snapshot(self) -> RateSnapshot:
        """Return the latest cached snapshot. O(1), lock-free read of an
        immutable object. Never blocks, never raises."""
        ...

    def get_rate(self, provider: str, model: str | None = None) -> RateObservation:
        """Convenience lookup. Falls back to cold-start if unknown."""
        ...

    def get_provider_rates(self) -> dict[str, float]:
        """Provider-level {provider: rate_per_m} dict — the shape the existing
        routers expect for seeding PriceKalman (drop-in for _base_rates)."""
        ...

    def feed_kalman(self, kalman: "PriceKalman", provider: str) -> bool:
        """Push the latest measured observation for *provider* into the given
        PriceKalman (calls kalman.update(rate)). Returns False if no
        observation exists. Used by LiveRouter/ShadowHook/PrimaryRouter to
        drive their own Kalman instances from real data."""
        ...

    # ── Per-source collectors (private) ────────────────────────────

    def _measure_zai_amortized(self) -> dict[tuple[str, str | None], RateObservation]:
        """z.ai flat-rate: monthly_fee_usd / (SUM(total_tokens this month)/1e6).

        Query:  SELECT model, SUM(total_tokens) FROM api_calls
                WHERE key_name IN ('ours','friend') AND ts >= month_start
                GROUP BY model
        Source tag: 'zai_amortized' (is_measured=False). friend gets
        monthly_fee=0 → rate=MIN_EFFECTIVE_PRICE; the 21% ADR-005 premium is
        applied by the optimizer, not here (keep base rates honest).
        """
        ...

    def _measure_ollama_billing(self) -> dict[tuple[str, str | None], RateObservation]:
        """Ollama Cloud: parse fetch_ollama_usage() response for per-model
        activity.cost / activity.total_tokens. Source: 'ollama_billing_api'
        (is_measured=True). Fallback chain on API failure → amortize $100/mo
        over SUM(total_tokens) WHERE key_name='ollama_cloud' → source
        'zai_amortized'. Final fallback → cold_start."""
        ...

    def _measure_ppq_ledger(self) -> dict[tuple[str, str | None], RateObservation]:
        """PPQ: SELECT SUM(cost_usd)/(SUM(total_tokens)/1e6) FROM ppq_queries
        WHERE ts > now-7d GROUP BY model. Source: 'ppq_ledger'
        (is_measured=True). On empty/missing → cold_start."""
        ...

    def _measure_deepinfra_spend(self) -> dict[tuple[str, str | None], RateObservation]:
        """DeepInfra: SELECT SUM(actual_cost)/(SUM(total_tokens)/1e6) FROM
        daily_spend WHERE tier='deepinfra' AND date > now-7d. Source:
        'deepinfra_actual' (is_measured=True). This path is ALREADY correct in
        the proxy (estimated_cost extraction); we just route it through here."""
        ...

    def _measure_openrouter_spend(self) -> dict[tuple[str, str | None], RateObservation]:
        """OpenRouter: SUM(cost_usd)/(SUM(total_tokens)/1e6) FROM daily_spend
        WHERE tier='openrouter'. Source: 'openrouter_actual' (is_measured=True)
        IF the proxy logs per-request cost; otherwise falls back to published
        prices (source tag 'published_list', is_measured=False) until the proxy
        extraction is added (see Migration Phase 3.6)."""
        ...

    # ── Persistence ────────────────────────────────────────────────

    def _write_observations(self, obs: dict[tuple[str, str | None], RateObservation]) -> None:
        """INSERT each observation into price_observations (WAL mode, separate
        connection, INSERT-only — no locks against the hot-path reader)."""
        ...
```

### 2.3 SQLite schema — `price_observations`

Created idempotently in `zai_usage.db` (same DB the proxy already uses) by
`RealtimePricing.__init__`:

```sql
CREATE TABLE IF NOT EXISTS price_observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL,
    provider        TEXT    NOT NULL,
    model           TEXT,                       -- NULL = provider-level aggregate
    rate_per_m      REAL    NOT NULL,
    source          TEXT    NOT NULL,           -- 'ollama_billing_api', 'zai_amortized', ...
    is_measured     INTEGER NOT NULL,           -- 1 = real billing; 0 = estimated/fallback
    confidence      REAL    DEFAULT 1.0,
    sample_tokens   INTEGER,
    sample_cost_usd REAL,
    velocity        REAL    DEFAULT 0.0,
    note            TEXT
);
CREATE INDEX IF NOT EXISTS idx_price_obs_provider_ts
    ON price_observations(provider, model, ts);
```

> **Schema gap to flag:** the live `api_calls` table
> (`hermes-orchestration/scripts/engine/zai_proxy.py` lines 308–324) has **no
> `cost_usd` column** — only token counts. That is fine for z.ai (we amortize
> the flat fee over tokens) and Ollama (we use the billing API), but per-request
> cost for DeepInfra/OpenRouter/PPQ must come from `daily_spend.actual_cost`
> (DeepInfra already writes this) or `api_burn.db.ppq_queries.cost_usd`. **No
> schema migration of `api_calls` is required for Phase 1–3.**

### 2.4 Internal Kalman grid

The module keeps its OWN `PriceKalman` per `(provider, model)` pair, separate
from the routers' Kalman instances (which track velocity for routing
multipliers). The module's Kalman grid is the canonical smoothed estimate;
consumers either read `base_rate` directly OR call `feed_kalman()` to push the
latest observation into their own filter.

```python
self._kalmans: dict[tuple[str, str | None], PriceKalman] = {}
# Initialised lazily on first observation for that key.
# measurement_noise per source (see REAL_PRICE_SYSTEM_DESIGN.md Appendix B):
_NOISE = {
    SRC_ZAI_AMORTIZED:     1e-6,   # slow drift (tokens accumulate)
    SRC_OLLAMA_BILLING:    1e-4,   # exact but may include promo periods
    SRC_PQQ_LEDGER:        1e-4,
    SRC_DEEPINFRA_ACTUAL:  1e-3,   # prompt caching → high variance
    SRC_OPENROUTER_ACTUAL: 1e-3,
}
```

### 2.5 Confidence scoring

```python
def _confidence(sample_tokens: int, is_measured: bool) -> float:
    """0.0–1.0. measured sources ramp from 0.3 at 1k tokens to 1.0 at 10M;
    estimated (amortized) sources cap at 0.7 (they depend on the fee being
    correct)."""
    if not is_measured:
        return min(0.7, 0.3 + (sample_tokens / 10_000_000) * 0.4)
    return min(1.0, 0.3 + (sample_tokens / 10_000_000) * 0.7)
```

Consumers can use `confidence < 0.5` to keep a provider on cold-start rather
than trust a thin sample.

---

## 3. Data Flow

### 3.1 Refresh cycle (batch, every 5–30 min)

```
┌─ cron / background thread ─────────────────────────────────────────────┐
│  RealtimePricing.get_instance().refresh()                              │
│                                                                       │
│  1. acquire RLock (no-op if another refresh is running)               │
│  2. for each collector in [zai, ollama, ppq, deepinfra, openrouter]:  │
│        obs = collector()          # try/except per collector          │
│        on failure: obs = {…: cold_start_fallback}                    │
│  3. merge into dict[(provider, model), RateObservation]               │
│  4. for each (pm, ob) in merged:                                      │
│        kalman = self._kalmans.setdefault(pm, PriceKalman(...))        │
│        kalman.update(ob.rate_per_m)                                   │
│        ob_with_velocity = replace(ob, velocity=kalman.velocity)       │
│  5. _write_observations(merged)  # INSERT into price_observations    │
│  6. build frozen RateSnapshot (by_provider_model + token-weighted     │
│     by_provider aggregate + any_cold_start flag)                      │
│  7. self._snapshot = snapshot  # atomic reference swap                │
│  8. release RLock; return snapshot                                    │
└───────────────────────────────────────────────────────────────────────┘
```

### 3.2 Per-request hot path (lock-free)

```
HTTP handler thread (ThreadingHTTPServer)
  │
  ├─ LiveRouter.select_failover(...)
  │     └─ rates = RealtimePricing.get_instance().get_provider_rates()
  │        # reads self._snapshot.by_provider — immutable, no lock
  │     └─ for each provider: feed_kalman(self._price_kalmans[p], p)
  │        # pushes the latest observation into the router's own Kalman
  │     └─ optimizer uses self._price_kalmans[p].base_rate (now data-driven)
  │
  └─ zai_proxy._estimate_cost_usd(key, tokens)
        └─ RealtimePricing.get_instance().get_rate(provider).rate_per_m
           # replaces _MODEL_COST_PER_1M lookup
```

### 3.3 Per-provider rate derivation

| Provider | Primary formula | Source tag | `is_measured` |
|----------|-----------------|------------|---------------|
| `ours` | `155 / (SUM(total_tokens ours this month)/1e6)` | `zai_amortized` | False |
| `friend` | `0 / (tokens/1e6)` → floored at `MIN_EFFECTIVE_PRICE`; 21% premium applied by optimizer | `zai_amortized` | False |
| `ollama_cloud` | `SUM(activity.cost) / (SUM(activity.total_tokens)/1e6)` from `/api/usage` | `ollama_billing_api` | True |
| `ollama_cloud` (API fail) | `100 / (SUM(total_tokens ollama this month)/1e6)` | `zai_amortized` | False |
| `ppq` | `SUM(cost_usd) / (SUM(total_tokens)/1e6)` from `ppq_queries` (7d) | `ppq_ledger` | True |
| `deepinfra` | `SUM(actual_cost) / (SUM(total_tokens)/1e6)` from `daily_spend` (7d) | `deepinfra_actual` | True |
| `openrouter` | `SUM(actual_cost) / (SUM(total_tokens)/1e6)` from `daily_spend` (7d) | `openrouter_actual` | True / False* |

\* `openrouter` is `is_measured=True` only once the proxy logs per-request cost
(Phase 3.6). Until then it falls back to published list prices with
`source='published_list'`, `is_measured=False`.

### 3.4 z.ai as sunk cost

The z.ai subscription is a flat monthly fee — the **marginal** cost of an extra
token is **$0**. We report the **amortized** rate (fee / tokens) so the optimizer
can compare providers on equal footing, but `is_measured=False` and
`confidence ≤ 0.7` flag that this is an accounting allocation, not a billing
measurement. The 21% friend premium (ADR-005) is applied in
`compute_effective_price`, NOT baked into the base rate.

---

## 4. Migration Plan (phased, safe)

Every phase is independently revertible. A single env-var kill switch
(`REALTIME_PRICING_ENABLED`, default `false`) gates Phases 2–4 so production can
flip back to the cold-start constants instantly.

### Phase 0 — Foundation (zero behavior change)
**Goal:** build the collector, prove it produces correct numbers, change nothing
in the routing path.

| Step | Action | Verify | Revert |
|------|--------|--------|--------|
| 0.1 | Add `price_observations` table (idempotent CREATE) to `zai_usage.db` | `\d price_observations` | DROP TABLE |
| 0.2 | Implement `src/realtime_pricing.py` skeleton + all 5 collectors + `refresh()` + `snapshot()` | unit tests (§6) | delete file |
| 0.3 | Add Hermes cron job `collect_real_prices` (5 min) calling `refresh()` | `price_observations` rows appear every 5 min | disable cron |
| 0.4 | Shadow-log: alongside every routing decision, write the tracker's rate to a new `routing_decision_rates` debug column | both old + new rate logged | stop logging |

**Exit gate:** tracker yields a fresh observation for all 6 providers every
cycle, Ollama measures ≤ $0.018/M (within 20% of the known $0.0155/M), no
provider is stale > 30 min. Hold 48 h.

### Phase 1 — Dual-write Kalman (no routing change)
**Goal:** feed real observations into the routers' Kalman filters while keeping
the existing seeds as the safety net.

| Step | Action | Verify | Revert |
|------|--------|--------|--------|
| 1.1 | Rename `_DEFAULT_CONVERGED_RATES` → `_COLD_START_RATES` in `live_router.py` (add comment: "fallback only") | tests pass | rename back |
| 1.2 | Same rename in `shadow_hook.py` `_SEED_COSTS` → `_COLD_START_RATES` | tests pass | rename back |
| 1.3 | Same in `primary_router.py` | tests pass | rename back |
| 1.4 | In each router's `__init__`, after seeding Kalman from cold-start, call `RealtimePricing.get_instance().feed_kalman(pk, name)` for each provider | Kalman `base_rate` drifts toward real rate over 5–10 cycles | remove the feed call |

**Exit gate:** after 48 h, every router's `price_kalman.base_rate` is within
10% of the tracker's `get_rate(provider).rate_per_m`. No routing decision
changed by more than the expected amount (Ollama gets cheaper → used more).

### Phase 2 — Switch the optimizer's rate source (feature-flagged)
**Goal:** the optimizer reads Kalman `base_rate` (now data-driven) instead of
the static `_base_rates` dict. Gated by `REALTIME_PRICING_ENABLED`.

| Step | Action | Verify | Revert |
|------|--------|--------|--------|
| 2.1 | In `live_router._do_select_failover`, replace `self._base_rates[name]` reads with `self._price_kalmans[name].base_rate` | routing uses smoothed real rates | flip flag → reads `_base_rates` again |
| 2.2 | Apply same to `shadow_hook._do_compare` and `primary_router._do_route` | shadow/primary consistent | flip flag |
| 2.3 | Wire the kill switch: `if not REALTIME_PRICING_ENABLED: use _base_rates` | flag=false reproduces old behavior exactly | env var |
| 2.4 | Enable on one canary instance for 24 h | cost/SLO dashboards unchanged-or-better | flip flag |

**Exit gate:** 24 h canary with no regression in p99 latency, no provider
selected with `rate=0` or `rate=inf`, total cost ≤ previous 7-day average.

### Phase 3 — Eliminate proxy + pricing-engine constants (feature-flagged)
**Goal:** the proxy and `pricing_engine.py` read live rates too.

| Step | Action | Verify | Revert |
|------|--------|--------|--------|
| 3.1 | `pricing_engine.EXTRA_USAGE_BASE_RATE` → derive from `RealtimePricing.get_rate('ollama_cloud').rate_per_m` (fallback to 0.024 if tracker cold) | extra-usage multiplier recomputed dynamically | revert constant |
| 3.2 | `zai_proxy._get_ollama_cloud_cost_per_1m()` → read base from tracker; `_OLLAMA_CLOUD_EXTRA_RATE` from `providers.yaml.extra_usage_rate_per_m` | spend tracking uses real base | revert function |
| 3.3 | `zai_proxy._estimate_cost_usd()` → `RealtimePricing.get_rate(provider).rate_per_m * tokens/1e6`; keep `_MODEL_COST_PER_1M` as the cold-start fallback only | daily_spend stops being circular for ollama | revert function |
| 3.4 | Remove `cost_per_1m_input/output` from `providers.yaml` externals (they are observations, not config) | yaml loads; tracker owns these | restore lines |
| 3.5 | CVM `cvm-server.ts`: replace `CFG.flatKeyCostPerM` with a `price_observations` query; keep `ollamaMonthlyUsd` (real config) | dashboard shows measured rates | revert constant |
| 3.6 | Add OpenRouter per-request cost extraction in the proxy (mirror DeepInfra's `estimated_cost` path) so `openrouter` flips to `is_measured=True` | `daily_spend` rows for openrouter have `actual_cost` | revert extraction |

**Exit gate:** grep for `_MODEL_COST_PER_1M`, `_OLLAMA_CLOUD_BASE_RATE`,
`EXTRA_USAGE_BASE_RATE`, `_SEED_COSTS`, `_DEFAULT_CONVERGED_RATES`,
`_QUOTA_TOTALS`, `flatKeyCostPerM` — each is either deleted, renamed to
`_COLD_START_*`, or clearly commented as fallback-only. Hold 7 days.

### Phase 4 — Change detection + alerting
**Goal:** detect when a provider's real rate shifts (promo ends, price hike).

| Step | Action |
|------|--------|
| 4.1 | Add `PriceChangeDetector` (CUSUM, 15% sustained deviation threshold) |
| 4.2 | Log changes to existing `anomaly_events` table with `category='price_change'` |
| 4.3 | Surface in CVM snapshot `alerts` block |

### Phase 5 — Cleanup (after 30 days stable)
Delete the `_COLD_START_RATES` / `_MODEL_COST_PER_1M` fallbacks entirely once
the tracker has run without a cold-start event for 30 consecutive days.

---

## 5. File Changes

### New files

| File | Purpose | Approx. LOC |
|------|---------|-------------|
| `src/realtime_pricing.py` | Core module: `RealtimePricing`, `RateObservation`, `RateSnapshot`, 5 collectors, table init | ~400 |
| `src/price_change_detector.py` | CUSUM detector (Phase 4) | ~100 |
| `tests/test_realtime_pricing.py` | Unit + integration tests (§6) | ~350 |
| `tests/test_price_change_detector.py` | Detector tests | ~80 |

### Modified files (with exact locations)

| File | Location | Change |
|------|----------|--------|
| `src/live_router.py` | lines 84–91 | Rename `_DEFAULT_CONVERGED_RATES` → `_COLD_START_RATES`; add "fallback only" comment |
| `src/live_router.py` | `__init__` (147–188) | After seeding Kalman, call `RealtimePricing.get_instance().feed_kalman(pk, name)` per provider |
| `src/live_router.py` | `_do_select_failover` (357+) | Under flag, read `self._price_kalmans[name].base_rate` instead of `self._base_rates[name]` |
| `src/live_router.py` | lines 94–101 | Mark `_QUOTA_TOTALS` as cold-start; source real totals from `ollama_quota_tracker` / z.ai quota API where available |
| `src/shadow_hook.py` | lines 51–58 | Rename `_SEED_COSTS` → `_COLD_START_RATES` |
| `src/shadow_hook.py` | `__init__` (93–135) | Feed Kalman from `RealtimePricing` |
| `src/shadow_hook.py` | `_do_compare` (179+) | Use `RealtimePricing` rates for shadow cost logging |
| `src/primary_router.py` | lines 51–58 | Rename `_SEED_COSTS` → `_COLD_START_RATES` |
| `src/primary_router.py` | `__init__` (91–112), `_load_converged_rates` (114–128) | Replace historical loader with `RealtimePricing` feed |
| `src/pricing_engine.py` | lines 109–116 | `EXTRA_USAGE_BASE_RATE` derived from `RealtimePricing.get_rate('ollama_cloud')`; keep 0.024 as in-fn fallback |
| `~/.hermes/bot/zai_proxy.py` | lines 1467–1498 | `_OLLAMA_CLOUD_BASE_RATE` / `_MODEL_COST_PER_1M` become cold-start fallbacks; `_estimate_cost_usd` and `_get_ollama_cloud_cost_per_1m` read from `RealtimePricing` |
| `config/providers.yaml` | lines 52–53, 63–64, 72–73 | Remove `cost_per_1m_input/output` (observations, not config). Keep `monthly_fee_usd`, `extra_usage_rate_per_m`. |
| `demo/cvm-server/src/cvm-server.ts` | lines 53, 282–323 | `flatKeyCostPerM` → query `price_observations`; `computePricing` reads from the unified source |
| `~/.hermes/cron/collect_real_prices.*` (new) | — | Hermes cron entry: every 5 min, `RealtimePricing.get_instance().refresh()` |

> **Production-proxy note:** `~/.hermes/bot/zai_proxy.py` lives outside this
> repo (it is the production source of truth per `AGENTS.md`). Changes there
> follow the existing revert-plan discipline (`docs/migration-plan.md`) and are
> gated by `REALTIME_PRICING_ENABLED`. The merchant-routing-engine changes
#  (routers, pricing_engine) ship first; the proxy switches last.

---

## 6. Test Plan

### 6.1 Unit tests — `tests/test_realtime_pricing.py`

| Test | What it verifies |
|------|------------------|
| `test_zai_amortized_rate` | ours: `155 / (tokens/1e6)` matches expected; friend floored at `MIN_EFFECTIVE_PRICE` |
| `test_ollama_billing_rate` | parsed `activity.cost / tokens` ≈ $0.0155/M; `is_measured=True`; fallback to amortized on API failure |
| `test_ppq_ledger_rate` | `SUM(cost_usd)/(SUM(tokens)/1e6)` from a fixture `api_burn.db`; `is_measured=True` |
| `test_deepinfra_spend_rate` | `SUM(actual_cost)/(SUM(tokens)/1e6)` from fixture `daily_spend`; matches ~$1.30/M |
| `test_openrouter_fallback_to_published` | when no `actual_cost` logged, source=`published_list`, `is_measured=False` |
| `test_cold_start_when_no_data` | empty DBs → every provider returns `source=cold_start_fallback`, `is_measured=False`, rate = cold-start constant |
| `test_confidence_ramps_with_sample` | 1k tokens → conf 0.3; 10M tokens → conf 1.0 (measured) / 0.7 (amortized) |
| `test_is_stale_flag` | observation older than 30 min → `is_stale=True` |
| `test_min_effective_price_floor` | any computed rate < 0.001 is floored to `MIN_EFFECTIVE_PRICE` (ADR-004) |
| `test_nan_guard` | zero tokens / zero cost does not produce NaN; falls back to cold-start |

### 6.2 Kalman integration tests

```python
def test_kalman_converges_to_measured_rate():
    """Seed Kalman at cold-start 0.024; feed 10 real observations at 0.0155;
    base_rate must converge within 10%."""
    rp = RealtimePricing.get_instance(zai_db_path=FIXTURE, burn_db_path=FIXTURE)
    for _ in range(10):
        rp.refresh()  # each cycle re-measures ~0.0155
    obs = rp.get_rate("ollama_cloud")
    assert obs.source == "ollama_billing_api"
    assert abs(obs.rate_per_m - 0.0155) / 0.0155 < 0.10

def test_feed_kalman_drives_external_filter():
    """feed_kalman() pushes the latest obs into a caller's PriceKalman."""
    rp = RealtimePricing.get_instance(...)
    pk = PriceKalman(initial_rate=0.024)
    assert rp.feed_kalman(pk, "ollama_cloud")
    assert pk.base_rate < 0.024  # moved toward real rate
```

### 6.3 Thread-safety tests (critical — proxy is ThreadingHTTPServer)

```python
def test_concurrent_snapshot_during_refresh():
    """100 threads call snapshot() while one thread calls refresh() in a loop.
    No exception, no torn read, every snapshot is internally consistent."""
    rp = RealtimePricing.get_instance(...)
    errors = []
    def reader():
        for _ in range(1000):
            s = rp.snapshot()
            assert s.ts > 0
            # all entries share the same ts (atomic swap)
            for ob in s.by_provider.values():
                assert ob.ts <= s.ts + 1.0
    def writer():
        for _ in range(50):
            rp.refresh()
    threads = [threading.Thread(target=reader) for _ in range(100)]
    threads.append(threading.Thread(target=writer))
    for t in threads: t.start()
    for t in threads: t.join(timeout=30)
    assert not errors

def test_refresh_is_serialised():
    """Two concurrent refresh() calls do not double-write observations."""
    ...
```

### 6.4 Integration tests (full refresh cycle)

```python
def test_refresh_produces_all_six_providers():
    rp = RealtimePricing.get_instance(zai_db_path=FIXTURE_FULL, burn_db_path=FIXTURE_FULL)
    snap = rp.refresh()
    for p in ["ours", "friend", "ollama_cloud", "ppq", "openrouter", "deepinfra"]:
        assert p in snap.by_provider, f"missing {p}"
        assert snap.by_provider[p].source != "cold_start_fallback"

def test_price_observations_table_populated():
    rp = RealtimePricing.get_instance(...)
    rp.refresh()
    rows = sqlite3.connect(FIXTURE).execute(
        "SELECT provider, source, is_measured FROM price_observations "
        "WHERE ts > ?", (time.time()-60,)).fetchall()
    assert any(r[1] == "ollama_billing_api" and r[2] == 1 for r in rows)
```

### 6.5 End-to-end routing tests

| Test | What it verifies |
|------|------------------|
| `test_live_router_uses_measured_rate` | With flag on, `select_failover` picks ollama_cloud more often once its real rate drops below ppq |
| `test_kill_switch_reproduces_old_behavior` | `REALTIME_PRICING_ENABLED=false` → routing decisions identical to pre-change baseline |
| `test_proxy_estimate_cost_uses_tracker` | `_estimate_cost_usd('ollama_cloud', 1_000_000)` returns ~0.0155 (not 0.024) after refresh |

### 6.6 Validation against known-real numbers

| Provider | Source of truth | Expected | Test |
|----------|-----------------|----------|------|
| ollama_cloud | Ollama `/api/usage` billing | $0.0155/M | `assert 0.012 < rate < 0.020` |
| ours | $155 / (4.97B tokens/1e6) | $0.0000311/M | `assert rate < 0.001` |
| friend | $0 / tokens → floor | $0.001 (MIN) | `assert rate == MIN_EFFECTIVE_PRICE` |
| deepinfra | daily_spend actual_cost | ~$1.30/M | `assert 1.0 < rate < 1.6` |

### 6.7 Performance budget

| Operation | Budget | Test |
|-----------|--------|------|
| `snapshot()` | < 1 µs (dict lookup) | microbenchmark |
| `get_rate()` | < 5 µs | microbenchmark |
| `refresh()` (all 5 collectors + DB write) | < 500 ms | timing test |
| Routing hot-path overhead added | < 100 µs per decision | p99 before/after |

---

## 7. Risk Assessment

| # | Risk | Likelihood | Impact | Mitigation |
|---|------|-----------|--------|------------|
| R1 | **Ollama `/api/usage` shape changes** → tracker returns stale rate | Med | Med | Per-collector try/except → fall back to amortization; 30-min staleness alert; cache means we retry next cycle |
| R2 | **`api_burn.db` empty** (no recent PPQ queries) → no ppq rate | Med | Low | Fall back to cold-start seed; log warning; surface `is_measured=False` so the dashboard shows it |
| R3 | **Month-start: z.ai tokens near zero** → amortized rate explodes (e.g. $155/1M tokens) | High at month boundary | High (mis-routes away from cheapest provider) | Require `MIN_SAMPLE_TOKENS` (1M) before publishing an amortized rate; otherwise reuse last good rate + `is_measured=False` |
| R4 | **DeepInfra prompt caching skews per-request rate** wildly | High | Low | Token-weighted aggregation over 7 days; `measurement_noise=1e-3`; Kalman smooths |
| R5 | **Tracker DB write blocks routing** | Low | High | Separate connection, WAL mode, INSERT-only (no locks against reader); writes happen in the batch thread, never in a handler |
| R6 | **Cold-start persists > 10 min** (collector silently broken) | Med | Med | `any_cold_start` flag in snapshot; alert if set; per-provider `is_stale` |
| R7 | **OpenRouter doesn't return per-request cost** | High (until Phase 3.6) | Low | Fall back to published list prices with `is_measured=False`; clearly flagged; routing still works |
| R8 | **Rate changes mid-session** (promo ends) → routing uses stale rate | Med | Med | Kalman velocity term tracks trend; 5-min cycle catches changes in 1–2 cycles; Phase 4 CUSUM detector alerts |
| R9 | **Multiple models per provider have different prices** → provider-level aggregate loses precision | Med | Low | Track per `(provider, model)` internally; aggregate to provider-level only when the optimizer (which keys on provider) queries it; model-level rates available via `get_rate(provider, model)` |
| R10 | **Subscription cancelled** (ours → $0/mo) → amortized rate drops to 0 | Low | Low | Correct behavior ($0/mo = $0/M); `MIN_EFFECTIVE_PRICE` floor keeps it selectable |
| R11 | **Thread-safety bug** corrupts snapshot mid-read | Low | Critical | `RateSnapshot` is frozen + slots; readers never lock; writers do an atomic reference swap under RLock; dedicated `test_concurrent_snapshot_during_refresh` (§6.3) |
| R12 | **Migration regression** breaks production routing | Med | Critical | Kill switch `REALTIME_PRICING_ENABLED` (default false); canary instance first; dual-write in Phase 1 means the old path still works; 48 h shadow gate before each phase |
| R13 | **Divergent Kalman instances** (router's vs module's) drift apart | Med | Low | `feed_kalman()` pushes the same observation into both; they converge to the same base_rate; monitor delta |
| R14 | **`api_calls` lacks `cost_usd`** → can't measure per-request cost for z.ai/ollama from that table | Known | None for z.ai/ollama | Use amortization (z.ai) and billing API (ollama); per-request cost comes from `daily_spend.actual_cost` (DeepInfra/OpenRouter) and `ppq_queries.cost_usd` (PPQ). No `api_calls` migration needed. |

### Guardrails summary

- **Kill switch** (`REALTIME_PRICING_ENABLED=false`) instantly restores every
  pre-change constant path. Shipped with every phase.
- **Cold-start monitoring**: `RateSnapshot.any_cold_start` and per-provider
  `is_stale` surface in the CVM snapshot; alert if either is true for > 15 min.
- **ADR invariants preserved**: every published rate is ≥
  `MIN_EFFECTIVE_PRICE` (0.001 $/M); NaN inputs fall back to cold-start, never
  propagate to the optimizer (ADR-004 invariant #1).
- **No per-request I/O**: the hot path reads an immutable in-memory snapshot.
  All DB and HTTP work happens in the batch thread.
- **Reversibility**: each phase has a one-line revert (env var flip, rename
  back, or DROP TABLE). No destructive schema change until Phase 5 cleanup.

---

## Appendix — Mapping to the existing design doc

`docs/REAL_PRICE_SYSTEM_DESIGN.md` remains the reference for:
- Detailed per-provider data-source walkthroughs (§2 of that doc)
- CVM dashboard JSON shapes (§9)
- Measurement-noise tuning table (Appendix B)
- The "what we already have" inventory (Appendix A)

This document supersedes it on:
- **Module name**: `src/realtime_pricing.py` (not `real_price_tracker.py`)
- **Migration phasing**: the explicit kill-switch + 5-phase plan here
- **Thread-safety contract**: the lock-free-snapshot / RLock-refresh model here
- **Risk register**: R1–R14 here supersede the §10 table

---

## 8. Quota-Aware Price Modeling

> **Felix's direction (2025-08-05):** "Make sure that the price goes up as the
> quota gets used up, and then redirect to z.ai based on price, not based on
> quota." Price-based routing replaces quota-threshold gating entirely.

### 8.1 Felix's question: "don't we already have a common filter?"

**Yes.** `RealtimePricing` (§2) IS the common filter. Every provider's $/M flows
through the same pipeline: collector → `PriceKalman` → deterministic multipliers
→ effective price → optimizer. The quota-awareness described here does NOT
create a parallel mechanism. It enriches the data that the **existing** pipeline
already consumes — specifically, it replaces the step-function
`extra_usage_multiplier` (§pricing_engine.py L314–351) and the RP-5 throttle
(`live_router.py` L66–99) with a single continuous price function that lives in
the same multiplier layer.

### 8.2 The price function — burn-rate-weighted expected marginal cost

**Recommendation: Option C (probability-weighted), grounded in Option D
(marginal cost).** Options A (step) and B (linear ramp on current usage) are
rejected: A is reactive-only (the current approach Felix is correcting), and B
ignores burn velocity — at 50% usage it raises price equally whether the window
resets in 4.5h or 30 seconds.

The effective $/M for an Ollama model is:

```
effective_rate = (1 - p_extra) × r_included + p_extra × r_extra
```

Where:
- `r_included` — measured base rate from the billing collector (§3.3), ~$0.0155/M
- `r_extra` — measured rate once in extra mode, ~$0.46/M for glm-5.2 (30× included)
- `p_extra` — probability that the next request falls in the extra-usage regime

`p_extra` is derived from the **predicted end-of-window usage**, not just current
usage:

```
predicted_end = usage_frac + (burn_rate × time_remaining_h) / quota_total
p_extra = clamp((predicted_end - ONSET) / (1.0 - ONSET), 0, 1)
```

- `ONSET` = 0.7 (configurable; below this predicted usage, `p_extra` = 0)
- `burn_rate` from `ConsumptionKalman` (already tracked per provider)
- `time_remaining_h` from the session/weekly window reset timestamps
- `quota_total` from `providers.yaml` (500M session / 3.5B weekly)

**Behavior:**

| Scenario | `predicted_end` | `p_extra` | Effective glm-5.2 rate |
|----------|-----------------|-----------|----------------------|
| 10% usage, 4h left, low burn | 0.25 | 0.0 | $0.0155/M (included) |
| 80% usage, 2h left, low burn | 0.90 | 0.67 | $0.31/M |
| 60% usage, 0.5h left, high burn | 0.95 | 0.83 | $0.38/M |
| 100% usage (in extra mode) | ≥1.0 | 1.0 | $0.46/M (measured = extra) |
| Window just reset | 0.05 | 0.0 | $0.0155/M |

The crossover where Ollama-glm-5.2 exceeds PPQ ($0.14/M) happens at
`p_extra ≈ 0.28`, i.e. `predicted_end ≈ 0.78`. The optimizer picks PPQ
automatically — no threshold, no gating.

### 8.3 Integration — new deterministic multiplier, NOT a new collector

Per ADR-003, quota-awareness is a **deterministic multiplier** on top of the
Kalman base rate, exactly like `peak_multiplier`, `scarcity_factor`, and
`pace_factor`. It is NOT a modification to the collector and NOT a new Kalman
input.

| Component | Change |
|-----------|--------|
| `_measure_ollama_billing` (collector) | **Unchanged.** Continues measuring the real backward-looking rate from `activity.cost / tokens`. When in extra mode, this naturally returns ~$0.46/M. |
| `PriceKalman` | **Unchanged.** Smooths the measured rate. Stays honest. |
| **NEW:** `quota_price_factor()` in `pricing_engine.py` | Computes `p_extra` and returns the blended rate. Applied in `compute_effective_price()`. |
| `extra_usage_multiplier()` (pricing_engine.py L314) | **Replaced** by `quota_price_factor()`. The regime-string interface ("included"/"extra"/"exhausted") is deleted. |
| RP-5 throttle (live_router.py L66–99) | **Replaced.** `_THROTTLE_THRESHOLD`, `_BLOCK_THRESHOLD`, `_THROTTLE_PRICE_MULT` deleted. The price function handles everything. |
| `pace_factor()` for Ollama | `pace_factor` returns 1.0 for Ollama — `quota_price_factor` subsumes it (both predict window exhaustion; only Ollama has the price transition). |

Signature:

```python
def quota_price_factor(
    usage_frac: float,        # 0–1, from ollama.com/api/usage
    burn_rate: float,         # tokens/hour, from ConsumptionKalman
    time_remaining_h: float,  # hours until window reset
    quota_total: float,       # tokens (session or weekly)
    r_included: float,        # $/M, from PriceKalman.base_rate
    r_extra: float,           # $/M, from providers.yaml extra_usage_rate_per_m
    onset: float = 0.7,       # predicted_end below this → p_extra = 0
) -> float:
    """Returns the quota-aware effective $/M. Replaces extra_usage_multiplier."""
```

When `usage_frac >= 1.0` (already in extra mode), the collector's measured rate
IS `r_extra`, the Kalman tracks it, and `quota_price_factor` returns `r_extra`
directly — no double-counting. The multiplier's sole job is the **proactive**
adjustment in the transition zone (predicted_end between `onset` and 1.0).

### 8.4 kimi-k3 (always extra) vs glm-5.2 (sometimes included)

| Model | Included quota? | Base rate measured | `quota_price_factor` behavior |
|-------|----------------|-------------------|------------------------------|
| **glm-5.2** | Yes (5h session + weekly) | ~$0.0155/M included, ~$0.46/M extra | Full proactive ramp. Price rises as quota depletes. |
| **kimi-k3** | **No** — always pay-per-token | Always ~r_extra (measured directly from billing) | `p_extra` is always 1.0 by definition. `quota_price_factor` returns `r_extra`. No ramp — the price is honestly high from the start. |

kimi-k3 is naturally deprioritized by the optimizer because its effective rate
(~$0.46/M) always exceeds PPQ ($0.14/M) and OpenRouter ($0.135/M). It only gets
routed when it's the only model that can serve the request (Ollama-exclusive
short-circuit in `live_router.py` L448–463 stays in place — that's model
availability, not price gating).

The per-`(provider, model)` Kalman grid (§2.4) handles this naturally: each
model has its own base rate. The collector must parse `activity` per-model from
the billing API (the `/api/usage` response already breaks down cost by model).

### 8.5 Session boundary reset — price drops instantly

At the 5h session reset, `usage_frac` drops to ~0. Three things happen:

1. **`quota_price_factor` returns `r_included` immediately** — no Kalman lag, no
   smoothing. The deterministic multiplier is instant by design (ADR-003).
2. **The Kalman base rate stays at its last converged value** (~$0.0155/M if we
   were in included mode, or transitioning down from $0.46/M if we were in extra
   mode — the Kalman smooths this over a few cycles, which is correct: we DID
   pay those rates).
3. **The optimizer picks Ollama again** if its effective price is now the lowest
   among externals. No "re-enable Ollama" flag — the price drop handles it.

For the weekly window (7 days), the same logic applies on a longer cycle. The
session and weekly windows are evaluated independently; the optimizer sees the
higher of the two effective rates (worst-case governs, matching
`pace_factor_multi` semantics).

### 8.6 The optimizer routes away naturally — no special gating

When `quota_price_factor` raises glm-5.2's effective rate above $0.14/M (PPQ)
or $0.135/M (OpenRouter), the `RoutingOptimizer` simply picks the cheaper
provider. No `if usage > threshold: exclude()` logic. No regime strings. No
breaker trip. The same `compute_effective_price → sort → pick cheapest` path
that routes to z.ai at $0.001/M also routes away from Ollama at $0.31/M.

The entire transition is invisible to the optimizer — it just sees a number go
up and picks the next-cheapest. This is the core of Felix's correction: **one
price comparison, no special cases.**

### 8.7 Why this beats quota-threshold blocking

| Quota-threshold blocking (current) | Quota-aware pricing (this design) |
|-------------------------------------|----------------------------------|
| Cliff behavior: fine at 84%, blocked at 85% | Smooth ramp: price rises continuously |
| Arbitrary thresholds (0.85, 1.0) need tuning | Single `onset` parameter (0.7), or none |
| Leaves quota unused if threshold is conservative | Uses every token the optimizer deems worth the price |
| Doesn't adapt to burn rate — blocks at 85% whether burn is fast or slow | Burn-rate-aware: slow burn → stay cheap longer |
| Requires separate code paths for throttle/block/extra regimes | One function, one code path |
| Binary: Ollama is either full-price or excluded | Graduated: Ollama competes on price at every usage level |
| Breaks the "one source of truth" principle — quota logic is separate from price logic | Price IS the quota logic. One pipeline. |

### 8.8 Migration note

This change is additive to the §4 phased plan. It slots into **Phase 2**
(feature-flagged optimizer switch) as a replacement for the existing
`extra_usage_multiplier` call in `compute_effective_price`. The kill switch
`REALTIME_PRICING_ENABLED=false` restores the old step-function behavior. No new
DB tables, no new collectors, no new cron jobs — just one new function in
`pricing_engine.py` and the deletion of three threshold constants from
`live_router.py`.

---

## 9. Endpoint-Per-Model Pricing Architecture

> **Felix's core vision (2025-08-05):** "Separate models for every AI endpoint
> so prices are set correctly, and the consumer can just choose based on the
> price per token exposed from these different endpoints. In future, these
> models live in a Routstr node; the consumer lives in a Routstr client."

Sections 1–8 describe a **centralised** pipeline: one `RealtimePricing`
singleton feeds Kalman filters → shared multiplier layer (`pricing_engine.py`)
→ smart optimizer that filters by tier, health, exhaustion, scarcity, then
sorts. This section describes the **inversion**: each endpoint becomes a
self-contained price model owning ALL pricing logic; the optimizer shrinks to
a one-line `min()`.

### 9.1 The inversion

| | Current (§1–8) | Target (§9) |
|---|---|---|
| **State lives in** | Optimizer + pricing_engine dicts | Each endpoint model |
| **Price computed by** | `compute_effective_price(base, peak, scarcity, health, pace, extra)` — 10 args | `model.get_price()` — zero args |
| **Optimizer does** | Filter tier, check health, check exhaustion, compute scarcity, sort | `min(models, key=lambda m: m.get_price())` |
| **Adding an endpoint** | Edit optimizer, add Kalman, add multipliers, add tier | Subclass `PriceModel`, implement 3 methods |
| **Coupling** | Optimizer imports pricing_engine, price_kalman, consumption_kalman | Optimizer imports only the `PriceModel` protocol |

### 9.2 Abstract base — the `PriceModel` protocol

```python
@runtime_checkable
class PriceModel(Protocol):
    """One endpoint = one self-contained price model. Owns ALL pricing
    complexity. Exposes exactly ONE number: $/M for the next token.

    Contract:
      • get_price() — O(1), lock-free, NEVER raises. +inf = unavailable.
      • refresh()   — batch state update from billing APIs. Cron-driven.
      • get_metadata() — frozen dict for dashboards/debugging.
    """
    @property
    def endpoint_id(self) -> str: ...          # 'ollama_cloud:glm-5.2'

    def get_price(self, ctx: "RequestContext | None" = None) -> float: ...
    def refresh(self) -> None: ...
    def get_metadata(self) -> dict: ...         # source, confidence, tier, breakdown
```

`RequestContext` is a frozen dataclass (`required_tier`, `estimated_tokens`,
`task_type`) describing the *request* — not the endpoint. A model that can't
serve the context returns `+inf` (wrong tier, insufficient quota). Without a
context, returns the model's default price.

### 9.3 Per-endpoint models

#### `OllamaCloudPriceModel`

Owns session/weekly windows, burn rate (`ConsumptionKalman`), per-model
billing rates, health, tier. The `quota_price_factor` from §8.2 lives HERE.

```
get_price():
  if breaker_tripped or exhausted: return +inf
  p_extra = predict_p_extra(burn_rate, time_remaining, quota_total)  # §8.2
  rate = (1 - p_extra) * base_included + p_extra * base_extra
  rate *= health_graduated(failure_count)     # 1.0 / 1.5 / 3.0 / 10.0
  if ctx and tier_rank[tier] < ctx.required_tier: return +inf
  return max(rate, MIN_EFFECTIVE_PRICE)
```

#### `ZaiSubscriptionPriceModel`

Owns monthly fee, tokens consumed this month, peak hours (UTC 06–09 ×3.0),
quota windows (5h/weekly/monthly), friend premium (21%), health.

```
get_price():
  if breaker_tripped: return +inf
  amortized = monthly_fee / (tokens_this_month / 1e6)    # $/M (ours: $155/mo)
  if is_friend: amortized *= 1.21                        # ADR-005 premium
  if hour_utc in peak_hours: amortized *= peak_multiplier
  amortized *= pace_factor_multi(quota_windows, burn_rate)
  amortized *= health_graduated(failure_count)
  if ctx and tier_rank[tier] < ctx.required_tier: return +inf
  return max(amortized, MIN_EFFECTIVE_PRICE)
```

At month-start (tokens ≈ 0), the model holds its last stable rate until
`tokens_this_month` exceeds `MIN_SAMPLE_TOKENS` (1M), matching §2 confidence
gating — prevents the amortized rate from exploding.

#### `PpqPayPerUsePriceModel` / `OpenRouterPriceModel` / `DeepInfraPriceModel`

Pay-per-token providers have constant marginal cost but still track balance,
depletion, and health. All three share this structure:

```
get_price():
  if breaker_tripped or balance_depleted: return +inf
  rate = published_rate_per_m * health_graduated(failure_count)
  if balance_low: rate *= scarcity_for_balance(balance_usd)
  if ctx and tier_rank[tier] < ctx.required_tier: return +inf
  return max(rate, MIN_EFFECTIVE_PRICE)
```

OpenRouter's `published_rate_per_m` shifts from `providers.yaml` list prices
(`source='published_list'`) to per-request `usage.cost` extraction once Phase
3.6 lands. PPQ tracks balance from `api_burn.db`; DeepInfra from
`daily_spend.actual_cost`.

### 9.4 What moves inside the price

Every multiplier in `pricing_engine.py` and every filter in
`routing_optimizer._evaluate_provider` migrates INTO the model:

| Current function | Moves to | How |
|---|---|---|
| `peak_multiplier()` (3.0× UTC 06–09) | `ZaiSubscriptionPriceModel` | Only z.ai has peak; baked into `get_price()` |
| `scarcity_factor()` (ramp >50% quota) | Each quota-bearing model | Model knows its own usage |
| `health_pricing_factor()` (1→1.5→3→10→∞) | Every model | Each tracks own `failure_count` |
| `pace_factor()` (predictive burn pacing) | Each quota-bearing model | Subsumed by `p_extra` for Ollama; standalone for z.ai |
| `extra_usage_multiplier()` (regime step) | `OllamaCloudPriceModel` | Replaced by continuous `p_extra` (§8.2) |
| tier gate (`TIER_RANK < required`) | Every model | Returns `+inf` for incompatible `ctx.required_tier` |
| exhaustion gate (`will_exhaust && low`) | Each model | Returns `+inf` if can't serve `ctx.estimated_tokens` |
| RP-5 throttle (0.85/1.0 thresholds) | `OllamaCloudPriceModel` | Price ramp handles it continuously |

After migration, `pricing_engine.py` retains only pure helpers
(`scarcity_factor`, `health_graduated`, `pace_factor`) as building blocks.
The orchestrating `compute_effective_price()` is deleted — each model
composes its own price.

### 9.5 The dumb consumer

```python
class PriceConsumer:
    """Reads prices, picks cheapest. Knows nothing about internals."""
    def select(self, ctx: RequestContext) -> PriceModel | None:
        priced = [(m, m.get_price(ctx)) for m in self._models]
        cheapest = min(priced, key=lambda p: p[1])
        return None if cheapest[1] == float("inf") else cheapest[0]
```

- **Failover** is implicit: on failure, the model's `failure_count`
  increments → price rises via `health_graduated` → next-cheapest wins on
  re-query. No separate failover list.
- **Capacity** is a price adjustment: quota depletion raises price (scarcity,
  pace, p_extra); when price exceeds competitors, traffic reroutes
  automatically. Zero capacity = `+inf` = excluded.
- **Tie-breaking** among equally-priced models (e.g. two z.ai keys) is the
  consumer's only non-trivial decision: round-robin within a ±5% band, or
  deterministic hash on request ID. Not routing logic.

### 9.6 Mapping to Routstr

| Merchant-routing-engine | Routstr |
|---|---|
| `PriceModel` instance | **Node** — runs independently, owns one endpoint |
| `PriceConsumer` | **Client** — subscribes to price events, routes requests |
| `get_price()` | `price_usd_per_m` tag in a Nostr event |
| `get_metadata()` | Other tags in the same event |
| `refresh()` cron | Node's internal update loop |

**Event design:** NIP-33 parameterized replaceable event, kind `31999`
(application-specific range), per `(node_pubkey, model)`:

```jsonc
{ "kind": 31999, "pubkey": "<node>",
  "tags": [
    ["d", "ollama_cloud:glm-5.2"],
    ["price_usd_per_m", "0.0155"],
    ["tier", "high"], ["model", "glm-5.2"],
    ["available", "true"],
    ["capacity_remaining", "450000000"],
    ["source", "ollama_billing_api"], ["confidence", "0.92"]
  ], "content": "" }
```

The client subscribes `["REQ", "sub", {"kinds": [31999]}]`, builds local
`PriceModel` adapters from events, runs the same `min(price)` selection.
When a node's price changes, it republishes — the client updates within
seconds. **Capacity and availability are encoded in the price and `available`
tag** — no separate protocol. `available=false` or `price=inf` → not selected.

Our `RealtimePricing` module (§2) IS the prototype Routstr node: it runs
collectors, maintains state, exposes a price. The only addition is publishing
that price as a Nostr event.

### 9.7 Migration path

Structural refactor in four phases, each revertible via
`REALTIME_PRICING_ENABLED`.

**Phase A — Protocol + adapters (no behavior change).** Define `PriceModel`.
Write thin adapters wrapping the existing `PriceKalman` +
`pricing_engine.compute_effective_price` pipeline. Optimizer gains
`select_v2()` using adapters; old `route()` stays default. Verify identical
decisions on replay.

**Phase B — Move state into models.** Migrate per-provider state (quota,
health, failure_count, tier) from optimizer dicts into each model. Models call
`pricing_engine` helpers internally. Delete tier/health/exhaustion gates and
scarcity computation from the optimizer — now inside models. Verify same
decisions; `routing_optimizer.py` loses ~100 lines.

**Phase C — Absorb the multiplier layer.** Move `peak_multiplier`,
`extra_usage_multiplier`, RP-5 throttle into their models. Delete
`compute_effective_price()`; keep only pure helpers. Optimizer becomes the
one-liner from §9.5. Verify replay suite passes.

**Phase D — Routstr split.** Each model becomes an independent process
publishing price events. Consumer subscribes. Requires the Nostr event layer
(§9.6); out of scope for this repo. The `PriceModel` interface is
forward-compatible: a Nostr-backed model implements the same protocol, with
`refresh()` reading from a relay subscription.

> Phases A–C are internal to merchant-routing-engine and don't depend on §4
> shipping first — adapters can wrap cold-start Kalman seeds. The full vision
> (measured rates inside self-contained models) requires both §4 (collection)
> and §9 (inversion) to land.
