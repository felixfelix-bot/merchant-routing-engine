"""real_price_tracker.py — rolling real $/M per provider per model from stored cost data.

RP-3 of the real-price-tracker plan (docs/real-price-tracker-plan.md §Step 3).

What this module is
    A focused, dependency-free calculator that turns the ``cost_usd`` values
    captured by RP-2 (one real USD charge per ``api_calls`` row) into a rolling
    token-weighted $/M rate per (provider, model) pair. It is the *direct,
    transparent* layer underneath the heavier Kalman-based
    :mod:`src.realtime_pricing` system: no filter, no smoothing, just
    ``SUM(cost_usd) / SUM(total_tokens) * 1e6`` over a trailing window.

    The heavier ``realtime_pricing.RealtimePricing`` singleton already exists and
    fuses many sources (Ollama billing API, ppq burn ledger, daily_spend, …)
    through a Kalman filter. This module deliberately does **less**: it answers a
    single, auditable question — "what did we actually pay per million tokens for
    this provider recently, according to the rows we logged?" — in constant time,
    with a 5-minute cache, so the proxy hot path (``ThreadingHTTPServer``) and the
    CVM snapshot can call it without contending on the Kalman refresh cycle.

Design rules (mirror src/cost_extraction.py)
    * **NEVER raises.** Runs inside the proxy's request-handling path. Any error
      — missing DB, locked table, missing ``cost_usd`` column, NaN — is swallowed
      and degrades to ``None`` (real rate unknown) or a documented fallback.
    * **Thread-safe.** A module-level lock guards the cache; every public call
      opens its own short-lived sqlite connection (connections are not shared
      across threads).
    * **Pure function of (provider, model, window, db).** No hidden global state
      beyond the self-contained TTL cache.
    * **Cheap.** One indexed aggregate query per (provider, model) per 5 minutes.

Public API
    ``get_real_rate(provider, model=None, window_hours=168) -> float | None``
        Token-weighted $/M over the trailing window. ``None`` when there are
        fewer than ``MIN_CALLS_FOR_RATE`` costed calls (insufficient data).

    ``get_all_rates(window_hours=168) -> dict[provider, dict[model, float]]``
        All provider→model→$/M rates in one query (for the CVM snapshot).

    ``get_rate_with_fallback(provider, model=None) -> float``
        Real rate first, then the Ollama billing API (for ollama_cloud), then
        clearly-marked last-resort estimates. Always returns a float.

    ``detect_price_change(provider, model=None) -> bool``
        True when the 24h rate deviates >50% from the 7d rate (price change).

Trailing-rate API (T6 — Ollama 90d + paid-endpoint 30d measured rates)
    These wrap :func:`get_real_rate` with a per-provider trailing window and a
    cold-start seed, so the router's base-rate dict can be populated from one
    auditable place. The window per provider is :data:`PROVIDER_WINDOW_HOURS`::

        ollama_cloud            →  90 days (2160 h)  — slow-moving subscription
        ppq / deepinfra /        →  30 days (720 h)  — faster price discovery
          openrouter
        ours / friend           → 365 days (8760 h) — z.ai amortization window

    ``get_trailing_rate(provider, model=None) -> float | None``
        Measured $/M over the provider's trailing window (cost_usd aggregate;
        ``ollama_cloud`` additionally falls back to its billing-API measurement
        when local cost_usd is empty). ``None`` when there is no measured data.

    ``get_trailing_rate_with_seed(provider, model=None) -> float``
        :func:`get_trailing_rate` first, then the cold-start
        :data:`SEED_RATES`, then :data:`UNKNOWN_PROVIDER_FALLBACK`. Always a
        float — safe to seed ``_DEFAULT_CONVERGED_RATES`` directly.

    ``get_all_trailing_rates() -> dict[str, float]``
        ``{provider: $/M}`` for every provider in
        :data:`PROVIDER_WINDOW_HOURS`, seeds filling the cold ones. This is the
        one-call shape that drops straight into ``live_router``'s base-rate dict.

    ``get_rate_readiness(providers=None) -> dict``
        Structured ``{provider: {rate, source, has_data, window_hours}}``.
        ``source`` is ``"measured"`` (real data), ``"seed"`` (cold start), or
        ``"unknown"`` (no seed entry). Powers dashboards and the gate below.

    ``gate_all_rates_have_data(providers=None) -> bool``
        True iff every provider in :data:`REQUIRED_RATE_PROVIDERS` has a
        non-zero *measured* trailing rate (i.e. the system is warm and no rate
        is running on a seed). The T6 acceptance gate.

Note on provider names
    ``key_name`` in ``api_calls`` is matched literally. Callers holding legacy
    aliases ("zai_ours", "manager", …) should normalize via
    :func:`src.provider_names.normalize_provider_name` before calling.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from typing import Any

__all__ = [
    "get_real_rate",
    "get_all_rates",
    "get_rate_with_fallback",
    "detect_price_change",
    "clear_cache",
    # ── Trailing-rate API (T6: Ollama 90d + paid-endpoint 30d measured rates) ──
    "get_trailing_rate",
    "get_trailing_rate_with_seed",
    "get_all_trailing_rates",
    "get_rate_readiness",
    "gate_all_rates_have_data",
    "PROVIDER_WINDOW_HOURS",
    "SEED_RATES",
    "REQUIRED_RATE_PROVIDERS",
    # ── z.ai amortized rate (T5: trailing-365d flat-rate amortization) ────────
    "get_zai_amortized_rate",
    "ZAI_ANNUAL_BUDGET",
    "ZAI_FRIEND_PREMIUM",
    "ZAI_SEED_RATE",
    "ZAI_MIN_DATA_DAYS",
    "ZAI_WINDOW_HOURS",
    "ZAI_CACHE_TTL_SECONDS",
    "ZAI_PROVIDERS",
    # ── Tunables / constants ────────────────────────────────────────────────────
    "LAST_RESORT_RATES",
    "DEFAULT_DB_PATH",
    "CACHE_TTL_SECONDS",
    "MIN_CALLS_FOR_RATE",
    "CHANGE_THRESHOLD",
    "CHANGE_RECENT_HOURS",
    "CHANGE_BASELINE_HOURS",
    "UNKNOWN_PROVIDER_FALLBACK",
]

_log = logging.getLogger(__name__)

# ── Tunables ─────────────────────────────────────────────────────────────────

#: Production usage DB (where ``api_calls.cost_usd`` lives after RP-1/RP-2).
DEFAULT_DB_PATH: str = os.path.expanduser("~/.hermes/bot/zai_usage.db")

#: Cache TTL. Prices do not change per-second; recompute at most every 5 minutes.
CACHE_TTL_SECONDS: float = 300.0

#: Minimum number of costed calls in the window before we trust the rate.
#: Below this we return ``None`` (insufficient data) rather than a noisy number.
MIN_CALLS_FOR_RATE: int = 100

#: Window for the "recent" rate used in change detection (24 hours).
CHANGE_RECENT_HOURS: float = 24.0

#: Window for the "baseline" rate used in change detection (7 days = 168 hours).
CHANGE_BASELINE_HOURS: float = 168.0

#: Relative deviation above which :func:`detect_price_change` returns True.
CHANGE_THRESHOLD: float = 0.50

#: Conservative rate returned for a provider with neither real data nor a known
#: last-resort estimate. Set high so the optimizer never preferentially routes
#: to an unknown "cheap" provider.
UNKNOWN_PROVIDER_FALLBACK: float = 1.0  # $/M

# ── Last-resort estimates ────────────────────────────────────────────────────
# These are ESTIMATES, not measurements. Used ONLY when there is no real
# ``cost_usd`` data AND (for ollama_cloud) the billing API is unreachable.
# Values sourced from src/realtime_pricing.py DEFAULT_COLD_START_RATES, which
# documents their provenance. Every entry here is stale the moment real data
# arrives; the warnings logged when it is used make that visible.
LAST_RESORT_RATES: dict[str, float] = {
    "ours":         0.001,    # z.ai flat-rate subscription → marginal $0, floored
    "friend":       0.001,    # shared z.ai subscription → marginal $0, floored
    "ollama_cloud": 0.0155,   # MEASURED included rate (pre-RP-3 observation)
    "ppq":          0.14,     # known list price
    "openrouter":   0.135,    # known list price
    "deepinfra":    1.30,     # known list price
}

# ── Trailing-rate configuration (T6) ─────────────────────────────────────────
# Per-provider trailing window (hours) for the measured "base rate" used to seed
# the router. Long windows give stability on slow-moving subscriptions; short
# windows give faster price discovery on pay-per-token endpoints. Mirrors the
# consultant-reviewed plan (docs/plan-review-consultant.md §R4 / §8):
#   3A z.ai   → 365 d (8760 h)   3B Ollama → 90 d (2160 h)
#   3C paid   →  30 d (720 h)    (ppq / deepinfra / openrouter)
PROVIDER_WINDOW_HOURS: dict[str, float] = {
    "ours":         365 * 24,   # 8760 — z.ai amortization
    "friend":       365 * 24,   # 8760 — z.ai amortization
    "ollama_cloud": 90 * 24,    # 2160 — slow-moving subscription
    "ppq":          30 * 24,    # 720  — pay-per-token
    "deepinfra":    30 * 24,    # 720  — pay-per-token
    "openrouter":   30 * 24,    # 720  — pay-per-token
}

#: Cold-start seed $/M. Returned by :func:`get_trailing_rate_with_seed` only
#: when there is NO measured data for the provider. Every entry here is stale
#: the moment real ``cost_usd`` (or, for ollama_cloud, the billing API) data
#: arrives; :func:`gate_all_rates_have_data` exists to flag exactly when seeds
#: are still in effect. Values are aligned with :data:`LAST_RESORT_RATES` (same
#: concept, single source of truth per provider) and the seed table in
#: docs/real-price-tracker-plan-v3.md.
SEED_RATES: dict[str, float] = {
    "ours":         0.001,
    "friend":       0.001,
    "ollama_cloud": 0.0155,   # measured blended rate (pre-RP-3)
    "ppq":          0.14,     # list price
    "openrouter":   0.135,    # list price
    "deepinfra":    1.30,     # all-time measured average (real)
}

#: Providers that must report a measured (non-seed) rate before the T6 gate
#: passes. Defaults to the paid + subscription endpoints the router routes to.
#: z.ai ``ours``/``friend`` are included because their amortized rate is also a
#: measurement (monthly_fee / tokens), not a guess.
REQUIRED_RATE_PROVIDERS: tuple[str, ...] = (
    "ollama_cloud",
    "ppq",
    "deepinfra",
    "openrouter",
)

# ── z.ai amortized-rate model (Task T5) ──────────────────────────────────────
# The z.ai keys are a flat-rate subscription, so the cost_usd layer (~$0
# marginal) cannot express their real per-token cost. Task T5 amortizes a fixed
# annual budget over trailing-365d token volume to give the router a stable,
# auditable base $/M for the flat-rate providers. See
# docs/plan-remaining-v2.md §Task 5.
#
# ``provider LIKE 'zai%'`` in the spec denotes the z.ai subscription keys. The
# production ``api_calls`` table stores them under their canonical names
# (``ours`` / ``friend``), with legacy aliases ``zai_ours`` / ``zai_friend``
# (src/provider_names.py). :func:`_query_zai_window` matches BOTH forms so the
# rate is always populated from real data regardless of the row's convention.

#: Annual budget ($) amortized over trailing-365d z.ai token volume.
ZAI_ANNUAL_BUDGET: float = 300.0

#: Premium applied to the ``friend`` key so the optimizer prefers ``ours``.
#: ``friend`` effective rate = ours base rate x this factor.
ZAI_FRIEND_PREMIUM: float = 1.21

#: Seed $/M returned when fewer than ``ZAI_MIN_DATA_DAYS`` of data exist.
#: Matches the observed z.ai folklore: ~21B tok/yr at $300 -> $0.0143/M.
ZAI_SEED_RATE: float = 0.014

#: Minimum data span (days) required before the calculated rate is trusted.
#: Below this the seed is returned to avoid a noisy cold-start number.
ZAI_MIN_DATA_DAYS: float = 30.0

#: Trailing window length (hours) for the amortized calculation (365 days).
ZAI_WINDOW_HOURS: float = 365.0 * 24.0

#: Cache TTL for the amortized rate. Per docs/plan-remaining-v2.md §Task 5 the
#: rate is refreshed daily, not per-request (token volume moves slowly).
ZAI_CACHE_TTL_SECONDS: float = 86400.0

#: Canonical z.ai flat-rate subscription providers.
ZAI_PROVIDERS: tuple[str, ...] = ("ours", "friend")

# ── Thread-safe TTL cache ────────────────────────────────────────────────────
# key: (provider, model, window_hours, db_path) → (value, computed_at)
# value is a float rate OR None (a cached "insufficient data" miss). Caching the
# miss too prevents a thundering herd of identical queries against a provider
# that simply has no costed rows yet.
_cache: dict[tuple[str, str | None, float, str], tuple[float | None, float]] = {}
_cache_lock = threading.Lock()

# Trailing-rate cache (T6). key: (provider, model, db_path) → (value, ts).
# Caches the full trailing-rate result — including the ollama billing-API
# fallback — so the (potentially slow) API is hit at most once per TTL per pair.
_trailing_cache: dict[tuple[str, str | None, str], tuple[float | None, float]] = {}

# z.ai amortized-rate cache (T5). key: (provider, db_path) → (rate, ts).
# Daily TTL (ZAI_CACHE_TTL_SECONDS) — separate from the 5-min cost_usd cache and
# the T6 trailing cache because the amortized rate moves on a daily cadence.
_zai_cache: dict[tuple[str, str], tuple[float, float]] = {}


def clear_cache() -> None:
    """Drop every cached rate. Tests call this between assertions; production
    may call it after a forced DB refresh."""
    with _cache_lock:
        _cache.clear()
        _trailing_cache.clear()
        _zai_cache.clear()


# ── Internal helpers ─────────────────────────────────────────────────────────


def _resolve_db(db_path: str | None) -> str:
    """Pick the DB path: explicit arg, else the production default."""
    return db_path if db_path is not None else DEFAULT_DB_PATH


def _to_float(val: Any) -> float | None:
    """Best-effort float coercion. Returns None on any failure / NaN / inf."""
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _rate_from_sums(count: int, sum_cost: float, sum_tokens: float) -> float | None:
    """Turn raw aggregates into a $/M rate, applying the minimum-call guard.

    Returns ``None`` when there is insufficient data (fewer than
    ``MIN_CALLS_FOR_RATE`` calls) or no tokens to divide by. Guards against
    NaN/negative rates. Pure function; never raises.
    """
    if count < MIN_CALLS_FOR_RATE:
        return None
    if sum_tokens <= 0:
        return None
    rate = (sum_cost / sum_tokens) * 1e6
    if rate != rate or rate < 0:  # NaN or nonsensical
        return None
    return rate


def _query_window(
    db_path: str,
    provider: str,
    model: str | None,
    since_ts: float,
) -> tuple[int, float, float]:
    """Aggregate the costed calls for (provider[, model]) since ``since_ts``.

    Returns ``(call_count, sum_cost_usd, sum_total_tokens)``. ``cost_usd IS NULL``
    rows are excluded (no real charge known). Never raises — on any DB error
    returns ``(0, 0.0, 0.0)`` so callers fall through to ``None``/fallback.
    """
    try:
        conn = sqlite3.connect(db_path, timeout=2)
        try:
            if model is None:
                row = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(cost_usd), 0), "
                    "COALESCE(SUM(total_tokens), 0) "
                    "FROM api_calls "
                    "WHERE key_name = ? AND ts > ? AND cost_usd IS NOT NULL",
                    (provider, since_ts),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(cost_usd), 0), "
                    "COALESCE(SUM(total_tokens), 0) "
                    "FROM api_calls "
                    "WHERE key_name = ? AND model = ? AND ts > ? "
                    "AND cost_usd IS NOT NULL",
                    (provider, model, since_ts),
                ).fetchone()
        finally:
            conn.close()
    except Exception:
        # Missing DB, locked table, pre-RP-1 schema without cost_usd, … — degrade
        # to "no data" rather than breaking the caller.
        _log.debug("real_price_tracker: window query failed", exc_info=True)
        return (0, 0.0, 0.0)
    if not row:
        return (0, 0.0, 0.0)
    return (int(row[0] or 0), float(row[1] or 0.0), float(row[2] or 0.0))


def _ollama_api_rate(model: str | None) -> float | None:
    """Measured $/M from the Ollama billing API (``activity.cost`` / tokens).

    Used as the first fallback for ``ollama_cloud`` when the local
    ``api_calls.cost_usd`` window is empty. Aggregates across models unless a
    specific ``model`` is requested. Returns ``None`` on any failure or when the
    API has no usable data. Never raises (the import is lazy so a missing
    dependency never breaks import of this module).
    """
    try:
        from src.ollama_extra_usage import fetch_ollama_usage

        data = fetch_ollama_usage()
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    activity = data.get("activity")
    if not isinstance(activity, dict):
        return None
    total_cost = 0.0
    total_tokens = 0.0
    for model_name, entry in activity.items():
        if not isinstance(entry, dict):
            continue
        if model is not None and model_name != model:
            continue
        cost = _to_float(entry.get("cost") or entry.get("spend"))
        toks = _to_float(entry.get("total_tokens") or entry.get("tokens"))
        if cost is None or toks is None or toks <= 0:
            continue
        total_cost += cost
        total_tokens += toks
    if total_tokens <= 0:
        return None
    rate = (total_cost / total_tokens) * 1e6
    if rate != rate or rate < 0:
        return None
    return rate


# ── Public API ───────────────────────────────────────────────────────────────


def get_real_rate(
    provider: str,
    model: str | None = None,
    window_hours: float = 168,
    *,
    db_path: str | None = None,
    _now: float | None = None,
) -> float | None:
    """Measured token-weighted $/M for ``(provider[, model])`` over the trailing
    ``window_hours`` (default 7 days).

    Computation::

        SUM(cost_usd) / SUM(total_tokens) * 1e6

    over every ``api_calls`` row matching ``key_name=provider`` (and
    ``model=model`` when given) with a non-null ``cost_usd`` and ``ts`` newer than
    ``now - window_hours``.

    Returns ``None`` when there are fewer than ``MIN_CALLS_FOR_RATE`` (100)
    matching calls — i.e. insufficient data. Results are cached per
    ``(provider, model, window_hours, db_path)`` for ``CACHE_TTL_SECONDS`` (5 min)
    so the proxy hot path does not re-aggregate on every request.

    Thread-safe. Never raises.

    Parameters
    ----------
    provider
        Canonical provider key as stored in ``api_calls.key_name``.
    model
        Optional model name (exact match against ``api_calls.model``). ``None``
        aggregates across all models for the provider.
    window_hours
        Trailing window length in hours (default 168 = 7 days).
    db_path
        Override the DB path (tests pass a temp DB). Defaults to
        :data:`DEFAULT_DB_PATH`.

    Returns
    -------
    float or None
        $/M as a non-negative float, or ``None`` if insufficient data.
    """
    now = _now if _now is not None else time.time()
    db = _resolve_db(db_path)
    key = (provider, model, float(window_hours), db)

    # Cache check (read under lock; cheap).
    with _cache_lock:
        cached = _cache.get(key)
    if cached is not None:
        value, computed_at = cached
        if (now - computed_at) < CACHE_TTL_SECONDS:
            return value

    # Cache miss or expired → recompute from the DB.
    since = now - window_hours * 3600.0
    count, sum_cost, sum_tokens = _query_window(db, provider, model, since)
    rate = _rate_from_sums(count, sum_cost, sum_tokens)

    # Cache both hits and misses so an under-populated provider doesn't get
    # hammered on every request.
    with _cache_lock:
        _cache[key] = (rate, now)
    return rate


def get_all_rates(
    window_hours: float = 168,
    *,
    db_path: str | None = None,
    _now: float | None = None,
) -> dict[str, dict[str | None, float]]:
    """Real $/M for every (provider, model) pair in one query.

    Groups costed calls by ``(key_name, model)`` and returns the same
    token-weighted rate as :func:`get_real_rate`, dropping any group with fewer
    than ``MIN_CALLS_FOR_RATE`` calls. Intended for the CVM snapshot (one call
    paints the whole pricing picture).

    Returns
    -------
    dict
        ``{provider: {model: $/M, ...}, ...}``. ``model`` is ``None`` for rows
        whose ``api_calls.model`` was NULL. Empty dict on any DB error.

    Note
    ----
    ``get_all_rates`` does not read the per-pair cache (it is a single GROUP BY
    query and is meant to be called infrequently, e.g. once per snapshot). For
    hot-path single-pair lookups use :func:`get_real_rate`.
    """
    now = _now if _now is not None else time.time()
    db = _resolve_db(db_path)
    since = now - window_hours * 3600.0

    try:
        conn = sqlite3.connect(db, timeout=2)
        try:
            rows = conn.execute(
                "SELECT key_name, model, COUNT(*), "
                "COALESCE(SUM(cost_usd), 0), COALESCE(SUM(total_tokens), 0) "
                "FROM api_calls "
                "WHERE ts > ? AND cost_usd IS NOT NULL "
                "GROUP BY key_name, model",
                (since,),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        _log.debug("real_price_tracker: get_all_rates query failed", exc_info=True)
        return {}

    out: dict[str, dict[str | None, float]] = {}
    for key_name, model, count, sum_cost, sum_tokens in rows:
        rate = _rate_from_sums(
            int(count or 0), float(sum_cost or 0.0), float(sum_tokens or 0.0)
        )
        if rate is None:
            continue
        provider = key_name if key_name else "unknown"
        out.setdefault(provider, {})[model] = rate
    return out


# ── Trailing-rate API (T6: Ollama 90d + paid-endpoint 30d measured rates) ────


def _provider_window(provider: str) -> float:
    """Trailing window (hours) for ``provider``.

    Falls back to the module default (168 h = 7 d) for any provider not listed
    in :data:`PROVIDER_WINDOW_HOURS`, so an unknown provider still gets a sane
    window rather than a KeyError.
    """
    return PROVIDER_WINDOW_HOURS.get(provider, 168.0)


def get_trailing_rate(
    provider: str,
    model: str | None = None,
    *,
    db_path: str | None = None,
    _now: float | None = None,
) -> float | None:
    """Measured $/M for ``provider`` over its provider-specific trailing window.

    The window is :data:`PROVIDER_WINDOW_HOURS` — 90 days for ``ollama_cloud``,
    30 days for the pay-per-token endpoints (``ppq``/``deepinfra``/
    ``openrouter``), 365 days for the z.ai keys. Computation is the same
    token-weighted ``SUM(cost_usd) / SUM(total_tokens) * 1e6`` as
    :func:`get_real_rate`.

    For ``ollama_cloud`` only: when the local ``cost_usd`` window is empty (the
    usual case — Ollama is a flat subscription with ~$0 marginal per-call cost),
    the Ollama billing-API measurement (:func:`_ollama_api_rate`) is used
    instead. That too is a *measurement*, not a seed, so it counts as real data
    for :func:`gate_all_rates_have_data`.

    Returns ``None`` when there is no measured data of any kind. Results are
    cached per ``(provider, model, db_path)`` for :data:`CACHE_TTL_SECONDS`.
    Thread-safe. Never raises.
    """
    now = _now if _now is not None else time.time()
    db = _resolve_db(db_path)
    key = (provider, model, db)

    with _cache_lock:
        cached = _trailing_cache.get(key)
    if cached is not None:
        value, computed_at = cached
        if (now - computed_at) < CACHE_TTL_SECONDS:
            return value

    window = _provider_window(provider)
    rate = get_real_rate(provider, model, window_hours=window, db_path=db, _now=now)

    # ollama_cloud: real measurement may live in the billing API, not cost_usd.
    if rate is None and provider == "ollama_cloud":
        rate = _ollama_api_rate(model)

    with _cache_lock:
        _trailing_cache[key] = (rate, now)
    return rate


def get_trailing_rate_with_seed(
    provider: str,
    model: str | None = None,
    *,
    db_path: str | None = None,
    _now: float | None = None,
) -> float:
    """Measured trailing rate, falling back to a cold-start seed. Always a float.

    Resolution order:

    1. :func:`get_trailing_rate` (the measured rate over the provider window).
    2. :data:`SEED_RATES` (clearly-marked cold-start estimate) — logged at INFO
       so operators can see which providers are still running on seeds.
    3. :data:`UNKNOWN_PROVIDER_FALLBACK` (conservative rate for a provider with
       no seed entry) — logged at WARNING.

    This is the function to call when seeding ``_DEFAULT_CONVERGED_RATES``: it
    never returns ``None``, so the router always has a numeric base rate, yet
    every non-measured value is visible in the logs and via
    :func:`get_rate_readiness`. Never raises.
    """
    rate = get_trailing_rate(provider, model, db_path=db_path, _now=_now)
    if rate is not None and rate > 0:
        return rate

    seed = SEED_RATES.get(provider)
    if seed is not None:
        _log.info(
            "real_price_tracker: %s%s cold-start — using SEED $%.6g/M "
            "(no measured data yet; will be replaced as cost_usd accumulates)",
            provider,
            f"/{model}" if model else "",
            seed,
        )
        return seed

    _log.warning(
        "real_price_tracker: %s%s has no measured data and no seed — returning "
        "conservative fallback $%.6g/M",
        provider,
        f"/{model}" if model else "",
        UNKNOWN_PROVIDER_FALLBACK,
    )
    return UNKNOWN_PROVIDER_FALLBACK


def get_all_trailing_rates(
    *,
    providers: tuple[str, ...] | list[str] | None = None,
    db_path: str | None = None,
    _now: float | None = None,
) -> dict[str, float]:
    """``{provider: $/M}`` for every provider, measured where possible.

    Iterates :data:`PROVIDER_WINDOW_HOURS` (or ``providers`` if given), calling
    :func:`get_trailing_rate_with_seed` for each. The result has the same shape
    as ``live_router._DEFAULT_CONVERGED_RATES`` — it can be assigned directly as
    the router's base-rate dict. Cold providers carry their seed value; the
    readiness/gate functions report which entries are still seeds.
    """
    provs = tuple(PROVIDER_WINDOW_HOURS) if providers is None else tuple(providers)
    return {
        p: get_trailing_rate_with_seed(p, db_path=db_path, _now=_now) for p in provs
    }


def get_rate_readiness(
    providers: tuple[str, ...] | list[str] | None = None,
    *,
    db_path: str | None = None,
    _now: float | None = None,
) -> dict[str, dict[str, object]]:
    """Per-provider readiness report for dashboards and the T6 gate.

    Returns ``{provider: {rate, source, has_data, window_hours}}`` where:

    * ``rate`` — the effective $/M (measured if available, else seed, else the
      conservative fallback). Always a float.
    * ``source`` — ``"measured"`` (real cost_usd / billing-API data),
      ``"seed"`` (cold-start :data:`SEED_RATES`), or ``"unknown"`` (no seed).
    * ``has_data`` — ``True`` iff ``source == "measured"`` and ``rate > 0``.
    * ``window_hours`` — the trailing window used for this provider.

    Defaults to :data:`PROVIDER_WINDOW_HOURS`; pass ``providers`` to narrow.
    Never raises.
    """
    provs = tuple(PROVIDER_WINDOW_HOURS) if providers is None else tuple(providers)
    report: dict[str, dict[str, object]] = {}
    for p in provs:
        window = _provider_window(p)
        measured = get_trailing_rate(p, db_path=db_path, _now=_now)
        if measured is not None and measured > 0:
            source, rate = "measured", measured
        elif p in SEED_RATES:
            source, rate = "seed", SEED_RATES[p]
        else:
            source, rate = "unknown", UNKNOWN_PROVIDER_FALLBACK
        report[p] = {
            "rate": rate,
            "source": source,
            "has_data": source == "measured",
            "window_hours": window,
        }
    return report


def gate_all_rates_have_data(
    providers: tuple[str, ...] | list[str] | None = None,
    *,
    db_path: str | None = None,
    _now: float | None = None,
) -> bool:
    """T6 acceptance gate: True iff every required provider has measured data.

    Checks that each provider in :data:`REQUIRED_RATE_PROVIDERS` (or
    ``providers`` if given) reports a non-zero *measured* trailing rate via
    :func:`get_trailing_rate` — i.e. no provider is still relying on a cold-start
    seed. Returns ``False`` the moment any required provider is cold, has a
    zero/negative rate, or errors out. This is the "all rates non-zero with
    data" gate from the task spec.

    Never raises — a query failure on one provider fails the gate closed
    (returns ``False``) rather than propagating.
    """
    provs = (
        tuple(REQUIRED_RATE_PROVIDERS) if providers is None else tuple(providers)
    )
    for p in provs:
        try:
            rate = get_trailing_rate(p, db_path=db_path, _now=_now)
        except Exception:  # never let the gate raise
            _log.debug("real_price_tracker: gate query failed for %s", p, exc_info=True)
            return False
        if rate is None or rate <= 0:
            return False
    return True


# ── z.ai amortized rate (Task T5) ────────────────────────────────────────────


def _query_zai_window(db_path: str, since_ts: float) -> tuple[int, float, float]:
    """Aggregate the z.ai providers' tokens since ``since_ts``.

    Matches every row whose ``key_name`` is a z.ai subscription key — the
    canonical names (``ours``/``friend``) OR the legacy ``zai_*`` aliases that
    the spec's ``provider LIKE 'zai%'`` denotes (see src/provider_names.py).

    Returns ``(call_count, sum_total_tokens, min_ts)``. ``min_ts`` is the
    earliest record in the window (used to measure the real data span); it is
    ``0.0`` when there is no matching data. Never raises — on any DB error
    returns ``(0, 0.0, 0.0)`` so callers fall through to the seed.
    """
    try:
        conn = sqlite3.connect(db_path, timeout=2)
        try:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(total_tokens), 0), "
                "COALESCE(MIN(ts), 0) "
                "FROM api_calls "
                "WHERE (key_name IN ('ours', 'friend') OR key_name LIKE 'zai%') "
                "AND ts > ?",
                (since_ts,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        _log.debug("real_price_tracker: zai window query failed", exc_info=True)
        return (0, 0.0, 0.0)
    if not row:
        return (0, 0.0, 0.0)
    return (int(row[0] or 0), float(row[1] or 0.0), float(row[2] or 0.0))


def get_zai_amortized_rate(
    provider: str = "ours",
    *,
    db_path: str | None = None,
    _now: float | None = None,
) -> float:
    """Amortized $/M for the z.ai flat-rate subscription over trailing 365 days.

    The z.ai keys (``ours``/``friend``) are a fixed subscription, so their real
    per-token cost is the annual budget spread over trailing-365d token volume::

        zai_rate = ZAI_ANNUAL_BUDGET / (SUM(total_tokens for z.ai keys) / 1e6)

    The ``friend`` key carries a ``ZAI_FRIEND_PREMIUM`` (1.21x) surcharge so the
    optimizer prefers the primary ``ours`` key. When fewer than
    ``ZAI_MIN_DATA_DAYS`` (30) days of data exist the function returns the
    ``ZAI_SEED_RATE`` ($0.014/M, x1.21 for friend) rather than a noisy number.

    Results are cached for ``ZAI_CACHE_TTL_SECONDS`` (24 h) — the rate is
    recomputed daily, not per request (:func:`clear_cache` drops it). This is
    the canonical base rate for the z.ai providers; the cost_usd-based
    :func:`get_trailing_rate` cannot price them (their marginal cost is ~$0).

    Thread-safe. Never raises; on any error it degrades to the seed rate.

    Parameters
    ----------
    provider
        ``"ours"`` (default) or ``"friend"``. Any other value is treated as
        ``"ours"``.
    db_path
        Override the DB path (tests pass a temp DB).
    _now
        Injected clock for deterministic tests.

    Returns
    -------
    float
        $/M as a non-negative float. Always a float (the seed on cold start or
        error).
    """
    now = _now if _now is not None else time.time()
    db = _resolve_db(db_path)
    friend = provider == "friend"
    premium = ZAI_FRIEND_PREMIUM if friend else 1.0

    # Daily cache (read under lock; cheap).
    key = (provider, db)
    with _cache_lock:
        cached = _zai_cache.get(key)
    if cached is not None:
        value, computed_at = cached
        if (now - computed_at) < ZAI_CACHE_TTL_SECONDS:
            return value

    # Compute the base rate from trailing-365d z.ai token volume.
    since = now - ZAI_WINDOW_HOURS * 3600.0
    _count, sum_tokens, min_ts = _query_zai_window(db, since)

    # Data span = age of the oldest matching record. <30d (or no tokens) → seed.
    data_days = (now - min_ts) / 86400.0 if min_ts > 0 else 0.0
    if data_days < ZAI_MIN_DATA_DAYS or sum_tokens <= 0:
        rate = ZAI_SEED_RATE * premium
    else:
        base = ZAI_ANNUAL_BUDGET / (sum_tokens / 1e6)
        if base != base or base < 0:  # NaN / nonsensical guard → seed
            base = ZAI_SEED_RATE
        rate = base * premium

    with _cache_lock:
        _zai_cache[key] = (rate, now)
    return rate


def get_rate_with_fallback(
    provider: str,
    model: str | None = None,
    *,
    db_path: str | None = None,
    _now: float | None = None,
) -> float:
    """Real rate with a graceful fallback chain. Always returns a float.

    Resolution order:

    1. :func:`get_real_rate` (the measured rate from ``api_calls.cost_usd``).
    2. For ``ollama_cloud`` only: the Ollama billing API
       (:func:`src.ollama_extra_usage.fetch_ollama_usage`).
    3. :data:`LAST_RESORT_RATES` (clearly-marked estimates).
    4. :data:`UNKNOWN_PROVIDER_FALLBACK` (a conservative rate for a provider with
       no entry anywhere — logged at WARNING).

    Every non-measured result is logged so operators can see where real data is
    missing. Never raises.
    """
    # 1. Real, measured data.
    rate = get_real_rate(provider, model, db_path=db_path, _now=_now)
    if rate is not None:
        return rate

    # 2. Ollama billing API (ollama_cloud only — it has a real per-model cost
    #    endpoint even when our local cost_usd window is empty).
    if provider == "ollama_cloud":
        api_rate = _ollama_api_rate(model)
        if api_rate is not None:
            _log.info(
                "real_price_tracker: ollama_cloud%s using Ollama billing API "
                "fallback $%.6g/M (no local cost_usd data yet)",
                f"/{model}" if model else "",
                api_rate,
            )
            return api_rate

    # 3. Last-resort hardcoded estimates.
    estimate = LAST_RESORT_RATES.get(provider)
    if estimate is not None:
        _log.warning(
            "real_price_tracker: no real data for %s/%s — using last-resort "
            "ESTIMATE $%.6g/M (this is not a measurement)",
            provider,
            model,
            estimate,
        )
        return estimate

    # 4. Unknown provider — return a conservative rate so the optimizer never
    #    routes to an unknown "cheap" provider.
    _log.warning(
        "real_price_tracker: unknown provider %r (model=%s) — returning "
        "conservative fallback $%.6g/M",
        provider,
        model,
        UNKNOWN_PROVIDER_FALLBACK,
    )
    return UNKNOWN_PROVIDER_FALLBACK


def detect_price_change(
    provider: str,
    model: str | None = None,
    *,
    db_path: str | None = None,
    _now: float | None = None,
) -> bool:
    """Return True when the recent rate deviates sharply from the baseline rate.

    Compares the 24h rate against the 7d rate for the same (provider, model).
    If the relative deviation exceeds :data:`CHANGE_THRESHOLD` (50%), a price
    change is flagged — useful for alerts and dashboard surfacing.

    Returns ``False`` (no alert) whenever either rate cannot be computed
    (insufficient calls in a window, or a zero baseline that would make the
    ratio meaningless). This is the non-noisy default: we only alert when we have
    enough data on both sides to be confident something moved.

    Never raises. Both underlying rates come from the same cached
    :func:`get_real_rate` (different ``window_hours`` ⇒ different cache keys).
    """
    now = _now if _now is not None else time.time()
    recent = get_real_rate(
        provider, model, CHANGE_RECENT_HOURS, db_path=db_path, _now=now
    )
    baseline = get_real_rate(
        provider, model, CHANGE_BASELINE_HOURS, db_path=db_path, _now=now
    )
    if recent is None or baseline is None or baseline <= 0:
        return False
    deviation = abs(recent - baseline) / baseline
    return deviation > CHANGE_THRESHOLD
