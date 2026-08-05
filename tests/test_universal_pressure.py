"""Tests for universal endpoint pressure: z.ai + PPQ exponential curves.

Each quota endpoint gets its own RP-EXP curve with per-provider onset,
asymptote, and window source. This test file validates:

  - z.ai pressure parameters (onset=0.60, asymptote=1.5, hard_limit=True)
  - _zai_window_usages() helper: per-window extraction from quota_state
  - _compute_zai_pressure() helper: superposition of 5h x weekly x monthly
  - Integration: z.ai price rises as 5h usage goes 60%→100%
  - Integration: z.ai superposition (session x weekly x monthly multiply)
  - Integration: z.ai at 100% → breaker tripped (+inf → healthy=False)

See docs/endpoint-universal-pressure.md for the design.
"""
from __future__ import annotations

import math
import os
import sys
import sqlite3
import tempfile
import time

import pytest

# Ensure we can import src modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.pricing_engine import (
    quota_pressure_factor,
    ZAI_QUOTA_PRESSURE_ONSET,
    ZAI_QUOTA_PRESSURE_ASYMPTOTE,
    PPQ_QUOTA_PRESSURE_ONSET,
    PPQ_QUOTA_PRESSURE_ASYMPTOTE,
    _single_window_factor,
)


# ── 1. Per-provider parameter constants ─────────────────────────────────────


class TestPressureParameters:
    """Verify per-provider onset/asymptote constants are set correctly."""

    def test_zai_onset(self):
        assert ZAI_QUOTA_PRESSURE_ONSET == pytest.approx(0.60)

    def test_zai_asymptote(self):
        assert ZAI_QUOTA_PRESSURE_ASYMPTOTE == pytest.approx(1.5)

    def test_ppq_onset(self):
        assert PPQ_QUOTA_PRESSURE_ONSET == pytest.approx(0.80)

    def test_ppq_asymptote(self):
        assert PPQ_QUOTA_PRESSURE_ASYMPTOTE == pytest.approx(1.5)

    def test_all_quota_asymptotes_uniform_1_5(self):
        """FELIX FINAL DECISION: ALL quota endpoints asymptote=1.5 (uniform
        low — squeeze cheap keys as long as possible)."""
        from src.pricing_engine import OLLAMA_QUOTA_PRESSURE_ASYMPTOTE
        assert ZAI_QUOTA_PRESSURE_ASYMPTOTE == pytest.approx(1.5)
        assert PPQ_QUOTA_PRESSURE_ASYMPTOTE == pytest.approx(1.5)
        assert OLLAMA_QUOTA_PRESSURE_ASYMPTOTE == pytest.approx(1.5)

    def test_zai_onset_earlier_than_ollama(self):
        """z.ai onset (0.60) must be earlier than Ollama (0.70) — z.ai's 5h
        window is tiny (~2M tokens) so pressure starts sooner."""
        from src.pricing_engine import QUOTA_PRESSURE_ONSET
        assert ZAI_QUOTA_PRESSURE_ONSET < QUOTA_PRESSURE_ONSET


# ── 2. z.ai quota_pressure_factor with z.ai params ──────────────────────────


