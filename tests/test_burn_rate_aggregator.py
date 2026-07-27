"""Tests for burn_rate_aggregator — 5-minute windowed burn-rate aggregation.

Implements the ADR-008 observation-frequency contract:
  - Aggregate token counts per provider in a sliding 5-minute window
  - Every 5 minutes, compute hourly_rate = sum(tokens_in_window) × 12
  - Feed hourly_rate to the corresponding ConsumptionKalman
  - Thread-safe (proxy handles concurrent requests)

These tests verify:
  - Tokens accumulate correctly per provider
  - After 5 min, hourly_rate = sum × 12
  - Before 5 min, maybe_feed returns empty dict
  - Force=True feeds immediately
  - Thread safety under concurrent record calls
  - Old observations outside the window are pruned
  - Zero tokens handled correctly
  - Multiple providers tracked independently
"""
from __future__ import annotations

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.consumption_kalman import ConsumptionKalman
from src.burn_rate_aggregator import BurnRateAggregator


# ── Helpers ──────────────────────────────────────────────────────────────────


class MockClock:
    """Controllable clock for deterministic test timing."""

    def __init__(self, start: float = 1_000_000.0):
        self._t = start

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


# ── Accumulation ──────────────────────────────────────────────────────────────


def test_tokens_accumulate_per_provider():
    """record() should store observations per provider."""
    clock = MockClock()
    agg = BurnRateAggregator(window_minutes=5, time_fn=clock.now)

    agg.record("zai", 10_000)
    agg.record("zai", 5_000)
    agg.record("ppq", 3_000)

    obs = agg.observations("zai")
    assert len(obs) == 2
    assert obs[0] == (1_000_000.0, 10_000)
    assert obs[1] == (1_000_000.0, 5_000)

    obs_ppq = agg.observations("ppq")
    assert len(obs_ppq) == 1
    assert obs_ppq[0] == (1_000_000.0, 3_000)


def test_record_negative_tokens_raises():
    """Negative token counts are nonsensical — should raise."""
    clock = MockClock()
    agg = BurnRateAggregator(time_fn=clock.now)

    with pytest.raises(ValueError):
        agg.record("zai", -100)


# ── 5-minute window timing ────────────────────────────────────────────────────


def test_before_window_maybe_feed_returns_empty():
    """Before 5 min have elapsed, maybe_feed should return an empty dict."""
    clock = MockClock()
    agg = BurnRateAggregator(window_minutes=5, time_fn=clock.now)
    kalman = ConsumptionKalman()
    kalmans = {"zai": kalman}

    agg.record("zai", 100_000)
    # Only 3 minutes elapsed — not time yet
    clock.advance(180)
    agg.record("zai", 50_000)

    fed = agg.maybe_feed(kalmans)
    assert fed == {}
    assert kalman.update_count == 0


def test_after_window_maybe_feed_returns_hourly_rate():
    """After 5 min, hourly_rate should be sum(tokens_in_window) × 12."""
    clock = MockClock()
    agg = BurnRateAggregator(window_minutes=5, time_fn=clock.now)
    kalman = ConsumptionKalman()
    kalmans = {"zai": kalman}

    agg.record("zai", 100_000)
    clock.advance(120)  # 2 min
    agg.record("zai", 50_000)
    clock.advance(180)  # total 5 min

    fed = agg.maybe_feed(kalmans)
    assert "zai" in fed
    # 100k + 50k = 150k tokens in 5 min → × 12 = 1.8M / hour
    assert fed["zai"] == 1_800_000
    assert kalman.update_count == 1
    assert kalman.last_measurement == 1_800_000


def test_force_true_feeds_immediately():
    """force=True should bypass the 5-minute wait and feed immediately."""
    clock = MockClock()
    agg = BurnRateAggregator(window_minutes=5, time_fn=clock.now)
    kalman = ConsumptionKalman()
    kalmans = {"zai": kalman}

    agg.record("zai", 100_000)
    clock.advance(30)  # only 30 seconds

    fed = agg.maybe_feed(kalmans, force=True)
    assert "zai" in fed
    assert fed["zai"] == 1_200_000  # 100k × 12
    assert kalman.update_count == 1


