"""Tests for Ollama Cloud extra-usage detection.

Validates that when included limits are exhausted (usage >= 1.0),
the system correctly flags extra_usage=true in the quota snapshot.

The detection works by:
1. Comparing usage fractions from ollama.com/api/usage against 1.0
2. Tracking cumulative tokens from api_calls in 5h/7d windows
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.ollama_extra_usage import (
    SESSION_WINDOW_S,
    WEEKLY_WINDOW_S,
    ExtraUsageStatus,
    build_snapshot_ollama_section,
    compute_cumulative_tokens,
    detect_extra_usage,
    fetch_ollama_usage,
    get_extra_usage_status,
    get_status_with_fallback,
    _reset_ollama_cache,
    _ollama_cache,
    _OLLAMA_CACHE_TTL_S,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_db(tmp_path: Path) -> str:
    """Create a temp api_calls table matching the production schema."""
    db_path = str(tmp_path / "test_usage.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE api_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            key_name TEXT,
            key_suffix TEXT,
            model TEXT,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            total_tokens INTEGER,
            tier TEXT,
            cache_hit INTEGER DEFAULT 0,
            ollama_hit INTEGER DEFAULT 0,
            ppq_hit INTEGER DEFAULT 0,
            status_code INTEGER,
            error TEXT,
            duration_ms INTEGER
        );
        """
    )
    conn.commit()
    conn.close()
    return db_path


def _insert_call(db_path: str, ts: float, key_name: str, total_tokens: int):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO api_calls (ts, key_name, total_tokens) VALUES (?, ?, ?)",
        (ts, key_name, total_tokens),
    )
    conn.commit()
    conn.close()


def _api_response(session_usage: float, weekly_usage: float) -> dict:
    """Build a mock ollama.com/api/usage response."""
    return {
        "limits": {
            "session": {"usage": session_usage},
            "weekly": {"usage": weekly_usage},
        }
    }


# ── Core detection logic ──────────────────────────────────────────────────────


class TestDetectExtraUsage:
    def test_within_limits_not_extra(self):
        assert detect_extra_usage(0.6, 0.11) is False

    def test_session_at_100_percent_is_extra(self):
        assert detect_extra_usage(1.0, 0.11) is True

    def test_weekly_at_100_percent_is_extra(self):
        assert detect_extra_usage(0.06, 1.0) is True

    def test_both_at_100_percent_is_extra(self):
        assert detect_extra_usage(1.0, 1.0) is True

    def test_above_100_percent_is_extra(self):
        assert detect_extra_usage(1.5, 0.11) is True
        assert detect_extra_usage(0.06, 2.0) is True

    def test_zero_usage_not_extra(self):
        assert detect_extra_usage(0.0, 0.0) is False

    def test_just_below_100_not_extra(self):
        assert detect_extra_usage(0.999, 0.99) is False

    def test_just_at_100_is_extra(self):
        # Boundary: exactly 1.0 should trigger
        assert detect_extra_usage(1.0, 0.0) is True
        assert detect_extra_usage(0.0, 1.0) is True


# ── Cumulative token tracking ──────────────────────────────────────────────────


class TestCumulativeTokens:
    def test_no_calls_returns_zero(self, tmp_db):
        assert compute_cumulative_tokens(tmp_db) == 0

    def test_sums_tokens_within_window(self, tmp_db):
        now = time.time()
        _insert_call(tmp_db, now - 100, "ollama_cloud", 5000)
        _insert_call(tmp_db, now - 200, "ollama_cloud", 3000)
        result = compute_cumulative_tokens(tmp_db, window_s=3600, now=now)
        assert result == 8000

    def test_excludes_old_calls(self, tmp_db):
        now = time.time()
        _insert_call(tmp_db, now - 100, "ollama_cloud", 5000)
        # This call is older than the 1h window
        _insert_call(tmp_db, now - 7200, "ollama_cloud", 9999)
        result = compute_cumulative_tokens(tmp_db, window_s=3600, now=now)
        assert result == 5000

    def test_excludes_other_keys(self, tmp_db):
        now = time.time()
        _insert_call(tmp_db, now - 100, "ollama_cloud", 5000)
        _insert_call(tmp_db, now - 100, "ours", 9999)
        result = compute_cumulative_tokens(tmp_db, window_s=3600, now=now)
        assert result == 5000

    def test_5h_session_window(self, tmp_db):
        now = time.time()
        # Within 5h
        _insert_call(tmp_db, now - 4 * 3600, "ollama_cloud", 10000)
        # Outside 5h but within 7d
        _insert_call(tmp_db, now - 6 * 3600, "ollama_cloud", 20000)
        session_tok = compute_cumulative_tokens(tmp_db, window_s=SESSION_WINDOW_S, now=now)
        weekly_tok = compute_cumulative_tokens(tmp_db, window_s=WEEKLY_WINDOW_S, now=now)
        assert session_tok == 10000
        assert weekly_tok == 30000

    def test_7d_weekly_window(self, tmp_db):
        now = time.time()
        # Within 7d
        _insert_call(tmp_db, now - 3 * 86400, "ollama_cloud", 50000)
        # Outside 7d
        _insert_call(tmp_db, now - 8 * 86400, "ollama_cloud", 99999)
        weekly_tok = compute_cumulative_tokens(tmp_db, window_s=WEEKLY_WINDOW_S, now=now)
        assert weekly_tok == 50000


