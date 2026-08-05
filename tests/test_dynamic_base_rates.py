"""Tests for P5-RATES: dynamic base-rate wiring in LiveRouter.

Covers:
  * ``_resolve_dynamic_base_rates`` — completeness, measured-vs-seed paths,
    amortized z.ai, and never-raise on bad/corrupt DBs.
  * ``LiveRouter.__init__`` — env kill switch gating, explicit-override wins.
  * ``LiveRouter.refresh_base_rates`` — updates ``_base_rates`` and feeds the
    PriceKalman filters; never raises.
  * Background refresh thread — started only when enabled, stopped on reset.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import time

import pytest

import src.live_router as lr
from src.live_router import LiveRouter, _resolve_dynamic_base_rates, _DEFAULT_CONVERGED_RATES
from src.real_price_tracker import clear_cache, ZAI_SEED_RATE, ZAI_FRIEND_PREMIUM

# ── Production schema (mirrors test_real_price_tracker.py) ───────────────────

_API_CALLS_DDL = """
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
    duration_ms INTEGER,
    cost_usd REAL,
    cost_source TEXT
)
"""

_ALL_PROVIDERS = ("ours", "friend", "ollama_cloud", "ppq", "openrouter", "deepinfra")


@pytest.fixture
def db():
    """Fresh temp DB with the production api_calls schema. Clears the
    real_price_tracker cache before/after for isolation."""
    clear_cache()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute(_API_CALLS_DDL)
    conn.commit()
    conn.close()
    yield path
    clear_cache()
    try:
        os.unlink(path)
    except OSError:
        pass


def _seed(db_path, rows):
    """Insert (ts, key_name, model, total_tokens, cost_usd) rows."""
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO api_calls (ts, key_name, model, total_tokens, cost_usd) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def _now():
    return time.time()


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Ensure no leaked singleton between tests."""
    LiveRouter.reset_instance()
    yield
    LiveRouter.reset_instance()


# ── _resolve_dynamic_base_rates ──────────────────────────────────────────────


