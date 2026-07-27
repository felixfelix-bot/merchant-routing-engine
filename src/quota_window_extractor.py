"""quota_window_extractor.py — Parse z.ai quota API response into pace_factor inputs.

This module bridges the production proxy's quota_cache structure to the
predictive quota-pacing multiplier (pace_factor) described in ADR-008.

The proxy at ~/.hermes/bot/zai_proxy.py stores quota state as::

    quota_cache[key_name] = (windows_list, timestamp)

where each window dict (from _parse_limit_entry) has::

    {
        "name":         "5-hour" | "weekly" | "monthly" | ...
        "type":         "TOKENS_LIMIT" | "TIME_LIMIT"
        "used_pct":     int (0-100, or 999 for error sentinel)
        "resets_at":    int unix timestamp (0 if unknown/error)
        "window_hours": int (5 for 5h, 168 for weekly, 720 for monthly)
    }

The pace_factor function in pricing_engine.py needs::

    (quota_used, quota_total, time_elapsed_pct, burn_rate, window_duration_hours)

This extractor:
    1. Iterates over all keys and windows in the cache.
    2. Converts used_pct + quota_total → quota_used (absolute tokens).
    3. Computes time_elapsed_pct from resets_at and window_hours.
    4. Returns a list of tuples ready for pace_factor_multi.

Graceful degradation:
    - Missing/malformed windows are silently skipped.
    - Error sentinel windows (used_pct=999, resets_at=0) are skipped.
    - Unknown window names are skipped (only known windows feed pace_factor).
    - elapsed_pct is clamped to [0.0, 1.0].

ADR-008: "The pace_factor multiplier applies the result instantly."
This module is the deterministic arithmetic layer that feeds that multiplier.
"""
from __future__ import annotations

import time
from typing import Sequence

# ── Known window names ────────────────────────────────────────────────────────
# Only these windows are relevant for quota pacing. Unknown window types (e.g.
# error sentinels, "unknown") are skipped.
_KNOWN_WINDOW_NAMES: frozenset[str] = frozenset({
    "5-hour",
    "weekly",
    "monthly",
})

# Default quota total (the proxy hardcodes 2_000_000 for z.ai keys).
_DEFAULT_QUOTA_TOTAL: float = 2_000_000.0

# Error sentinel value used by the proxy (_fetch_quota_windows error path).
_ERROR_SENTINEL_PCT: int = 999


def extract_quota_windows(
    quota_cache: dict[str, tuple[list[dict], float]],
    burn_rate: float,
    quota_total: float | dict[str, float] | Sequence[float] = _DEFAULT_QUOTA_TOTAL,
) -> list[tuple[float, float, float, float, float]]:
    """Extract pace_factor input tuples from the proxy's quota_cache.

    Iterates over every key in *quota_cache*, parses each window dict, and
    returns a flat list of tuples in the format expected by
    :func:`src.pricing_engine.pace_factor_multi`::

        (quota_used, quota_total, time_elapsed_pct, burn_rate, window_duration_hours)

    Args:
        quota_cache: The proxy's ``quota_cache`` dict mapping key names to
            ``(windows_list, timestamp)`` tuples. Each window dict must have
            ``name``, ``used_pct``, ``resets_at``, and ``window_hours``.
        burn_rate: Current burn rate from ConsumptionKalman (tokens/hour).
            Applied uniformly to all windows.
        quota_total: Total token allocation for a quota window. Can be:
            - A single float (same total for all keys).
            - A dict mapping key names to their individual totals.
            - A sequence aligned with the iteration order of quota_cache keys
              (less reliable — prefer dict).
            Defaults to 2,000,000 (the proxy's hardcoded z.ai default).

    Returns:
        List of ``(quota_used, quota_total, time_elapsed_pct, burn_rate,
        window_duration_hours)`` tuples. May be empty if no valid windows
        are found. Malformed/missing/error-sentinel windows are silently
        skipped.

    Examples::

        >>> cache = {"ours": ([{"name": "5-hour", "used_pct": 80,
        ...                     "resets_at": 9999999999, "window_hours": 5}],
        ...                    0.0)}
        >>> result = extract_quota_windows(cache, burn_rate=200.0,
        ...                                quota_total=1_000_000)
        >>> len(result)
        1
        >>> result[0][1]  # quota_total
        1000000.0
        >>> result[0][3]  # burn_rate
        200.0
        >>> result[0][4]  # window_duration_hours
        5.0
    """
    result: list[tuple[float, float, float, float, float]] = []

    if not quota_cache:
        return result

    now = time.time()

    for key_name, cache_entry in quota_cache.items():
        # Unpack (windows_list, timestamp) — timestamp is unused here.
        if not isinstance(cache_entry, (tuple, list)) or len(cache_entry) < 1:
            continue
        windows = cache_entry[0]
        if not isinstance(windows, list):
            continue

        total_for_key = _resolve_quota_total(key_name, quota_total)

        for window in windows:
            tuple_result = _parse_single_window(
                window, total_for_key, burn_rate, now
            )
            if tuple_result is not None:
                result.append(tuple_result)

    return result


