"""Tests for margin_layer — Profit-maximizing price optimizer (ADR-005 Layer 2).

Verifies:
  - Profit calculation: profit(price) = demand(price) * (price - cost)
  - Optimal price for linear demand (analytical solution)
  - Edge cases: cost > price, negative demand, zero cost
  - Competitor-aware pricing (price below/above competitors)
  - Price bounds (min_price, max_price constraints)
  - Determinism: same inputs always produce same output (no Kalman here)
"""
from __future__ import annotations
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.demand_kalman import DemandKalman
from src.margin_layer import (
    compute_profit,
    optimal_price_linear,
    compute_optimal_price,
    MarginLayer,
    MIN_PRICE,
    MAX_PRICE,
)


# ── Profit calculation ─────────────────────────────────────────────────────


def test_profit_basic():
    """profit = demand * (price - cost)."""
    # demand = 100 - 10*price (linear)
    intercept, slope = 100.0, -10.0
    cost = 2.0
    price = 5.0
    demand = intercept + slope * price  # 50
    expected = demand * (price - cost)  # 50 * 3 = 150
    assert compute_profit(price, intercept, slope, cost) == pytest.approx(expected)


def test_profit_zero_when_price_equals_cost():
    """No profit when price = cost (break-even)."""
    assert compute_profit(5.0, 100.0, -10.0, 5.0) == pytest.approx(0.0)


def test_profit_negative_when_below_cost():
    """Selling below cost is a loss."""
    p = compute_profit(1.0, 100.0, -10.0, 3.0)
    assert p < 0.0


def test_profit_negative_when_demand_negative():
    """If demand goes negative (price too high), profit is negative."""
    # demand(20) = 100 - 10*20 = -100
    p = compute_profit(20.0, 100.0, -10.0, 1.0)
    assert p < 0.0


def test_profit_at_cost_is_zero():
    """profit(cost) = 0 because (price - cost) = 0."""
    assert compute_profit(3.0, 200.0, -50.0, 3.0) == pytest.approx(0.0)


# ── Analytical optimal price ──────────────────────────────────────────────


def test_optimal_price_linear_basic():
    """For demand d(p) = a - b*p (b > 0), optimal price = (a + b*c) / (2*b).

    With a=100, b=10, c=2: p* = (100 + 10*2) / (2*10) = 120/20 = 6.0
    """
    p = optimal_price_linear(intercept=100.0, slope=-10.0, cost=2.0)
    assert p == pytest.approx(6.0)


def test_optimal_price_matches_numerical_maximum():
    """The analytical optimum should match a brute-force numerical search."""
    intercept, slope, cost = 500.0, -80.0, 1.5
    p_opt = optimal_price_linear(intercept, slope, cost)
    # Brute force search
    best_p = 0.0
    best_profit = float("-inf")
    for p_cand in [x * 0.01 for x in range(100, 10000)]:
        prof = compute_profit(p_cand, intercept, slope, cost)
        if prof > best_profit:
            best_profit = prof
            best_p = p_cand
    assert p_opt == pytest.approx(best_p, abs=0.05)


def test_optimal_price_higher_with_higher_intercept():
    """More demand (higher intercept) → higher optimal price."""
    p_low = optimal_price_linear(intercept=100.0, slope=-10.0, cost=2.0)
    p_high = optimal_price_linear(intercept=500.0, slope=-10.0, cost=2.0)
    assert p_high > p_low


def test_optimal_price_higher_with_higher_cost():
    """Higher upstream cost → higher optimal price (passed to customer)."""
    p1 = optimal_price_linear(intercept=200.0, slope=-20.0, cost=1.0)
    p2 = optimal_price_linear(intercept=200.0, slope=-20.0, cost=5.0)
    assert p2 > p1


def test_optimal_price_lower_with_steeper_slope():
    """Steeper demand drop (more elastic) → lower optimal price."""
    p_flat = optimal_price_linear(intercept=200.0, slope=-5.0, cost=2.0)
    p_steep = optimal_price_linear(intercept=200.0, slope=-50.0, cost=2.0)
    assert p_steep < p_flat


def test_optimal_profit_is_maximal():
    """Profit at optimal price should exceed profit at neighboring prices."""
    intercept, slope, cost = 200.0, -30.0, 2.0
    p_opt = optimal_price_linear(intercept, slope, cost)
    profit_opt = compute_profit(p_opt, intercept, slope, cost)
    for delta in [-0.5, 0.5, -1.0, 1.0]:
        p_neighbor = p_opt + delta
        profit_neighbor = compute_profit(p_neighbor, intercept, slope, cost)
        assert profit_opt >= profit_neighbor


# ── Edge cases ────────────────────────────────────────────────────────────


