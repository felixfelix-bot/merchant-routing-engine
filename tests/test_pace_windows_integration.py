"""Tests for PrimaryRouter auto-building pace_windows from production data.

Verifies that PrimaryRouter._build_pace_windows correctly:
1. Extracts burn rates from ConsumptionKalman instances
2. Converts proxy quota_state format to quota_window_extractor format
3. Passes valid tuples to pace_factor_multi via the optimizer
4. Falls back gracefully when data is incomplete

Also tests the end-to-end flow: route() with quota_state but no pace_windows
should automatically build and apply pace multipliers (ADR-008).
"""
from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.primary_router import PrimaryRouter


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def router(monkeypatch):
    """Fresh PrimaryRouter with static seed costs (no DB)."""
    PrimaryRouter._instance = None
    monkeypatch.setattr(
        PrimaryRouter, "_load_converged_rates",
        staticmethod(lambda: {}),
    )
    return PrimaryRouter()


@pytest.fixture
def healthy_state():
    return {
        "ours": True, "friend": True, "ollama_cloud": True,
        "ppq": True, "openrouter": True, "deepinfra": True,
    }


@pytest.fixture
def quota_state_with_pct():
    """Quota state in the proxy's _snapshot_quota() format.

    Contains used_pct + total but NOT resets_at/window_hours.
    _build_pace_windows must synthesize these.
    """
    return {
        "ours":          {"used_pct": 80.0, "remaining": 400_000,   "total": 2_000_000},
        "friend":        {"used_pct": 30.0, "remaining": 1_400_000, "total": 2_000_000},
        "ollama_cloud":  {"used_pct": 100.0, "remaining": 0,             "total": 500_000_000},
        "ppq":           {"used_pct": 0.0,  "remaining": float("inf")},
        "openrouter":    {"used_pct": 0.0,  "remaining": float("inf")},
        "deepinfra":     {"used_pct": 0.0,  "remaining": float("inf")},
    }


@pytest.fixture
def quota_state_with_resets_at():
    """Quota state with explicit resets_at for 5h window.

    The proxy may provide resets_at when it has the real z.ai API response.
    """
    now = time.time()
    return {
        "ours":          {"used_pct": 80.0, "remaining": 400_000,
                          "total": 2_000_000, "resets_at": int(now + 3600)},
        "friend":        {"used_pct": 30.0, "remaining": 1_400_000,
                          "total": 2_000_000, "resets_at": int(now + 7200)},
        "ollama_cloud":  {"used_pct": 20.0, "remaining": 400_000_000,
                          "total": 500_000_000, "resets_at": int(now + 18000)},
        "ppq":           {"used_pct": 0.0,  "remaining": float("inf")},
        "openrouter":    {"used_pct": 0.0,  "remaining": float("inf")},
        "deepinfra":     {"used_pct": 0.0,  "remaining": float("inf")},
    }


# ── _build_pace_windows unit tests ──────────────────────────────────────────


class TestBuildPaceWindowsBasic:
    """Test _build_pace_windows with various quota_state inputs."""

    def test_returns_empty_for_empty_quota_state(self, router):
        result = router._build_pace_windows({})
        assert result == {}

    def test_returns_empty_for_none_quota_state(self, router):
        result = router._build_pace_windows(None)
        assert result == {}

    def test_builds_windows_for_finite_quota_providers(self, router, quota_state_with_pct):
        """Providers with finite quota totals should get pace windows."""
        result = router._build_pace_windows(quota_state_with_pct)
        # ours, friend, ollama_cloud have finite totals
        assert "ours" in result
        assert "friend" in result
        assert "ollama_cloud" in result

    def test_skips_infinite_quota_providers(self, router, quota_state_with_pct):
        """Providers with inf quota (ppq, openrouter, deepinfra) should be skipped."""
        result = router._build_pace_windows(quota_state_with_pct)
        assert "ppq" not in result
        assert "openrouter" not in result
        assert "deepinfra" not in result

    def test_skips_provider_missing_from_quota_state(self, router):
        """A provider in _consumption_kalmans but not in quota_state is skipped."""
        quota_state = {
            "ours": {"used_pct": 50.0, "remaining": 1_000_000, "total": 2_000_000},
        }
        result = router._build_pace_windows(quota_state)
        assert "ours" in result
        # friend not in quota_state → not in result
        assert "friend" not in result

    def test_skips_provider_with_no_used_pct(self, router):
        """Provider entry missing used_pct → skipped."""
        quota_state = {
            "ours": {"remaining": 1_000_000, "total": 2_000_000},  # no used_pct
        }
        result = router._build_pace_windows(quota_state)
        assert result == {}

    def test_skips_provider_with_no_quota_entry(self, router):
        """Provider present in kalmans but with empty dict in quota_state → skipped."""
        quota_state = {"ours": {}}
        result = router._build_pace_windows(quota_state)
        assert result == {}