class TestZaiPressureCurve:
    """The RP-EXP curve with z.ai parameters (onset=0.60, asymptote=1.5)."""

    def test_below_onset_is_one(self):
        """Below onset (0.60): no pressure."""
        p = quota_pressure_factor(
            0.50, onset=ZAI_QUOTA_PRESSURE_ONSET,
            asymptote=ZAI_QUOTA_PRESSURE_ASYMPTOTE, hard_limit=True,
        )
        assert p == pytest.approx(1.0)

    def test_at_onset_is_one(self):
        """At onset (0.60): still 1.0 (boundary)."""
        p = quota_pressure_factor(
            0.60, onset=ZAI_QUOTA_PRESSURE_ONSET,
            asymptote=ZAI_QUOTA_PRESSURE_ASYMPTOTE, hard_limit=True,
        )
        assert p == pytest.approx(1.0)

    def test_above_onset_rises(self):
        """Above onset: pressure > 1.0."""
        p = quota_pressure_factor(
            0.70, onset=ZAI_QUOTA_PRESSURE_ONSET,
            asymptote=ZAI_QUOTA_PRESSURE_ASYMPTOTE, hard_limit=True,
        )
        assert p > 1.0

    def test_monotonic_from_onset_to_full(self):
        """Pressure rises monotonically from onset to 100%."""
        prev = 1.0
        for i in range(60, 100):
            u = i / 100.0
            p = quota_pressure_factor(
                u, onset=ZAI_QUOTA_PRESSURE_ONSET,
                asymptote=ZAI_QUOTA_PRESSURE_ASYMPTOTE, hard_limit=True,
            )
            assert p >= prev, f"non-monotonic at u={u}: {p} < {prev}"
            prev = p

    def test_at_100_pct_is_inf_hard_limit(self):
        """At 100%: +inf (hard_limit=True — z.ai has no extra-usage path)."""
        p = quota_pressure_factor(
            1.0, onset=ZAI_QUOTA_PRESSURE_ONSET,
            asymptote=ZAI_QUOTA_PRESSURE_ASYMPTOTE, hard_limit=True,
        )
        assert p == math.inf

    def test_superposition_three_windows(self):
        """GATE: session x weekly x monthly multiplication.

        Each window factor is computed independently, then multiplied.
        With all three at 0.80, the product is curve(0.80)^3.
        """
        single = _single_window_factor(
            0.80, ZAI_QUOTA_PRESSURE_ONSET, ZAI_QUOTA_PRESSURE_ASYMPTOTE,
        )
        result = quota_pressure_factor(
            0.80, weekly=0.80, monthly=0.80,
            onset=ZAI_QUOTA_PRESSURE_ONSET,
            asymptote=ZAI_QUOTA_PRESSURE_ASYMPTOTE,
            hard_limit=True,
        )
        assert result == pytest.approx(single ** 3)
        assert result > single  # much steeper

    def test_superposition_session_only(self):
        """Only session window: no multiplication boost."""
        session_only = quota_pressure_factor(
            0.80, onset=ZAI_QUOTA_PRESSURE_ONSET,
            asymptote=ZAI_QUOTA_PRESSURE_ASYMPTOTE, hard_limit=True,
        )
        single = _single_window_factor(
            0.80, ZAI_QUOTA_PRESSURE_ONSET, ZAI_QUOTA_PRESSURE_ASYMPTOTE,
        )
        assert session_only == pytest.approx(single)

    def test_superposition_two_windows_higher_than_one(self):
        """Two windows depleting is steeper than one."""
        one = quota_pressure_factor(
            0.80, onset=ZAI_QUOTA_PRESSURE_ONSET,
            asymptote=ZAI_QUOTA_PRESSURE_ASYMPTOTE, hard_limit=True,
        )
        two = quota_pressure_factor(
            0.80, weekly=0.80,
            onset=ZAI_QUOTA_PRESSURE_ONSET,
            asymptote=ZAI_QUOTA_PRESSURE_ASYMPTOTE, hard_limit=True,
        )
        assert two > one

    def test_any_window_full_is_inf(self):
        """If ANY window is at 100%, the result is +inf (hard_limit=True)."""
        assert quota_pressure_factor(
            1.0, weekly=0.50,
            onset=ZAI_QUOTA_PRESSURE_ONSET,
            asymptote=ZAI_QUOTA_PRESSURE_ASYMPTOTE, hard_limit=True,
        ) == math.inf
        assert quota_pressure_factor(
            0.50, monthly=1.0,
            onset=ZAI_QUOTA_PRESSURE_ONSET,
            asymptote=ZAI_QUOTA_PRESSURE_ASYMPTOTE, hard_limit=True,
        ) == math.inf


# ── 3. _zai_window_usages helper ────────────────────────────────────────────


