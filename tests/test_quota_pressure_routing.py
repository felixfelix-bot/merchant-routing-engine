"""Integration tests for RP-PRICING: continuous quota-pressure rerouting.

Validates the live_router wiring of ``quota_pressure_factor`` — Felix's
directive of price-based rerouting (no thresholds, no regime strings).

Scenarios:
  1. Low usage  → pressure=1.0 → ollama_cloud cheapest → chosen
  2. High usage → pressure>1.0 → ollama effective crosses z.ai → friend chosen
  3. Over-quota → ollama very expensive → external (openrouter) chosen
  4. kimi-k2.7-code always routes to ollama regardless of pressure
     (kimi-k3:cloud removed in TELNYX-2.4 — Telnyx serves kimi-k3)
  5. last_quota_pressure property reflects the multiplier
  6. Kill switch OFF → no pressure applied (legacy path, pressure=1.0)
"""
from __future__ import annotations

import math
import os
import sys
import sqlite3
import tempfile

import pytest

# Ensure we can import src modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.live_router import LiveRouter
from src.pricing_engine import quota_pressure_factor, EXTRA_USAGE_MULTIPLIER, OLLAMA_QUOTA_PRESSURE_ASYMPTOTE


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_usage_db() -> str:
    """Create a temp zai_usage.db with api_calls table."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE api_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            key_name TEXT,
            total_tokens INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()
    return path


def _mock_usage_response(session_usage: float, weekly_usage: float = 0.0) -> dict:
    return {
        "limits": {
            "session": {"usage": session_usage},
            "weekly": {"usage": weekly_usage},
        }
    }


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_singleton():
    LiveRouter.reset_instance()
    yield
    LiveRouter.reset_instance()


@pytest.fixture(autouse=True)
def _enable_pressure(monkeypatch):
    """Enable continuous quota-pressure for all tests in this file.

    Disable the legacy binary extra_usage AND throttle paths so the test
    isolates the pressure behaviour.
    """
    import src.live_router as lr
    monkeypatch.setattr(lr, "_QUOTA_PRESSURE_ENABLED", True, raising=True)
    monkeypatch.setattr(lr, "_EXTRA_USAGE_ENABLED", False, raising=True)
    monkeypatch.setattr(lr, "_THROTTLE_ENABLED", False, raising=True)


@pytest.fixture
def rates():
    return {
        "ours":          0.001,
        "friend":        0.028983,
        "ollama_cloud":  0.023952,
        "ppq":           0.14,
        "openrouter":    0.135,
        "deepinfra":     1.30,
    }


@pytest.fixture
def quota_zai_friend_available():
    """ours exhausted, friend available — the reroute comparison scenario."""
    return {
        "ours":         {"used_pct": 100.0, "remaining": 0, "total": 2_000_000},
        "friend":       {"used_pct": 50.0, "remaining": 1_000_000, "total": 2_000_000},
        "ollama_cloud": {"used_pct": 20.0, "remaining": 400_000_000, "total": 500_000_000},
        "ppq":          {"used_pct": 0.0, "remaining": float("inf")},
        "openrouter":   {"used_pct": 0.0, "remaining": float("inf")},
        "deepinfra":    {"used_pct": 0.0, "remaining": float("inf")},
    }


@pytest.fixture
def quota_all_externals():
    """Both z.ai keys exhausted — ollama competes with paid externals only."""
    return {
        "ours":         {"used_pct": 100.0, "remaining": 0, "total": 2_000_000},
        "friend":       {"used_pct": 100.0, "remaining": 0, "total": 2_000_000},
        "ollama_cloud": {"used_pct": 20.0, "remaining": 400_000_000, "total": 500_000_000},
        "ppq":          {"used_pct": 0.0, "remaining": float("inf")},
        "openrouter":   {"used_pct": 0.0, "remaining": float("inf")},
        "deepinfra":    {"used_pct": 0.0, "remaining": float("inf")},
    }


@pytest.fixture
def health_ours_down():
    return {
        "ours": False, "friend": True, "ollama_cloud": True,
        "ppq": True, "openrouter": True, "deepinfra": True,
    }


@pytest.fixture
def all_healthy():
    return {
        "ours": True, "friend": True, "ollama_cloud": True,
        "ppq": True, "openrouter": True, "deepinfra": True,
    }


@pytest.fixture
def both_zai_down():
    return {
        "ours": False, "friend": False, "ollama_cloud": True,
        "ppq": True, "openrouter": True, "deepinfra": True,
    }


# ── Tests ────────────────────────────────────────────────────────────────────


class TestPressureRerouteToZai:
    """GATE: when Ollama's effective price crosses z.ai, the optimizer reroutes."""

    def test_low_usage_chooses_ollama(
        self, rates, quota_zai_friend_available, health_ours_down, monkeypatch,
    ):
        """At 50% usage, pressure=1.0, ollama ($0.024) < friend ($0.029)."""
        db_path = _make_usage_db()
        import src.live_router as lr
        monkeypatch.setattr(
            lr, "fetch_ollama_usage",
            lambda **kw: _mock_usage_response(0.50),
        )
        try:
            router = LiveRouter(db_path=db_path, converged_rates=rates)
            (chosen, _), _ = router.select_failover(
                quota_state=quota_zai_friend_available,
                health_state=health_ours_down,
                peak=False,
                model="glm-5.2",
            )
            assert chosen == "ollama_cloud"
            assert router.last_quota_pressure == pytest.approx(1.0)
        finally:
            os.unlink(db_path)

    def test_high_usage_reroutes_to_friend(
        self, rates, quota_zai_friend_available, health_ours_down, monkeypatch,
    ):
        """GATE: at 90% usage, ollama effective > friend → friend (z.ai) chosen.

        pressure(0.90) ≈ 2.14 → ollama $0.024*2.14 ≈ $0.051 > friend $0.029.
        """
        db_path = _make_usage_db()
        import src.live_router as lr
        monkeypatch.setattr(
            lr, "fetch_ollama_usage",
            lambda **kw: _mock_usage_response(0.90),
        )
        try:
            router = LiveRouter(db_path=db_path, converged_rates=rates)
            (chosen, _), _ = router.select_failover(
                quota_state=quota_zai_friend_available,
                health_state=health_ours_down,
                peak=False,
                model="glm-5.2",
            )
            assert chosen == "friend", (
                f"expected z.ai friend (cheaper at high pressure), got {chosen}"
            )
            assert router.last_quota_pressure > 1.0
        finally:
            os.unlink(db_path)

    def test_pressure_increases_monotonically_with_usage(
        self, rates, quota_zai_friend_available, health_ours_down, monkeypatch,
    ):
        """last_quota_pressure rises smoothly as session_usage increases.

        The RP-EXP rational curve diverges toward +inf as u → 1.0, where it is
        clipped to +inf (provider unreachable). Monotonicity holds across the
        ramp range [onset, 0.99]; at 100% the value is +inf.
        """
        pressures = {}
        db_path = _make_usage_db()
        import src.live_router as lr
        try:
            for u in (0.50, 0.80, 0.85, 0.90, 0.95, 0.99, 1.0):
                monkeypatch.setattr(
                    lr, "fetch_ollama_usage",
                    (lambda _u: (lambda **kw: _mock_usage_response(_u)))(u),
                )
                router = LiveRouter(db_path=db_path, converged_rates=rates)
                router.select_failover(
                    quota_state=quota_zai_friend_available,
                    health_state=health_ours_down,
                    peak=False,
                    model="glm-5.2",
                )
                pressures[u] = router.last_quota_pressure

            # Below onset (0.70): no pressure.
            assert pressures[0.50] == pytest.approx(1.0)
            # Monotonically increasing strictly within the ramp range.
            assert (
                pressures[0.80]
                < pressures[0.85]
                < pressures[0.90]
                < pressures[0.95]
                < pressures[0.99]
            )
            # At 100% usage, pressure caps at the asymptote (hard_limit=False
            # for Ollama — extra usage available). FELIX FINAL DECISION: 1.5.
            assert pressures[1.0] == pytest.approx(OLLAMA_QUOTA_PRESSURE_ASYMPTOTE, abs=0.01)
        finally:
            os.unlink(db_path)


