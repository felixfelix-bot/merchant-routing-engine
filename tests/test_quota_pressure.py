"""Tests for quota_pressure_factor — the continuous replacement for extra_usage_multiplier.

Validates Felix's price-based routing directive: as Ollama quota depletes,
price rises smoothly until z.ai becomes cheaper, triggering automatic reroute.
No thresholds, no regime strings.
"""
from __future__ import annotations

import math

import pytest

from src.pricing_engine import (
    EXTRA_USAGE_MULTIPLIER,
    compute_effective_price,
    quota_pressure_factor,
)


# ── quota_pressure_factor() ──────────────────────────────────────────────────


class TestQuotaPressureFactor:
    def test_low_usage_no_penalty(self):
        """Below onset (75%), pressure = 1.0 (Ollama cheapest)."""
        assert quota_pressure_factor(0.0) == 1.0
        assert quota_pressure_factor(0.50) == 1.0
        assert quota_pressure_factor(0.75) == 1.0  # at onset boundary

    def test_at_onset_is_one(self):
        """At exactly the onset, pressure = 1.0."""
        assert quota_pressure_factor(0.75) == 1.0

    def test_at_full_usage_equals_asymptote(self):
        """At 100% usage, pressure = asymptote (≈4.17, maps to extra-usage rate)."""
        p = quota_pressure_factor(1.0)
        assert p == pytest.approx(EXTRA_USAGE_MULTIPLIER, abs=0.01)

    def test_monotonic_increasing(self):
        """Pressure is monotonically non-decreasing in the ramp range [onset, 0.99].

        The exponential ramp 1/(1-t)^k diverges toward infinity as usage → 1.0,
        but at u >= 1.0 the factor is CAPPED at the asymptote (≈4.17) so that
        exclusive models (kimi-k3) remain reachable at their true cost. This cap
        means the factor is NOT monotonic across the full 0→1.5 range (it drops
        from 625x at 0.99 to 4.17x at 1.0). Monotonicity holds within the ramp
        range only; the over-quota behaviour is verified separately.
        """
        onset = 0.75
        prev = 0.0
        # Test monotonic increase strictly within the ramp range [onset, 0.99].
        for i in range(int(onset * 100), 100):  # 0.75 → 0.99
            u = i / 100.0
            p = quota_pressure_factor(u)
            assert p >= prev, f"non-monotonic at u={u}: {p} < {prev}"
            prev = p
        # Sanity: factor at 0.99 is huge (asymptotic divergence).
        assert quota_pressure_factor(0.99) > 100.0

    def test_over_quota_caps_at_asymptote(self):
        """At u >= 1.0 the factor is CAPPED at the asymptote (≈4.17), NOT
        monotonic. The factor at 0.99 (625x) exceeds the asymptote, so the cap
        at u >= 1.0 represents a deliberate drop to the extra-usage rate."""
        # Factor at 0.99 (625x) is far above the asymptote cap.
        assert quota_pressure_factor(0.99) > EXTRA_USAGE_MULTIPLIER
        # At and above 100%, factor is flat at the asymptote.
        assert quota_pressure_factor(1.0) == pytest.approx(EXTRA_USAGE_MULTIPLIER, abs=0.01)
        assert quota_pressure_factor(1.10) == pytest.approx(EXTRA_USAGE_MULTIPLIER, abs=0.01)
        assert quota_pressure_factor(1.25) == pytest.approx(EXTRA_USAGE_MULTIPLIER, abs=0.01)
        assert quota_pressure_factor(1.50) == pytest.approx(EXTRA_USAGE_MULTIPLIER, abs=0.01)

    def test_over_quota_flat_at_asymptote(self):
        """Past 100%, pressure is FLAT at the asymptote (≈4.17x).

        The exponential ramp diverges to infinity as u → 1.0, but at and above
        100% usage the factor is deliberately capped at the asymptote (the actual
        extra-usage rate multiplier) so exclusive models like kimi-k3 stay
        reachable at their true cost instead of being priced out at +∞.
        """
        p100 = quota_pressure_factor(1.0)
        p110 = quota_pressure_factor(1.10)
        p125 = quota_pressure_factor(1.25)
        # All flat at the asymptote — no further ramping past 100%.
        assert p100 == pytest.approx(EXTRA_USAGE_MULTIPLIER, abs=0.01)
        assert p110 == pytest.approx(p100, abs=0.001)
        assert p125 == pytest.approx(p100, abs=0.001)

    def test_weekly_takes_max(self):
        """When weekly usage is higher, it governs (worst case)."""
        # session 0.5 (no pressure), weekly 0.90 (pressure active)
        p = quota_pressure_factor(0.50, 0.90)
        p_weekly_only = quota_pressure_factor(0.0, 0.90)
        assert p == pytest.approx(p_weekly_only)

    def test_session_takes_max(self):
        """When session usage is higher, it governs."""
        p = quota_pressure_factor(0.95, 0.50)
        p_session_only = quota_pressure_factor(0.95, 0.0)
        assert p == pytest.approx(p_session_only)

    def test_custom_onset(self):
        """Custom onset shifts where pressure begins."""
        # onset=0.50 → pressure starts at 50%
        assert quota_pressure_factor(0.50, onset=0.50) == 1.0
        assert quota_pressure_factor(0.75, onset=0.50) > 1.0

    def test_custom_asymptote(self):
        """Custom asymptote changes the max multiplier."""
        p = quota_pressure_factor(1.0, asymptote=8.0)
        assert p == pytest.approx(8.0)

    def test_degenerate_onset_at_one(self):
        """onset=1.0 → span is zero → returns asymptote for any u>1."""
        p = quota_pressure_factor(0.50, onset=1.0)
        assert p == pytest.approx(EXTRA_USAGE_MULTIPLIER)

    def test_exponential_shape(self):
        """Verify the exponential ramp: at midpoint between onset and 1.0,
        pressure is 1 / (1-0.5)^2 = 4.0.

        The ramp is 1/(1-t)^k with k=2.0. At the midpoint of [0.75, 1.0] = 0.875,
        the normalised position t = (0.875-0.75)/0.25 = 0.5, so the factor is
        1/(0.5)^2 = 4.0 — far steeper than the old quadratic 1+(A-1)*0.25≈1.79.
        """
        # Midpoint of [0.75, 1.0] = 0.875
        u_mid = (0.75 + 1.0) / 2  # 0.875
        p_mid = quota_pressure_factor(u_mid)
        # Exponential: 1 / (1 - 0.5)^2 = 1 / 0.25 = 4.0
        expected = 4.0
        assert p_mid == pytest.approx(expected, abs=0.001)
        # Verify the exponential diverges faster than the old quadratic (1.79).
        quadratic_expected = 1.0 + (EXTRA_USAGE_MULTIPLIER - 1.0) * 0.25
        assert p_mid > quadratic_expected  # exponential > quadratic at midpoint