def test_repeated_feed_waits_for_window():
    """After a feed, the next feed should wait another 5 minutes."""
    clock = MockClock()
    agg = BurnRateAggregator(window_minutes=5, time_fn=clock.now)
    kalman = ConsumptionKalman()
    kalmans = {"zai": kalman}

    agg.record("zai", 100_000)
    clock.advance(300)  # 5 min

    fed1 = agg.maybe_feed(kalmans)
    assert fed1 == {"zai": 1_200_000}

    # Record more tokens, but only 2 min since last feed
    agg.record("zai", 50_000)
    clock.advance(120)
    fed2 = agg.maybe_feed(kalmans)
    assert fed2 == {}  # not time yet

    # Another 3 min → total 5 min since last feed
    clock.advance(180)
    agg.record("zai", 30_000)
    fed3 = agg.maybe_feed(kalmans)
    # Only observations in the current 5-min window: 50k (at t=420) + 30k (at t=600)
    # The original 100k at t=1000000 is pruned (outside window from t=1000600)
    # Wait, let me recalculate. Clock starts at 1000000.
    # t=1000000: record 100k
    # t=1000300: feed (window 1000000-1000300), obs: 100k → hourly = 1.2M
    # t=1000300: record 50k
    # t=1000420: maybe_feed → no (420-300=120 < 300)
    # t=1000600: record 30k, maybe_feed → yes (600-300=300 >= 300)
    #   window: [1000300, 1000600], obs: 50k (t=1000300), 30k (t=1000600) → 80k × 12 = 960k
    assert fed3 == {"zai": 960_000}
    assert kalman.update_count == 2


# ── Pruning ───────────────────────────────────────────────────────────────────


def test_old_observations_pruned():
    """Observations older than the window should be pruned after a feed."""
    clock = MockClock()
    agg = BurnRateAggregator(window_minutes=5, time_fn=clock.now)

    # Record at t=0
    agg.record("zai", 100_000)
    # Record at t=2min
    clock.advance(120)
    agg.record("zai", 50_000)
    # Record at t=6min — the t=0 observation is now outside the 5-min window
    clock.advance(240)  # total 6 min
    agg.record("zai", 80_000)

    # Feed at t=6min — window is [1min, 6min]
    # The t=0 obs (100k) is outside the window and should be pruned
    kalman = ConsumptionKalman()
    fed = agg.maybe_feed({"zai": kalman}, force=True)

    # Only 50k + 80k = 130k in window → × 12 = 1.56M
    assert fed == {"zai": 1_560_000}

    # The old observation should be gone
    obs = agg.observations("zai")
    # All remaining should be within the window
    now = clock.now()
    window_start = now - 300
    for ts, _ in obs:
        assert ts >= window_start


def test_pruning_happens_even_without_feed():
    """Pruning should also happen on record when observations get stale."""
    clock = MockClock()
    agg = BurnRateAggregator(window_minutes=5, time_fn=clock.now)

    agg.record("zai", 100_000)
    clock.advance(600)  # 10 min later
    agg.record("zai", 50_000)

    # The first observation should have been pruned
    obs = agg.observations("zai")
    assert len(obs) == 1
    assert obs[0][1] == 50_000


# ── Zero tokens ───────────────────────────────────────────────────────────────


def test_zero_tokens_handled_correctly():
    """Zero-token requests should be recorded and contribute zero to the sum."""
    clock = MockClock()
    agg = BurnRateAggregator(window_minutes=5, time_fn=clock.now)
    kalman = ConsumptionKalman()
    kalmans = {"zai": kalman}

    agg.record("zai", 0)
    agg.record("zai", 100_000)
    agg.record("zai", 0)

    clock.advance(300)
    fed = agg.maybe_feed(kalmans)
    assert fed == {"zai": 1_200_000}  # (0 + 100k + 0) × 12
    assert kalman.update_count == 1


def test_all_zero_tokens_feed_zero():
    """If all requests had zero tokens, hourly_rate should be zero."""
    clock = MockClock()
    agg = BurnRateAggregator(window_minutes=5, time_fn=clock.now)
    kalman = ConsumptionKalman()
    kalmans = {"zai": kalman}

    agg.record("zai", 0)
    agg.record("zai", 0)
    clock.advance(300)

    fed = agg.maybe_feed(kalmans)
    assert fed == {"zai": 0}
    assert kalman.last_measurement == 0


def test_no_observations_returns_empty():
    """If no observations exist for a provider, it should not be fed."""
    clock = MockClock()
    agg = BurnRateAggregator(window_minutes=5, time_fn=clock.now)
    kalman = ConsumptionKalman()
    kalmans = {"zai": kalman}

    clock.advance(300)
    fed = agg.maybe_feed(kalmans)
    assert fed == {}
    assert kalman.update_count == 0


# ── Multiple providers ────────────────────────────────────────────────────────


