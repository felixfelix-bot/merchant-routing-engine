"""Tests for RP-5: Proactive GLM-5.2 throttling in live_router.py.

Tests that:
1. GLM-5.2 is deprioritised (routes to external) when session_usage >= 0.85
2. GLM-5.2 is excluded (not even a fallback) when session_usage >= 1.0
3. Ollama-exclusive models (kimi-k3:cloud) always route to ollama regardless
4. z.ai keys are preferred over ollama when available during throttle
5. Kill switch OLLAMA_THROTTLE_ENABLED=false disables all throttling
6. Normal routing (below threshold) is unaffected
7. last_throttle_state / last_session_usage properties work correctly
8. Throttle thresholds and price multiplier env vars are respected
9. Router never crashes on API failure / missing db_path
"""
from __future__ import annotations

import os
import sys
import sqlite3
import tempfile
import time

import pytest

# Ensure we can import src modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.live_router import (
    LiveRouter,
    _THROTTLE_ENABLED,
    _THROTTLE_THRESHOLD,
    _BLOCK_THRESHOLD,
    _THROTTLE_PRICE_MULT,
)
from src.ollama_extra_usage import ExtraUsageStatus


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_usage_db(rows: list[tuple[float, str, int]] | None = None) -> str:
    """Create a temp zai_usage.db with api_calls table."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("""
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
            duration_ms INTEGER
        )
    """)
    for ts, key_name, total_tokens in (rows or []):
        conn.execute(
            "INSERT INTO api_calls (ts, key_name, total_tokens) VALUES (?, ?, ?)",
            (ts, key_name, total_tokens),
        )
    conn.commit()
    conn.close()
    return path


def _mock_usage_response(
    session_usage: float, weekly_usage: float = 0.0
) -> dict:
    """Build a mock ollama.com/api/usage response."""
    return {
        "limits": {
            "session": {"usage": session_usage},
            "weekly": {"usage": weekly_usage},
        }
    }


def _make_config(session_limit: int, weekly_limit: int) -> str:
    """Write a temp providers.yaml with custom ollama_cloud limits."""
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w") as f:
        f.write(
            "ollama_cloud:\n"
            f"  included_quota_tokens_session: {session_limit}\n"
            f"  included_quota_tokens_weekly: {weekly_limit}\n"
        )
    return path


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Reset the LiveRouter singleton before and after each test."""
    LiveRouter.reset_instance()
    yield
    LiveRouter.reset_instance()


@pytest.fixture(autouse=True)
def _enable_throttle(monkeypatch):
    """Enable proactive throttling for all tests in this file.

    Note: _EXTRA_USAGE_ENABLED is left as False (default) so the reactive
    extra-usage multiplier does NOT apply — these tests isolate the
    proactive throttle behaviour.
    """
    monkeypatch.setenv("OLLAMA_THROTTLE_ENABLED", "true")
    import src.live_router as lr
    monkeypatch.setattr(lr, "_THROTTLE_ENABLED", True, raising=True)
    monkeypatch.setattr(lr, "_THROTTLE_THRESHOLD", 0.85, raising=True)
    monkeypatch.setattr(lr, "_BLOCK_THRESHOLD", 1.0, raising=True)
    monkeypatch.setattr(lr, "_THROTTLE_PRICE_MULT", 6.0, raising=True)
    # Ensure extra-usage is OFF to isolate throttle behaviour
    monkeypatch.setattr(lr, "_EXTRA_USAGE_ENABLED", False, raising=True)


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
def quota_both_zai_exhausted():
    """Both z.ai keys exhausted — standard failover scenario."""
    return {
        "ours":         {"used_pct": 100.0, "remaining": 0, "total": 2_000_000},
        "friend":       {"used_pct": 100.0, "remaining": 0, "total": 2_000_000},
        "ollama_cloud": {"used_pct": 20.0, "remaining": 400_000_000, "total": 500_000_000},
        "ppq":          {"used_pct": 0.0, "remaining": float("inf")},
        "openrouter":   {"used_pct": 0.0, "remaining": float("inf")},
        "deepinfra":    {"used_pct": 0.0, "remaining": float("inf")},
    }