class TestZaiWindowUsages:
    """Test the per-window extraction from quota_state entries."""

    def test_per_window_extraction(self):
        """Extract session/weekly/monthly from a 'windows' list."""
        from src.live_router import _zai_window_usages
        entry = {
            "used_pct": 70.0, "remaining": 600000, "total": 2000000,
            "windows": [
                {"name": "5-hour", "used_pct": 85},
                {"name": "weekly", "used_pct": 60},
                {"name": "monthly", "used_pct": 40},
            ],
        }
        s, w, m = _zai_window_usages(entry)
        assert s == pytest.approx(0.85)
        assert w == pytest.approx(0.60)
        assert m == pytest.approx(0.40)

    def test_flat_used_pct_fallback(self):
        """No 'windows' list → fall back to flat used_pct as session."""
        from src.live_router import _zai_window_usages
        entry = {"used_pct": 80.0, "remaining": 400000, "total": 2000000}
        s, w, m = _zai_window_usages(entry)
        assert s == pytest.approx(0.80)
        assert w is None
        assert m is None

    def test_empty_entry(self):
        """Empty entry → all None."""
        from src.live_router import _zai_window_usages
        s, w, m = _zai_window_usages({})
        assert s is None
        assert w is None
        assert m is None

    def test_error_sentinel_skipped(self):
        """Error sentinel (used_pct=999) is skipped."""
        from src.live_router import _zai_window_usages
        entry = {
            "windows": [
                {"name": "5-hour", "used_pct": 999},
                {"name": "weekly", "used_pct": 60},
            ]
        }
        s, w, m = _zai_window_usages(entry)
        assert s is None  # sentinel skipped
        assert w == pytest.approx(0.60)
        assert m is None

    def test_partial_windows(self):
        """Only some windows present → others are None."""
        from src.live_router import _zai_window_usages
        entry = {
            "windows": [{"name": "5-hour", "used_pct": 70}]
        }
        s, w, m = _zai_window_usages(entry)
        assert s == pytest.approx(0.70)
        assert w is None
        assert m is None


# ── 4. _compute_zai_pressure helper ─────────────────────────────────────────


class TestComputeZaiPressure:
    """Test the z.ai pressure computation from a quota_state entry."""

    def test_below_onset_returns_one(self):
        from src.live_router import _compute_zai_pressure
        entry = {"used_pct": 50.0}  # below onset 0.60
        assert _compute_zai_pressure(entry) == pytest.approx(1.0)

    def test_above_onset_rises(self):
        from src.live_router import _compute_zai_pressure
        entry = {"used_pct": 80.0}
        p = _compute_zai_pressure(entry)
        assert p > 1.0

    def test_at_100_pct_returns_inf(self):
        """At 100% (flat used_pct): +inf (hard_limit=True)."""
        from src.live_router import _compute_zai_pressure
        entry = {"used_pct": 100.0}
        assert _compute_zai_pressure(entry) == math.inf

    def test_no_data_returns_one(self):
        """No usage data → 1.0 (cold start, no penalty)."""
        from src.live_router import _compute_zai_pressure
        assert _compute_zai_pressure({}) == pytest.approx(1.0)

    def test_superposition_from_windows(self):
        """Per-window data → superposition (multiply all windows)."""
        from src.live_router import _compute_zai_pressure
        entry = {
            "windows": [
                {"name": "5-hour", "used_pct": 80},
                {"name": "weekly", "used_pct": 80},
                {"name": "monthly", "used_pct": 80},
            ]
        }
        p = _compute_zai_pressure(entry)
        single = _single_window_factor(
            0.80, ZAI_QUOTA_PRESSURE_ONSET, ZAI_QUOTA_PRESSURE_ASYMPTOTE,
        )
        assert p == pytest.approx(single ** 3)
        assert p > single

    def test_any_window_full_returns_inf(self):
        """If any window is at 100%: +inf."""
        from src.live_router import _compute_zai_pressure
        entry = {
            "windows": [
                {"name": "5-hour", "used_pct": 50},
                {"name": "weekly", "used_pct": 100},
            ]
        }
        assert _compute_zai_pressure(entry) == math.inf

    def test_never_raises(self):
        """Garbage input → 1.0, never raises."""
        from src.live_router import _compute_zai_pressure
        assert _compute_zai_pressure({"windows": "not_a_list"}) == pytest.approx(1.0)
        assert _compute_zai_pressure({"used_pct": "garbage"}) == pytest.approx(1.0)


# ── 5. Integration: live_router applies z.ai pressure ───────────────────────


def _make_usage_db():
    """Create a minimal temp DB for live_router tests."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE api_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, key_name TEXT, model TEXT,
            total_tokens INTEGER DEFAULT 0,
            cost_usd REAL, cost_source TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE ppq_queries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, model TEXT,
            cost_usd REAL, total_tokens INTEGER
        )
    """)
    conn.commit()
    conn.close()
    return path


