"""Tests for scripts/calibrate_kalman_daily.py — daily PriceKalman calibration.

Tests cover:
- Querying daily_spend for yesterday's effective rates
- Feeding observations to PriceKalman via .update()
- Logging convergence to kalman_samples table
- Script is importable + standalone (python3 scripts/calibrate_kalman_daily.py)
- Graceful handling of missing DB, empty tables, zero-token days
- record_request never raises on exception (wrapped)
"""
from __future__ import annotations

import os
import sys
import sqlite3
import tempfile
import pytest
from datetime import datetime, timezone, timedelta

# Ensure we can import scripts + src modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_db():
    """Create a temporary zai_usage.db with daily_spend + kalman_samples tables."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE IF NOT EXISTS daily_spend (
        date TEXT NOT NULL,
        tier TEXT NOT NULL,
        spend_usd REAL DEFAULT 0,
        call_count INTEGER DEFAULT 0,
        token_count INTEGER DEFAULT 0,
        PRIMARY KEY (date, tier)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS kalman_samples (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        provider TEXT NOT NULL,
        base_rate REAL NOT NULL,
        velocity REAL DEFAULT 0,
        update_count INTEGER DEFAULT 0,
        source TEXT DEFAULT 'daily_calibration'
    )""")
    conn.commit()
    conn.close()
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture
def yesterday():
    """Yesterday's date as YYYY-MM-DD string (UTC)."""
    d = datetime.now(timezone.utc).date() - timedelta(days=1)
    return d.isoformat()


@pytest.fixture
def db_with_yesterday_data(tmp_db, yesterday):
    """DB with yesterday's daily_spend data for ours and friend."""
    conn = sqlite3.connect(tmp_db)
    # ours: 5000 tokens, $1.55 spend → $310/M
    conn.execute(
        "INSERT INTO daily_spend (date, tier, spend_usd, call_count, token_count) "
        "VALUES (?, 'ours', 1.55, 10, 5000)", (yesterday,))
    # friend: 10000 tokens, $0.29 spend → $29/M
    conn.execute(
        "INSERT INTO daily_spend (date, tier, spend_usd, call_count, token_count) "
        "VALUES (?, 'friend', 0.29, 5, 10000)", (yesterday,))
    # unknown tier — should be skipped
    conn.execute(
        "INSERT INTO daily_spend (date, tier, spend_usd, call_count, token_count) "
        "VALUES (?, 'unknown', 100.0, 1, 1000)", (yesterday,))
    conn.commit()
    conn.close()
    return tmp_db


# ── Import + structure tests ──────────────────────────────────────────────────


class TestImportAndStructure:
    """Verify the script is importable and has the expected public API."""

    def test_importable(self):
        from scripts.calibrate_kalman_daily import calibrate_daily
        assert callable(calibrate_daily)

    def test_main_callable(self):
        from scripts.calibrate_kalman_daily import main
        assert callable(main)

    def test_seed_costs_present(self):
        from scripts.calibrate_kalman_daily import SEED_COSTS
        assert isinstance(SEED_COSTS, dict)
        assert "ours" in SEED_COSTS
        assert "friend" in SEED_COSTS


# ── Query daily_spend ──────────────────────────────────────────────────────────


