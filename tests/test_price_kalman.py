"""Tests for price_kalman — Base-Rate Kalman filter (ADR-001/003/004).

Verifies:
  - Amortization: base_rate decreases as tokens accumulate in a billing cycle
  - Peak multiplier applied correctly (deterministic step function)
  - Effective price is ALWAYS > 0 (ADR-004 positivity invariant)
  - Kalman converges toward true rate from noisy observations
  - Scarcity factor ramps deterministically
  - Health factor = infinity when circuit breaker tripped
"""
from __future__ import annotations
import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.price_kalman import (
    PriceKalman,
    MIN_EFFECTIVE_PRICE,
    peak_multiplier,
    scarcity_factor,
    health_factor,
)


# ── Amortization: base_rate falls as tokens consumed rises ──────────────────


def test_amortization_base_rate_decreases():
    """As cumulative tokens rise within a billing cycle, the observed
    rate = subscription_fee / tokens falls. The Kalman estimate should
    follow that downward trend (velocity < 0)."""
    kf = PriceKalman(initial_rate=1.0, process_noise=1e-6, measurement_noise=1e-4)
    # z.ai ours: $155/mo. Simulate observed $/M falling as tokens accumulate.
    # tokens: 50M, 200M, 500M, 1B, 2B  → rate = 155/tokens*1e6
    for tokens in (50e6, 200e6, 500e6, 1e9, 2e9):
        observed = 155.0 / tokens * 1e6
        kf.update(observed)
    final = kf.predict()
    assert final < 1.0, "base_rate should decrease as tokens accumulate"
    assert kf.velocity < 0.0, "velocity should be negative (cost falling)"


def test_amortization_monotonic_decreasing_observations():
    """Feed a strictly decreasing observation sequence; estimate at the end
    must be below the estimate after the first few updates."""
    kf = PriceKalman(initial_rate=1.0, process_noise=1e-6, measurement_noise=1e-4)
    seq = [1.0, 0.5, 0.2, 0.1, 0.07]
    estimates = []
    for obs in seq:
        kf.update(obs)
        estimates.append(kf.predict())
    assert estimates[-1] < estimates[0]
    # Trend should be downward overall
    assert estimates[-1] < estimates[2]


# ── Peak multiplier: deterministic step function (ADR-003) ──────────────────


def test_peak_multiplier_off_peak_is_one():
    assert peak_multiplier(hour=3) == 1.0
    assert peak_multiplier(hour=15) == 1.0
    assert peak_multiplier(hour=23) == 1.0


def test_peak_multiplier_during_peak_is_three():
    for h in (6, 7, 8, 9):
        assert peak_multiplier(hour=h) == 3.0


def test_peak_multiplier_custom_hours():
    # operator can change the peak window
    assert peak_multiplier(peak_hours_utc=(0, 1), peak_mult=2.5, hour=1) == 2.5
    assert peak_multiplier(peak_hours_utc=(0, 1), peak_mult=2.5, hour=5) == 1.0


def test_effective_price_peak_triples_offpeak():
    """effective_price(base, peak=3) must be 3× effective_price(base, peak=1)."""
    kf = PriceKalman(initial_rate=0.068, process_noise=1e-6, measurement_noise=1e-4)
    kf.update(0.068)
    offpeak = kf.effective_price(peak_mult=1.0)
    peak = kf.effective_price(peak_mult=3.0)
    assert offpeak > 0
    assert peak == pytest.approx(3.0 * offpeak, rel=1e-9)


# ── Effective price ALWAYS > 0 (ADR-004) ────────────────────────────────────


def test_free_provider_has_positive_price():
    """A provider with observed_rate=0 (local Ollama) still gets a positive
    effective price — never zero. ADR-004."""
    kf = PriceKalman(initial_rate=0.0, process_noise=1e-6, measurement_noise=1e-4)
    kf.update(0.0)
    kf.update(0.0)
    price = kf.effective_price()
    assert price >= MIN_EFFECTIVE_PRICE
    assert price > 0


def test_min_effective_price_floor_applied():
    """When raw computation would drop below MIN_EFFECTIVE_PRICE, the floor
    kicks in."""
    kf = PriceKalman(initial_rate=1e-9, process_noise=1e-12, measurement_noise=1e-12)
    price = kf.effective_price()
    assert price == pytest.approx(MIN_EFFECTIVE_PRICE)


def test_effective_price_never_zero_or_negative():
    """Even with tiny / negative-ish inputs, the floor keeps it positive."""
    kf = PriceKalman(initial_rate=1e-12, process_noise=1e-12, measurement_noise=1e-12)
    for obs in (0.0, 1e-15, 0.0):
        kf.update(obs)
    assert kf.effective_price() > 0
    assert kf.effective_price() >= MIN_EFFECTIVE_PRICE


