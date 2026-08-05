"""Tests for src/ollama_quota_tracker.py — cumulative token tracking per 5h/7d windows.

Tests use an in-memory SQLite DB populated with controlled timestamps and
token counts to verify:
  - included / extra / exhausted regime transitions
  - 5h session window boundary (tokens before cutoff excluded, after included)
  - 7d weekly window boundary
  - config override via custom providers.yaml
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import time

import pytest

from src.ollama_quota_tracker import (
    DEFAULT_SESSION_LIMIT,
    DEFAULT_WEEKLY_LIMIT,
    SESSION_WINDOW_S,
    WEEKLY_WINDOW_S,
    get_quota_status,
    query_cumulative_tokens,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_db(rows: list[tuple[float, str, int]]) -> str:
    """Create a temp DB with api_calls table and insert (ts, key_name, total_tokens) rows.

    Returns the path to the temp DB file.
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE api_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            key_name TEXT,
            key_suffix TEXT,
            model TEXT,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            tier TEXT,
            cache_hit INTEGER DEFAULT 0,
            ollama_hit INTEGER DEFAULT 0,
            ppq_hit INTEGER DEFAULT 0,
            status_code INTEGER,
            error TEXT,
            duration_ms INTEGER
        )
        """
    )
    for ts, key_name, total_tokens in rows:
        conn.execute(
            "INSERT INTO api_calls (ts, key_name, total_tokens) VALUES (?, ?, ?)",
            (ts, key_name, total_tokens),
        )
    conn.commit()
    conn.close()
    return path


def _make_config(session_limit: int, weekly_limit: int) -> str:
    """Write a temp providers.yaml with custom ollama_cloud limits."""
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w") as f:
        f.write(
            "ollama_cloud:\n"
            f"  included_quota_tokens_session: {session_limit}\n"
            f"  included_quota_tokens_weekly: {weekly_limit}\n"
        )
    return path


def _cleanup(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def now():
    """Fixed 'now' timestamp so window boundaries are deterministic."""
    return 1_000_000_000.0  # arbitrary fixed point


@pytest.fixture
def empty_db():
    path = _make_db([])
    yield path
    _cleanup(path)


@pytest.fixture
def small_config():
    """Config with small limits so we don't need billions of tokens."""
    path = _make_config(session_limit=1_000_000, weekly_limit=10_000_000)
    yield path
    _cleanup(path)


# ── Tests: query_cumulative_tokens ──────────────────────────────────────────


class TestQueryCumulativeTokens:
    def test_empty_db_returns_zero(self, empty_db, now):
        assert query_cumulative_tokens(empty_db, now=now) == 0

    def test_only_recent_tokens_counted(self, now):
        """Tokens within the 5h window are counted; older ones excluded."""
        rows = [
            (now - 100, "ollama_cloud", 500),       # 100s ago — inside
            (now - 7200, "ollama_cloud", 300),      # 2h ago — inside (< 5h)
            (now - 18001, "ollama_cloud", 999),     # 5h+1s ago — outside
            (now - 20000, "ollama_cloud", 999),     # way outside
        ]
        path = _make_db(rows)
        try:
            result = query_cumulative_tokens(
                path, window_s=SESSION_WINDOW_S, now=now
            )
            assert result == 800  # 500 + 300
        finally:
            _cleanup(path)

    def test_other_key_names_excluded(self, now):
        rows = [
            (now - 100, "ollama_cloud", 500),
            (now - 100, "zai_ours", 500),
            (now - 100, "ppq", 500),
        ]
        path = _make_db(rows)
        try:
            result = query_cumulative_tokens(path, now=now)
            assert result == 500
        finally:
            _cleanup(path)

    def test_weekly_window_includes_older_tokens(self, now):
        """7d window should include tokens that are outside the 5h window."""
        rows = [
            (now - 100, "ollama_cloud", 500),       # inside both windows
            (now - 10000, "ollama_cloud", 300),     # ~2.8h ago — inside 5h
            (now - 50000, "ollama_cloud", 200),     # ~13.9h ago — outside 5h, inside 7d
            (now - 700000, "ollama_cloud", 100),     # ~7.8d ago — outside 7d
        ]
        path = _make_db(rows)
        try:
            session_result = query_cumulative_tokens(
                path, window_s=SESSION_WINDOW_S, now=now
            )
            weekly_result = query_cumulative_tokens(
                path, window_s=WEEKLY_WINDOW_S, now=now
            )
            assert session_result == 800  # 500 + 300
            assert weekly_result == 1000  # 500 + 300 + 200
        finally:
            _cleanup(path)


