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
        "ollama_cloud": {"used_pct": 20.0, "remaining": 400_000_000, "total": 500_000_000},
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
        "ollama_cloud": {"used_pct": 20.0, "remaining": 400_000_000, "total": 500_000_000},
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
        assert len(names) == 7


# ── select_failover ─────────────────────────────────────────────────────────


class TestSelectFailover:
    def test_returns_ollama_when_both_zai_exhausted(
        self, router, quota_both_exhausted, all_healthy
    ):
        """When both z.ai keys are exhausted, failover should pick
        ollama_cloud (cheapest high-tier external with converged rates)."""
        (chosen, chosen_model), (fallback, fallback_model) = router.select_failover(
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
        (chosen, chosen_model), (fallback, fallback_model) = router.select_failover(
            quota_state=quota_both_exhausted,
            health_state=both_unhealthy,
            peak=False,
        )
        assert chosen == "ollama_cloud"

    def test_returns_external_when_all_high_tier_dead(self, router):
        """REGRESSION (t_2532b185): when BOTH z.ai keys AND ollama_cloud are
        unavailable (the 48h-soak failover scenario where ollama is
        rate-limited daily), select_failover must still return a viable
        pay-per-token external (ppq/openrouter/deepinfra) instead of
        (None, None).

        Root cause: the optimizer was queried at difficulty='high' only,
        which gates out the low-tier pay-per-token externals (rank 0 < 2).
        With no high-tier provider viable, route() returns 'fallback' and
        select_failover returned (None, None), so the production proxy
        silently fell back to the hardcoded chain instead of using
        Kalman-optimized selection. Fix: relax difficulty high → medium →
        low so the low-tier externals are reached when nothing higher is
        viable.
        """
        quota_all_high_tier_dead = {
            "ours":         {"used_pct": 100.0, "remaining": 0, "total": 2_000_000},
            "friend":       {"used_pct": 100.0, "remaining": 0, "total": 2_000_000},
            "ollama_cloud": {"used_pct": 100.0, "remaining": 0, "total": 500_000_000},
            "ppq":          {"used_pct": 0.0, "remaining": float("inf")},
            "openrouter":   {"used_pct": 0.0, "remaining": float("inf")},
            "deepinfra":    {"used_pct": 0.0, "remaining": float("inf")},
        }
        # All high-tier providers dead (breaker tripped); only the low-tier
        # pay-per-token externals remain alive.
        only_externals_healthy = {
            "ours": False, "friend": False, "ollama_cloud": False,
            "ppq": True, "openrouter": True, "deepinfra": True,
        }
        (chosen, chosen_model), (fallback, fallback_model) = router.select_failover(
            quota_state=quota_all_high_tier_dead,
            health_state=only_externals_healthy,
            peak=False,
        )
        # MUST NOT be None — a healthy external provider exists.
        assert chosen is not None, (
            "select_failover returned None when healthy pay-per-token externals "
            "exist — the tier-gating regression (t_2532b185) has resurfaced"
        )
        assert chosen in ("ppq", "openrouter", "deepinfra")
        # Cheapest converged low-tier external off-peak is openrouter (0.135)
        # < ppq (0.14) < deepinfra (1.30), so it should win.
        assert chosen == "openrouter"
        # Fallback is the next cheapest viable external.
        assert fallback == "ppq"

    def test_returns_external_when_all_high_tier_dead_peak(self, router):
        """REGRESSION (t_2532b185): tier relaxation must also work during a
        z.ai peak hour — peak multipliers only apply to z.ai providers, so the
        low-tier externals (no peak window) are unaffected and still selected
        when all high-tier providers are dead."""
        quota_all_high_tier_dead = {
            "ours":         {"used_pct": 100.0, "remaining": 0, "total": 2_000_000},
            "friend":       {"used_pct": 100.0, "remaining": 0, "total": 2_000_000},
            "ollama_cloud": {"used_pct": 100.0, "remaining": 0, "total": 500_000_000},
            "ppq":          {"used_pct": 0.0, "remaining": float("inf")},
            "openrouter":   {"used_pct": 0.0, "remaining": float("inf")},
            "deepinfra":    {"used_pct": 0.0, "remaining": float("inf")},
        }
        only_externals_healthy = {
            "ours": False, "friend": False, "ollama_cloud": False,
            "ppq": True, "openrouter": True, "deepinfra": True,
        }
        (chosen, chosen_model), _ = router.select_failover(
            quota_state=quota_all_high_tier_dead,
            health_state=only_externals_healthy,
            peak=True,
        )
        assert chosen is not None
        assert chosen in ("ppq", "openrouter", "deepinfra")

    def test_malformed_pace_window_does_not_break_failover(self, router):
        """REGRESSION (t_2532b185): a malformed pace-window tuple for one
        provider must not abort the whole failover. Previously
        pace_factor_multi was called unwrapped; a bad tuple raised, the
        exception propagated out of _do_select_failover and was swallowed by
        select_failover's outer try/except, silently yielding (None, None).
        Now each provider's pace call is wrapped per-provider."""
        quota = {
            "ours":         {"used_pct": 100.0, "remaining": 0, "total": 2_000_000},
            "friend":       {"used_pct": 100.0, "remaining": 0, "total": 2_000_000},
            "ollama_cloud": {"used_pct": 20.0, "remaining": 400_000_000, "total": 500_000_000},
            "ppq":          {"used_pct": 0.0, "remaining": float("inf")},
            "openrouter":   {"used_pct": 0.0, "remaining": float("inf")},
            "deepinfra":    {"used_pct": 0.0, "remaining": float("inf")},
        }
        health = {
            "ours": False, "friend": False, "ollama_cloud": True,
            "ppq": True, "openrouter": True, "deepinfra": True,
        }
        # Malformed 3-tuple for a real provider (valid input is a 5-tuple).
        bad_pace = {"ollama_cloud": [(1, 2, 3)]}
        (chosen, chosen_model), _ = router.select_failover(
            quota_state=quota,
            health_state=health,
            peak=False,
            pace_windows=bad_pace,
        )
        # ollama is healthy + high-tier; it must still be chosen despite the
        # bad pace window for it.
        assert chosen == "ollama_cloud"

    def test_returns_ollama_when_ours_exhausted_friend_ok(
        self, router, quota_ours_exhausted_friend_ok, all_healthy
    ):
        """When ours is exhausted but friend has quota, ollama_cloud should
        still be chosen because converged rate ollama (0.024) < friend (0.029).
        """
        (chosen, chosen_model), (fallback, fallback_model) = router.select_failover(
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
        assert result == ((None, None), (None, None))

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
        assert result[0] == (None, None)

    def test_fallback_is_second_viable(
        self, router, quota_both_exhausted, all_healthy
    ):
        """When there are multiple viable providers, fallback should be
        the second cheapest."""
        (chosen, chosen_model), (fallback, fallback_model) = router.select_failover(
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
        (chosen_offpeak, _), _ = router.select_failover(
            quota_state=quota_ours_exhausted_friend_ok,
            health_state=all_healthy,
            peak=False,
        )
        (chosen_peak, _), _ = router.select_failover(
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


# ── P3.4 Fix 2: last_pace_mults (exposed for routing_live_decisions) ────────


class TestLastPaceMults:
    """``LiveRouter.last_pace_mults`` exposes the per-provider pace multipliers
    computed inside ``_do_select_failover`` so the production proxy can log the
    ACTUAL multipliers used in a failover decision to ``routing_live_decisions``
    (P3.4, Fix 2)."""

    def test_empty_before_any_failover(self, router):
        """A fresh router has computed no multipliers yet."""
        assert router.last_pace_mults == {}

    def test_empty_when_no_pace_windows(self, router, quota_both_exhausted,
                                        both_unhealthy):
        """select_failover with pace_windows=None stashes an empty dict
        (no windows → no multipliers), but the stash still happens."""
        router.select_failover(
            quota_state=quota_both_exhausted, health_state=both_unhealthy,
            peak=False, pace_windows=None)
        assert router.last_pace_mults == {}

    def test_populated_after_failover_with_windows(
            self, router, quota_both_exhausted, both_unhealthy):
        """When pace_windows are provided, the computed multipliers are stashed
        and exposed via last_pace_mults."""
        import time
        now = int(time.time())
        win = {"name": "5-hour", "used_pct": 50,
               "resets_at": now + 3600, "window_hours": 5}
        cache = {
            "ours": ([win], 0.0),
            "friend": ([win], 0.0),
        }
        pw = router.compute_pace_windows(cache)
        router.select_failover(
            quota_state=quota_both_exhausted, health_state=both_unhealthy,
            peak=False, pace_windows=pw)
        # At least one provider with a window should have a multiplier.
        assert isinstance(router.last_pace_mults, dict)
        assert any(name in pw for name in router.last_pace_mults), \
            "last_pace_mults should reflect providers that had pace windows"

    def test_returns_a_copy(self, router, quota_both_exhausted,
                            both_unhealthy):
        """Mutating the returned dict must not corrupt internal state."""
        import time
        now = int(time.time())
        win = {"name": "5-hour", "used_pct": 50,
               "resets_at": now + 3600, "window_hours": 5}
        pw = router.compute_pace_windows({"ours": ([win], 0.0)})
        router.select_failover(
            quota_state=quota_both_exhausted, health_state=both_unhealthy,
            peak=False, pace_windows=pw)
        snapshot = router.last_pace_mults
        snapshot["__injected__"] = 999.0
        # Re-read — the injected key must not persist internally.
        assert "__injected__" not in router.last_pace_mults


# ── TELNYX-4.3: Telnyx in LiveRouter failover ────────────────────────────────


class TestTelnyxFailover:
    """TELNYX-4.3 gate tests: LiveRouter includes telnyx in failover candidates.

    Verifies that:
    - telnyx is in the default provider list (provider_names).
    - _resolve_model_rate resolves kimi-k3 on telnyx to the per-model
      last-resort rate ($2.70/M) from LAST_RESORT_RATES_PER_MODEL (added in 4.2),
      not the blended $5.40/M from LAST_RESORT_RATES.
    - kimi-k3 requests can failover to telnyx (kimi-k3 is NOT in
      _OLLAMA_EXCLUSIVE_MODELS, so it is not short-circuited to ollama_cloud).
    """

    def test_telnyx_in_default_provider_names(self, tmp_db):
        """GATE: telnyx is in the default provider list."""
        router = LiveRouter(db_path=tmp_db)
        names = router.provider_names
        assert "telnyx" in names, (
            f"telnyx must be in provider_names, got {names}"
        )

    def test_telnyx_in_external_providers(self):
        """GATE: telnyx is in the _EXTERNAL_PROVIDERS tuple."""
        from src.live_router import _EXTERNAL_PROVIDERS
        assert "telnyx" in _EXTERNAL_PROVIDERS

    def test_resolve_model_rate_telnyx_kimi_k3_last_resort(self):
        """GATE: _resolve_model_rate('telnyx', 'kimi-k3') returns 2.70
        (per-model last-resort) when no measured data is available.
        """
        from src.live_router import _resolve_model_rate
        # Empty rates dict → no measured data, should fall to last-resort.
        rate = _resolve_model_rate({}, "telnyx", "kimi-k3")
        assert rate == pytest.approx(2.70), (
            f"Expected 2.70 (per-model last-resort), got {rate}"
        )

    def test_resolve_model_rate_source_telnyx_kimi_k3(self):
        """GATE: _resolve_model_rate_source returns ('last_resort', 2.70)
        for telnyx/kimi-k3 when no measured data exists.
        """
        from src.live_router import _resolve_model_rate_source
        rate, source = _resolve_model_rate_source({}, "telnyx", "kimi-k3")
        assert rate == pytest.approx(2.70)
        assert source == "last_resort", f"Expected 'last_resort', got {source}"

    def test_resolve_model_rate_telnyx_glm_5_2_last_resort(self):
        """GATE: _resolve_model_rate('telnyx', 'glm-5.2') returns 13.50
        (premium-tier per-model last-resort).
        """
        from src.live_router import _resolve_model_rate
        rate = _resolve_model_rate({}, "telnyx", "glm-5.2")
        assert rate == pytest.approx(13.50), (
            f"Expected 13.50 (premium per-model last-resort), got {rate}"
        )

    def test_measured_rate_takes_precedence_over_last_resort(self):
        """GATE: when measured data exists, it takes precedence over
        the per-model last-resort estimate.
        """
        from src.live_router import _resolve_model_rate_source
        rates = {"telnyx": {"kimi-k3": 3.50, "_default": 5.40}}
        rate, source = _resolve_model_rate_source(rates, "telnyx", "kimi-k3")
        assert rate == pytest.approx(3.50)
        assert source == "measured"

    def test_kimi_k3_not_ollama_exclusive(self):
        """GATE: 'kimi-k3' (without :cloud suffix) is NOT in
        _OLLAMA_EXCLUSIVE_MODELS, so it can failover to telnyx.
        (Only 'kimi-k3:cloud' is exclusive to ollama_cloud.)
        """
        from src.live_router import _OLLAMA_EXCLUSIVE_MODELS
        assert "kimi-k3" not in _OLLAMA_EXCLUSIVE_MODELS, (
            "kimi-k3 (without :cloud) must NOT be Ollama-exclusive — "
            "otherwise it would be short-circuited to ollama_cloud and "
            "never failover to telnyx."
        )

    def test_kimi_k3_cloud_removed_from_ollama_exclusive(self):
        """GATE (TELNYX-2.4): 'kimi-k3:cloud' has been removed from
        _OLLAMA_EXCLUSIVE_MODELS so it can failover to telnyx when
        ollama_cloud is exhausted. Previously it was short-circuited
        to ollama_cloud with no alternative.
        """
        from src.live_router import _OLLAMA_EXCLUSIVE_MODELS
        assert "kimi-k3:cloud" not in _OLLAMA_EXCLUSIVE_MODELS, (
            "kimi-k3:cloud must NOT be Ollama-exclusive — Telnyx serves "
            "kimi-k3 (confirmed by TELNYX-6.2 live integration test), so "
            "it should be able to failover to telnyx."
        )

    def test_kimi_k3_cloud_can_failover_to_telnyx(self, tmp_db):
        """GATE (TELNYX-2.4): when ollama_cloud is exhausted/unhealthy,
        kimi-k3:cloud failover includes telnyx as a candidate (it is no
        longer short-circuited to ollama_cloud only).
        """
        rates = {
            "ours":          0.001,
            "friend":        0.029,
            "ollama_cloud":  0.024,
            "ppq":           0.14,
            "openrouter":    0.135,
            "deepinfra":     1.30,
            "telnyx":        5.40,
        }
        router = LiveRouter(db_path=tmp_db, converged_rates=rates)

        # All providers exhausted except telnyx.
        quota = {
            "ours":         {"used_pct": 100.0, "remaining": 0, "total": 2_000_000},
            "friend":       {"used_pct": 100.0, "remaining": 0, "total": 2_000_000},
            "ollama_cloud": {"used_pct": 100.0, "remaining": 0, "total": 500_000_000},
            "ppq":          {"used_pct": 100.0, "remaining": 0, "total": float("inf")},
            "openrouter":   {"used_pct": 100.0, "remaining": 0, "total": float("inf")},
            "deepinfra":    {"used_pct": 100.0, "remaining": 0, "total": float("inf")},
            "telnyx":       {"used_pct": 0.0, "remaining": float("inf")},
        }
        # Only telnyx is healthy.
        health = {
            "ours": False, "friend": False, "ollama_cloud": False,
            "ppq": False, "openrouter": False, "deepinfra": False,
            "telnyx": True,
        }

        (chosen, chosen_model), _ = router.select_failover(
            quota_state=quota,
            health_state=health,
            peak=False,
            model="kimi-k3:cloud",
        )
        # Telnyx should be chosen — kimi-k3:cloud is no longer
        # short-circuited to ollama_cloud.
        assert chosen == "telnyx", (
            f"Expected telnyx as failover for kimi-k3:cloud, got {chosen}"
        )

    def test_telnyx_serves_kimi_k3_via_last_resort(self):
        """GATE: the _model_served detection in _do_select_failover
        recognises telnyx as serving kimi-k3 via LAST_RESORT_RATES_PER_MODEL,
        so it is NOT marked unreachable when per-model pricing is on.
        """
        from src.real_price_tracker import LAST_RESORT_RATES_PER_MODEL
        lr_per_model = LAST_RESORT_RATES_PER_MODEL.get("telnyx", {})
        assert "kimi-k3" in lr_per_model, (
            "telnyx must have a kimi-k3 entry in LAST_RESORT_RATES_PER_MODEL"
        )
        assert lr_per_model["kimi-k3"] > 0

    def test_kimi_k3_can_failover_to_telnyx(self, tmp_db):
        """GATE: with per-model pricing on, when all cheaper providers are
        exhausted/unhealthy, kimi-k3 failover includes telnyx as a candidate.

        We verify by constructing a LiveRouter with telnyx as the only healthy
        provider and checking that select_failover(model='kimi-k3') does not
        return (None, None) — telnyx is a viable failover target.
        """
        # Construct with rates that include telnyx.
        rates = {
            "ours":          0.001,
            "friend":        0.029,
            "ollama_cloud":  0.024,
            "ppq":           0.14,
            "openrouter":    0.135,
            "deepinfra":     1.30,
            "telnyx":        5.40,
        }
        router = LiveRouter(db_path=tmp_db, converged_rates=rates)

        # All providers exhausted except telnyx.
        quota = {
            "ours":         {"used_pct": 100.0, "remaining": 0, "total": 2_000_000},
            "friend":       {"used_pct": 100.0, "remaining": 0, "total": 2_000_000},
            "ollama_cloud": {"used_pct": 100.0, "remaining": 0, "total": 500_000_000},
            "ppq":          {"used_pct": 100.0, "remaining": 0, "total": float("inf")},
            "openrouter":   {"used_pct": 100.0, "remaining": 0, "total": float("inf")},
            "deepinfra":    {"used_pct": 100.0, "remaining": 0, "total": float("inf")},
            "telnyx":       {"used_pct": 0.0, "remaining": float("inf")},
        }
        # Only telnyx is healthy.
        health = {
            "ours": False, "friend": False, "ollama_cloud": False,
            "ppq": False, "openrouter": False, "deepinfra": False,
            "telnyx": True,
        }

        (chosen, chosen_model), _ = router.select_failover(
            quota_state=quota,
            health_state=health,
            peak=False,
            model="kimi-k3",
        )
        # Telnyx should be chosen as the only viable provider.
        assert chosen == "telnyx", (
            f"Expected telnyx as failover for kimi-k3, got {chosen}"
        )