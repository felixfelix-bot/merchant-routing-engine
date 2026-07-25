"""tests/test_integration.py — End-to-end Phase 1 pipeline tests.

Exercises the full merchant-routing-engine pipeline composed from every
Phase 1 module:

    PriceKalman + ConsumptionKalman  →  RoutingOptimizer  →  ShadowLogger

These tests are deliberately *scenario-driven* (not unit tests). Each scenario
crosses module boundaries to prove the modules compose into the routing engine
described in the ADRs:

    * Effective price = base × peak × scarcity × health (ADR-003)
    * Effective price is ALWAYS > 0 (ADR-004)
    * Tripped breaker → ∞ cost, not 0 (ADR-005)
    * Peak hours multiply cost instantly — a step function, not Kalman-smoothed
    * Shadow logger records both live and optimizer decisions for soak analysis

Scenarios 1–10 below mirror the brief's task list. The local helpers
(`_pk`, `_ck`, `_ck_burning`, `_make_optimizer`) make each test read like a
story rather than a fixture soup.
"""
from __future__ import annotations

import math
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.price_kalman import MIN_EFFECTIVE_PRICE, PriceKalman
from src.consumption_kalman import ConsumptionKalman
from src.routing_optimizer import RoutingOptimizer, TIER_RANK
from src.shadow_logger import ShadowLogger


# ── Shared local helpers ────────────────────────────────────────────────────


def _pk(rate: float) -> PriceKalman:
    """PriceKalman seeded at ``rate`` with one confirming observation.

    Training with a single observation lets the filter settle close to the
    seed rate while keeping the covariance update active.
    """
    kf = PriceKalman(initial_rate=rate, process_noise=1e-6, measurement_noise=1e-4)
    kf.update(rate)
    return kf


def _ck() -> ConsumptionKalman:
    """Fresh, uninitialized ConsumptionKalman — predicts no exhaustion."""
    return ConsumptionKalman()


def _ck_burning(tokens_per_period: float, periods: int = 3) -> ConsumptionKalman:
    """ConsumptionKalman trained on a steady burn so will_exhaust works."""
    kf = ConsumptionKalman()
    for _ in range(periods):
        kf.update(tokens_per_period)
    return kf


def _three_provider_optimizer(hour_offpeak: bool = True) -> RoutingOptimizer:
    """Build a representative 3-provider optimizer matching config/providers.yaml.

    Providers mirror the production topology:
      * zai_ours     — flat-rate, cheapest off-peak, high tier
      * ollama       — flat-rate secondary, standard tier, mid price
      * ppq          — per-token external, low tier (fallback-ish), most $/M
    """
    opt = RoutingOptimizer(peak_hours_utc=(6, 10), peak_mult=3.0)
    opt.add_provider(
        "zai_ours", _pk(0.068), _ck(),
        quota_remaining=1_000_000, model_tier="high",
        quota_total=2_000_000,
    )
    opt.add_provider(
        "ollama_cloud", _pk(0.40), _ck(),
        quota_remaining=500_000, model_tier="standard",
        quota_total=1_000_000,
    )
    opt.add_provider(
        "ppq_external", _pk(0.80), _ck(),
        quota_remaining=10_000_000, model_tier="low",
        quota_total=20_000_000,
    )
    return opt


# ── 1. Full pipeline: 3 providers → route() → valid decision ────────────────


def test_full_pipeline_three_producers_valid_decision():
    """End-to-end: build Kalman-backed providers, route, validate the result.

    Verifies the composed pipeline returns a complete decision dict with
    sensible invariants: chosen provider/model are non-empty strings,
    cost is finite and > 0, all three providers appear in candidates.
    """
    opt = _three_provider_optimizer()

    result = opt.route(difficulty="low", estimated_tokens=10_000, hour=3)

    assert result["chosen_provider"] == "zai_ours"
    assert isinstance(result["chosen_model"], str) and result["chosen_model"]
    assert math.isfinite(result["effective_cost_per_1m"])
    assert result["effective_cost_per_1m"] > 0
    assert isinstance(result["reason"], str) and result["reason"]
    assert len(result["candidates"]) == 3
    # Cheapest provider was picked.
    assert result["chosen_provider"] == "zai_ours"
    # Candidate list is sorted cheapest-first.
    prices = [c["price"] for c in result["candidates"]]
    assert prices == sorted(prices)
    # zai_ours is the cheapest of the three off-peak.
    assert min(prices) == result["effective_cost_per_1m"]