# ── Tests: get_quota_status — regime transitions ────────────────────────────


class TestRegimeTransitions:
    def test_included_regime(self, now, small_config):
        """Both windows below 100% → regime='included'."""
        rows = [
            (now - 100, "ollama_cloud", 100_000),   # 10% of session limit
        ]
        path = _make_db(rows)
        try:
            status = get_quota_status(path, config_path=small_config, now=now)
            assert status["regime"] == "included"
            assert status["session_used_pct"] == pytest.approx(10.0, abs=0.01)
            assert status["weekly_used_pct"] == pytest.approx(1.0, abs=0.01)
            assert status["session_tokens"] == 100_000
            assert status["weekly_tokens"] == 100_000
        finally:
            _cleanup(path)

    def test_extra_regime_session_exceeded(self, now, small_config):
        """Session window >= 100% but weekly < 100% → regime='extra'."""
        rows = [
            (now - 100, "ollama_cloud", 1_000_000),  # exactly 100% of session
        ]
        path = _make_db(rows)
        try:
            status = get_quota_status(path, config_path=small_config, now=now)
            assert status["regime"] == "extra"
            assert status["session_used_pct"] == pytest.approx(100.0, abs=0.01)
            assert status["weekly_used_pct"] == pytest.approx(10.0, abs=0.01)
        finally:
            _cleanup(path)

    def test_extra_regime_weekly_exceeded(self, now, small_config):
        """Weekly window >= 100% but session < 100% → regime='extra'."""
        # Spread tokens over 7d so session window has less, weekly has more
        rows = []
        for i in range(10):
            # 10 calls of 1M each, spread across 6 days (well outside 5h window)
            rows.append((now - 50000 - i * 50000, "ollama_cloud", 1_000_000))
        path = _make_db(rows)
        try:
            status = get_quota_status(path, config_path=small_config, now=now)
            # session window (5h = 18000s): only calls with ts > now-18000
            # All calls are at now-50000 or earlier, so session_tokens = 0
            # weekly window (7d = 604800s): all 10 calls fit → 10M = 100%
            assert status["weekly_tokens"] == 10_000_000
            assert status["weekly_used_pct"] == pytest.approx(100.0, abs=0.01)
            assert status["regime"] == "extra"
        finally:
            _cleanup(path)

    def test_exhausted_regime(self, now, small_config):
        """Both windows >= 100% → regime='exhausted'."""
        # Fill session window with 1M tokens (100% of session limit)
        # Fill weekly window with enough tokens to also hit 100%
        rows = [
            (now - 100, "ollama_cloud", 1_000_000),    # session: 100%
            (now - 50000, "ollama_cloud", 9_000_000),  # weekly adds 9M → total 10M = 100%
        ]
        path = _make_db(rows)
        try:
            status = get_quota_status(path, config_path=small_config, now=now)
            assert status["session_used_pct"] == pytest.approx(100.0, abs=0.01)
            assert status["weekly_used_pct"] == pytest.approx(100.0, abs=0.01)
            assert status["regime"] == "exhausted"
        finally:
            _cleanup(path)

    def test_exhausted_regime_both_over_100(self, now, small_config):
        """Both windows well over 100% → regime='exhausted'."""
        rows = [
            (now - 100, "ollama_cloud", 2_000_000),       # session: 200%
            (now - 50000, "ollama_cloud", 15_000_000),    # weekly only: 150%
        ]
        path = _make_db(rows)
        try:
            status = get_quota_status(path, config_path=small_config, now=now)
            assert status["regime"] == "exhausted"
            assert status["session_used_pct"] == pytest.approx(200.0, abs=0.01)
            # weekly = 2M + 15M = 17M → 170% of 10M limit
            assert status["weekly_used_pct"] == pytest.approx(170.0, abs=0.01)
        finally:
            _cleanup(path)


