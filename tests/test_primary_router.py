"""Tests for primary_router.py — Phase 3 optimizer-as-primary."""
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.primary_router import PrimaryRouter


@pytest.fixture
def router(monkeypatch):
    """Fresh PrimaryRouter for each test.

    Mocks _load_converged_rates to return empty dict so tests use
    static seed costs (the historical DB may exist on the dev machine
    and would change routing decisions).
    """
    PrimaryRouter._instance = None
    # Prevent loading historical rates from the real DB
    monkeypatch.setattr(
        PrimaryRouter, "_load_converged_rates",
        staticmethod(lambda: {}),
    )
    return PrimaryRouter()


@pytest.fixture
def healthy_state():
    return {
        "ours": True, "friend": True, "ollama_cloud": True,
        "ppq": True, "openrouter": True, "deepinfra": True,
    }


@pytest.fixture
def normal_quota():
    return {
        "ours":          {"used_pct": 30.0, "remaining": 1_400_000, "total": 2_000_000},
        "friend":        {"used_pct": 45.0, "remaining": 1_100_000, "total": 2_000_000},
        "ollama_cloud":  {"used_pct": 100.0, "remaining": 0,             "total": 500_000_000},
        "ppq":           {"used_pct": 0.0,  "remaining": float("inf")},
        "openrouter":    {"used_pct": 0.0,  "remaining": float("inf")},
        "deepinfra":     {"used_pct": 0.0,  "remaining": float("inf")},
    }


class TestReturnType:
    """route() must return str | None, same as best_key()."""

    def test_returns_str_or_none(self, router, normal_quota, healthy_state):
        result = router.route(model="glm-5.2", tokens=5000,
                             quota_state=normal_quota, health_state=healthy_state)
        assert result is None or isinstance(result, str)

    def test_returns_valid_key_name(self, router, normal_quota, healthy_state):
        result = router.route(model="glm-5.2", tokens=5000,
                             quota_state=normal_quota, health_state=healthy_state)
        assert result in (None, "ours", "friend")

    def test_never_raises(self, router):
        """Must NEVER raise — even with garbage inputs."""
        # Empty inputs
        r = router.route()
        assert r is None or isinstance(r, str)
        # Zero tokens, no state
        r = router.route(None, 0, None, None, None)
        assert r is None or isinstance(r, str)
        # Invalid difficulty — ValueError caught internally
        r = router.route("garbage", -1, {}, {}, "invalid")
        assert r is None or isinstance(r, str)


class TestOffPeak:
    """Off-peak: ours should be cheapest (lowest seed cost)."""

    def test_off_peak_prefers_ours(self, router, normal_quota, healthy_state, monkeypatch):
        # Mock UTC hour to be outside peak (e.g., 15:00 UTC)
        import src.primary_router as pr_mod
        class FakeTime:
            @staticmethod
            def gmtime():
                class T: tm_hour = 15
                return T()
        monkeypatch.setattr(pr_mod.time, "gmtime", FakeTime.gmtime)

        result = router.route(model="glm-5.2", tokens=5000,
                             quota_state=normal_quota, health_state=healthy_state)
        # Off-peak: ours is $0.31/M, friend is $0.375/M, ollama exhausted → ours wins
        assert result == "ours"


class TestPeak:
    """During peak hours, z.ai keys get 3x cost — optimizer may prefer ollama."""

    def test_peak_returns_none_for_ollama(self, router, healthy_state, monkeypatch):
        """During peak, ours cost = 0.31*3 = 0.93, ollama = 0.024.
        Optimizer picks ollama → returns None (proxy falls through)."""
        # Use a quota where ollama is NOT exhausted so it gets picked
        peak_quota = {
            "ours":          {"used_pct": 30.0, "remaining": 1_400_000, "total": 2_000_000},
            "friend":        {"used_pct": 45.0, "remaining": 1_100_000, "total": 2_000_000},
            "ollama_cloud":  {"used_pct": 20.0, "remaining": 400_000_000, "total": 500_000_000},
            "ppq":           {"used_pct": 0.0,  "remaining": float("inf")},
            "openrouter":    {"used_pct": 0.0,  "remaining": float("inf")},
            "deepinfra":     {"used_pct": 0.0,  "remaining": float("inf")},
        }
        import src.primary_router as pr_mod
        class FakeTime:
            @staticmethod
            def gmtime():
                class T: tm_hour = 8  # peak hour
                return T()
        monkeypatch.setattr(pr_mod.time, "gmtime", FakeTime.gmtime)

        result = router.route(model="glm-5.2", tokens=5000,
                             quota_state=peak_quota, health_state=healthy_state)
        # Peak: ollama cheaper → optimizer picks ollama → None
        assert result is None