def test_full_pipeline_difficulty_filters_high_tier_only():
    """For high difficulty only the high-tier z.ai provider is viable."""
    opt = _three_provider_optimizer()

    result = opt.route(difficulty="high", hour=3)

    # Only zai_ours qualifies (model_tier="high").
    assert result["chosen_provider"] == "zai_ours"
    viable = {c["provider"] for c in result["candidates"] if c["viable"]}
    assert viable == {"zai_ours"}


# ── 2. Mock API calls through shadow_logger → both decisions logged ─────────


def test_shadow_logger_logs_live_and_shadow_decisions(tmp_path):
    """Feed two mock API calls through ShadowLogger and verify persistence.

    The logger is supposed to record BOTH the live best_key pick and the
    shadow optimizer pick for every API call, so the strategies can be
    compared after a soak period (ADR-Phase-1 shadow mode).
    """
    db = tmp_path / "shadow.db"
    log = ShadowLogger(db_path=str(db))

    # Mock call 1: live picks zai_ours, optimizer picks zai_ours (agree).
    log.log_decision(
        ts=1000.0,
        live_provider="zai_ours", live_model="glm-4.5-air",
        shadow_provider="zai_ours", shadow_model="glm-4.5-air",
        shadow_cost=0.068, tokens=1500, live_cost=0.068,
    )
    # Mock call 2: live picks zai_friend (best_key fallback), optimizer picks ppq.
    log.log_decision(
        ts=1001.0,
        live_provider="zai_friend", live_model="glm-4.5-air",
        shadow_provider="ppq_external", shadow_model="deepseek-v4-flash",
        shadow_cost=0.80, tokens=9000, live_cost=0.21,
    )

    # Both rows persisted.
    rows = log._conn.execute(
        "SELECT live_provider, shadow_provider, agree FROM "
        "routing_shadow_decisions ORDER BY ts;"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0] == ("zai_ours", "zai_ours", 1)
    assert rows[1] == ("zai_friend", "ppq_external", 0)
    log.close()


# ── 3. Peak hours transition: z.ai triples, optimizer switches ──────────────


def test_peak_hours_triples_zai_and_optimizer_switches_provider():
    """During peak, z.ai's price triples; a cheaper standard-tier provider wins.

    Construct: zai_ours ($0.068 off-peak, high tier) vs ollama ($0.20,
    standard tier). Off-peak, zai_ours is cheaper. At peak (×3), zai_ours
    becomes $0.204 — and ollama at $0.20 should win.
    """
    opt = RoutingOptimizer()
    opt.add_provider(
        "zai_ours", _pk(0.068), _ck(),
        quota_remaining=1_000_000, model_tier="high",
        peak_hours_utc=(6, 10), peak_mult=3.0,
    )
    opt.add_provider(
        "ollama_cloud", _pk(0.20), _ck(),
        quota_remaining=1_000_000, model_tier="standard",
    )

    offpeak = opt.route(difficulty="low", hour=3)
    peak = opt.route(difficulty="low", hour=8)

    # Off-peak: zai_ours is the cheaper pick.
    assert offpeak["chosen_provider"] == "zai_ours"

    # Peak: zai_ours tripled to ~$0.204, ollama at $0.20 wins.
    zai_peak = next(
        c["price"] for c in peak["candidates"] if c["provider"] == "zai_ours"
    )
    assert zai_peak == pytest.approx(0.068 * 3.0, rel=1e-6)
    assert peak["chosen_provider"] == "ollama_cloud"
    assert peak["effective_cost_per_1m"] < zai_peak