# ── Full status integration ───────────────────────────────────────────────────


class TestGetExtraUsageStatus:
    def test_normal_usage_no_extra(self, tmp_db):
        now = time.time()
        _insert_call(tmp_db, now - 100, "ollama_cloud", 5000)
        status = get_extra_usage_status(
            _api_response(0.06, 0.11), db_path=tmp_db, now=now
        )
        assert status.extra_usage is False
        assert status.session_usage == 0.06
        assert status.weekly_usage == 0.11
        assert status.session_tokens == 5000
        assert status.reason == "within included limits"

    def test_session_exhausted_triggers_extra(self, tmp_db):
        status = get_extra_usage_status(
            _api_response(1.0, 0.11), db_path=tmp_db, now=time.time()
        )
        assert status.extra_usage is True
        assert "session limit exhausted" in status.reason

    def test_weekly_exhausted_triggers_extra(self, tmp_db):
        status = get_extra_usage_status(
            _api_response(0.06, 1.0), db_path=tmp_db, now=time.time()
        )
        assert status.extra_usage is True
        assert "weekly limit exhausted" in status.reason

    def test_both_exhausted_triggers_extra(self, tmp_db):
        status = get_extra_usage_status(
            _api_response(1.0, 1.0), db_path=tmp_db, now=time.time()
        )
        assert status.extra_usage is True
        assert "session limit exhausted" in status.reason
        assert "weekly limit exhausted" in status.reason

    def test_no_db_path_zeros_tokens(self):
        status = get_extra_usage_status(
            _api_response(0.06, 0.11), db_path=None
        )
        assert status.session_tokens == 0
        assert status.weekly_tokens == 0
        assert status.extra_usage is False

    def test_handles_missing_limits(self):
        status = get_extra_usage_status({}, db_path=None)
        assert status.session_usage == 0.0
        assert status.weekly_usage == 0.0
        assert status.extra_usage is False

    def test_handles_null_usage(self):
        resp = {"limits": {"session": {"usage": None}, "weekly": {"usage": None}}}
        status = get_extra_usage_status(resp, db_path=None)
        assert status.session_usage == 0.0
        assert status.weekly_usage == 0.0
        assert status.extra_usage is False

    def test_token_tracking_with_time_windows(self, tmp_db):
        now = time.time()
        # Recent call (within 5h)
        _insert_call(tmp_db, now - 100, "ollama_cloud", 3000)
        # Old call (outside 5h, within 7d)
        _insert_call(tmp_db, now - 6 * 3600, "ollama_cloud", 7000)
        status = get_extra_usage_status(
            _api_response(0.5, 0.5), db_path=tmp_db, now=now
        )
        assert status.session_tokens == 3000   # only recent
        assert status.weekly_tokens == 10000   # both


# ── Snapshot output shape ──────────────────────────────────────────────────────


