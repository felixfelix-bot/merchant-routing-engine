"""demand_kalman.py — Demand-curve Kalman filter (ADR-005 Layer 2).

Estimates the demand curve from (price, traffic) observations using a 2-state
Kalman filter.  The REUSABLE CORE for all marketplace applications — whether
selling API tokens (TollGate), internet access (Routster), or products (Nostr
webshops), the same math estimates the demand curve.

State vector: [slope, intercept]
    demand(price) = slope * price + intercept

Convention: slope is typically negative (higher price → lower traffic).

The demand curve is assumed to be slowly-varying — process noise is small so
the filter tracks gradual shifts in customer behaviour without overreacting
to noisy traffic observations.

This is the Layer 2 Demand Kalman from ADR-005.  It feeds into margin_layer
which computes the profit-maximising price given the demand estimate and
upstream cost.
"""
from __future__ import annotations

import numpy as np

__all__ = ["DemandKalman"]


class DemandKalman:
    """2-state Kalman filter estimating a linear demand curve.

    State: x = [slope, intercept]^T
        demand(price) = slope * price + intercept

    Observation model: for each (price, traffic) pair, the observed traffic
    is a noisy measurement of demand(price):

        z = H(p) @ x + noise
        H(p) = [p, 1]      (so  H @ [slope, intercept]^T = slope*p + intercept)

    Process model: demand curve is slowly-varying.  F = I (identity), Q small.
    The filter does NOT have a velocity term — the demand curve is assumed
    to drift slowly rather than trend directionally.

    Parameters
    ----------
    initial_intercept:
        Prior estimate of the demand intercept (demand at zero price).
    initial_slope:
        Prior estimate of the demand slope (typically negative).
    process_noise:
        Diagonal of the process-noise covariance Q.  Small = the demand curve
        is expected to drift slowly.  Larger = more reactive to shifts.
    measurement_noise:
        Observation-noise variance R.  Should reflect typical traffic noise.
    """

    def __init__(
        self,
        initial_intercept: float = 0.0,
        initial_slope: float = 0.0,
        process_noise: float = 1e-4,
        measurement_noise: float = 1.0,
    ) -> None:
        # State: [slope, intercept]
        self._x = np.array(
            [[initial_slope], [initial_intercept]], dtype=float
        )

        # State covariance — start moderately uncertain
        self._P = np.eye(2, dtype=float) * 10.0

        # Transition matrix = identity (slowly-varying demand, no velocity)
        self._F = np.eye(2, dtype=float)

        # Process noise (diagonal)
        self._Q = np.eye(2, dtype=float) * process_noise

        # Measurement noise (scalar, applied per observation)
        self._R_base = float(measurement_noise)

        # Store originals for reset()
        self._init_intercept = float(initial_intercept)
        self._init_slope = float(initial_slope)
        self._init_process_noise = float(process_noise)
        self._init_measurement_noise = float(measurement_noise)

        self._update_count = 0

    # ── Properties ───────────────────────────────────────────────────────

    @property
    def intercept(self) -> float:
        """Estimated demand intercept (demand at price = 0)."""
        return float(self._x[1, 0])

    @property
    def slope(self) -> float:
        """Estimated demand slope (change in demand per unit price)."""
        return float(self._x[0, 0])

    @property
    def covariance(self) -> np.ndarray:
        """2×2 state covariance matrix."""
        return self._P.copy()

    @property
    def uncertainty(self) -> float:
        """Overall estimate uncertainty (trace of covariance)."""
        return float(np.trace(self._P))

    @property
    def update_count(self) -> int:
        """Number of observations processed."""
        return self._update_count

    # ── Demand prediction ────────────────────────────────────────────────

    def demand(self, price: float) -> float:
        """Estimate demand at *price*.

        Returns the raw estimate (can be negative for high prices with a
        negative slope — flooring is the caller's responsibility).
        """
        p = float(price)
        return self.slope * p + self.intercept

    def predict_horizon(self, prices: list[float]) -> list[float]:
        """Estimate demand at multiple prices (non-mutating)."""
        return [self.demand(p) for p in prices]

    # ── Kalman update cycle ──────────────────────────────────────────────

    def update(self, price: float, traffic: float) -> None:
        """Incorporate a (price, traffic) observation.

        The observed traffic is a noisy measurement of demand(price).

        Args:
            price:  The price that was charged.
            traffic: The traffic (demand) observed at that price.
        """
        p = float(price)
        z = float(traffic)

        # Observation matrix for this price: H = [price, 1]
        # H @ [slope, intercept]^T = slope * price + intercept = demand(price)
        H = np.array([[p, 1.0]], dtype=float)

        # Measurement noise — can scale with traffic magnitude for
        # heteroscedasticity, but we keep it constant for simplicity.
        R = np.array([[self._R_base]], dtype=float)

        # ── Predict step (time update) ────────────────────────────────
        # F = I, so x_prior = x, P_prior = P + Q
        self._x = self._F @ self._x
        self._P = self._F @ self._P @ self._F.T + self._Q

        # ── Update step (measurement update) ──────────────────────────
        # Innovation (residual): y = z - H @ x_prior
        z_vec = np.array([[z]], dtype=float)
        y = z_vec - H @ self._x

        # Innovation covariance: S = H @ P @ H^T + R
        S = H @ self._P @ H.T + R

        # Kalman gain: K = P @ H^T @ S^{-1}
        K = self._P @ H.T @ np.linalg.inv(S)

        # State + covariance update
        self._x = self._x + K @ y
        I = np.eye(2)
        self._P = (I - K @ H) @ self._P

        self._update_count += 1

    # ── Reset ────────────────────────────────────────────────────────────

    def reset(self) -> None:
        """Reset the filter to its initial configuration."""
        self.__init__(
            initial_intercept=self._init_intercept,
            initial_slope=self._init_slope,
            process_noise=self._init_process_noise,
            measurement_noise=self._init_measurement_noise,
        )

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"DemandKalman(intercept={self.intercept:.4g}, "
            f"slope={self.slope:.4g}, "
            f"uncertainty={self.uncertainty:.4g}, "
            f"updates={self._update_count})"
        )