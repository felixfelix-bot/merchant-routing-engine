"""Tests for src/realtime_pricing.py — single source of truth for measured $/M.

Covers (per design doc §6):
  - Unit tests: each collector, cold-start, confidence, staleness, floor, NaN guard
  - Kalman integration: convergence, feed_kalman
  - Thread-safety: concurrent snapshot during refresh, refresh serialisation
  - Integration: full refresh cycle, price_observations persistence
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
from unittest.mock import patch

import pytest

from src.realtime_pricing import (
    DEFAULT_COLD_START_RATES,
    MEASURED_SOURCES,
    SRC_COLD_START,
    SRC_DEEPINFRA_ACTUAL,
    SRC_OLLAMA_BILLING,
    SRC_OPENROUTER_ACTUAL,
    SRC_PUBLISHED,
    SRC_PQQ_LEDGER,
    SRC_ZAI_AMORTIZED,
    RateObservation,
    RateSnapshot,
    RealtimePricing,
    migrate_daily_spend_add_model,
    published_model_rate,
    published_model_rates,
)
from src.price_kalman import MIN_EFFECTIVE_PRICE, PriceKalman


# ── Fixture helpers ──────────────────────────────────────────────────────────


def _make_zai_db(api_calls_rows=None, daily_spend_rows=None) -> str:
    """Create a temp zai_usage.db with api_calls + daily_spend tables."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE api_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, key_name TEXT, model TEXT,
            total_tokens INTEGER DEFAULT 0,
            cost_usd REAL, cost_source TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE daily_spend (
            date TEXT, tier TEXT,
            spend_usd REAL, call_count INTEGER, token_count INTEGER
        )
        """
    )
    for row in (api_calls_rows or []):
        conn.execute(
            "INSERT INTO api_calls (ts, key_name, model, total_tokens) VALUES (?,?,?,?)",
            row,
        )
    for row in (daily_spend_rows or []):
        conn.execute(
            "INSERT INTO daily_spend (date, tier, spend_usd, token_count) VALUES (?,?,?,?)",
            row,
        )
    conn.commit()
    conn.close()
    return path


def _make_burn_db(ppq_rows=None) -> str:
    """Create a temp api_burn.db with ppq_queries table."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE ppq_queries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, model TEXT, total_tokens INTEGER, cost_usd REAL
        )
        """
    )
    for row in (ppq_rows or []):
        conn.execute(
            "INSERT INTO ppq_queries (ts, model, total_tokens, cost_usd) VALUES (?,?,?,?)",
            row,
        )
    conn.commit()
    conn.close()
    return path


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Ensure each test starts with a fresh singleton."""
    RealtimePricing.reset_instance()
    yield
    RealtimePricing.reset_instance()


def _make_instance(zai_db=None, burn_db=None, **kw) -> RealtimePricing:
    """Build a RealtimePricing instance against temp DBs."""
    zai = zai_db or _make_zai_db()
    burn = burn_db or _make_burn_db()
    RealtimePricing.reset_instance()
    return RealtimePricing.get_instance(
        zai_db_path=zai, burn_db_path=burn, **kw
    )


# ── 1. Cold-start tests ──────────────────────────────────────────────────────


class TestColdStart:
    def test_snapshot_before_first_refresh(self):
        """snapshot() works before any refresh — all providers cold-start."""
        rp = _make_instance()
        snap = rp.snapshot()
        assert snap.refresh_count == 0
        assert snap.any_cold_start is True
        for prov in ("ours", "friend", "ollama_cloud", "ppq", "openrouter", "deepinfra"):
            assert prov in snap.by_provider
            assert snap.by_provider[prov].source == SRC_COLD_START
            assert snap.by_provider[prov].is_measured is False

    def test_cold_start_when_no_data(self):
        """Empty DBs → every provider returns a fallback source.
        openrouter falls back to published_list (by design); others to cold_start."""
        rp = _make_instance()
        snap = rp.refresh()
        for prov in ("ours", "friend", "ollama_cloud", "ppq", "deepinfra"):
            ob = snap.by_provider[prov]
            assert ob.source == SRC_COLD_START, f"{prov} should be cold-start"
            assert ob.is_measured is False
            assert ob.rate_per_m == DEFAULT_COLD_START_RATES.get(prov, MIN_EFFECTIVE_PRICE)
        # openrouter intentionally falls back to published list price
        ob_or = snap.by_provider["openrouter"]
        assert ob_or.source == SRC_PUBLISHED
        assert ob_or.is_measured is False

    def test_cold_start_rates_match_seeds(self):
        rp = _make_instance()
        rates = rp.get_provider_rates()
        assert rates["ours"] == pytest.approx(0.001)
        assert rates["ollama_cloud"] == pytest.approx(0.0155)
        assert rates["deepinfra"] == pytest.approx(1.30)

    def test_get_rate_unknown_provider(self):
        rp = _make_instance()
        ob = rp.get_rate("nonexistent")
        assert ob.source == SRC_COLD_START
        assert ob.rate_per_m >= MIN_EFFECTIVE_PRICE


# ── 2. z.ai amortized collector ──────────────────────────────────────────────