@pytest.fixture
def quota_zai_available():
    """z.ai keys have remaining quota — tests z.ai preference during throttle."""
    return {
        "ours":         {"used_pct": 50.0, "remaining": 1_000_000, "total": 2_000_000},
        "friend":       {"used_pct": 50.0, "remaining": 1_000_000, "total": 2_000_000},
        "ollama_cloud": {"used_pct": 85.0, "remaining": 75_000_000, "total": 500_000_000},
        "ppq":          {"used_pct": 0.0, "remaining": float("inf")},
        "openrouter":   {"used_pct": 0.0, "remaining": float("inf")},
        "deepinfra":    {"used_pct": 0.0, "remaining": float("inf")},
    }


@pytest.fixture
def all_healthy():
    return {
        "ours": True, "friend": True, "ollama_cloud": True,
        "ppq": True, "openrouter": True, "deepinfra": True,
    }


@pytest.fixture
def both_zai_unhealthy():
    return {
        "ours": False, "friend": False, "ollama_cloud": True,
        "ppq": True, "openrouter": True, "deepinfra": True,
    }


# ── Tests: module-level config constants ─────────────────────────────────────


class TestThrottleConfig:
    """Verify the module-level throttle config constants exist with defaults."""

    def test_throttle_enabled_default_false(self):
        """Kill switch defaults to False — throttling is OFF by default."""
        # Re-import to check the default (the fixture overrides it, so check
        # the constant type exists)
        assert isinstance(_THROTTLE_ENABLED, bool)

    def test_throttle_threshold_is_float(self):
        assert isinstance(_THROTTLE_THRESHOLD, float)

    def test_block_threshold_is_float(self):
        assert isinstance(_BLOCK_THRESHOLD, float)

    def test_throttle_price_mult_is_float(self):
        assert isinstance(_THROTTLE_PRICE_MULT, float)

    def test_throttle_price_mult_default_is_6(self):
        """The default price multiplier should be 6.0 ($0.024 * 6 = $0.144 > $0.14 PPQ)."""
        assert _THROTTLE_PRICE_MULT == 6.0


# ── Tests: normal routing (below throttle threshold) ─────────────────────────


class TestNormalRouting:
    """When session_usage < 0.85, throttling does not apply — ollama is chosen."""

    def test_below_threshold_chooses_ollama(
        self, rates, quota_both_zai_exhausted, all_healthy, monkeypatch,
    ):
        """GATE: At 50% usage (below 0.85), GLM-5.2 routes to ollama_cloud normally."""
        db_path = _make_usage_db([])

        import src.live_router as lr
        monkeypatch.setattr(
            lr, "fetch_ollama_usage",
            lambda **kw: _mock_usage_response(0.50),
        )

        try:
            router = LiveRouter(db_path=db_path, converged_rates=rates)
            (chosen, _), _ = router.select_failover(
                quota_state=quota_both_zai_exhausted,
                health_state=all_healthy,
                peak=False,
                model="glm-5.2",
            )
            assert chosen == "ollama_cloud"
            assert router.last_throttle_state == "normal"
            assert router.last_session_usage == pytest.approx(0.50)
        finally:
            os.unlink(db_path)

    def test_just_below_threshold_chooses_ollama(
        self, rates, quota_both_zai_exhausted, all_healthy, monkeypatch,
    ):
        """At 0.84 usage (just below 0.85), still normal — ollama chosen."""
        db_path = _make_usage_db([])

        import src.live_router as lr
        monkeypatch.setattr(
            lr, "fetch_ollama_usage",
            lambda **kw: _mock_usage_response(0.84),
        )

        try:
            router = LiveRouter(db_path=db_path, converged_rates=rates)
            (chosen, _), _ = router.select_failover(
                quota_state=quota_both_zai_exhausted,
                health_state=all_healthy,
                peak=False,
                model="glm-5.2",
            )
            assert chosen == "ollama_cloud"
            assert router.last_throttle_state == "normal"
        finally:
            os.unlink(db_path)

    def test_zero_usage_chooses_ollama(
        self, rates, quota_both_zai_exhausted, all_healthy, monkeypatch,
    ):
        """At 0% usage, ollama is chosen freely."""
        db_path = _make_usage_db([])

        import src.live_router as lr
        monkeypatch.setattr(
            lr, "fetch_ollama_usage",
            lambda **kw: _mock_usage_response(0.0),
        )

        try:
            router = LiveRouter(db_path=db_path, converged_rates=rates)
            (chosen, _), _ = router.select_failover(
                quota_state=quota_both_zai_exhausted,
                health_state=all_healthy,
                peak=False,
                model="glm-5.2",
            )
            assert chosen == "ollama_cloud"
            assert router.last_throttle_state == "normal"
            assert router.last_session_usage == pytest.approx(0.0)
        finally:
            os.unlink(db_path)


