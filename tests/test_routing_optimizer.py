"""Tests for routing_optimizer — deterministic cost minimizer.

Verifies the filter pipeline from ADR-003/004/005:
  - Cheapest viable provider is selected when several are available
  - Exhausted providers (will_exhaust + insufficient quota) are filtered
  - Circuit-breaker providers get infinite effective price → filtered
  - Low-tier models are filtered for high-difficulty tasks
  - All-exhausted → fallback model is chosen
  - Effective price is ALWAYS > 0 (ADR-004 positivity invariant)
  - A human-readable reason string is always returned
  - Peak hours correctly triple the z.ai effective price (ADR-003 step fn)
"""
from __future__ import annotations

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.price_kalman import PriceKalman
from src.consumption_kalman import ConsumptionKalman
from src.routing_optimizer import RoutingOptimizer


# ── Helpers ──────────────────────────────────────────────────────────────────


def _pk(rate: float) -> PriceKalman:
    """A PriceKalman seeded at ``rate`` with one confirming observation."""
    kf = PriceKalman(initial_rate=rate, process_noise=1e-6, measurement_noise=1e-4)
    kf.update(rate)
    return kf


def _ck() -> ConsumptionKalman:
    """A fresh, uninitialized ConsumptionKalman — predicts no exhaustion."""
    return ConsumptionKalman()


# ── 1. Cheapest viable provider selected ─────────────────────────────────────


def test_cheapest_viable_provider_selected():
    opt = RoutingOptimizer()
    opt.add_provider("cheap", _pk(0.01), _ck(), quota_remaining=1_000_000)
    opt.add_provider("pricey", _pk(0.50), _ck(), quota_remaining=1_000_000)

    result = opt.route(difficulty="medium")

    assert result["chosen_provider"] == "cheap"
    # Cheaper provider's effective cost must be below the pricey one.
    cheap_price = result["effective_cost_per_1m"]
    pricey_price = next(
        c["price"] for c in result["candidates"] if c["provider"] == "pricey"
    )
    assert cheap_price < pricey_price


# ── 2. Exhausted providers filtered out ──────────────────────────────────────


def test_exhausted_providers_filtered():
    opt = RoutingOptimizer()
    # Cheap but exhausted (quota=0 → will_exhaust=True, 0 < tokens).
    opt.add_provider("cheap_dead", _pk(0.001), _ck(), quota_remaining=0)
    # Pricey but alive.
    opt.add_provider("pricey_alive", _pk(0.50), _ck(), quota_remaining=1_000_000)

    result = opt.route(difficulty="medium")

    assert result["chosen_provider"] == "pricey_alive"
    dead = next(c for c in result["candidates"] if c["provider"] == "cheap_dead")
    assert dead["viable"] is False


# ── 3. Circuit-breaker providers get infinite price ──────────────────────────


def test_circuit_breaker_provider_filtered():
    opt = RoutingOptimizer()
    opt.add_provider(
        "cheap_broken", _pk(0.001), _ck(),
        quota_remaining=1_000_000, breaker_tripped=True,
    )
    opt.add_provider("pricey_healthy", _pk(0.50), _ck(), quota_remaining=1_000_000)

    result = opt.route(difficulty="medium")

    assert result["chosen_provider"] == "pricey_healthy"
    broken = next(
        c for c in result["candidates"] if c["provider"] == "cheap_broken"
    )
    assert broken["viable"] is False
    assert math.isinf(broken["price"])


# ── 4. Low-tier models filtered for high-difficulty tasks ────────────────────


def test_low_tier_filtered_for_high_difficulty():
    opt = RoutingOptimizer()
    opt.add_provider(
        "cheap_low", _pk(0.001), _ck(),
        quota_remaining=1_000_000, model_tier="low",
    )
    opt.add_provider(
        "pricey_high", _pk(0.50), _ck(),
        quota_remaining=1_000_000, model_tier="high",
    )

    result = opt.route(difficulty="high")

    assert result["chosen_provider"] == "pricey_high"
    low = next(c for c in result["candidates"] if c["provider"] == "cheap_low")
    assert low["viable"] is False

    # But for low difficulty the cheap provider becomes viable.
    result_low = opt.route(difficulty="low")
    assert result_low["chosen_provider"] == "cheap_low"


# ── 5. All providers exhausted → fallback ────────────────────────────────────


def test_all_exhausted_returns_fallback():
    opt = RoutingOptimizer()
    opt.add_provider("a", _pk(0.01), _ck(), quota_remaining=0)
    opt.add_provider("b", _pk(0.02), _ck(), quota_remaining=0)

    result = opt.route(difficulty="medium")

    assert result["chosen_provider"] == "fallback"
    assert isinstance(result["chosen_model"], str)
    assert result["chosen_model"]


# ── 6. Effective price always > 0 ────────────────────────────────────────────


def test_effective_price_always_positive():
    opt = RoutingOptimizer()
    opt.add_provider("p", _pk(0.068), _ck(), quota_remaining=1_000_000)

    result = opt.route(difficulty="medium")

    assert result["effective_cost_per_1m"] > 0


def test_effective_price_positive_for_free_provider():
    """A zero-cost provider still returns > 0 (ADR-004 floor)."""
    opt = RoutingOptimizer()
    free = PriceKalman(initial_rate=0.0)
    free.update(0.0)
    opt.add_provider("free", free, _ck(), quota_remaining=1_000_000)

    result = opt.route(difficulty="medium")

    assert result["effective_cost_per_1m"] > 0


# ── 7. Reason string always returned ─────────────────────────────────────────


def test_returns_reason_string():
    opt = RoutingOptimizer()
    opt.add_provider("p", _pk(0.068), _ck(), quota_remaining=1_000_000)

    result = opt.route(difficulty="medium")

    assert isinstance(result["reason"], str)
    assert result["reason"]  # non-empty


def test_fallback_returns_reason_string():
    opt = RoutingOptimizer()
    opt.add_provider("dead", _pk(0.01), _ck(), quota_remaining=0)

    result = opt.route(difficulty="medium")

    assert result["chosen_provider"] == "fallback"
    assert isinstance(result["reason"], str)
    assert result["reason"]


# ── 8. Peak hours correctly triple z.ai price ────────────────────────────────


def test_peak_hours_triple_price():
    """During peak UTC hours the effective price triples (ADR-003 step fn).

    Uses the configured peak window [6, 10] inclusive with peak_mult=3.0.
    """
    opt = RoutingOptimizer()
    pk = _pk(0.068)  # canonical z.ai off-peak $/M
    opt.add_provider("zai_ours", pk, _ck(), quota_remaining=1_000_000,
                     peak_hours_utc=(6, 10), peak_mult=3.0)

    offpeak = opt.route(difficulty="medium", hour=3)
    peak = opt.route(difficulty="medium", hour=8)

    assert offpeak["chosen_provider"] == "zai_ours"
    assert peak["chosen_provider"] == "zai_ours"
    assert peak["effective_cost_per_1m"] == pytest.approx(
        3.0 * offpeak["effective_cost_per_1m"], rel=1e-9
    )


# ── Extra: candidate list shape ─────────────────────────────────────────────


def test_candidates_list_shape():
    opt = RoutingOptimizer()
    opt.add_provider("a", _pk(0.01), _ck(), quota_remaining=1_000_000)
    opt.add_provider("b", _pk(0.50), _ck(), quota_remaining=0)

    result = opt.route(difficulty="medium")

    assert isinstance(result["candidates"], list)
    assert len(result["candidates"]) == 2
    for c in result["candidates"]:
        assert set(c.keys()) >= {"provider", "price", "viable", "reason"}
