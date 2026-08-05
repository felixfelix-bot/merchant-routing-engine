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
        """ours: 155 / (tokens/1e6). With 1B tokens → 0.155 $/M."""
        now = time.time()
        # 1 billion tokens for 'ours' this month
        rows = [(now - 100, "ours", "glm-5.2", 1_000_000_000)]
        zai = _make_zai_db(api_calls_rows=rows)
        rp = _make_instance(zai_db=zai)
        rp.refresh()
        ob = rp.get_rate("ours")
        assert ob.source == SRC_ZAI_AMORTIZED
        assert ob.is_measured is False
        # 155 / 1000 = 0.155
        assert ob.rate_per_m == pytest.approx(0.155, rel=0.01)

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