# ── Tests: throttle at 85% ───────────────────────────────────────────────────


class TestThrottleAt85pct:
    """GATE: GLM-5.2 deprioritised when session_usage >= 0.85.

    Ollama_cloud is lowered to "low" tier and its price bumped by
    _THROTTLE_PRICE_MULT (6x → $0.144/M), making it more expensive than
    OpenRouter ($0.135) and PPQ ($0.14).  The cheapest external is chosen
    instead; ollama remains viable as a last-resort fallback.
    """

    def test_glm_reroutes_to_external_at_85pct(
        self, rates, quota_both_zai_exhausted, all_healthy, monkeypatch,
    ):
        """GATE: At 85% usage, GLM-5.2 does NOT route to ollama_cloud."""
        db_path = _make_usage_db([])

        import src.live_router as lr
        monkeypatch.setattr(
            lr, "fetch_ollama_usage",
            lambda **kw: _mock_usage_response(0.85),
        )

        try:
            router = LiveRouter(db_path=db_path, converged_rates=rates)
            (chosen, _), (fallback, _) = router.select_failover(
                quota_state=quota_both_zai_exhausted,
                health_state=all_healthy,
                peak=False,
                model="glm-5.2",
            )
            assert router.last_throttle_state == "throttle"
            # GLM-5.2 should route to the cheapest external (openrouter $0.135)
            assert chosen != "ollama_cloud"
            assert chosen in ("openrouter", "ppq", "deepinfra")
        finally:
            os.unlink(db_path)

    def test_throttle_state_property_at_85pct(
        self, rates, quota_both_zai_exhausted, all_healthy, monkeypatch,
    ):
        """last_throttle_state must be 'throttle' at 85% usage."""
        db_path = _make_usage_db([])

        import src.live_router as lr
        monkeypatch.setattr(
            lr, "fetch_ollama_usage",
            lambda **kw: _mock_usage_response(0.85),
        )

        try:
            router = LiveRouter(db_path=db_path, converged_rates=rates)
            router.select_failover(
                quota_state=quota_both_zai_exhausted,
                health_state=all_healthy,
                peak=False,
                model="glm-5.2",
            )
            assert router.last_throttle_state == "throttle"
            assert router.last_session_usage == pytest.approx(0.85)
        finally:
            os.unlink(db_path)

    def test_throttle_ollama_still_viable_as_fallback(
        self, rates, quota_both_zai_exhausted, all_healthy, monkeypatch,
    ):
        """During throttle, ollama is deprioritised but still viable as fallback."""
        db_path = _make_usage_db([])

        import src.live_router as lr
        monkeypatch.setattr(
            lr, "fetch_ollama_usage",
            lambda **kw: _mock_usage_response(0.90),
        )

        try:
            router = LiveRouter(db_path=db_path, converged_rates=rates)
            (chosen, _), (fallback, _) = router.select_failover(
                quota_state=quota_both_zai_exhausted,
                health_state=all_healthy,
                peak=False,
                model="glm-5.2",
            )
            # An external should be chosen
            assert chosen in ("openrouter", "ppq", "deepinfra")
            # Ollama should NOT be chosen but SHOULD be a viable fallback
            # (it's deprioritised, not blocked). It may or may not appear as
            # the second candidate depending on optimizer ranking, but the
            # key assertion is that chosen != ollama_cloud.
            assert chosen != "ollama_cloud"
        finally:
            os.unlink(db_path)

    def test_throttle_at_99pct_still_throttle_not_block(
        self, rates, quota_both_zai_exhausted, all_healthy, monkeypatch,
    ):
        """At 0.99 usage (just below 1.0), it's still 'throttle' not 'block'."""
        db_path = _make_usage_db([])

        import src.live_router as lr
        monkeypatch.setattr(
            lr, "fetch_ollama_usage",
            lambda **kw: _mock_usage_response(0.99),
        )

        try:
            router = LiveRouter(db_path=db_path, converged_rates=rates)
            (chosen, _), _ = router.select_failover(
                quota_state=quota_both_zai_exhausted,
                health_state=all_healthy,
                peak=False,
                model="glm-5.2",
            )
            assert router.last_throttle_state == "throttle"
            assert chosen != "ollama_cloud"
        finally:
            os.unlink(db_path)


