"""Tests for the Telnyx self-tracking balance collector (src/balance_collectors.py).

The Telnyx collector is a "self-tracking" collector (pitfall #47: no balance
API). Instead of querying an external endpoint, it sums ``cost_usd`` from the
local ``api_calls`` table in api_burn.db:

    1) SELECT SUM(cost_usd) FROM api_calls WHERE key_name='telnyx'
    2) remaining = TELNYX_STARTING_BALANCE - sum_spent
    3) usage_fraction = 1 - (remaining / TELNYX_STARTING_BALANCE)
    4) Write to provider_balances table (provider='telnyx')

Covers:
  * usage_fraction derivation for all edge cases:
      - fresh account (no spend)           -> 0.0
      - half spent                          -> 0.5
      - fully exhausted (remaining == 0)    -> 1.0
      - overrun (remaining < 0)            -> 1.0
      - starting <= 0 (misconfig)           -> 0.0
      - remaining None                      -> 0.0
  * collect_telnyx_balance with a real tmp SQLite DB:
      - happy path with spend rows
      - empty DB (no api_calls table)       -> spends 0.0
      - non-telnyx rows excluded
      - starting balance from env
      - starting balance from explicit arg
  * never-raises invariant against bad DB paths
  * SQLite round-trip: store -> get_latest, idempotent table, time-series
  * collect_and_store_telnyx (cron-friendly)
  * telnyx_quota_entry bridge with cold-start contract
  * CLI main() dispatch for --provider telnyx
"""
from __future__ import annotations

import json
import sqlite3
import time
from unittest.mock import patch

import pytest