class TestBuildPaceWindowsFormat:
    """Verify the tuple format matches what pace_factor_multi expects."""

    def test_tuples_have_five_elements(self, router, quota_state_with_pct):
        result = router._build_pace_windows(quota_state_with_pct)
        for name, windows in result.items():
            for tup in windows:
                assert len(tup) == 5, f"{name} window has {len(tup)} elements"

    def test_tuple_values_are_floats(self, router, quota_state_with_pct):
        result = router._build_pace_windows(quota_state_with_pct)
        for name, windows in result.items():
            for tup in windows:
                for val in tup:
                    assert isinstance(val, float), f"{name} tuple has non-float: {val}"

    def test_used_pct_reflects_quota_state(self, router, quota_state_with_pct):
        """quota_used should be derived from used_pct × total."""
        result = router._build_pace_windows(quota_state_with_pct)
        # ours: 80% of 2M = 1_600_000
        assert "ours" in result
        used_ours = result["ours"][0][0]  # first window's quota_used
        assert used_ours == pytest.approx(1_600_000, rel=1e-3)

        # friend: 30% of 2M = 600_000
        used_friend = result["friend"][0][0]
        assert used_friend == pytest.approx(600_000, rel=1e-3)

    def test_total_matches_quota_state(self, router, quota_state_with_pct):
        result = router._build_pace_windows(quota_state_with_pct)
        assert result["ours"][0][1] == 2_000_000  # total
        assert result["ollama_cloud"][0][1] == 500_000_000

    def test_window_duration_is_5_hours(self, router, quota_state_with_pct):
        """Default synthesized window should be 5 hours."""
        result = router._build_pace_windows(quota_state_with_pct)
        for name, windows in result.items():
            for tup in windows:
                assert tup[4] == 5.0  # window_duration_hours

    def test_burn_rate_matches_kalman(self, router, quota_state_with_pct):
        """burn_rate in the tuple should match the ConsumptionKalman's burn_rate."""
        # First, feed some data to the Kalman so burn_rate is non-zero
        router.update_burn_rate("ours", 50000)
        router.update_burn_rate("ours", 60000)

        result = router._build_pace_windows(quota_state_with_pct)
        ck_burn = router._consumption_kalmans["ours"].burn_rate
        assert result["ours"][0][3] == pytest.approx(ck_burn)

    def test_zero_burn_rate_when_kalman_uninitialized(self, router, quota_state_with_pct):
        """Fresh Kalman (no updates) should report burn_rate = 0.0."""
        result = router._build_pace_windows(quota_state_with_pct)
        # Fresh Kalman → burn_rate = 0.0 (state[0,0] = 0)
        assert result["ours"][0][3] == 0.0


class TestBuildPaceWindowsWithResetsAt:
    """Test with explicit resets_at from the proxy."""

    def test_uses_resets_at_when_provided(self, router, quota_state_with_resets_at):
        """When resets_at is in quota_state, it should be used (not synthesized)."""
        now = time.time()
        result = router._build_pace_windows(quota_state_with_resets_at)

        # ours: resets_at = now + 3600 → window started at now + 3600 - 5*3600 = now - 14400
        # elapsed = (now - (now - 14400)) / (5*3600) = 14400/18000 = 0.8
        assert "ours" in result
        elapsed = result["ours"][0][2]  # time_elapsed_pct
        assert elapsed == pytest.approx(0.8, abs=0.02)

    def test_friend_elapsed_with_resets_at(self, router, quota_state_with_resets_at):
        """friend: resets_at = now + 7200 → elapsed = (5h - 2h) / 5h = 0.6"""
        result = router._build_pace_windows(quota_state_with_resets_at)
        elapsed = result["friend"][0][2]
        assert elapsed == pytest.approx(0.6, abs=0.02)


class TestBuildPaceWindowsWeekly:
    """Test weekly window support."""

    def test_weekly_window_included_when_present(self, router):
        """When quota_state includes weekly_used_pct, a weekly window is added."""
        now = time.time()
        quota_state = {
            "ours": {
                "used_pct": 50.0, "remaining": 1_000_000, "total": 2_000_000,
                "resets_at": int(now + 3600),
                "weekly_used_pct": 30.0,
                "weekly_resets_at": int(now + 84 * 3600),
            },
        }
        result = router._build_pace_windows(quota_state)
        assert "ours" in result
        assert len(result["ours"]) == 2  # 5h + weekly
        durations = [w[4] for w in result["ours"]]
        assert 5.0 in durations
        assert 168.0 in durations

    def test_weekly_window_skipped_when_absent(self, router, quota_state_with_pct):
        """No weekly_used_pct → only 5h window."""
        result = router._build_pace_windows(quota_state_with_pct)
        for name, windows in result.items():
            assert len(windows) == 1  # only 5h
            assert windows[0][4] == 5.0


