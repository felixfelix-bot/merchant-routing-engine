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

# ── Thread-safe TTL cache ────────────────────────────────────────────────────
# key: (provider, model, window_hours, db_path) → (value, computed_at)
# value is a float rate OR None (a cached "insufficient data" miss). Caching the
# miss too prevents a thundering herd of identical queries against a provider
# that simply has no costed rows yet.
_cache: dict[tuple[str, str | None, float, str], tuple[float | None, float]] = {}
_cache_lock = threading.Lock()


def clear_cache() -> None:
    """Drop every cached rate. Tests call this between assertions; production
    may call it after a forced DB refresh."""
    with _cache_lock:
        _cache.clear()


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
