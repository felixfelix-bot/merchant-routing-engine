#!/usr/bin/env python3
"""CG-13: Kalman-composed cost outlier detection for the escalation script.

Replaces the static ``$2/h`` fixed threshold in ``cost-escalation-check.py`` with
an **adaptive EWMA** (exponentially weighted moving average) tracker that
computes a dynamic outlier threshold from the 7-day baseline of hourly paid
spend. The threshold adapts to baseline shifts — e.g. when a cheaper provider
goes live, the normal $/h drops, and the old fixed threshold becomes stale; the
EWMA tracks the new normal automatically.

**Two signals are composed:**

1. **EWMA outlier** — the current hour's paid $ spend is compared against
   ``threshold = max(3 × EWMA, mean + 2 × std)`` over the 7-day baseline.
   When ``actual > threshold`` → the hour is flagged as an outlier with a
   ratio (``"16x normal"``).

2. **Kalman composition** — the existing Kalman filter (``kalman_samples``
   table) predicts the token burn rate (``burn_rate_tph``). Composing this
   with the effective price gives the *expected* $/h cost:

       expected = burn_rate_tph × effective_price / 1M

   Comparing ``expected`` vs ``actual`` reveals the *cause*:
   - ``actual >> expected`` → **routing inefficiency** (expensive provider
     picked when a cheaper one exists, or the free quota was available but
     the proxy failed over to paid).
   - ``actual ≈ expected`` but both high → **quota exhaustion** (structural
     issue: free quota is depleted, paid failover is the only option).

**Cold start:** when fewer than ``MIN_BASELINE_HOURS`` of history exist, the
EWMA and baseline stats are unreliable. The module falls back to a fixed
``COLD_START_THRESHOLD`` ($2/h, matching the previous static threshold) until
enough data accumulates.

Usage::

    from src.cost_outlier_detector import detect_cost_outliers
    alerts = detect_cost_outliers()
    for a in alerts:
        print(a["message"])

The pure core (``compute_ewma``, ``compute_baseline``, ``detect_outlier``,
``classify_discrepancy``) is testable without a live DB. The I/O layer
(``fetch_hourly_spends``, ``detect_cost_outliers``) reads ``zai_usage.db``
strictly read-only.
"""
from __future__ import annotations

import os
import sqlite3
import time
from urllib.request import pathname2url

__all__ = [
    "DEFAULT_DB_PATH",
    "EWMA_ALPHA",
    "COLD_START_THRESHOLD",
    "MIN_BASELINE_HOURS",
    "LOOKBACK_HOURS",
    "DEFAULT_PAID_PRICE_PER_M",
    "compute_ewma",
    "compute_baseline",
    "detect_outlier",
    "compute_expected_cost",
    "classify_discrepancy",
    "build_provider_breakdown",
    "fetch_hourly_spends",
    "fetch_provider_breakdown",
    "fetch_kalman_state",
    "fetch_effective_price",
    "detect_cost_outliers",
]

# ── constants ────────────────────────────────────────────────────────────────

#: Production usage DB — READ-ONLY (never written by this module).
DEFAULT_DB_PATH: str = os.path.expanduser("~/.hermes/bot/zai_usage.db")

#: EWMA smoothing factor — alpha=0.3 gives ~5h memory (1/alpha ≈ 3.3h
#: half-life). Higher alpha → more responsive to recent changes.
EWMA_ALPHA: float = 0.3

#: Cold-start fixed threshold ($/h). Used when < MIN_BASELINE_HOURS of
#: history exist. Matches the previous static threshold in the escalation
#: script so the transition is seamless.
COLD_START_THRESHOLD: float = 2.0

#: Minimum hours of baseline data before adaptive thresholds kick in.
#: Below this, we don't have enough data for mean+std to be reliable.
MIN_BASELINE_HOURS: int = 24

#: Lookback window for baseline computation (7 days).
LOOKBACK_HOURS: int = 168

#: Fallback paid price per million tokens (OpenRouter measured 2026-08-22).
DEFAULT_PAID_PRICE_PER_M: float = 0.47


# ── pure core ────────────────────────────────────────────────────────────────


def compute_ewma(series: list[float], alpha: float = EWMA_ALPHA) -> float:
    """Exponentially weighted moving average of a numeric series.

    Weights recent observations more heavily than old ones. With
    ``alpha=0.3``, the half-life is ~3.3 samples (hours), giving ~5h memory.

    Parameters
    ----------
    series : list[float]
        Chronologically ordered values (oldest first, newest last).
    alpha : float
        Smoothing factor in (0, 1]. Higher → more responsive.

    Returns
    -------
    float
        The EWMA of the series. Empty → 0.0.
    """
    if not series:
        return 0.0
    ewma = series[0]
    for val in series[1:]:
        ewma = alpha * val + (1 - alpha) * ewma
    return ewma


