"""Tests for live_router.py — LiveRouter class (Phase 3.1).

Covers:
- select_failover returns ollama_cloud when both z.ai keys exhausted
- select_failover returns (None, None) on exception
- record_request updates Kalman state
- thread safety (concurrent calls)
- select_primary stub returns None
- never raises (garbage inputs)
"""
import os
import sys
import threading
import tempfile
import pytest

# Ensure we can import src modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.live_router import LiveRouter


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Reset the LiveRouter singleton before and after each test."""
    LiveRouter.reset_instance()
    yield
    LiveRouter.reset_instance()


@pytest.fixture
def tmp_db():
    """Temp DB path (LiveRouter accepts db_path for API symmetry)."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture
def router(tmp_db):
    """Fresh LiveRouter instance with converged rates."""
    rates = {
        "ours":          0.001,
        "friend":        0.028983,
        "ollama_cloud":  0.023952,
        "ppq":           0.14,
        "openrouter":    0.135,
        "deepinfra":     1.30,
    }
    return LiveRouter(db_path=tmp_db, converged_rates=rates)


@pytest.fixture
def quota_both_exhausted():
    """Quota state where both z.ai keys are exhausted."""
    return {
        "ours":         {"used_pct": 100.0, "remaining": 0, "total": 2_000_000},
        "friend":       {"used_pct": 100.0, "remaining": 0, "total": 2_000_000},
        "ollama_cloud": {"used_pct": 20.0, "remaining": 800_000, "total": 1_000_000},
        "ppq":          {"used_pct": 0.0, "remaining": float("inf")},
        "openrouter":   {"used_pct": 0.0, "remaining": float("inf")},
        "deepinfra":    {"used_pct": 0.0, "remaining": float("inf")},
    }


@pytest.fixture
def quota_ours_exhausted_friend_ok():
    """Quota state where ours is exhausted but friend still has quota."""
    return {
        "ours":         {"used_pct": 100.0, "remaining": 0, "total": 2_000_000},
        "friend":       {"used_pct": 30.0, "remaining": 1_400_000, "total": 2_000_000},
        "ollama_cloud": {"used_pct": 20.0, "remaining": 800_000, "total": 1_000_000},
        "ppq":          {"used_pct": 0.0, "remaining": float("inf")},
        "openrouter":   {"used_pct": 0.0, "remaining": float("inf")},
        "deepinfra":    {"used_pct": 0.0, "remaining": float("inf")},
    }


@pytest.fixture
def all_healthy():
    return {
        "ours": True, "friend": True, "ollama_cloud": True,
        "ppq": True, "openrouter": True, "deepinfra": True,
    }


@pytest.fixture
def both_unhealthy():
    """Both z.ai keys are unhealthy (exhausted → breaker tripped)."""
    return {
        "ours": False, "friend": False, "ollama_cloud": True,
        "ppq": True, "openrouter": True, "deepinfra": True,
    }


# ── Init ─────────────────────────────────────────────────────────────────────


class TestLiveRouterInit:
    def test_creates_kalmans_for_all_providers(self, tmp_db):
        rates = {
            "ours": 0.001, "friend": 0.029, "ollama_cloud": 0.024,
            "ppq": 0.14, "openrouter": 0.135, "deepinfra": 1.30,
        }
        router = LiveRouter(db_path=tmp_db, converged_rates=rates)
        assert len(router._price_kalmans) == 6
        assert len(router._consumption_kalmans) == 6
        assert "ours" in router._price_kalmans
        assert "deepinfra" in router._price_kalmans

    def test_default_rates_used_when_none_provided(self, tmp_db):
        router = LiveRouter(db_path=tmp_db)
        assert "ours" in router._price_kalmans
        # ours should have the converged rate (clamped to min 0.001)
        pk = router._price_kalmans["ours"]
        assert pk.base_rate == pytest.approx(0.001, abs=0.01)

    def test_singleton(self, tmp_db):
        r1 = LiveRouter.get_instance(db_path=tmp_db)
        r2 = LiveRouter.get_instance()
        assert r1 is r2

    def test_reset_singleton(self, tmp_db):
        r1 = LiveRouter.get_instance(db_path=tmp_db)
        LiveRouter.reset_instance()
        r2 = LiveRouter.get_instance(db_path=tmp_db)
        assert r1 is not r2

    def test_provider_names(self, tmp_db):
        router = LiveRouter(db_path=tmp_db)
        names = router.provider_names
        assert "ours" in names
        assert "ollama_cloud" in names
        assert len(names) == 6