# ── Crossover verification (the key behaviour Felix wants) ───────────────────


class TestCrossoverPoints:
    """Verify that Ollama's price crosses z.ai's at the right usage levels."""

    OLLAMA_BASE = 0.024  # $/M
    ZAI_FRIEND = 0.029   # $/M (off-peak)
    ZAI_PEAK = 0.029 * 3.0  # $/M (peak, 3x)

    def test_ollama_cheaper_below_75pct(self):
        """Below 75% usage, Ollama is cheaper than z.ai off-peak."""
        for u in [0.0, 0.25, 0.50, 0.75]:
            ollama = self.OLLAMA_BASE * quota_pressure_factor(u)
            assert ollama < self.ZAI_FRIEND, \
                f"Ollama ({ollama:.4f}) should be < z.ai ({self.ZAI_FRIEND}) at u={u}"

    def test_crossover_offpeak_around_77pct(self):
        """Around 77% usage, Ollama crosses z.ai off-peak price.

        The exponential ramp diverges faster than the old quadratic, so the
        crossover with z.ai off-peak ($0.029) happens earlier:
            0.024 * 1/(1-t)^2 = 0.029  →  (1-t)^2 = 0.828  →  t = 0.09
            → u = 0.75 + 0.09*0.25 ≈ 0.7725
        """
        # Just below crossover (~77%): Ollama cheaper
        assert self.OLLAMA_BASE * quota_pressure_factor(0.76) < self.ZAI_FRIEND
        # Just above crossover (~77%): Ollama more expensive
        assert self.OLLAMA_BASE * quota_pressure_factor(0.78) > self.ZAI_FRIEND

    def test_crossover_peak_around_87pct(self):
        """Around 87% usage, Ollama crosses z.ai peak price.

        With the exponential ramp the crossover with z.ai peak ($0.087) happens
        much earlier than the old quadratic's ~98%:
            0.024 * 1/(1-t)^2 = 0.087  →  (1-t)^2 = 0.276  →  t = 0.475
            → u = 0.75 + 0.475*0.25 ≈ 0.869
        """
        # Below crossover (~87%): Ollama still cheaper than peak z.ai
        assert self.OLLAMA_BASE * quota_pressure_factor(0.85) < self.ZAI_PEAK
        # Above crossover (~87%): Ollama more expensive than peak z.ai
        assert self.OLLAMA_BASE * quota_pressure_factor(0.90) > self.ZAI_PEAK

    def test_scarcity_cancels_in_comparison(self):
        """Scarcity applies to both providers equally, so it doesn't change
        the crossover point."""
        u = 0.90
        scarcity = 1.0 + max(0.0, (90.0 - 50.0) / 50.0)  # 1.8
        pressure = quota_pressure_factor(u)

        ollama_with_scarcity = self.OLLAMA_BASE * scarcity * pressure
        zai_with_scarcity = self.ZAI_FRIEND * scarcity

        # Same ratio as without scarcity
        ollama_without = self.OLLAMA_BASE * pressure
        zai_without = self.ZAI_FRIEND

        ratio_with = ollama_with_scarcity / zai_with_scarcity
        ratio_without = ollama_without / zai_without
        assert ratio_with == pytest.approx(ratio_without)


