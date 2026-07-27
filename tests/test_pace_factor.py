"""Tests for src/pricing_engine.py — pace_factor (predictive quota-pacing).

Covers the predictive burn-rate regulator that adjusts price based on
whether we're on pace to exhaust quota before the window resets.

Priority: NEVER run out > use everything.

  time_remaining_hours = (1 - time_elapsed_pct) * window_duration_hours
  predicted_usage = burn_rate * time_remaining_hours
  predicted_total = quota_used + predicted_usage
  pace_ratio = predicted_total / quota_total
  pace_factor = max(0.5, min(3.0, pace_ratio ** 2))

  pace_ratio > 1.0  → will exhaust → increase price (slow traffic)
  pace_ratio < 0.9  → underutilizing → decrease price (attract traffic)
  0.9 ≤ ratio ≤ 1.0 → optimal pace → ~1.0 multiplier

All tests use window_duration_hours=5.0 (z.ai 5h window) unless otherwise
noted. At perfect pace: burn_rate = quota_total / window_duration_hours.
"""
from __future__ import annotations

import math

import pytest

from src.pricing_engine import pace_factor, pace_factor_multi


# ── Perfect pace ────────────────────────────────────────────────────────────


class TestPerfectPace:
    def test_perfect_pace_returns_one(self):
        """pace_ratio = 1.0 → pace_factor = 1.0.

        window=5h, used=800, total=1000, elapsed=0.8, burn=200/h:
        remaining = (1-0.8)*5 = 1h → predicted = 800 + 200*1 = 1000 → ratio=1.0
        """
        pf = pace_factor(
            quota_used=800,
            quota_total=1000,
            time_elapsed_pct=0.8,
            burn_rate=200,
            window_duration_hours=5.0,
        )
        assert pf == pytest.approx(1.0)

    def test_optimal_band_returns_near_one(self):
        """pace_ratio = 0.95 → pace_factor = 0.9025.

        window=5h, used=700, total=1000, elapsed=0.75, burn=200/h:
        remaining = (1-0.75)*5 = 1.25h → predicted = 700 + 200*1.25 = 950 → ratio=0.95
        """
        pf = pace_factor(
            quota_used=700,
            quota_total=1000,
            time_elapsed_pct=0.75,
            burn_rate=200,
            window_duration_hours=5.0,
        )
        assert pf == pytest.approx(0.9025, rel=1e-4)
        assert pf < 1.0


# ── Underutilizing ──────────────────────────────────────────────────────────


class TestUnderutilizing:
    def test_underutilizing_decreases_price(self):
        """pace_ratio = 0.75 → pace_factor = 0.5625 (< 1.0, attract traffic).

        window=5h, used=600, total=1000, elapsed=0.8, burn=75/h:
        remaining = 1h → predicted = 600 + 75 = 675 → ratio=0.675 → 0.4556 → floored 0.5

        For ratio=0.75 without floor: predicted=750. used=600, burn=150, remaining=1h:
        600 + 150 = 750. ratio=0.75. 0.75^2=0.5625. elapsed=0.8 (remaining=1h).
        """
        pf = pace_factor(
            quota_used=600,
            quota_total=1000,
            time_elapsed_pct=0.8,
            burn_rate=150,
            window_duration_hours=5.0,
        )
        assert pf < 1.0
        assert pf == pytest.approx(0.5625, rel=1e-4)

    def test_slight_underutilizing(self):
        """pace_ratio = 0.9 → pace_factor = 0.81.

        window=5h, used=600, total=1000, elapsed=0.5, burn=120/h:
        remaining = 2.5h → predicted = 600 + 300 = 900 → ratio=0.9
        """
        pf = pace_factor(
            quota_used=600,
            quota_total=1000,
            time_elapsed_pct=0.5,
            burn_rate=120,
            window_duration_hours=5.0,
        )
        assert pf == pytest.approx(0.81, rel=1e-4)


# ── Will exhaust ────────────────────────────────────────────────────────────


class TestWillExhaust:
    def test_will_exhaust_increases_price(self):
        """pace_ratio = 1.5 → pace_factor = 2.25 (> 1.0, slow traffic).

        window=5h, used=800, total=1000, elapsed=0.5, burn=700/h:
        remaining = 2.5h → predicted = 800 + 1750 = 2550 → ratio=2.55 → 6.5 → capped 3.0

        For exact ratio=1.5: predicted=1500. used=800, burn=280, remaining=2.5h:
        800 + 280*2.5 = 1500. ratio=1.5. elapsed=0.5.
        """
        pf = pace_factor(
            quota_used=800,
            quota_total=1000,
            time_elapsed_pct=0.5,
            burn_rate=280,
            window_duration_hours=5.0,
        )
        assert pf > 1.0
        assert pf == pytest.approx(2.25, rel=1e-4)

    def test_will_exhaust_early_increases_more(self):
        """pace_ratio = 1.2 → pace_factor = 1.44.

        window=5h, used=700, total=1000, elapsed=0.5, burn=200/h:
        remaining = 2.5h → predicted = 700 + 500 = 1200 → ratio=1.2
        """
        pf = pace_factor(
            quota_used=700,
            quota_total=1000,
            time_elapsed_pct=0.5,
            burn_rate=200,
            window_duration_hours=5.0,
        )
        assert pf > 1.0
        assert pf == pytest.approx(1.44, rel=1e-4)


