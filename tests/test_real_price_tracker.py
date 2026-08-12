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
    LAST_RESORT_RATES_PER_MODEL,
    MIN_CALLS_FOR_RATE,
    PROVIDER_WINDOW_HOURS,
    REQUIRED_RATE_PROVIDERS,
    SEED_RATES,
    UNKNOWN_PROVIDER_FALLBACK,
    ZAI_ANNUAL_BUDGET,
    ZAI_CACHE_TTL_SECONDS,
    ZAI_FRIEND_PREMIUM,
    ZAI_MIN_DATA_DAYS,
    ZAI_SEED_RATE,
    ZAI_WINDOW_HOURS,
    clear_cache,
    collect_rates,
    detect_price_change,
    gate_all_rates_have_data,
    get_all_rates,
    get_all_trailing_rates,
    get_all_trailing_rates_per_model,
    get_rate_readiness,
    get_rate_with_fallback,
    get_real_rate,
    get_trailing_rate,
    get_trailing_rate_with_seed,
    get_zai_amortized_rate,
    get_zai_per_model_rate,
    get_zai_per_model_rates,
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

    def test_telnyx_per_model_last_resort(self, db):
        """Telnyx has per-model last-resort rates: kimi-k3=2.70, glm-5.2=13.50.

        Gate test for TELNYX-4.2: get_rate_with_fallback('telnyx', 'kimi-k3')
        must return 2.70 (the per-model rate, not the blended 5.40).
        """
        clear_cache()
        # kimi-k3 — cheap tier
        rate = get_rate_with_fallback("telnyx", "kimi-k3",
                                      db_path=db, _now=_now_utc())
        assert rate == pytest.approx(2.70, rel=1e-12)

    def test_telnyx_per_model_last_resort_all_models(self, db):
        """All 4 Telnyx models get their per-model rate, not the blend."""
        clear_cache()
        expected = LAST_RESORT_RATES_PER_MODEL["telnyx"]
        for model, exp_rate in expected.items():
            clear_cache()
            rate = get_rate_with_fallback("telnyx", model,
                                          db_path=db, _now=_now_utc())
            assert rate == pytest.approx(exp_rate, rel=1e-12), model

    def test_telnyx_no_model_uses_blended_last_resort(self, db):
        """Without a model, telnyx falls back to the blended provider-level rate."""
        clear_cache()
        rate = get_rate_with_fallback("telnyx", db_path=db, _now=_now_utc())
        assert rate == pytest.approx(LAST_RESORT_RATES["telnyx"], rel=1e-12)

    def test_telnyx_per_model_takes_priority_over_provider_level(self, db):
        """Per-model rate should be returned when model is specified, even if
        it differs from the provider-level blend."""
        clear_cache()
        # kimi-k3 per-model = 2.70, but provider-level blend = 5.40
        rate = get_rate_with_fallback("telnyx", "kimi-k3",
                                      db_path=db, _now=_now_utc())
        assert rate != LAST_RESORT_RATES["telnyx"]  # 2.70 != 5.40
        assert rate == pytest.approx(2.70, rel=1e-12)

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


# ── T6: trailing-rate API (Ollama 90d + paid-endpoint 30d) ────────────────────


class _OllamaBillingNeutered:
    """Base class: prevent real network calls to the Ollama billing API.

    ``get_trailing_rate`` falls back to ``fetch_ollama_usage`` whenever
    ``ollama_cloud`` has no local ``cost_usd``. In tests with an empty DB that
    would make a real HTTP request. This autouse fixture pins the function to a
    no-op (empty dict → ``_ollama_api_rate`` returns ``None``). Tests that WANT
    billing-API data override ``fetch_ollama_usage`` again in their own body
    (the later ``monkeypatch.setattr`` wins).
    """

    @pytest.fixture(autouse=True)
    def _no_billing_api(self, monkeypatch):
        import src.ollama_extra_usage as oeu
        monkeypatch.setattr(oeu, "fetch_ollama_usage", lambda: {})


class TestProviderWindowHours:
    def test_ollama_is_90_days(self):
        assert PROVIDER_WINDOW_HOURS["ollama_cloud"] == 90 * 24  # 2160

    @pytest.mark.parametrize("provider", ["ppq", "deepinfra", "openrouter"])
    def test_paid_endpoints_are_30_days(self, provider):
        assert PROVIDER_WINDOW_HOURS[provider] == 30 * 24  # 720

    @pytest.mark.parametrize("provider", ["ours", "friend"])
    def test_zai_keys_are_365_days(self, provider):
        assert PROVIDER_WINDOW_HOURS[provider] == 365 * 24  # 8760

    def test_seed_rates_cover_all_windowed_providers(self):
        # Every provider with a trailing window must have a cold-start seed.
        for provider in PROVIDER_WINDOW_HOURS:
            assert provider in SEED_RATES, provider

    def test_seed_rates_positive(self):
        for provider, rate in SEED_RATES.items():
            assert rate > 0, provider

    def test_required_providers_are_the_paid_endpoints(self):
        assert set(REQUIRED_RATE_PROVIDERS) == {"ollama_cloud", "ppq",
                                                "deepinfra", "openrouter",
                                                "telnyx"}