# ── Tests: window boundary ──────────────────────────────────────────────────


class TestWindowBoundary:
    def test_5h_boundary_tokens_before_cutoff_excluded(self, now, small_config):
        """Tokens exactly at the 5h cutoff (ts == now - 18000) are excluded (ts > cutoff)."""
        rows = [
            # At exactly the boundary (ts = now - 18000) — excluded because query uses ts > since
            (now - SESSION_WINDOW_S, "ollama_cloud", 500_000),
            # Just inside (1 second before boundary)
            (now - SESSION_WINDOW_S + 1, "ollama_cloud", 200_000),
            # Well inside
            (now - 100, "ollama_cloud", 100_000),
        ]
        path = _make_db(rows)
        try:
            status = get_quota_status(path, config_path=small_config, now=now)
            # Only 200_000 + 100_000 = 300_000 counted in session
            assert status["session_tokens"] == 300_000
            assert status["session_used_pct"] == pytest.approx(30.0, abs=0.01)
            # All 3 rows are within 7d, so weekly = 800_000
            assert status["weekly_tokens"] == 800_000
        finally:
            _cleanup(path)

    def test_7d_boundary(self, now, small_config):
        """Tokens at the 7d boundary are excluded; just inside are included."""
        rows = [
            # At exactly 7d boundary — excluded
            (now - WEEKLY_WINDOW_S, "ollama_cloud", 500_000),
            # Just inside 7d
            (now - WEEKLY_WINDOW_S + 1, "ollama_cloud", 300_000),
            # Well inside both windows
            (now - 100, "ollama_cloud", 100_000),
        ]
        path = _make_db(rows)
        try:
            status = get_quota_status(path, config_path=small_config, now=now)
            # Session: only the 100_000 call (others are >5h old)
            assert status["session_tokens"] == 100_000
            # Weekly: 300_000 + 100_000 = 400_000 (boundary one excluded)
            assert status["weekly_tokens"] == 400_000
        finally:
            _cleanup(path)


# ── Tests: default config fallback ──────────────────────────────────────────


class TestConfigFallback:
    def test_default_limits_used_when_no_config(self, now, empty_db):
        """When config_path points to a non-existent file, defaults are used."""
        status = get_quota_status(
            empty_db, config_path="/nonexistent/providers.yaml", now=now
        )
        # Empty DB → 0 tokens → 0% → included
        assert status["regime"] == "included"
        assert status["session_used_pct"] == 0.0
        assert status["weekly_used_pct"] == 0.0
        assert status["session_tokens"] == 0
        assert status["weekly_tokens"] == 0

    def test_default_limits_values(self):
        """Sanity check on default limit constants."""
        assert DEFAULT_SESSION_LIMIT == 500_000_000
        assert DEFAULT_WEEKLY_LIMIT == 3_500_000_000


# ── Tests: return dict shape ─────────────────────────────────────────────────


class TestReturnShape:
    def test_dict_has_all_required_keys(self, now, empty_db, small_config):
        status = get_quota_status(empty_db, config_path=small_config, now=now)
        required_keys = {
            "regime",
            "session_used_pct",
            "weekly_used_pct",
            "session_tokens",
            "weekly_tokens",
        }
        assert set(status.keys()) == required_keys

    def test_regime_is_valid_string(self, now, empty_db, small_config):
        status = get_quota_status(empty_db, config_path=small_config, now=now)
        assert status["regime"] in {"included", "extra", "exhausted"}

    def test_percentages_are_floats(self, now, empty_db, small_config):
        status = get_quota_status(empty_db, config_path=small_config, now=now)
        assert isinstance(status["session_used_pct"], float)
        assert isinstance(status["weekly_used_pct"], float)

    def test_tokens_are_ints(self, now, empty_db, small_config):
        status = get_quota_status(empty_db, config_path=small_config, now=now)
        assert isinstance(status["session_tokens"], int)
        assert isinstance(status["weekly_tokens"], int)