def compute_baseline(hourly_spends: list[float]) -> dict:
    """Compute mean, std, and count from a series of hourly $ spends.

    Uses sample standard deviation (n-1 denominator) to match the
    unbiased estimator for small samples.

    Parameters
    ----------
    hourly_spends : list[float]
        Chronologically ordered hourly $ spend values.

    Returns
    -------
    dict
        ``{"mean": float, "std": float, "n": int}``. Empty → zeros.
    """
    n = len(hourly_spends)
    if n == 0:
        return {"mean": 0.0, "std": 0.0, "n": 0}
    mean = sum(hourly_spends) / n
    if n < 2:
        return {"mean": mean, "std": 0.0, "n": n}
    var = sum((x - mean) ** 2 for x in hourly_spends) / (n - 1)
    return {"mean": mean, "std": var ** 0.5, "n": n}


def detect_outlier(
    actual: float,
    ewma: float,
    baseline: dict,
    cold_start_threshold: float = COLD_START_THRESHOLD,
    min_baseline_hours: int = MIN_BASELINE_HOURS,
) -> dict:
    """Detect whether the current hour's spend is an outlier.

    The threshold adapts to the baseline:

    - **Sufficient history** (``baseline.n >= min_baseline_hours``):
      ``threshold = max(3 × EWMA, mean + 2 × std)``
      The ``3 × EWMA`` catches sudden spikes; ``mean + 2 × std`` catches
      statistically unlikely values relative to the distribution.
    - **Cold start** (insufficient history):
      ``threshold = cold_start_threshold`` (fixed $2/h).

    Parameters
    ----------
    actual : float
        Current hour's $ spend.
    ewma : float
        EWMA of historical hourly spends (excluding the current hour).
    baseline : dict
        Output of :func:`compute_baseline` over the lookback window.
    cold_start_threshold : float
        Fixed threshold for cold start ($/h).
    min_baseline_hours : int
        Minimum baseline data points before adaptive thresholds kick in.

    Returns
    -------
    dict
        ``{"is_outlier": bool, "ratio": float, "threshold": float,
        "explanation": str}``
    """
    n = baseline.get("n", 0)
    if n < min_baseline_hours:
        threshold = cold_start_threshold
        reason = f"cold start (n={n} < {min_baseline_hours}), fixed ${cold_start_threshold}/h"
    else:
        mean = baseline["mean"]
        std = baseline["std"]
        threshold_3x = 3 * ewma
        threshold_stat = mean + 2 * std
        threshold = max(threshold_3x, threshold_stat)
        if threshold_3x >= threshold_stat:
            reason = f"3× EWMA (${threshold_3x:.2f}) > mean+2σ (${threshold_stat:.2f})"
        else:
            reason = f"mean+2σ (${threshold_stat:.2f}) > 3× EWMA (${threshold_3x:.2f})"

    is_outlier = actual > threshold
    # Ratio: actual vs the EWMA "normal" — how many times normal is this?
    # Guard against division by zero.
    safe_ewma = max(ewma, 0.01)
    ratio = actual / safe_ewma

    if is_outlier:
        explanation = (
            f"${actual:.2f}/h exceeds threshold ${threshold:.2f}/h "
            f"({reason}) — {ratio:.1f}x normal"
        )
    else:
        explanation = (
            f"${actual:.2f}/h within normal range "
            f"(threshold ${threshold:.2f}/h, {ratio:.1f}x normal)"
        )

    return {
        "is_outlier": is_outlier,
        "ratio": round(ratio, 2),
        "threshold": round(threshold, 4),
        "explanation": explanation,
    }


def compute_expected_cost(burn_rate_tph: float, price_per_m: float) -> float:
    """Expected $/h cost from Kalman token burn rate × price.

    ``expected = burn_rate_tph × price_per_m / 1_000_000``

    When quota is available, the effective price is the subscription rate
    (very low), so expected cost is near zero. When quota is exhausted, the
    effective price is the paid rate, and the expected cost reflects the
    true $ burn. The *discrepancy* between expected and actual $ spend
    reveals the root cause.

    Parameters
    ----------
    burn_rate_tph : float
        Kalman-predicted token burn rate (tokens/hour) from
        ``kalman_samples.burn_rate_tph``.
    price_per_m : float
        Effective price per million tokens ($/M).

    Returns
    -------
    float
        Expected $/h cost.
    """
    return burn_rate_tph * price_per_m / 1_000_000