class TestSnapshotSection:
    def test_snapshot_includes_extra_usage_boolean(self):
        status = ExtraUsageStatus(
            session_usage=0.06,
            weekly_usage=0.11,
            session_tokens=5000,
            weekly_tokens=50000,
            extra_usage=False,
        )
        snap = build_snapshot_ollama_section(status)
        assert "extra_usage" in snap
        assert isinstance(snap["extra_usage"], bool)
        assert snap["extra_usage"] is False

    def test_snapshot_extra_usage_true(self):
        status = ExtraUsageStatus(
            session_usage=1.0,
            weekly_usage=0.11,
            session_tokens=999999,
            weekly_tokens=5000000,
            extra_usage=True,
        )
        snap = build_snapshot_ollama_section(status)
        assert snap["extra_usage"] is True
        assert "EXTRA USAGE" in snap["note"]

    def test_snapshot_includes_token_counts(self):
        status = ExtraUsageStatus(
            session_usage=0.5,
            weekly_usage=0.5,
            session_tokens=12345,
            weekly_tokens=67890,
            extra_usage=False,
        )
        snap = build_snapshot_ollama_section(status)
        assert snap["session_tokens"] == 12345
        assert snap["weekly_tokens"] == 67890

    def test_snapshot_includes_usage_fractions(self):
        status = ExtraUsageStatus(
            session_usage=0.06,
            weekly_usage=0.11,
            session_tokens=0,
            weekly_tokens=0,
            extra_usage=False,
        )
        snap = build_snapshot_ollama_section(status)
        assert snap["session_usage"] == 0.06
        assert snap["weekly_usage"] == 0.11
        assert snap["used_pct"] == 6.0
        assert snap["weekly_pct"] == 11.0

    def test_snapshot_healthy_flag_inverts_extra_usage(self):
        status = ExtraUsageStatus(
            session_usage=1.0, weekly_usage=0.0,
            session_tokens=0, weekly_tokens=0,
            extra_usage=True,
        )
        snap = build_snapshot_ollama_section(status)
        assert snap["healthy"] is False

        status.extra_usage = False
        snap = build_snapshot_ollama_section(status)
        assert snap["healthy"] is True

    def test_snapshot_includes_limit_windows(self):
        status = ExtraUsageStatus(
            session_usage=0.06, weekly_usage=0.11,
            session_tokens=0, weekly_tokens=0,
            extra_usage=False,
        )
        snap = build_snapshot_ollama_section(status)
        assert snap["session_limit"]["window"] == "5h"
        assert snap["weekly_limit"]["window"] == "7d"

    def test_snapshot_has_all_required_fields(self):
        """GATE: /snapshot includes quota.ollama.extra_usage boolean"""
        status = ExtraUsageStatus(
            session_usage=0.06, weekly_usage=0.11,
            session_tokens=5000, weekly_tokens=50000,
            extra_usage=False,
        )
        snap = build_snapshot_ollama_section(status)
        required = {
            "used_pct", "weekly_pct", "session_usage", "weekly_usage",
            "session_tokens", "weekly_tokens", "extra_usage",
            "remaining", "healthy", "locked", "resets_in_min", "note",
            "session_limit", "weekly_limit",
        }
        assert required.issubset(snap.keys()), f"Missing: {required - snap.keys()}"


# ── fetch_ollama_usage: API fetch with cache ──────────────────────────────────


class TestFetchOllamaUsage:
    """Tests for fetch_ollama_usage() — API fetch with 30s cache + stampede guard."""

    def setup_method(self):
        """Reset cache before each test to avoid cross-test contamination."""
        _reset_ollama_cache()

    def _mock_response(self, data: dict):
        """Build a mock HTTP response object."""
        mock = MagicMock()
        mock.status = 200
        mock.read.return_value = json.dumps(data).encode("utf-8")
        mock.__enter__ = MagicMock(return_value=mock)
        mock.__exit__ = MagicMock(return_value=False)
        return mock

    @patch("src.ollama_extra_usage.urllib.request.urlopen")
    def test_fetch_success_returns_parsed_dict(self, mock_urlopen):
        """Successful fetch returns the parsed JSON dict."""
        api_data = {"limits": {"session": {"usage": 0.06}, "weekly": {"usage": 0.11}}}
        mock_urlopen.return_value = self._mock_response(api_data)

        result = fetch_ollama_usage(api_key="test-key")
        assert result is not None
        assert result["limits"]["session"]["usage"] == 0.06
        assert result["limits"]["weekly"]["usage"] == 0.11

    @patch("src.ollama_extra_usage.urllib.request.urlopen")
    def test_fetch_cache_second_call_within_30s_does_not_fetch(self, mock_urlopen):
        """Second call within cache TTL does not make another HTTP request."""
        api_data = {"limits": {"session": {"usage": 0.5}, "weekly": {"usage": 0.3}}}
        mock_urlopen.return_value = self._mock_response(api_data)

        # First call — hits the API
        result1 = fetch_ollama_usage(api_key="test-key")
        assert result1 is not None
        assert mock_urlopen.call_count == 1

        # Second call within 30s — should use cache, no new HTTP request
        result2 = fetch_ollama_usage(api_key="test-key")
        assert result2 is not None
        assert result2 == result1
        assert mock_urlopen.call_count == 1  # Still only 1 HTTP call

    @patch("src.ollama_extra_usage.urllib.request.urlopen")
    def test_fetch_cache_expires_after_30s(self, mock_urlopen):
        """After cache TTL expires, a new fetch is made."""
        api_data = {"limits": {"session": {"usage": 0.5}, "weekly": {"usage": 0.3}}}
        mock_urlopen.return_value = self._mock_response(api_data)

        base_time = time.time()

        # First call at t=0
        with patch("src.ollama_extra_usage.time.time", return_value=base_time):
            fetch_ollama_usage(api_key="test-key", now=base_time)
        assert mock_urlopen.call_count == 1

        # Second call at t=31 (past 30s TTL) — should fetch again
        with patch("src.ollama_extra_usage.time.time", return_value=base_time + 31):
            fetch_ollama_usage(api_key="test-key", now=base_time + 31)
        assert mock_urlopen.call_count == 2

    @patch("src.ollama_extra_usage.urllib.request.urlopen")
    def test_fetch_failure_returns_none(self, mock_urlopen):
        """Network error returns None."""
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        result = fetch_ollama_usage(api_key="test-key")
        assert result is None

    @patch("src.ollama_extra_usage.urllib.request.urlopen")
    def test_fetch_failure_updates_cache_timestamp(self, mock_urlopen):
        """On failure, cache timestamp is updated to prevent stampede."""
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        base_time = time.time()
        with patch("src.ollama_extra_usage.time.time", return_value=base_time):
            fetch_ollama_usage(api_key="test-key", now=base_time)

        # Cache timestamp should be set even on failure
        assert _ollama_cache["at"] == base_time
        assert _ollama_cache["data"] is None

    @patch("src.ollama_extra_usage.urllib.request.urlopen")
    def test_fetch_failure_then_second_call_within_ttl_does_not_fetch(self, mock_urlopen):
        """After a failed fetch, second call within TTL should NOT re-fetch."""
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        base_time = time.time()
        with patch("src.ollama_extra_usage.time.time", return_value=base_time):
            result1 = fetch_ollama_usage(api_key="test-key", now=base_time)
        assert result1 is None
        assert mock_urlopen.call_count == 1

        # Second call within TTL — should NOT fetch (stampede prevention)
        with patch("src.ollama_extra_usage.time.time", return_value=base_time + 10):
            result2 = fetch_ollama_usage(api_key="test-key", now=base_time + 10)
        assert result2 is None
        assert mock_urlopen.call_count == 1  # Still only 1 call

    @patch("src.ollama_extra_usage.urllib.request.urlopen")
    def test_fetch_timeout_returns_none(self, mock_urlopen):
        """Timeout returns None."""
        mock_urlopen.side_effect = TimeoutError("Request timed out")

        result = fetch_ollama_usage(api_key="test-key")
        assert result is None

    @patch("src.ollama_extra_usage.urllib.request.urlopen")
    def test_fetch_non_200_returns_none(self, mock_urlopen):
        """Non-200 response returns None and updates cache timestamp."""
        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_resp.read.return_value = b"Internal Server Error"
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = fetch_ollama_usage(api_key="test-key")
        assert result is None
        assert _ollama_cache["at"] > 0  # Timestamp updated for backoff

    @patch("src.ollama_extra_usage.urllib.request.urlopen")
    def test_fetch_sets_authorization_header(self, mock_urlopen):
        """Authorization header is set with Bearer token."""
        mock_urlopen.return_value = self._mock_response({"limits": {}})

        fetch_ollama_usage(api_key="my-secret-key")

        # Verify the request was made with correct auth header
        args, kwargs = mock_urlopen.call_args
        req = args[0]
        assert req.headers["Authorization"] == "Bearer my-secret-key"