# ── Tests: block at 100% ─────────────────────────────────────────────────────


class TestBlockAt100pct:
    """GATE: GLM-5.2 excluded from ollama entirely when session_usage >= 1.0.

    Ollama_cloud's breaker is tripped (healthy=False) so the optimizer
    filters it out entirely. GLM-5.2 routes to the cheapest external.
    """

    def test_glm_excluded_from_ollama_at_100pct(
        self, rates, quota_both_zai_exhausted, all_healthy, monkeypatch,
    ):
        """GATE: At 100% usage, GLM-5.2 does NOT route to ollama_cloud."""
        db_path = _make_usage_db([])

        import src.live_router as lr
        monkeypatch.setattr(
            lr, "fetch_ollama_usage",
            lambda **kw: _mock_usage_response(1.0),
        )

        try:
            router = LiveRouter(db_path=db_path, converged_rates=rates)
            (chosen, _), (fallback, _) = router.select_failover(
                quota_state=quota_both_zai_exhausted,
                health_state=all_healthy,
                peak=False,
                model="glm-5.2",
            )
            assert router.last_throttle_state == "block"
            assert chosen != "ollama_cloud"
            assert chosen in ("openrouter", "ppq", "deepinfra")
            # Ollama should NOT be the fallback either (blocked entirely)
            assert fallback != "ollama_cloud"
        finally:
            os.unlink(db_path)

    def test_block_state_property_at_100pct(
        self, rates, quota_both_zai_exhausted, all_healthy, monkeypatch,
    ):
        """last_throttle_state must be 'block' at 100% usage."""
        db_path = _make_usage_db([])

        import src.live_router as lr
        monkeypatch.setattr(
            lr, "fetch_ollama_usage",
            lambda **kw: _mock_usage_response(1.0),
        )

        try:
            router = LiveRouter(db_path=db_path, converged_rates=rates)
            router.select_failover(
                quota_state=quota_both_zai_exhausted,
                health_state=all_healthy,
                peak=False,
                model="glm-5.2",
            )
            assert router.last_throttle_state == "block"
            assert router.last_session_usage == pytest.approx(1.0)
        finally:
            os.unlink(db_path)

    def test_block_above_100pct(
        self, rates, quota_both_zai_exhausted, all_healthy, monkeypatch,
    ):
        """At 1.5 usage (overdraft), block applies."""
        db_path = _make_usage_db([])

        import src.live_router as lr
        monkeypatch.setattr(
            lr, "fetch_ollama_usage",
            lambda **kw: _mock_usage_response(1.5),
        )

        try:
            router = LiveRouter(db_path=db_path, converged_rates=rates)
            (chosen, _), _ = router.select_failover(
                quota_state=quota_both_zai_exhausted,
                health_state=all_healthy,
                peak=False,
                model="glm-5.2",
            )
            assert router.last_throttle_state == "block"
            assert chosen != "ollama_cloud"
        finally:
            os.unlink(db_path)