def classify_discrepancy(
    actual: float,
    expected: float,
    ewma: float,
    baseline: dict,
) -> dict:
    """Classify the discrepancy between actual and expected $/h cost.

    Categories:

    - ``"routing_inefficiency"``: actual >> expected. The proxy is spending
      more $ than the Kalman token burn predicts. This means either an
      expensive provider was picked when a cheaper one exists, or the free
      quota was available but the proxy failed over to paid.
    - ``"quota_exhaustion"``: actual ≈ expected and both are high
      (above the outlier threshold). The paid spend matches what the Kalman
      predicts at the paid rate — the free quota is depleted, and paid
      failover is the only option (structural issue).
    - ``"normal"``: actual ≈ expected and both are within normal range.

    Parameters
    ----------
    actual : float
        Current hour's actual $ spend.
    expected : float
        Expected $ spend from Kalman composition.
    ewma : float
        EWMA of historical hourly spends.
    baseline : dict
        Output of :func:`compute_baseline`.

    Returns
    -------
    dict
        ``{"category": str, "explanation": str, "actual": float,
        "expected": float, "discrepancy_ratio": float}``
    """
    outlier = detect_outlier(actual, ewma, baseline)
    threshold = outlier["threshold"]

    # Discrepancy ratio: how many times expected is the actual?
    safe_expected = max(expected, 0.0001)
    disc_ratio = actual / safe_expected

    # Case 1: expected ~ 0 but actual > 0 → free path expected, paid happened
    if expected <= 0.0001 and actual > 0.01:
        return {
            "category": "routing_inefficiency",
            "explanation": (
                f"actual ${actual:.2f}/h vs expected ~$0/h — "
                f"Kalman predicts no token burn but $ are being spent "
                f"(paid failover active while quota expected free)"
            ),
            "actual": round(actual, 4),
            "expected": round(expected, 4),
            "discrepancy_ratio": round(disc_ratio, 1),
        }

    # Case 2: actual >> expected (5x or more) → routing inefficiency
    if disc_ratio > 5.0:
        return {
            "category": "routing_inefficiency",
            "explanation": (
                f"actual ${actual:.2f}/h is {disc_ratio:.0f}x the expected "
                f"${expected:.4f}/h (Kalman burn {0:.0f} tok/h × "
                f"${0:.2f}/M) — expensive provider picked when cheaper exists"
            ),
            "actual": round(actual, 4),
            "expected": round(expected, 4),
            "discrepancy_ratio": round(disc_ratio, 1),
        }

    # Case 3: actual ≈ expected (within 50%) and both above threshold
    # → quota exhaustion (structural, not routing)
    within_50pct = abs(actual - expected) / safe_expected < 0.5
    if within_50pct and actual > threshold:
        return {
            "category": "quota_exhaustion",
            "explanation": (
                f"actual ${actual:.2f}/h ≈ expected ${expected:.2f}/h "
                f"(within 50%) and both above threshold ${threshold:.2f}/h — "
                f"quota exhausted, paid failover is structural"
            ),
            "actual": round(actual, 4),
            "expected": round(expected, 4),
            "discrepancy_ratio": round(disc_ratio, 1),
        }

    # Case 4: normal
    return {
        "category": "normal",
        "explanation": (
            f"actual ${actual:.2f}/h ≈ expected ${expected:.4f}/h, "
            f"within normal range"
        ),
        "actual": round(actual, 4),
        "expected": round(expected, 4),
        "discrepancy_ratio": round(disc_ratio, 1),
    }


def build_provider_breakdown(rows: list[dict]) -> list[dict]:
    """Sort provider rows by spend descending and add percentage of total.

    Parameters
    ----------
    rows : list[dict]
        Each dict has ``key_name``, ``spend``, ``calls``.

    Returns
    -------
    list[dict]
        Sorted by spend desc, each with ``key_name``, ``spend``, ``calls``,
        ``pct_of_total``.
    """
    if not rows:
        return []
    total = sum(r["spend"] for r in rows)
    if total <= 0:
        total = 1.0  # avoid div by zero
    breakdown = []
    for r in sorted(rows, key=lambda x: x["spend"], reverse=True):
        breakdown.append({
            "key_name": r["key_name"],
            "spend": round(r["spend"], 4),
            "calls": r["calls"],
            "pct_of_total": round(r["spend"] / total * 100, 1),
        })
    return breakdown


# ── I/O layer ────────────────────────────────────────────────────────────────