class TestOverQuotaRerouteToExternal:
    """When both z.ai keys are exhausted AND ollama is over-quota, the
    optimizer picks the cheapest paid external (openrouter)."""

    def test_over_quota_reroutes_to_openrouter(
        self, rates, quota_all_externals, both_zai_down, monkeypatch,
    ):
        """GATE: at 110% usage, ollama is +inf → openrouter ($0.135) chosen.

        RP-EXP: pressure(1.10) = +inf (u >= 100% → unreachable). The optimizer
        filters the infinite-priced ollama_cloud and picks the cheapest paid
        external (openrouter).
        """
        db_path = _make_usage_db()
        import src.live_router as lr
        monkeypatch.setattr(
            lr, "fetch_ollama_usage",
            lambda **kw: _mock_usage_response(1.10),
        )
        try:
            router = LiveRouter(db_path=db_path, converged_rates=rates)
            (chosen, _), _ = router.select_failover(
                quota_state=quota_all_externals,
                health_state=both_zai_down,
                peak=False,
                model="glm-5.2",
            )
            assert chosen != "ollama_cloud"
            assert chosen in ("openrouter", "ppq", "deepinfra")
        finally:
            os.unlink(db_path)


class TestKimiAlwaysOllama:
    """GATE: Ollama-exclusive models route to ollama_cloud regardless of pressure."""

    def test_kimi_routes_to_ollama_at_high_usage(
        self, rates, quota_zai_friend_available, health_ours_down, monkeypatch,
    ):
        """kimi-k2.7-code routes to ollama even at 95% usage (no alternative)."""
        db_path = _make_usage_db()
        import src.live_router as lr
        monkeypatch.setattr(
            lr, "fetch_ollama_usage",
            lambda **kw: _mock_usage_response(0.95),
        )
        try:
            router = LiveRouter(db_path=db_path, converged_rates=rates)
            (chosen, chosen_model), _ = router.select_failover(
                quota_state=quota_zai_friend_available,
                health_state=health_ours_down,
                peak=False,
                model="kimi-k2.7-code",
            )
            assert chosen == "ollama_cloud"
            assert chosen_model == "kimi-k2.7-code"
        finally:
            os.unlink(db_path)

    def test_kimi_routes_to_ollama_at_100pct(
        self, rates, quota_zai_friend_available, health_ours_down, monkeypatch,
    ):
        """kimi-k2.7-code routes to ollama even at 100% usage."""
        db_path = _make_usage_db()
        import src.live_router as lr
        monkeypatch.setattr(
            lr, "fetch_ollama_usage",
            lambda **kw: _mock_usage_response(1.0),
        )
        try:
            router = LiveRouter(db_path=db_path, converged_rates=rates)
            (chosen, _), _ = router.select_failover(
                quota_state=quota_zai_friend_available,
                health_state=health_ours_down,
                peak=False,
                model="kimi-k2.7-code",
            )
            assert chosen == "ollama_cloud"
        finally:
            os.unlink(db_path)


class TestKillSwitch:
    """When OLLAMA_QUOTA_PRESSURE_ENABLED is off, pressure stays at 1.0."""

    def test_pressure_off_no_effect(
        self, rates, quota_zai_friend_available, health_ours_down, monkeypatch,
    ):
        """Kill switch OFF → ollama chosen even at 90% usage (no pressure)."""
        import src.live_router as lr
        monkeypatch.setattr(lr, "_QUOTA_PRESSURE_ENABLED", False, raising=True)
        monkeypatch.setattr(
            lr, "fetch_ollama_usage",
            lambda **kw: _mock_usage_response(0.90),
        )
        db_path = _make_usage_db()
        try:
            router = LiveRouter(db_path=db_path, converged_rates=rates)
            (chosen, _), _ = router.select_failover(
                quota_state=quota_zai_friend_available,
                health_state=health_ours_down,
                peak=False,
                model="glm-5.2",
            )
            # No pressure → ollama cheapest → chosen.
            assert chosen == "ollama_cloud"
            assert router.last_quota_pressure == pytest.approx(1.0)
        finally:
            os.unlink(db_path)