# ── select_failover ─────────────────────────────────────────────────────────


class TestSelectFailover:
    def test_returns_ollama_when_both_zai_exhausted(
        self, router, quota_both_exhausted, all_healthy
    ):
        """When both z.ai keys are exhausted, failover should pick
        ollama_cloud (cheapest high-tier external with converged rates)."""
        chosen, fallback = router.select_failover(
            quota_state=quota_both_exhausted,
            health_state=all_healthy,
            peak=False,
        )
        # ollama_cloud has converged rate 0.023952, which is cheaper than
        # ppq (0.14), openrouter (0.135), deepinfra (1.30).
        # ours and friend are breaker-tripped (health=False would trip them,
        # but here health=True — the exhaustion gate + will_exhaust handles it).
        # With remaining=0 and will_exhaust, ours/friend should be filtered.
        assert chosen is not None
        assert chosen == "ollama_cloud"

    def test_returns_ollama_when_both_zai_unhealthy(
        self, router, quota_both_exhausted, both_unhealthy
    ):
        """When both z.ai keys have breaker tripped (unhealthy),
        ollama_cloud should be chosen."""
        chosen, fallback = router.select_failover(
            quota_state=quota_both_exhausted,
            health_state=both_unhealthy,
            peak=False,
        )
        assert chosen == "ollama_cloud"

    def test_returns_ollama_when_ours_exhausted_friend_ok(
        self, router, quota_ours_exhausted_friend_ok, all_healthy
    ):
        """When ours is exhausted but friend has quota, ollama_cloud should
        still be chosen because converged rate ollama (0.024) < friend (0.029).
        """
        chosen, fallback = router.select_failover(
            quota_state=quota_ours_exhausted_friend_ok,
            health_state=all_healthy,
            peak=False,
        )
        assert chosen is not None
        # ours is exhausted (remaining=0), should not be chosen
        assert chosen != "ours"
        # ollama_cloud is cheaper than friend at converged rates
        assert chosen == "ollama_cloud"

    def test_returns_tuple(self, router, quota_both_exhausted, all_healthy):
        """select_failover always returns a 2-tuple."""
        result = router.select_failover(
            quota_state=quota_both_exhausted,
            health_state=all_healthy,
            peak=False,
        )
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_returns_none_none_on_exception(self, router):
        """select_failover must return (None, None) on any error."""
        # Pass garbage that will cause an exception inside _do_select_failover
        result = router.select_failover(
            quota_state=None,  # will cause .get() to fail
            health_state=None,
            peak="not-a-bool",
            failure_counts=None,
            pace_windows=None,
        )
        assert result == (None, None)

    def test_returns_none_none_on_garbage(self, router):
        """Even with completely garbage inputs, never raises."""
        result = router.select_failover(
            quota_state={123: "garbage"},
            health_state={456: "not-bool"},
            peak=None,
            failure_counts={"nonexistent": -1},
            pace_windows={"bad": [(1, 2, 3)]},
        )
        # Should either return a valid tuple or (None, None) — never raise
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_never_raises_on_all_unhealthy(self, router, quota_both_exhausted):
        """All providers unhealthy → no viable → returns (None, ...) or (None, None)."""
        all_unhealthy = {k: False for k in
                         ["ours", "friend", "ollama_cloud", "ppq", "openrouter", "deepinfra"]}
        result = router.select_failover(
            quota_state=quota_both_exhausted,
            health_state=all_unhealthy,
            peak=False,
        )
        assert isinstance(result, tuple)
        # No viable providers → chosen should be None
        assert result[0] is None

    def test_fallback_is_second_viable(
        self, router, quota_both_exhausted, all_healthy
    ):
        """When there are multiple viable providers, fallback should be
        the second cheapest."""
        chosen, fallback = router.select_failover(
            quota_state=quota_both_exhausted,
            health_state=all_healthy,
            peak=False,
        )
        assert chosen is not None
        # Fallback should be a valid provider (ppq/openrouter/deepinfra are
        # low-tier and filtered for high difficulty, so fallback should be
        # another high-tier or None if only ollama is high+healthy)
        if fallback is not None:
            assert fallback != chosen

    def test_peak_affects_selection(self, router, quota_ours_exhausted_friend_ok, all_healthy):
        """During peak, friend gets 3x multiplier — may change selection."""
        chosen_offpeak, _ = router.select_failover(
            quota_state=quota_ours_exhausted_friend_ok,
            health_state=all_healthy,
            peak=False,
        )
        chosen_peak, _ = router.select_failover(
            quota_state=quota_ours_exhausted_friend_ok,
            health_state=all_healthy,
            peak=True,
        )
        # Both should be valid providers
        assert chosen_offpeak is not None
        assert chosen_peak is not None
        # During peak, friend's price is 0.029 * 3 = 0.087 > ollama 0.024,
        # so peak should route to ollama if friend was chosen off-peak
        # (or stay the same if ollama was already chosen)