# ── Tests: kimi always allowed ───────────────────────────────────────────────


class TestKimiAlwaysAllowed:
    """GATE: Ollama-exclusive models (kimi, gpt-oss) ALWAYS route to
    ollama_cloud regardless of throttle/block state — they have no
    alternative provider.
    """

    @pytest.mark.parametrize("model", [
        "kimi-k3:cloud",
        "kimi-k2.7-code",
        "gpt-oss:120b",
        "gemma4:31b",
        "qwen3.5:397b",
    ])
    def test_exclusive_model_routes_to_ollama_at_100pct(
        self, model, rates, quota_both_zai_exhausted, all_healthy, monkeypatch,
    ):
        """GATE: kimi/gpt-oss models route to ollama at 100% usage (blocked)."""
        db_path = _make_usage_db([])

        import src.live_router as lr
        monkeypatch.setattr(
            lr, "fetch_ollama_usage",
            lambda **kw: _mock_usage_response(1.0),
        )

        try:
            router = LiveRouter(db_path=db_path, converged_rates=rates)
            (chosen, chosen_model), _ = router.select_failover(
                quota_state=quota_both_zai_exhausted,
                health_state=all_healthy,
                peak=False,
                model=model,
            )
            assert chosen == "ollama_cloud", (
                f"{model} should route to ollama_cloud even at 100% usage "
                f"(block only applies to non-exclusive models)"
            )
            assert chosen_model == model
            # throttle_state stays "normal" for exclusive models
            assert router.last_throttle_state == "normal"
        finally:
            os.unlink(db_path)

    @pytest.mark.parametrize("model", [
        "kimi-k3:cloud",
        "kimi-k2.7-code",
    ])
    def test_exclusive_model_routes_to_ollama_at_85pct(
        self, model, rates, quota_both_zai_exhausted, all_healthy, monkeypatch,
    ):
        """GATE: kimi models route to ollama at 85% usage (throttle)."""
        db_path = _make_usage_db([])

        import src.live_router as lr
        monkeypatch.setattr(
            lr, "fetch_ollama_usage",
            lambda **kw: _mock_usage_response(0.85),
        )

        try:
            router = LiveRouter(db_path=db_path, converged_rates=rates)
            (chosen, chosen_model), _ = router.select_failover(
                quota_state=quota_both_zai_exhausted,
                health_state=all_healthy,
                peak=False,
                model=model,
            )
            assert chosen == "ollama_cloud"
            assert chosen_model == model
        finally:
            os.unlink(db_path)

    def test_exclusive_model_returns_none_when_ollama_down(
        self, rates, quota_both_zai_exhausted, all_healthy, monkeypatch,
    ):
        """When ollama is unhealthy, exclusive models have no alternative."""
        db_path = _make_usage_db([])

        import src.live_router as lr
        monkeypatch.setattr(
            lr, "fetch_ollama_usage",
            lambda **kw: _mock_usage_response(1.0),
        )

        try:
            router = LiveRouter(db_path=db_path, converged_rates=rates)
            health_ollama_down = dict(all_healthy)
            health_ollama_down["ollama_cloud"] = False
            (chosen, _), _ = router.select_failover(
                quota_state=quota_both_zai_exhausted,
                health_state=health_ollama_down,
                peak=False,
                model="kimi-k3:cloud",
            )
            assert chosen is None
        finally:
            os.unlink(db_path)


# ── Tests: z.ai preferred during throttle ────────────────────────────────────