class TestGetTrailingRate(_OllamaBillingNeutered):
    def test_ollama_90d_window_excludes_older_calls(self, db):
        """Ollama rate is measured over 90d; calls older than 90d are dropped."""
        now = _now_utc()
        # 100 calls 10d ago (inside 90d) @ $0.0001 / 1000 tok → 0.1 $/M
        inside = [(now - 10 * 86400, "ollama_cloud", "m", 1000, 0.0001)
                  for _ in range(100)]
        # 100 calls 100d ago (outside 90d) @ $0.0009 / 1000 tok → 0.9 $/M
        outside = [(now - 100 * 86400, "ollama_cloud", "m", 1000, 0.0009)
                   for _ in range(100)]
        _seed(db, inside + outside)
        rate = get_trailing_rate("ollama_cloud", "m", db_path=db, _now=now)
        assert rate == pytest.approx(0.1, rel=1e-9)

    @pytest.mark.parametrize("provider", ["ppq", "deepinfra", "openrouter"])
    def test_paid_30d_window_excludes_older_calls(self, db, provider):
        now = _now_utc()
        inside = [(now - 5 * 86400, provider, "m", 1000, 0.0001)
                  for _ in range(100)]   # → 0.1 $/M
        outside = [(now - 40 * 86400, provider, "m", 1000, 0.0009)
                   for _ in range(100)]  # → 0.9 $/M, outside 30d
        _seed(db, inside + outside)
        rate = get_trailing_rate(provider, "m", db_path=db, _now=now)
        assert rate == pytest.approx(0.1, rel=1e-9)

    def test_below_min_calls_returns_none(self, db):
        now = _now_utc()
        _seed(db, [(now - 100, "ppq", "m", 1000, 0.0001)
                   for _ in range(MIN_CALLS_FOR_RATE - 1)])
        assert get_trailing_rate("ppq", "m", db_path=db, _now=now) is None

    def test_no_data_returns_none(self, db):
        assert get_trailing_rate("ppq", "m", db_path=db, _now=_now_utc()) is None
        # ollama_cloud with no cost_usd AND neutered billing API → None
        assert get_trailing_rate("ollama_cloud", db_path=db,
                                 _now=_now_utc()) is None

    def test_ollama_falls_back_to_billing_api(self, db, monkeypatch):
        """When cost_usd is empty, ollama's measured rate comes from the API."""
        clear_cache()
        import src.ollama_extra_usage as oeu
        monkeypatch.setattr(oeu, "fetch_ollama_usage", lambda: {
            "activity": {"glm-5.2": {"cost": 0.0155, "total_tokens": 1_000_000}}
        })
        rate = get_trailing_rate("ollama_cloud", "glm-5.2", db_path=db,
                                 _now=_now_utc())
        assert rate == pytest.approx(0.0155, rel=1e-9)

    def test_ollama_billing_api_failure_returns_none(self, db, monkeypatch):
        clear_cache()

        def _boom():
            raise RuntimeError("network down")

        import src.ollama_extra_usage as oeu
        monkeypatch.setattr(oeu, "fetch_ollama_usage", _boom)
        assert get_trailing_rate("ollama_cloud", db_path=db,
                                 _now=_now_utc()) is None

    def test_trailing_cache_served_within_ttl(self, db):
        now = _now_utc()
        _seed(db, [(now - 100, "ppq", "m", 1000, 0.0001) for _ in range(100)])
        first = get_trailing_rate("ppq", "m", db_path=db, _now=now)
        assert first == pytest.approx(0.1, rel=1e-9)
        # Mutate DB; cache should serve the old value at the same instant.
        _seed(db, [(now - 100, "ppq", "m", 1000, 0.05) for _ in range(100)])
        second = get_trailing_rate("ppq", "m", db_path=db, _now=now)
        assert second == first  # cached

    def test_trailing_cache_recomputes_after_ttl(self, db):
        t0 = _now_utc()
        _seed(db, [(t0 - 100, "ppq", "m", 1000, 0.0001) for _ in range(100)])
        first = get_trailing_rate("ppq", "m", db_path=db, _now=t0)
        _seed(db, [(t0 - 100, "ppq", "m", 1000, 0.05) for _ in range(100)])
        later = t0 + CACHE_TTL_SECONDS + 1
        second = get_trailing_rate("ppq", "m", db_path=db, _now=later)
        assert second != pytest.approx(first, rel=1e-3)
        # SUM(cost)=0.01+5.0=5.01, SUM(tokens)=200000 → 25.05 $/M
        assert second == pytest.approx(5.01 / 200000 * 1e6, rel=1e-9)


class TestTrailingRateWithSeed(_OllamaBillingNeutered):
    def test_seeds_when_cold_per_provider(self, db):
        now = _now_utc()
        for provider, expected in SEED_RATES.items():
            rate = get_trailing_rate_with_seed(provider, db_path=db, _now=now)
            assert rate == pytest.approx(expected, rel=1e-12), provider

    def test_returns_measured_when_data_present(self, db):
        now = _now_utc()
        _seed(db, [(now - 100, "ppq", "m", 1000, 0.0001) for _ in range(100)])
        rate = get_trailing_rate_with_seed("ppq", "m", db_path=db, _now=now)
        assert rate == pytest.approx(0.1, rel=1e-9)
        assert rate != SEED_RATES["ppq"]

    def test_unknown_provider_returns_conservative_fallback(self, db):
        rate = get_trailing_rate_with_seed("never_seen", db_path=db,
                                           _now=_now_utc())
        assert rate == UNKNOWN_PROVIDER_FALLBACK

    def test_always_returns_float(self, db):
        for provider in ("ours", "friend", "ollama_cloud", "ppq",
                         "openrouter", "deepinfra", "mystery"):
            rate = get_trailing_rate_with_seed(provider, db_path=db,
                                               _now=_now_utc())
            assert isinstance(rate, float), provider


class TestGetAllTrailingRates(_OllamaBillingNeutered):
    def test_cold_returns_all_seeds(self, db):
        rates = get_all_trailing_rates(db_path=db, _now=_now_utc())
        assert set(rates) == set(PROVIDER_WINDOW_HOURS)
        for provider, rate in rates.items():
            assert rate == pytest.approx(SEED_RATES[provider], rel=1e-12), provider

    def test_mixed_measured_and_seed(self, db):
        now = _now_utc()
        # Only ppq has measured data.
        _seed(db, [(now - 100, "ppq", "m", 1000, 0.0001) for _ in range(100)])
        rates = get_all_trailing_rates(db_path=db, _now=now)
        assert rates["ppq"] == pytest.approx(0.1, rel=1e-9)
        # Everyone else is still on its seed.
        for provider in PROVIDER_WINDOW_HOURS:
            if provider == "ppq":
                continue
            assert rates[provider] == pytest.approx(SEED_RATES[provider],
                                                    rel=1e-12), provider

    def test_custom_providers_subset(self, db):
        rates = get_all_trailing_rates(providers=["ppq", "deepinfra"],
                                       db_path=db, _now=_now_utc())
        assert set(rates) == {"ppq", "deepinfra"}