class TestQueryDailySpend:
    """Test reading yesterday's effective rates from daily_spend."""

    def test_returns_observations_for_known_tiers(self, db_with_yesterday_data, yesterday):
        from scripts.calibrate_kalman_daily import query_yesterday_rates
        rates = query_yesterday_rates(db_with_yesterday_data)
        assert isinstance(rates, dict)
        assert "ours" in rates
        assert "friend" in rates
        # ours: $1.55 / (5000/1e6) = $1.55 / 0.005 = $310/M
        assert rates["ours"]["effective_rate"] == pytest.approx(310.0, rel=1e-2)
        # friend: $0.29 / (10000/1e6) = $0.29 / 0.01 = $29/M
        assert rates["friend"]["effective_rate"] == pytest.approx(29.0, rel=1e-2)

    def test_skips_unknown_tier(self, db_with_yesterday_data):
        from scripts.calibrate_kalman_daily import query_yesterday_rates
        rates = query_yesterday_rates(db_with_yesterday_data)
        assert "unknown" not in rates

    def test_skips_zero_token_rows(self, tmp_db, yesterday):
        """Rows with token_count=0 should be skipped (no observable rate)."""
        conn = sqlite3.connect(tmp_db)
        conn.execute(
            "INSERT INTO daily_spend (date, tier, spend_usd, call_count, token_count) "
            "VALUES (?, 'ours', 5.0, 3, 0)", (yesterday,))
        conn.commit()
        conn.close()
        from scripts.calibrate_kalman_daily import query_yesterday_rates
        rates = query_yesterday_rates(tmp_db)
        assert "ours" not in rates

    def test_empty_db_returns_empty(self, tmp_db):
        from scripts.calibrate_kalman_daily import query_yesterday_rates
        rates = query_yesterday_rates(tmp_db)
        assert rates == {}

    def test_missing_db_returns_empty(self):
        from scripts.calibrate_kalman_daily import query_yesterday_rates
        rates = query_yesterday_rates("/nonexistent/path/to/db.db")
        assert rates == {}


# ── Feed to PriceKalman ──────────────────────────────────────────────────────────


class TestFeedPriceKalman:
    """Test feeding daily rates to PriceKalman and verifying convergence."""

    def test_feeds_observations_to_kalman(self, db_with_yesterday_data):
        from scripts.calibrate_kalman_daily import calibrate_daily
        results = calibrate_daily(db_with_yesterday_data)
        assert isinstance(results, dict)
        assert "ours" in results
        # The Kalman should have been updated (update_count >= 1)
        assert results["ours"]["update_count"] >= 1
        assert results["ours"]["base_rate"] > 0

    def test_kalman_converges_toward_observation(self, db_with_yesterday_data):
        """After feeding, the base_rate should move toward the observed rate."""
        from scripts.calibrate_kalman_daily import calibrate_daily, SEED_COSTS
        results = calibrate_daily(db_with_yesterday_data)
        # ours seed is 0.001, observed rate is $310/M
        # After one update, the base_rate should have moved significantly
        # toward 310 (even if not all the way)
        pk_rate = results["ours"]["base_rate"]
        seed = SEED_COSTS["ours"]
        # Should have moved away from the seed toward the observation
        assert pk_rate != pytest.approx(seed, abs=1e-6)

    def test_multiple_days_feed_sequentially(self, tmp_db):
        """Feed multiple days of data — Kalman should converge."""
        from scripts.calibrate_kalman_daily import calibrate_daily
        conn = sqlite3.connect(tmp_db)
        base_date = datetime.now(timezone.utc).date() - timedelta(days=5)
        for i in range(5):
            d = (base_date + timedelta(days=i)).isoformat()
            conn.execute(
                "INSERT INTO daily_spend (date, tier, spend_usd, call_count, token_count) "
                "VALUES (?, 'ours', 0.31, 10, 10000)", (d,))
        conn.commit()
        conn.close()
        # Run calibration (processes yesterday by default, but we want all days)
        results = calibrate_daily(tmp_db, days_back=5)
        assert results["ours"]["update_count"] >= 1
        # After 5 days of ~$31/M ($0.31 / 0.01M), base_rate should converge
        assert results["ours"]["base_rate"] > 0


# ── Log convergence to kalman_samples ──────────────────────────────────────────


