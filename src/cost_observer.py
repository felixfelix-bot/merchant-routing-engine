"""cost_observer.py — Failure-cost observation path for PriceKalman (ADR-008).

Implements the hybrid observation contract from ADR-008 §"The Hybrid":

    On success: kalman.update(actual_cost)                    # spend_usd / (tokens / 1e6)
    On failure: kalman.update(fallback_cost + retry_penalty) # true cost of failed attempt

The Kalman learns the TRUE expected cost of a provider, including failure
overhead. A key with 50% failure rate at $0.03/M success cost and $0.14/M
fallback cost converges to $0.085/M — the real expected cost per attempt.

The deterministic health MULTIPLIER (ADR-003) handles ACUTE failure (instant
step change). The Kalman observation handles CHRONIC failure (learned trend).

This module is designed to be used by PrimaryRouter and ShadowHook:

    observer = CostObserver(price_kalmans=router._price_kalmans, retry_penalty=0.01)

    # After a successful request:
    observer.observe_success(provider="ours", spend_usd=0.30, tokens=10_000_000)

    # After a failed request that fell back:
    observer.record_fallback_cost(fallback_provider="ppq", spend_usd=1.40, tokens=10_000_000)
    observer.observe_failure(provider="ours", fallback_provider="ppq")
"""
from __future__ import annotations

import os
import sys
from typing import Any

_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from src.price_kalman import PriceKalman
from src.provider_names import normalize_provider_name

__all__ = ["CostObserver"]


#: Default retry penalty in $/M — latency overhead for a wasted attempt.
#: Per ADR-008: "$0.01/M for latency/wasted compute".
DEFAULT_RETRY_PENALTY: float = 0.01

#: Default fallback cost when no fallback provider data is available.
#: A conservative estimate of external failover cost.
DEFAULT_FALLBACK_COST: float = 0.50