def test_multiple_providers_tracked_independently():
    """Each provider should have its own observation list and feed timing."""
    clock = MockClock()
    agg = BurnRateAggregator(window_minutes=5, time_fn=clock.now)
    kalmans = {"zai": ConsumptionKalman(), "ppq": ConsumptionKalman()}

    # Record for both providers
    agg.record("zai", 100_000)
    agg.record("ppq", 200_000)
    clock.advance(300)

    fed = agg.maybe_feed(kalmans)
    assert fed == {"zai": 1_200_000, "ppq": 2_400_000}
    assert kalmans["zai"].update_count == 1
    assert kalmans["ppq"].update_count == 1

    # Record more for zai only
    agg.record("zai", 50_000)
    clock.advance(300)

    fed2 = agg.maybe_feed(kalmans)
    # zai: 50k × 12 = 600k (100k pruned)
    # ppq: 0 × 12 = 0 (no new observations, but the old one was pruned)
    assert fed2["zai"] == 600_000
    # ppq has no new observations — its window is empty
    assert fed2["ppq"] == 0


def test_provider_not_in_kalmans_skipped():
    """If a provider has observations but no kalman, it should be skipped."""
    clock = MockClock()
    agg = BurnRateAggregator(window_minutes=5, time_fn=clock.now)

    agg.record("zai", 100_000)
    agg.record("unknown", 50_000)
    clock.advance(300)

    kalman = ConsumptionKalman()
    fed = agg.maybe_feed({"zai": kalman})
    assert "zai" in fed
    assert "unknown" not in fed


# ── Thread safety ─────────────────────────────────────────────────────────────


def test_concurrent_record_thread_safe():
    """Concurrent record calls should not lose observations or corrupt state."""
    clock = MockClock()
    agg = BurnRateAggregator(window_minutes=5, time_fn=clock.now)

    n_threads = 20
    n_per_thread = 500
    barrier = threading.Barrier(n_threads)

    def worker():
        barrier.wait()
        for _ in range(n_per_thread):
            agg.record("zai", 100)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    obs = agg.observations("zai")
    expected = n_threads * n_per_thread
    assert len(obs) == expected, f"Expected {expected} observations, got {len(obs)}"

    # Verify total tokens
    total = sum(tok for _, tok in obs)
    assert total == expected * 100


def test_concurrent_record_and_maybe_feed_thread_safe():
    """Concurrent record + maybe_feed should not deadlock or corrupt."""
    clock = MockClock()
    agg = BurnRateAggregator(window_minutes=5, time_fn=clock.now)
    kalman = ConsumptionKalman()
    kalmans = {"zai": kalman}

    # Pre-record some data
    for _ in range(100):
        agg.record("zai", 100)

    stop = threading.Event()
    errors: list[Exception] = []

    def recorder():
        try:
            while not stop.is_set():
                agg.record("zai", 100)
        except Exception as e:
            errors.append(e)

    def feeder():
        try:
            while not stop.is_set():
                agg.maybe_feed(kalmans, force=True)
        except Exception as e:
            errors.append(e)

    t_rec = threading.Thread(target=recorder)
    t_feed = threading.Thread(target=feeder)
    t_rec.start()
    t_feed.start()

    # Let them run briefly
    t_rec.join(timeout=2)
    t_feed.join(timeout=2)
    stop.set()
    t_rec.join(timeout=1)
    t_feed.join(timeout=1)

    assert errors == [], f"Errors in threads: {errors}"


# ── Integration with ConsumptionKalman ─────────────────────────────────────────


def test_feeded_rate_converges_in_kalman():
    """Multiple 5-minute feeds should let the Kalman converge on the true rate."""
    clock = MockClock()
    agg = BurnRateAggregator(window_minutes=5, time_fn=clock.now)
    kalman = ConsumptionKalman()
    kalmans = {"zai": kalman}

    # Simulate 12 windows (1 hour) of ~100k tokens per 5-min window
    # → hourly_rate = 1.2M per window
    true_hourly = 1_200_000
    for i in range(12):
        agg.record("zai", 100_000)
        clock.advance(300)
        agg.maybe_feed(kalmans)

    # Kalman should have converged near the true rate
    assert kalman.update_count == 12
    err = abs(kalman.burn_rate - true_hourly)
    assert err < true_hourly * 0.1, (
        f"Kalman burn_rate {kalman.burn_rate:.0f} not within 10% of {true_hourly}"
    )


def test_custom_window_minutes():
    """The aggregator should work with non-default window sizes."""
    clock = MockClock()
    agg = BurnRateAggregator(window_minutes=1, time_fn=clock.now)
    kalman = ConsumptionKalman()
    kalmans = {"zai": kalman}

    agg.record("zai", 100_000)
    clock.advance(60)  # 1 minute
    fed = agg.maybe_feed(kalmans)
    # 100k tokens in 1-min window → × 60 = 6M / hour
    assert fed == {"zai": 6_000_000}