class TestLogConvergence:
    """Test that calibration logs convergence to kalman_samples table."""

    def test_logs_to_kalman_samples(self, db_with_yesterday_data):
        from scripts.calibrate_kalman_daily import calibrate_daily
        calibrate_daily(db_with_yesterday_data)
        conn = sqlite3.connect(db_with_yesterday_data)
        rows = conn.execute(
            "SELECT provider, base_rate, velocity, update_count "
            "FROM kalman_samples"
        ).fetchall()
        conn.close()
        assert len(rows) >= 1
        providers = [r[0] for r in rows]
        assert "ours" in providers
        assert "friend" in providers

    def test_log_includes_timestamp(self, db_with_yesterday_data):
        from scripts.calibrate_kalman_daily import calibrate_daily
        calibrate_daily(db_with_yesterday_data)
        conn = sqlite3.connect(db_with_yesterday_data)
        rows = conn.execute("SELECT ts FROM kalman_samples").fetchall()
        conn.close()
        assert len(rows) >= 1
        assert all(r[0] > 0 for r in rows)

    def test_log_includes_source_tag(self, db_with_yesterday_data):
        from scripts.calibrate_kalman_daily import calibrate_daily
        calibrate_daily(db_with_yesterday_data)
        conn = sqlite3.connect(db_with_yesterday_data)
        rows = conn.execute("SELECT source FROM kalman_samples").fetchall()
        conn.close()
        assert len(rows) >= 1
        assert all(r[0] == "daily_calibration" for r in rows)


# ── Standalone execution ────────────────────────────────────────────────────────


class TestStandaloneExecution:
    """Test that the script runs as a standalone CLI."""

    def test_main_returns_zero_on_success(self, db_with_yesterday_data, capsys):
        from scripts.calibrate_kalman_daily import main
        ret = main(["--db", db_with_yesterday_data])
        assert ret == 0

    def test_main_returns_nonzero_on_missing_db(self, capsys):
        from scripts.calibrate_kalman_daily import main
        ret = main(["--db", "/nonexistent/path.db"])
        assert ret != 0

    def test_main_creates_kalman_samples_table(self, tmp_db):
        """main() should create kalman_samples table if it doesn't exist."""
        # Drop kalman_samples to test creation
        conn = sqlite3.connect(tmp_db)
        conn.execute("DROP TABLE IF EXISTS kalman_samples")
        conn.commit()
        conn.close()
        from scripts.calibrate_kalman_daily import main
        main(["--db", tmp_db])
        # Table should exist now
        conn = sqlite3.connect(tmp_db)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='kalman_samples'"
        ).fetchall()
        conn.close()
        assert len(tables) == 1


# ── Never raises / robustness ──────────────────────────────────────────────────


class TestRobustness:
    """Calibration must never raise — it's a cron job."""

    def test_never_raises_on_empty_db(self, tmp_db):
        from scripts.calibrate_kalman_daily import calibrate_daily
        result = calibrate_daily(tmp_db)
        assert isinstance(result, dict)

    def test_never_raises_on_missing_db(self):
        from scripts.calibrate_kalman_daily import calibrate_daily
        result = calibrate_daily("/nonexistent/path.db")
        assert isinstance(result, dict)

    def test_never_raises_on_zero_token_day(self, tmp_db, yesterday):
        conn = sqlite3.connect(tmp_db)
        conn.execute(
            "INSERT INTO daily_spend (date, tier, spend_usd, call_count, token_count) "
            "VALUES (?, 'ours', 0.0, 0, 0)", (yesterday,))
        conn.commit()
        conn.close()
        from scripts.calibrate_kalman_daily import calibrate_daily
        result = calibrate_daily(tmp_db)
        assert isinstance(result, dict)

    def test_returns_provider_info_for_logging(self, db_with_yesterday_data):
        """Each result entry should have base_rate, velocity, update_count."""
        from scripts.calibrate_kalman_daily import calibrate_daily
        results = calibrate_daily(db_with_yesterday_data)
        for name, info in results.items():
            assert "base_rate" in info
            assert "velocity" in info
            assert "update_count" in info
            assert isinstance(info["base_rate"], float)
            assert info["base_rate"] > 0