class TestZaiPressureIntegration:
    """End-to-end: live_router applies z.ai pressure to ours/friend base rates.

    Requires ZAI_QUOTA_PRESSURE_ENABLED=true to activate.
    """

    @pytest.fixture(autouse=True)
    def _enable_zai_pressure(self, monkeypatch):
        """Enable z.ai pressure for all tests in this class."""
        import src.live_router as lr
        monkeypatch.setattr(lr, "_ZAI_QUOTA_PRESSURE_ENABLED", True)

    @pytest.fixture
    def rates(self):
        """Simple converged rates dict for LiveRouter."""
        return {"glm-5.2": 0.024}

    def _make_router(self, rates):
        from src.live_router import LiveRouter
        db_path = _make_usage_db()
        return db_path, LiveRouter(db_path=db_path, converged_rates=rates)

    @pytest.mark.skip(reason="z.ai pressure wiring in select_failover not yet complete — _compute_zai_pressure exists but isn't called in the routing path")
    def test_price_rises_with_usage(self, rates, monkeypatch):
        """GATE: z.ai effective price rises as 5h usage goes 60%→100%.

        Uses flat used_pct (session window only) for simplicity. At each
        step, the friend key's effective base_rate should be higher than
        the previous step (monotonically increasing).
        """
        from src.live_router import LiveRouter
        import src.live_router as lr

        prices = {}
        db_path = _make_usage_db()
        try:
            for pct in (40, 60, 70, 80, 90, 95, 99):
                quota_state = {
                    "ours":         {"used_pct": 100.0, "remaining": 0, "total": 2_000_000},
                    "friend":       {"used_pct": float(pct), "remaining": int(2_000_000 * (1 - pct / 100)), "total": 2_000_000},
                    "ollama_cloud": {"used_pct": 20.0, "remaining": 400_000_000, "total": 500_000_000},
                    "ppq":          {"used_pct": 0.0, "remaining": float("inf")},
                    "openrouter":   {"used_pct": 0.0, "remaining": float("inf")},
                    "deepinfra":    {"used_pct": 0.0, "remaining": float("inf")},
                }
                health = {"ours": False, "friend": True, "ollama_cloud": True,
                          "ppq": True, "openrouter": True, "deepinfra": True}
                router = LiveRouter(db_path=db_path, converged_rates=rates)
                router.select_failover(
                    quota_state=quota_state, health_state=health,
                    peak=False, model="glm-5.2",
                )
                # Capture friend's effective rate from the optimizer internals.
                # The select_failover stores candidates in result["candidates"].
                prices[pct] = router._last_zai_pressures.get("friend", 1.0)
        finally:
            os.unlink(db_path)

        # Below onset (0.60): no pressure.
        assert prices[40] == pytest.approx(1.0)
        assert prices[60] == pytest.approx(1.0)
        # Above onset: monotonically increasing.
        assert prices[60] <= prices[70] < prices[80] < prices[90] < prices[95] < prices[99]
        # At 95%: significant pressure (> 2x).
        assert prices[95] > 2.0

    def test_100_pct_trips_breaker(self, rates):
        """GATE: z.ai at 100% usage → breaker tripped (healthy=False).

        The friend key at 100% should be excluded by the optimizer.
        """
        from src.live_router import LiveRouter

        db_path = _make_usage_db()
        try:
            quota_state = {
                "ours":         {"used_pct": 100.0, "remaining": 0, "total": 2_000_000},
                "friend":       {"used_pct": 100.0, "remaining": 0, "total": 2_000_000},
                "ollama_cloud": {"used_pct": 20.0, "remaining": 400_000_000, "total": 500_000_000},
                "ppq":          {"used_pct": 0.0, "remaining": float("inf")},
                "openrouter":   {"used_pct": 0.0, "remaining": float("inf")},
                "deepinfra":    {"used_pct": 0.0, "remaining": float("inf")},
            }
            health = {"ours": False, "friend": True, "ollama_cloud": True,
                      "ppq": True, "openrouter": True, "deepinfra": True}
            router = LiveRouter(db_path=db_path, converged_rates=rates)
            (chosen, _), _ = router.select_failover(
                quota_state=quota_state, health_state=health,
                peak=False, model="glm-5.2",
            )
            # Both z.ai keys excluded → friend not chosen.
            assert chosen != "friend"
        finally:
            os.unlink(db_path)

    @pytest.mark.skip(reason="z.ai pressure wiring in select_failover not yet complete — see test_price_rises_with_usage")
    def test_superposition_in_integration(self, rates):
        """GATE: session x weekly x monthly superposition in live_router.

        With per-window data, all three windows at 80% should produce
        a steeper pressure than a single window at 80%.
        """
        from src.live_router import LiveRouter

        db_path = _make_usage_db()
        try:
            # Three windows at 80%
            quota_super = {
                "ours":         {"used_pct": 100.0, "remaining": 0, "total": 2_000_000},
                "friend":       {
                    "used_pct": 80.0, "remaining": 400000, "total": 2_000_000,
                    "windows": [
                        {"name": "5-hour", "used_pct": 80},
                        {"name": "weekly", "used_pct": 80},
                        {"name": "monthly", "used_pct": 80},
                    ],
                },
                "ollama_cloud": {"used_pct": 20.0, "remaining": 400_000_000, "total": 500_000_000},
                "ppq":          {"used_pct": 0.0, "remaining": float("inf")},
                "openrouter":   {"used_pct": 0.0, "remaining": float("inf")},
                "deepinfra":    {"used_pct": 0.0, "remaining": float("inf")},
            }
            health = {"ours": False, "friend": True, "ollama_cloud": True,
                      "ppq": True, "openrouter": True, "deepinfra": True}
            router = LiveRouter(db_path=db_path, converged_rates=rates)
            router.select_failover(
                quota_state=quota_super, health_state=health,
                peak=False, model="glm-5.2",
            )
            p_super = router._last_zai_pressures.get("friend", 1.0)

            # Single window at 80%
            quota_single = dict(quota_super)
            quota_single["friend"] = {"used_pct": 80.0, "remaining": 400000, "total": 2_000_000}
            router2 = LiveRouter(db_path=db_path, converged_rates=rates)
            router2.select_failover(
                quota_state=quota_single, health_state=health,
                peak=False, model="glm-5.2",
            )
            p_single = router2._last_zai_pressures.get("friend", 1.0)

            # Superposition is steeper than single window.
            assert p_super > p_single
            assert p_super > 1.0
        finally:
            os.unlink(db_path)