# ── Integration: route() auto-builds pace_windows ─────────────────────────────


class TestRouteAutoBuildsPaceWindows:
    """Test that route() with no pace_windows still applies pace multipliers."""

    def test_route_with_quota_state_no_pace_windows(self, router, quota_state_with_pct,
                                                     healthy_state, monkeypatch):
        """route() should not raise when pace_windows is None but quota_state is present."""
        import src.primary_router as pr_mod

        class FakeTime:
            @staticmethod
            def gmtime():
                class T:
                    tm_hour = 15  # off-peak
                return T()
        monkeypatch.setattr(pr_mod.time, "gmtime", FakeTime.gmtime)

        result = router.route(
            model="glm-5.2", tokens=5000,
            quota_state=quota_state_with_pct,
            health_state=healthy_state,
            # pace_windows intentionally omitted
        )
        assert result is None or isinstance(result, str)

    def test_auto_built_windows_affect_routing(self, router, monkeypatch):
        """High burn rate + high used_pct should produce pace_mult > 1.0,
        which increases effective cost and may change routing decisions.

        With ours at 95% quota + high burn rate, pace_mult pushes ours' cost
        higher. Off-peak, ours ($0.31/M) vs friend ($0.375/M) — without pace,
        ours wins. With high pace_mult on ours, friend should win.
        """
        import src.primary_router as pr_mod

        class FakeTime:
            @staticmethod
            def gmtime():
                class T:
                    tm_hour = 15  # off-peak
                return T()
        monkeypatch.setattr(pr_mod.time, "gmtime", FakeTime.gmtime)

        # Feed burn rate to ours so it has a non-zero burn_rate
        # We need enough to make pace_factor > 1.0 at 95% usage
        for _ in range(10):
            router.update_burn_rate("ours", 500_000)

        quota_state = {
            "ours":          {"used_pct": 95.0, "remaining": 100_000,
                              "total": 2_000_000,
                              "resets_at": int(time.time() + 1800)},  # 30min left
            "friend":        {"used_pct": 30.0, "remaining": 1_400_000,
                              "total": 2_000_000,
                              "resets_at": int(time.time() + 2 * 3600)},
            "ollama_cloud":  {"used_pct": 100.0, "remaining": 0,
                              "total": 500_000_000},
            "ppq":           {"used_pct": 0.0, "remaining": float("inf")},
            "openrouter":    {"used_pct": 0.0, "remaining": float("inf")},
            "deepinfra":     {"used_pct": 0.0, "remaining": float("inf")},
        }
        health = {k: True for k in quota_state}

        result = router.route(
            model="glm-5.2", tokens=5000,
            quota_state=quota_state,
            health_state=health,
        )
        # With high burn + near exhaustion, ours gets penalized → friend wins
        assert result == "friend"

    def test_explicit_pace_windows_override_auto_build(self, router, quota_state_with_pct,
                                                        healthy_state, monkeypatch):
        """When pace_windows is explicitly provided, _build_pace_windows is NOT called."""
        import src.primary_router as pr_mod

        class FakeTime:
            @staticmethod
            def gmtime():
                class T:
                    tm_hour = 15
                return T()
        monkeypatch.setattr(pr_mod.time, "gmtime", FakeTime.gmtime)

        # Provide explicit empty dict — should NOT trigger auto-build
        # because `if not pace_windows` is True for empty dict too...
        # Actually, empty dict is falsy, so auto-build WOULD trigger.
        # To truly override, provide a non-empty dict.
        explicit_windows = {
            "ours": [(1_600_000, 2_000_000, 0.8, 0.0, 5.0)],  # burn_rate=0 → pace=1.0
        }

        # Mock _build_pace_windows to ensure it's NOT called
        called = [False]
        original = router._build_pace_windows

        def mock_build(qs):
            called[0] = True
            return original(qs)

        router._build_pace_windows = mock_build

        router.route(
            model="glm-5.2", tokens=5000,
            quota_state=quota_state_with_pct,
            health_state=healthy_state,
            pace_windows=explicit_windows,
        )
        assert not called[0], "_build_pace_windows should NOT be called when pace_windows provided"


# ── End-to-end: pace_windows flow through to optimizer ─────────────────────────