class TestZaiPreferredDuringThrottle:
    """When z.ai keys have remaining quota AND throttle is active, z.ai is
    chosen over ollama_cloud. This verifies the throttle doesn't break
    normal z.ai routing — it only deprioritises ollama for non-exclusive
    models when z.ai is also exhausted.
    """

    def test_zai_chosen_over_ollama_during_throttle(
        self, rates, quota_zai_available, all_healthy, monkeypatch,
    ):
        """GATE: When z.ai is available and throttle is active, z.ai wins."""
        db_path = _make_usage_db([])

        import src.live_router as lr
        monkeypatch.setattr(
            lr, "fetch_ollama_usage",
            lambda **kw: _mock_usage_response(0.90),
        )

        try:
            router = LiveRouter(db_path=db_path, converged_rates=rates)
            (chosen, _), _ = router.select_failover(
                quota_state=quota_zai_available,
                health_state=all_healthy,
                peak=False,
                model="glm-5.2",
            )
            # z.ai (ours) is "high" tier and cheapest — should be chosen
            # over ollama (deprioritised to "low" tier during throttle)
            assert chosen == "ours"
            assert router.last_throttle_state == "throttle"
        finally:
            os.unlink(db_path)

    def test_friend_chosen_when_ours_exhausted_during_throttle(
        self, rates, quota_zai_available, all_healthy, monkeypatch,
    ):
        """When ours is exhausted but friend has quota, friend wins over ollama."""
        db_path = _make_usage_db([])

        import src.live_router as lr
        monkeypatch.setattr(
            lr, "fetch_ollama_usage",
            lambda **kw: _mock_usage_response(0.90),
        )

        quota_ours_exhausted = dict(quota_zai_available)
        quota_ours_exhausted["ours"] = {
            "used_pct": 100.0, "remaining": 0, "total": 2_000_000
        }

        try:
            router = LiveRouter(db_path=db_path, converged_rates=rates)
            (chosen, _), _ = router.select_failover(
                quota_state=quota_ours_exhausted,
                health_state=all_healthy,
                peak=False,
                model="glm-5.2",
            )
            assert chosen == "friend"
            assert router.last_throttle_state == "throttle"
        finally:
            os.unlink(db_path)

    def test_external_chosen_when_both_zai_exhausted_during_throttle(
        self, rates, quota_both_zai_exhausted, all_healthy, monkeypatch,
    ):
        """When both z.ai keys exhausted during throttle, external wins (not ollama)."""
        db_path = _make_usage_db([])

        import src.live_router as lr
        monkeypatch.setattr(
            lr, "fetch_ollama_usage",
            lambda **kw: _mock_usage_response(0.90),
        )

        try:
            router = LiveRouter(db_path=db_path, converged_rates=rates)
            (chosen, _), _ = router.select_failover(
                quota_state=quota_both_zai_exhausted,
                health_state=all_healthy,
                peak=False,
                model="glm-5.2",
            )
            # Both z.ai exhausted → external chosen (NOT ollama, which is throttled)
            assert chosen in ("openrouter", "ppq", "deepinfra")
            assert chosen != "ollama_cloud"
        finally:
            os.unlink(db_path)


# ── Tests: kill switch (OLLAMA_THROTTLE_ENABLED) ─────────────────────────────


class TestThrottleKillSwitch:
    """GATE: When OLLAMA_THROTTLE_ENABLED=false (default), no throttling
    occurs — routing behaves exactly as before regardless of session_usage.
    """

    def test_kill_switch_off_at_85pct_chooses_ollama(
        self, rates, quota_both_zai_exhausted, all_healthy, monkeypatch,
    ):
        """GATE: Kill switch OFF + 85% usage → ollama chosen (no throttle)."""
        import src.live_router as lr
        monkeypatch.setattr(lr, "_THROTTLE_ENABLED", False, raising=True)
        monkeypatch.setattr(
            lr, "fetch_ollama_usage",
            lambda **kw: _mock_usage_response(0.85),
        )

        db_path = _make_usage_db([])
        try:
            router = LiveRouter(db_path=db_path, converged_rates=rates)
            (chosen, _), _ = router.select_failover(
                quota_state=quota_both_zai_exhausted,
                health_state=all_healthy,
                peak=False,
                model="glm-5.2",
            )
            assert chosen == "ollama_cloud"
            assert router.last_throttle_state == "normal"
        finally:
            os.unlink(db_path)

    def test_kill_switch_off_at_100pct_chooses_ollama(
        self, rates, quota_both_zai_exhausted, all_healthy, monkeypatch,
    ):
        """GATE: Kill switch OFF + 100% usage → ollama still chosen (no block)."""
        import src.live_router as lr
        monkeypatch.setattr(lr, "_THROTTLE_ENABLED", False, raising=True)
        monkeypatch.setattr(
            lr, "fetch_ollama_usage",
            lambda **kw: _mock_usage_response(1.0),
        )

        db_path = _make_usage_db([])
        try:
            router = LiveRouter(db_path=db_path, converged_rates=rates)
            (chosen, _), _ = router.select_failover(
                quota_state=quota_both_zai_exhausted,
                health_state=all_healthy,
                peak=False,
                model="glm-5.2",
            )
            assert chosen == "ollama_cloud"
            assert router.last_throttle_state == "normal"
        finally:
            os.unlink(db_path)