def _ro(db_path: str) -> sqlite3.Connection:
    """Open db_path strictly read-only (mode=ro URI)."""
    uri = "file:" + pathname2url(os.path.abspath(db_path)) + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=10)


def fetch_hourly_spends(
    db_path: str = DEFAULT_DB_PATH,
    lookback_hours: int = LOOKBACK_HOURS,
) -> list[dict]:
    """Fetch per-hour paid spend aggregation from api_calls.

    Each entry represents one hour's total paid $ spend and call count.

    Parameters
    ----------
    db_path : str
        Path to zai_usage.db (read-only).
    lookback_hours : int
        How far back to look (default 168 = 7 days).

    Returns
    -------
    list[dict]
        Each dict: ``{"hour": str, "spend": float, "calls": int}``.
        Chronologically ordered (oldest first). Empty on DB errors.
    """
    try:
        c = _ro(db_path)
        c.row_factory = sqlite3.Row
        cutoff = time.time() - lookback_hours * 3600
        rows = c.execute(
            """SELECT strftime('%Y-%m-%d %H', datetime(ts, 'unixepoch')) as hour,
                      SUM(cost_usd) as spend, COUNT(*) as calls
               FROM api_calls
               WHERE cost_usd > 0 AND ts > ?
               GROUP BY hour
               ORDER BY hour""",
            (cutoff,),
        ).fetchall()
        c.close()
        return [
            {"hour": r["hour"], "spend": float(r["spend"] or 0), "calls": r["calls"]}
            for r in rows
        ]
    except Exception:
        return []


def fetch_provider_breakdown(
    db_path: str = DEFAULT_DB_PATH,
    since_ts: float = 0,
) -> list[dict]:
    """Fetch per-provider paid spend breakdown from api_calls.

    Parameters
    ----------
    db_path : str
        Path to zai_usage.db (read-only).
    since_ts : float
        Unix timestamp — only include calls after this time.

    Returns
    -------
    list[dict]
        Each dict: ``{"key_name": str, "spend": float, "calls": int}``.
        Sorted by spend descending. Empty on DB errors.
    """
    try:
        c = _ro(db_path)
        c.row_factory = sqlite3.Row
        rows = c.execute(
            """SELECT key_name, SUM(cost_usd) as spend, COUNT(*) as calls
               FROM api_calls
               WHERE cost_usd > 0 AND ts > ?
               GROUP BY key_name
               ORDER BY spend DESC""",
            (since_ts,),
        ).fetchall()
        c.close()
        return [
            {"key_name": r["key_name"], "spend": float(r["spend"] or 0), "calls": r["calls"]}
            for r in rows
        ]
    except Exception:
        return []


def fetch_kalman_state(db_path: str = DEFAULT_DB_PATH) -> dict:
    """Fetch the latest Kalman sample (burn rate, uncertainty, exhaustion).

    Returns
    -------
    dict
        ``{"burn_rate_tph": float, "uncertainty": float,
        "exhausts_in_hours": float | None}``. Zeros/None when no data.
    """
    try:
        c = _ro(db_path)
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT burn_rate_tph, uncertainty, exhausts_in_hours "
            "FROM kalman_samples ORDER BY ts DESC LIMIT 1",
        ).fetchone()
        c.close()
        if row:
            return {
                "burn_rate_tph": float(row["burn_rate_tph"] or 0),
                "uncertainty": float(row["uncertainty"] or 0),
                "exhausts_in_hours": (
                    float(row["exhausts_in_hours"])
                    if row["exhausts_in_hours"] is not None
                    else None
                ),
            }
    except Exception:
        pass
    return {
        "burn_rate_tph": 0.0,
        "uncertainty": 0.0,
        "exhausts_in_hours": None,
    }


def fetch_effective_price(db_path: str = DEFAULT_DB_PATH) -> float:
    """Fetch the cheapest measured paid price from price_observations.

    Falls back to ``DEFAULT_PAID_PRICE_PER_M`` when no data.

    Returns
    -------
    float
        Effective price per million tokens ($/M).
    """
    try:
        c = _ro(db_path)
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT rate_per_m FROM price_observations "
            "WHERE provider NOT IN ('friend', 'ours') "
            "ORDER BY ts DESC LIMIT 1",
        ).fetchone()
        c.close()
        if row and row["rate_per_m"]:
            return float(row["rate_per_m"])
    except Exception:
        pass
    return DEFAULT_PAID_PRICE_PER_M


# ── main entry point ─────────────────────────────────────────────────────────