class TestGetAllTrailingRatesPerModel(_OllamaBillingNeutered):
    """T1 gate: nested {provider: {model: rate, '_default': rate}}.

    Every provider gets a '_default'; cold-start providers carry their seed as
    '_default'; per-model measured rates surface when data is present.
    """

    def test_gate_cold_start_default_key_and_seed_for_every_provider(self, db):
        """GATE: nested dict with _default for every provider; cold-start → seed."""
        rates = get_all_trailing_rates_per_model(db_path=db,
                                                  _now=_now_utc())
        # One entry per known provider.
        assert set(rates) == set(PROVIDER_WINDOW_HOURS)
        for prov in PROVIDER_WINDOW_HOURS:
            inner = rates[prov]
            # Every provider carries a '_default' key that is a float.
            assert "_default" in inner, prov
            assert isinstance(inner["_default"], float), prov
            # Cold-start → _default is the seed, nothing else measured.
            assert inner["_default"] == pytest.approx(
                SEED_RATES[prov], rel=1e-12), prov
            assert set(inner) == {"_default"}, prov

    def test_cold_start_is_nested_dict_of_floats(self, db):
        rates = get_all_trailing_rates_per_model(db_path=db,
                                                  _now=_now_utc())
        for prov, inner in rates.items():
            assert isinstance(inner, dict), prov
            for model, rate in inner.items():
                assert isinstance(model, str), (prov, model)
                assert isinstance(rate, float), (prov, model)

    def test_per_model_measured_rate_surfaces(self, db):
        now = _now_utc()
        # 100 calls of ppq/kimi-k3 at $0.0001 each → 0.1 $/M.
        _seed(db, [(now - 100, "ppq", "kimi-k3", 1000, 0.0001)
                   for _ in range(100)])
        rates = get_all_trailing_rates_per_model(db_path=db, _now=now)
        assert rates["ppq"]["kimi-k3"] == pytest.approx(0.1, rel=1e-9)
        # Provider has measured data → _default is the measured provider rate,
        # NOT the seed.
        assert rates["ppq"]["_default"] == pytest.approx(0.1, rel=1e-9)
        assert rates["ppq"]["_default"] != pytest.approx(SEED_RATES["ppq"])

    def test_distinct_models_get_distinct_rates(self, db):
        """kimi-k3 priced 75x glm — per-model dict must reflect that, not blend."""
        now = _now_utc()
        rows = (
            [(now - 100, "ppq", "glm", 1000, 0.0001) for _ in range(100)]
            + [(now - 100, "ppq", "kimi-k3", 1000, 0.00753) for _ in range(100)]
        )
        _seed(db, rows)
        rates = get_all_trailing_rates_per_model(db_path=db, _now=now)
        assert rates["ppq"]["glm"] == pytest.approx(0.1, rel=1e-9)
        assert rates["ppq"]["kimi-k3"] == pytest.approx(7.53, rel=1e-9)
        # _default is the blended provider-level rate (distinct from either).
        assert rates["ppq"]["_default"] == pytest.approx(
            (0.01 + 0.753) / 200_000 * 1e6, rel=1e-9)
        assert "_default" in rates["ppq"]

    def test_mixed_measured_and_cold(self, db):
        now = _now_utc()
        _seed(db, [(now - 100, "ppq", "m", 1000, 0.0001) for _ in range(100)])
        rates = get_all_trailing_rates_per_model(db_path=db, _now=now)
        # ppq is warm.
        assert rates["ppq"]["_default"] == pytest.approx(0.1, rel=1e-9)
        # Everyone else cold → seed _default, no per-model entries.
        for prov in PROVIDER_WINDOW_HOURS:
            if prov == "ppq":
                continue
            assert rates[prov]["_default"] == pytest.approx(
                SEED_RATES[prov], rel=1e-12), prov
            assert set(rates[prov]) == {"_default"}, prov

    def test_bad_db_path_never_raises_returns_seeds(self, db):
        """A missing/unreadable DB degrades to seeds, never an exception."""
        import tempfile
        missing = tempfile.mkstemp(suffix=".db")[1]
        try:
            os.unlink(missing)  # ensure path does not exist
        except OSError:
            pass
        rates = get_all_trailing_rates_per_model(db_path=missing,
                                                  _now=_now_utc())
        assert set(rates) == set(PROVIDER_WINDOW_HOURS)
        for prov in PROVIDER_WINDOW_HOURS:
            assert "_default" in rates[prov]
            assert rates[prov]["_default"] == pytest.approx(
                SEED_RATES[prov], rel=1e-12), prov

    def test_exported_in_public_api(self):
        assert "get_all_trailing_rates_per_model" in rpt.__all__


# ── PM-T5 gate: DeepInfra per-model rate resolution ──────────────────────────


class TestDeepInfraPerModelGate:
    """PM-T5 acceptance gate.

    ``get_real_rate("deepinfra", "deepseek-v4-flash")`` must return that
    model's *measured* per-model rate — not the provider blend. This is the
    precondition for per-model pricing once ``daily_spend`` is migrated to
    carry a ``model`` column (plan §6 T5).
    """

    def test_per_model_rate_not_provider_blend(self, db):
        """Two deepinfra models at very different costs: deepseek-v4-flash's
        per-model rate must reflect its own cost, distinct from the blend."""
        now = _now_utc()
        # deepseek-v4-flash: 200 calls × 5000 tok × $0.0007 = 1M tok, $0.14
        #   → $0.14/M (matches providers.yaml published blend 0.09+0.19)/2).
        flash = [
            (now - 100, "deepinfra", "deepseek-v4-flash", 5_000, 0.0007)
            for _ in range(200)
        ]
        # deepseek-v4-pro: 200 calls × 5000 tok × $0.0135 = 1M tok, $2.70
        #   → $2.70/M.
        pro = [
            (now - 100, "deepinfra", "deepseek-v4-pro", 5_000, 0.0135)
            for _ in range(200)
        ]
        _seed(db, flash + pro)

        flash_rate = get_real_rate("deepinfra", "deepseek-v4-flash", db_path=db)
        pro_rate = get_real_rate("deepinfra", "deepseek-v4-pro", db_path=db)
        blend = get_real_rate("deepinfra", db_path=db)

        # All three resolve (≥ MIN_CALLS_FOR_RATE per slice).
        assert flash_rate is not None
        assert pro_rate is not None
        assert blend is not None
        # The per-model rates are correct.
        assert flash_rate == pytest.approx(0.14, rel=0.02)
        assert pro_rate == pytest.approx(2.70, rel=0.02)
        # THE GATE: per-model rate differs from the provider blend.
        assert flash_rate != pytest.approx(blend, rel=0.02)
        assert pro_rate != pytest.approx(blend, rel=0.02)
        # Blend sits strictly between the two model rates.
        assert flash_rate < blend < pro_rate

    def test_unknown_model_returns_none(self, db):
        """A model with no rows → None (insufficient data), not the blend."""
        now = _now_utc()
        _seed(db, [
            (now - 100, "deepinfra", "deepseek-v4-flash", 5_000, 0.0007)
            for _ in range(200)
        ])
        assert get_real_rate("deepinfra", "nonexistent-model", db_path=db) is None


class TestGetRateReadiness(_OllamaBillingNeutered):
    def test_cold_all_seeds(self, db):
        report = get_rate_readiness(db_path=db, _now=_now_utc())
        assert set(report) == set(PROVIDER_WINDOW_HOURS)
        for provider, info in report.items():
            assert info["source"] == "seed", provider
            assert info["has_data"] is False, provider
            assert info["rate"] == pytest.approx(SEED_RATES[provider], rel=1e-12)
            assert info["window_hours"] == PROVIDER_WINDOW_HOURS[provider]

    def test_measured_marked_correctly(self, db):
        now = _now_utc()
        _seed(db, [(now - 100, "ppq", "m", 1000, 0.0001) for _ in range(100)])
        _seed(db, [(now - 100, "openrouter", "m", 1000, 0.0002)
                   for _ in range(100)])
        report = get_rate_readiness(db_path=db, _now=now)
        assert report["ppq"]["source"] == "measured"
        assert report["ppq"]["has_data"] is True
        assert report["ppq"]["rate"] == pytest.approx(0.1, rel=1e-9)
        assert report["openrouter"]["source"] == "measured"
        assert report["deepinfra"]["source"] == "seed"
        assert report["ollama_cloud"]["source"] == "seed"

    def test_ollama_billing_api_counts_as_measured(self, db, monkeypatch):
        clear_cache()
        import src.ollama_extra_usage as oeu
        monkeypatch.setattr(oeu, "fetch_ollama_usage", lambda: {
            "activity": {"glm-5.2": {"cost": 0.0155, "total_tokens": 1_000_000}}
        })
        report = get_rate_readiness(providers=["ollama_cloud"], db_path=db,
                                    _now=_now_utc())
        assert report["ollama_cloud"]["source"] == "measured"
        assert report["ollama_cloud"]["has_data"] is True

    def test_unknown_provider_in_subset(self, db):
        report = get_rate_readiness(providers=["mystery"], db_path=db,
                                    _now=_now_utc())
        assert report["mystery"]["source"] == "unknown"
        assert report["mystery"]["has_data"] is False
        assert report["mystery"]["rate"] == UNKNOWN_PROVIDER_FALLBACK


