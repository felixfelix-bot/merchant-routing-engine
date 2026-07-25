"""Tests for consumption_kalman — provider-agnostic token-burn Kalman filter.

Extracted from ~/.hermes/bot/burn_predictor.py (KalmanPredictor, lines 65-138).
Extends the original 2-state [volume, velocity] filter to a 3-state
constant-acceleration model [burn_rate, velocity, acceleration] per ADR-002
invariant #2.

These tests verify:
  - Filter converges on the true burn rate with noisy observations
  - Prediction accuracy improves over time
  - Exhaustion prediction is within tolerance
  - Handles zero / no-data gracefully
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.consumption_kalman import ConsumptionKalman


# ── Helpers ──────────────────────────────────────────────────────────────────

def make_noisy_observations(true_rate, n, noise_std, seed):
    """Generate n noisy per-period token observations around true_rate."""
    rng = np.random.default_rng(seed=seed)
    return [max(0.0, true_rate + rng.normal(0, noise_std)) for _ in range(n)]


# ── Convergence ──────────────────────────────────────────────────────────────

def test_converges_on_true_burn_rate_with_noise():
    """After enough observations the smoothed burn_rate ≈ true rate."""
    true_rate = 1_000_000.0  # tokens per period
    noise_std = 50_000.0
    obs = make_noisy_observations(true_rate, n=60, noise_std=noise_std, seed=42)

    kf = ConsumptionKalman.from_history(obs)

    err = abs(kf.burn_rate - true_rate)
    assert err < noise_std, (
        f"burn_rate {kf.burn_rate:.0f} off by {err:.0f} > noise floor {noise_std:.0f}"
    )


def test_prediction_error_decreases_over_time():
    """Predictions trained on more data should be more accurate vs the true rate."""
    true_rate = 500_000.0
    obs = make_noisy_observations(true_rate, n=80, noise_std=30_000.0, seed=7)

    def horizon_error(window_end):
        kf = ConsumptionKalman.from_history(obs[:window_end])
        pred = kf.predict_horizon(1)[0]
        return abs(pred - true_rate)

    early = horizon_error(5)
    late = horizon_error(60)
    assert late < early, f"late error {late:.0f} should be < early {early:.0f}"


# ── Exhaustion prediction ────────────────────────────────────────────────────

def test_exhaustion_prediction_within_tolerance():
    """Constant burn → exhausts at quota / burn_rate periods."""
    burn = 100_000.0
    rng = np.random.default_rng(seed=1)
    obs = [burn + rng.normal(0, 100) for _ in range(30)]  # ~constant
    kf = ConsumptionKalman.from_history(obs)

    quota = 1_000_000.0  # exactly 10 periods of burn
    will, in_periods = kf.will_exhaust(quota, steps=20)

    assert will is True
    assert in_periods is not None
    assert abs(in_periods - 10.0) < 0.5, f"expected ~10 periods, got {in_periods:.2f}"


def test_will_not_exhaust_when_quota_sufficient():
    obs = [1_000.0] * 20
    kf = ConsumptionKalman.from_history(obs)
    will, in_periods = kf.will_exhaust(quota_remaining=1_000_000, steps=10)
    assert will is False
    assert in_periods is None


def test_exhaustion_immediate_when_quota_zero():
    kf = ConsumptionKalman()
    kf.update(1000)
    will, in_periods = kf.will_exhaust(0, steps=5)
    assert will is True
    assert in_periods == 0.0


def test_exhaustion_negative_quota_treated_as_exhausted():
    kf = ConsumptionKalman.from_history([1000.0] * 5)
    will, in_periods = kf.will_exhaust(-50, steps=5)
    assert will is True
    assert in_periods == 0.0


# ── Graceful handling of no-data / zero ──────────────────────────────────────

def test_no_data_returns_safe_defaults():
    kf = ConsumptionKalman()
    assert kf.burn_rate == 0.0
    assert kf.velocity == 0.0
    assert kf.acceleration == 0.0
    assert kf.tokens_used == 0.0
    assert kf.uncertainty >= 0.0
    assert kf.is_initialized is False
    assert kf.update_count == 0
    # No data → cannot predict exhaustion
    will, in_periods = kf.will_exhaust(quota_remaining=1000, steps=10)
    assert will is False
    assert in_periods is None


def test_zero_observations_converge_to_zero():
    kf = ConsumptionKalman(measurement_noise=1.0, process_noise=1e-3)
    for _ in range(20):
        kf.update(0.0)
    assert kf.is_initialized is True
    assert kf.burn_rate == pytest.approx(0.0, abs=1.0)
    assert kf.tokens_used == 0.0


def test_predict_horizon_uninitialized_returns_zeros():
    kf = ConsumptionKalman()
    horizon = kf.predict_horizon(5)
    assert len(horizon) == 5
    assert all(h == 0.0 for h in horizon)


# ── Horizon projection ───────────────────────────────────────────────────────

def test_predict_horizon_length_and_nonnegativity():
    obs = make_noisy_observations(100_000.0, n=15, noise_std=1_000.0, seed=3)
    kf = ConsumptionKalman.from_history(obs)
    horizon = kf.predict_horizon(12)
    assert len(horizon) == 12
    assert all(h >= 0 for h in horizon)


def test_predict_horizon_does_not_mutate_state():
    """Querying the horizon must be a pure read — no side effects on the filter."""
    obs = make_noisy_observations(100_000.0, n=15, noise_std=5_000.0, seed=3)
    kf = ConsumptionKalman.from_history(obs)
    before = (kf.burn_rate, kf.velocity, kf.acceleration, kf.uncertainty)
    h1 = kf.predict_horizon(10)
    h2 = kf.predict_horizon(10)
    after = (kf.burn_rate, kf.velocity, kf.acceleration, kf.uncertainty)
    assert before == after
    assert h1 == h2


def test_predict_horizon_zero_steps_returns_empty():
    kf = ConsumptionKalman.from_history([100.0, 200.0, 150.0])
    assert kf.predict_horizon(0) == []
    assert kf.predict_horizon(-3) == []


def test_predict_cumulative_sums_horizon():
    kf = ConsumptionKalman.from_history([100_000.0] * 20)
    total = kf.predict_cumulative(5)
    # ~100k per period for 5 periods
    assert abs(total - 500_000.0) < 25_000.0


def test_acceleration_tracks_trend():
    """Linearly increasing burn → positive velocity; acceleration near zero
    (constant slope). Acceleration becomes positive for quadratic growth."""
    obs = [10_000.0 * (i + 1) for i in range(20)]  # 10k, 20k, ..., 200k
    kf = ConsumptionKalman.from_history(obs)
    assert kf.velocity > 0
    # Linear growth → acceleration should be small relative to velocity
    assert abs(kf.acceleration) < abs(kf.velocity) * 0.1


# ── Instance independence (one per provider) ─────────────────────────────────

def test_provider_instances_are_independent():
    a = ConsumptionKalman.from_history([100_000.0] * 10)
    b = ConsumptionKalman.from_history([1_000_000.0] * 10)
    assert abs(a.burn_rate - b.burn_rate) > 100_000.0
    assert a.tokens_used == 1_000_000.0
    assert b.tokens_used == 10_000_000.0
    # Updating one must not affect the other
    a.update(5_000_000.0)
    assert b.burn_rate < 1_100_000.0


# ── from_history adaptive tuning ─────────────────────────────────────────────

def test_from_history_adapts_measurement_noise_to_signal():
    big = ConsumptionKalman.from_history([1_000_000.0 + i * 100 for i in range(15)])
    small = ConsumptionKalman.from_history([100.0 + i * 0.01 for i in range(15)])
    # Adaptive R should scale with the data magnitude
    assert big.R[0, 0] > small.R[0, 0]


def test_from_history_empty_returns_uninitialized():
    kf = ConsumptionKalman.from_history([])
    assert kf.is_initialized is False
    assert kf.burn_rate == 0.0


# ── Standalone / no external deps ────────────────────────────────────────────

def test_module_is_standalone():
    """Must not import provider-specific or hermes-internal code."""
    import src.consumption_kalman as mod
    src = open(mod.__file__).read()
    forbidden = [
        "zai_proxy",
        "multi_resource_kalman",
        "import sqlite3",
        "urllib",
        "from hermes",
        "import hermes",
        "burn_predictor",
    ]
    for token in forbidden:
        assert token not in src, f"standalone module must not reference {token!r}"
    # numpy is the only third-party dependency
    assert "import numpy" in src
