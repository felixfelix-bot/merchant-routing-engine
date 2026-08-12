"""Tests for src/ppq_balance_collector.py — PPQ credit-balance collector.

Covers: starting-balance resolution, parsing (incl. exhausted / refund /
non-numeric), storage + retrieval in the shared provider_balances table,
HTTP collection (mocked), cron entrypoint exit codes, and compatibility with
the pricing-engine pressure curve (_compute_ppq_pressure / quota_pressure_factor).
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import balance_collectors as pc


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "burn.db")


@pytest.fixture
def clean_env(monkeypatch):
    for k in ("PPQ_API_KEY", "PPQ_STARTING_BALANCE", "API_BURN_DB"):
        monkeypatch.delenv(k, raising=False)
    yield


# ── Starting balance resolution ──────────────────────────────────────────────

class TestStarting:
    def test_default_is_20(self, clean_env):
        assert pc._resolve_starting(None) == 20.0

    def test_env_override(self, monkeypatch, clean_env):
        monkeypatch.setenv("PPQ_STARTING_BALANCE", "42.5")
        assert pc._resolve_starting(None) == 42.5

    def test_explicit_wins(self, monkeypatch, clean_env):
        monkeypatch.setenv("PPQ_STARTING_BALANCE", "42")
        assert pc._resolve_starting(7) == 7.0

    def test_garbage_env_falls_back(self, monkeypatch, clean_env):
        monkeypatch.setenv("PPQ_STARTING_BALANCE", "nope")
        assert pc._resolve_starting(None) == 20.0


# ── Parsing ──────────────────────────────────────────────────────────────────

class TestParse:
    def test_quarter_spent(self):
        b = pc.parse_ppq_balance({"balance": 15.0, "currency": "USD"}, 20.0)
        assert b is not None
        assert b.balance == 15.0
        assert b.starting == 20.0
        assert b.usage_fraction == pytest.approx(0.25)
        assert b.is_exhausted is False
        assert b.remaining == 15.0
        assert b.used_pct == pytest.approx(25.0)

    def test_three_quarters(self):
        b = pc.parse_ppq_balance({"balance": 5.0}, 20.0)
        assert b is not None
        assert b.usage_fraction == pytest.approx(0.75)

    def test_full_unused(self):
        b = pc.parse_ppq_balance({"balance": 20.0}, 20.0)
        assert b is not None
        assert b.usage_fraction == pytest.approx(0.0)
        assert b.is_exhausted is False

    def test_exhausted(self):
        b = pc.parse_ppq_balance({"balance": 0.0}, 20.0)
        assert b is not None
        assert b.usage_fraction == 1.0
        assert b.is_exhausted is True

    def test_negative_is_exhausted(self):
        b = pc.parse_ppq_balance({"balance": -2.0}, 20.0)
        assert b is not None
        assert b.usage_fraction == 1.0
        assert b.is_exhausted is True

    def test_refund_clamped(self):
        b = pc.parse_ppq_balance({"balance": 25.0}, 20.0)
        assert b is not None
        assert b.usage_fraction == 0.0

    def test_numeric_string(self):
        b = pc.parse_ppq_balance({"balance": "10"}, 20.0)
        assert b is not None and b.balance == 10.0

    def test_missing_balance_none(self):
        assert pc.parse_ppq_balance({"currency": "USD"}, 20.0) is None

    def test_non_dict_none(self):
        assert pc.parse_ppq_balance("x", 20.0) is None
        assert pc.parse_ppq_balance(None, 20.0) is None

    def test_non_numeric_none(self):
        assert pc.parse_ppq_balance({"balance": "free"}, 20.0) is None
        assert pc.parse_ppq_balance({"balance": None}, 20.0) is None

    def test_nan_none(self):
        assert pc.parse_ppq_balance({"balance": float("nan")}, 20.0) is None

    def test_starting_zero_no_pressure(self):
        b = pc.parse_ppq_balance({"balance": 0.0}, 0.0)
        assert b is not None and b.usage_fraction == 0.0


# ── Storage ──────────────────────────────────────────────────────────────────

class TestStorage:
    def test_store_and_latest(self, db_path):
        b = pc.parse_ppq_balance({"balance": 5.0}, 20.0)
        assert b is not None
        assert pc.store_ppq_balance(db_path, b) is True
        got = pc.get_latest_ppq_balance(db_path)
        assert got is not None
        assert got.balance == 5.0
        assert got.starting == 20.0
        assert got.usage_fraction == pytest.approx(0.75)

    def test_latest_none_when_empty(self, db_path):
        assert pc.get_latest_ppq_balance(db_path) is None

    def test_latest_is_most_recent(self, db_path):
        b1 = pc.parse_ppq_balance({"balance": 18.0}, 20.0)
        b2 = pc.parse_ppq_balance({"balance": 5.0}, 20.0)
        assert b1 is not None and b2 is not None
        b1.collected_at, b2.collected_at = 100.0, 200.0
        pc.store_ppq_balance(db_path, b1)
        pc.store_ppq_balance(db_path, b2)
        got = pc.get_latest_ppq_balance(db_path)
        assert got is not None and got.balance == 5.0

    def test_store_none_false(self, db_path):
        assert pc.store_ppq_balance(db_path, None) is False

    def test_shared_table_schema(self, db_path):
        b = pc.parse_ppq_balance({"balance": 1.0}, 20.0)
        assert b is not None
        pc.store_ppq_balance(db_path, b)
        cols = [r[1] for r in sqlite3.connect(db_path)
                .execute("PRAGMA table_info(provider_balances)").fetchall()]
        assert cols == [
            "id", "provider", "collected_at", "usage", "limit_credits",
            "limit_remaining", "usage_fraction", "is_unlimited",
            "is_free_tier", "raw_json",
        ]

    def test_provider_row_is_ppq(self, db_path):
        b = pc.parse_ppq_balance({"balance": 1.0}, 20.0)
        assert b is not None
        pc.store_ppq_balance(db_path, b)
        row = sqlite3.connect(db_path).execute(
            "SELECT provider, usage, limit_credits, limit_remaining, is_unlimited "
            "FROM provider_balances WHERE provider='ppq'"
        ).fetchone()
        assert row is not None
        prov, usage, lim, rem, unlim = row
        assert prov == "ppq"
        assert usage == pytest.approx(19.0)   # 20 - 1
        assert lim == pytest.approx(20.0)     # starting
        assert rem == pytest.approx(1.0)      # balance
        assert unlim == 0


# ── HTTP collection (mocked) ─────────────────────────────────────────────────

def _mock_urlopen(monkeypatch, payload, *, status=200, exc=None):
    class _Resp:
        def __init__(self, data):
            self._b = io.BytesIO(data)
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self._b.read()

    def fake(req, timeout=None):
        if exc is not None:
            raise exc
        return _Resp(payload)

    monkeypatch.setattr(pc.urllib.request, "urlopen", fake)


class TestCollect:
    def test_no_key_none(self, clean_env):
        assert pc.collect_ppq_balance(api_key=None) is None

    def test_env_key(self, monkeypatch, clean_env):
        _mock_urlopen(monkeypatch, json.dumps({"balance": 13.5}).encode())
        monkeypatch.setenv("PPQ_API_KEY", "env-key")
        b = pc.collect_ppq_balance()
        assert b is not None and b.balance == 13.5

    def test_happy_path(self, monkeypatch, clean_env):
        _mock_urlopen(monkeypatch, json.dumps({"balance": 12.0}).encode())
        b = pc.collect_ppq_balance(api_key="k", starting=20.0)
        assert b is not None
        assert b.balance == 12.0
        assert b.usage_fraction == pytest.approx(0.4)

    def test_env_starting(self, monkeypatch, clean_env):
        _mock_urlopen(monkeypatch, json.dumps({"balance": 5.0}).encode())
        monkeypatch.setenv("PPQ_STARTING_BALANCE", "10")
        b = pc.collect_ppq_balance(api_key="k")
        assert b is not None
        assert b.starting == 10.0
        assert b.usage_fraction == pytest.approx(0.5)

    def test_non_200_none(self, monkeypatch, clean_env):
        _mock_urlopen(monkeypatch, b"err", status=500)
        assert pc.collect_ppq_balance(api_key="k") is None

    def test_network_error_none(self, monkeypatch, clean_env):
        _mock_urlopen(monkeypatch, b"", exc=urllib.error.URLError("timeout"))
        assert pc.collect_ppq_balance(api_key="k") is None

    def test_malformed_json_none(self, monkeypatch, clean_env):
        _mock_urlopen(monkeypatch, b"<<bad>>")
        assert pc.collect_ppq_balance(api_key="k") is None

    def test_invalid_balance_none(self, monkeypatch, clean_env):
        _mock_urlopen(monkeypatch, json.dumps({"balance": "free"}).encode())
        assert pc.collect_ppq_balance(api_key="k") is None


class TestCollectAndStore:
    def test_persists_on_success(self, monkeypatch, db_path, clean_env):
        _mock_urlopen(monkeypatch, json.dumps({"balance": 8.0}).encode())
        b = pc.collect_and_store_ppq(db_path=db_path, api_key="k", starting=20.0)
        assert b is not None and b.balance == 8.0
        got = pc.get_latest_ppq_balance(db_path)
        assert got is not None and got.balance == 8.0

    def test_no_persist_on_failure(self, monkeypatch, db_path, clean_env):
        _mock_urlopen(monkeypatch, b"", exc=urllib.error.URLError("x"))
        assert pc.collect_and_store_ppq(db_path=db_path, api_key="k") is None
        assert pc.get_latest_ppq_balance(db_path) is None


# ── Cron CLI ─────────────────────────────────────────────────────────────────

class TestCLI:
    def test_success_exit0(self, monkeypatch, db_path, clean_env, capsys):
        _mock_urlopen(monkeypatch, json.dumps({"balance": 9.0}).encode())
        monkeypatch.setenv("PPQ_API_KEY", "k")
        rc = pc.main(["--provider", "ppq", "--db", db_path])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True
        assert out["balance"] == 9.0
        assert out["provider"] == "ppq"

    def test_failure_exit1(self, monkeypatch, db_path, clean_env, capsys):
        rc = pc.main(["--provider", "ppq", "--db", db_path])  # no key
        assert rc == 1
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is False
        assert "PPQ_API_KEY" in out["error"]


# ── Pressure-consumer compatibility ──────────────────────────────────────────

class TestPressureCompat:
    def test_usage_fraction_feeds_curve(self):
        from src.pricing_engine import (
            quota_pressure_factor, PPQ_QUOTA_PRESSURE_ONSET, PPQ_QUOTA_PRESSURE_ASYMPTOTE,
        )
        b = pc.parse_ppq_balance({"balance": 2.0}, 20.0)  # 90% spent
        assert b is not None
        factor = quota_pressure_factor(
            b.usage_fraction, onset=PPQ_QUOTA_PRESSURE_ONSET,
            asymptote=PPQ_QUOTA_PRESSURE_ASYMPTOTE, hard_limit=True,
        )
        assert b.usage_fraction == pytest.approx(0.9)
        assert factor > 1.0  # past onset 0.80 → pressure active

    def test_used_pct_matches_live_router(self):
        b = pc.parse_ppq_balance({"balance": 2.0}, 20.0)
        assert b is not None
        # live_router._compute_ppq_pressure does u = used_pct / 100.0
        assert b.used_pct == pytest.approx(90.0)
        assert b.used_pct / 100.0 == pytest.approx(b.usage_fraction)

    def test_exhausted_yields_inf(self):
        from src.pricing_engine import quota_pressure_factor
        b = pc.parse_ppq_balance({"balance": 0.0}, 20.0)
        assert b is not None
        factor = quota_pressure_factor(
            b.usage_fraction, onset=0.80, asymptote=1.5, hard_limit=True,
        )
        assert factor == float("inf")  # no credits → breaker trips


# ── Bridge to quota_state['ppq'] (P3-PPQ STEP 3 + STEP 4) ────────────────────
# End-to-end: stored balance → ppq_quota_entry() → live_router._compute_ppq_pressure.
# These exercise the REAL consumer (not quota_pressure_factor directly), proving
# the collector → quota_state → pressure wiring for the two regimes the task pins:
#   balance=0  (credits gone)   → +inf
#   balance=full (nothing spent) → 1.0

def _store(db_path, balance, starting=20.0, collected_at=None):
    """Store one PPQ balance row and return it parsed."""
    b = pc.parse_ppq_balance({"balance": balance}, starting)
    assert b is not None
    if collected_at is not None:
        b.collected_at = collected_at
    assert pc.store_ppq_balance(db_path, b)
    return b


class TestBridge:
    def test_exhausted_balance_yields_inf(self, db_path):
        """STEP 4: balance=0 → ppq_quota_entry → _compute_ppq_pressure == +inf."""
        from src.live_router import _compute_ppq_pressure
        _store(db_path, 0.0)
        entry = pc.ppq_quota_entry(db_path)
        assert "used_pct" in entry
        assert entry["is_exhausted"] is True
        assert entry["used_pct"] == pytest.approx(100.0)
        assert _compute_ppq_pressure(entry) == float("inf")

    def test_full_balance_yields_baseline(self, db_path):
        """STEP 4: balance == starting (nothing spent) → pressure == 1.0."""
        from src.live_router import _compute_ppq_pressure
        _store(db_path, 20.0, starting=20.0)
        entry = pc.ppq_quota_entry(db_path)
        assert entry["used_pct"] == pytest.approx(0.0)
        assert entry["is_exhausted"] is False
        assert _compute_ppq_pressure(entry) == pytest.approx(1.0)

    def test_negative_balance_yields_inf(self, db_path):
        """Negative balance (credit overrun — what the live API actually returns
        for an overdrawn account) is treated as exhausted → +inf."""
        from src.live_router import _compute_ppq_pressure
        _store(db_path, -0.0026, starting=20.0)
        entry = pc.ppq_quota_entry(db_path)
        assert entry["is_exhausted"] is True
        assert _compute_ppq_pressure(entry) == float("inf")

    def test_mid_balance_ramps_past_onset(self, db_path):
        """balance=2 of 20 → 90% used → pressure > 1.0 (past onset 0.80)."""
        from src.live_router import _compute_ppq_pressure
        _store(db_path, 2.0, starting=20.0)
        entry = pc.ppq_quota_entry(db_path)
        assert entry["used_pct"] == pytest.approx(90.0)
        assert _compute_ppq_pressure(entry) > 1.0

    def test_no_data_is_cold_start(self, db_path):
        """No stored row → ppq_quota_entry() == {} → conservative cold_start (>1.0)."""
        from src.live_router import _compute_ppq_pressure
        from src.pricing_engine import (
            cold_start_pressure, PPQ_QUOTA_PRESSURE_ASYMPTOTE,
        )
        entry = pc.ppq_quota_entry(db_path)
        assert entry == {}  # cold-start marker: no used_pct key
        expected = cold_start_pressure(
            asymptote=PPQ_QUOTA_PRESSURE_ASYMPTOTE, hard_limit=True,
        )
        assert _compute_ppq_pressure(entry) == pytest.approx(expected)
        assert _compute_ppq_pressure(entry) > 1.0

    def test_stale_row_is_cold_start(self, db_path):
        """A row older than max_age (collector stopped) → cold-start {} entry."""
        import time as _time
        from src.live_router import _compute_ppq_pressure
        from src.pricing_engine import (
            cold_start_pressure, PPQ_QUOTA_PRESSURE_ASYMPTOTE,
        )
        _store(db_path, 10.0, collected_at=_time.time() - 3600)  # 1h old → stale
        entry = pc.ppq_quota_entry(db_path)  # default max_age = 1200s
        assert entry == {}
        expected = cold_start_pressure(
            asymptote=PPQ_QUOTA_PRESSURE_ASYMPTOTE, hard_limit=True,
        )
        assert _compute_ppq_pressure(entry) == pytest.approx(expected)

    def test_stale_disabled_uses_newest(self, db_path):
        """max_age=None ignores staleness and returns the row regardless of age."""
        import time as _time
        _store(db_path, 10.0, collected_at=_time.time() - 999999)
        entry = pc.ppq_quota_entry(db_path, max_age=None)
        assert "used_pct" in entry
        assert entry["used_pct"] == pytest.approx(50.0)

    def test_bridge_never_raises_on_bad_db(self, tmp_path):
        """A garbage/unreachable db_path must not raise — returns cold-start {}."""
        entry = pc.ppq_quota_entry(str(tmp_path / "nope" / "deep" / "x.db"))
        assert entry == {}

    def test_entry_shape_matches_snapshot_contract(self, db_path):
        """The entry carries the fields _snapshot_quota / diagnostics expect."""
        _store(db_path, 8.0, starting=20.0)
        entry = pc.ppq_quota_entry(db_path)
        for k in ("used_pct", "remaining", "starting", "is_exhausted", "collected_at"):
            assert k in entry, f"missing {k}"
        assert entry["remaining"] == pytest.approx(8.0)
        assert entry["starting"] == pytest.approx(20.0)
        assert entry["used_pct"] == pytest.approx(60.0)
        assert isinstance(entry["collected_at"], float)
