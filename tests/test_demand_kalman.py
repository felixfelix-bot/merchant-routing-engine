"""Tests for demand_kalman — Demand-curve Kalman filter (ADR-005 Layer 2).

Verifies:
  - State init: intercept and slope start at configured priors
  - Update: (price, traffic) observations move the estimate
  - Prediction: demand(price) = intercept + slope * price
  - Convergence: filter converges to true demand curve from noisy observations
  - Edge cases: single observation, zero traffic, negative slope
  - Slowly-varying demand: process noise allows tracking demand shifts
  - Independence: separate instances don't cross-contaminate
"""
from __future__ import annotations
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.demand_kalman import DemandKalman


# ── State initialization ────────────────────────────────────────────────────


def test_initial_state_defaults():
    """Default init: intercept=0, slope=0 → demand(p) = 0 for any p."""
    kf = DemandKalman()
    assert kf.intercept == pytest.approx(0.0)
    assert kf.slope == pytest.approx(0.0)


def test_initial_state_custom():
    """Custom priors should be reflected in the state."""
    kf = DemandKalman(initial_intercept=100.0, initial_slope=-2.0)
    assert kf.intercept == pytest.approx(100.0)
    assert kf.slope == pytest.approx(-2.0)


def test_demand_at_init():
    """demand(price) at init = initial_intercept + initial_slope * price."""
    kf = DemandKalman(initial_intercept=50.0, initial_slope=-1.0)
    assert kf.demand(10.0) == pytest.approx(40.0)  # 50 - 1*10
    assert kf.demand(0.0) == pytest.approx(50.0)


# ── Update from observations ───────────────────────────────────────────────


def test_single_update_moves_intercept():
    """A single (price, traffic) observation should move the intercept away
    from its prior (since the observation carries information)."""
    kf = DemandKalman(initial_intercept=0.0, initial_slope=0.0)
    kf.update(price=1.0, traffic=100.0)
    # After one observation, the estimate should be non-zero
    assert kf.demand(1.0) != pytest.approx(0.0)


def test_multiple_updates_track_demand():
    """Feed observations consistent with demand(p) = 200 - 50*p;
    the filter should converge close to true parameters."""
    true_intercept = 200.0
    true_slope = -50.0
    kf = DemandKalman(
        initial_intercept=100.0,
        initial_slope=-10.0,
        process_noise=1e-4,
        measurement_noise=1.0,
    )
    prices = [1.0, 2.0, 1.5, 3.0, 0.5, 2.5, 1.0, 1.8, 2.2, 1.3]
    for p in prices:
        traffic = true_intercept + true_slope * p
        kf.update(price=p, traffic=max(0.0, traffic))
    assert abs(kf.intercept - true_intercept) < 30.0, (
        f"intercept {kf.intercept:.2f} not close to {true_intercept}"
    )
    assert abs(kf.slope - true_slope) < 15.0, (
        f"slope {kf.slope:.2f} not close to {true_slope}"
    )


def test_update_with_zero_traffic():
    """Zero traffic at a positive price should push the slope negative."""
    kf = DemandKalman(initial_intercept=50.0, initial_slope=0.0)
    kf.update(price=5.0, traffic=0.0)
    # Demand at price 5 should have dropped
    assert kf.demand(5.0) < 50.0


def test_observation_matrix_shape():
    """The observation matrix H should be [price, 1] for the state [slope, intercept]
    so that H @ x = slope*price + intercept = demand(price)."""
    kf = DemandKalman(initial_intercept=10.0, initial_slope=-1.0)
    # demand(3.0) should equal H @ x for the observation at price=3
    expected = -1.0 * 3.0 + 10.0
    assert kf.demand(3.0) == pytest.approx(expected)


# ── Convergence ────────────────────────────────────────────────────────────


def test_converges_to_true_demand_curve():
    """With enough noisy observations, the filter should converge to within
    a reasonable tolerance of the true demand curve."""
    rng = np.random.default_rng(seed=99)
    true_intercept = 500.0
    true_slope = -100.0
    kf = DemandKalman(
        initial_intercept=250.0,
        initial_slope=-25.0,
        process_noise=1e-3,
        measurement_noise=100.0,
    )
    for _ in range(500):
        p = rng.uniform(0.5, 4.0)
        true_traffic = true_intercept + true_slope * p
        noisy = max(0.0, true_traffic + rng.normal(0, 20.0))
        kf.update(price=p, traffic=noisy)
    assert abs(kf.intercept - true_intercept) < 50.0, (
        f"intercept {kf.intercept:.1f} vs true {true_intercept}"
    )
    assert abs(kf.slope - true_slope) < 20.0, (
        f"slope {kf.slope:.1f} vs true {true_slope}"
    )


def test_demand_prediction_accuracy():
    """After training, demand(price) should predict traffic accurately
    at unseen prices."""
    rng = np.random.default_rng(seed=42)
    true_intercept = 1000.0
    true_slope = -200.0
    kf = DemandKalman(
        initial_intercept=500.0,
        initial_slope=-50.0,
        process_noise=1e-4,
        measurement_noise=50.0,
    )
    train_prices = np.linspace(1.0, 4.0, 100)
    for p in train_prices:
        t = true_intercept + true_slope * p + rng.normal(0, 10.0)
        kf.update(price=p, traffic=max(0.0, t))
    # Test at unseen prices
    for test_p in [1.5, 2.5, 3.5]:
        true_d = true_intercept + true_slope * test_p
        est_d = kf.demand(test_p)
        assert abs(est_d - true_d) < 100.0, (
            f"demand({test_p}): est {est_d:.1f} vs true {true_d:.1f}"
        )