# ── Clamping ────────────────────────────────────────────────────────────────


class TestClamping:
    def test_way_under_floored_at_half(self):
        """pace_ratio very low → factor very low → floored at 0.5.

        window=5h, used=10, total=1000, elapsed=0.5, burn=10/h:
        remaining = 2.5h → predicted = 10 + 25 = 35 → ratio=0.035 → 0.0012 → floored 0.5
        """
        pf = pace_factor(
            quota_used=10,
            quota_total=1000,
            time_elapsed_pct=0.5,
            burn_rate=10,
            window_duration_hours=5.0,
        )
        assert pf == 0.5

    def test_way_over_capped_at_three(self):
        """pace_ratio = 2.0 → 4.0 → capped at 3.0.

        window=5h, used=500, total=1000, elapsed=0.2, burn=1000/h:
        remaining = 4h → predicted = 500 + 4000 = 4500 → ratio=4.5 → 20.25 → capped 3.0

        For exact ratio=2.0: predicted=2000. used=500, burn=375, remaining=4h:
        500 + 375*4 = 2000. ratio=2.0. factor=4.0 → capped 3.0. elapsed=0.2.
        """
        pf = pace_factor(
            quota_used=500,
            quota_total=1000,
            time_elapsed_pct=0.2,
            burn_rate=375,
            window_duration_hours=5.0,
        )
        assert pf == 3.0

    def test_extreme_over_capped_at_three(self):
        """pace_ratio = 10.0 → 100.0 → still capped at 3.0."""
        pf = pace_factor(
            quota_used=0,
            quota_total=100,
            time_elapsed_pct=0.5,
            burn_rate=2000,
            window_duration_hours=5.0,
        )
        # remaining = 2.5h → predicted = 0 + 5000 = 5000 → ratio=50 → capped 3.0
        assert pf == 3.0

    def test_extreme_under_floored_at_half(self):
        """pace_ratio ≈ 0 → 0 → floored at 0.5."""
        pf = pace_factor(
            quota_used=0,
            quota_total=1000,
            time_elapsed_pct=0.5,
            burn_rate=0.01,
            window_duration_hours=5.0,
        )
        # remaining = 2.5h → predicted = 0 + 0.025 = 0.025 → ratio=0.000025 → floored 0.5
        assert pf == 0.5


