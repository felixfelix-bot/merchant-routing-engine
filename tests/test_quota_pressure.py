"""Tests for quota_pressure_factor — the continuous replacement for extra_usage_multiplier.

Validates Felix's price-based routing directive (RP-PRICING / RP-EXP): as Ollama
quota depletes, price rises smoothly until z.ai becomes cheaper, triggering
automatic reroute. No thresholds, no regime strings.

RP-EXP curve:  1 + K * t / (1 - t)   for onset < u < 1.0,  +inf at u >= 1.0
               where K = EXTRA_USAGE_MULTIPLIER - 1.0 (~3.17), onset = 0.70.
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
    # ── RP-EXP gate tests (explicit requirements from the task spec) ──

    def test_gate_multiplier_at_99pct_above_10x(self):
        """GATE: multiplier at usage=0.99 is well above 10x (steep divergence)."""
        assert quota_pressure_factor(0.99) > 10.0

    def test_gate_multiplier_at_70pct_is_one(self):
        """GATE: multiplier at usage=0.70 (the onset) is exactly 1.0."""
        assert quota_pressure_factor(0.70) == 1.0

    def test_gate_multiplier_at_50pct_is_one(self):
        """GATE: multiplier at usage=0.50 (below onset) is exactly 1.0."""
        assert quota_pressure_factor(0.50) == 1.0

    # ── Shape & boundary tests ──

    def test_low_usage_no_penalty(self):
        """Below onset (70%), pressure = 1.0 (Ollama cheapest)."""
        assert quota_pressure_factor(0.0) == 1.0
        assert quota_pressure_factor(0.50) == 1.0
        assert quota_pressure_factor(0.70) == 1.0  # at onset boundary

    def test_at_onset_is_one(self):
        """At exactly the onset, pressure = 1.0."""
        assert quota_pressure_factor(0.70) == 1.0

    def test_at_full_usage_is_infinity(self):
        """At 100% usage, pressure = +inf (provider unreachable — the router
        ALWAYS finds a cheaper alternative first)."""
        assert quota_pressure_factor(1.0) == math.inf

    def test_monotonic_increasing(self):
        """Pressure is monotonically increasing in the ramp range [onset, 1.0).

        The RP-EXP curve 1 + K*t/(1-t) diverges toward +inf as usage -> 1.0,
        where it is clipped to +inf (verified separately). Monotonicity holds
        across the whole 0 -> 1.0 domain; at u >= 1.0 the value is flat at +inf.
        """
        prev = 0.0
        for i in range(0, 100):  # 0.00 -> 0.99
            u = i / 100.0
            p = quota_pressure_factor(u)
            assert p >= prev, f"non-monotonic at u={u}: {p} < {prev}"
            prev = p
        # At 100% the curve reaches +inf (>= the last finite value at 0.99).
        assert quota_pressure_factor(1.0) == math.inf
        assert quota_pressure_factor(1.0) >= prev

    def test_over_quota_is_infinity(self):
        """At u >= 1.0 the factor is +inf (unreachable), NOT a finite cap.

        The curve's true asymptote is +inf at 100%; the optimizer therefore
        always reroutes non-exclusive models to a cheaper alternative. Exclusive
        models (kimi-k3) are protected by live_router's short-circuit, not by a
        price cap here.
        """
        assert quota_pressure_factor(0.99) > 10.0  # large but finite below 100%
        assert quota_pressure_factor(1.0) == math.inf
        assert quota_pressure_factor(1.10) == math.inf
        assert quota_pressure_factor(1.25) == math.inf
        assert quota_pressure_factor(1.50) == math.inf

    def test_over_quota_flat_at_infinity(self):
        """Past 100%, pressure is flat at +inf (no further finite ramping)."""
        p100 = quota_pressure_factor(1.0)
        p110 = quota_pressure_factor(1.10)
        p125 = quota_pressure_factor(1.25)
        assert p100 == math.inf
        assert p110 == math.inf
        assert p125 == math.inf

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
        # onset=0.50 -> pressure starts at 50%
        assert quota_pressure_factor(0.50, onset=0.50) == 1.0
        assert quota_pressure_factor(0.75, onset=0.50) > 1.0

    def test_custom_asymptote_at_midpoint(self):
        """The factor passes through `asymptote` at the midpoint of the ramp.

        With the RP-EXP curve 1 + K*t/(1-t) and K = asymptote - 1, at the
        midpoint (t=0.5) the multiplier equals 1 + K = asymptote exactly. This
        is the defining property that ties the curve's steepness to the
        extra-rate/base-rate ratio.
        """
        # Default onset 0.70 -> midpoint = 0.70 + 0.5*0.30 = 0.85
        p = quota_pressure_factor(0.85, asymptote=8.0)
        assert p == pytest.approx(8.0)

    def test_default_asymptote_at_midpoint(self):
        """With the default asymptote, the midpoint multiplier equals
        EXTRA_USAGE_MULTIPLIER (the full extra-usage rate)."""
        p = quota_pressure_factor(0.85)
        assert p == pytest.approx(EXTRA_USAGE_MULTIPLIER, abs=0.001)

    def test_degenerate_onset_at_one(self):
        """onset=1.0 -> below-onset usage returns 1.0 (no ramp zone)."""
        # With onset at 100%, there is no ramp interval; usage below onset has
        # no penalty, and usage >= 1.0 is +inf (asymptote).
        assert quota_pressure_factor(0.50, onset=1.0) == 1.0
        assert quota_pressure_factor(1.0, onset=1.0) == math.inf

    def test_rational_curve_shape(self):
        """Verify the RP-EXP rational curve: at the midpoint between onset and
        1.0, the multiplier equals EXTRA_USAGE_MULTIPLIER (= K + 1).

        At the midpoint of [0.70, 1.0] = 0.85, the normalised position
        t = (0.85-0.70)/0.30 = 0.5, so 1 + K*0.5/0.5 = 1 + K = 4.17x — the full
        extra-usage rate. Past the midpoint the curve diverges toward +inf.
        """
        u_mid = 0.70 + 0.5 * (1.0 - 0.70)  # 0.85
        p_mid = quota_pressure_factor(u_mid)
        expected = EXTRA_USAGE_MULTIPLIER  # 1 + K
        assert p_mid == pytest.approx(expected, abs=0.001)
        # Past the midpoint, the multiplier exceeds the extra-usage rate.
        assert quota_pressure_factor(0.90) > EXTRA_USAGE_MULTIPLIER


# ── Crossover verification (the key behaviour Felix wants) ───────────────────


class TestCrossoverPoints:
    """Verify that Ollama's price crosses z.ai's at the right usage levels.

    With the RP-EXP curve (onset=0.70, K~3.17) the crossovers are EARLIER than
    the old quadratic ramp because the new curve is steeper near the onset:
      - off-peak z.ai ($0.029): crossover ~72% usage
      - peak z.ai ($0.087):     crossover ~84% usage
    """

    OLLAMA_BASE = 0.024  # $/M
    ZAI_FRIEND = 0.029   # $/M (off-peak)
    ZAI_PEAK = 0.029 * 3.0  # $/M (peak, 3x)

    def test_ollama_cheaper_below_70pct(self):
        """At/below the 70% onset, Ollama is cheaper than z.ai off-peak
        (pressure = 1.0, Ollama base $0.024 < friend $0.029)."""
        for u in [0.0, 0.25, 0.50, 0.70]:
            ollama = self.OLLAMA_BASE * quota_pressure_factor(u)
            assert ollama < self.ZAI_FRIEND, \
                f"Ollama ({ollama:.4f}) should be < z.ai ({self.ZAI_FRIEND}) at u={u}"

    def test_crossover_offpeak_around_72pct(self):
        """Around 72% usage, Ollama crosses z.ai off-peak price.

        0.024 * (1 + K*t/(1-t)) = 0.029  ->  t ~ 0.062  ->  u ~ 0.719.
        """
        # Just below crossover (~72%): Ollama cheaper
        assert self.OLLAMA_BASE * quota_pressure_factor(0.71) < self.ZAI_FRIEND
        # Just above crossover (~72%): Ollama more expensive
        assert self.OLLAMA_BASE * quota_pressure_factor(0.73) > self.ZAI_FRIEND

    def test_crossover_peak_around_84pct(self):
        """Around 84% usage, Ollama crosses z.ai peak price.

        0.024 * (1 + K*t/(1-t)) = 0.087  ->  t ~ 0.453  ->  u ~ 0.836.
        """
        # Below crossover (~84%): Ollama still cheaper than peak z.ai
        assert self.OLLAMA_BASE * quota_pressure_factor(0.83) < self.ZAI_PEAK
        # Above crossover (~84%): Ollama more expensive than peak z.ai
        assert self.OLLAMA_BASE * quota_pressure_factor(0.85) > self.ZAI_PEAK

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
        """At low usage, quota_pressure=1.0 -> no change."""
        price = compute_effective_price(
            0.024, "ollama_cloud", 50, True,
            hour_utc=12, quota_pressure=1.0,
        )
        assert price == pytest.approx(0.024)

    def test_high_usage_increases_price(self):
        """At 90% usage, pressure > 1.0 -> price increases."""
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
        """When quota_pressure is provided (!=1.0), it takes precedence
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
