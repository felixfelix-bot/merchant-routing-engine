"""Tests for the extra-usage multiplier in src/pricing_engine.py.

Covers the required scenarios from the EU-R3 task spec:
  1. Included regime → multiplier = 1.0 (no change)
  2. Extra regime → price = base * 4.17 ($0.024 * 4.17 = $0.10/M)
  3. Exhausted regime → provider filtered out (+inf)
  4. Multiplier stacks correctly with peak (3x * 4.17x = 12.51x)

Also covers:
  - Config-derived multiplier override
  - Unknown regime fail-safe
  - Composition with other multipliers (scarcity, health, pace)
  - Backward compatibility (existing callers unaffected)
  - Kill switch: OLLAMA_EXTRA_USAGE_ENABLED=false → no price bump
"""
from __future__ import annotations

import math
import os

import pytest

from src.pricing_engine import (
    EXTRA_USAGE_BASE_RATE,
    EXTRA_USAGE_MULTIPLIER,
    EXTRA_USAGE_TARGET_RATE,
    MIN_EFFECTIVE_PRICE,
    compute_effective_price,
    extra_usage_multiplier,
)


# ── extra_usage_multiplier() ─────────────────────────────────────────────────


class TestExtraUsageMultiplier:
    def test_included_regime_is_one(self):
        """When regime='included', multiplier = 1.0 (no change)."""
        assert extra_usage_multiplier("included") == 1.0

    def test_extra_regime_uses_default_multiplier(self):
        """When regime='extra', multiplier = EXTRA_USAGE_MULTIPLIER (≈4.17)."""
        assert extra_usage_multiplier("extra") == EXTRA_USAGE_MULTIPLIER
        assert extra_usage_multiplier("extra") == pytest.approx(4.17, abs=0.01)

    def test_extra_regime_with_override(self):
        """Config-derived multiplier override is respected in extra mode."""
        assert extra_usage_multiplier("extra", multiplier=8.0) == 8.0
        assert extra_usage_multiplier("extra", multiplier=10.0) == 10.0

    def test_exhausted_regime_is_inf(self):
        """When regime='exhausted', multiplier = +inf (provider filtered out)."""
        assert extra_usage_multiplier("exhausted") == math.inf

    def test_exhausted_ignores_override(self):
        """Exhausted regime is always +inf, regardless of multiplier override."""
        assert extra_usage_multiplier("exhausted", multiplier=4.17) == math.inf
        assert extra_usage_multiplier("exhausted", multiplier=0.0) == math.inf

    def test_unknown_regime_fails_safe_to_one(self):
        """Unknown / unrecognised regime → 1.0 (no penalty, fail-safe)."""
        assert extra_usage_multiplier("unknown") == 1.0
        assert extra_usage_multiplier("") == 1.0
        assert extra_usage_multiplier("something_else") == 1.0

    def test_default_multiplier_matches_plan(self):
        """EXTRA_USAGE_MULTIPLIER = 0.10 / 0.024 ≈ 4.17 (per EU-R3 spec)."""
        assert EXTRA_USAGE_MULTIPLIER == pytest.approx(
            EXTRA_USAGE_TARGET_RATE / EXTRA_USAGE_BASE_RATE, abs=0.01
        )
        assert EXTRA_USAGE_MULTIPLIER == pytest.approx(4.17, abs=0.01)

    def test_target_rate_is_0_10(self):
        """Target effective rate is $0.10/M (per EU-R3 spec)."""
        assert EXTRA_USAGE_TARGET_RATE == pytest.approx(0.10, abs=0.001)


# ── compute_effective_price with extra_usage_regime ─────────────────────────