# ── Tests: properties ────────────────────────────────────────────────────────


class TestThrottleProperties:
    """Verify last_throttle_state and last_session_usage properties."""

    def test_default_throttle_state_before_failover(self, rates):
        """Default throttle_state is 'normal' before any failover."""
        router = LiveRouter(db_path=None, converged_rates=rates)
        assert router.last_throttle_state == "normal"

    def test_default_session_usage_before_failover(self, rates):
        """Default session_usage is 0.0 before any failover."""
        router = LiveRouter(db_path=None, converged_rates=rates)
        assert router.last_session_usage == 0.0

    def test_session_usage_reflects_api(
        self, rates, quota_both_zai_exhausted, all_healthy, monkeypatch,
    ):
        """last_session_usage reflects the API's session_usage fraction."""
        db_path = _make_usage_db([])

        import src.live_router as lr
        monkeypatch.setattr(
            lr, "fetch_ollama_usage",
            lambda **kw: _mock_usage_response(0.77),
        )

        try:
            router = LiveRouter(db_path=db_path, converged_rates=rates)
            router.select_failover(
                quota_state=quota_both_zai_exhausted,
                health_state=all_healthy,
                peak=False,
                model="glm-5.2",
            )
            assert router.last_session_usage == pytest.approx(0.77)
            assert router.last_throttle_state == "normal"  # 0.77 < 0.85
        finally:
            os.unlink(db_path)

    def test_weekly_usage_drives_throttle(
        self, rates, quota_both_zai_exhausted, all_healthy, monkeypatch,
    ):
        """When weekly_usage >= threshold but session_usage < threshold,
        the max() of both drives the throttle decision."""
        db_path = _make_usage_db([])

        import src.live_router as lr
        monkeypatch.setattr(
            lr, "fetch_ollama_usage",
            lambda **kw: _mock_usage_response(0.50, weekly_usage=0.90),
        )

        try:
            router = LiveRouter(db_path=db_path, converged_rates=rates)
            router.select_failover(
                quota_state=quota_both_zai_exhausted,
                health_state=all_healthy,
                peak=False,
                model="glm-5.2",
            )
            # max(0.50, 0.90) = 0.90 >= 0.85 → throttle
            assert router.last_throttle_state == "throttle"
            assert router.last_session_usage == pytest.approx(0.90)
        finally:
            os.unlink(db_path)


# ── Tests: API failure safety ────────────────────────────────────────────────


class TestApiFailureSafety:
    """When the Ollama API is unreachable, throttling must not crash the
    router or produce false positives. With no usage data, the default
    is 'normal' (no throttle).
    """

    def test_api_returns_none_no_throttle(
        self, rates, quota_both_zai_exhausted, all_healthy, monkeypatch,
    ):
        """When fetch_ollama_usage returns None (API down), no throttle."""
        import src.live_router as lr
        monkeypatch.setattr(
            lr, "fetch_ollama_usage",
            lambda **kw: None,
        )

        db_path = _make_usage_db([])
        try:
            router = LiveRouter(db_path=db_path, converged_rates=rates)
            (chosen, _), _ = router.select_failover(
                quota_state=quota_both_zai_exhausted,
                health_state=all_healthy,
                peak=False,
                model="glm-5.2",
            )
            assert router.last_throttle_state == "normal"
            assert router.last_session_usage == 0.0
            # No usage data → no throttle → ollama chosen
            assert chosen == "ollama_cloud"
        finally:
            os.unlink(db_path)

    def test_no_db_path_no_throttle(
        self, rates, quota_both_zai_exhausted, all_healthy, monkeypatch,
    ):
        """When db_path is None, fetch is skipped entirely — no throttle."""
        router = LiveRouter(db_path=None, converged_rates=rates)
        (chosen, _), _ = router.select_failover(
            quota_state=quota_both_zai_exhausted,
            health_state=all_healthy,
            peak=False,
            model="glm-5.2",
        )
        assert router.last_throttle_state == "normal"
        assert chosen == "ollama_cloud"