from src.balance_collectors import (
    TELNYX_DEFAULT_STARTING_BALANCE,
    TELNYX_STARTING_ENV,
    TelnyxBalance,
    _telnyx_usage_fraction,
    collect_and_store_telnyx,
    collect_telnyx_balance,
    default_db_path,
    get_latest_telnyx_balance,
    main,
    store_telnyx_balance,
    telnyx_quota_entry,
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_db(tmp_path, rows=None):
    """Create a tmp SQLite DB with api_calls table and optional rows.

    Each row is (ts, key_name, cost_usd).
    """
    db = str(tmp_path / "burn.db")
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE api_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            key_name TEXT,
            model TEXT,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            cost_usd REAL,
            cost_source TEXT
        )"""
    )
    for row in rows or []:
        conn.execute(
            "INSERT INTO api_calls (ts, key_name, cost_usd) VALUES (?, ?, ?)",
            row,
        )
    conn.commit()
    conn.close()
    return db


# ════════════════════════════════════════════════════════════════════════════
# usage_fraction derivation
# ════════════════════════════════════════════════════════════════════════════

class TestUsageFraction:
    def test_fresh_no_spend(self):
        # remaining == starting -> fraction 0
        assert _telnyx_usage_fraction(10.0, 10.0) == pytest.approx(0.0)

    def test_half_spent(self):
        # remaining 5, starting 10 -> 0.5
        assert _telnyx_usage_fraction(5.0, 10.0) == pytest.approx(0.5)

    def test_quarter_spent(self):
        assert _telnyx_usage_fraction(7.5, 10.0) == pytest.approx(0.25)

    def test_exhausted_zero(self):
        assert _telnyx_usage_fraction(0.0, 10.0) == 1.0

    def test_overrun_negative(self):
        assert _telnyx_usage_fraction(-2.0, 10.0) == 1.0

    def test_starting_zero_misconfig(self):
        assert _telnyx_usage_fraction(5.0, 0.0) == 0.0

    def test_starting_negative_misconfig(self):
        assert _telnyx_usage_fraction(5.0, -1.0) == 0.0

    def test_remaining_none(self):
        assert _telnyx_usage_fraction(None, 10.0) == 0.0

    def test_clamp_above_one(self):
        # remaining > starting (negative spend? shouldn't happen but defensive)
        assert _telnyx_usage_fraction(15.0, 10.0) == 0.0

    def test_within_unit_interval(self):
        for rem, start in [(9.0, 10.0), (1.0, 10.0), (0.1, 10.0)]:
            frac = _telnyx_usage_fraction(rem, start)
            assert 0.0 <= frac <= 1.0


# ════════════════════════════════════════════════════════════════════════════
# collect_telnyx_balance
# ════════════════════════════════════════════════════════════════════════════

class TestCollectTelnyx:
    def test_happy_path_with_spend(self, tmp_path):
        db = _make_db(tmp_path, [
            (time.time(), "telnyx", 0.50),
            (time.time(), "telnyx", 0.30),
            (time.time(), "openrouter", 5.0),  # should be excluded
        ])
        bal = collect_telnyx_balance(starting=10.0, db_path=db)
        assert bal.ok
        assert bal.error is None
        assert bal.total_spent_usd == pytest.approx(0.80)
        assert bal.remaining_usd == pytest.approx(9.20)
        assert bal.usage_fraction == pytest.approx(0.08)
        assert bal.is_exhausted is False
        assert bal.used_pct == pytest.approx(8.0)

    def test_empty_db_no_rows(self, tmp_path):
        db = _make_db(tmp_path)
        bal = collect_telnyx_balance(starting=10.0, db_path=db)
        assert bal.ok
        assert bal.total_spent_usd == pytest.approx(0.0)
        assert bal.remaining_usd == pytest.approx(10.0)
        assert bal.usage_fraction == pytest.approx(0.0)
        assert bal.is_exhausted is False

    def test_non_telnyx_rows_excluded(self, tmp_path):
        db = _make_db(tmp_path, [
            (time.time(), "deepinfra", 3.0),
            (time.time(), "ppq", 1.0),
            (time.time(), "openrouter", 2.0),
        ])
        bal = collect_telnyx_balance(starting=10.0, db_path=db)
        assert bal.ok
        assert bal.total_spent_usd == pytest.approx(0.0)

    def test_exhausted(self, tmp_path):
        db = _make_db(tmp_path, [
            (time.time(), "telnyx", 10.0),
        ])
        bal = collect_telnyx_balance(starting=10.0, db_path=db)
        assert bal.ok
        assert bal.total_spent_usd == pytest.approx(10.0)
        assert bal.remaining_usd == pytest.approx(0.0)
        assert bal.usage_fraction == 1.0
        assert bal.is_exhausted is True
        assert bal.used_pct == 100.0

    def test_overrun(self, tmp_path):
        db = _make_db(tmp_path, [
            (time.time(), "telnyx", 12.0),
        ])
        bal = collect_telnyx_balance(starting=10.0, db_path=db)
        assert bal.ok
        assert bal.total_spent_usd == pytest.approx(12.0)
        assert bal.remaining_usd == pytest.approx(-2.0)
        assert bal.usage_fraction == 1.0
        assert bal.is_exhausted is True

    def test_starting_from_env(self, tmp_path, monkeypatch):
        db = _make_db(tmp_path, [
            (time.time(), "telnyx", 2.0),
        ])
        monkeypatch.setenv(TELNYX_STARTING_ENV, "20.0")
        bal = collect_telnyx_balance(db_path=db)  # no explicit starting
        assert bal.ok
        assert bal.starting == 20.0
        assert bal.remaining_usd == pytest.approx(18.0)
        assert bal.usage_fraction == pytest.approx(0.1)

    def test_starting_env_zero(self, tmp_path, monkeypatch):
        """TELNYX_STARTING_BALANCE=0 should be treated as misconfig → usage_fraction 0."""
        db = _make_db(tmp_path, [
            (time.time(), "telnyx", 1.0),
        ])
        monkeypatch.setenv(TELNYX_STARTING_ENV, "0")
        bal = collect_telnyx_balance(db_path=db)
        assert bal.ok
        assert bal.starting == 0.0
        # starting <= 0 → usage_fraction = 0.0 (cold-start path)
        assert bal.usage_fraction == 0.0

    def test_starting_env_garbage_falls_back(self, tmp_path, monkeypatch):
        db = _make_db(tmp_path)
        monkeypatch.setenv(TELNYX_STARTING_ENV, "not-a-number")
        bal = collect_telnyx_balance(db_path=db)
        assert bal.ok
        assert bal.starting == TELNYX_DEFAULT_STARTING_BALANCE

    def test_starting_env_empty_falls_back(self, tmp_path, monkeypatch):
        db = _make_db(tmp_path)
        monkeypatch.setenv(TELNYX_STARTING_ENV, "")
        bal = collect_telnyx_balance(db_path=db)
        assert bal.ok
        assert bal.starting == TELNYX_DEFAULT_STARTING_BALANCE

    def test_starting_env_unset_uses_default(self, tmp_path, monkeypatch):
        db = _make_db(tmp_path)
        monkeypatch.delenv(TELNYX_STARTING_ENV, raising=False)
        bal = collect_telnyx_balance(db_path=db)
        assert bal.ok
        assert bal.starting == TELNYX_DEFAULT_STARTING_BALANCE

    def test_explicit_starting_overrides_env(self, tmp_path, monkeypatch):
        db = _make_db(tmp_path, [
            (time.time(), "telnyx", 1.0),
        ])
        monkeypatch.setenv(TELNYX_STARTING_ENV, "5.0")
        bal = collect_telnyx_balance(starting=100.0, db_path=db)
        assert bal.ok
        assert bal.starting == 100.0
        assert bal.remaining_usd == pytest.approx(99.0)

    def test_no_api_calls_table_creates_it(self, tmp_path):
        """A fresh DB with no api_calls table should still work — the collector
        creates it defensively and gets SUM=0."""
        db = str(tmp_path / "fresh.db")
        bal = collect_telnyx_balance(starting=10.0, db_path=db)
        assert bal.ok
        assert bal.total_spent_usd == pytest.approx(0.0)
        assert bal.remaining_usd == pytest.approx(10.0)


# ════════════════════════════════════════════════════════════════════════════
# never-raises invariant
# ════════════════════════════════════════════════════════════════════════════

class TestNeverRaises:
    def test_bad_db_path_does_not_raise(self, tmp_path):
        bal = collect_telnyx_balance(starting=10.0, db_path="/no/such/dir/x/db.db")
        assert bal.ok is False
        assert bal.error is not None
        assert bal.total_spent_usd is None

    def test_bad_db_path_store_does_not_raise(self, tmp_path):
        b = TelnyxBalance(
            total_spent_usd=1.0, starting=10.0, remaining_usd=9.0,
            usage_fraction=0.1, is_exhausted=False,
        )
        assert store_telnyx_balance("/no/such/dir/x/db.db", b) is False

    def test_store_none_returns_false(self, tmp_path):
        db = str(tmp_path / "bal.db")
        assert store_telnyx_balance(db, None) is False

    def test_get_latest_bad_db_returns_none(self, tmp_path):
        assert get_latest_telnyx_balance("/no/such/dir/x/db.db") is None

    def test_quota_entry_bad_db_returns_empty(self, tmp_path):
        assert telnyx_quota_entry("/no/such/dir/x/db.db") == {}


# ════════════════════════════════════════════════════════════════════════════
# SQLite persistence
# ════════════════════════════════════════════════════════════════════════════

class TestStorage:
    def test_store_and_read_back(self, tmp_path):
        db = str(tmp_path / "bal.db")
        b = TelnyxBalance(
            total_spent_usd=2.0,
            starting=10.0,
            remaining_usd=8.0,
            usage_fraction=0.2,
            is_exhausted=False,
        )
        assert store_telnyx_balance(db, b) is True

        got = get_latest_telnyx_balance(db)
        assert got is not None
        assert got.total_spent_usd == pytest.approx(2.0)
        assert got.starting == pytest.approx(10.0)
        assert got.remaining_usd == pytest.approx(8.0)
        assert got.usage_fraction == pytest.approx(0.2)
        assert got.is_exhausted is False

    def test_table_creation_idempotent(self, tmp_path):
        db = str(tmp_path / "bal.db")
        b = TelnyxBalance(1.0, 10.0, 9.0, 0.1, False)
        store_telnyx_balance(db, b)
        store_telnyx_balance(db, b)
        conn = sqlite3.connect(db)
        tabs = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        conn.close()
        assert tabs.count("provider_balances") == 1

    def test_time_series_latest_wins(self, tmp_path):
        db = str(tmp_path / "bal.db")
        old = TelnyxBalance(5.0, 10.0, 5.0, 0.5, False,
                            collected_at=time.time() - 100)
        new = TelnyxBalance(9.0, 10.0, 1.0, 0.9, False,
                            collected_at=time.time())
        store_telnyx_balance(db, old)
        store_telnyx_balance(db, new)
        got = get_latest_telnyx_balance(db)
        assert got is not None
        assert got.usage_fraction == pytest.approx(0.9)
        assert got.total_spent_usd == pytest.approx(9.0)

    def test_get_latest_empty_returns_none(self, tmp_path):
        db = str(tmp_path / "bal.db")
        assert get_latest_telnyx_balance(db) is None


# ════════════════════════════════════════════════════════════════════════════
# collect_and_store_telnyx (cron-friendly)
# ════════════════════════════════════════════════════════════════════════════

class TestCollectAndStore:
    def test_success_stores_and_returns(self, tmp_path):
        db = _make_db(tmp_path, [
            (time.time(), "telnyx", 4.0),
        ])
        bal = collect_and_store_telnyx(db_path=db, starting=10.0)
        assert bal is not None
        assert bal.ok
        assert bal.total_spent_usd == pytest.approx(4.0)
        assert bal.usage_fraction == pytest.approx(0.4)

        got = get_latest_telnyx_balance(db)
        assert got is not None
        assert got.usage_fraction == pytest.approx(0.4)

    def test_failure_returns_none_no_store(self, tmp_path):
        bal = collect_and_store_telnyx(
            db_path="/no/such/dir/x/db.db", starting=10.0
        )
        assert bal is None


# ════════════════════════════════════════════════════════════════════════════
# telnyx_quota_entry bridge
# ════════════════════════════════════════════════════════════════════════════

class TestQuotaEntry:
    def test_cold_start_no_rows(self, tmp_path):
        db = str(tmp_path / "bal.db")
        assert telnyx_quota_entry(db) == {}

    def test_fresh_row_returns_entry(self, tmp_path):
        db = str(tmp_path / "bal.db")
        b = TelnyxBalance(
            total_spent_usd=3.0,
            starting=10.0,
            remaining_usd=7.0,
            usage_fraction=0.3,
            is_exhausted=False,
            collected_at=time.time(),
        )
        store_telnyx_balance(db, b)
        entry = telnyx_quota_entry(db)
        assert "used_pct" in entry
        assert entry["used_pct"] == pytest.approx(30.0)
        assert entry["remaining"] == pytest.approx(7.0)
        assert entry["starting"] == pytest.approx(10.0)
        assert entry["is_exhausted"] is False

    def test_stale_row_returns_empty(self, tmp_path):
        db = str(tmp_path / "bal.db")
        b = TelnyxBalance(
            total_spent_usd=3.0,
            starting=10.0,
            remaining_usd=7.0,
            usage_fraction=0.3,
            is_exhausted=False,
            collected_at=time.time() - 99999.0,  # very old
        )
        store_telnyx_balance(db, b)
        assert telnyx_quota_entry(db) == {}

    def test_stale_row_with_max_age_none(self, tmp_path):
        db = str(tmp_path / "bal.db")
        b = TelnyxBalance(
            total_spent_usd=3.0,
            starting=10.0,
            remaining_usd=7.0,
            usage_fraction=0.3,
            is_exhausted=False,
            collected_at=time.time() - 99999.0,
        )
        store_telnyx_balance(db, b)
        entry = telnyx_quota_entry(db, max_age=None)
        assert "used_pct" in entry
        assert entry["used_pct"] == pytest.approx(30.0)


# ════════════════════════════════════════════════════════════════════════════
# CLI main()
# ════════════════════════════════════════════════════════════════════════════

class TestMain:
    def test_success_returns_0(self, tmp_path, monkeypatch, capsys):
        db = _make_db(tmp_path, [
            (time.time(), "telnyx", 1.0),
        ])
        monkeypatch.setenv(TELNYX_STARTING_ENV, "10.0")
        rc = main(["--provider", "telnyx", "--db", db])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True
        assert out["provider"] == "telnyx"
        assert out["total_spent_usd"] == pytest.approx(1.0)
        assert out["usage_fraction"] == pytest.approx(0.1)
        assert out["used_pct"] == pytest.approx(10.0)
        assert out["db_path"] == db

    def test_failure_returns_1(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv(TELNYX_STARTING_ENV, "10.0")
        rc = main(["--provider", "telnyx", "--db", "/no/such/dir/x/db.db"])
        assert rc == 1
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is False
        assert out["provider"] == "telnyx"
        assert "error" in out

    def test_unknown_provider_returns_1(self, capsys):
        rc = main(["--provider", "bogus"])
        assert rc == 1
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is False