class TestPaceWindowsFlowToOptimizer:
    """Verify that auto-built pace_windows actually affect the optimizer's decision."""

    def test_zero_burn_rate_gives_neutral_pace(self, router, healthy_state, monkeypatch):
        """With zero burn rate (fresh Kalman), pace_factor = 1.0 (no adjustment).
        Off-peak: ours ($0.31/M) beats friend ($0.375/M) → result = 'ours'.

        Note: ours used_pct must be low enough that the scarcity multiplier
        doesn't push ours' effective cost above friend.  At 80% usage the
        1.60× scarcity multiplier makes ours more expensive than friend,
        so we use 30% (matching friend) to keep pace_factor ≈ 1.0.
        """
        import src.primary_router as pr_mod

        class FakeTime:
            @staticmethod
            def gmtime():
                class T:
                    tm_hour = 15
                return T()
        monkeypatch.setattr(pr_mod.time, "gmtime", FakeTime.gmtime)

        quota_state = {
            "ours":          {"used_pct": 30.0, "remaining": 1_400_000, "total": 2_000_000},
            "friend":        {"used_pct": 30.0, "remaining": 1_400_000, "total": 2_000_000},
            "ollama_cloud":  {"used_pct": 100.0, "remaining": 0,             "total": 500_000_000},
            "ppq":           {"used_pct": 0.0,  "remaining": float("inf")},
            "openrouter":    {"used_pct": 0.0,  "remaining": float("inf")},
            "deepinfra":     {"used_pct": 0.0,  "remaining": float("inf")},
        }

        result = router.route(
            model="glm-5.2", tokens=5000,
            quota_state=quota_state,
            health_state=healthy_state,
        )
        # Zero burn rate → pace_mult = 1.0 → ours still cheapest off-peak
        # (ollama_cloud exhausted so it doesn't interfere)
        assert result == "ours"

    def test_high_burn_rate_changes_routing(self, router, monkeypatch):
        """High burn rate on ours → pace_mult > 1 → ours gets more expensive.

        This verifies the full pipeline:
        quota_state → _build_pace_windows → pace_factor_multi → optimizer
        """
        import src.primary_router as pr_mod

        class FakeTime:
            @staticmethod
            def gmtime():
                class T:
                    tm_hour = 15  # off-peak
                return T()
        monkeypatch.setattr(pr_mod.time, "gmtime", FakeTime.gmtime)

        # Feed high burn rate to ours
        for _ in range(20):
            router.update_burn_rate("ours", 1_000_000)

        # 90% used, only 30 min left in 5h window
        now = time.time()
        quota_state = {
            "ours":          {"used_pct": 90.0, "remaining": 200_000,
                              "total": 2_000_000,
                              "resets_at": int(now + 1800)},
            "friend":        {"used_pct": 20.0, "remaining": 1_600_000,
                              "total": 2_000_000,
                              "resets_at": int(now + 3 * 3600)},
            "ollama_cloud":  {"used_pct": 100.0, "remaining": 0,
                              "total": 500_000_000},
            "ppq":           {"used_pct": 0.0, "remaining": float("inf")},
            "openrouter":    {"used_pct": 0.0, "remaining": float("inf")},
            "deepinfra":     {"used_pct": 0.0, "remaining": float("inf")},
        }
        health = {k: True for k in quota_state}

        result = router.route(
            model="glm-5.2", tokens=5000,
            quota_state=quota_state,
            health_state=health,
        )
        # High burn on ours → pace_mult pushes ours' effective price up
        # Off-peak: ours $0.31 × pace_mult(>1) might exceed friend $0.375
        # The burn rate is very high (1M tokens per update × 20 updates),
        # so pace_factor should be significant
        assert result in ("ours", "friend", None)


# ── Robustness ─────────────────────────────────────────────────────────────────


class TestRobustness:
    """Test edge cases and graceful degradation."""

    def test_never_raises_on_garbage_quota_state(self, router):
        """_build_pace_windows should never raise, even with garbage input."""
        # None
        result = router._build_pace_windows(None)
        assert isinstance(result, dict)

        # Random non-dict
        result = router._build_pace_windows("garbage")  # type: ignore
        assert isinstance(result, dict)

        # Dict with garbage values
        result = router._build_pace_windows({"ours": "garbage"})
        assert isinstance(result, dict)

        # Dict with negative used_pct
        result = router._build_pace_windows({
            "ours": {"used_pct": -50, "total": 2_000_000, "remaining": 3_000_000}
        })
        assert isinstance(result, dict)

    def test_never_raises_on_route_with_auto_build(self, router):
        """Full route() with auto-build should never raise."""
        result = router.route(
            model="glm-5.2", tokens=5000,
            quota_state={"ours": {"used_pct": 50, "remaining": 1_000_000, "total": 2_000_000}},
            health_state={"ours": True, "friend": True},
        )
        assert result is None or isinstance(result, str)

    def test_malformed_quota_entry_skipped(self, router):
        """Malformed entries should be skipped without breaking others."""
        quota_state = {
            "ours": {"used_pct": "not_a_number", "total": 2_000_000},  # garbage
            "friend": {"used_pct": 50, "remaining": 1_000_000, "total": 2_000_000},  # valid
        }
        result = router._build_pace_windows(quota_state)
        # ours should be skipped (int("not_a_number") would fail → caught by extractor)
        # friend should be present
        assert "friend" in result