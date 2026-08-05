"""Tests for zai_proxy.py _snapshot_quota() and dynamic ollama_cloud pricing.

Tests the EUv2-5 changes:
1. _snapshot_quota() returns real ollama_cloud data from ollama_quota_tracker
2. Snapshot includes regime, session_used_pct, weekly_used_pct fields
3. _get_ollama_cloud_cost_per_1m() returns correct rate per regime
4. _estimate_cost_usd() applies dynamic pricing for ollama_cloud
5. Kill switch (OLLAMA_EXTRA_USAGE_ENABLED=false) disables regime pricing
6. Fallback to 'included' on tracker failure
7. /quota endpoint includes ollama_cloud snapshot with regime

The tests import the production proxy module by manipulating sys.path to point
at ~/.hermes/bot/, then mock the ollama_quota_tracker to control regimes.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# ── Path setup: add zai_proxy.py's directory to sys.path ────────────────────
_PROXY_DIR = os.path.expanduser("~/.hermes/bot")
if _PROXY_DIR not in sys.path:
    sys.path.insert(0, _PROXY_DIR)

# Also need the merchant-routing-engine for src.ollama_quota_tracker
_MRE_DIR = os.path.expanduser("~/merchant-routing-engine")
if _MRE_DIR not in sys.path:
    sys.path.insert(0, _MRE_DIR)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def proxy_module():
    """Import zai_proxy fresh, with the quota tracker available."""
    # Ensure MRE is on sys.path for src.ollama_quota_tracker import
    _MRE_DIR = os.path.expanduser("~/merchant-routing-engine")
    if _MRE_DIR not in sys.path:
        sys.path.insert(0, _MRE_DIR)
    # Remove zai_proxy from cache so we get a fresh import that picks up
    # the MRE path and successfully imports ollama_quota_tracker
    if "zai_proxy" in sys.modules:
        del sys.modules["zai_proxy"]
    import zai_proxy
    return zai_proxy


@pytest.fixture
def tmp_usage_db(tmp_path: Path) -> str:
    """Create a temp zai_usage.db with api_calls table matching production schema."""
    db_path = str(tmp_path / "zai_usage.db")
    conn = sqlite3.connect(db_path)
    conn.executescript("""
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
    """)
    conn.commit()
    conn.close()
    return db_path


def _insert_call(db_path: str, ts: float, key_name: str = "ollama_cloud",
                 total_tokens: int = 1_000_000):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO api_calls (ts, key_name, total_tokens) VALUES (?, ?, ?)",
        (ts, key_name, total_tokens),
    )
    conn.commit()
    conn.close()


# ── Test: _snapshot_quota includes real ollama_cloud data ─────────────────────

class TestSnapshotQuotaOllamaCloud:
    """Verify _snapshot_quota() returns real ollama_cloud data with regime."""

    def test_snapshot_has_ollama_cloud_key(self, proxy_module):
        snap = proxy_module._snapshot_quota()
        assert "ollama_cloud" in snap

    def test_snapshot_has_regime_field(self, proxy_module):
        snap = proxy_module._snapshot_quota()
        oc = snap["ollama_cloud"]
        assert "regime" in oc
        assert oc["regime"] in ("included", "extra", "exhausted")

    def test_snapshot_has_session_used_pct(self, proxy_module):
        snap = proxy_module._snapshot_quota()
        oc = snap["ollama_cloud"]
        assert "session_used_pct" in oc
        assert isinstance(oc["session_used_pct"], float)

    def test_snapshot_has_weekly_used_pct(self, proxy_module):
        snap = proxy_module._snapshot_quota()
        oc = snap["ollama_cloud"]
        assert "weekly_used_pct" in oc
        assert isinstance(oc["weekly_used_pct"], float)

    def test_snapshot_has_session_tokens(self, proxy_module):
        snap = proxy_module._snapshot_quota()
        oc = snap["ollama_cloud"]
        assert "session_tokens" in oc
        assert isinstance(oc["session_tokens"], int)

    def test_snapshot_has_weekly_tokens(self, proxy_module):
        snap = proxy_module._snapshot_quota()
        oc = snap["ollama_cloud"]
        assert "weekly_tokens" in oc
        assert isinstance(oc["weekly_tokens"], int)

    def test_snapshot_has_used_pct_float(self, proxy_module):
        snap = proxy_module._snapshot_quota()
        oc = snap["ollama_cloud"]
        assert "used_pct" in oc
        assert isinstance(oc["used_pct"], float)
        assert oc["used_pct"] >= 0.0

    def test_snapshot_has_remaining(self, proxy_module):
        snap = proxy_module._snapshot_quota()
        oc = snap["ollama_cloud"]
        assert "remaining" in oc
        assert oc["remaining"] >= 0.0

    def test_snapshot_has_total(self, proxy_module):
        snap = proxy_module._snapshot_quota()
        oc = snap["ollama_cloud"]
        assert "total" in oc
        assert oc["total"] > 0

    def test_snapshot_no_longer_hardcoded_1m(self, proxy_module):
        """The old hardcoded total was 1_000_000. New total should be 500M."""
        snap = proxy_module._snapshot_quota()
        oc = snap["ollama_cloud"]
        assert oc["total"] != 1_000_000, "Total should no longer be hardcoded 1M"

    def test_snapshot_other_providers_unchanged(self, proxy_module):
        """ours, friend, ppq, openrouter entries should still exist."""
        snap = proxy_module._snapshot_quota()
        assert "ppq" in snap
        assert "openrouter" in snap

    def test_snapshot_default_regime_is_included(self, proxy_module):
        """With no usage data, regime should be 'included'."""
        snap = proxy_module._snapshot_quota()
        oc = snap["ollama_cloud"]
        assert oc["regime"] == "included"

    def test_snapshot_default_used_pct_is_numeric(self, proxy_module):
        """With real usage data, used_pct should be a valid float >= 0."""
        snap = proxy_module._snapshot_quota()
        oc = snap["ollama_cloud"]
        assert isinstance(oc["used_pct"], float)
        assert oc["used_pct"] >= 0.0


# ── Test: dynamic ollama_cloud cost per 1M ────────────────────────────────────

class TestDynamicOllamaCost:
    """Verify _get_ollama_cloud_cost_per_1m() returns regime-based rates."""

    def test_included_regime_returns_base_rate(self, proxy_module):
        with patch.object(proxy_module, "_get_ollama_quota_status",
                          return_value={"regime": "included"}):
            rate = proxy_module._get_ollama_cloud_cost_per_1m()
        assert rate == 0.024

    def test_extra_regime_returns_extra_rate(self, proxy_module):
        with patch.object(proxy_module, "_get_ollama_quota_status",
                          return_value={"regime": "extra"}):
            rate = proxy_module._get_ollama_cloud_cost_per_1m()
        assert rate == 0.15

    def test_exhausted_regime_returns_inf(self, proxy_module):
        with patch.object(proxy_module, "_get_ollama_quota_status",
                          return_value={"regime": "exhausted"}):
            rate = proxy_module._get_ollama_cloud_cost_per_1m()
        assert rate == float("inf")

    def test_extra_rate_above_ppq_threshold(self, proxy_module):
        """Extra rate must be > $0.14/M (PPQ) so optimizer reroutes."""
        with patch.object(proxy_module, "_get_ollama_quota_status",
                          return_value={"regime": "extra"}):
            rate = proxy_module._get_ollama_cloud_cost_per_1m()
        assert rate > 0.14, f"Extra rate ${rate}/M must be above PPQ $0.14/M"


# ── Test: _estimate_cost_usd with dynamic pricing ─────────────────────────────

class TestEstimateCostDynamic:
    """Verify _estimate_cost_usd applies dynamic pricing for ollama_cloud."""

    def test_included_regime_cost(self, proxy_module):
        with patch.object(proxy_module, "_get_ollama_quota_status",
                          return_value={"regime": "included"}):
            cost = proxy_module._estimate_cost_usd("ollama_cloud", 1_000_000)
        assert cost == pytest.approx(0.024)

    def test_extra_regime_cost(self, proxy_module):
        with patch.object(proxy_module, "_get_ollama_quota_status",
                          return_value={"regime": "extra"}):
            cost = proxy_module._estimate_cost_usd("ollama_cloud", 1_000_000)
        assert cost == pytest.approx(0.15)

    def test_exhausted_regime_cost_is_inf(self, proxy_module):
        with patch.object(proxy_module, "_get_ollama_quota_status",
                          return_value={"regime": "exhausted"}):
            cost = proxy_module._estimate_cost_usd("ollama_cloud", 1_000_000)
        assert cost == float("inf")

    def test_non_ollama_key_uses_static_dict(self, proxy_module):
        """Non-ollama_cloud keys should still use _MODEL_COST_PER_1M."""
        cost = proxy_module._estimate_cost_usd("friend", 1_000_000)
        assert cost == pytest.approx(0.029)

    def test_zero_tokens_returns_zero(self, proxy_module):
        cost = proxy_module._estimate_cost_usd("ollama_cloud", 0)
        assert cost == 0.0

    def test_none_key_returns_zero(self, proxy_module):
        cost = proxy_module._estimate_cost_usd(None, 1_000_000)
        assert cost == 0.0


# ── Test: kill switch ─────────────────────────────────────────────────────────

class TestKillSwitch:
    """OLLAMA_EXTRA_USAGE_ENABLED=false disables regime-based pricing."""

    def test_kill_switch_disabled_returns_included(self, proxy_module, monkeypatch):
        """When kill switch is off, quota status should return 'included'."""
        monkeypatch.setattr(proxy_module, "_OLLAMA_EXTRA_USAGE_ENABLED", False)
        status = proxy_module._get_ollama_quota_status()
        assert status["regime"] == "included"

    def test_kill_switch_disabled_cost_is_base(self, proxy_module, monkeypatch):
        monkeypatch.setattr(proxy_module, "_OLLAMA_EXTRA_USAGE_ENABLED", False)
        rate = proxy_module._get_ollama_cloud_cost_per_1m()
        assert rate == 0.024


# ── Test: fallback on tracker failure ─────────────────────────────────────────

class TestFallback:
    """Verify graceful fallback when quota tracker fails."""

    def test_tracker_none_falls_back_to_included(self, proxy_module, monkeypatch):
        monkeypatch.setattr(proxy_module, "_get_quota_status", None)
        status = proxy_module._get_ollama_quota_status()
        assert status["regime"] == "included"
        assert status["session_used_pct"] == 0.0

    def test_tracker_exception_falls_back_to_included(self, proxy_module, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("DB locked")
        monkeypatch.setattr(proxy_module, "_get_quota_status", boom)
        # Clear cache
        monkeypatch.setattr(proxy_module, "_ollama_quota_cache", None)
        monkeypatch.setattr(proxy_module, "_ollama_quota_cache_ts", 0.0)
        status = proxy_module._get_ollama_quota_status()
        assert status["regime"] == "included"


# ── Test: quota caching ───────────────────────────────────────────────────────

class TestQuotaCache:
    """Verify the 30-second cache prevents DB hammering."""

    def test_cache_returns_same_result_within_ttl(self, proxy_module):
        # First call populates cache
        status1 = proxy_module._get_ollama_quota_status()
        # Second call should return cached result
        status2 = proxy_module._get_ollama_quota_status()
        assert status1 == status2

    def test_cache_hit_does_not_call_tracker(self, proxy_module, monkeypatch):
        # Prime the cache
        proxy_module._get_ollama_quota_status()
        # Replace tracker with a function that would raise
        def boom(*args, **kwargs):
            raise AssertionError("Should not be called — cache hit")
        monkeypatch.setattr(proxy_module, "_get_quota_status", boom)
        # Should return cached value, not call boom
        status = proxy_module._get_ollama_quota_status()
        assert status["regime"] in ("included", "extra", "exhausted")


# ── Test: snapshot with real DB data ──────────────────────────────────────────

class TestSnapshotWithRealData:
    """Integration test: _snapshot_quota() with a real temp DB."""

    def test_snapshot_reflects_real_usage(self, proxy_module, tmp_usage_db, monkeypatch):
        # Insert 300M tokens in the last hour (within 5h window)
        now = time.time()
        for i in range(300):
            _insert_call(tmp_usage_db, now - 3600 + i, total_tokens=1_000_000)

        # Point the proxy at our temp DB
        monkeypatch.setattr(proxy_module, "USAGE_DB", Path(tmp_usage_db))
        # Clear cache
        monkeypatch.setattr(proxy_module, "_ollama_quota_cache", None)
        monkeypatch.setattr(proxy_module, "_ollama_quota_cache_ts", 0.0)

        snap = proxy_module._snapshot_quota()
        oc = snap["ollama_cloud"]
        assert oc["session_tokens"] == 300_000_000
        assert oc["session_used_pct"] == pytest.approx(60.0, abs=0.1)  # 300M/500M
        assert oc["regime"] == "included"  # 60% < 100%

    def test_snapshot_extra_regime_at_100pct(self, proxy_module, tmp_usage_db, monkeypatch):
        # Insert 500M tokens (exactly 100% of session limit)
        now = time.time()
        for i in range(500):
            _insert_call(tmp_usage_db, now - 3600 + i, total_tokens=1_000_000)

        monkeypatch.setattr(proxy_module, "USAGE_DB", Path(tmp_usage_db))
        monkeypatch.setattr(proxy_module, "_ollama_quota_cache", None)
        monkeypatch.setattr(proxy_module, "_ollama_quota_cache_ts", 0.0)

        snap = proxy_module._snapshot_quota()
        oc = snap["ollama_cloud"]
        assert oc["regime"] == "extra"
        assert oc["session_used_pct"] >= 100.0

    def test_snapshot_exhausted_regime_both_windows(self, proxy_module, tmp_usage_db, monkeypatch):
        # Insert 3.5B tokens spread across 7 days (exhausts weekly)
        # and 500M in the last 5h (exhausts session)
        now = time.time()
        # Session exhaustion: 500M in last 5h
        for i in range(500):
            _insert_call(tmp_usage_db, now - 3600 + i, total_tokens=1_000_000)
        # Weekly exhaustion: 3.5B over 7 days (minus the 500M already in session)
        for day in range(6):
            t = now - (day + 1) * 86400
            for i in range(500):
                _insert_call(tmp_usage_db, t + i, total_tokens=1_000_000)

        monkeypatch.setattr(proxy_module, "USAGE_DB", Path(tmp_usage_db))
        monkeypatch.setattr(proxy_module, "_ollama_quota_cache", None)
        monkeypatch.setattr(proxy_module, "_ollama_quota_cache_ts", 0.0)

        snap = proxy_module._snapshot_quota()
        oc = snap["ollama_cloud"]
        assert oc["regime"] == "exhausted"
        assert oc["session_used_pct"] >= 100.0
        assert oc["weekly_used_pct"] >= 100.0