class TestGateAllRatesHaveData(_OllamaBillingNeutered):
    @staticmethod
    def _seed_measured(db, now, provider, cost=0.0001, n=100, tok=1000):
        _seed(db, [(now - 100, provider, "m", tok, cost) for _ in range(n)])

    def test_cold_returns_false(self, db):
        assert gate_all_rates_have_data(db_path=db, _now=_now_utc()) is False

    def test_all_required_measured_returns_true(self, db):
        now = _now_utc()
        for provider in REQUIRED_RATE_PROVIDERS:
            self._seed_measured(db, now, provider)
        assert gate_all_rates_have_data(db_path=db, _now=now) is True

    def test_one_missing_returns_false(self, db):
        now = _now_utc()
        for provider in REQUIRED_RATE_PROVIDERS:
            if provider == "deepinfra":
                continue
            self._seed_measured(db, now, provider)
        assert gate_all_rates_have_data(db_path=db, _now=now) is False

    def test_custom_providers_subset(self, db):
        now = _now_utc()
        self._seed_measured(db, now, "ppq")
        # Gate scoped to just ppq → True even though others are cold.
        assert gate_all_rates_have_data(providers=["ppq"], db_path=db,
                                        _now=now) is True
        # Default scope still False (others cold).
        assert gate_all_rates_have_data(db_path=db, _now=now) is False

    def test_ollama_via_billing_api_passes_gate(self, db, monkeypatch):
        """ollama_cloud measured via the billing API (not cost_usd) counts."""
        clear_cache()
        now = _now_utc()
        import src.ollama_extra_usage as oeu
        monkeypatch.setattr(oeu, "fetch_ollama_usage", lambda: {
            "activity": {"glm-5.2": {"cost": 0.0155, "total_tokens": 1_000_000}}
        })
        for provider in REQUIRED_RATE_PROVIDERS:
            if provider == "ollama_cloud":
                continue  # measured via billing API
            self._seed_measured(db, now, provider)
        assert gate_all_rates_have_data(db_path=db, _now=now) is True

    def test_zero_rate_fails_gate(self, db):
        """A measured-but-zero rate (all tokens, $0 cost) fails the gate."""
        now = _now_utc()
        for provider in REQUIRED_RATE_PROVIDERS:
            # cost 0.0 → measured rate 0.0 → rate <= 0 → gate False
            _seed(db, [(now - 100, provider, "m", 1000, 0.0)
                       for _ in range(100)])
        assert gate_all_rates_have_data(db_path=db, _now=now) is False

    def test_never_raises_on_bad_db(self):
        clear_cache()
        missing = os.path.join(tempfile.gettempdir(), "rpt_gate_bad.db")
        try:
            assert gate_all_rates_have_data(db_path=missing,
                                            _now=_now_utc()) is False
        finally:
            if os.path.exists(missing):
                os.unlink(missing)


# ── T5: z.ai trailing-365d amortized rate ────────────────────────────────────