def _resolve_quota_total(
    key_name: str,
    quota_total: float | dict[str, float] | Sequence[float],
) -> float:
    """Resolve the quota_total for a specific key from various input types."""
    if isinstance(quota_total, dict):
        return float(quota_total.get(key_name, _DEFAULT_QUOTA_TOTAL))
    if isinstance(quota_total, (int, float)):
        return float(quota_total)
    if isinstance(quota_total, Sequence):
        # Best-effort: if it's a sequence (not str), try to use it as-is.
        # This is unreliable (depends on dict iteration order) but provided
        # for completeness. Prefer dict or scalar.
        try:
            return float(quota_total[0])  # type: ignore[index]
        except (IndexError, TypeError):
            return _DEFAULT_QUOTA_TOTAL
    return _DEFAULT_QUOTA_TOTAL


def _parse_single_window(
    window: dict,
    quota_total: float,
    burn_rate: float,
    now: float,
) -> tuple[float, float, float, float, float] | None:
    """Parse a single window dict into a pace_factor tuple.

    Returns ``None`` if the window should be skipped (missing fields,
    unknown name, error sentinel, zero window_hours, or resets_at==0).
    """
    if not isinstance(window, dict):
        return None

    # ── Required fields ───────────────────────────────────────────────────
    name = window.get("name")
    if name is None or name not in _KNOWN_WINDOW_NAMES:
        return None

    used_pct = window.get("used_pct")
    if used_pct is None:
        return None

    resets_at = window.get("resets_at")
    if resets_at is None or resets_at == 0:
        return None

    window_hours = window.get("window_hours")
    if window_hours is None or window_hours <= 0:
        return None

    # ── Skip error sentinel ──────────────────────────────────────────────
    try:
        used_pct = int(used_pct)
    except (TypeError, ValueError):
        return None

    if used_pct == _ERROR_SENTINEL_PCT:
        return None

    # ── Compute quota_used from percentage ────────────────────────────────
    # The proxy only stores used_pct, not raw token counts.
    # quota_used = quota_total * (used_pct / 100)
    # Clamp used_pct to [0, 100] — values >100 (over-quota) map to 100%
    # for absolute token accounting (the percentage itself signals scarcity).
    clamped_pct = max(0, min(used_pct, 100))
    quota_used = quota_total * (clamped_pct / 100.0)

    # ── Compute time_elapsed_pct ──────────────────────────────────────────
    # window_start = resets_at - window_hours * 3600
    # elapsed_pct = (now - window_start) / (window_hours * 3600)
    #            = (now - resets_at + window_hours * 3600) / (window_hours * 3600)
    #            = 1 - (resets_at - now) / (window_hours * 3600)
    window_seconds = float(window_hours) * 3600.0
    window_start = float(resets_at) - window_seconds
    elapsed_raw = (now - window_start) / window_seconds
    elapsed_pct = max(0.0, min(elapsed_raw, 1.0))

    return (
        quota_used,
        quota_total,
        elapsed_pct,
        float(burn_rate),
        float(window_hours),
    )