# ── get_status_with_fallback: API + DB fallback ───────────────────────────────


class TestGetStatusWithFallback:
    """Tests for get_status_with_fallback() — API fetch with DB fallback."""

    def setup_method(self):
        _reset_ollama_cache()

    @patch("src.ollama_extra_usage.urllib.request.urlopen")
    def test_with_api_uses_api_response(self, mock_urlopen, tmp_db):
        """When API is reachable, uses API response for usage fractions."""
        api_data = {"limits": {"session": {"usage": 0.06}, "weekly": {"usage": 0.11}}}
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps(api_data).encode("utf-8")
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        status = get_status_with_fallback(db_path=tmp_db, api_key="test-key")
        assert status.session_usage == 0.06
        assert status.weekly_usage == 0.11
        assert status.extra_usage is False
        assert "within included limits" in status.reason

    @patch("src.ollama_extra_usage.urllib.request.urlopen")
    def test_api_unreachable_falls_back_to_db(self, mock_urlopen, tmp_db):
        """When API is unreachable, falls back to DB token counts."""
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        now = time.time()
        _insert_call(tmp_db, now - 100, "ollama_cloud", 5000)

        status = get_status_with_fallback(db_path=tmp_db, api_key="test-key", now=now)
        assert status.session_usage == 0.0  # Unknown without API
        assert status.weekly_usage == 0.0
        assert status.session_tokens == 5000  # From DB
        assert status.extra_usage is False  # Can't determine without API fractions
        assert "API unreachable" in status.reason

    @patch("src.ollama_extra_usage.urllib.request.urlopen")
    def test_api_exhausted_status_with_fallback(self, mock_urlopen, tmp_db):
        """When API shows usage >= 1.0, extra_usage is True even with fallback."""
        api_data = {"limits": {"session": {"usage": 1.0}, "weekly": {"usage": 0.5}}}
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps(api_data).encode("utf-8")
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        status = get_status_with_fallback(db_path=tmp_db, api_key="test-key")
        assert status.extra_usage is True
        assert "session limit exhausted" in status.reason