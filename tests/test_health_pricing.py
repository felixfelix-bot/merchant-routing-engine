"""Tests for graduated health pricing — replaces binary health_factor.

The new health_pricing_factor is a pure function of (failure_count,
breaker_tripped) that returns a graduated multiplier:

    0 failures       → 1.0x  (no penalty)
    1-2 failures    → 1.5x  (soft penalty, transient issue)
    3-5 failures    → 3.0x  (moderate penalty, clearly problematic)
    6-10 failures   → 10.0x (severe penalty, almost unreachable)
    >10 failures    → +inf  (circuit breaker, fully unreachable)
    breaker_tripped → +inf  (circuit breaker, fully unreachable)

The old 429 burst penalty (2.0x for >3 recent 429s) is integrated into
this graduated scale: 429s increment failure_count, so a burst of 429s
naturally falls into the 3-5 range → 3.0x.
"""
from __future__ import annotations

import math

import pytest

from src.pricing_engine import health_pricing_factor


# ── Graduated scale ───────────────────────────────────────────────────────────


class TestHealthPricingFactorGraduated:
    """Verify the five-tier graduated penalty scale."""

    def test_zero_failures_no_penalty(self):
        assert health_pricing_factor(failure_count=0) == 1.0

    def test_one_failure_soft_penalty(self):
        assert health_pricing_factor(failure_count=1) == 1.5

    def test_two_failures_soft_penalty(self):
        assert health_pricing_factor(failure_count=2) == 1.5

    def test_three_failures_moderate_penalty(self):
        assert health_pricing_factor(failure_count=3) == 3.0

    def test_four_failures_moderate_penalty(self):
        assert health_pricing_factor(failure_count=4) == 3.0

    def test_five_failures_moderate_penalty(self):
        assert health_pricing_factor(failure_count=5) == 3.0

    def test_six_failures_severe_penalty(self):
        assert health_pricing_factor(failure_count=6) == 10.0

    def test_eight_failures_severe_penalty(self):
        assert health_pricing_factor(failure_count=8) == 10.0

    def test_ten_failures_severe_penalty(self):
        assert health_pricing_factor(failure_count=10) == 10.0

    def test_eleven_failures_circuit_breaker(self):
        assert math.isinf(health_pricing_factor(failure_count=11))

    def test_twenty_failures_circuit_breaker(self):
        assert math.isinf(health_pricing_factor(failure_count=20))

    def test_hundred_failures_circuit_breaker(self):
        assert math.isinf(health_pricing_factor(failure_count=100))


class TestHealthPricingFactorBreaker:
    """breaker_tripped overrides everything → infinity."""

    def test_breaker_tripped_with_zero_failures(self):
        assert math.isinf(health_pricing_factor(failure_count=0, breaker_tripped=True))

    def test_breaker_tripped_with_many_failures(self):
        assert math.isinf(health_pricing_factor(failure_count=50, breaker_tripped=True))

    def test_breaker_tripped_precedence(self):
        """breaker_tripped takes precedence over any failure_count."""
        for fc in (0, 1, 5, 10, 11, 100):
            assert math.isinf(health_pricing_factor(fc, breaker_tripped=True))


class TestHealthPricingFactorDefaults:
    """Default arguments and edge cases."""

    def test_default_no_failures_no_breaker(self):
        assert health_pricing_factor() == 1.0

    def test_default_breaker_false(self):
        assert health_pricing_factor(failure_count=5) == 3.0

    def test_negative_failure_count_treated_as_zero(self):
        """Negative failure_count should not cause issues — treat as 0."""
        assert health_pricing_factor(failure_count=-1) == 1.0
        assert health_pricing_factor(failure_count=-100) == 1.0


class TestHealthPricingFactorPure:
    """health_pricing_factor is a pure function — no side effects."""

    def test_same_input_same_output(self):
        for fc in (0, 1, 3, 6, 11, 50):
            a = health_pricing_factor(fc)
            b = health_pricing_factor(fc)
            assert a == b

    def test_monotonically_increasing(self):
        """Penalty should never decrease as failure_count increases."""
        prev = health_pricing_factor(0)
        for fc in range(1, 20):
            curr = health_pricing_factor(fc)
            assert curr >= prev, f"non-monotonic at fc={fc}: {curr} < {prev}"
            prev = curr


class TestHealthPricingFactorIntegrates429:
    """The old 429 burst penalty is now subsumed by failure_count.

    A burst of >3 recent 429s used to give a flat 2.0x penalty.
    Now 429s increment failure_count, so 4 or more 429s → failure_count>=4
    → falls in 3-5 range → 3.0x (stronger than old 2.0x, which is correct:
    a 429 burst is a clear signal of trouble).
    """

    def test_four_429s_maps_to_moderate_penalty(self):
        """4 recent 429s → failure_count=4 → 3.0x (was 2.0x under old system)."""
        assert health_pricing_factor(failure_count=4) == 3.0

    def test_three_429s_maps_to_moderate_penalty(self):
        """3 recent 429s → failure_count=3 → 3.0x."""
        assert health_pricing_factor(failure_count=3) == 3.0

    def test_one_429_maps_to_soft_penalty(self):
        """1 recent 429 → failure_count=1 → 1.5x."""
        assert health_pricing_factor(failure_count=1) == 1.5