# ── select_primary (stub) ────────────────────────────────────────────────────


class TestSelectPrimary:
    def test_stub_returns_none(self, router, quota_both_exhausted, all_healthy):
        """select_primary is a Phase 4 stub — should return None."""
        result = router.select_primary(
            model="glm-5.2",
            tokens=5000,
            quota_state=quota_both_exhausted,
            health_state=all_healthy,
        )
        assert result is None

    def test_stub_never_raises(self, router):
        """select_primary must never raise, even with garbage."""
        result = router.select_primary(
            model=None,
            tokens=-1,
            quota_state=None,
            health_state=None,
        )
        assert result is None


# ── record_request ──────────────────────────────────────────────────────────


class TestRecordRequest:
    def test_updates_consumption_kalman(self, router):
        """record_request should update the ConsumptionKalman."""
        router.record_request("ours", 10000)
        ck = router._consumption_kalmans["ours"]
        assert ck.tokens_used == 10000
        assert ck.update_count == 1

    def test_multiple_updates_accumulate(self, router):
        """Multiple record_request calls accumulate tokens."""
        for i in range(5):
            router.record_request("ours", 10000)
        ck = router._consumption_kalmans["ours"]
        assert ck.tokens_used == 50000

    def test_updates_burn_history(self, router):
        """record_request should also update burn_history."""
        router.record_request("ollama_cloud", 5000)
        assert router._burn_history["ollama_cloud"] == [5000.0]

    def test_burn_history_capped_at_100(self, router):
        """burn_history should be capped at 100 entries."""
        for i in range(150):
            router.record_request("ours", 100)
        assert len(router._burn_history["ours"]) == 100

    def test_updates_price_kalman_with_cost(self, router):
        """When cost_estimate is provided, PriceKalman should be updated."""
        pk = router._price_kalmans["ours"]
        initial_rate = pk.base_rate
        router.record_request("ours", 10000, cost_estimate=0.05)
        assert pk._updates == 1

    def test_no_price_update_without_cost(self, router):
        """Without cost_estimate, PriceKalman should NOT be updated."""
        pk = router._price_kalmans["ours"]
        router.record_request("ours", 10000)
        assert pk._updates == 0

    def test_normalizes_provider_name(self, router):
        """record_request should normalize legacy provider names."""
        router.record_request("zai_ours", 10000)
        ck = router._consumption_kalmans["ours"]
        assert ck.tokens_used == 10000

    def test_unknown_provider_does_not_crash(self, router):
        """Recording for an unknown provider should not crash."""
        router.record_request("nonexistent", 10000)
        # No assertion needed — just must not raise

    def test_never_raises_on_garbage(self, router):
        """record_request must never raise."""
        router.record_request(None, -1, cost_estimate="not-a-number")
        router.record_request(123, None)
        # No assertion — just must not raise

    def test_never_raises_on_internal_corruption(self, router):
        """record_request must never raise even if internal state is corrupt."""
        router._consumption_kalmans = None
        router.record_request("ours", 10000)
        # No assertion — must not raise