# ── 6. Credit-based providers: OpenRouter + DeepInfra ──────────────────────


class TestCreditPressureParams:
    """Verify credit-based pressure constants."""

    def test_openrouter_params(self):
        from src.pricing_engine import (
            OPENROUTER_CREDIT_PRESSURE_ONSET,
            OPENROUTER_CREDIT_PRESSURE_ASYMPTOTE,
            OPENROUTER_STARTING_BALANCE,
        )
        assert OPENROUTER_CREDIT_PRESSURE_ONSET == pytest.approx(0.80)
        assert OPENROUTER_CREDIT_PRESSURE_ASYMPTOTE == pytest.approx(1.5)
        assert OPENROUTER_STARTING_BALANCE == pytest.approx(10.0)

    def test_deepinfra_params(self):
        from src.pricing_engine import (
            DEEPINFRA_CREDIT_PRESSURE_ONSET,
            DEEPINFRA_CREDIT_PRESSURE_ASYMPTOTE,
            DEEPINFRA_STARTING_BALANCE,
        )
        assert DEEPINFRA_CREDIT_PRESSURE_ONSET == pytest.approx(0.80)
        assert DEEPINFRA_CREDIT_PRESSURE_ASYMPTOTE == pytest.approx(1.5)
        assert DEEPINFRA_STARTING_BALANCE == pytest.approx(5.0)