# ── 4. Provider exhaustion: will_exhaust=True filters it out ────────────────


def test_will_exhaust_provider_filtered_out():
    """A provider whose ConsumptionKalman predicts exhaustion is filtered.

    Construct a provider burning 100k tokens/period with only 50k quota
    remaining. The exhaustion horizon (default 1) predicts burn > quota →
    filtered; the next-cheapest provider is selected instead.
    """
    opt = RoutingOptimizer(exhaustion_horizon=1)
    opt.add_provider(
        "cheap_exhausting", _pk(0.01),
        _ck_burning(tokens_per_period=100_000, periods=4),
        quota_remaining=50_000,  # less than one period's predicted burn
        model_tier="high",
    )
    opt.add_provider(
        "pricey_alive", _pk(0.50), _ck(),
        quota_remaining=1_000_000, model_tier="high",
    )

    result = opt.route(difficulty="medium", estimated_tokens=100_000, hour=3)

    assert result["chosen_provider"] == "pricey_alive"
    exhausted = next(
        c for c in result["candidates"] if c["provider"] == "cheap_exhausting"
    )
    assert exhausted["viable"] is False
    assert "exhaust" in exhausted["reason"].lower()


# ── 5. Circuit breaker: tripped → ∞ price → filtered ────────────────────────


def test_circuit_breaker_infinity_price_filtered():
    """A tripped circuit breaker makes a provider unreachable (ADR-005).

    The breaker yields ∞ cost (NOT zero), so the provider is filtered and
    cannot be picked even if its base rate is the lowest.
    """
    opt = RoutingOptimizer()
    opt.add_provider(
        "zai_broken", _pk(0.001), _ck(),
        quota_remaining=1_000_000, breaker_tripped=True, model_tier="high",
    )
    opt.add_provider(
        "ollama_healthy", _pk(0.50), _ck(),
        quota_remaining=1_000_000, model_tier="high",
    )

    result = opt.route(difficulty="medium", hour=3)

    assert result["chosen_provider"] == "ollama_healthy"
    broken = next(
        c for c in result["candidates"] if c["provider"] == "zai_broken"
    )
    assert broken["viable"] is False
    assert math.isinf(broken["price"])


# ── 6. All providers exhausted → fallback returned ──────────────────────────


def test_all_providers_exhausted_returns_fallback():
    """When no provider is viable, the external fallback model is chosen."""
    opt = RoutingOptimizer()
    opt.add_provider(
        "a", _pk(0.01), _ck_burning(100_000, 4),
        quota_remaining=0, model_tier="high",
    )
    opt.add_provider(
        "b", _pk(0.02), _ck_burning(100_000, 4),
        quota_remaining=0, model_tier="standard",
    )

    result = opt.route(difficulty="medium", estimated_tokens=10_000, hour=3)

    assert result["chosen_provider"] == "fallback"
    assert result["chosen_model"]  # non-empty
    assert math.isinf(result["effective_cost_per_1m"])
    assert all(not c["viable"] for c in result["candidates"])
    assert "no viable" in result["reason"].lower()


# ── 7. Zero tokens edge case: no exception ───────────────────────────────────


def test_zero_tokens_request_does_not_raise():
    """A zero-token request must route without raising.

    Some requests (e.g. system pings) carry zero estimated tokens; the router
    must handle that gracefully rather than dividing by zero or filtering
    everything.
    """
    opt = _three_provider_optimizer()
    # Should NOT raise.
    result = opt.route(difficulty="low", estimated_tokens=0, hour=3)

    assert math.isfinite(result["effective_cost_per_1m"])
    assert result["effective_cost_per_1m"] >= MIN_EFFECTIVE_PRICE
    assert result["chosen_provider"] == "zai_ours"