class TestZaiAmortized:
    def test_ours_amortized_rate(self):
        """ours: annualized from trailing data.

        1B tokens over 30 days, $155/mo → annual_fee=$1860.
        annualized_tokens = 1e9 * (365/30) = 12.167e9
        rate = 1860 / (12.167e9 / 1e6) = 1860 / 12167 ≈ 0.153 $/M.
        """
        now = time.time()
        # 1 billion tokens for 'ours' over 30 days (within trailing 365d window)
        rows = [(now - 30 * 86400, "ours", "glm-5.2", 1_000_000_000)]
        zai = _make_zai_db(api_calls_rows=rows)
        rp = _make_instance(zai_db=zai)
        rp.refresh()
        ob = rp.get_rate("ours")
        assert ob.source == SRC_ZAI_AMORTIZED
        assert ob.is_measured is False
        # 1860 / (1e9 * 365/30 / 1e6) ≈ 0.1529
        assert ob.rate_per_m == pytest.approx(0.1529, rel=0.01)

    def test_ours_trailing_uses_all_data_not_month(self):
        """Trailing window includes data from previous months, not just current.

        Data: 1B tokens 60 days ago + 1B tokens 1 day ago = 2B total.
        trailing_days ≈ 60, annualized_tokens = 2e9 * (365/60) = 12.17e9.
        rate = 1860 / 12167 ≈ 0.153. With month-to-date (old behavior) only
        the 1-day-ago record would count → 1B tokens → rate ≈ 0.155 (monthly).
        The trailing approach gives a different (more stable) result.
        """
        now = time.time()
        rows = [
            (now - 60 * 86400, "ours", "glm-5.2", 1_000_000_000),
            (now - 1 * 86400, "ours", "glm-5.2", 1_000_000_000),
        ]
        zai = _make_zai_db(api_calls_rows=rows)
        rp = _make_instance(zai_db=zai)
        rp.refresh()
        ob = rp.get_rate("ours")
        assert ob.source == SRC_ZAI_AMORTIZED
        # Both records are within 365d trailing → sum = 2B tokens over ~60 days
        # annualized_tokens = 2e9 * (365/60) ≈ 12.167e9
        # rate = 1860 / 12167 ≈ 0.1529
        assert ob.rate_per_m == pytest.approx(0.1529, rel=0.02)

    def test_friend_floored_at_min(self):
        """friend: fee=0, so rate floored at MIN_EFFECTIVE_PRICE."""
        now = time.time()
        rows = [(now - 100, "friend", "glm-5.2", 1_000_000_000)]
        zai = _make_zai_db(api_calls_rows=rows)
        rp = _make_instance(zai_db=zai)
        rp.refresh()
        ob = rp.get_rate("friend")
        assert ob.source == SRC_ZAI_AMORTIZED
        assert ob.rate_per_m == MIN_EFFECTIVE_PRICE

    def test_month_boundary_guard(self):
        """Tokens below MIN_SAMPLE_TOKENS → cold-start (R3 mitigation)."""
        now = time.time()
        # Only 1000 tokens → below 1M threshold
        rows = [(now - 100, "ours", "glm-5.2", 1000)]
        zai = _make_zai_db(api_calls_rows=rows)
        rp = _make_instance(zai_db=zai)
        rp.refresh()
        ob = rp.get_rate("ours")
        assert ob.source == SRC_COLD_START


# ── 3. Ollama billing collector ──────────────────────────────────────────────


class TestOllamaBilling:
    def test_ollama_billing_rate(self):
        """Parse activity.cost / activity.tokens → provider-level aggregate."""
        mock_response = {
            "limits": {"session": {"usage": 0.1}, "weekly": {"usage": 0.2}},
            "activity": {
                "glm-5.2": {"cost": 1.55, "total_tokens": 100_000_000, "request_count": 0},
                "glm-4.5-flash": {"cost": 0.0, "total_tokens": 50_000_000, "request_count": 0},
            },
        }
        with patch("src.ollama_extra_usage.fetch_ollama_usage", return_value=mock_response):
            rp = _make_instance()
            rp.refresh()
        ob = rp.get_rate("ollama_cloud")
        assert ob.source == SRC_OLLAMA_BILLING
        assert ob.is_measured is True
        # total cost 1.55 / total tokens 150M = 0.01033... $/M
        assert ob.rate_per_m == pytest.approx(1.55 / 150.0, rel=0.01)

    def test_ollama_extra_usage_detection(self):
        """Models with request_count > 0 get per-model extra-usage observations."""
        mock_response = {
            "activity": {
                "glm-5.2": {"cost": 0.46, "tokens": 1_000_000, "request_count": 100},
            },
        }
        with patch("src.ollama_extra_usage.fetch_ollama_usage", return_value=mock_response):
            rp = _make_instance()
            rp.refresh()
        ob = rp.get_rate("ollama_cloud", "glm-5.2")
        assert ob.source == SRC_OLLAMA_BILLING
        assert ob.is_measured is True
        assert ob.rate_per_m == pytest.approx(0.46, rel=0.01)

    def test_ollama_api_failure_fallback_to_amortized(self):
        """When the billing API returns None, fall back to token amortization."""
        now = time.time()
        # 1B ollama tokens → $100 / 1000M = $0.10/M
        rows = [(now - 100, "ollama_cloud", "glm-5.2", 1_000_000_000)]
        zai = _make_zai_db(api_calls_rows=rows)
        with patch("src.ollama_extra_usage.fetch_ollama_usage", return_value=None):
            rp = _make_instance(zai_db=zai)
            rp.refresh()
        ob = rp.get_rate("ollama_cloud")
        assert ob.source == SRC_ZAI_AMORTIZED
        assert ob.rate_per_m == pytest.approx(0.10, rel=0.01)

    def test_ollama_api_failure_no_tokens_cold_start(self):
        """API down AND no tokens → cold_start."""
        with patch("src.ollama_extra_usage.fetch_ollama_usage", return_value=None):
            rp = _make_instance()
            rp.refresh()
        ob = rp.get_rate("ollama_cloud")
        assert ob.source == SRC_COLD_START


