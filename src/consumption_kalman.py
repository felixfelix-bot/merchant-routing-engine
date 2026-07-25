"""consumption_kalman.py — Provider-agnostic token-burn Kalman filter.

Extracted from the production proxy's KalmanPredictor class (lines 65-138)
and generalised. This is the Consumption Kalman from ADR-002 (Multi-Kalman
Separation): it estimates how fast a provider is burning tokens and predicts
quota exhaustion.

State vector (3-state constant-acceleration model, ADR-002 invariant #2):

    x = [burn_rate, velocity, acceleration]
        burn_rate    = smoothed tokens consumed per period  (directly observed)
        velocity     = first derivative  — trend of burn_rate
        acceleration = second derivative — curvature of the trend

The original production filter was a 2-state local-linear-trend filter
``[volume, velocity]``. Extending to 3-state adds an acceleration term so the
filter can track accelerating/decelerating burn instead of only constant-rate
trends, while remaining a textbook constant-acceleration kinematic model.

A running ``tokens_used`` accumulator is maintained alongside the filter state
so exhaustion logic can reason about cumulative consumption. This honours the
plan's ``[tokens_used, burn_rate, acceleration]`` naming while keeping the
filter itself a clean kinematic estimator.

Scope / invariants (ADR-002):
  - One instance per provider/key. No cross-provider coupling.
  - Knows nothing about price, cost, peak hours, or health — those live in
    ``pricing_engine.py`` as deterministic multipliers applied AFTER this
    filter's output.
  - Standalone: depends only on numpy + stdlib. No external dependencies
    hermes internals, sqlite, or any provider-specific code.

The unit of a "period" (hour, minute, call) is caller-defined — the filter is
unit-agnostic. Pass consistent per-period token counts to ``update()``.
"""
from __future__ import annotations

import numpy as np

__all__ = ["ConsumptionKalman"]