# ── Slowly-varying demand (process model) ──────────────────────────────────


def test_tracks_shifting_demand():
    """The demand curve shifts over time (intercept increases). The filter
    should track the shift thanks to process noise."""
    kf = DemandKalman(
        initial_intercept=100.0,
        initial_slope=-20.0,
        process_noise=5.0,
        measurement_noise=10.0,
    )
    # Phase 1: demand = 200 - 40*p
    for p in [1.0, 2.0, 1.5, 2.5, 1.0, 2.0]:
        kf.update(price=p, traffic=max(0.0, 200.0 - 40.0 * p))
    # Phase 2: demand shifts to 400 - 40*p (intercept doubles)
    for _ in range(50):
        for p in [1.0, 2.0, 1.5, 2.5]:
            kf.update(price=p, traffic=max(0.0, 400.0 - 40.0 * p))
    assert kf.intercept > 300.0, (
        f"intercept {kf.intercept:.1f} should track shift to ~400"
    )


# ── Edge cases ─────────────────────────────────────────────────────────────


def test_negative_demand_floored():
    """demand(price) can go negative for high prices with a negative slope.
    The filter should return the raw estimate (can be negative) — the
    margin layer handles flooring."""
    kf = DemandKalman(initial_intercept=100.0, initial_slope=-50.0)
    # At price=10: 100 - 50*10 = -400
    d = kf.demand(10.0)
    assert d == pytest.approx(-400.0)


def test_demand_at_zero_price():
    """demand(0) = intercept (the maximum demand when free)."""
    kf = DemandKalman(initial_intercept=200.0, initial_slope=-30.0)
    assert kf.demand(0.0) == pytest.approx(200.0)


def test_zero_measurement_noise_converges_exactly():
    """With zero measurement noise, the filter should converge tightly
    to the exact demand curve from a few observations."""
    kf = DemandKalman(
        initial_intercept=0.0,
        initial_slope=0.0,
        process_noise=1e-12,
        measurement_noise=1e-12,
    )
    # Two exact observations fully determine a line
    kf.update(price=1.0, traffic=150.0)  # a + b*1 = 150
    kf.update(price=3.0, traffic=50.0)   # a + b*3 = 50
    # Solving: b = (50 - 150) / (3 - 1) = -50, a = 150 - (-50)*1 = 200
    assert kf.demand(1.0) == pytest.approx(150.0, abs=1e-3)
    assert kf.demand(3.0) == pytest.approx(50.0, abs=1e-3)


# ── Instance independence ──────────────────────────────────────────────────


def test_independent_instances():
    """Two demand Kalman filters must not affect each other."""
    a = DemandKalman(initial_intercept=100.0, initial_slope=-10.0)
    b = DemandKalman(initial_intercept=500.0, initial_slope=-50.0)
    a.update(price=2.0, traffic=80.0)
    assert b.intercept == pytest.approx(500.0)
    assert b.slope == pytest.approx(-50.0)


def test_update_count_increments():
    """update_count should track the number of observations fed."""
    kf = DemandKalman()
    assert kf.update_count == 0
    kf.update(price=1.0, traffic=10.0)
    assert kf.update_count == 1
    kf.update(price=2.0, traffic=5.0)
    assert kf.update_count == 2


# ── Properties / introspection ─────────────────────────────────────────────


def test_uncertainty_decreases():
    """Covariance should decrease after observations (we're more confident)."""
    kf = DemandKalman(
        initial_intercept=100.0,
        initial_slope=-10.0,
        process_noise=1e-6,
        measurement_noise=1.0,
    )
    p0 = kf.uncertainty
    for _ in range(10):
        kf.update(price=2.0, traffic=50.0)
    assert kf.uncertainty < p0


def test_state_covariance_matrix():
    """P should be a 2x2 positive semi-definite matrix."""
    kf = DemandKalman()
    P = kf.covariance
    assert P.shape == (2, 2)
    # Diagonal should be non-negative
    assert P[0, 0] >= 0
    assert P[1, 1] >= 0


def test_predict_horizon():
    """predict_horizon should return demand estimates for a list of prices."""
    kf = DemandKalman(initial_intercept=100.0, initial_slope=-20.0)
    prices = [1.0, 2.0, 3.0]
    demands = kf.predict_horizon(prices)
    assert len(demands) == 3
    assert demands[0] == pytest.approx(80.0)
    assert demands[1] == pytest.approx(60.0)
    assert demands[2] == pytest.approx(40.0)


def test_reset():
    """reset() should restore the filter to its initial configuration."""
    kf = DemandKalman(initial_intercept=100.0, initial_slope=-10.0)
    kf.update(price=2.0, traffic=80.0)
    kf.update(price=3.0, traffic=50.0)
    assert kf.update_count == 2
    kf.reset()
    assert kf.update_count == 0
    assert kf.intercept == pytest.approx(100.0)
    assert kf.slope == pytest.approx(-10.0)