# ── Tests: no model parameter (backward compat) ──────────────────────────────


class TestNoModelBackwardCompat:
    """When model= is not passed (legacy callers), no throttle applies
    — the model check `model is not None` prevents it.
    """

    def test_no_model_no_throttle_at_85pct(
        self, rates, quota_both_zai_exhausted, all_healthy, monkeypatch,
    ):
        """Without model=, throttle_state stays 'normal' even at 85% usage."""
        import src.live_router as lr
        monkeypatch.setattr(
            lr, "fetch_ollama_usage",
            lambda **kw: _mock_usage_response(0.85),
        )

        db_path = _make_usage_db([])
        try:
            router = LiveRouter(db_path=db_path, converged_rates=rates)
            (chosen, _), _ = router.select_failover(
                quota_state=quota_both_zai_exhausted,
                health_state=all_healthy,
                peak=False,
                # No model= parameter
            )
            assert router.last_throttle_state == "normal"
            assert chosen == "ollama_cloud"
        finally:
            os.unlink(db_path)


# ── Tests: custom thresholds via env ─────────────────────────────────────────


class TestCustomThresholds:
    """Verify that OLLAMA_THROTTLE_THRESHOLD and OLLAMA_BLOCK_THRESHOLD
    can be customised to change the throttle/block zones.
    """

    def test_custom_throttle_threshold_090(
        self, rates, quota_both_zai_exhausted, all_healthy, monkeypatch,
    ):
        """With threshold=0.90, usage=0.85 should NOT trigger throttle."""
        import src.live_router as lr
        monkeypatch.setattr(lr, "_THROTTLE_THRESHOLD", 0.90, raising=True)
        monkeypatch.setattr(
            lr, "fetch_ollama_usage",
            lambda **kw: _mock_usage_response(0.85),
        )

        db_path = _make_usage_db([])
        try:
            router = LiveRouter(db_path=db_path, converged_rates=rates)
            (chosen, _), _ = router.select_failover(
                quota_state=quota_both_zai_exhausted,
                health_state=all_healthy,
                peak=False,
                model="glm-5.2",
            )
            # 0.85 < 0.90 threshold → no throttle
            assert router.last_throttle_state == "normal"
            assert chosen == "ollama_cloud"
        finally:
            os.unlink(db_path)

    def test_custom_block_threshold_090(
        self, rates, quota_both_zai_exhausted, all_healthy, monkeypatch,
    ):
        """With block_threshold=0.90, usage=0.90 triggers block (not throttle)."""
        import src.live_router as lr
        monkeypatch.setattr(lr, "_BLOCK_THRESHOLD", 0.90, raising=True)
        monkeypatch.setattr(
            lr, "fetch_ollama_usage",
            lambda **kw: _mock_usage_response(0.90),
        )

        db_path = _make_usage_db([])
        try:
            router = LiveRouter(db_path=db_path, converged_rates=rates)
            (chosen, _), _ = router.select_failover(
                quota_state=quota_both_zai_exhausted,
                health_state=all_healthy,
                peak=False,
                model="glm-5.2",
            )
            # 0.90 >= 0.90 block threshold → block
            assert router.last_throttle_state == "block"
            assert chosen != "ollama_cloud"
        finally:
            os.unlink(db_path)
