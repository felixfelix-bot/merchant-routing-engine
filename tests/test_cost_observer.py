"""Tests for cost_observer — failure-cost observation path for PriceKalman.

Verifies the ADR-008 observation contract:
  - On success: feeds actual_cost = spend_usd / (tokens / 1e6) to PriceKalman
  - On failure: feeds fallback_cost + retry_penalty to PriceKalman
  - Multiple failures converge to expected cost including failure overhead
  - Edge cases: zero tokens, zero spend, no fallback available
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.price_kalman import PriceKalman
from src.cost_observer import CostObserver


# ── Success observations ────────────────────────────────────────────────────


def test_success_feeds_actual_cost():
    """On success, the Kalman should be updated with spend_usd / (tokens/1e6)."""
    pk = PriceKalman(initial_rate=0.50, process_noise=1e-6, measurement_noise=1e-4)
    observer = CostObserver(price_kalmans={"ours": pk})

    # $0.30 for 10M tokens → $0.03/M
    observer.observe_success(
        provider="ours",
        spend_usd=0.30,
        tokens=10_000_000,
    )

    # After one update, the Kalman should have moved toward 0.03
    assert pk.base_rate != pytest.approx(0.50, abs=1e-6)
    assert pk._updates == 1


def test_success_cost_calculation():
    """Verify the actual cost computed is correct."""
    pk = PriceKalman(initial_rate=0.50, process_noise=1e-6, measurement_noise=1e-4)
    observer = CostObserver(price_kalmans={"ours": pk})

    cost = observer.observe_success(
        provider="ours",
        spend_usd=3.00,
        tokens=100_000_000,  # 100M tokens
    )
    # $3.00 / (100M / 1M) = $3.00 / 100 = $0.03/M
    assert cost == pytest.approx(0.03, rel=1e-9)


def test_success_updates_kalman_toward_real_cost():
    """Feed many success observations; Kalman should converge near the true rate."""
    pk = PriceKalman(initial_rate=1.0, process_noise=1e-6, measurement_noise=1e-4)
    observer = CostObserver(price_kalmans={"ours": pk})

    # True cost: $0.31/M (z.ai ours)
    # $3.10 per 10M tokens
    for _ in range(100):
        observer.observe_success(
            provider="ours",
            spend_usd=3.10,
            tokens=10_000_000,
        )

    assert pk.base_rate == pytest.approx(0.31, abs=0.05)


# ── Failure observations ───────────────────────────────────────────────────


def test_failure_feeds_fallback_cost_plus_penalty():
    """On failure, the Kalman should be updated with fallback_cost + retry_penalty."""
    pk = PriceKalman(initial_rate=0.03, process_noise=1e-6, measurement_noise=1e-4)
    fallback_pk = PriceKalman(initial_rate=0.14, process_noise=1e-6, measurement_noise=1e-4)
    observer = CostObserver(
        price_kalmans={"ours": pk, "ppq": fallback_pk},
        retry_penalty=0.01,
    )

    # Record a fallback provider's cost
    observer.record_fallback_cost(
        fallback_provider="ppq",
        spend_usd=1.40,
        tokens=10_000_000,
    )

    cost = observer.observe_failure(
        provider="ours",
        fallback_provider="ppq",
    )

    # fallback cost for ppq = $1.40 / (10M/1M) = $0.14/M
    # + retry_penalty $0.01/M = $0.15/M
    assert cost == pytest.approx(0.15, rel=1e-9)
    assert pk._updates == 1


def test_failure_updates_kalman_toward_fallback_plus_penalty():
    """Kalman for a failing provider should move toward fallback_cost + penalty."""
    pk = PriceKalman(initial_rate=0.03, process_noise=1e-6, measurement_noise=1e-4)
    fallback_pk = PriceKalman(initial_rate=0.14)
    observer = CostObserver(
        price_kalmans={"ours": pk, "ppq": fallback_pk},
        retry_penalty=0.01,
    )

    # Tell observer the fallback cost
    observer.record_fallback_cost(
        fallback_provider="ppq",
        spend_usd=1.40,
        tokens=10_000_000,
    )

    # Feed 50 failures
    for _ in range(50):
        observer.observe_failure(
            provider="ours",
            fallback_provider="ppq",
        )

    # Should converge near 0.15 ($0.14 + $0.01)
    assert pk.base_rate == pytest.approx(0.15, abs=0.02)


def test_failure_no_fallback_uses_default_penalty():
    """When no fallback is available, should use default fallback cost + penalty."""
    pk = PriceKalman(initial_rate=0.03, process_noise=1e-6, measurement_noise=1e-4)
    observer = CostObserver(
        price_kalmans={"ours": pk},
        retry_penalty=0.01,
        default_fallback_cost=0.50,  # $0.50/M for unknown fallback
    )

    cost = observer.observe_failure(
        provider="ours",
        fallback_provider=None,
    )

    # default_fallback_cost + retry_penalty
    assert cost == pytest.approx(0.51, rel=1e-9)
    assert pk._updates == 1


def test_failure_unknown_fallback_uses_default():
    """When fallback provider is not in tracked costs, use default."""
    pk = PriceKalman(initial_rate=0.03, process_noise=1e-6, measurement_noise=1e-4)
    observer = CostObserver(
        price_kalmans={"ours": pk},
        retry_penalty=0.01,
        default_fallback_cost=0.50,
    )

    cost = observer.observe_failure(
        provider="ours",
        fallback_provider="some_unknown_provider",
    )

    assert cost == pytest.approx(0.51, rel=1e-9)


# ── Convergence: mixed success/failure ─────────────────────────────────────


def test_mixed_success_failure_converges_to_expected_cost():
    """A key with 50% failure rate should converge to the true expected cost:
    0.5 * success_cost + 0.5 * (fallback_cost + penalty).

    success_cost = $0.03/M
    fallback_cost = $0.14/M (PPQ)
    penalty = $0.01/M
    expected = 0.5 * 0.03 + 0.5 * 0.15 = 0.085/M
    """
    pk = PriceKalman(initial_rate=0.50, process_noise=1e-6, measurement_noise=1e-4)
    fallback_pk = PriceKalman(initial_rate=0.14)
    observer = CostObserver(
        price_kalmans={"ours": pk, "ppq": fallback_pk},
        retry_penalty=0.01,
    )

    # Record fallback cost
    observer.record_fallback_cost(
        fallback_provider="ppq",
        spend_usd=1.40,
        tokens=10_000_000,
    )

    # Alternate success and failure (50% failure rate)
    for i in range(100):
        if i % 2 == 0:
            observer.observe_success(
                provider="ours",
                spend_usd=0.30,
                tokens=10_000_000,  # $0.03/M
            )
        else:
            observer.observe_failure(
                provider="ours",
                fallback_provider="ppq",
            )

    # Expected: 0.5 * 0.03 + 0.5 * 0.15 = 0.085
    assert pk.base_rate == pytest.approx(0.085, abs=0.02)


def test_chronic_failure_makes_provider_expensive():
    """A provider with high failure rate should have higher Kalman base rate
    than a provider with low failure rate, given same success cost."""
    # Provider A: 90% success rate
    pk_a = PriceKalman(initial_rate=0.50, process_noise=1e-6, measurement_noise=1e-4)
    # Provider B: 10% success rate
    pk_b = PriceKalman(initial_rate=0.50, process_noise=1e-6, measurement_noise=1e-4)
    fallback_pk = PriceKalman(initial_rate=0.14)

    observer_a = CostObserver(
        price_kalmans={"a": pk_a, "ppq": fallback_pk},
        retry_penalty=0.01,
    )
    observer_b = CostObserver(
        price_kalmans={"b": pk_b, "ppq": fallback_pk},
        retry_penalty=0.01,
    )

    for obs in [observer_a, observer_b]:
        obs.record_fallback_cost(
            fallback_provider="ppq",
            spend_usd=1.40,
            tokens=10_000_000,
        )

    for i in range(100):
        # Provider A: 90% success
        if i % 10 < 9:
            observer_a.observe_success("a", spend_usd=0.30, tokens=10_000_000)
        else:
            observer_a.observe_failure("a", fallback_provider="ppq")

        # Provider B: 10% success
        if i % 10 < 1:
            observer_b.observe_success("b", spend_usd=0.30, tokens=10_000_000)
        else:
            observer_b.observe_failure("b", fallback_provider="ppq")

    # B should be more expensive than A
    assert pk_b.base_rate > pk_a.base_rate


# ── Edge cases ──────────────────────────────────────────────────────────────


def test_zero_tokens_success_no_update():
    """When tokens=0 on success, should not update Kalman (can't compute cost)."""
    pk = PriceKalman(initial_rate=0.50)
    observer = CostObserver(price_kalmans={"ours": pk})

    cost = observer.observe_success(
        provider="ours",
        spend_usd=0.30,
        tokens=0,
    )

    assert cost is None
    assert pk._updates == 0


def test_zero_spend_success_feeds_zero():
    """When spend=0 but tokens>0 (free provider), cost is $0/M."""
    pk = PriceKalman(initial_rate=0.50, process_noise=1e-6, measurement_noise=1e-4)
    observer = CostObserver(price_kalmans={"ours": pk})

    cost = observer.observe_success(
        provider="ours",
        spend_usd=0.0,
        tokens=10_000_000,
    )

    assert cost == pytest.approx(0.0, abs=1e-12)
    assert pk._updates == 1


def test_no_fallback_available():
    """When fallback_provider is None and no default is set, should still
    feed retry_penalty alone (or a configured default)."""
    pk = PriceKalman(initial_rate=0.03, process_noise=1e-6, measurement_noise=1e-4)
    observer = CostObserver(
        price_kalmans={"ours": pk},
        retry_penalty=0.01,
        default_fallback_cost=0.0,  # no fallback → zero fallback cost
    )

    cost = observer.observe_failure(
        provider="ours",
        fallback_provider=None,
    )

    # With no fallback: 0.0 + 0.01 = 0.01
    assert cost == pytest.approx(0.01, rel=1e-9)
    assert pk._updates == 1


def test_unknown_provider_no_crash():
    """Observing a provider not in the kalmans dict should not crash."""
    observer = CostObserver(price_kalmans={"ours": PriceKalman()})

    # Should silently return None, not raise
    cost = observer.observe_success(
        provider="nonexistent",
        spend_usd=1.0,
        tokens=1_000_000,
    )
    assert cost is None

    cost = observer.observe_failure(
        provider="nonexistent",
        fallback_provider=None,
    )
    assert cost is None


def test_record_fallback_cost_unknown_provider():
    """Recording fallback cost for unknown provider should not crash."""
    observer = CostObserver(price_kalmans={"ours": PriceKalman()})

    # Should silently store the cost even if provider isn't in kalmans
    observer.record_fallback_cost(
        fallback_provider="unknown_provider",
        spend_usd=1.0,
        tokens=10_000_000,
    )

    cost = observer.observe_failure(
        provider="ours",
        fallback_provider="unknown_provider",
    )

    # Should use the recorded cost
    assert cost == pytest.approx(0.11, rel=1e-9)  # 0.10 + 0.01 penalty


def test_record_fallback_cost_zero_tokens():
    """Recording fallback cost with zero tokens should store nothing."""
    observer = CostObserver(
        price_kalmans={"ours": PriceKalman()},
        retry_penalty=0.01,
    )

    observer.record_fallback_cost(
        fallback_provider="ppq",
        spend_usd=1.0,
        tokens=0,
    )

    # Should have no recorded cost for ppq
    assert "ppq" not in observer.fallback_costs


def test_get_fallback_cost():
    """get_fallback_cost should return recorded cost or None."""
    observer = CostObserver(
        price_kalmans={"ours": PriceKalman()},
        retry_penalty=0.01,
    )

    observer.record_fallback_cost(
        fallback_provider="ppq",
        spend_usd=1.40,
        tokens=10_000_000,
    )

    assert observer.get_fallback_cost("ppq") == pytest.approx(0.14, rel=1e-9)
    assert observer.get_fallback_cost("unknown") is None


# ── Stats and inspection ───────────────────────────────────────────────────


def test_stats_tracks_observations():
    """CostObserver should track observation counts."""
    pk = PriceKalman(initial_rate=0.50)
    fallback_pk = PriceKalman(initial_rate=0.14)
    observer = CostObserver(
        price_kalmans={"ours": pk, "ppq": fallback_pk},
        retry_penalty=0.01,
    )

    observer.record_fallback_cost("ppq", spend_usd=1.40, tokens=10_000_000)

    observer.observe_success("ours", spend_usd=0.30, tokens=10_000_000)
    observer.observe_success("ours", spend_usd=0.30, tokens=10_000_000)
    observer.observe_failure("ours", fallback_provider="ppq")

    stats = observer.stats
    assert stats["success_observations"] == 2
    assert stats["failure_observations"] == 1
    assert stats["total_observations"] == 3


def test_retry_penalty_configurable():
    """Retry penalty should be configurable per CostObserver instance."""
    pk1 = PriceKalman(initial_rate=0.03, process_noise=1e-6, measurement_noise=1e-4)
    pk2 = PriceKalman(initial_rate=0.03, process_noise=1e-6, measurement_noise=1e-4)

    obs1 = CostObserver(
        price_kalmans={"ours": pk1},
        retry_penalty=0.01,
        default_fallback_cost=0.14,
    )
    obs2 = CostObserver(
        price_kalmans={"ours": pk2},
        retry_penalty=0.05,
        default_fallback_cost=0.14,
    )

    cost1 = obs1.observe_failure("ours", fallback_provider=None)
    cost2 = obs2.observe_failure("ours", fallback_provider=None)

    assert cost1 == pytest.approx(0.15, rel=1e-9)  # 0.14 + 0.01
    assert cost2 == pytest.approx(0.19, rel=1e-9)  # 0.14 + 0.05


def test_default_retry_penalty():
    """Default retry penalty should be $0.01/M per ADR-008."""
    observer = CostObserver(price_kalmans={"ours": PriceKalman()})
    assert observer.retry_penalty == pytest.approx(0.01, rel=1e-9)