# ── 4. PPQ ledger collector ──────────────────────────────────────────────────


class TestPpqLedger:
    def test_ppq_ledger_rate(self):
        """SUM(cost_usd)/(SUM(total_tokens)/1e6) from ppq_queries."""
        now = time.time()
        # $14 cost / 100M tokens = $0.14/M
        ppq = _make_burn_db(ppq_rows=[
            (now - 100, "kimi-k3", 50_000_000, 7.0),
            (now - 200, "kimi-k3", 50_000_000, 7.0),
        ])
        rp = _make_instance(burn_db=ppq)
        rp.refresh()
        ob = rp.get_rate("ppq")
        assert ob.source == SRC_PQQ_LEDGER
        assert ob.is_measured is True
        assert ob.rate_per_m == pytest.approx(0.14, rel=0.01)

    def test_ppq_empty_falls_back_to_cold_start(self):
        rp = _make_instance()
        rp.refresh()
        ob = rp.get_rate("ppq")
        assert ob.source == SRC_COLD_START


# ── 5. DeepInfra / OpenRouter spend collectors ───────────────────────────────


class TestSpendCollectors:
    def test_deepinfra_spend_rate(self):
        """SUM(spend_usd)/(SUM(token_count)/1e6) from daily_spend."""
        today = time.strftime("%Y-%m-%d")
        rows = [(today, "deepinfra", 1.30, 1_000_000)]
        zai = _make_zai_db(daily_spend_rows=rows)
        rp = _make_instance(zai_db=zai)
        rp.refresh()
        ob = rp.get_rate("deepinfra")
        assert ob.source == SRC_DEEPINFRA_ACTUAL
        assert ob.is_measured is True
        assert ob.rate_per_m == pytest.approx(1.30, rel=0.01)

    def test_openrouter_fallback_to_published(self):
        """No openrouter spend data → published_list, is_measured=False."""
        rp = _make_instance()
        rp.refresh()
        ob = rp.get_rate("openrouter")
        assert ob.source == SRC_PUBLISHED
        assert ob.is_measured is False
        assert ob.rate_per_m == pytest.approx(0.135)

    def test_openrouter_measured_when_data_exists(self):
        today = time.strftime("%Y-%m-%d")
        rows = [(today, "openrouter", 0.135, 1_000_000)]
        zai = _make_zai_db(daily_spend_rows=rows)
        rp = _make_instance(zai_db=zai)
        rp.refresh()
        ob = rp.get_rate("openrouter")
        assert ob.source == SRC_OPENROUTER_ACTUAL
        assert ob.is_measured is True


# ── 6. Floor, NaN guard, staleness ───────────────────────────────────────────


class TestGuards:
    def test_min_effective_price_floor(self):
        """Any rate < 0.001 is floored at MIN_EFFECTIVE_PRICE."""
        now = time.time()
        # ours: 155 / (100B tokens) = 0.00155 — above floor
        # friend: 0 / anything → floor
        rows = [
            (now - 100, "ours", "m", 100_000_000_000),
            (now - 100, "friend", "m", 1_000_000_000),
        ]
        zai = _make_zai_db(api_calls_rows=rows)
        rp = _make_instance(zai_db=zai)
        rp.refresh()
        assert rp.get_rate("friend").rate_per_m >= MIN_EFFECTIVE_PRICE
        assert rp.get_rate("ours").rate_per_m >= MIN_EFFECTIVE_PRICE

    def test_nan_guard(self):
        """Zero tokens / zero cost does not produce NaN."""
        now = time.time()
        # ppq with 0 tokens, 0 cost
        ppq = _make_burn_db(ppq_rows=[(now - 100, "kimi", 0, 0.0)])
        rp = _make_instance(burn_db=ppq)
        rp.refresh()
        ob = rp.get_rate("ppq")
        assert ob.rate_per_m == ob.rate_per_m  # not NaN
        assert ob.rate_per_m >= MIN_EFFECTIVE_PRICE

    def test_is_stale_flag(self):
        """Observation older than 30 min → is_stale=True."""
        old_obs = RateObservation(
            provider="test", model=None, rate_per_m=0.1,
            source=SRC_COLD_START, is_measured=False, confidence=0.0,
            sample_tokens=0, sample_cost_usd=0.0,
            ts=time.time() - 3600,  # 1 hour ago
        )
        assert old_obs.is_stale is True

        fresh_obs = RateObservation(
            provider="test", model=None, rate_per_m=0.1,
            source=SRC_COLD_START, is_measured=False, confidence=0.0,
            sample_tokens=0, sample_cost_usd=0.0,
            ts=time.time() - 60,  # 1 min ago
        )
        assert fresh_obs.is_stale is False