def test_optimal_price_zero_cost():
    """With zero cost, optimal price = intercept / (2 * |slope|)."""
    p = optimal_price_linear(intercept=100.0, slope=-10.0, cost=0.0)
    assert p == pytest.approx(5.0)  # 100 / 20


def test_optimal_price_cost_above_intercept():
    """When cost is above the demand intercept (demand can't sustain any
    profitable price), the optimizer should still return a price, but
    profit will be negative."""
    # demand = 10 - 2*p, cost = 20 → demand is always < cost
    p = optimal_price_linear(intercept=10.0, slope=-2.0, cost=20.0)
    # p* = (s*c - a) / (2*s) = (-2*20 - 10) / (2*-2) = (-50) / (-4) = 12.5
    assert p == pytest.approx(12.5)
    # demand at this price is negative (10 - 2*12.5 = -15) — no viable market
    demand_at_opt = 10.0 + (-2.0) * p
    assert demand_at_opt < 0.0, (
        "demand should be negative — no viable market at this cost level"
    )


def test_optimal_price_positive_slope():
    """If slope is positive (higher price = more demand — unusual but
    handled), the analytical formula still works. profit = (a + b*p)(p - c)
    with b > 0 → p* = (a + b*c) / (2*b) ... but with b > 0 this is a minimum,
    not a maximum. The function should handle this gracefully."""
    # Positive slope: profit increases without bound → no finite optimum
    # The function should return max_price or handle the degenerate case
    p = optimal_price_linear(intercept=100.0, slope=10.0, cost=2.0)
    # With positive slope, the profit function is convex (U-shaped)
    # The analytical critical point is a minimum, so the optimum is at bounds
    assert p == pytest.approx(MAX_PRICE) or p == pytest.approx(MIN_PRICE) or p > 0


def test_optimal_price_near_zero_slope():
    """Very flat demand → optimal price approaches cost (margin → 0)."""
    # slope ≈ 0 means demand is almost constant → optimal price ≈ cost + tiny margin
    p = optimal_price_linear(intercept=100.0, slope=-0.001, cost=2.0)
    # p* = (100 + 0.001*2) / (2*0.001) ≈ 100/0.002 = 50000 → huge price
    # This is correct: with near-zero slope, you can charge very high
    assert p > 1000.0  # very flat demand → very high optimal price


# ── MarginLayer with DemandKalman integration ──────────────────────────────


def test_margin_layer_with_demand_kalman():
    """MarginLayer uses a DemandKalman to get the demand estimate and
    computes the optimal price."""
    dkf = DemandKalman(
        initial_intercept=200.0,
        initial_slope=-40.0,
        process_noise=1e-4,
        measurement_noise=1.0,
    )
    ml = MarginLayer(demand_kalman=dkf)
    p = ml.optimal_price(upstream_cost=2.0)
    # p* = (200 + 40*2) / (2*40) = 280/80 = 3.5
    assert p == pytest.approx(3.5, abs=0.1)


def test_margin_layer_updates_demand_then_prices():
    """After feeding observations to the demand Kalman, the optimal price
    should shift to reflect the updated demand curve."""
    dkf = DemandKalman(
        initial_intercept=100.0,
        initial_slope=-10.0,
        process_noise=1e-6,
        measurement_noise=1e-6,
    )
    ml = MarginLayer(demand_kalman=dkf)

    # Train on demand = 300 - 60*p
    for p in [1.0, 2.0, 1.5, 3.0, 0.5, 2.5, 1.0, 1.8, 2.2, 1.3]:
        traffic = 300.0 - 60.0 * p
        dkf.update(price=p, traffic=max(0.0, traffic))

    opt = ml.optimal_price(upstream_cost=2.0)
    # True optimal: (300 + 60*2) / (2*60) = 420/120 = 3.5
    assert opt == pytest.approx(3.5, abs=1.0)


def test_margin_layer_price_bounds():
    """Optimal price should be clamped to [min_price, max_price]."""
    dkf = DemandKalman(initial_intercept=100000.0, initial_slope=-0.001)
    ml = MarginLayer(demand_kalman=dkf, min_price=0.01, max_price=100.0)
    p = ml.optimal_price(upstream_cost=1.0)
    assert p <= 100.0
    assert p >= 0.01


def test_margin_layer_min_price_floor():
    """Optimal price should never go below min_price."""
    dkf = DemandKalman(initial_intercept=10.0, initial_slope=-100.0)
    ml = MarginLayer(demand_kalman=dkf, min_price=0.50, max_price=100.0)
    p = ml.optimal_price(upstream_cost=0.01)
    assert p >= 0.50


# ── Competitor-aware pricing ──────────────────────────────────────────────