# ── Kalman convergence with noisy observations ──────────────────────────────


def test_converges_to_true_rate_with_noise():
    """Feed many noisy observations around a true rate; the Kalman estimate
    should converge close to the true value (closer than the naive mean of
    the last few would be under heavy noise)."""
    rng = np.random.default_rng(seed=42)
    true_rate = 0.068  # z.ai ours off-peak $/M
    kf = PriceKalman(
        initial_rate=true_rate, process_noise=1e-5, measurement_noise=1e-3
    )
    n = 200
    for _ in range(n):
        noise = rng.normal(0, 0.02)  # ±2¢ noise — large relative to 6.8¢
        kf.update(max(1e-6, true_rate + noise))
    est = kf.predict()
    # Should be within 30% of the true rate despite heavy noise
    assert abs(est - true_rate) < 0.3 * true_rate, (
        f"Kalman estimate {est:.4f} not within 30% of true {true_rate}"
    )


def test_kalman_reduces_noise_vs_raw():
    """The Kalman estimate should be less noisy than the raw observations."""
    rng = np.random.default_rng(seed=7)
    true_rate = 0.068
    kf = PriceKalman(
        initial_rate=true_rate, process_noise=1e-6, measurement_noise=1e-3
    )
    raw = []
    est = []
    for _ in range(100):
        obs = max(1e-6, true_rate + rng.normal(0, 0.02))
        raw.append(obs)
        kf.update(obs)
        est.append(kf.predict())
    raw_var = float(np.var(raw))
    est_var = float(np.var(est))
    # Kalman output variance must be materially smaller than raw variance
    assert est_var < raw_var * 0.5, (
        f"Kalman variance {est_var:.2e} not < 50% of raw {raw_var:.2e}"
    )


def test_velocity_captures_downward_trend():
    """For a steadily-falling observation sequence, velocity goes negative
    and the predicted rate extrapolates below the last observation."""
    kf = PriceKalman(initial_rate=0.2, process_noise=1e-5, measurement_noise=1e-4)
    for obs in (0.20, 0.18, 0.16, 0.14, 0.12, 0.10):
        kf.update(obs)
    assert kf.velocity < 0.0


# ── Deterministic multiplier helpers ────────────────────────────────────────


def test_scarcity_factor_below_50pct_is_one():
    assert scarcity_factor(0) == 1.0
    assert scarcity_factor(49) == 1.0
    assert scarcity_factor(50) == 1.0


def test_scarcity_factor_ramps_to_two_at_full():
    assert scarcity_factor(75) == pytest.approx(1.5)
    assert scarcity_factor(100) == pytest.approx(2.0)


def test_scarcity_factor_clamps_negative():
    """quota_used_pct above 100 (over-quota) should not blow past 2.0 ramp
    unboundedly — but per ADR formula it's 1+max(0,(q-50)/50), so 110→2.2.
    This documents the formula behaviour."""
    assert scarcity_factor(110) == pytest.approx(2.2)


def test_health_factor_healthy():
    assert health_factor(False) == 1.0


def test_health_factor_breaker_tripped_is_inf():
    """When the circuit breaker is tripped, health_factor = infinity,
    making the provider unreachable (not zero-cost). ADR-004 invariant 4."""
    h = health_factor(True)
    assert math.isinf(h)


def test_effective_price_with_inf_health_is_inf():
    """A tripped circuit breaker makes effective_price infinite regardless
    of base_rate — the provider becomes unreachable."""
    kf = PriceKalman(initial_rate=0.068)
    kf.update(0.068)
    price = kf.effective_price(health=float("inf"))
    assert math.isinf(price)


def test_effective_price_full_formula():
    """base_rate × peak × scarcity × health, floored at MIN_EFFECTIVE_PRICE."""
    kf = PriceKalman(initial_rate=0.068, process_noise=1e-6, measurement_noise=1e-4)
    kf.update(0.068)
    base = kf.predict()
    # peak 3.0, scarcity 1.5 (75% quota), health 1.0
    expected = base * 3.0 * 1.5 * 1.0
    got = kf.effective_price(peak_mult=3.0, scarcity=1.5, health=1.0)
    assert got == pytest.approx(max(expected, MIN_EFFECTIVE_PRICE))


# ── Per-provider instance independence ──────────────────────────────────────


def test_independent_instances_per_provider():
    """One Kalman per provider — updates to one must not affect another."""
    a = PriceKalman(initial_rate=0.068)
    b = PriceKalman(initial_rate=0.28)
    a.update(0.068)
    b.update(0.28)
    assert a.predict() < b.predict()


def test_initial_state():
    """Before any update, predict() returns the initial rate (or its evolved
    form); base_rate property is readable."""
    kf = PriceKalman(initial_rate=0.5)
    assert kf.base_rate == pytest.approx(0.5)
    assert kf.predict() >= 0