class TestUnhealthyProviders:
    """Unhealthy providers should be filtered out."""

    def test_ours_unhealthy_picks_friend(self, router, normal_quota, monkeypatch):
        health = {
            "ours": False, "friend": True, "ollama_cloud": True,
            "ppq": True, "openrouter": True, "deepinfra": True,
        }
        # Off-peak so ollama isn't cheaper
        import src.primary_router as pr_mod
        class FakeTime:
            @staticmethod
            def gmtime():
                class T: tm_hour = 15
                return T()
        monkeypatch.setattr(pr_mod.time, "gmtime", FakeTime.gmtime)

        result = router.route(model="glm-5.2", tokens=5000,
                             quota_state=normal_quota, health_state=health)
        # ours dead, friend alive, off-peak → friend
        assert result == "friend"

    def test_all_zai_unhealthy_returns_none(self, router, normal_quota):
        """Both z.ai keys dead → optimizer picks ollama → None."""
        health = {
            "ours": False, "friend": False, "ollama_cloud": True,
            "ppq": True, "openrouter": True, "deepinfra": True,
        }
        result = router.route(model="glm-5.2", tokens=5000,
                             quota_state=normal_quota, health_state=health)
        assert result is None


class TestQuotaExhaustion:
    """Near-exhaustion should steer away from that provider."""

    def test_ours_near_exhaustion(self, router, monkeypatch):
        """Ours at 99% quota → scarcity ramps cost → optimizer picks friend."""
        quota = {
            "ours":          {"used_pct": 99.0, "remaining": 20_000,  "total": 2_000_000},
            "friend":        {"used_pct": 30.0, "remaining": 1_400_000, "total": 2_000_000},
            "ollama_cloud":  {"used_pct": 100.0, "remaining": 0,             "total": 500_000_000},
            "ppq":           {"used_pct": 0.0,  "remaining": float("inf")},
            "openrouter":    {"used_pct": 0.0,  "remaining": float("inf")},
            "deepinfra":     {"used_pct": 0.0,  "remaining": float("inf")},
        }
        health = {k: True for k in quota}

        import src.primary_router as pr_mod
        class FakeTime:
            @staticmethod
            def gmtime():
                class T: tm_hour = 15
                return T()
        monkeypatch.setattr(pr_mod.time, "gmtime", FakeTime.gmtime)

        result = router.route(model="glm-5.2", tokens=5000,
                             quota_state=quota, health_state=health)
        # ours at 99% → scarcity ~2x → effective ~0.62 vs friend 0.375
        # → optimizer picks friend
        assert result == "friend"


class TestBurnRateTracking:
    def test_update_burn_rate(self, router):
        router.update_burn_rate("ours", 10000)
        router.update_burn_rate("ours", 20000)
        assert router._consumption_kalmans["ours"].tokens_used == 30000

    def test_update_ignores_unknown_provider(self, router):
        router.update_burn_rate("unknown", 10000)
        # Should not raise

    def test_update_ignores_zero_tokens(self, router):
        router.update_burn_rate("ours", 0)
        assert router._consumption_kalmans["ours"].tokens_used == 0


class TestModelMapping:
    def test_high(self, router):
        assert router._model_to_difficulty("glm-5.2") == "high"
        assert router._model_to_difficulty("glm-4.5") == "high"

    def test_low(self, router):
        assert router._model_to_difficulty("glm-4.5-flash") == "low"
        assert router._model_to_difficulty("glm-4.5-air") == "low"

    def test_none(self, router):
        assert router._model_to_difficulty(None) == "medium"


class TestSingleton:
    def test_get_instance_returns_same(self):
        PrimaryRouter._instance = None
        r1 = PrimaryRouter.get_instance()
        r2 = PrimaryRouter.get_instance()
        assert r1 is r2


class TestStats:
    def test_stats_track_calls(self, router, normal_quota, healthy_state):
        assert router.stats["call_count"] == 0
        router.route(model="glm-5.2", tokens=5000,
                    quota_state=normal_quota, health_state=healthy_state)
        assert router.stats["call_count"] == 1
        assert "last_decision" in router.stats
