"""Ollama Cloud extra-usage detection.

When included limits are exhausted, Ollama silently switches to pay-per-token
with no 429 signal. This module detects that regime switch by:

1. Comparing usage fractions from ollama.com/api/usage against 1.0 (100%)
   - data.limits.session.usage  (0-1 fraction, 5h session window)
   - data.limits.weekly.usage   (0-1 fraction, 7d weekly window)

2. Tracking cumulative tokens from the api_calls table (key_name='ollama_cloud')
   in 5h session and 7d weekly windows as a secondary confirmation.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

# Window durations in seconds
SESSION_WINDOW_S = 5 * 3600      # 5 hours
WEEKLY_WINDOW_S = 7 * 86400      # 7 days

# ── Ollama API fetch with caching ─────────────────────────────────────────────

_OLLAMA_API_URL = "https://ollama.com/api/usage"
_OLLAMA_TIMEOUT_S = 2            # 2s timeout — mirrors cvm-server.ts AbortSignal.timeout(2000)
_OLLAMA_CACHE_TTL_S = 30         # 30s cache — mirrors OLLAMA_CACHE_TTL = 30_000

# Thread-safe cache state (mirrors cvm-server.ts ollamaApiCache + ollamaFetching)
_ollama_cache_lock = threading.Lock()
_ollama_cache: dict = {"at": 0.0, "data": None}
_ollama_fetching = False


def fetch_ollama_usage(
    api_key: Optional[str] = None,
    now: Optional[float] = None,
) -> Optional[dict]:
    """Fetch usage data from ollama.com/api/usage with a 30s cache.

    Mirrors the cvm-server.ts pattern:
    - 2s timeout on the HTTP request
    - 30s cache with thread-safe lock (thundering-herd guard)
    - Cache timestamp updated on BOTH success and failure (prevents stampede
      when API is down — without this, every snapshot tick would re-fetch)
    - Returns parsed dict on success, None on failure

    Args:
        api_key: Ollama Cloud API key. If None, reads OLLAMA_CLOUD_API_KEY env var.
        now: Override current time for testing.

    Returns:
        Parsed JSON dict from the API, or None if the fetch failed / timed out.
    """
    global _ollama_fetching

    if now is None:
        now = time.time()

    if api_key is None:
        api_key = os.environ.get("OLLAMA_CLOUD_API_KEY", "")

    # Check cache under lock — fast path, no HTTP if cache is fresh
    with _ollama_cache_lock:
        cached = _ollama_cache
        # TTL applies to both success and failure — the timestamp is updated
        # on both paths (mirrors cvm-server.ts stampede prevention). Even when
        # data is None (failed fetch), we don't re-fetch within the TTL window.
        if cached["at"] > 0 and (now - cached["at"]) < _OLLAMA_CACHE_TTL_S:
            return cached["data"]
        if _ollama_fetching:
            # Another thread is already fetching — return stale cache or None
            return cached["data"]
        _ollama_fetching = True

    try:
        req = urllib.request.Request(
            _OLLAMA_API_URL,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(req, timeout=_OLLAMA_TIMEOUT_S) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                with _ollama_cache_lock:
                    _ollama_cache["at"] = time.time()
                    _ollama_cache["data"] = data
                return data
            else:
                # Non-OK — still update cache timestamp for TTL backoff
                with _ollama_cache_lock:
                    _ollama_cache["at"] = time.time()
                return None
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError, Exception):
        # Network error, timeout, parse error — update cache timestamp to
        # prevent stampede (mirrors cvm-server.ts catch block)
        with _ollama_cache_lock:
            _ollama_cache["at"] = time.time()
        return None
    finally:
        with _ollama_cache_lock:
            _ollama_fetching = False


def _reset_ollama_cache():
    """Reset cache state — for testing only."""
    global _ollama_fetching
    with _ollama_cache_lock:
        _ollama_cache["at"] = 0.0
        _ollama_cache["data"] = None
        _ollama_fetching = False


@dataclass
class ExtraUsageStatus:
    """Result of extra-usage detection."""
    session_usage: float       # 0-1 fraction from API
    weekly_usage: float        # 0-1 fraction from API
    session_tokens: int        # cumulative tokens in 5h window from api_calls
    weekly_tokens: int         # cumulative tokens in 7d window from api_calls
    extra_usage: bool          # True when either usage >= 1.0
    reason: str = field(default="")

    def to_dict(self) -> dict:
        return {
            "session_usage": round(self.session_usage, 4),
            "weekly_usage": round(self.weekly_usage, 4),
            "session_tokens": self.session_tokens,
            "weekly_tokens": self.weekly_tokens,
            "extra_usage": self.extra_usage,
            "reason": self.reason,
        }


def detect_extra_usage(
    session_usage: float,
    weekly_usage: float,
) -> bool:
    """Return True if either usage fraction >= 1.0 (100%).

    This is the core regime-switch detection. Ollama does not send a 429
    when included limits are exhausted — it silently switches to
    pay-per-token. Any usage fraction at or above 1.0 means the
    included quota is depleted.
    """
    return session_usage >= 1.0 or weekly_usage >= 1.0


def compute_cumulative_tokens(
    db_path: str,
    key_name: str = "ollama_cloud",
    window_s: int = SESSION_WINDOW_S,
    now: Optional[float] = None,
) -> int:
    """Sum total_tokens from api_calls for the given key within a time window.

    Uses the existing api_calls table — no schema changes needed.
    """
    if now is None:
        now = time.time()
    since = now - window_s
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "SELECT COALESCE(SUM(total_tokens), 0) AS v "
            "FROM api_calls WHERE key_name = ? AND ts > ?",
            (key_name, since),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def get_extra_usage_status(
    ollama_api_response: dict,
    db_path: Optional[str] = None,
    now: Optional[float] = None,
) -> ExtraUsageStatus:
    """Build full extra-usage status from API response + api_calls table.

    Args:
        ollama_api_response: Parsed JSON from ollama.com/api/usage.
            Expected shape: {"limits": {"session": {"usage": 0.06}, "weekly": {"usage": 0.11}}}
        db_path: Path to zai_usage.db for token counting. Optional —
            if None, session_tokens and weekly_tokens will be 0.
        now: Override current time for testing.

    Returns:
        ExtraUsageStatus with all fields populated.
    """
    if now is None:
        now = time.time()

    limits = ollama_api_response.get("limits", {}) or {}
    session_usage = float(limits.get("session", {}).get("usage", 0) or 0)
    weekly_usage = float(limits.get("weekly", {}).get("usage", 0) or 0)

    session_tokens = 0
    weekly_tokens = 0
    if db_path:
        session_tokens = compute_cumulative_tokens(
            db_path, window_s=SESSION_WINDOW_S, now=now
        )
        weekly_tokens = compute_cumulative_tokens(
            db_path, window_s=WEEKLY_WINDOW_S, now=now
        )

    extra = detect_extra_usage(session_usage, weekly_usage)

    reasons = []
    if session_usage >= 1.0:
        reasons.append(f"session limit exhausted ({session_usage:.1%})")
    if weekly_usage >= 1.0:
        reasons.append(f"weekly limit exhausted ({weekly_usage:.1%})")
    reason = "; ".join(reasons) if reasons else "within included limits"

    return ExtraUsageStatus(
        session_usage=session_usage,
        weekly_usage=weekly_usage,
        session_tokens=session_tokens,
        weekly_tokens=weekly_tokens,
        extra_usage=extra,
        reason=reason,
    )


def build_snapshot_ollama_section(status: ExtraUsageStatus) -> dict:
    """Build the quota.ollama section for /snapshot output.

    Mirrors the shape produced by cvm-server.ts computeQuota().
    """
    session_pct = round(status.session_usage * 100, 1)
    weekly_pct = round(status.weekly_usage * 100, 1)
    return {
        "used_pct": session_pct,
        "weekly_pct": weekly_pct,
        "session_usage": round(status.session_usage, 4),
        "weekly_usage": round(status.weekly_usage, 4),
        "session_limit": {
            "window": "5h",
            "usage": round(status.session_usage, 4),
            "usage_pct": session_pct,
        },
        "weekly_limit": {
            "window": "7d",
            "usage": round(status.weekly_usage, 4),
            "usage_pct": weekly_pct,
        },
        "session_tokens": status.session_tokens,
        "weekly_tokens": status.weekly_tokens,
        "extra_usage": status.extra_usage,
        "remaining": None,
        "healthy": not status.extra_usage,
        "locked": False,
        "resets_in_min": 300,
        "note": (
            f"EXTRA USAGE — session {session_pct}% / weekly {weekly_pct}% (pay-per-token)"
            if status.extra_usage
            else f"session {session_pct}% / weekly {weekly_pct}%"
        ),
    }


def get_status_with_fallback(
    db_path: str,
    api_key: Optional[str] = None,
    now: Optional[float] = None,
) -> ExtraUsageStatus:
    """Get extra-usage status with API fetch + DB fallback.

    Tries fetch_ollama_usage() first. If the API is unreachable (returns None),
    falls back to compute_cumulative_tokens() from zai_usage.db to provide
    a secondary signal based on actual token consumption.

    Args:
        db_path: Path to zai_usage.db for token counting (always used for
            session_tokens / weekly_tokens, and as fallback usage source
            when the API is unreachable).
        api_key: Ollama Cloud API key. If None, reads OLLAMA_CLOUD_API_KEY env var.
        now: Override current time for testing.

    Returns:
        ExtraUsageStatus with all fields populated. When API is unreachable,
        session_usage and weekly_usage are set to 0.0 (unknown) and the
        status is determined from token counts only (extra_usage=False
        unless token counts are non-zero, which alone cannot determine
        extra usage without the API fractions).
    """
    if now is None:
        now = time.time()

    api_response = fetch_ollama_usage(api_key=api_key, now=now)

    if api_response is not None:
        return get_extra_usage_status(api_response, db_path=db_path, now=now)

    # Fallback: API unreachable — use token counts from DB only
    session_tokens = compute_cumulative_tokens(
        db_path, window_s=SESSION_WINDOW_S, now=now
    )
    weekly_tokens = compute_cumulative_tokens(
        db_path, window_s=WEEKLY_WINDOW_S, now=now
    )

    return ExtraUsageStatus(
        session_usage=0.0,
        weekly_usage=0.0,
        session_tokens=session_tokens,
        weekly_tokens=weekly_tokens,
        extra_usage=False,
        reason="API unreachable — using token counts from DB (usage fractions unknown)",
    )