class CostObserver:
    """Feeds real cost observations to PriceKalman instances per ADR-008.

    On success, computes the actual cost ($/M) from spend and token count,
    and feeds it to the provider's PriceKalman. On failure, feeds the
    fallback provider's cost plus a retry penalty, so the Kalman learns
    the true expected cost including failure overhead.

    Attributes:
        price_kalmans: Dict of provider name → PriceKalman instance.
        retry_penalty: $/M penalty added on failure observations.
        default_fallback_cost: $/M used when no fallback data is available.
        fallback_costs: Tracked per-provider fallback costs ($/M).
    """

    def __init__(
        self,
        price_kalmans: dict[str, PriceKalman],
        retry_penalty: float = DEFAULT_RETRY_PENALTY,
        default_fallback_cost: float = DEFAULT_FALLBACK_COST,
    ) -> None:
        """Initialize the cost observer.

        Args:
            price_kalmans: Dict mapping canonical provider names to their
                PriceKalman instances. These are the same instances used
                by PrimaryRouter / ShadowHook, so observations persist.
            retry_penalty: $/M penalty for failure overhead (latency,
                wasted compute). Default $0.01/M per ADR-008.
            default_fallback_cost: $/M used when a failure has no recorded
                fallback cost. Default $0.50/M (conservative external cost).
        """
        self._price_kalmans: dict[str, PriceKalman] = price_kalmans
        self._retry_penalty: float = float(retry_penalty)
        self._default_fallback_cost: float = float(default_fallback_cost)

        # Per-provider tracked fallback costs ($/M)
        self._fallback_costs: dict[str, float] = {}

        # Observation counters for stats
        self._success_count: int = 0
        self._failure_count: int = 0

    # ── Properties ───────────────────────────────────────────────────────

    @property
    def retry_penalty(self) -> float:
        """Retry penalty in $/M."""
        return self._retry_penalty

    @property
    def default_fallback_cost(self) -> float:
        """Default fallback cost in $/M when no fallback data is available."""
        return self._default_fallback_cost

    @property
    def fallback_costs(self) -> dict[str, float]:
        """Read-only view of tracked per-provider fallback costs ($/M)."""
        return dict(self._fallback_costs)

    @property
    def stats(self) -> dict[str, Any]:
        """Return observation statistics for monitoring."""
        return {
            "success_observations": self._success_count,
            "failure_observations": self._failure_count,
            "total_observations": self._success_count + self._failure_count,
            "tracked_fallbacks": list(self._fallback_costs.keys()),
        }

    # ── Fallback cost tracking ──────────────────────────────────────────

    def record_fallback_cost(
        self,
        fallback_provider: str,
        spend_usd: float,
        tokens: int,
    ) -> float | None:
        """Record the actual cost of a fallback provider.

        Call this when a fallback provider successfully handles a request
        that the primary provider failed on. The recorded cost is used
        by observe_failure() to compute the true cost of the failed attempt.

        Args:
            fallback_provider: Name of the fallback provider (normalized).
            spend_usd: How much the fallback provider charged for this request.
            tokens: Total tokens consumed by the fallback request.

        Returns:
            The computed $/M cost, or None if tokens is zero.
        """
        if tokens <= 0:
            return None

        provider = normalize_provider_name(fallback_provider)
        cost_per_m = spend_usd / (tokens / 1_000_000.0)
        self._fallback_costs[provider] = cost_per_m
        return cost_per_m

    def get_fallback_cost(self, fallback_provider: str) -> float | None:
        """Get the recorded fallback cost for a provider.

        Args:
            fallback_provider: Provider name (normalized internally).

        Returns:
            The $/M cost if recorded, None otherwise.
        """
        provider = normalize_provider_name(fallback_provider)
        return self._fallback_costs.get(provider)

    # ── Success observation ─────────────────────────────────────────────

    def observe_success(
        self,
        provider: str,
        spend_usd: float,
        tokens: int,
    ) -> float | None:
        """Feed a success observation to the provider's PriceKalman.

        Computes actual_cost = spend_usd / (tokens / 1e6) and calls
        price_kalman.update(actual_cost).

        Args:
            provider: Provider name (normalized internally).
            spend_usd: Dollar amount spent for this request.
            tokens: Total tokens consumed.

        Returns:
            The actual cost in $/M that was fed to the Kalman, or None
            if the observation was skipped (zero tokens or unknown provider).
        """
        if tokens <= 0:
            return None

        provider = normalize_provider_name(provider)
        pk = self._price_kalmans.get(provider)
        if pk is None:
            return None

        actual_cost = spend_usd / (tokens / 1_000_000.0)
        pk.update(actual_cost)
        self._success_count += 1
        return actual_cost

    # ── Failure observation ─────────────────────────────────────────────

    def observe_failure(
        self,
        provider: str,
        fallback_provider: str | None,
    ) -> float | None:
        """Feed a failure observation to the provider's PriceKalman.

        Computes the observed cost as:
            fallback_cost + retry_penalty

        Where fallback_cost is the recorded $/M of the fallback provider
        (or default_fallback_cost if no data is available).

        This makes the Kalman learn the true expected cost including
        failure overhead. A chronically failing key becomes progressively
        more expensive in the Kalman's smoothed estimate.

        Args:
            provider: Provider name that failed (normalized internally).
            fallback_provider: Name of the provider that handled the
                request as fallback, or None if no fallback was used.

        Returns:
            The observed cost in $/M fed to the Kalman, or None if the
            provider is unknown.
        """
        provider = normalize_provider_name(provider)
        pk = self._price_kalmans.get(provider)
        if pk is None:
            return None

        # Determine fallback cost
        fallback_cost = self._default_fallback_cost
        if fallback_provider is not None:
            fb = normalize_provider_name(fallback_provider)
            recorded = self._fallback_costs.get(fb)
            if recorded is not None:
                fallback_cost = recorded
            # If not recorded, use default_fallback_cost

        observed_cost = fallback_cost + self._retry_penalty
        pk.update(observed_cost)
        self._failure_count += 1
        return observed_cost