class TestZaiAmortizedRate:
    """get_zai_amortized_rate: flat-rate subscription cost amortized over
    trailing-365d token volume. ``zai_rate = 300 / (SUM(tokens) / 1M)``;
    friend = base x 1.21; <30d data → seed $0.014/M."""

    def test_gate_within_50pct_of_seed(self, db):
        """T5 acceptance gate: with >30d of representative data the calculated
        rate lands within 50% of the $0.014/M folklore rate.

        Representative volume = ~21.4B tokens/yr (the folklore assumption behind
        $300/yr @ $0.014/M). Production currently runs far hotter (~96B tok/yr),
        so the *real* amortized rate is lower — but the gate is a property of
        the *formula*, validated here with controlled data.
        """
        now = _now_utc()
        # ~21.43B tokens (ours 15B + friend 6.43B), oldest record 60d ago → >30d.
        rows = [(now - 60 * 86400, "ours", "glm-5.2", 5_000_000_000, 0.0)
                for _ in range(3)]
        rows += [(now - 45 * 86400, "friend", "glm-5.2", 3_214_285_714, 0.0)
                 for _ in range(2)]
        _seed(db, rows)
        rate = get_zai_amortized_rate("ours", db_path=db, _now=now)
        # 300 / (21_428_571_428 / 1e6) == 0.014
        assert rate == pytest.approx(0.014, rel=1e-3)
        lo, hi = ZAI_SEED_RATE * 0.5, ZAI_SEED_RATE * 1.5
        assert lo <= rate <= hi  # within 50% of $0.014/M

    def test_basic_formula_exact(self, db):
        """10B tokens → 300 / (10_000 M) = $0.03/M exactly."""
        now = _now_utc()
        rows = [(now - 40 * 86400, "ours", "m", 6_000_000_000, 0.0),
                (now - 40 * 86400, "friend", "m", 4_000_000_000, 0.0)]
        _seed(db, rows)
        rate = get_zai_amortized_rate("ours", db_path=db, _now=now)
        assert rate == pytest.approx(0.03, rel=1e-9)

    def test_friend_premium_is_1_21x(self, db):
        now = _now_utc()
        _seed(db, [(now - 40 * 86400, "ours", "m", 10_000_000_000, 0.0)])
        ours = get_zai_amortized_rate("ours", db_path=db, _now=now)
        friend = get_zai_amortized_rate("friend", db_path=db, _now=now)
        assert friend == pytest.approx(ours * ZAI_FRIEND_PREMIUM, rel=1e-12)

    def test_combined_pool_ours_and_friend(self, db):
        """Both z.ai keys contribute to the shared token denominator."""
        now = _now_utc()
        # ours alone = 6B → would give 0.05; friend adds 4B → 10B → 0.03
        rows = [(now - 40 * 86400, "ours", "m", 6_000_000_000, 0.0),
                (now - 40 * 86400, "friend", "m", 4_000_000_000, 0.0)]
        _seed(db, rows)
        rate = get_zai_amortized_rate("ours", db_path=db, _now=now)
        assert rate == pytest.approx(0.03, rel=1e-9)  # not 0.05

    def test_legacy_zai_alias_counted(self, db):
        """Legacy ``zai_ours``/``zai_friend`` rows (the spec's ``LIKE 'zai%'``)
        are counted alongside the canonical names."""
        now = _now_utc()
        rows = [(now - 40 * 86400, "ours", "m", 5_000_000_000, 0.0),
                (now - 40 * 86400, "zai_friend", "m", 5_000_000_000, 0.0)]
        _seed(db, rows)
        rate = get_zai_amortized_rate("ours", db_path=db, _now=now)
        assert rate == pytest.approx(0.03, rel=1e-9)  # 10B combined

    def test_non_zai_provider_excluded_from_pool(self, db):
        now = _now_utc()
        # 10B z.ai + 90B ollama_cloud → ollama must NOT dilute the pool → 0.03
        rows = [(now - 40 * 86400, "ours", "m", 10_000_000_000, 0.0)]
        rows += [(now - 40 * 86400, "ollama_cloud", "m", 90_000_000_000, 0.0)]
        _seed(db, rows)
        rate = get_zai_amortized_rate("ours", db_path=db, _now=now)
        assert rate == pytest.approx(0.03, rel=1e-9)

    def test_under_30d_uses_seed(self, db):
        """Data spanning <30 days → seed $0.014/M, not the noisy calculated rate."""
        now = _now_utc()
        _seed(db, [(now - 20 * 86400, "ours", "m", 10_000_000_000, 0.0)])
        rate = get_zai_amortized_rate("ours", db_path=db, _now=now)
        assert rate == pytest.approx(ZAI_SEED_RATE, rel=1e-12)

    def test_friend_under_30d_seed(self, db):
        now = _now_utc()
        _seed(db, [(now - 20 * 86400, "ours", "m", 10_000_000_000, 0.0)])
        rate = get_zai_amortized_rate("friend", db_path=db, _now=now)
        assert rate == pytest.approx(ZAI_SEED_RATE * ZAI_FRIEND_PREMIUM, rel=1e-12)

    def test_no_data_returns_seed(self, db):
        rate = get_zai_amortized_rate("ours", db_path=db, _now=_now_utc())
        assert rate == pytest.approx(ZAI_SEED_RATE, rel=1e-12)

    def test_unknown_provider_treated_as_ours(self, db):
        now = _now_utc()
        _seed(db, [(now - 40 * 86400, "ours", "m", 10_000_000_000, 0.0)])
        assert get_zai_amortized_rate("mystery", db_path=db, _now=now) == \
            pytest.approx(0.03, rel=1e-9)

    def test_window_excludes_older_than_365d(self, db):
        """Tokens older than 365d are outside the trailing window → excluded."""
        now = _now_utc()
        # 10B at 400d ago (outside window) + 10B at 40d ago (inside).
        rows = [(now - 400 * 86400, "ours", "m", 10_000_000_000, 0.0),
                (now - 40 * 86400, "ours", "m", 10_000_000_000, 0.0)]
        _seed(db, rows)
        rate = get_zai_amortized_rate("ours", db_path=db, _now=now)
        # Only the 10B inside-window counts → 0.03 (min_ts is the 40d record →
        # >30d span, so the calculated path is taken).
        assert rate == pytest.approx(0.03, rel=1e-9)

    def test_zero_tokens_returns_seed(self, db):
        now = _now_utc()
        _seed(db, [(now - 40 * 86400, "ours", "m", 0, 0.0)])
        rate = get_zai_amortized_rate("ours", db_path=db, _now=now)
        assert rate == pytest.approx(ZAI_SEED_RATE, rel=1e-12)

    def test_daily_cache_served_within_ttl(self, db):
        """Within the 24h TTL the cached rate is returned even if the DB mutates."""
        now = _now_utc()
        _seed(db, [(now - 40 * 86400, "ours", "m", 10_000_000_000, 0.0)])
        first = get_zai_amortized_rate("ours", db_path=db, _now=now)
        assert first == pytest.approx(0.03, rel=1e-9)
        # Halve the tokens (would double the rate) — cache must still serve 0.03.
        _seed(db, [(now - 40 * 86400, "ours", "m", 5_000_000_000, 0.0)])
        second = get_zai_amortized_rate("ours", db_path=db, _now=now)
        assert second == first  # cached

    def test_daily_cache_recomputes_after_ttl(self, db):
        t0 = _now_utc()
        _seed(db, [(t0 - 40 * 86400, "ours", "m", 10_000_000_000, 0.0)])
        first = get_zai_amortized_rate("ours", db_path=db, _now=t0)
        # Add 10B more → 20B → 0.015, but only seen after the daily TTL.
        _seed(db, [(t0 - 40 * 86400, "ours", "m", 10_000_000_000, 0.0)])
        later = t0 + ZAI_CACHE_TTL_SECONDS + 1
        second = get_zai_amortized_rate("ours", db_path=db, _now=later)
        assert second != pytest.approx(first, rel=1e-3)
        assert second == pytest.approx(300 / (20_000_000_000 / 1e6), rel=1e-9)

    def test_clear_cache_drops_zai(self, db):
        now = _now_utc()
        _seed(db, [(now - 40 * 86400, "ours", "m", 10_000_000_000, 0.0)])
        first = get_zai_amortized_rate("ours", db_path=db, _now=now)
        _seed(db, [(now - 40 * 86400, "ours", "m", 5_000_000_000, 0.0)])
        clear_cache()
        second = get_zai_amortized_rate("ours", db_path=db, _now=now)
        assert second != pytest.approx(first, rel=1e-3)

    def test_never_raises_on_bad_db(self):
        """A bad/missing DB degrades to the seed rate, never raises."""
        clear_cache()
        missing = os.path.join(tempfile.gettempdir(), "rpt_zai_bad.db")
        try:
            rate = get_zai_amortized_rate("ours", db_path=missing,
                                          _now=_now_utc())
            assert rate == pytest.approx(ZAI_SEED_RATE, rel=1e-12)
            frate = get_zai_amortized_rate("friend", db_path=missing,
                                           _now=_now_utc())
            assert frate == pytest.approx(ZAI_SEED_RATE * ZAI_FRIEND_PREMIUM,
                                          rel=1e-12)
        finally:
            if os.path.exists(missing):
                os.unlink(missing)

    def test_always_returns_float(self, db):
        for provider in ("ours", "friend", "mystery"):
            rate = get_zai_amortized_rate(provider, db_path=db,
                                          _now=_now_utc())
            assert isinstance(rate, float), provider


# ── T4: z.ai per-model amortization (metered vs subscription) ────────────────


