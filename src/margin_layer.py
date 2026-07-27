"""margin_layer.py — Profit-maximising price optimizer (ADR-005 Layer 2).

Deterministic (NO Kalman) module that computes the profit-maximising price
given a demand estimate (from DemandKalman) and upstream cost (from
PriceKalman / Layer 1).

    profit(price) = demand(price) * (price - upstream_cost)

For linear demand d(p) = a + b*p  (b < 0 typically):

    profit = (a + b*p)(p - c) = a*p - a*c + b*p^2 - b*c*p
    d/dp = a + 2*b*p - b*c = 0
    p* = (b*c - a) / (2*b) = c/2 - a/(2*b)

Equivalently, with demand d(p) = a - b*|p| (b > 0):
    p* = (a + b*c) / (2*b)

The module also considers competitor prices: the announced price is capped
at the cheapest competitor to remain competitive in the marketplace.

This is Layer 2 of the three-layer architecture (ADR-005).  It is the
decision layer between cost tracking (Layer 1) and routing (Layer 3).
"""
from __future__ import annotations

import math

from src.demand_kalman import DemandKalman

__all__ = [
    "compute_profit",
    "optimal_price_linear",
    "compute_optimal_price",
    "MarginLayer",
    "MIN_PRICE",
    "MAX_PRICE",
]

# ── Price bounds ────────────────────────────────────────────────────────────

MIN_PRICE: float = 0.001   # floor — never announce a price below this
MAX_PRICE: float = 1e6     # ceiling — sanity bound to avoid runaway values


# ── Pure functions ──────────────────────────────────────────────────────────


def compute_profit(
    price: float,
    intercept: float,
    slope: float,
    cost: float,
) -> float:
    """Compute profit at a given price.

    profit = demand(price) * (price - cost)
           = (intercept + slope * price) * (price - cost)

    Can be negative (selling below cost, or demand is negative at that price).
    """
    demand = intercept + slope * float(price)
    return demand * (float(price) - float(cost))


def optimal_price_linear(
    intercept: float,
    slope: float,
    cost: float,
    min_price: float = MIN_PRICE,
    max_price: float = MAX_PRICE,
) -> float:
    """Analytically solve for the profit-maximising price.

    For demand d(p) = intercept + slope * p:

        profit = d(p) * (p - c)
        d/dp = slope * (p - c) + d(p) = slope*p - slope*c + intercept + slope*p
             = 2*slope*p + intercept - slope*c
        Setting to 0:
        p* = (slope*c - intercept) / (2*slope)
           = (intercept + slope*c) / (2*slope)    [rearranged]
           = c/2 - intercept/(2*slope)             [alternative form]

    For negative slope (the normal case), this gives a maximum.
    For positive slope (unusual), the critical point is a minimum, so the
    optimum lies at the boundary (max_price).

    The result is clamped to [min_price, max_price].

    Args:
        intercept: Demand intercept (demand at zero price).
        slope:     Demand slope (typically negative).
        cost:      Upstream cost per unit.
        min_price: Lower bound on announced price.
        max_price: Upper bound on announced price.

    Returns:
        Optimal price, clamped to [min_price, max_price].
    """
    s = float(slope)
    a = float(intercept)
    c = float(cost)

    # Degenerate case: slope ≈ 0 (perfectly inelastic demand)
    # profit = a * (p - c) → increases without bound if a > 0
    # → return max_price (charge as much as possible)
    if abs(s) < 1e-12:
        if a > 0:
            return max_price
        return min_price

    p_star = (s * c - a) / (2.0 * s)
    # Equivalent: p_star = (a + s * c) / (2 * s) ... let's verify:
    # (s*c - a) / (2*s) = c/2 - a/(2*s)
    # (a + s*c) / (2*s) = a/(2*s) + c/2
    # These are the same: c/2 - a/(2*s) vs a/(2*s) + c/2
    # Wait: (s*c - a)/(2*s) = sc/(2s) - a/(2s) = c/2 - a/(2s)
    #        (a + sc)/(2s) = a/(2s) + sc/(2s) = a/(2s) + c/2
    # So c/2 - a/(2s) ≠ a/(2s) + c/2 unless a/(2s) = 0.
    # The correct derivation:
    # d/dp [(a + s*p)(p - c)] = s*(p-c) + (a + s*p) = s*p - s*c + a + s*p
    #                        = 2*s*p + a - s*c
    # Setting to 0: 2*s*p = s*c - a → p* = (s*c - a) / (2*s)
    # This is correct.

    # For positive slope: the profit function is convex (opens upward),
    # so p_star is a minimum → optimum is at a boundary.
    if s > 0:
        # Convex profit: check both boundaries
        profit_min = compute_profit(min_price, a, s, c)
        profit_max = compute_profit(max_price, a, s, c)
        return max_price if profit_max >= profit_min else min_price

    # For negative slope (normal case): p_star is a maximum.
    # Clamp to bounds.
    return max(min_price, min(p_star, max_price))