def detect_cost_outliers(
    db_path: str = DEFAULT_DB_PATH,
    state_path: str | None = None,
) -> list[dict]:
    """Detect cost outliers using EWMA + Kalman composition.

    This is the main entry point called by the escalation script. It reads
    ``zai_usage.db`` (read-only), computes the EWMA baseline, detects
    outliers in the current hour's spend, and composes the result with the
    Kalman token prediction to classify the discrepancy.

    Parameters
    ----------
    db_path : str
        Path to zai_usage.db (read-only).
    state_path : str | None
        Unused (kept for API compatibility with urgency_cost_estimator).
        The Kalman state is read from the DB's kalman_samples table.

    Returns
    -------
    list[dict]
        List of alert dicts. Each alert has at minimum:
        ``{"alert_type": str, "message": str, "actual": float}``.
        Empty list when no outliers detected or on DB errors.
    """
    alerts: list[dict] = []

    # 1. Fetch hourly spends for the lookback window
    hourly = fetch_hourly_spends(db_path, lookback_hours=LOOKBACK_HOURS)
    if not hourly:
        return alerts  # no data → nothing to detect

    # 2. Separate current hour from historical baseline
    # The last entry is the most recent complete hour
    current = hourly[-1]
    historical_spends = [h["spend"] for h in hourly[:-1]] if len(hourly) > 1 else []

    # EWMA from historical data (excluding current hour)
    ewma = compute_ewma(historical_spends, alpha=EWMA_ALPHA) if historical_spends else 0.0

    # Baseline stats from ALL hourly spends (including current — for 7d baseline)
    all_spends = [h["spend"] for h in hourly]
    baseline = compute_baseline(all_spends)

    actual = current["spend"]
    calls = current["calls"]

    # 3. Detect outlier
    outlier = detect_outlier(actual, ewma, baseline)

    if outlier["is_outlier"]:
        # 4. Fetch provider breakdown for context (last 1h)
        now = time.time()
        providers = fetch_provider_breakdown(db_path, since_ts=now - 3600)
        provider_summary = build_provider_breakdown(providers)

        # Format provider context string
        if provider_summary:
            top = provider_summary[0]
            prov_str = f"{top['calls']} calls to {top['key_name']} ({top['pct_of_total']:.0f}% of paid spend)"
            if len(provider_summary) > 1:
                others = ", ".join(
                    f"{p['key_name']} ({p['pct_of_total']:.0f}%)"
                    for p in provider_summary[1:3]
                )
                prov_str += f" + {others}"
        else:
            prov_str = f"{calls} paid calls"

        message = (
            f"💸 SPEND OUTLIER: ${actual:.2f}/h, {outlier['ratio']:.1f}x normal "
            f"(threshold ${outlier['threshold']:.2f}/h) — {prov_str}"
        )

        alerts.append({
            "alert_type": "spend_outlier",
            "actual": round(actual, 4),
            "ewma": round(ewma, 4),
            "ratio": outlier["ratio"],
            "threshold": outlier["threshold"],
            "calls": calls,
            "provider_breakdown": provider_summary,
            "message": message,
        })

    # 5. Kalman composition — compare expected vs actual
    kalman = fetch_kalman_state(db_path)
    price = fetch_effective_price(db_path)

    # Guard: skip Kalman composition when burn_rate_tph == 0.0.
    # A zero burn rate means the Kalman filter has no data (stale/never
    # collected). Computing expected = 0 × price = $0, then classifying
    # actual_spend / $0.0001 = 18x/61x/79x/16600x is pure noise —
    # these false "routing_inefficiency" alerts have no diagnostic value.
    if kalman["burn_rate_tph"] > 0:
        expected = compute_expected_cost(kalman["burn_rate_tph"], price)
        disc = classify_discrepancy(actual, expected, ewma, baseline)

        if disc["category"] != "normal":
            message = (
                f"🔍 KALMAN COMPOSITION: {disc['category'].replace('_', ' ').title()} — "
                f"actual ${actual:.2f}/h vs expected ${expected:.4f}/h "
                f"({disc['discrepancy_ratio']:.0f}x discrepancy; "
                f"Kalman {kalman['burn_rate_tph']:.0f} tok/h × ${price:.2f}/M)"
            )

            alerts.append({
                "alert_type": "kalman_composition",
                "actual": round(actual, 4),
                "expected": round(expected, 4),
                "discrepancy_category": disc["category"],
                "discrepancy_ratio": disc["discrepancy_ratio"],
                "burn_rate_tph": kalman["burn_rate_tph"],
                "effective_price": price,
                "calls": calls,
                "message": message,
            })

    return alerts