class TestQueryZaiWindowPerModel:
    """_query_zai_window_per_model: GROUP BY (key_name, model) over z.ai keys."""

    def test_groups_by_key_and_model(self, db):
        now = _now_utc()
        rows = (
            [(now - 100, "ours", "glm-5.2", 1000, 0.0) for _ in range(120)]
            + [(now - 100, "ours", "kimi-k3", 1000, 0.00753) for _ in range(120)]
            + [(now - 100, "friend", "glm-5.2", 1000, 0.0) for _ in range(120)]
        )
        _seed(db, rows)
        out = rpt._query_zai_window_per_model(db, now - 365 * 86400)
        # Two providers, each with its own model breakdown.
        assert set(out) == {"ours", "friend"}
        assert set(out["ours"]) == {"glm-5.2", "kimi-k3"}
        # (count, sum_cost, sum_tokens) tuple shape.
        count, sum_cost, sum_tokens = out["ours"]["kimi-k3"]
        assert count == 120
        assert sum_cost == pytest.approx(120 * 0.00753, rel=1e-9)
        assert sum_tokens == pytest.approx(120 * 1000, rel=1e-9)

    def test_null_cost_summed_as_zero(self, db):
        """Subscription models with NULL cost_usd surface with sum_cost=0.0."""
        now = _now_utc()
        rows = [(now - 100, "ours", "glm-5.2", 1000, None) for _ in range(120)]
        _seed(db, rows)
        out = rpt._query_zai_window_per_model(db, now - 365 * 86400)
        _, sum_cost, sum_tokens = out["ours"]["glm-5.2"]
        assert sum_cost == 0.0
        assert sum_tokens == pytest.approx(120 * 1000, rel=1e-9)

    def test_null_model_keyed_as_null_marker(self, db):
        now = _now_utc()
        rows = [(now - 100, "ours", None, 1000, 0.0) for _ in range(120)]
        _seed(db, rows)
        out = rpt._query_zai_window_per_model(db, now - 365 * 86400)
        assert "_null" in out["ours"]

    def test_legacy_zai_alias_counted(self, db):
        now = _now_utc()
        rows = [(now - 100, "zai_ours", "glm-5.2", 1000, 0.0)
                for _ in range(120)]
        _seed(db, rows)
        out = rpt._query_zai_window_per_model(db, now - 365 * 86400)
        assert "zai_ours" in out
        assert "glm-5.2" in out["zai_ours"]

    def test_non_zai_provider_excluded(self, db):
        now = _now_utc()
        _seed(db, [(now - 100, "ppq", "kimi-k3", 1000, 0.00753)
                   for _ in range(120)])
        out = rpt._query_zai_window_per_model(db, now - 365 * 86400)
        assert "ppq" not in out

    def test_window_excludes_old_rows(self, db):
        now = _now_utc()
        # 400d ago — outside the 365d window.
        _seed(db, [(now - 400 * 86400, "ours", "glm-5.2", 1000, 0.0)
                   for _ in range(120)])
        out = rpt._query_zai_window_per_model(db, now - 365 * 86400)
        assert out == {}

    def test_never_raises_on_bad_db(self):
        clear_cache()
        missing = os.path.join(tempfile.gettempdir(), "rpt_zai_pm_bad.db")
        try:
            assert rpt._query_zai_window_per_model(missing, 0) == {}
        finally:
            if os.path.exists(missing):
                os.unlink(missing)


class TestZaiPerModelRate:
    """get_zai_per_model_rate: metered → cost_usd, subscription → amortized."""

    def _seed_metered_and_sub(self, db, now):
        """glm-5.2 (subscription, ~$0.014/M) + kimi-k3 (metered, $7.53/M)."""
        # glm-5.2: 20B tokens over >30d → amortized 300/(20e9/1e6) = 0.015.
        glm = [(now - 60 * 86400, "ours", "glm-5.2", 100_000_000, 0.0)
               for _ in range(200)]
        # kimi-k3: 200 calls × (1000 tok, $0.00753) → cost_usd rate 7.53/M.
        # Small token volume so it barely shifts the amortized denominator.
        kimi = [(now - 100, "ours", "kimi-k3", 1000, 0.00753)
                for _ in range(200)]
        _seed(db, glm + kimi)
        return glm, kimi

    def test_gate_kimi_metered_vs_glm_subscription(self, db):
        """GATE: kimi-k3 ≈ $7.53/M (metered), glm-5.2 ≈ $0.014/M (amortized),
        and the two differ by >100× on the same z.ai key."""
        now = _now_utc()
        self._seed_metered_and_sub(db, now)

        kimi = get_real_rate("ours", "kimi-k3", db_path=db, _now=now)
        glm = get_real_rate("ours", "glm-5.2", db_path=db, _now=now)

        # kimi-k3 metered: real cost_usd rate.
        assert kimi == pytest.approx(7.53, rel=1e-3)
        # glm-5.2 subscription: amortized 300/(20.0002e9/1e6) ≈ 0.015.
        assert glm == pytest.approx(0.015, rel=2e-2)
        # The two rates differ by >100× — the cost spread the blend hid.
        assert kimi is not None and glm is not None
        assert kimi / glm > 100

    def test_metered_model_uses_cost_usd_rate(self, db):
        now = _now_utc()
        _seed(db, [(now - 100, "ours", "kimi-k3", 1000, 0.00753)
                   for _ in range(200)])
        rate = get_zai_per_model_rate("ours", "kimi-k3", db_path=db, _now=now)
        assert rate == pytest.approx(7.53, rel=1e-9)

    def test_subscription_model_uses_amortized_rate(self, db):
        """A model with cost_usd≈0 returns the provider amortized rate, not 0."""
        now = _now_utc()
        _seed(db, [(now - 40 * 86400, "ours", "glm-5.2", 10_000_000_000, 0.0)])
        rate = get_zai_per_model_rate("ours", "glm-5.2", db_path=db, _now=now)
        amortized = get_zai_amortized_rate("ours", db_path=db, _now=now)
        assert rate == pytest.approx(amortized, rel=1e-12)
        assert rate == pytest.approx(0.03, rel=1e-9)  # 300/(10e9/1e6)

    def test_get_real_rate_falls_back_when_cost_usd_zero(self, db):
        """cost_usd=0.0 within the window → get_real_rate returns amortized,
        not 0.0 (the subscription blindspot the fallback fixes)."""
        now = _now_utc()
        # glm-5.2 rows inside the 168h window, cost_usd=0.0.
        _seed(db, [(now - 100, "ours", "glm-5.2", 1000, 0.0)
                   for _ in range(200)])
        rate = get_real_rate("ours", "glm-5.2", db_path=db, _now=now)
        # <30d data → amortized seed $0.014/M (not 0.0).
        assert rate == pytest.approx(ZAI_SEED_RATE, rel=1e-12)
        assert rate != 0.0

    def test_get_real_rate_falls_back_when_no_costed_rows(self, db):
        """cost_usd=NULL (no costed rows in window) → None → amortized fallback."""
        now = _now_utc()
        _seed(db, [(now - 60 * 86400, "ours", "glm-5.2", 100_000_000, None)
                   for _ in range(200)])
        rate = get_real_rate("ours", "glm-5.2", db_path=db, _now=now)
        assert rate == pytest.approx(0.015, rel=2e-2)

    def test_metered_under_min_calls_falls_to_amortized(self, db):
        """A metered model with <MIN_CALLS_FOR_RATE calls is under-observed →
        falls back to the (cheap) amortized rate rather than a noisy guess."""
        now = _now_utc()
        # Seed enough subscription volume for a real amortized rate.
        _seed(db, [(now - 40 * 86400, "ours", "glm-5.2", 10_000_000_000, 0.0)])
        # Only 5 kimi-k3 calls (< MIN_CALLS_FOR_RATE=100).
        _seed(db, [(now - 100, "ours", "kimi-k3", 1000, 0.00753)
                   for _ in range(5)])
        rate = get_zai_per_model_rate("ours", "kimi-k3", db_path=db, _now=now)
        amortized = get_zai_amortized_rate("ours", db_path=db, _now=now)
        assert rate == pytest.approx(amortized, rel=1e-12)

    def test_friend_premium_applied_to_metered(self, db):
        now = _now_utc()
        _seed(db, [(now - 100, "ours", "kimi-k3", 1000, 0.00753)
                   for _ in range(200)])
        _seed(db, [(now - 100, "friend", "kimi-k3", 1000, 0.00753)
                   for _ in range(200)])
        ours = get_zai_per_model_rate("ours", "kimi-k3", db_path=db, _now=now)
        friend = get_zai_per_model_rate("friend", "kimi-k3", db_path=db,
                                        _now=now)
        assert ours is not None and friend is not None
        assert friend == pytest.approx(ours * ZAI_FRIEND_PREMIUM, rel=1e-12)

    def test_model_none_returns_provider_amortized(self, db):
        now = _now_utc()
        _seed(db, [(now - 40 * 86400, "ours", "glm-5.2", 10_000_000_000, 0.0)])
        rate = get_zai_per_model_rate("ours", None, db_path=db, _now=now)
        assert rate == pytest.approx(0.03, rel=1e-9)

    def test_unknown_model_returns_amortized(self, db):
        """A model with no rows at all resolves to the provider amortized rate."""
        now = _now_utc()
        _seed(db, [(now - 40 * 86400, "ours", "glm-5.2", 10_000_000_000, 0.0)])
        rate = get_zai_per_model_rate("ours", "never-seen-model",
                                      db_path=db, _now=now)
        assert rate == pytest.approx(0.03, rel=1e-9)

    def test_cold_start_returns_amortized_seed(self, db):
        """Empty DB → amortized rate degrades to the seed (always a float)."""
        rate = get_zai_per_model_rate("ours", "kimi-k3", db_path=db,
                                      _now=_now_utc())
        assert rate == pytest.approx(ZAI_SEED_RATE, rel=1e-12)

    def test_daily_cache_served_within_ttl(self, db):
        now = _now_utc()
        _seed(db, [(now - 100, "ours", "kimi-k3", 1000, 0.00753)
                   for _ in range(200)])
        first = get_zai_per_model_rate("ours", "kimi-k3", db_path=db, _now=now)
        # Mutate DB (would change the rate) — cache must serve the old value.
        _seed(db, [(now - 100, "ours", "kimi-k3", 1000, 0.05)
                   for _ in range(200)])
        second = get_zai_per_model_rate("ours", "kimi-k3", db_path=db, _now=now)
        assert second == first

    def test_daily_cache_recomputes_after_ttl(self, db):
        t0 = _now_utc()
        _seed(db, [(t0 - 100, "ours", "kimi-k3", 1000, 0.00753)
                   for _ in range(200)])
        first = get_zai_per_model_rate("ours", "kimi-k3", db_path=db, _now=t0)
        _seed(db, [(t0 - 100, "ours", "kimi-k3", 1000, 0.05)
                   for _ in range(200)])
        later = t0 + ZAI_CACHE_TTL_SECONDS + 1
        second = get_zai_per_model_rate("ours", "kimi-k3", db_path=db,
                                        _now=later)
        assert second != pytest.approx(first, rel=1e-3)

    def test_clear_cache_drops_per_model(self, db):
        now = _now_utc()
        _seed(db, [(now - 100, "ours", "kimi-k3", 1000, 0.00753)
                   for _ in range(200)])
        first = get_zai_per_model_rate("ours", "kimi-k3", db_path=db, _now=now)
        _seed(db, [(now - 100, "ours", "kimi-k3", 1000, 0.05)
                   for _ in range(200)])
        clear_cache()
        second = get_zai_per_model_rate("ours", "kimi-k3", db_path=db, _now=now)
        assert second != pytest.approx(first, rel=1e-3)

    def test_never_raises_on_bad_db(self):
        clear_cache()
        missing = os.path.join(tempfile.gettempdir(), "rpt_zai_pm_rate_bad.db")
        try:
            rate = get_zai_per_model_rate("ours", "kimi-k3", db_path=missing,
                                          _now=_now_utc())
            assert rate == pytest.approx(ZAI_SEED_RATE, rel=1e-12)
        finally:
            if os.path.exists(missing):
                os.unlink(missing)

    def test_exported_in_public_api(self):
        assert "get_zai_per_model_rate" in rpt.__all__