def compute_optimal_price(
    intercept: float,
    slope: float,
    upstream_cost: float,
    competitor_prices: list[float] | None = None,
    min_price: float = MIN_PRICE,
    max_price: float = MAX_PRICE,
) -> float:
    """Compute the optimal announced price.

    Combines the analytical optimum with a competitor price cap and
    min/max bounds.

    Args:
        intercept:          Demand intercept.
        slope:              Demand slope (typically negative).
        upstream_cost:      Cost per unit from Layer 1.
        competitor_prices:  List of competitor prices. If provided, the
            announced price is capped at the minimum competitor price to
            remain competitive.
        min_price:          Lower bound.
        max_price:          Upper bound.

    Returns:
        Optimal price, capped at the cheapest competitor and clamped to
        [min_price, max_price].
    """
    # Step 1: analytical optimum
    p_opt = optimal_price_linear(
        intercept, slope, upstream_cost, min_price, max_price
    )

    # Step 2: competitor cap
    if competitor_prices:
        cheapest_competitor = min(competitor_prices)
        p_opt = min(p_opt, cheapest_competitor)

    # Step 3: final clamp
    return max(min_price, min(p_opt, max_price))


# ── MarginLayer (stateful wrapper) ─────────────────────────────────────────


class MarginLayer:
    """Profit-maximising price layer (ADR-005 Layer 2).

    Wraps a DemandKalman instance and provides convenience methods for
    computing optimal prices and profit forecasts.  Contains NO Kalman
    itself — all computation is deterministic given the demand estimate.

    Args:
        demand_kalman:  A DemandKalman instance providing the demand estimate.
        min_price:      Minimum announced price (floor).
        max_price:      Maximum announced price (ceiling).
    """

    def __init__(
        self,
        demand_kalman: DemandKalman,
        min_price: float = MIN_PRICE,
        max_price: float = MAX_PRICE,
    ) -> None:
        self._dkf = demand_kalman
        self.min_price = float(min_price)
        self.max_price = float(max_price)

    @property
    def demand_kalman(self) -> DemandKalman:
        """Underlying DemandKalman instance."""
        return self._dkf

    def optimal_price(
        self,
        upstream_cost: float,
        competitor_prices: list[float] | None = None,
    ) -> float:
        """Compute the profit-maximising announced price.

        Args:
            upstream_cost:      Cost per unit (from Layer 1 / PriceKalman).
            competitor_prices:  Optional list of competitor prices. If provided,
                the announced price is capped at the cheapest competitor.

        Returns:
            Optimal price, clamped to [min_price, max_price] and capped at
            the cheapest competitor.
        """
        return compute_optimal_price(
            intercept=self._dkf.intercept,
            slope=self._dkf.slope,
            upstream_cost=upstream_cost,
            competitor_prices=competitor_prices,
            min_price=self.min_price,
            max_price=self.max_price,
        )

    def profit_at_price(self, price: float, upstream_cost: float) -> float:
        """Forecast profit at a given price using the current demand estimate.

        profit = demand(price) * (price - cost)
        """
        return compute_profit(
            price=price,
            intercept=self._dkf.intercept,
            slope=self._dkf.slope,
            cost=upstream_cost,
        )

    def expected_profit(
        self,
        upstream_cost: float,
        competitor_prices: list[float] | None = None,
    ) -> float:
        """Compute expected profit at the optimal price."""
        p_opt = self.optimal_price(upstream_cost, competitor_prices)
        return self.profit_at_price(p_opt, upstream_cost)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"MarginLayer(demand={self._dkf!r}, "
            f"min_price={self.min_price}, max_price={self.max_price})"
        )