# ── Edge cases ──────────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_zero_burn_rate_returns_one(self):
        """No burn data → no adjustment → 1.0."""
        pf = pace_factor(
            quota_used=500,
            quota_total=1000,
            time_elapsed_pct=0.5,
            burn_rate=0.0,
            window_duration_hours=5.0,
        )
        assert pf == 1.0

    def test_negative_burn_rate_returns_one(self):
        """Negative burn rate → invalid data → 1.0 (no adjustment)."""
        pf = pace_factor(
            quota_used=500,
            quota_total=1000,
            time_elapsed_pct=0.5,
            burn_rate=-100,
            window_duration_hours=5.0,
        )
        assert pf == 1.0

    def test_time_elapsed_zero_returns_one(self):
        """Window just reset → no pace data yet → 1.0."""
        pf = pace_factor(
            quota_used=0,
            quota_total=1000,
            time_elapsed_pct=0.0,
            burn_rate=200,
            window_duration_hours=5.0,
        )
        assert pf == 1.0

    def test_full_quota_used_with_time_remaining_high_multiplier(self):
        """quota_used = total but time remains → high multiplier.

        window=5h, used=1000, total=1000, elapsed=0.8, burn=100/h:
        remaining = 1h → predicted = 1000 + 100 = 1100 → ratio=1.1 → 1.21
        """
        pf = pace_factor(
            quota_used=1000,
            quota_total=1000,
            time_elapsed_pct=0.8,
            burn_rate=100,
            window_duration_hours=5.0,
        )
        assert pf > 1.0
        assert pf <= 3.0
        assert pf == pytest.approx(1.21, rel=1e-4)

    def test_zero_quota_total_returns_one(self):
        """quota_total = 0 → avoid division by zero → 1.0."""
        pf = pace_factor(
            quota_used=0,
            quota_total=0,
            time_elapsed_pct=0.5,
            burn_rate=100,
            window_duration_hours=5.0,
        )
        assert pf == 1.0

    def test_time_elapsed_full_window(self):
        """time_elapsed_pct = 1.0 → window about to reset.

        remaining = 0 → predicted = quota_used.
        ratio = quota_used / total.
        window=5h, used=800, total=1000 → ratio=0.8 → 0.64
        (0.5 ratio → 0.25 → floored at 0.5, so use 0.8 ratio instead)
        """
        pf = pace_factor(
            quota_used=800,
            quota_total=1000,
            time_elapsed_pct=1.0,
            burn_rate=200,
            window_duration_hours=5.0,
        )
        assert pf == pytest.approx(0.64, rel=1e-4)

    def test_time_elapsed_above_one_clamped(self):
        """time_elapsed_pct > 1.0 → treat as 1.0 (no negative time remaining)."""
        pf = pace_factor(
            quota_used=800,
            quota_total=1000,
            time_elapsed_pct=1.5,
            burn_rate=200,
            window_duration_hours=5.0,
        )
        # Should behave as if time_elapsed=1.0 → ratio=0.8 → 0.64
        assert pf == pytest.approx(0.64, rel=1e-4)

    def test_time_elapsed_negative_returns_one(self):
        """time_elapsed_pct < 0 → treated as 0.0 → returns 1.0."""
        pf = pace_factor(
            quota_used=0,
            quota_total=1000,
            time_elapsed_pct=-0.5,
            burn_rate=200,
            window_duration_hours=5.0,
        )
        assert pf == 1.0

    def test_weekly_window(self):
        """Test with 168h (weekly) window.

        window=168h, used=1500000, total=2000000, elapsed=0.5, burn=6000/h:
        remaining = 84h → predicted = 1500000 + 504000 = 2004000 → ratio=1.002 → 1.004
        """
        pf = pace_factor(
            quota_used=1500000,
            quota_total=2000000,
            time_elapsed_pct=0.5,
            burn_rate=6000,
            window_duration_hours=168.0,
        )
        assert pf == pytest.approx(1.004, rel=1e-3)


# ── Multi-window composition ────────────────────────────────────────────────


class TestMultiWindow:
    def test_worst_case_governs(self):
        """When computing pace for multiple windows, the MAX pace_factor
        should be used (worst case governs — never run out priority).

        5h window: used=800, total=1000, elapsed=0.8, burn=200/h, window=5h
          remaining=1h → 800+200=1000 → ratio=1.0 → factor=1.0

        weekly window: used=1200000, total=2000000, elapsed=0.5, burn=7000/h, window=168h
          remaining=84h → 1200000+588000=1788000 → ratio=0.894 → 0.799

        Wait, that gives the 5h window as the max. Let me construct a case
        where the weekly window is worse:

        5h: used=800, total=1000, elapsed=0.5, burn=100/h, window=5h
          remaining=2.5h → 800+250=1050 → ratio=1.05 → 1.1025

        weekly: used=1500000, total=2000000, elapsed=0.5, burn=10000/h, window=168h
          remaining=84h → 1500000+840000=2340000 → ratio=1.17 → 1.3689

        MAX = 1.3689 (weekly governs).
        """
        pf = pace_factor_multi(
            windows=[
                (800, 1000, 0.5, 100, 5.0),        # 5h: ratio=1.05 → 1.1025
                (1500000, 2000000, 0.5, 10000, 168.0),  # weekly: ratio=1.17 → 1.3689
            ]
        )
        assert pf == pytest.approx(1.3689, rel=1e-3)
        assert pf > 1.0  # worst case (weekly) governs

    def test_multi_window_underutilizing_both(self):
        """Both windows underutilizing → MAX is the less-underutilizing one.

        window A: used=100, total=1000, elapsed=0.5, burn=10/h, window=5h
          remaining=2.5h → 100+25=125 → ratio=0.125 → 0.0156 → floored 0.5

        window B: used=300, total=1000, elapsed=0.5, burn=100/h, window=5h
          remaining=2.5h → 300+250=550 → ratio=0.55 → 0.3025

        MAX = 0.3025
        """
        pf = pace_factor_multi(
            windows=[
                (100, 1000, 0.5, 10, 5.0),    # floored at 0.5
                (300, 1000, 0.5, 100, 5.0),   # 0.3025
            ]
        )
        assert pf == pytest.approx(0.5, rel=1e-4)

    def test_multi_window_empty_returns_one(self):
        """No windows → 1.0 (no data, no adjustment)."""
        pf = pace_factor_multi(windows=[])
        assert pf == 1.0

    def test_multi_window_single_window(self):
        """Single window → same as pace_factor directly."""
        pf_multi = pace_factor_multi(
            windows=[(800, 1000, 0.8, 200, 5.0)]
        )
        pf_single = pace_factor(800, 1000, 0.8, 200, 5.0)
        assert pf_multi == pytest.approx(pf_single)