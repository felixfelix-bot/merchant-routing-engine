"""Tests for src/pricing_engine.py — deterministic multiplier layer.

Covers every function and the ADR-004 positivity invariant, including the
tricky +inf-preservation and 0*inf=NaN edge cases.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

import src.pricing_engine as pe
from src.pricing_engine import (
    MIN_EFFECTIVE_PRICE,
    PEAK_HOURS_UTC,
    PEAK_MULTIPLIER,
    compute_effective_price,
    health_factor,
    peak_multiplier,
    scarcity_factor,
)


# ── peak_multiplier ─────────────────────────────────────────────────────────


class TestPeakMultiplier:
    def test_zai_at_each_peak_hour(self):
        for h in (6, 7, 8, 9):
            assert peak_multiplier("zai", h) == PEAK_MULTIPLIER == 3.0

    def test_zai_off_peak_is_one(self):
        # boundary hours just outside the window, plus midnight and midday
        for h in (0, 1, 5, 10, 11, 15, 23):
            assert peak_multiplier("zai", h) == 1.0

    def test_non_zai_providers_never_peak(self):
        for provider in ("ollama_cloud", "ppq", "openrouter", "external"):
            assert peak_multiplier(provider, 8) == 1.0

    def test_zai_key_variants_peak(self):
        # both zai keys share the upstream and the peak window
        assert peak_multiplier("zai_ours", 7) == 3.0
        assert peak_multiplier("zai_friend", 9) == 3.0

    def test_provider_match_is_case_insensitive(self):
        assert peak_multiplier("ZAI", 8) == 3.0
        assert peak_multiplier("Zai_Ours", 6) == 3.0

    def test_provider_prefix_not_substring(self):
        # "zai" must be a prefix, not an arbitrary substring
        assert peak_multiplier("blazai", 8) == 1.0
        assert peak_multiplier("pizza", 8) == 1.0

    def test_none_hour_uses_current_utc_hour(self, monkeypatch):
        class FakeDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 1, 1, 8, 0, 0, tzinfo=timezone.utc)

        monkeypatch.setattr(pe, "datetime", FakeDateTime)
        assert peak_multiplier("zai", None) == 3.0

        class FakeDateTimeOffPeak(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        monkeypatch.setattr(pe, "datetime", FakeDateTimeOffPeak)
        assert peak_multiplier("zai", None) == 1.0

    def test_peak_hours_constant_matches_spec(self):
        assert PEAK_HOURS_UTC == frozenset({6, 7, 8, 9})


# ── scarcity_factor ─────────────────────────────────────────────────────────


class TestScarcityFactor:
    @pytest.mark.parametrize("pct", [0, 25, 49, 50])
    def test_at_or_below_onset_is_one(self, pct):
        assert scarcity_factor(pct) == 1.0

    def test_at_75_is_1p5(self):
        assert scarcity_factor(75) == pytest.approx(1.5)

    def test_at_100_is_two(self):
        assert scarcity_factor(100) == 2.0

    def test_over_quota_keeps_ramping(self):
        # no upper clamp — reflects genuine over-allocation pressure
        assert scarcity_factor(150) == pytest.approx(3.0)

    def test_negative_quota_is_one(self):
        assert scarcity_factor(-20) == 1.0

    def test_always_at_least_one(self):
        for pct in (-100, -1, 0, 50, 99, 100, 250):
            assert scarcity_factor(pct) >= 1.0


# ── health_factor ───────────────────────────────────────────────────────────


class TestHealthFactor:
    def test_unhealthy_is_inf(self):
        assert health_factor(False) == math.inf

    def test_unhealthy_precedence_over_429(self):
        # both conditions set → inf wins (unreachable, not just penalised)
        assert health_factor(False, recent_429=100) == math.inf

    def test_healthy_no_429_is_one(self):
        assert health_factor(True, recent_429=0) == 1.0

    def test_429_at_threshold_is_not_penalised(self):
        # penalty is strictly greater than 3
        assert health_factor(True, recent_429=3) == 1.0

    def test_429_above_threshold_is_penalised(self):
        # Under graduated health pricing, 4 recent 429s → failure_count=4
        # → falls in 3-5 range → 3.0x (was 2.0x under old binary system).
        # The old flat 2.0x is replaced by the graduated scale, which is
        # stronger because a 429 burst is a clear signal of trouble.
        assert health_factor(True, recent_429=4) == 3.0
        assert health_factor(True, recent_429=50) == math.inf

    def test_default_recent_429_is_zero(self):
        assert health_factor(True) == 1.0


# ── compute_effective_price ─────────────────────────────────────────────────


class TestComputeEffectivePrice:
    def test_basic_composition_off_peak(self):
        # 0.068 * 1.0 (off-peak) * 1.0 (50%) * 1.0 (healthy) = 0.068
        price = compute_effective_price(0.068, "zai", 50, True, hour_utc=12)
        assert price == pytest.approx(0.068)

    def test_full_composition_peak_exhausted(self):
        # 0.068 * 3.0 (peak) * 2.0 (100%) * 1.0 (healthy) = 0.408
        price = compute_effective_price(0.068, "zai", 100, True, hour_utc=8)
        assert price == pytest.approx(0.408)

    def test_peak_and_scarcity_partial(self):
        # 0.068 * 3.0 (peak) * 1.5 (75%) * 1.0 = 0.306
        price = compute_effective_price(0.068, "zai", 75, True, hour_utc=6)
        assert price == pytest.approx(0.306)

    def test_429_penalty_applied(self):
        # Under graduated health pricing, 4 recent 429s → failure_count=4
        # → 3.0x penalty (was 2.0x under old binary system).
        # 0.068 * 1.0 (off-peak) * 1.0 (50%) * 3.0 (graduated penalty) = 0.204
        price = compute_effective_price(0.068, "zai", 50, True, recent_429=4, hour_utc=12)
        assert price == pytest.approx(0.204)

    def test_unhealthy_preserves_inf(self):
        # ADR-004: inf is the unreachable signal and must NOT be floored
        price = compute_effective_price(0.068, "zai", 50, False, hour_utc=12)
        assert price == math.inf

    def test_free_provider_floored_to_epsilon(self):
        # base_rate 0 → floored to MIN_EFFECTIVE_PRICE (ADR-004)
        price = compute_effective_price(0.0, "zai", 50, True, hour_utc=12)
        assert price == MIN_EFFECTIVE_PRICE

    def test_tiny_positive_floored_to_epsilon(self):
        price = compute_effective_price(0.0001, "zai", 50, True, hour_utc=12)
        assert price == MIN_EFFECTIVE_PRICE

    def test_negative_base_floored_to_epsilon(self):
        price = compute_effective_price(-5.0, "zai", 50, True, hour_utc=8)
        assert price == MIN_EFFECTIVE_PRICE

    def test_zero_base_unhealthy_avoids_nan(self):
        # 0 * inf would be NaN; must be caught and floored (ADR-004 invariant #4)
        price = compute_effective_price(0.0, "zai", 50, False, hour_utc=12)
        assert not math.isnan(price)
        assert price == MIN_EFFECTIVE_PRICE

    def test_non_zai_provider_ignores_peak(self):
        # ollama_cloud at hour 8 → peak mult 1.0
        price = compute_effective_price(0.10, "ollama_cloud", 50, True, hour_utc=8)
        assert price == pytest.approx(0.10)

    def test_result_always_positive(self):
        cases = [
            (0.068, "zai", 100, True, 0, 8),
            (0.0, "ollama_cloud", 0, True, 0, 3),
            (-1.0, "ppq", 200, True, 10, 23),
            (0.068, "zai", 50, False, 0, 12),  # inf
            (0.0, "zai", 50, False, 0, 12),    # nan→floor
        ]
        for base, prov, quota, healthy, r429, hour in cases:
            price = compute_effective_price(base, prov, quota, healthy, r429, hour)
            assert not math.isnan(price)
            assert price > 0, f"non-positive price {price} for {prov}"

    def test_default_hour_utc_none_runs(self):
        # smoke: None hour resolves to current hour without error
        price = compute_effective_price(0.068, "zai", 50, True)
        assert price > 0

    # ── New graduated health pricing interface ─────────────────────────────

    def test_graduated_soft_penalty(self):
        """failure_count=1 → 1.5x penalty."""
        price = compute_effective_price(0.068, "zai", 50, hour_utc=12, failure_count=1)
        assert price == pytest.approx(0.068 * 1.5)

    def test_graduated_moderate_penalty(self):
        """failure_count=4 → 3.0x penalty."""
        price = compute_effective_price(0.068, "zai", 50, hour_utc=12, failure_count=4)
        assert price == pytest.approx(0.068 * 3.0)

    def test_graduated_severe_penalty(self):
        """failure_count=8 → 10.0x penalty."""
        price = compute_effective_price(0.068, "zai", 50, hour_utc=12, failure_count=8)
        assert price == pytest.approx(0.068 * 10.0)

    def test_graduated_circuit_breaker(self):
        """failure_count=15 → +inf (circuit breaker)."""
        price = compute_effective_price(0.068, "zai", 50, hour_utc=12, failure_count=15)
        assert price == math.inf

    def test_breaker_tripped_explicit(self):
        """breaker_tripped=True → +inf regardless of failure_count."""
        price = compute_effective_price(0.068, "zai", 50, hour_utc=12,
                                         failure_count=0, breaker_tripped=True)
        assert price == math.inf

    def test_new_interface_no_legacy_args(self):
        """New interface works without is_healthy at all."""
        price = compute_effective_price(0.068, "zai", 50, hour_utc=12, failure_count=2)
        assert price == pytest.approx(0.068 * 1.5)