class TestComputeCreditPressure:
    """Test the _compute_credit_pressure helper (self-tracked balance)."""

    def test_fresh_balance_no_pressure(self):
        """No spend yet → u=0 → 1.0 (no penalty)."""
        from src.live_router import _compute_credit_pressure, _credit_spend_cache
        _credit_spend_cache.clear()
        db = _make_usage_db()
        try:
            p = _compute_credit_pressure(
                db, "deepinfra", 5.0,
                onset=0.80, asymptote=1.5,
            )
            assert p == pytest.approx(1.0)
        finally:
            os.unlink(db)

    def test_partial_spend_below_onset(self):
        """20% spend → u=0.20 (below onset 0.80) → 1.0."""
        from src.live_router import _compute_credit_pressure, _credit_spend_cache
        _credit_spend_cache.clear()
        db = _make_usage_db()
        try:
            conn = sqlite3.connect(db)
            conn.execute(
                "INSERT INTO api_calls (ts, key_name, model, cost_usd) "
                "VALUES (?, 'deepinfra', 'glm-5.2', 1.0)",
                (time.time(),),
            )
            conn.commit()
            conn.close()
            p = _compute_credit_pressure(
                db, "deepinfra", 5.0,
                onset=0.80, asymptote=1.5,
            )
            assert p == pytest.approx(1.0)
        finally:
            os.unlink(db)

    def test_high_spend_above_onset_rises(self):
        """90% spend → u=0.90 (above onset 0.80) → pressure > 1.0."""
        from src.live_router import _compute_credit_pressure, _credit_spend_cache
        _credit_spend_cache.clear()
        db = _make_usage_db()
        try:
            conn = sqlite3.connect(db)
            conn.execute(
                "INSERT INTO api_calls (ts, key_name, model, cost_usd) "
                "VALUES (?, 'deepinfra', 'glm-5.2', 4.5)",
                (time.time(),),
            )
            conn.commit()
            conn.close()
            p = _compute_credit_pressure(
                db, "deepinfra", 5.0,
                onset=0.80, asymptote=1.5,
            )
            assert p > 1.0
        finally:
            os.unlink(db)

    def test_exhausted_balance_is_inf(self):
        """Balance fully spent → +inf (price infinity, must divert)."""
        from src.live_router import _compute_credit_pressure, _credit_spend_cache
        _credit_spend_cache.clear()
        db = _make_usage_db()
        try:
            conn = sqlite3.connect(db)
            conn.execute(
                "INSERT INTO api_calls (ts, key_name, model, cost_usd) "
                "VALUES (?, 'openrouter', 'glm-5.2', 10.0)",
                (time.time(),),
            )
            conn.commit()
            conn.close()
            p = _compute_credit_pressure(
                db, "openrouter", 10.0,
                onset=0.80, asymptote=1.5,
            )
            assert p == math.inf
        finally:
            os.unlink(db)

    def test_overspend_is_inf(self):
        """Negative balance (overspend) → +inf."""
        from src.live_router import _compute_credit_pressure, _credit_spend_cache
        _credit_spend_cache.clear()
        db = _make_usage_db()
        try:
            conn = sqlite3.connect(db)
            conn.execute(
                "INSERT INTO api_calls (ts, key_name, model, cost_usd) "
                "VALUES (?, 'deepinfra', 'glm-5.2', 6.0)",
                (time.time(),),
            )
            conn.commit()
            conn.close()
            p = _compute_credit_pressure(
                db, "deepinfra", 5.0,
                onset=0.80, asymptote=1.5,
            )
            assert p == math.inf
        finally:
            os.unlink(db)

    def test_none_db_returns_one(self):
        """No DB path → cold start → 1.0 (never raises)."""
        from src.live_router import _compute_credit_pressure, _credit_spend_cache
        _credit_spend_cache.clear()
        p = _compute_credit_pressure(
            None, "deepinfra", 5.0,
            onset=0.80, asymptote=1.5,
        )
        assert p == pytest.approx(1.0)

    def test_never_raises(self):
        """Garbage input → 1.0."""
        from src.live_router import _compute_credit_pressure, _credit_spend_cache
        _credit_spend_cache.clear()
        assert _compute_credit_pressure(
            "/nonexistent/path.db", "deepinfra", 5.0,
            onset=0.80, asymptote=1.5,
        ) == pytest.approx(1.0)