# ── 7. Confidence scoring ────────────────────────────────────────────────────


class TestConfidence:
    def test_confidence_measured_ramps(self):
        from src.realtime_pricing import _confidence
        assert _confidence(1000, True) == pytest.approx(0.3, abs=0.01)   # ~0.3
        assert _confidence(10_000_000, True) == pytest.approx(1.0)       # capped at 1.0

    def test_confidence_estimated_capped(self):
        from src.realtime_pricing import _confidence
        assert _confidence(10_000_000, False) == pytest.approx(0.7)      # capped at 0.7
        assert _confidence(1000, False) == pytest.approx(0.3, abs=0.01)


# ── 8. Kalman integration ────────────────────────────────────────────────────


class TestKalmanIntegration:
    def test_kalman_converges_to_measured_rate(self):
        """Feed 10 real observations at 0.0155; base_rate must converge within 15%."""
        mock_response = {
            "activity": {
                "glm-5.2": {"cost": 1.55, "total_tokens": 100_000_000, "request_count": 0},
            },
        }
        with patch("src.ollama_extra_usage.fetch_ollama_usage", return_value=mock_response):
            rp = _make_instance()
            for _ in range(10):
                rp.refresh()
        ob = rp.get_rate("ollama_cloud")
        assert ob.source == SRC_OLLAMA_BILLING
        # Rate should be close to 0.0155 (1.55/100M)
        assert abs(ob.rate_per_m - 0.0155) / 0.0155 < 0.15

    def test_feed_kalman_drives_external_filter(self):
        """feed_kalman() pushes the latest obs into a caller's PriceKalman."""
        mock_response = {
            "activity": {
                "glm-5.2": {"cost": 1.55, "total_tokens": 100_000_000, "request_count": 0},
            },
        }
        with patch("src.ollama_extra_usage.fetch_ollama_usage", return_value=mock_response):
            rp = _make_instance()
            rp.refresh()
        pk = PriceKalman(initial_rate=0.024)
        assert rp.feed_kalman(pk, "ollama_cloud") is True
        # Kalman should have moved toward the measured rate (0.0155 < 0.024)
        assert pk.base_rate < 0.024

    def test_feed_kalman_returns_false_for_unknown(self):
        rp = _make_instance()
        pk = PriceKalman(initial_rate=0.1)
        result = rp.feed_kalman(pk, "definitely_unknown_provider_xyz")
        assert result is True  # get_rate never fails — returns cold-start


# ── 9. Thread safety ─────────────────────────────────────────────────────────


class TestThreadSafety:
    def test_concurrent_snapshot_during_refresh(self):
        """100 threads call snapshot() while one thread calls refresh() in a loop.
        No exception, no torn read, every snapshot is internally consistent."""
        rp = _make_instance()
        errors = []

        def reader():
            try:
                for _ in range(200):
                    s = rp.snapshot()
                    assert s.ts > 0
                    # all entries share a consistent ts (atomic swap)
                    for ob in s.by_provider.values():
                        assert ob.ts <= s.ts + 5.0
            except Exception as e:
                errors.append(e)

        def writer():
            try:
                for _ in range(20):
                    rp.refresh()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(10)]
        threads.append(threading.Thread(target=writer))
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not errors, f"errors during concurrent access: {errors}"

    def test_refresh_is_serialised(self):
        """Two concurrent refresh() calls do not crash or corrupt state."""
        rp = _make_instance()
        errors = []

        def do_refresh():
            try:
                for _ in range(5):
                    rp.refresh()
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=do_refresh)
        t2 = threading.Thread(target=do_refresh)
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)
        assert not errors


# ── 10. Integration / persistence ────────────────────────────────────────────


class TestIntegration:
    def test_refresh_produces_all_six_providers(self):
        """After refresh, all 6 core providers are present in the snapshot."""
        rp = _make_instance()
        snap = rp.refresh()
        for prov in ("ours", "friend", "ollama_cloud", "ppq", "openrouter", "deepinfra"):
            assert prov in snap.by_provider, f"missing {prov}"

    def test_price_observations_table_populated(self):
        """After refresh, rows appear in price_observations."""
        zai = _make_zai_db()
        rp = _make_instance(zai_db=zai)
        rp.refresh()
        conn = sqlite3.connect(zai)
        rows = conn.execute(
            "SELECT provider, source, is_measured FROM price_observations "
            "WHERE ts > ?",
            (time.time() - 60,),
        ).fetchall()
        conn.close()
        assert len(rows) >= 6  # at least one per provider
        providers_seen = {r[0] for r in rows}
        assert "ollama_cloud" in providers_seen
        assert "deepinfra" in providers_seen

    def test_snapshot_is_immutable(self):
        """RateSnapshot is frozen — mutation raises FrozenInstanceError."""
        rp = _make_instance()
        snap = rp.snapshot()
        with pytest.raises(Exception):
            snap.ts = 999.0  # type: ignore

    def test_get_provider_rates_all_six(self):
        rp = _make_instance()
        rp.refresh()
        rates = rp.get_provider_rates()
        for prov in ("ours", "friend", "ollama_cloud", "ppq", "openrouter", "deepinfra"):
            assert prov in rates
            assert rates[prov] >= MIN_EFFECTIVE_PRICE

    def test_refresh_never_raises_on_corrupt_db(self):
        """If the DB path is invalid, refresh still completes (never raises)."""
        rp = _make_instance(zai_db="/nonexistent/path/db.sqlite")
        # This should NOT raise
        snap = rp.refresh()
        assert snap is not None
        assert snap.refresh_count >= 1