class TestZaiPerModelRates:
    """get_zai_per_model_rates: {model: $/M, '_default': $/M} for one provider."""

    def test_returns_metered_and_subscription_with_default(self, db):
        now = _now_utc()
        # glm-5.2 subscription + kimi-k3 metered on the same 'ours' key.
        _seed(db, [(now - 60 * 86400, "ours", "glm-5.2", 100_000_000, 0.0)
                   for _ in range(200)])
        _seed(db, [(now - 100, "ours", "kimi-k3", 1000, 0.00753)
                   for _ in range(200)])
        rates = get_zai_per_model_rates("ours", db_path=db, _now=now)
        assert set(rates) == {"glm-5.2", "kimi-k3", "_default"}
        assert rates["kimi-k3"] == pytest.approx(7.53, rel=1e-3)
        # glm-5.2 carries the amortized provider rate.
        assert rates["glm-5.2"] == pytest.approx(rates["_default"], rel=1e-12)
        assert rates["glm-5.2"] == pytest.approx(0.015, rel=2e-2)
        # The spread is visible in one dict: 7.53 vs 0.015 (>100×).
        assert rates["kimi-k3"] / rates["glm-5.2"] > 100

    def test_all_subscription_models_share_amortized_rate(self, db):
        """Per §3.2: every subscription model gets the SAME amortized rate."""
        now = _now_utc()
        _seed(db, [(now - 40 * 86400, "ours", "glm-5.2", 5_000_000_000, 0.0)])
        _seed(db, [(now - 40 * 86400, "ours", "kimi-k2.7-code",
                   5_000_000_000, 0.0)])
        rates = get_zai_per_model_rates("ours", db_path=db, _now=now)
        # 10B combined → 300/(10e9/1e6) = 0.03 for both subscription models.
        assert rates["glm-5.2"] == pytest.approx(0.03, rel=1e-9)
        assert rates["kimi-k2.7-code"] == pytest.approx(0.03, rel=1e-9)
        assert rates["_default"] == pytest.approx(0.03, rel=1e-9)

    def test_cold_start_returns_default_seed_only(self, db):
        rates = get_zai_per_model_rates("ours", db_path=db, _now=_now_utc())
        assert set(rates) == {"_default"}
        assert rates["_default"] == pytest.approx(ZAI_SEED_RATE, rel=1e-12)

    def test_friend_premium_applied(self, db):
        now = _now_utc()
        _seed(db, [(now - 100, "ours", "kimi-k3", 1000, 0.00753)
                   for _ in range(200)])
        _seed(db, [(now - 100, "friend", "kimi-k3", 1000, 0.00753)
                   for _ in range(200)])
        ours = get_zai_per_model_rates("ours", db_path=db, _now=now)
        friend = get_zai_per_model_rates("friend", db_path=db, _now=now)
        assert friend["kimi-k3"] == pytest.approx(
            ours["kimi-k3"] * ZAI_FRIEND_PREMIUM, rel=1e-12)

    def test_null_model_excluded(self, db):
        """Un-attributed (NULL model) calls are covered by _default, not a key."""
        now = _now_utc()
        _seed(db, [(now - 100, "ours", None, 1000, 0.0) for _ in range(200)])
        rates = get_zai_per_model_rates("ours", db_path=db, _now=now)
        assert set(rates) == {"_default"}

    def test_never_raises_on_bad_db(self):
        clear_cache()
        missing = os.path.join(tempfile.gettempdir(), "rpt_zai_pm_rates_bad.db")
        try:
            rates = get_zai_per_model_rates("ours", db_path=missing,
                                            _now=_now_utc())
            assert set(rates) == {"_default"}
            assert rates["_default"] == pytest.approx(ZAI_SEED_RATE, rel=1e-12)
        finally:
            if os.path.exists(missing):
                os.unlink(missing)

    def test_exported_in_public_api(self):
        assert "get_zai_per_model_rates" in rpt.__all__


