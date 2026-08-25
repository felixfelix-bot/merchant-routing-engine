"""ollama_quota_tracker.py — Cumulative token tracking per 5h/7d windows.

Determines the current Ollama Cloud quota regime by querying the local
api_calls table (zai_usage.db) for cumulative token usage within the
rolling 5-hour session window and 7-day weekly window, then comparing
against configured included-quota limits from providers.yaml.

Regimes:
  - "included":  both windows below 100% of their included quota
  - "extra":     at least one window >= 100% but not both at hard limit
  - "exhausted": both windows have hit their hard limit (no tokens left)

This module is the deterministic arithmetic layer that feeds the
pricing_engine's extra-usage multiplier (Step 3 of the plan) and the
live_router's routing decisions (Step 4).
"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Optional

try:
    import yaml
except ImportError:  # pragma: no cover — yaml is a dev dep
    yaml = None

# ── Window durations in seconds ──────────────────────────────────────────────
SESSION_WINDOW_S = 5 * 3600   # 5 hours
WEEKLY_WINDOW_S = 7 * 86400   # 7 days

# ── Default limits (from providers.yaml, used if config not readable) ────────
DEFAULT_SESSION_LIMIT = 500_000_000      # 500M tokens / 5h
DEFAULT_WEEKLY_LIMIT = 3_500_000_000     # 3.5B tokens / 7d

# ── Config path ──────────────────────────────────────────────────────────────
_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config",
    "providers.yaml",
)


def _load_limits(
    config_path: Optional[str] = None,
) -> tuple[int, int]:
    """Read included-quota limits from providers.yaml.

    Returns (session_limit, weekly_limit). Falls back to defaults if
    the config file is missing or the keys are absent.
    """
    path = config_path or _CONFIG_PATH
    session_limit = DEFAULT_SESSION_LIMIT
    weekly_limit = DEFAULT_WEEKLY_LIMIT

    if yaml is None:
        return session_limit, weekly_limit

    try:
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}
        oc = cfg.get("ollama_cloud", {}) or {}
        session_limit = int(
            oc.get("included_quota_tokens_session", session_limit)
        )
        weekly_limit = int(
            oc.get("included_quota_tokens_weekly", weekly_limit)
        )
    except (FileNotFoundError, OSError, ValueError, TypeError):
        pass

    return session_limit, weekly_limit


def query_cumulative_tokens(
    db_path: str,
    key_name: str = "ollama_cloud",
    window_s: int = SESSION_WINDOW_S,
    now: Optional[float] = None,
) -> int:
    """Sum total_tokens from api_calls for *key_name* within *window_s*.

    Uses ``ts > (now - window_s)`` so the window is a rolling cutoff.
    Returns 0 when the table is empty or no rows match.
    """
    if now is None:
        now = time.time()
    since = now - window_s

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "SELECT COALESCE(SUM(total_tokens), 0) "
            "FROM api_calls WHERE key_name = ? AND ts > ?",
            (key_name, since),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def get_quota_status(
    db_path: str,
    config_path: Optional[str] = None,
    now: Optional[float] = None,
    key_name: str = "ollama_cloud",
) -> dict:
    """Compute the full quota status for an Ollama Cloud key.

    Args:
        db_path: Absolute path to zai_usage.db.
        config_path: Optional override for providers.yaml path.
        now: Override current time for testing.
        key_name: Which Ollama Cloud key to query (default: "ollama_cloud").
            Each subscription account gets its own quota window tracked
            independently via the api_calls.key_name column.

    Returns:
        Dict with keys::

            {
                "regime": "included" | "extra" | "exhausted",
                "session_used_pct": float,   # 0.0-100.0+
                "weekly_used_pct": float,    # 0.0-100.0+
                "session_tokens": int,       # cumulative tokens in 5h
                "weekly_tokens": int,        # cumulative tokens in 7d
            }
    """
    if now is None:
        now = time.time()

    session_limit, weekly_limit = _load_limits(config_path)

    session_tokens = query_cumulative_tokens(
        db_path, key_name=key_name, window_s=SESSION_WINDOW_S, now=now
    )
    weekly_tokens = query_cumulative_tokens(
        db_path, key_name=key_name, window_s=WEEKLY_WINDOW_S, now=now
    )

    session_used_pct = (
        (session_tokens / session_limit * 100.0) if session_limit > 0 else 100.0
    )
    weekly_used_pct = (
        (weekly_tokens / weekly_limit * 100.0) if weekly_limit > 0 else 100.0
    )

    session_exhausted = session_used_pct >= 100.0
    weekly_exhausted = weekly_used_pct >= 100.0

    if session_exhausted and weekly_exhausted:
        regime = "exhausted"
    elif session_exhausted or weekly_exhausted:
        regime = "extra"
    else:
        regime = "included"

    return {
        "regime": regime,
        "session_used_pct": round(session_used_pct, 2),
        "weekly_used_pct": round(weekly_used_pct, 2),
        "session_tokens": session_tokens,
        "weekly_tokens": weekly_tokens,
    }