# ── compute_effective_price integration ─────────────────────────────────────


class TestComputeEffectivePriceWithPressure:
    def test_low_usage_unchanged(self):
        """At low usage, quota_pressure=1.0 → no change."""
        price = compute_effective_price(
            0.024, "ollama_cloud", 50, True,
            hour_utc=12, quota_pressure=1.0,
        )
        assert price == pytest.approx(0.024)

    def test_high_usage_increases_price(self):
        """At 90% usage, pressure > 1.0 → price increases."""
        pressure = quota_pressure_factor(0.90)
        price = compute_effective_price(
            0.024, "ollama_cloud", 50, True,
            hour_utc=12, quota_pressure=pressure,
        )
        expected = 0.024 * pressure
        assert price == pytest.approx(expected, abs=0.001)
        assert price > 0.024  # increased

    def test_pressure_stacks_with_other_multipliers(self):
        """Pressure stacks with scarcity, health, pace."""
        pressure = quota_pressure_factor(0.90)
        price = compute_effective_price(
            0.024, "zai", 90, True,  # z.ai for peak testing
            hour_utc=8,  # peak
            pace_mult=1.5,
            quota_pressure=pressure,
        )
        scarcity = 1.0 + max(0.0, (90.0 - 50.0) / 50.0)  # 1.8
        expected = 0.024 * 3.0 * scarcity * 1.0 * 1.5 * pressure
        assert price == pytest.approx(expected, abs=0.001)

    def test_default_pressure_is_one(self):
        """When quota_pressure not provided, default 1.0 (backward compat)."""
        price_old = compute_effective_price(
            0.024, "ollama_cloud", 50, True, hour_utc=12,
        )
        price_default = compute_effective_price(
            0.024, "ollama_cloud", 50, True,
            hour_utc=12, quota_pressure=1.0,
        )
        assert price_old == pytest.approx(price_default)

    def test_pressure_overrides_regime(self):
        """When quota_pressure is provided (≠1.0), it takes precedence
        over the old regime-based step function."""
        pressure = quota_pressure_factor(0.90)
        price_pressure = compute_effective_price(
            0.024, "ollama_cloud", 50, True,
            hour_utc=12, quota_pressure=pressure,
        )
        price_regime_included = compute_effective_price(
            0.024, "ollama_cloud", 50, True,
            hour_utc=12, extra_usage_regime="included",
        )
        assert price_pressure > price_regime_included  # pressure applied