class TestResolveDynamicBaseRates:
    def test_returns_all_six_providers(self, db):
        rates = _resolve_dynamic_base_rates(db)
        assert set(rates.keys()) == set(_ALL_PROVIDERS)
        for v in rates.values():
            assert isinstance(v, float) and v > 0

    def test_empty_db_falls_back_to_seeds(self, db):
        """With no data, ours/friend get the z.ai amortized seed; the paid
        endpoints and ollama get their SEED_RATES entries."""
        rates = _resolve_dynamic_base_rates(db)
        # z.ai amortized seed (<30d data → seed × premium)
        assert rates["ours"] == pytest.approx(ZAI_SEED_RATE, rel=1e-9)
        assert rates["friend"] == pytest.approx(ZAI_SEED_RATE * ZAI_FRIEND_PREMIUM, rel=1e-9)
        # paid + ollama → SEED_RATES
        from src.real_price_tracker import SEED_RATES
        assert rates["ollama_cloud"] == pytest.approx(SEED_RATES["ollama_cloud"])
        assert rates["ppq"] == pytest.approx(SEED_RATES["ppq"])
        assert rates["openrouter"] == pytest.approx(SEED_RATES["openrouter"])
        assert rates["deepinfra"] == pytest.approx(SEED_RATES["deepinfra"])

    def test_measured_ollama_overrides_seed(self, db):
        """When ollama_cloud has >=100 costed rows in the 90d window, the
        measured rate wins over the seed."""
        now = _now()
        # 200 calls × 1000 tokens × $0.05 → $10 / 0.2M = $50/M
        rows = [
            (now - 100, "ollama_cloud", "m", 1000, 0.05) for _ in range(200)
        ]
        _seed(db, rows)
        rates = _resolve_dynamic_base_rates(db)
        assert rates["ollama_cloud"] == pytest.approx(50.0, rel=1e-6)

    def test_measured_paid_provider_overrides_seed(self, db):
        """When ppq has >=100 costed rows in the 30d window, measured wins."""
        now = _now()
        rows = [(now - 100, "ppq", "kimi", 1000, 0.02) for _ in range(200)]
        _seed(db, rows)
        rates = _resolve_dynamic_base_rates(db)
        # $0.02 / 1000 tok × 1e6 = $20/M
        assert rates["ppq"] == pytest.approx(20.0, rel=1e-6)

    def test_amortized_zai_when_enough_data(self, db):
        """When ours has 30+ days of token data, the amortized rate is used
        (not the seed)."""
        now = _now()
        # 60 days of data: 1M tokens/day × 60 days = 60M tokens
        # annual budget $300 → $300 / (60M/1e6) = $5/M
        day = 86400.0
        rows = [
            (now - day * (60 - i), "ours", "m", 1_000_000, None)
            for i in range(60)
        ]
        _seed(db, rows)
        rates = _resolve_dynamic_base_rates(db)
        assert rates["ours"] == pytest.approx(5.0, rel=0.05)

    def test_never_raises_nonexistent_db(self):
        """A non-existent DB path yields the seed/default rates, not an error."""
        rates = _resolve_dynamic_base_rates("/nonexistent/path/to/db.db")
        assert set(rates.keys()) == set(_ALL_PROVIDERS)
        for v in rates.values():
            assert isinstance(v, float) and v > 0

    def test_never_raises_corrupt_db(self):
        """A DB with no api_calls table degrades to seeds/defaults."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE dummy (id INTEGER)")
        conn.commit()
        conn.close()
        try:
            rates = _resolve_dynamic_base_rates(path)
            assert set(rates.keys()) == set(_ALL_PROVIDERS)
        finally:
            os.unlink(path)


# ── LiveRouter.__init__ kill-switch gating ───────────────────────────────────


class TestInitGating:
    def test_off_uses_hardcoded_defaults(self, db):
        """Feature OFF (default) → _base_rates == _DEFAULT_CONVERGED_RATES."""
        router = LiveRouter(db_path=db)
        for k, v in _DEFAULT_CONVERGED_RATES.items():
            assert router._base_rates[k] == pytest.approx(v)

    def test_on_uses_dynamic_rates(self, db, monkeypatch):
        """Feature ON + empty DB → _base_rates come from the resolver (seeds)."""
        monkeypatch.setattr(lr, "_DYNAMIC_RATES_ENABLED", True)
        router = LiveRouter(db_path=db)
        # ours → amortized seed, not the hardcoded 0.001
        assert router._base_rates["ours"] == pytest.approx(ZAI_SEED_RATE, rel=1e-9)
        assert router._base_rates["ours"] != _DEFAULT_CONVERGED_RATES["ours"]

    def test_explicit_override_wins_over_dynamic(self, db, monkeypatch):
        """Even with the feature ON, an explicit converged_rates dict wins."""
        monkeypatch.setattr(lr, "_DYNAMIC_RATES_ENABLED", True)
        override = {"ours": 0.5, "friend": 0.6, "ollama_cloud": 0.7,
                    "ppq": 0.8, "openrouter": 0.9, "deepinfra": 1.1}
        router = LiveRouter(db_path=db, converged_rates=override)
        for k, v in override.items():
            assert router._base_rates[k] == pytest.approx(v)

    def test_off_dynamic_resolver_not_called_for_rates(self, db, monkeypatch):
        """Feature OFF → the resolver is never consulted."""
        called = {"n": 0}
        orig = lr._resolve_dynamic_base_rates

        def _spy(*a, **kw):
            called["n"] += 1
            return orig(*a, **kw)

        monkeypatch.setattr(lr, "_resolve_dynamic_base_rates", _spy)
        LiveRouter(db_path=db)
        assert called["n"] == 0


# ── refresh_base_rates ───────────────────────────────────────────────────────


class TestRefreshBaseRates:
    def test_updates_base_rates_and_kalman(self, db, monkeypatch):
        """refresh_base_rates() pulls fresh rates into _base_rates and feeds
        each as an observation into the PriceKalman."""
        monkeypatch.setattr(lr, "_DYNAMIC_RATES_ENABLED", True)
        router = LiveRouter(db_path=db)
        # Seed the router with a clearly-wrong ours rate, then refresh.
        before = router._base_rates["ours"]
        fresh = router.refresh_base_rates()
        assert fresh["ours"] == pytest.approx(ZAI_SEED_RATE, rel=1e-9)
        assert router._last_rate_refresh_ts > 0
        # The Kalman base_rate should have moved toward the observed rate.
        pk = router._price_kalmans["ours"]
        assert pk.base_rate == pytest.approx(ZAI_SEED_RATE, rel=1e-6)

    def test_refresh_never_raises_bad_db(self, monkeypatch):
        """A bad db_path on the instance must not crash refresh."""
        monkeypatch.setattr(lr, "_DYNAMIC_RATES_ENABLED", True)
        router = LiveRouter(db_path="/nonexistent/db.db")
        result = router.refresh_base_rates()  # must not raise
        assert set(result.keys()) == set(_ALL_PROVIDERS)

    def test_refresh_thread_not_started_when_off(self, db):
        """Feature OFF → no refresh thread is created, even via get_instance."""
        LiveRouter.reset_instance()
        LiveRouter.get_instance(db_path=db)
        inst = LiveRouter.get_instance()
        assert inst._rate_refresh_thread is None

    def test_refresh_thread_started_and_stopped_when_on(self, db, monkeypatch):
        """Feature ON → get_instance starts the daemon; reset stops it."""
        monkeypatch.setattr(lr, "_DYNAMIC_RATES_ENABLED", True)
        monkeypatch.setattr(lr, "_RATE_REFRESH_INTERVAL_SECONDS", 9999.0)
        LiveRouter.reset_instance()
        inst = LiveRouter.get_instance(db_path=db)
        thread = inst._rate_refresh_thread
        assert thread is not None
        assert thread.is_alive()
        assert thread.daemon
        LiveRouter.reset_instance()
        # After reset the stop event was set; thread should exit promptly.
        # (Capture the ref before reset since _stop nulls the attribute.)
        thread.join(timeout=5.0)
        assert not thread.is_alive()