def test_zero_tokens_with_no_providers_does_not_raise():
    """An empty optimizer with a zero-token request falls back cleanly."""
    opt = RoutingOptimizer()
    result = opt.route(difficulty="low", estimated_tokens=0, hour=3)
    assert result["chosen_provider"] == "fallback"
    assert result["candidates"] == []


# ── 8. Multiple PriceKalman updates → price converges (amortization) ────────


def test_price_kalman_converges_on_repeated_updates():
    """Repeated observations of the same rate drive the estimate toward it.

    Mirrors the z.ai amortization story: as more tokens are consumed at the
    same flat rate, the smoothed estimate should converge to that rate.
    """
    true_rate = 0.068
    pk = PriceKalman(initial_rate=0.5, process_noise=1e-6, measurement_noise=1e-4)

    initial_err = abs(pk.predict() - true_rate)

    # Feed 50 observations at the true rate; small noise to be realistic.
    import random
    rng = random.Random(42)
    for _ in range(50):
        noisy = true_rate + rng.gauss(0, 0.005)
        pk.update(noisy)

    final_err = abs(pk.predict() - true_rate)

    # Estimate moved substantially toward the true rate.
    assert final_err < initial_err
    # And is now close in absolute terms.
    assert pk.predict() == pytest.approx(true_rate, abs=0.02)


def test_price_kalman_tracks_a_drifting_rate():
    """The 2-state filter (with velocity) follows a smoothly drifting rate."""
    pk = PriceKalman(initial_rate=0.10, process_noise=1e-4, measurement_noise=1e-3)

    # Rate climbs 0.10 → 0.20 over 20 steps.
    for i in range(20):
        rate = 0.10 + 0.005 * i
        pk.update(rate)

    # Predicted rate should be in the climbing region, not stuck near 0.10.
    assert pk.predict() > 0.15
    # Velocity should be positive (filter detected the upward trend).
    assert pk.velocity > 0


# ── 9. Multiple ConsumptionKalman updates → burn rate converges ─────────────


def test_consumption_kalman_converges_on_steady_burn():
    """A steady burn-rate observation converges the filter to that rate.

    Initial state is zero; after enough updates the burn_rate property should
    sit near the true per-period value.
    """
    true_burn = 12_345.0
    ck = ConsumptionKalman(process_noise=1.0, measurement_noise=1e4)

    for _ in range(40):
        ck.update(true_burn)

    assert ck.burn_rate == pytest.approx(true_burn, rel=0.10)
    # Uncertainty should drop as the filter becomes confident.
    assert ck.uncertainty > 0  # never collapses to exactly 0
    assert ck.is_initialized
    assert ck.update_count == 40
    assert ck.tokens_used == pytest.approx(true_burn * 40, rel=1e-9)


def test_consumption_kalman_predict_horizon_tracks_acceleration():
    """With acceleration, predict_horizon produces a curved (growing) burn."""
    ck = ConsumptionKalman(process_noise=1.0, measurement_noise=1e3)

    # Accelerating burn: 1000, 2000, 3000, 4000 → velocity & accel emerge.
    for v in (1000.0, 2000.0, 3000.0, 4000.0):
        ck.update(v)

    horizon = ck.predict_horizon(5)
    assert len(horizon) == 5
    assert all(h >= 0 for h in horizon)
    # Last horizon point should exceed the most-recent measurement if accel>0.
    if ck.acceleration > 0:
        assert horizon[-1] > 4000.0
    # Cumulative should be a sensible positive number.
    assert ck.predict_cumulative(5) > 0


# ── 10. ShadowLogger.get_agreement_rate() after 10 mixed decisions ──────────


