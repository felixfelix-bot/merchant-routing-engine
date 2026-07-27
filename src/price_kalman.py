"""price_kalman.py — Base-Rate Kalman filter per ADR-001/003/004.

Estimates the SMOOTH component of effective cost per million tokens.
Peak multiplier, scarcity, and health are DETERMINISTIC functions applied
on top — they are NOT Kalman inputs (ADR-003).

State vector: [base_rate, rate_velocity]
  base_rate    — current estimated $/M (amortized cost)
  velocity     — rate of change in $/M per update cycle

The filter smooths noisy observations of cost/M. Peak hours, quota
scarcity, and circuit-breaker state are multiplied on top as deterministic
step/ramp functions — preserving instant step changes (ADR-003).
"""
from __future__ import annotations

import math
import time
from typing import Tuple

import numpy as np

# ── ADR-004: effective price is ALWAYS > 0 ──────────────────────────────────

MIN_EFFECTIVE_PRICE = 0.001  # $/M — floor for free providers

# ── Deterministic multiplier helpers (ADR-003) ──────────────────────────────


def peak_multiplier(
    hour: int | None = None,
    peak_hours_utc: Tuple[int, int] = (6, 10),
    peak_mult: float = 3.0,
) -> float:
    """Deterministic step function: peak_mult during peak hours, 1.0 otherwise.

    Instant step change — NOT smoothed by Kalman (ADR-003).
    """
    if hour is None:
        hour = int(time.gmtime().tm_hour)
    start, end = peak_hours_utc
    return peak_mult if start <= hour <= end else 1.0


def scarcity_factor(quota_used_pct: float) -> float:
    """Deterministic ramp: 1.0 below 50%, ramps to 2.0 at 100% quota used.

    Formula: 1 + max(0, (quota_used_pct - 50) / 50)
    Values above 100% continue ramping (over-quota penalty).
    """
    return 1.0 + max(0.0, (quota_used_pct - 50.0) / 50.0)


def health_factor(breaker_tripped: bool) -> float:
    """Deterministic: 1.0 if healthy, infinity if circuit breaker tripped.

    Infinity makes the provider UNREACHABLE (infinite cost), not zero-cost.
    ADR-004 invariant 4.

    .. deprecated::
        Use :func:`health_pricing_factor` for graduated penalties.
    """
    return float("inf") if breaker_tripped else 1.0


def health_pricing_factor(failure_count: int = 0, breaker_tripped: bool = False) -> float:
    """Graduated health multiplier based on failure count.

    Re-exports the implementation from pricing_engine to maintain the
    single-source-of-truth principle. The routing optimizer imports from
    this module, so we expose it here.

    Scale:
        breaker_tripped          → +inf   (circuit breaker: unreachable)
        failure_count > 10       → +inf   (circuit breaker: unreachable)
        6 ≤ failure_count ≤ 10   → 10.0   (severe penalty)
        3 ≤ failure_count ≤ 5    → 3.0    (moderate penalty)
        1 ≤ failure_count ≤ 2    → 1.5    (soft penalty, transient)
        failure_count ≤ 0        → 1.0    (no penalty)
    """
    from src.pricing_engine import health_pricing_factor as _hpf
    return _hpf(failure_count, breaker_tripped)


# ── PriceKalman: 2-state Kalman filter ──────────────────────────────────────


class PriceKalman:
    """Base-rate Kalman filter — estimates smooth cost/M trend for one provider.

    State: x = [base_rate, velocity]
    Transition: F = [[1, dt], [0, 1]]  (constant-velocity model, dt=1)
    Observation: z = base_rate  (H = [[1, 0]])
    """

    def __init__(
        self,
        initial_rate: float = 0.1,
        process_noise: float = 1e-6,
        measurement_noise: float = 1e-4,
    ):
        # State estimate
        self._x = np.array([[initial_rate], [0.0]], dtype=float)  # [rate, velocity]
        # State covariance — start uncertain
        self._P = np.eye(2, dtype=float) * 1.0
        # Transition matrix (constant velocity, dt=1)
        self._F = np.array([[1.0, 1.0], [0.0, 1.0]], dtype=float)
        # Observation matrix
        self._H = np.array([[1.0, 0.0]], dtype=float)
        # Process noise covariance
        self._Q = np.eye(2, dtype=float) * process_noise
        # Measurement noise
        self._R = np.array([[measurement_noise]], dtype=float)
        self._updates = 0

    # ── Properties ───────────────────────────────────────────────────────

    @property
    def base_rate(self) -> float:
        """Current estimated $/M (state[0])."""
        return float(self._x[0, 0])

    @property
    def velocity(self) -> float:
        """Current rate of change in $/M per cycle (state[1])."""
        return float(self._x[1, 0])

    # ── Kalman update cycle ──────────────────────────────────────────────

    def predict_step(self) -> None:
        """Predict next state (prior)."""
        self._x = self._F @ self._x
        self._P = self._F @ self._P @ self._F.T + self._Q

    def update(self, observed_rate: float) -> None:
        """Incorporate a new rate measurement.

        Args:
            observed_rate: measured $/M for this billing cycle snapshot.
        """
        # Predict step first
        self.predict_step()

        # Innovation (residual)
        z = np.array([[observed_rate]], dtype=float)
        y = z - self._H @ self._x  # innovation

        # Innovation covariance
        S = self._H @ self._P @ self._H.T + self._R

        # Kalman gain
        K = self._P @ self._H.T @ np.linalg.inv(S)

        # State update
        self._x = self._x + K @ y
        self._P = (np.eye(2) - K @ self._H) @ self._P

        self._updates += 1

    def predict(self) -> float:
        """Return current estimated $/M (smooth base rate)."""
        return max(0.0, self.base_rate)

    def effective_price(
        self,
        peak_mult: float = 1.0,
        scarcity: float = 1.0,
        health: float = 1.0,
        pace_mult: float = 1.0,
    ) -> float:
        """Compute effective price: base × peak × scarcity × health × pace.

        Always returns >= MIN_EFFECTIVE_PRICE (ADR-004).

        Args:
            pace_mult: Predictive quota-pacing multiplier from
                :func:`~src.pricing_engine.pace_factor`. Default 1.0
                (no pace adjustment).
        """
        raw = self.predict() * peak_mult * scarcity * health * pace_mult
        if math.isinf(raw) or math.isnan(raw):
            return float("inf")
        return max(raw, MIN_EFFECTIVE_PRICE)