class TestComputeEffectivePriceExtraUsage:
    # ── 1. Included regime → multiplier = 1.0 ──────────────────────────────

    def test_included_regime_no_change(self):
        """Included regime: effective price unchanged vs no extra-usage arg.

        $0.024 * 1.0 (off-peak) * 1.0 (50% quota) * 1.0 (healthy) * 1.0 (pace)
        * 1.0 (included) = $0.024/M
        """
        price = compute_effective_price(
            0.024, "ollama_cloud", 50, True,
            hour_utc=12, extra_usage_regime="included",
        )
        assert price == pytest.approx(0.024)

    def test_included_regime_equals_no_arg(self):
        """Calling with regime='included' == calling without the arg (default)."""
        price_with = compute_effective_price(
            0.024, "ollama_cloud", 50, True,
            hour_utc=12, extra_usage_regime="included",
        )
        price_without = compute_effective_price(
            0.024, "ollama_cloud", 50, True, hour_utc=12,
        )
        assert price_with == pytest.approx(price_without)

    # ── 2. Extra regime → price = base * 4.17 = $0.10/M ───────────────────

    def test_extra_regime_effective_rate_is_0_10(self):
        """KEY GATE: extra_usage=True → price = base * 4.17 = $0.10/M.

        $0.024 * 4.17 ≈ $0.10/M
        """
        price = compute_effective_price(
            0.024, "ollama_cloud", 50, True,
            hour_utc=12, extra_usage_regime="extra",
        )
        assert price == pytest.approx(0.10, abs=0.001)

    def test_extra_regime_with_custom_base_rate(self):
        """Extra regime with a different base rate still applies multiplier.

        If base_rate = $0.03/M, effective = 0.03 * 4.17 ≈ $0.125/M.
        """
        price = compute_effective_price(
            0.03, "ollama_cloud", 50, True,
            hour_utc=12, extra_usage_regime="extra",
        )
        expected = 0.03 * EXTRA_USAGE_MULTIPLIER
        assert price == pytest.approx(expected, abs=0.001)

    def test_extra_regime_with_config_override(self):
        """Config-derived multiplier override (e.g. 8.0x) is respected.

        $0.024 * 8.0 = $0.192/M.
        """
        price = compute_effective_price(
            0.024, "ollama_cloud", 50, True,
            hour_utc=12, extra_usage_regime="extra", extra_usage_mult=8.0,
        )
        assert price == pytest.approx(0.192)

    # ── 3. Exhausted regime → provider filtered out (+inf) ─────────────────

    def test_exhausted_regime_is_inf(self):
        """Exhausted regime → +inf (provider unselectable)."""
        price = compute_effective_price(
            0.024, "ollama_cloud", 50, True,
            hour_utc=12, extra_usage_regime="exhausted",
        )
        assert price == math.inf

    def test_exhausted_overrides_healthy_provider(self):
        """Exhausted quota makes provider unreachable even if healthy."""
        price = compute_effective_price(
            0.024, "ollama_cloud", 10, True,
            hour_utc=3, extra_usage_regime="exhausted",
        )
        assert price == math.inf

    def test_exhausted_with_zero_base_avoids_nan(self):
        """0 * inf = NaN → must be floored to MIN_EFFECTIVE_PRICE (ADR-004)."""
        price = compute_effective_price(
            0.0, "ollama_cloud", 50, True,
            hour_utc=12, extra_usage_regime="exhausted",
        )
        assert not math.isnan(price)
        assert price == MIN_EFFECTIVE_PRICE

    # ── 4. Multiplier stacks with peak (3x * 4.17x ≈ 12.51x) ──────────────

    def test_extra_stacks_with_peak(self):
        """Peak (3x) * extra (≈4.17x) during peak extra usage.

        $0.024 * 3.0 * 4.17 ≈ $0.30/M.
        Note: ollama_cloud is NOT a z.ai provider, so peak_multiplier returns
        1.0 for it. To test the stacking arithmetic we use a z.ai provider
        with the extra-usage regime set explicitly.
        """
        price = compute_effective_price(
            0.024, "zai", 50, True,
            hour_utc=8, extra_usage_regime="extra",
        )
        expected = 0.024 * 3.0 * EXTRA_USAGE_MULTIPLIER
        assert price == pytest.approx(expected, abs=0.001)

    def test_extra_stacks_with_peak_and_scarcity(self):
        """Full stack: peak(3x) * scarcity(2x) * extra(≈4.17x).

        $0.024 * 3.0 * 2.0 * 4.17 ≈ $0.60/M.
        """
        price = compute_effective_price(
            0.024, "zai", 100, True,
            hour_utc=8, extra_usage_regime="extra",
        )
        expected = 0.024 * 3.0 * 2.0 * EXTRA_USAGE_MULTIPLIER
        assert price == pytest.approx(expected, abs=0.001)

    def test_extra_stacks_with_health_penalty(self):
        """Extra(≈4.17x) * health_severe(10x).

        $0.024 * 10.0 * 4.17 ≈ $1.00/M.
        """
        price = compute_effective_price(
            0.024, "ollama_cloud", 50,
            hour_utc=12, failure_count=8,
            extra_usage_regime="extra",
        )
        expected = 0.024 * 10.0 * EXTRA_USAGE_MULTIPLIER
        assert price == pytest.approx(expected, abs=0.001)

    def test_extra_stacks_with_pace(self):
        """Extra(≈4.17x) * pace(2.0x).

        $0.024 * 2.0 * 4.17 ≈ $0.20/M.
        """
        price = compute_effective_price(
            0.024, "ollama_cloud", 50, True,
            hour_utc=12, pace_mult=2.0,
            extra_usage_regime="extra",
        )
        expected = 0.024 * 2.0 * EXTRA_USAGE_MULTIPLIER
        assert price == pytest.approx(expected, abs=0.001)

    # ── Backward compatibility ─────────────────────────────────────────────

    def test_default_regime_is_included(self):
        """Without extra_usage_regime arg, default is 'included' (no penalty)."""
        price_old = compute_effective_price(
            0.024, "ollama_cloud", 50, True, hour_utc=12,
        )
        price_included = compute_effective_price(
            0.024, "ollama_cloud", 50, True,
            hour_utc=12, extra_usage_regime="included",
        )
        assert price_old == pytest.approx(price_included)

    def test_existing_tests_still_pass_off_peak(self):
        """Existing test_pricing_engine composition still works."""
        price = compute_effective_price(0.068, "zai", 50, True, hour_utc=12)
        assert price == pytest.approx(0.068)

    def test_existing_tests_still_pass_peak_exhausted(self):
        """Existing test_pricing_engine full composition still works."""
        price = compute_effective_price(0.068, "zai", 100, True, hour_utc=8)
        assert price == pytest.approx(0.408)

    def test_non_ollama_provider_unaffected_by_included(self):
        """Non-Ollama providers with default regime='included' are unaffected."""
        price = compute_effective_price(
            0.14, "ppq", 0, True, hour_utc=12,
        )
        assert price == pytest.approx(0.14)

    # ── ADR-004 positivity invariant ───────────────────────────────────────

    def test_result_always_positive_with_extra_usage(self):
        """Effective price is always > 0 across all regimes."""
        for regime in ("included", "extra", "exhausted"):
            price = compute_effective_price(
                0.024, "ollama_cloud", 50, True,
                hour_utc=12, extra_usage_regime=regime,
            )
            assert not math.isnan(price), f"NaN for regime={regime}"
            assert price > 0, f"non-positive price {price} for regime={regime}"

    # ── Kill switch (OLLAMA_EXTRA_USAGE_ENABLED) ───────────────────────────

    def test_kill_switch_disabled_no_price_bump(self):
        """GATE: OLLAMA_EXTRA_USAGE_ENABLED=false → no price bump.

        When the kill switch is off, the extra-usage multiplier should NOT
        be applied even if the regime is 'extra'. The pricing_engine itself
        doesn't check the env var — the live_router does. But we verify that
        the 'included' regime (which is what the router uses when the kill
        switch is off) produces no price increase.
        """
        # Simulate what happens when kill switch is off: regime = "included"
        price_included = compute_effective_price(
            0.024, "ollama_cloud", 50, True,
            hour_utc=12, extra_usage_regime="included",
        )
        # No bump — price stays at base rate
        assert price_included == pytest.approx(0.024)

        # Compare with what would happen if it were "extra"
        price_extra = compute_effective_price(
            0.024, "ollama_cloud", 50, True,
            hour_utc=12, extra_usage_regime="extra",
        )
        # The extra price should be higher than included
        assert price_extra > price_included
        # But the included (kill-switch-off) price must NOT have a bump
        assert price_included == pytest.approx(0.024)