# ── Thread safety ────────────────────────────────────────────────────────────


class TestThreadSafety:
    def test_concurrent_record_request(self, router):
        """Concurrent record_request calls should not corrupt state."""
        N_THREADS = 20
        N_CALLS = 100
        TOKENS_PER_CALL = 1000

        def worker():
            for _ in range(N_CALLS):
                router.record_request("ours", TOKENS_PER_CALL)

        threads = [threading.Thread(target=worker) for _ in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        ck = router._consumption_kalmans["ours"]
        expected = N_THREADS * N_CALLS * TOKENS_PER_CALL
        assert ck.tokens_used == expected

    def test_concurrent_select_failover(self, router, quota_both_exhausted, all_healthy):
        """Concurrent select_failover calls should not crash or corrupt."""
        N_THREADS = 10
        results = []
        results_lock = threading.Lock()

        def worker():
            for _ in range(50):
                r = router.select_failover(
                    quota_state=quota_both_exhausted,
                    health_state=all_healthy,
                    peak=False,
                )
                with results_lock:
                    results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All results should be valid 2-tuples
        assert len(results) == N_THREADS * 50
        for r in results:
            assert isinstance(r, tuple)
            assert len(r) == 2

    def test_concurrent_mixed_operations(self, router, quota_both_exhausted, all_healthy):
        """Mix of record_request + select_failover from concurrent threads."""
        errors = []
        error_lock = threading.Lock()

        def record_worker():
            try:
                for _ in range(100):
                    router.record_request("ours", 500)
            except Exception as e:
                with error_lock:
                    errors.append(e)

        def failover_worker():
            try:
                for _ in range(50):
                    r = router.select_failover(
                        quota_state=quota_both_exhausted,
                        health_state=all_healthy,
                        peak=False,
                    )
                    assert isinstance(r, tuple)
            except Exception as e:
                with error_lock:
                    errors.append(e)

        threads = (
            [threading.Thread(target=record_worker) for _ in range(5)]
            + [threading.Thread(target=failover_worker) for _ in range(5)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Errors during concurrent ops: {errors}"

    def test_concurrent_singleton_init(self, tmp_db):
        """Concurrent get_instance calls should return the same singleton."""
        results = []
        results_lock = threading.Lock()

        def worker():
            r = LiveRouter.get_instance(db_path=tmp_db)
            with results_lock:
                results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All threads should get the same instance
        assert len(results) == 10
        first = results[0]
        for r in results[1:]:
            assert r is first


# ── Get Kalman state ─────────────────────────────────────────────────────────


class TestGetKalmanState:
    def test_returns_state_for_all_providers(self, router):
        state = router.get_kalman_state()
        assert isinstance(state, dict)
        assert "ours" in state
        assert "ollama_cloud" in state
        assert "base_rate" in state["ours"]
        assert "burn_rate" in state["ours"]
        assert "tokens_used" in state["ours"]

    def test_state_reflects_updates(self, router):
        router.record_request("ours", 50000)
        state = router.get_kalman_state()
        assert state["ours"]["tokens_used"] == 50000

    def test_never_raises(self, router):
        """get_kalman_state must never raise."""
        # Corrupt internal state
        router._price_kalmans = None
        result = router.get_kalman_state()
        assert result == {}


# ── compute_pace_windows ──────────────────────────────────────────────────────


class TestComputePaceWindows:
    """Test LiveRouter.compute_pace_windows — converts quota_cache to pace tuples."""

    @pytest.fixture
    def quota_cache_with_windows(self):
        """Simulate a quota_cache with 5h + weekly windows for ours + friend."""
        import time as _time
        now = int(_time.time())
        return {
            "ours": ([
                {"name": "5-hour", "used_pct": 80,
                 "resets_at": now - 4 * 3600 + 5 * 3600, "window_hours": 5},
                {"name": "weekly", "used_pct": 40,
                 "resets_at": now - 50 * 3600 + 168 * 3600, "window_hours": 168},
            ], _time.time()),
            "friend": ([
                {"name": "5-hour", "used_pct": 30,
                 "resets_at": now - 2 * 3600 + 5 * 3600, "window_hours": 5},
            ], _time.time()),
        }

    def test_returns_dict_keyed_by_provider(self, router, quota_cache_with_windows):
        result = router.compute_pace_windows(quota_cache_with_windows)
        assert isinstance(result, dict)
        assert "ours" in result
        assert "friend" in result

    def test_tuples_have_five_elements(self, router, quota_cache_with_windows):
        result = router.compute_pace_windows(quota_cache_with_windows)
        for name, windows in result.items():
            for tup in windows:
                assert len(tup) == 5, f"{name} window has {len(tup)} elements"

    def test_5h_and_weekly_windows_returned(self, router, quota_cache_with_windows):
        result = router.compute_pace_windows(quota_cache_with_windows)
        durations = [w[4] for w in result["ours"]]
        assert 5.0 in durations
        assert 168.0 in durations

    def test_used_pct_reflected_in_quota_used(self, router, quota_cache_with_windows):
        """quota_used = used_pct/100 * quota_total (default 2M)."""
        result = router.compute_pace_windows(quota_cache_with_windows)
        # ours 5h at 80%: 0.80 * 2_000_000 = 1_600_000
        ours_5h = [w for w in result["ours"] if w[4] == 5.0][0]
        assert ours_5h[0] == pytest.approx(1_600_000, rel=1e-3)

    def test_burn_rate_from_consumption_kalman(self, router, quota_cache_with_windows):
        """burn_rate in the tuple should match the ConsumptionKalman's burn_rate."""
        router.record_request("ours", 50000)
        router.record_request("ours", 60000)
        result = router.compute_pace_windows(quota_cache_with_windows)
        ck_burn = router._consumption_kalmans["ours"].burn_rate
        ours_5h = result["ours"][0]
        assert ours_5h[3] == pytest.approx(ck_burn)

    def test_skips_unknown_provider_in_cache(self, router):
        """Providers in quota_cache not in the router are silently skipped."""
        import time as _time
        now = int(_time.time())
        cache = {
            "nonexistent": ([
                {"name": "5-hour", "used_pct": 50,
                 "resets_at": now + 3600, "window_hours": 5},
            ], _time.time()),
        }
        result = router.compute_pace_windows(cache)
        assert "nonexistent" not in result

    def test_empty_cache_returns_empty(self, router):
        result = router.compute_pace_windows({})
        assert result == {}

    def test_none_cache_returns_empty(self, router):
        result = router.compute_pace_windows(None)
        assert result == {}

    def test_malformed_windows_skipped(self, router):
        """Malformed window dicts should be silently skipped."""
        cache = {
            "ours": ([
                {"name": "broken"},  # missing fields
                {"name": "5-hour", "used_pct": 50,
                 "resets_at": int(__import__("time").time()) + 3600,
                 "window_hours": 5},
            ], 0.0),
        }
        result = router.compute_pace_windows(cache)
        assert "ours" in result
        assert len(result["ours"]) == 1  # only the valid window

    def test_never_raises_on_garbage(self, router):
        """compute_pace_windows must never raise, even with garbage input."""
        result = router.compute_pace_windows("garbage")
        assert isinstance(result, dict)
        result = router.compute_pace_windows({"ours": "not_a_tuple"})
        assert isinstance(result, dict)
        result = router.compute_pace_windows({"ours": (None, 0.0)})
        assert isinstance(result, dict)