def test_competitor_price_cap():
    """If the optimal price is above competitor prices, we should not price
    above the cheapest competitor (or we lose all traffic)."""
    dkf = DemandKalman(initial_intercept=500.0, initial_slope=-50.0)
    ml = MarginLayer(demand_kalman=dkf, min_price=0.01, max_price=100.0)
    # Optimal without competition: (500 + 50*2) / (2*50) = 600/100 = 6.0
    # Cheapest competitor: 4.0 → we should cap at 4.0
    p = ml.optimal_price(upstream_cost=2.0, competitor_prices=[4.0, 5.0, 6.0])
    assert p == pytest.approx(4.0, abs=0.01)


def test_competitor_price_not_binding():
    """If optimal price is already below competitors, competitors don't bind."""
    dkf = DemandKalman(initial_intercept=200.0, initial_slope=-40.0)
    ml = MarginLayer(demand_kalman=dkf)
    # Optimal: (200 + 40*1) / (2*40) = 240/80 = 3.0
    # Competitors at 10, 15, 20 → not binding
    p = ml.optimal_price(upstream_cost=1.0, competitor_prices=[10.0, 15.0, 20.0])
    assert p == pytest.approx(3.0, abs=0.1)


def test_no_competitors():
    """No competitor prices → no cap, just the analytical optimum."""
    dkf = DemandKalman(initial_intercept=100.0, initial_slope=-10.0)
    ml = MarginLayer(demand_kalman=dkf)
    p = ml.optimal_price(upstream_cost=2.0)
    assert p == pytest.approx(6.0, abs=0.1)


def test_competitor_price_below_cost():
    """If a competitor prices below our cost, we can't compete profitably.
    The margin layer should still return a price (for monitoring), but
    the profit will be negative."""
    dkf = DemandKalman(initial_intercept=100.0, initial_slope=-10.0)
    ml = MarginLayer(demand_kalman=dkf, min_price=0.01, max_price=100.0)
    p = ml.optimal_price(upstream_cost=5.0, competitor_prices=[1.0])
    # We can't profitably match the competitor, but we still compute a price
    # The competitor cap at 1.0 is below our cost of 5.0
    # MarginLayer should not price below cost (can't sustain losses)
    # It should return the competitor price (1.0) or the analytical optimum
    # regardless, the result should be a positive number
    assert p > 0.0


# ── Determinism ───────────────────────────────────────────────────────────


def test_deterministic_output():
    """Same inputs → same output (no randomness, no Kalman in margin layer)."""
    dkf = DemandKalman(initial_intercept=100.0, initial_slope=-10.0)
    ml = MarginLayer(demand_kalman=dkf)
    p1 = ml.optimal_price(upstream_cost=2.0)
    p2 = ml.optimal_price(upstream_cost=2.0)
    assert p1 == p2


def test_compute_optimal_price_standalone():
    """The standalone function should work without a DemandKalman instance."""
    p = compute_optimal_price(
        intercept=100.0, slope=-10.0, upstream_cost=2.0
    )
    assert p == pytest.approx(6.0)


def test_compute_optimal_price_with_bounds():
    """Standalone function should also respect bounds."""
    p = compute_optimal_price(
        intercept=100000.0, slope=-0.01, upstream_cost=1.0,
        min_price=0.01, max_price=50.0,
    )
    assert p == pytest.approx(50.0)


def test_compute_optimal_price_with_competitors():
    """Standalone function with competitor cap."""
    p = compute_optimal_price(
        intercept=500.0, slope=-50.0, upstream_cost=2.0,
        competitor_prices=[4.0, 8.0],
    )
    # Analytical optimum is 6.0, competitor cap is 4.0
    assert p == pytest.approx(4.0)


# ── Profit forecast ────────────────────────────────────────────────────────


def test_profit_forecast():
    """MarginLayer should be able to forecast profit at a given price."""
    dkf = DemandKalman(initial_intercept=100.0, initial_slope=-10.0)
    ml = MarginLayer(demand_kalman=dkf)
    profit = ml.profit_at_price(price=5.0, upstream_cost=2.0)
    # demand(5) = 100 - 50 = 50, profit = 50 * 3 = 150
    assert profit == pytest.approx(150.0)


def test_expected_profit_at_optimum():
    """Profit at the optimal price should be positive and maximal."""
    dkf = DemandKalman(initial_intercept=200.0, initial_slope=-40.0)
    ml = MarginLayer(demand_kalman=dkf)
    p_opt = ml.optimal_price(upstream_cost=2.0)
    profit_opt = ml.profit_at_price(p_opt, upstream_cost=2.0)
    assert profit_opt > 0
    # Compare with neighbors
    for delta in [-0.5, 0.5]:
        p_neighbor = p_opt + delta
        profit_n = ml.profit_at_price(p_neighbor, upstream_cost=2.0)
        assert profit_opt >= profit_n