class ConsumptionKalman:
    """Constant-acceleration Kalman filter for token burn-rate prediction.

    One instance per provider/key. Feed per-period token consumption via
    :meth:`update`; query forward predictions via :meth:`predict_horizon` and
    quota-exhaustion checks via :meth:`will_exhaust`.

    Parameters
    ----------
    process_noise:
        Diagonal of the process-noise covariance ``Q``. Higher = the filter
        trusts new measurements more (more reactive, less smoothing).
    measurement_noise:
        Observation-noise variance ``R``. Should be on the order of the
        measurement noise in the caller's units. The filter behaves poorly if
        ``R`` is catastrophically mismatched to the signal magnitude (the
        original code's fixed ``R=50`` against ~1M-token buckets is the
        cautionary tale). Use :meth:`from_history` for auto-tuning.
    dt:
        Period length (default 1.0). All state derivatives are per ``dt``.
    """

    def __init__(
        self,
        process_noise: float = 1.0,
        measurement_noise: float = 1.0,
        dt: float = 1.0,
    ) -> None:
        if dt <= 0:
            raise ValueError(f"dt must be positive, got {dt}")

        # State: [burn_rate, velocity, acceleration]
        self.x = np.array([[0.0], [0.0], [0.0]])

        # Constant-acceleration state transition matrix.
        #   burn_rate_{k+1} = burn_rate_k + velocity_k*dt + 0.5*acc_k*dt^2
        #   velocity_{k+1}  = velocity_k + acc_k*dt
        #   acc_{k+1}       = acc_k
        self.dt = dt
        dt2 = dt * dt
        self.F = np.array(
            [
                [1.0, dt, 0.5 * dt2],
                [0.0, 1.0, dt],
                [0.0, 0.0, 1.0],
            ]
        )

        # Observation: we only measure burn_rate (tokens in the period).
        self.H = np.array([[1.0, 0.0, 0.0]])

        # Covariance — high initial uncertainty, scaled to R so the gain can
        # actually move (mirrors the original's P init).
        self.P = np.eye(3) * measurement_noise

        # Process noise (diagonal). Burn rate can be bursty; keep Q tunable.
        self.Q = np.eye(3) * process_noise

        # Measurement noise.
        self.R = np.array([[measurement_noise]])

        self._initialized = False
        self._update_count = 0
        self._tokens_used = 0.0
        self._last_measurement = 0.0

    # ── Training / observation ────────────────────────────────────────────

    def update(self, measurement: float) -> None:
        """Incorporate a new per-period token-consumption observation.

        Implements the standard predict-update Kalman cycle. The first call
        seeds the state position (no covariance update) so the filter can
        start from a single observation.
        """
        m = float(measurement)
        self._tokens_used += m
        self._last_measurement = m
        self._update_count += 1

        if not self._initialized:
            self.x[0, 0] = m
            self._initialized = True
            return

        # ── Predict step (time update) ───────────────────────────────────
        # Without this, cross-covariance between position and velocity stays
        # zero, and velocity/acceleration never get updated by measurements.
        self.x = self.F @ self.x
        self.P = self.F @ self.P @ self.F.T + self.Q

        # ── Update step (measurement update) ─────────────────────────────
        z = np.array([[m]])
        # Innovation (prediction residual)
        y = z - self.H @ self.x
        # Innovation covariance
        S = self.H @ self.P @ self.H.T + self.R
        # Kalman gain
        K = self.P @ self.H.T @ np.linalg.inv(S)
        # State + covariance update
        self.x = self.x + K @ y
        I = np.eye(3)
        self.P = (I - K @ self.H) @ self.P

    def predict(self) -> float:
        """Advance the state one period (time update). **Mutates** state.

        Returns the predicted burn_rate for the next period. Prefer
        :meth:`predict_horizon` for non-mutating forward queries.
        """
        self.x = self.F @ self.x
        self.P = self.F @ self.P @ self.F.T + self.Q
        return float(self.x[0, 0])

    # ── Forward prediction (non-mutating) ─────────────────────────────────

    def predict_horizon(self, steps: int) -> list[float]:
        """Predict burn_rate for the next ``steps`` periods **without** mutating.

        Projects a constant-acceleration trajectory from the current posterior
        estimate. ``steps <= 0`` returns an empty list. All values are floored
        at 0 (token consumption cannot be negative).
        """
        if steps <= 0:
            return []
        s = float(self.x[0, 0])
        v = float(self.x[1, 0])
        a = float(self.x[2, 0])
        out: list[float] = []
        for n in range(1, steps + 1):
            tn = self.dt * n
            val = s + v * tn + 0.5 * a * tn * tn
            out.append(max(0.0, val))
        return out

    def predict_cumulative(self, steps: int) -> float:
        """Total tokens expected to be burned over the next ``steps`` periods."""
        return float(sum(self.predict_horizon(steps)))

    def will_exhaust(
        self, quota_remaining: float, steps: int
    ) -> tuple[bool, float | None]:
        """Will the provider exhaust ``quota_remaining`` within ``steps`` periods?

        Returns ``(will_exhaust, exhausts_in_periods)`` where
        ``exhausts_in_periods`` is the fractional period index (0-based, measured
        from now) at which cumulative burn crosses the quota, or ``None`` if it
        never crosses within the horizon. Linear interpolation is used inside
        the crossing period for sub-period precision.

        Edge cases:
          - ``quota_remaining <= 0``  → already exhausted → ``(True, 0.0)``
          - no data / uninitialised   → ``(False, None)``
        """
        if quota_remaining <= 0:
            return (True, 0.0)
        if not self._initialized or steps <= 0:
            return (False, None)

        horizon = self.predict_horizon(steps)
        cumulative = 0.0
        for i, burn in enumerate(horizon, start=1):
            cumulative += burn
            if cumulative >= quota_remaining:
                prev = cumulative - burn
                remaining = quota_remaining - prev
                frac = remaining / burn if burn > 0 else 0.0
                return (True, float(i - 1 + frac))
        return (False, None)

    # ── Adaptive factory ───────────────────────────────────────────────

    @classmethod
    def from_history(
        cls,
        observations,
        process_noise: float | None = None,
        measurement_noise: float | None = None,
        dt: float = 1.0,
    ) -> "ConsumptionKalman":
        """Train a filter from a sequence of per-period observations.

        Auto-tunes ``R`` to the empirical observation variance and ``Q`` to a
        small fraction of ``R`` (burn rate should not swing wildly period to
        period). Explicit overrides win over the adaptive estimate. This is
        the extraction of the production proxy's adaptive training path,
        with a unit-agnostic floor (1.0) instead of the production 1e6 floor.
        """
        obs = [float(o) for o in observations]
        n = len(obs)
        if n == 0:
            return cls(dt=dt)

        mean_v = sum(obs) / n
        variance = sum((o - mean_v) ** 2 for o in obs) / max(n - 1, 1)

        if measurement_noise is None:
            measurement_noise = max(variance, 1.0)  # floor prevents collapse
        if process_noise is None:
            process_noise = max(measurement_noise * 1e-3, 1e-9)

        kf = cls(
            process_noise=process_noise,
            measurement_noise=measurement_noise,
            dt=dt,
        )
        for o in obs:
            kf.update(o)
        return kf

    # ── Introspection ─────────────────────────────────────────────────────

    @property
    def tokens_used(self) -> float:
        """Cumulative tokens observed via :meth:`update`."""
        return self._tokens_used

    @property
    def last_measurement(self) -> float:
        return self._last_measurement

    @property
    def burn_rate(self) -> float:
        """Smoothed tokens-per-period estimate (state[0])."""
        return float(self.x[0, 0])

    @property
    def velocity(self) -> float:
        """First derivative of burn_rate (tokens / period^2)."""
        return float(self.x[1, 0])

    @property
    def acceleration(self) -> float:
        """Second derivative of burn_rate (tokens / period^3)."""
        return float(self.x[2, 0])

    @property
    def uncertainty(self) -> float:
        """Standard deviation of the burn_rate estimate (sqrt of P[0,0])."""
        return float(np.sqrt(max(self.P[0, 0], 0.0)))

    @property
    def update_count(self) -> int:
        return self._update_count

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"ConsumptionKalman(burn_rate={self.burn_rate:.3g}, "
            f"velocity={self.velocity:.3g}, acceleration={self.acceleration:.3g}, "
            f"tokens_used={self._tokens_used:.3g}, updates={self._update_count})"
        )