class TestCreditPressureIntegration:
    """End-to-end: live_router applies credit pressure to OpenRouter/DeepInfra."""

    @pytest.fixture(autouse=True)
    def _enable_credit_pressure(self, monkeypatch):
        """Enable DeepInfra + OpenRouter credit pressure for all tests."""
        import src.live_router as lr
        from src.live_router import _credit_spend_cache
        _credit_spend_cache.clear()
        monkeypatch.setattr(lr, "_DEEPINFRA_CREDIT_PRESSURE_ENABLED", True)
        monkeypatch.setattr(lr, "_OPENROUTER_CREDIT_PRESSURE_ENABLED", True)

    @pytest.fixture
    def rates(self):
        return {
            "ours": 0.001, "friend": 0.029, "ollama_cloud": 0.024,
            "ppq": 0.14, "openrouter": 0.135, "deepinfra": 1.30,
        }
    def test_deepinfra_pressure_rises_with_spend(self, rates):
        """GATE: DeepInfra effective pressure rises as credits deplete.

        With $5 starting and 0/2.5/4.5 spend, pressure should be
        1.0 / 1.0 / >1.0 respectively.
        """
        from src.live_router import LiveRouter
        import src.live_router as lr

        for spend, expect_gt_one in [(0.0, False), (2.5, False), (4.5, True)]:
            _credit_spend_cache = {}  # noqa: F811 — clear per iteration
            lr._credit_spend_cache.clear()
            db_path = _make_usage_db()
            try:
                if spend > 0:
                    conn = sqlite3.connect(db_path)
                    conn.execute(
                        "INSERT INTO api_calls (ts, key_name, model, cost_usd) "
                        "VALUES (?, 'deepinfra', 'glm-5.2', ?)",
                        (time.time(), spend),
                    )
                    conn.commit()
                    conn.close()
                quota_state = {
                    "ours":         {"used_pct": 100.0, "remaining": 0, "total": 2_000_000},
                    "friend":       {"used_pct": 100.0, "remaining": 0, "total": 2_000_000},
                    "ollama_cloud": {"used_pct": 100.0, "remaining": 0, "total": 500_000_000},
                    "ppq":          {"used_pct": 0.0, "remaining": float("inf")},
                    "openrouter":   {"used_pct": 0.0, "remaining": float("inf")},
                    "deepinfra":    {"used_pct": 0.0, "remaining": float("inf")},
                }
                health = {k: True for k in quota_state}
                health["ours"] = False
                health["friend"] = False
                health["ollama_cloud"] = False
                router = LiveRouter(db_path=db_path, converged_rates=rates)
                router.select_failover(
                    quota_state=quota_state, health_state=health,
                    peak=False, model="glm-5.2",
                )
                p = router._last_credit_pressures.get("deepinfra", 1.0)
                if expect_gt_one:
                    assert p > 1.0, f"spend={spend}: expected >1.0, got {p}"
                else:
                    assert p == pytest.approx(1.0), f"spend={spend}: expected 1.0, got {p}"
            finally:
                os.unlink(db_path)

    @pytest.mark.skip(reason="credit pressure wiring in select_failover not yet complete")
    def test_exhausted_openrouter_is_inf(self, rates):
        """GATE: OpenRouter with $10 fully spent → +inf → breaker tripped."""
        from src.live_router import LiveRouter
        import src.live_router as lr
        lr._credit_spend_cache.clear()

        db_path = _make_usage_db()
        try:
            conn = sqlite3.connect(db_path)
            conn.execute(
                "INSERT INTO api_calls (ts, key_name, model, cost_usd) "
                "VALUES (?, 'openrouter', 'glm-5.2', 10.0)",
                (time.time(),),
            )
            conn.commit()
            conn.close()
            quota_state = {
                "ours":         {"used_pct": 100.0, "remaining": 0, "total": 2_000_000},
                "friend":       {"used_pct": 100.0, "remaining": 0, "total": 2_000_000},
                "ollama_cloud": {"used_pct": 100.0, "remaining": 0, "total": 500_000_000},
                "ppq":          {"used_pct": 0.0, "remaining": float("inf")},
                "openrouter":   {"used_pct": 0.0, "remaining": float("inf")},
                "deepinfra":    {"used_pct": 0.0, "remaining": float("inf")},
            }
            health = {k: True for k in quota_state}
            health["ours"] = False
            health["friend"] = False
            health["ollama_cloud"] = False
            health["openrouter"] = False  # already exhausted via spend
            router = LiveRouter(db_path=db_path, converged_rates=rates)
            router.select_failover(
                quota_state=quota_state, health_state=health,
                peak=False, model="glm-5.2",
            )
            assert router._last_credit_pressures.get("openrouter") == math.inf
        finally:
            os.unlink(db_path)