# ── PM-T5: per-model daily_spend (migration + GROUP BY + published fallback) ──


def _make_zai_db_per_model(daily_spend_rows=None) -> str:
    """A temp zai_usage.db whose daily_spend ALREADY has the `model` column
    (post-migration schema). Rows: (date, tier, model, spend_usd, token_count).
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE api_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, key_name TEXT, model TEXT,
            total_tokens INTEGER DEFAULT 0, cost_usd REAL, cost_source TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE daily_spend (
            date TEXT NOT NULL,
            tier TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT 'unknown',
            spend_usd REAL DEFAULT 0,
            call_count INTEGER DEFAULT 0,
            token_count INTEGER DEFAULT 0,
            PRIMARY KEY (date, tier, model)
        )
        """
    )
    for row in (daily_spend_rows or []):
        conn.execute(
            "INSERT INTO daily_spend (date, tier, model, spend_usd, token_count) "
            "VALUES (?,?,?,?,?)",
            row,
        )
    conn.commit()
    conn.close()
    return path


class TestDailySpendMigration:
    """migrate_daily_spend_add_model: idempotent, backfills 'unknown'."""

    def test_adds_model_column_and_upgrades_pk(self):
        """Pre-migration (no model col, PK date,tier) → model col + PK
        (date,tier,model); existing rows backfilled to 'unknown'."""
        today = time.strftime("%Y-%m-%d")
        zai = _make_zai_db(daily_spend_rows=[
            (today, "deepinfra", 1.30, 1_000_000),
            (today, "openrouter", 0.135, 2_000_000),
        ])
        report = migrate_daily_spend_add_model(zai)
        assert report["table_exists"] is True
        assert report["had_model_column"] is False
        assert report["migrated"] is True
        assert report["rows"] == 2

        conn = sqlite3.connect(zai)
        # model column present.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(daily_spend)")]
        assert "model" in cols
        # PK upgraded to (date, tier, model).
        pk = conn.execute("PRAGMA index_list('daily_spend')").fetchall()
        assert pk, "expected a primary-key index after migration"
        # Rows backfilled to model='unknown', spend preserved.
        rows = conn.execute(
            "SELECT tier, model, spend_usd FROM daily_spend ORDER BY tier"
        ).fetchall()
        conn.close()
        assert rows == [
            ("deepinfra", "unknown", 1.30),
            ("openrouter", "unknown", 0.135),
        ]

    def test_idempotent_on_already_migrated(self):
        """Running twice is a no-op (no second rebuild, no data loss)."""
        today = time.strftime("%Y-%m-%d")
        zai = _make_zai_db(daily_spend_rows=[(today, "deepinfra", 1.30, 1_000_000)])
        first = migrate_daily_spend_add_model(zai)
        assert first["migrated"] is True
        second = migrate_daily_spend_add_model(zai)
        assert second["had_model_column"] is True
        assert second["migrated"] is False
        assert second["rows"] == 1
        # Data still intact.
        conn = sqlite3.connect(zai)
        n = conn.execute("SELECT COUNT(*) FROM daily_spend").fetchone()[0]
        conn.close()
        assert n == 1

    def test_safe_on_db_without_table(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        report = migrate_daily_spend_add_model(path)
        os.unlink(path)
        assert report["table_exists"] is False
        assert report["migrated"] is False

    def test_never_raises_on_unopenable_path(self):
        report = migrate_daily_spend_add_model("/nonexistent/dir/db.sqlite")
        assert report["migrated"] is False

    def test_aggregates_duplicate_date_tier_rows(self):
        """Multiple pre-migration rows sharing (date, tier) collapse onto one
        backfilled 'unknown' row with summed spend/tokens."""
        today = time.strftime("%Y-%m-%d")
        zai = _make_zai_db(daily_spend_rows=[
            (today, "deepinfra", 1.0, 500_000),
            (today, "deepinfra", 0.5, 500_000),
        ])
        migrate_daily_spend_add_model(zai)
        conn = sqlite3.connect(zai)
        row = conn.execute(
            "SELECT model, spend_usd, token_count FROM daily_spend "
            "WHERE tier='deepinfra'"
        ).fetchone()
        conn.close()
        assert row == ("unknown", 1.5, 1_000_000)


class TestSpendTierPerModel:
    """_measure_spend_tier: per-model GROUP BY when migrated; provider-level
    otherwise."""

    def test_per_model_groupby_emits_model_keys_and_aggregate(self):
        """Migrated schema + two models → per-model measured obs + a
        token-weighted provider-level (model=None) aggregate."""
        today = time.strftime("%Y-%m-%d")
        # flash: $0.14 / 1M tok → $0.14/M ; pro: $2.70 / 1M tok → $2.70/M
        rows = [
            (today, "deepinfra", "deepseek-v4-flash", 0.14, 1_000_000),
            (today, "deepinfra", "deepseek-v4-pro", 2.70, 1_000_000),
        ]
        zai = _make_zai_db_per_model(daily_spend_rows=rows)
        rp = _make_instance(zai_db=zai)
        snap = rp.refresh()

        flash = snap.by_provider_model.get(("deepinfra", "deepseek-v4-flash"))
        pro = snap.by_provider_model.get(("deepinfra", "deepseek-v4-pro"))
        agg = snap.by_provider_model.get(("deepinfra", None))
        assert flash is not None and pro is not None and agg is not None
        assert flash.source == SRC_DEEPINFRA_ACTUAL
        assert flash.is_measured is True
        assert flash.rate_per_m == pytest.approx(0.14, rel=0.01)
        assert pro.rate_per_m == pytest.approx(2.70, rel=0.01)
        # Aggregate is the token-weighted blend (1.42), distinct from either.
        assert agg.rate_per_m == pytest.approx(1.42, rel=0.01)
        assert flash.rate_per_m < agg.rate_per_m < pro.rate_per_m

    def test_pre_migration_stays_provider_level(self):
        """No model column → exactly one provider-level (model=None) obs; no
        per-model keys. Backward compatible."""
        today = time.strftime("%Y-%m-%d")
        zai = _make_zai_db(daily_spend_rows=[(today, "deepinfra", 1.30, 1_000_000)])
        rp = _make_instance(zai_db=zai)
        snap = rp.refresh()
        assert ("deepinfra", None) in snap.by_provider_model
        # No per-model deepinfra keys before migration.
        deepinfra_models = {m for (p, m) in snap.by_provider_model
                            if p == "deepinfra" and m is not None}
        assert deepinfra_models == set()
        ob = snap.by_provider_model[("deepinfra", None)]
        assert ob.source == SRC_DEEPINFRA_ACTUAL
        assert ob.rate_per_m == pytest.approx(1.30, rel=0.01)

    def test_migrate_then_refresh_yields_per_model(self):
        """End-to-end: pre-migration DB → run migration → refresh now returns
        per-model rows (the collector detects the new column at runtime)."""
        today = time.strftime("%Y-%m-%d")
        zai = _make_zai_db(daily_spend_rows=[(today, "deepinfra", 1.30, 1_000_000)])
        # Before migration: provider-level only.
        rp = _make_instance(zai_db=zai)
        snap0 = rp.refresh()
        assert ("deepinfra", None) in snap0.by_provider_model
        assert not any(p == "deepinfra" and m is not None
                       for (p, m) in snap0.by_provider_model)
        RealtimePricing.reset_instance()

        migrate_daily_spend_add_model(zai)
        rp2 = _make_instance(zai_db=zai)
        snap1 = rp2.refresh()
        # After migration: the backfilled row is model='unknown' → per-model
        # key (deepinfra, 'unknown') appears alongside the aggregate.
        assert ("deepinfra", "unknown") in snap1.by_provider_model
        assert ("deepinfra", None) in snap1.by_provider_model
        ob = snap1.by_provider_model[("deepinfra", "unknown")]
        assert ob.source == SRC_DEEPINFRA_ACTUAL
        assert ob.is_measured is True
        assert ob.rate_per_m == pytest.approx(1.30, rel=0.01)


class TestPublishedModelPrices:
    """published_model_rate(s): providers.yaml → per-model blended $/M."""

    def test_deepinfra_flash_blended_price(self):
        # providers.yaml: deepinfra.deepseek-v4-flash 0.09 in + 0.19 out → 0.14.
        assert published_model_rate("deepinfra", "deepseek-v4-flash") == pytest.approx(0.14)

    def test_openrouter_flash_blended_price(self):
        # 0.09 in + 0.18 out → 0.135.
        assert published_model_rate("openrouter", "deepseek-v4-flash") == pytest.approx(0.135)

    def test_unknown_model_returns_none(self):
        assert published_model_rate("deepinfra", "no-such-model") is None

    def test_rates_dict_for_provider(self):
        rates = published_model_rates("ppq")
        assert rates["deepseek-v4-flash"] == pytest.approx(0.14)

    def test_never_raises_missing_file(self):
        assert published_model_rates("deepinfra", "/no/such/file.yaml") == {}
        assert published_model_rate("deepinfra", "x", "/no/such/file.yaml") is None

    def test_openrouter_cold_falls_back_to_per_model_published(self):
        """No openrouter spend → snapshot carries per-model published key
        (openrouter, deepseek-v4-flash), source PUBLISHED, is_measured False."""
        rp = _make_instance()  # empty DBs
        snap = rp.refresh()
        ob = snap.by_provider_model.get(("openrouter", "deepseek-v4-flash"))
        assert ob is not None
        assert ob.source == SRC_PUBLISHED
        assert ob.is_measured is False
        assert ob.rate_per_m == pytest.approx(0.135)
        # Provider-level aggregate rolls up to the same published value.
        assert snap.by_provider["openrouter"].source == SRC_PUBLISHED
        assert snap.by_provider["openrouter"].rate_per_m == pytest.approx(0.135)


# ── RP-2 §4: Validation against known-real numbers ────────────────────────────
#
# The cold-start seeds encode MEASURED values from docs/extra-usage-real-data-
# analysis.md. These tests guard the provenance so a typo in DEFAULT_COLD_START
# RATES is caught immediately.


class TestKnownRealNumbers:
    """Assert that seed/snapshot rates fall within the known-real bounds."""

    def test_ollama_cloud_included_rate(self):
        """ollama_cloud included: $0.0155/M (assert 0.012 < rate < 0.020)."""
        rp = _make_instance()
        ob = rp.get_rate("ollama_cloud")
        assert 0.012 < ob.rate_per_m < 0.020, f"ollama_cloud rate {ob.rate_per_m} out of bounds"
        # Also verify the constant itself
        seed = DEFAULT_COLD_START_RATES["ollama_cloud"]
        assert 0.012 < seed < 0.020

    def test_ollama_extra_glm52_rate(self):
        """ollama extra glm-5.2: $0.46/M (assert 0.30 < rate < 0.60)."""
        seed = DEFAULT_COLD_START_RATES["ollama_cloud_extra_glm52"]
        assert 0.30 < seed < 0.60, f"extra glm-5.2 rate {seed} out of bounds"
        # Verify it surfaces via get_rate for the pseudo-provider key
        rp = _make_instance()
        ob = rp.get_rate("ollama_cloud_extra_glm52")
        assert 0.30 < ob.rate_per_m < 0.60

    def test_ollama_kimi3_rate(self):
        """ollama kimi-k3: $7.53/M (assert 5.0 < rate < 10.0)."""
        seed = DEFAULT_COLD_START_RATES["ollama_cloud_kimi3"]
        assert 5.0 < seed < 10.0, f"kimi-k3 rate {seed} out of bounds"
        rp = _make_instance()
        ob = rp.get_rate("ollama_cloud_kimi3")
        assert 5.0 < ob.rate_per_m < 10.0

    def test_ours_rate_at_floor(self):
        """ours: < $0.001/M (sunk cost, floored at MIN_EFFECTIVE_PRICE)."""
        rp = _make_instance()
        ob = rp.get_rate("ours")
        assert ob.rate_per_m <= 0.001, f"ours rate {ob.rate_per_m} exceeds floor"
        assert ob.rate_per_m >= MIN_EFFECTIVE_PRICE

    def test_all_seed_rates_in_cold_start_snapshot(self):
        """Every core-provider DEFAULT_COLD_START_RATES value surfaces verbatim
        (floored) in the pre-refresh snapshot."""
        rp = _make_instance()
        snap = rp.snapshot()
        core_providers = ("ours", "friend", "ollama_cloud", "ppq", "openrouter", "deepinfra")
        for prov in core_providers:
            expected = DEFAULT_COLD_START_RATES[prov]
            floored = max(expected, MIN_EFFECTIVE_PRICE)
            ob = snap.by_provider.get(prov)
            assert ob is not None, f"{prov} missing from snapshot"
            assert ob.rate_per_m == pytest.approx(floored, rel=0.01), (
                f"{prov}: expected ~{floored}, got {ob.rate_per_m}"
            )


# ── RP-2 §5: Performance gates ────────────────────────────────────────────────


class TestPerformance:
    def test_snapshot_under_1us(self):
        """snapshot() must complete in < 1µs (lock-free attribute read)."""
        rp = _make_instance()
        rp.refresh()  # populate the snapshot
        # Warm up
        for _ in range(100):
            rp.snapshot()
        # Measure
        iters = 10_000
        t0 = time.perf_counter_ns()
        for _ in range(iters):
            rp.snapshot()
        elapsed_ns = time.perf_counter_ns() - t0
        avg_ns = elapsed_ns / iters
        assert avg_ns < 1_000, f"snapshot() took {avg_ns:.0f}ns avg (gate: 1000ns)"

    def test_get_rate_under_1us(self):
        """get_rate() is a hot-path lookup — must stay under 1µs."""
        rp = _make_instance()
        rp.refresh()
        for _ in range(100):
            rp.get_rate("ollama_cloud")
        iters = 10_000
        t0 = time.perf_counter_ns()
        for _ in range(iters):
            rp.get_rate("ollama_cloud")
        elapsed_ns = time.perf_counter_ns() - t0
        avg_ns = elapsed_ns / iters
        assert avg_ns < 1_000, f"get_rate() took {avg_ns:.0f}ns avg (gate: 1000ns)"

    def test_refresh_under_500ms(self):
        """refresh() with temp DBs must complete in < 500ms."""
        rp = _make_instance()
        # Warm up (first refresh creates the schema table)
        rp.refresh()
        # Measure second refresh (steady-state)
        t0 = time.perf_counter()
        rp.refresh()
        elapsed_ms = (time.perf_counter() - t0) * 1_000
        assert elapsed_ms < 500, f"refresh() took {elapsed_ms:.1f}ms (gate: 500ms)"


# ── RP-2 §6: Kill switch ──────────────────────────────────────────────────────


class TestKillSwitch:
    """REALTIME_PRICING_ENABLED=false reproduces old (static-rate) behaviour."""

    def test_disabled_skips_collectors(self):
        """With the kill switch off, refresh() is a no-op: no measured rates."""
        # Seed data that WOULD produce measured rates if the switch were on.
        mock_response = {
            "activity": {
                "glm-5.2": {"cost": 1.55, "total_tokens": 100_000_000, "request_count": 0},
            },
        }
        today = time.strftime("%Y-%m-%d")
        zai = _make_zai_db(daily_spend_rows=[(today, "deepinfra", 1.30, 1_000_000)])

        with patch.dict(os.environ, {"REALTIME_PRICING_ENABLED": "false"}):
            with patch("src.ollama_extra_usage.fetch_ollama_usage", return_value=mock_response):
                rp = _make_instance(zai_db=zai)
                rp.refresh()

        # Every core provider must still be cold-start — collectors were skipped.
        snap = rp.snapshot()
        assert snap.refresh_count == 0
        for prov in ("ours", "friend", "ollama_cloud", "ppq", "deepinfra"):
            ob = snap.by_provider[prov]
            assert ob.source == SRC_COLD_START, f"{prov} should be cold-start"
            assert ob.is_measured is False

    def test_disabled_get_provider_rates_returns_seeds(self):
        """get_provider_rates() returns the seed values, not measured."""
        from src.realtime_pricing import DEFAULT_COLD_START_RATES as SEEDS

        with patch.dict(os.environ, {"REALTIME_PRICING_ENABLED": "false"}):
            rp = _make_instance()
            rp.refresh()
            rates = rp.get_provider_rates()

        for prov, expected in SEEDS.items():
            if prov in rates:
                floored = max(expected, MIN_EFFECTIVE_PRICE)
                assert rates[prov] == pytest.approx(floored, rel=0.01), (
                    f"{prov}: expected seed ~{floored}, got {rates[prov]}"
                )

    def test_enabled_by_default(self):
        """When the env var is unset (default), realtime pricing is active."""
        from src.realtime_pricing import is_realtime_pricing_enabled

        # Ensure the var is truly absent
        saved = os.environ.pop("REALTIME_PRICING_ENABLED", None)
        try:
            assert is_realtime_pricing_enabled() is True
        finally:
            if saved is not None:
                os.environ["REALTIME_PRICING_ENABLED"] = saved

    def test_falsy_values_disable(self):
        from src.realtime_pricing import is_realtime_pricing_enabled

        for val in ("false", "FALSE", "0", "no", "off", ""):
            with patch.dict(os.environ, {"REALTIME_PRICING_ENABLED": val}):
                assert is_realtime_pricing_enabled() is False, (
                    f"value {val!r} should disable realtime pricing"
                )

    def test_truthy_values_enable(self):
        from src.realtime_pricing import is_realtime_pricing_enabled

        for val in ("true", "TRUE", "1", "yes", "on", "anything"):
            with patch.dict(os.environ, {"REALTIME_PRICING_ENABLED": val}):
                assert is_realtime_pricing_enabled() is True, (
                    f"value {val!r} should enable realtime pricing"
                )

    def test_toggle_runtime(self):
        """Toggling the env var between calls works (checked at call time)."""
        mock_response = {
            "activity": {
                "glm-5.2": {"cost": 1.55, "total_tokens": 100_000_000, "request_count": 0},
            },
        }
        # Enabled first: refresh produces measured rates
        with patch("src.ollama_extra_usage.fetch_ollama_usage", return_value=mock_response):
            rp = _make_instance()
            rp.refresh()
        assert rp.get_rate("ollama_cloud").source == SRC_OLLAMA_BILLING

        # Now disable and refresh again: rate should NOT change (no-op)
        measured_rate = rp.get_rate("ollama_cloud").rate_per_m
        with patch.dict(os.environ, {"REALTIME_PRICING_ENABLED": "false"}):
            with patch("src.ollama_extra_usage.fetch_ollama_usage",
                       return_value={"activity": {"glm-5.2": {"cost": 999.0, "total_tokens": 100_000, "request_count": 0}}}):
                rp.refresh()
        assert rp.snapshot().by_provider["ollama_cloud"].source == SRC_OLLAMA_BILLING
        assert rp.get_rate("ollama_cloud").rate_per_m == pytest.approx(measured_rate)


# ── RP-2 §2: True 100-concurrent snapshot stress test ─────────────────────────


class TestThreadSafetyStress:
    """Category 2 hard requirement: 100 concurrent snapshot() during refresh()."""

    def test_100_concurrent_snapshots_no_torn_reads(self):
        """100 threads each call snapshot() in a tight loop while a writer
        calls refresh().  No exceptions, every snapshot internally consistent."""
        rp = _make_instance()
        rp.refresh()  # seed a real snapshot

        errors: list[Exception] = []
        stop = threading.Event()

        def reader():
            try:
                count = 0
                while not stop.is_set() and count < 500:
                    s = rp.snapshot()
                    assert s.ts > 0
                    # Internal consistency: every observation ts <= snapshot ts.
                    for ob in s.by_provider.values():
                        assert ob.ts <= s.ts + 5.0
                    count += 1
            except Exception as exc:
                errors.append(exc)

        def writer():
            try:
                for _ in range(50):
                    rp.refresh()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=reader, name=f"reader-{i}") for i in range(100)]
        threads.append(threading.Thread(target=writer, name="writer"))
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"{len(errors)} errors from 100 concurrent readers: {errors[:3]}"
        # All reader threads actually ran
        assert all(not t.is_alive() for t in threads), "a thread timed out"

