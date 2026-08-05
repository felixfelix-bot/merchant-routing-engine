"""Tests for src/real_price_tracker.py — RP-3 rolling real $/M from api_calls.cost_usd.

Covers (per the task gate):
  - 1000 calls with known cost → correct token-weighted $/M calculated
  - < 100 calls → None (insufficient data)
  - cache TTL (5 min): cached within TTL, recomputed after expiry
  - price change detection (24h vs 7d, >50% deviation)
  - fallback to hardcoded LAST_RESORT_RATES when no data

Plus robustness/edge cases:
  - get_all_rates returns nested {provider: {model: rate}}
  - model-specific rate vs provider-aggregate
  - token weighting (cost spread unevenly across calls)
  - ollama_cloud falls back to the Ollama billing API (mocked)
  - unknown provider → conservative fallback
  - never raises: bad DB path, missing cost_usd column, NULL cost rows
  - NULL total_tokens / negative cost guards
  - clear_cache invalidates
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import time

import pytest

import src.real_price_tracker as rpt
from src.real_price_tracker import (
    CACHE_TTL_SECONDS,
    CHANGE_BASELINE_HOURS,
    CHANGE_RECENT_HOURS,
    CHANGE_THRESHOLD,
    LAST_RESORT_RATES,
    MIN_CALLS_FOR_RATE,
    UNKNOWN_PROVIDER_FALLBACK,
    clear_cache,
    detect_price_change,
    get_all_rates,
    get_rate_with_fallback,
    get_real_rate,
)


# ── Production schema (post RP-1/RP-2: includes cost_usd, cost_source) ─────────

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


@pytest.fixture
def db():
    """A fresh temp DB with the production api_calls schema. Yields the path;
    clears the module cache before and after so tests are isolated."""
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


def _now_utc():
    return time.time()


# ── get_real_rate: core correctness ──────────────────────────────────────────


class TestGetRealRate:
    def test_1000_calls_known_cost_correct_rate(self, db):
        """1000 calls: 1000 tokens each at $0.0001 each → $/M = 0.1."""
        now = _now_utc()
        # 1000 calls × (1000 tokens, $0.0001 cost)
        rows = [
            (now - 100, "openrouter", "glm-5.2", 1000, 0.0001)
            for _ in range(1000)
        ]
        _seed(db, rows)
        rate = get_real_rate("openrouter", "glm-5.2", window_hours=168, db_path=db)
        # SUM(cost)=0.1, SUM(tokens)=1_000_000 → 0.1/1e6 * 1e6 = 0.1 $/M
        assert rate == pytest.approx(0.1, rel=1e-9)

    def test_below_min_calls_returns_none(self, db):
        """Fewer than MIN_CALLS_FOR_RATE costed calls → None."""
        now = _now_utc()
        rows = [
            (now - 100, "ppq", "kimi", 1000, 0.0001)
            for _ in range(MIN_CALLS_FOR_RATE - 1)  # 99 calls
        ]
        _seed(db, rows)
        rate = get_real_rate("ppq", "kimi", db_path=db)
        assert rate is None

    def test_exactly_min_calls_returns_rate(self, db):
        """Exactly MIN_CALLS_FOR_RATE calls is enough (boundary)."""
        now = _now_utc()
        rows = [
            (now - 100, "ppq", "kimi", 1000, 0.0001)
            for _ in range(MIN_CALLS_FOR_RATE)
        ]
        _seed(db, rows)
        rate = get_real_rate("ppq", "kimi", db_path=db)
        assert rate is not None
        assert rate == pytest.approx(0.1, rel=1e-9)

    def test_provider_aggregate_without_model(self, db):
        """model=None aggregates across all models for the provider."""
        now = _now_utc()
        rows = []
        # 50 calls of model A: 1000 tokens @ $0.0001
        rows += [(now - 50, "openrouter", "A", 1000, 0.0001) for _ in range(50)]
        # 50 calls of model B: 1000 tokens @ $0.0003
        rows += [(now - 50, "openrouter", "B", 1000, 0.0003) for _ in range(50)]
        _seed(db, rows)
        # Aggregate: SUM(cost)=0.005+0.015=0.02, SUM(tokens)=100000 → 0.2 $/M
        rate = get_real_rate("openrouter", db_path=db)
        assert rate == pytest.approx(0.2, rel=1e-9)

    def test_model_specific_isolates_from_other_models(self, db):
        now = _now_utc()
        rows = []
        rows += [(now - 50, "openrouter", "A", 1000, 0.0001) for _ in range(100)]
        rows += [(now - 50, "openrouter", "B", 1000, 0.0005) for _ in range(100)]
        _seed(db, rows)
        assert get_real_rate("openrouter", "A", db_path=db) == pytest.approx(0.1, rel=1e-9)
        assert get_real_rate("openrouter", "B", db_path=db) == pytest.approx(0.5, rel=1e-9)

    def test_token_weighted_not_arithmetic_mean(self, db):
        """Rate must be SUM(cost)/SUM(tokens), not the mean of per-call rates."""
        now = _now_utc()
        rows = []
        # 100 calls: 1 token @ $1.0  (rate $1M/M each)
        rows += [(now - 50, "di", "m", 1, 1.0) for _ in range(100)]
        # but that's dominated token-wise; instead mix big and small
        # 100 calls of 1_000_000 tokens @ $0.01 (rate 0.01 $/M)
        rows += [(now - 50, "di", "m", 1_000_000, 0.01) for _ in range(100)]
        _seed(db, rows)
        rate = get_real_rate("di", "m", db_path=db)
        # SUM(cost)=100*1.0 + 100*0.01 = 101; SUM(tokens)=100*1 + 100*1e6 = 100000100
        expected = 101 / 100000100 * 1e6
        assert rate == pytest.approx(expected, rel=1e-9)

    def test_window_excludes_old_calls(self, db):
        """Calls older than window_hours are excluded."""
        now = _now_utc()
        # old calls (48h ago) — OUTSIDE a 24h window
        _seed(db, [(now - 48 * 3600, "openrouter", "m", 1000, 0.0001)
                   for _ in range(50)])
        # recent calls (1h ago) — INSIDE a 24h window
        _seed(db, [(now - 3600, "openrouter", "m", 1000, 0.0005)
                   for _ in range(100)])
        # 24h window → only the 100 recent calls count
        rate = get_real_rate("openrouter", "m", window_hours=24, db_path=db)
        assert rate == pytest.approx(0.5, rel=1e-9)

    def test_null_cost_rows_excluded(self, db):
        """Rows with cost_usd IS NULL must not count toward the call minimum."""
        now = _now_utc()
        rows = []
        # 100 costed calls (enough)
        rows += [(now - 50, "openrouter", "m", 1000, 0.0001) for _ in range(100)]
        # plus 200 NULL-cost calls that should be ignored
        rows += [(now - 50, "openrouter", "m", 1000, None) for _ in range(200)]
        _seed(db, rows)
        rate = get_real_rate("openrouter", "m", db_path=db)
        # Only 100 costed rows counted; rate still computable
        assert rate == pytest.approx(0.1, rel=1e-9)

    def test_zero_total_tokens_returns_none(self, db):
        """If all costed rows have total_tokens=0, division is impossible → None."""
        now = _now_utc()
        rows = [(now - 50, "openrouter", "m", 0, 0.0001) for _ in range(200)]
        _seed(db, rows)
        assert get_real_rate("openrouter", "m", db_path=db) is None


# ── Cache TTL ────────────────────────────────────────────────────────────────


class TestCache:
    def test_cached_within_ttl(self, db):
        """Within TTL, a second call returns the cached rate even after the DB
        changes — proving we hit the cache, not the DB."""
        now = _now_utc()
        _seed(db, [(now - 100, "openrouter", "m", 1000, 0.0001) for _ in range(100)])
        first = get_real_rate("openrouter", "m", db_path=db, _now=now)
        assert first == pytest.approx(0.1, rel=1e-9)

        # Mutate the DB heavily (would change the rate if we re-queried).
        _seed(db, [(now - 100, "openrouter", "m", 1000, 0.05) for _ in range(100)])

        # Same logical instant → cache hit → old rate.
        second = get_real_rate("openrouter", "m", db_path=db, _now=now)
        assert second == first  # unchanged because cache served it

    def test_recomputed_after_ttl_expiry(self, db):
        """After CACHE_TTL_SECONDS, a fresh aggregate runs."""
        t0 = _now_utc()
        _seed(db, [(t0 - 100, "openrouter", "m", 1000, 0.0001) for _ in range(100)])
        first = get_real_rate("openrouter", "m", db_path=db, _now=t0)
        assert first == pytest.approx(0.1, rel=1e-9)

        # Change the DB, then advance time past the TTL.
        _seed(db, [(t0 - 100, "openrouter", "m", 1000, 0.05) for _ in range(100)])
        later = t0 + CACHE_TTL_SECONDS + 1
        second = get_real_rate("openrouter", "m", db_path=db, _now=later)
        # New aggregate: now SUM(cost)=0.01+5.0=5.01, SUM(tokens)=200000 → ~25.05
        assert second != pytest.approx(first, rel=1e-3)
        assert second == pytest.approx(5.01 / 200000 * 1e6, rel=1e-9)

    def test_clear_cache_forces_recompute(self, db):
        now = _now_utc()
        _seed(db, [(now - 100, "openrouter", "m", 1000, 0.0001) for _ in range(100)])
        first = get_real_rate("openrouter", "m", db_path=db, _now=now)
        _seed(db, [(now - 100, "openrouter", "m", 1000, 0.05) for _ in range(100)])
        clear_cache()
        second = get_real_rate("openrouter", "m", db_path=db, _now=now)
        assert second != pytest.approx(first, rel=1e-3)

    def test_different_windows_cached_separately(self, db):
        """24h and 168h windows must not collide in the cache."""
        now = _now_utc()
        # old calls (only count toward the 7d window)
        _seed(db, [(now - 5 * 86400, "openrouter", "m", 1000, 0.0001)
                   for _ in range(150)])
        r7d = get_real_rate("openrouter", "m", window_hours=168, db_path=db, _now=now)
        r24h = get_real_rate("openrouter", "m", window_hours=24, db_path=db, _now=now)
        assert r7d is not None
        # 24h window has no recent calls → None
        assert r24h is None


# ── get_all_rates ────────────────────────────────────────────────────────────


class TestGetAllRates:
    def test_returns_nested_provider_model_dict(self, db):
        now = _now_utc()
        rows = []
        rows += [(now - 100, "openrouter", "A", 1000, 0.0001) for _ in range(100)]
        rows += [(now - 100, "openrouter", "B", 1000, 0.0003) for _ in range(100)]
        rows += [(now - 100, "ppq", "kimi", 1000, 0.0002) for _ in range(100)]
        _seed(db, rows)
        rates = get_all_rates(db_path=db, _now=now)
        assert set(rates.keys()) == {"openrouter", "ppq"}
        assert rates["openrouter"]["A"] == pytest.approx(0.1, rel=1e-9)
        assert rates["openrouter"]["B"] == pytest.approx(0.3, rel=1e-9)
        assert rates["ppq"]["kimi"] == pytest.approx(0.2, rel=1e-9)

    def test_drops_groups_below_min_calls(self, db):
        now = _now_utc()
        rows = []
        # ppq has only 10 calls → dropped
        rows += [(now - 100, "ppq", "k", 1000, 0.0001) for _ in range(10)]
        # openrouter has 100 → kept
        rows += [(now - 100, "openrouter", "A", 1000, 0.0001) for _ in range(100)]
        _seed(db, rows)
        rates = get_all_rates(db_path=db, _now=now)
        assert "openrouter" in rates
        assert "ppq" not in rates

    def test_empty_db_returns_empty_dict(self, db):
        assert get_all_rates(db_path=db) == {}

    def test_null_model_key_preserved(self, db):
        """Rows with model=NULL appear under the None key."""
        now = _now_utc()
        rows = [(now - 100, "openrouter", None, 1000, 0.0001) for _ in range(100)]
        _seed(db, rows)
        rates = get_all_rates(db_path=db, _now=now)
        assert rates["openrouter"][None] == pytest.approx(0.1, rel=1e-9)


# ── get_rate_with_fallback ───────────────────────────────────────────────────


class TestGetRateWithFallback:
    def test_returns_real_rate_when_available(self, db):
        now = _now_utc()
        _seed(db, [(now - 100, "openrouter", "m", 1000, 0.0001) for _ in range(100)])
        rate = get_rate_with_fallback("openrouter", "m", db_path=db, _now=now)
        assert rate == pytest.approx(0.1, rel=1e-9)

    def test_falls_back_to_last_resort_when_no_data(self, db):
        """No costed rows at all → LAST_RESORT_RATES estimate."""
        rate = get_rate_with_fallback("ppq", db_path=db, _now=_now_utc())
        assert rate == LAST_RESORT_RATES["ppq"]

    def test_falls_back_to_last_resort_per_provider(self, db):
        for provider, expected in LAST_RESORT_RATES.items():
            rate = get_rate_with_fallback(provider, db_path=db, _now=_now_utc())
            assert rate == pytest.approx(expected, rel=1e-12), provider

    def test_unknown_provider_returns_conservative_fallback(self, db):
        rate = get_rate_with_fallback("totally_new_provider", db_path=db, _now=_now_utc())
        assert rate == UNKNOWN_PROVIDER_FALLBACK

    def test_always_returns_float(self, db):
        # Regardless of provider, never returns None.
        for provider in ("ours", "friend", "ollama_cloud", "ppq",
                         "openrouter", "deepinfra", "never_seen"):
            rate = get_rate_with_fallback(provider, db_path=db, _now=_now_utc())
            assert isinstance(rate, float), provider

    def test_ollama_cloud_falls_back_to_billing_api(self, db, monkeypatch):
        """When local cost_usd is empty, ollama_cloud uses fetch_ollama_usage.
        The lazy import inside _ollama_api_rate reads the (monkeypatched)
        attribute of the real module at call time."""
        clear_cache()
        captured = {}
        import src.ollama_extra_usage as oeu

        def fake_fetch():
            captured["called"] = True
            return {
                "activity": {
                    "glm-5.2": {"cost": 0.0155, "total_tokens": 1_000_000, "request_count": 1},
                    "kimi": {"cost": 0.0209, "total_tokens": 1_000_000, "request_count": 1},
                }
            }

        monkeypatch.setattr(oeu, "fetch_ollama_usage", fake_fetch)

        # Provider-level aggregate: (0.0155+0.0209)/(2e6) * 1e6 = 0.0182
        rate = get_rate_with_fallback("ollama_cloud", db_path=db, _now=_now_utc())
        assert captured.get("called") is True
        assert rate == pytest.approx(0.0182, rel=1e-9)

    def test_ollama_cloud_specific_model_via_billing_api(self, db, monkeypatch):
        clear_cache()
        import src.ollama_extra_usage as oeu

        def fake_fetch():
            return {"activity": {"glm-5.2": {"cost": 0.0155, "total_tokens": 1_000_000}}}

        monkeypatch.setattr(oeu, "fetch_ollama_usage", fake_fetch)
        rate = get_rate_with_fallback("ollama_cloud", "glm-5.2", db_path=db, _now=_now_utc())
        assert rate == pytest.approx(0.0155, rel=1e-9)

    def test_ollama_billing_api_failure_falls_to_last_resort(self, db, monkeypatch):
        """If the billing API errors, we fall through to LAST_RESORT_RATES."""
        clear_cache()
        import src.ollama_extra_usage as oeu

        def fake_fetch():
            raise RuntimeError("network down")

        monkeypatch.setattr(oeu, "fetch_ollama_usage", fake_fetch)
        rate = get_rate_with_fallback("ollama_cloud", db_path=db, _now=_now_utc())
        assert rate == LAST_RESORT_RATES["ollama_cloud"]


# ── detect_price_change ──────────────────────────────────────────────────────


class TestDetectPriceChange:
    # SEMANTICS NOTE: the 7d baseline window strictly contains the 24h recent
    # window (7d ⊇ 24h). So with equal token volumes in each regime, the 7d rate
    # is the blend (R+B)/2 and the relative deviation is |R-B|/(R+B). The
    # >50% threshold is crossed exactly when R > 3*B (or B > 3*R). The tests
    # below use these closed-form expectations so they stay correct as the
    # threshold is tuned.
    def _seed_two_regimes(self, db, now, recent_cost, baseline_cost,
                          recent_tokens=1000, n_recent=100, n_baseline=100):
        """n_recent recent (1h ago) + n_baseline baseline (5d ago) calls."""
        rows = [
            (now - 3600, "openrouter", "m", recent_tokens, recent_cost)
            for _ in range(n_recent)
        ]
        rows += [
            (now - 5 * 86400, "openrouter", "m", recent_tokens, baseline_cost)
            for _ in range(n_baseline)
        ]
        _seed(db, rows)

    def test_detects_large_increase(self, db):
        # R=0.4, B=0.1 → deviation = 0.3/0.5 = 0.60 > 0.5 → True
        now = _now_utc()
        self._seed_two_regimes(db, now, recent_cost=0.0004, baseline_cost=0.0001)
        assert detect_price_change("openrouter", "m", db_path=db, _now=now) is True

    def test_detects_large_decrease(self, db):
        # R=0.04, B=0.2 → deviation = 0.16/0.24 = 0.667 > 0.5 → True
        now = _now_utc()
        self._seed_two_regimes(db, now, recent_cost=0.00004, baseline_cost=0.0002)
        assert detect_price_change("openrouter", "m", db_path=db, _now=now) is True

    def test_no_change_when_stable(self, db):
        now = _now_utc()
        # R=B → 0% deviation → False
        self._seed_two_regimes(db, now, recent_cost=0.0001, baseline_cost=0.0001)
        assert detect_price_change("openrouter", "m", db_path=db, _now=now) is False

    def test_small_drift_within_threshold(self, db):
        # R=0.13, B=0.1 → deviation = 0.03/0.23 ≈ 0.13 < 0.5 → False
        now = _now_utc()
        self._seed_two_regimes(db, now, recent_cost=0.00013, baseline_cost=0.0001)
        assert detect_price_change("openrouter", "m", db_path=db, _now=now) is False

    def test_boundary_just_above_threshold(self, db):
        # R=0.31, B=0.1 → deviation = 0.21/0.41 ≈ 0.512 > 0.5 → True
        now = _now_utc()
        self._seed_two_regimes(db, now, recent_cost=0.00031, baseline_cost=0.0001)
        assert detect_price_change("openrouter", "m", db_path=db, _now=now) is True

    def test_boundary_just_below_threshold(self, db):
        # R=0.29, B=0.1 → deviation = 0.19/0.39 ≈ 0.487 < 0.5 → False
        now = _now_utc()
        self._seed_two_regimes(db, now, recent_cost=0.00029, baseline_cost=0.0001)
        assert detect_price_change("openrouter", "m", db_path=db, _now=now) is False

    def test_insufficient_recent_data_returns_false(self, db):
        """If the 24h window has <100 calls, no recent rate → False (no alert)."""
        now = _now_utc()
        rows = [(now - 5 * 86400, "openrouter", "m", 1000, 0.0001) for _ in range(100)]
        _seed(db, rows)  # only baseline; no recent calls
        assert detect_price_change("openrouter", "m", db_path=db, _now=now) is False

    def test_no_data_at_all_returns_false(self, db):
        assert detect_price_change("ppq", "m", db_path=db, _now=_now_utc()) is False


# ── Robustness: never raises ─────────────────────────────────────────────────


class TestNeverRaises:
    def test_missing_db_path_returns_none(self):
        clear_cache()
        # A path that does not exist → sqlite creates an empty file with no table
        # → query fails → None. Use a path in /tmp that won't have the table.
        missing = os.path.join(tempfile.gettempdir(), "rpt_nonexistent_xyz.db")
        try:
            assert get_real_rate("openrouter", db_path=missing, _now=_now_utc()) is None
        finally:
            if os.path.exists(missing):
                os.unlink(missing)

    def test_db_without_cost_column_returns_none(self):
        """Pre-RP-1 schema (no cost_usd column) → query fails → None, no raise."""
        clear_cache()
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(path)
        # Old schema, no cost_usd/cost_source columns.
        conn.execute(
            "CREATE TABLE api_calls (id INTEGER PRIMARY KEY, ts REAL, key_name TEXT, "
            "total_tokens INTEGER)"
        )
        conn.execute(
            "INSERT INTO api_calls (ts, key_name, total_tokens) VALUES (?, ?, ?)",
            (_now_utc(), "openrouter", 1000),
        )
        conn.commit()
        conn.close()
        try:
            assert get_real_rate("openrouter", db_path=path, _now=_now_utc()) is None
            assert get_all_rates(db_path=path, _now=_now_utc()) == {}
        finally:
            os.unlink(path)

    def test_negative_cost_treated_as_data_not_crash(self, db):
        """A negative cost_usd (shouldn't happen, but) doesn't crash; the rate
        just reflects the (bad) sum. NaN/negative guard returns None only when the
        final rate is negative."""
        now = _now_utc()
        rows = [(now - 100, "openrouter", "m", 1000, -0.0001) for _ in range(100)]
        _seed(db, rows)
        rate = get_real_rate("openrouter", "m", db_path=db, _now=now)
        # final rate negative → None
        assert rate is None

    def test_get_rate_with_fallback_never_raises_on_bad_db(self):
        clear_cache()
        missing = os.path.join(tempfile.gettempdir(), "rpt_bad_fb.db")
        try:
            rate = get_rate_with_fallback("ppq", db_path=missing, _now=_now_utc())
            assert isinstance(rate, float)
            assert rate == LAST_RESORT_RATES["ppq"]
        finally:
            if os.path.exists(missing):
                os.unlink(missing)