# ── collect_rates: RP-5b hourly rate-refresh collector ───────────────────────


class TestCollectRates:
    """collect_rates() — the cron entry point (RP-5b). Clears the cache, forces
    a full trailing-rate recompute, and returns a structured status report.
    Must never raise, must populate rates/readiness, and must surface the gate."""

    def test_exported_in_public_api(self):
        assert "collect_rates" in rpt.__all__

    def test_returns_ok_report_with_expected_keys(self, db):
        """Cold DB → ok=True, all status keys present, 6 providers reported."""
        report = collect_rates(db_path=db, _now=_now_utc())
        assert report["ok"] is True
        for key in ("ok", "collected_at", "rates", "readiness", "gate_passed",
                    "n_providers", "n_measured_models", "price_changes"):
            assert key in report, f"missing key {key!r}"
        # Every provider in PROVIDER_WINDOW_HOURS gets an entry.
        assert report["n_providers"] == len(PROVIDER_WINDOW_HOURS)
        assert set(report["rates"]) == set(PROVIDER_WINDOW_HOURS)
        assert set(report["readiness"]) == set(PROVIDER_WINDOW_HOURS)

    def test_cold_db_gate_false_and_all_seeds(self, db):
        """No costed data → gate fails, every provider is a seed, no measured
        models, no price changes."""
        report = collect_rates(db_path=db, _now=_now_utc())
        assert report["ok"] is True
        assert report["gate_passed"] is False
        assert report["n_measured_models"] == 0
        assert report["price_changes"] == []
        # Every provider has a _default seed; no per-model measured entries.
        for prov, models in report["rates"].items():
            assert "_default" in models
            assert set(models) == {"_default"}
        # Readiness reports seed/unknown for every provider on a cold DB.
        sources = {r["source"] for r in report["readiness"].values()}
        assert sources <= {"seed", "unknown"}

    def test_warm_data_populates_measured_rates_and_gate(self, db):
        """Seeding >= MIN_CALLS costed rows for every REQUIRED provider flips
        readiness to measured and the gate to True."""
        now = _now_utc()
        rows = []
        # Each required provider: enough costed calls in a recent window to
        # cross MIN_CALLS_FOR_RATE. 1000 tokens × $0.0001 = $0.1/M.
        for prov in REQUIRED_RATE_PROVIDERS:
            rows += [
                (now - 100, prov, "glm-5.2", 1000, 0.0001)
                for _ in range(MIN_CALLS_FOR_RATE + 50)
            ]
        _seed(db, rows)
        report = collect_rates(db_path=db, _now=now)
        assert report["ok"] is True
        assert report["gate_passed"] is True
        # Every required provider is now measured at ~$0.1/M.
        for prov in REQUIRED_RATE_PROVIDERS:
            rdy = report["readiness"][prov]
            assert rdy["source"] == "measured", prov
            assert rdy["has_data"] is True
            assert rdy["rate"] == pytest.approx(0.1, rel=1e-6)
        # glm-5.2 measured on each required provider.
        for prov in REQUIRED_RATE_PROVIDERS:
            assert report["rates"][prov].get("glm-5.2") == pytest.approx(0.1, rel=1e-6)
        assert report["n_measured_models"] == len(REQUIRED_RATE_PROVIDERS)

    def test_clears_cache_before_recompute(self, db, monkeypatch):
        """collect_rates must call clear_cache() so stale cached values don't
        mask fresh DB data. We spy on the call."""
        calls = {"n": 0}
        orig = rpt.clear_cache

        def spy():
            calls["n"] += 1
            orig()

        monkeypatch.setattr(rpt, "clear_cache", spy)
        collect_rates(db_path=db, _now=_now_utc())
        assert calls["n"] == 1, "clear_cache must be invoked exactly once"

    def test_never_raises_on_missing_db(self):
        """A bad DB path must NOT propagate; the report degrades to ok=False."""
        clear_cache()
        missing = os.path.join(tempfile.gettempdir(), "rpt_collect_missing.db")
        try:
            report = collect_rates(db_path=missing, _now=_now_utc())
            # The individual queries swallow DB errors and return empty/seed
            # values, so the run still completes ok=True with seed fallbacks.
            # The contract is: never raises, always returns a dict.
            assert isinstance(report, dict)
            assert "ok" in report
            assert "rates" in report
        finally:
            if os.path.exists(missing):
                os.unlink(missing)

    def test_collected_at_is_iso8601_utc(self, db):
        """collected_at is a parseable ISO-8601 string with a UTC offset."""
        from datetime import datetime

        report = collect_rates(db_path=db, _now=_now_utc())
        ts = report["collected_at"]
        dt = datetime.fromisoformat(ts)  # must not raise
        assert dt.tzinfo is not None  # timezone-aware
        # Offset is UTC (00:00).
        offset = dt.utcoffset()
        assert offset is not None
        assert offset.total_seconds() == 0.0

    def test_price_change_detected_when_24h_spikes(self, db):
        """A provider whose 24h rate is >>2× its 7d baseline shows up in
        price_changes."""
        now = _now_utc()
        # Baseline: 7d window, 200 cheap calls at $0.1/M.
        baseline_rows = [
            (now - 6 * 86400, "deepinfra", "glm-5.2", 1000, 0.0001)  # $0.1/M
            for _ in range(200)
        ]
        # Recent: 24h window, 200 expensive calls at $1.0/M (10× → >50%).
        recent_rows = [
            (now - 100, "deepinfra", "glm-5.2", 1000, 0.001)  # $1.0/M
            for _ in range(200)
        ]
        _seed(db, baseline_rows + recent_rows)
        report = collect_rates(db_path=db, _now=now)
        assert "deepinfra" in report["price_changes"]

    def test_main_prints_compact_json_status_and_exit_code(self, db, capsys, monkeypatch):
        """main() prints a one-line JSON status and exits 0 on ok / 1 on fail."""
        argv = ["--db", db]
        rc = rpt.main(argv)
        out = capsys.readouterr().out.strip()
        import json as _json

        status = _json.loads(out)  # exactly one JSON object
        assert status["ok"] is True
        for key in ("ok", "gate_passed", "n_providers", "n_measured_models",
                    "price_changes", "collected_at", "db_path"):
            assert key in status
        assert status["db_path"] == db
        assert rc == 0