def test_shadow_logger_agreement_rate_after_ten_mixed_decisions(tmp_path):
    """10 calls, 6 agree + 4 disagree → agreement rate 0.6."""
    db = tmp_path / "shadow.db"
    log = ShadowLogger(db_path=str(db))

    decisions = [
        # (live, shadow, agree?)
        ("zai_ours", "zai_ours", True),
        ("zai_ours", "zai_ours", True),
        ("zai_friend", "ppq_external", False),
        ("zai_ours", "zai_ours", True),
        ("ollama_cloud", "ppq_external", False),
        ("zai_ours", "zai_ours", True),
        ("zai_friend", "zai_ours", False),
        ("zai_ours", "zai_ours", True),
        ("ollama_cloud", "ollama_cloud", True),
        ("zai_friend", "ppq_external", False),
    ]
    for i, (live, shadow, _agree) in enumerate(decisions):
        log.log_decision(
            ts=1000.0 + i,
            live_provider=live, live_model="m",
            shadow_provider=shadow, shadow_model="m",
            shadow_cost=0.10, tokens=1000, live_cost=0.20,
        )

    rate = log.get_agreement_rate()
    expected = sum(1 for d in decisions if d[0] == d[1]) / len(decisions)
    assert rate == pytest.approx(expected)
    assert rate == pytest.approx(0.6)
    log.close()


def test_shadow_logger_agreement_rate_since_ts_filter(tmp_path):
    """The since_ts window correctly filters which decisions are counted."""
    db = tmp_path / "shadow.db"
    log = ShadowLogger(db_path=str(db))

    log.log_decision(
        ts=100.0, live_provider="a", live_model="m",
        shadow_provider="a", shadow_model="m",
        shadow_cost=1.0, tokens=10,
    )
    log.log_decision(
        ts=200.0, live_provider="a", live_model="m",
        shadow_provider="b", shadow_model="m",
        shadow_cost=1.0, tokens=10,
    )
    log.log_decision(
        ts=300.0, live_provider="a", live_model="m",
        shadow_provider="a", shadow_model="m",
        shadow_cost=1.0, tokens=10,
    )

    # Full window: 2 of 3 agree.
    assert log.get_agreement_rate() == pytest.approx(2 / 3)
    # Since ts=200: 1 of 2 agree.
    assert log.get_agreement_rate(since_ts=200.0) == pytest.approx(0.5)
    # Empty table inside window → 0.0 (NOT 1.0).
    assert log.get_agreement_rate(since_ts=999.0) == 0.0
    log.close()


def test_shadow_logger_cost_comparison_after_mixed_decisions(tmp_path):
    """get_cost_comparison returns (avg_live, avg_shadow) averages."""
    db = tmp_path / "shadow.db"
    log = ShadowLogger(db_path=str(db))

    # 2 calls with known live_cost / shadow_cost pairs.
    log.log_decision(
        ts=1.0, live_provider="a", live_model="m",
        shadow_provider="b", shadow_model="m",
        shadow_cost=0.5, tokens=10, live_cost=0.8,
    )
    log.log_decision(
        ts=2.0, live_provider="a", live_model="m",
        shadow_provider="b", shadow_model="m",
        shadow_cost=0.3, tokens=10, live_cost=0.6,
    )

    live_avg, shadow_avg = log.get_cost_comparison()
    assert live_avg == pytest.approx(0.7)
    assert shadow_avg == pytest.approx(0.4)
    log.close()


# ── Cross-module invariants (belt-and-suspenders) ────────────────────────────


def test_effective_price_floor_held_across_pipeline():
    """Even a zero-rate provider never produces a sub-floor effective price.

    This is the ADR-004 invariant held end-to-end through RoutingOptimizer.
    """
    opt = RoutingOptimizer()
    zero = PriceKalman(initial_rate=0.0)
    zero.update(0.0)
    opt.add_provider("free", zero, _ck(), quota_remaining=1_000_000, model_tier="low")

    result = opt.route(difficulty="low", hour=3)
    assert result["effective_cost_per_1m"] >= MIN_EFFECTIVE_PRICE


def test_tier_rank_constants_match_config():
    """The routing optimizer's tier ranks mirror config/providers.yaml."""
    assert TIER_RANK == {"low": 0, "standard": 1, "high": 2}
