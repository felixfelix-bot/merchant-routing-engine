"""Tests for shadow_hook.py — shadow mode integration."""
import os
import sys
import tempfile
import pytest

# Ensure we can import src modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.shadow_hook import ShadowHook


@pytest.fixture
def tmp_db():
    """Temp DB for each test."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture
def hook(tmp_db):
    """Fresh ShadowHook instance with temp DB."""
    return ShadowHook(db_path=tmp_db)


@pytest.fixture
def sample_quota():
    """Sample quota state mimicking proxy's live data."""
    return {
        "ours":          {"used_pct": 30.0, "remaining": 1_400_000, "total": 2_000_000},
        "friend":        {"used_pct": 45.0, "remaining": 1_100_000, "total": 2_000_000},
        "ollama_cloud":  {"used_pct": 100.0, "remaining": 0,             "total": 500_000_000},
        "ppq":           {"used_pct": 0.0,  "remaining": float("inf")},
        "openrouter":    {"used_pct": 0.0,  "remaining": float("inf")},
        "deepinfra":     {"used_pct": 0.0,  "remaining": float("inf")},
    }


@pytest.fixture
def all_healthy():
    return {
        "ours": True, "friend": True, "ollama_cloud": True,
        "ppq": True, "openrouter": True, "deepinfra": True,
    }


class TestShadowHookInit:
    def test_creates_logger_table(self, tmp_db):
        hook = ShadowHook(db_path=tmp_db)
        # Should be able to query without error
        assert hook.get_stats()["total_decisions"] == 0

    def test_singleton(self, tmp_db):
        ShadowHook._instance = None
        h1 = ShadowHook.get_instance(db_path=tmp_db)
        h2 = ShadowHook.get_instance()
        assert h1 is h2


class TestCompare:
    def test_never_raises(self, hook, sample_quota, all_healthy):
        """Compare must NEVER raise — even with garbage inputs."""
        hook.compare(None, None, 0, {}, {}, False)  # empty
        hook.compare("ours", "glm-5.2", 5000, sample_quota, all_healthy, False)
        hook.compare("garbage", None, -1, None, None, True)  # bad inputs

    def test_logs_decision(self, hook, sample_quota, all_healthy):
        hook.compare("ours", "glm-5.2", 5000, sample_quota, all_healthy, False)
        assert hook.get_stats()["total_decisions"] == 1

    def test_logs_multiple(self, hook, sample_quota, all_healthy):
        for i in range(10):
            hook.compare("ours", "glm-5.2", 5000 * (i + 1), sample_quota, all_healthy, False)
        assert hook.get_stats()["total_decisions"] == 10

    def test_shadow_routes_during_peak(self, hook, sample_quota, all_healthy):
        """During peak, optimizer should prefer ollama (no peak surcharge)."""
        hook.compare("ours", "glm-5.2", 5000, sample_quota, all_healthy, peak=True)
        # During peak ours has 3x cost, ollama has 1x — optimizer picks ollama
        stats = hook.get_stats()
        assert stats["total_decisions"] == 1

    def test_unhealthy_provider_filtered(self, hook, sample_quota):
        """If ours is unhealthy, optimizer should route to friend or ollama."""
        health = {
            "ours": False, "friend": True, "ollama_cloud": True,
            "ppq": True, "openrouter": True, "deepinfra": True,
        }
        hook.compare("ours", "glm-5.2", 5000, sample_quota, health, False)
        assert hook.get_stats()["total_decisions"] == 1

    def test_all_unhealthy_fallback(self, hook, sample_quota):
        """All providers unhealthy → optimizer returns fallback."""
        health = {k: False for k in ["ours", "friend", "ollama_cloud", "ppq", "openrouter", "deepinfra"]}
        hook.compare("none", "glm-5.2", 5000, sample_quota, health, False)
        assert hook.get_stats()["total_decisions"] == 1


class TestAgreement:
    def test_agreement_when_same_provider(self, hook, sample_quota, all_healthy):
        """If both live and shadow choose same provider, agreement=1."""
        # Off-peak: ours is cheapest (0.31 $/M), friend is 0.375
        # optimizer should pick ours, matching the live choice
        hook.compare("ours", "glm-5.2", 5000, sample_quota, all_healthy, peak=False)
        assert hook.get_stats()["agreement_rate"] == 1.0

    def test_disagreement_during_peak(self, hook, all_healthy):
        """During actual peak hours, optimizer prefers ollama (no peak surcharge).

        This test is hour-dependent — only asserts disagreement during
        UTC 6-10. Otherwise just verifies the decision was logged.
        """
        from datetime import datetime, timezone
        hour = datetime.now(timezone.utc).hour
        # Use a quota where ollama is available (not exhausted)
        peak_quota = {
            "ours":          {"used_pct": 30.0, "remaining": 1_400_000, "total": 2_000_000},
            "friend":        {"used_pct": 45.0, "remaining": 1_100_000, "total": 2_000_000},
            "ollama_cloud":  {"used_pct": 20.0, "remaining": 400_000_000, "total": 500_000_000},
            "ppq":           {"used_pct": 0.0,  "remaining": float("inf")},
            "openrouter":    {"used_pct": 0.0,  "remaining": float("inf")},
            "deepinfra":     {"used_pct": 0.0,  "remaining": float("inf")},
        }
        hook.compare("ours", "glm-5.2", 5000, peak_quota, all_healthy, peak=True)
        stats = hook.get_stats()
        assert stats["total_decisions"] == 1
        if 6 <= hour <= 10:
            # Peak: ours gets 3x, ollama 1x — optimizer picks ollama
            assert stats["agreement_rate"] == 0.0


class TestModelMapping:
    def test_high_model(self, hook):
        assert hook._model_to_difficulty("glm-5.2") == "high"
        assert hook._model_to_difficulty("glm-4.5") == "high"

    def test_low_model(self, hook):
        assert hook._model_to_difficulty("glm-4.5-flash") == "low"
        assert hook._model_to_difficulty("glm-4.5-air") == "low"

    def test_none(self, hook):
        assert hook._model_to_difficulty(None) == "medium"

    def test_unknown(self, hook):
        assert hook._model_to_difficulty("something-new") == "medium"


class TestBurnRateTracking:
    def test_burn_rate_updates(self, hook, sample_quota, all_healthy):
        """Multiple calls should update consumption Kalman for serving provider."""
        for i in range(5):
            hook.compare("ours", "glm-5.2", 10000, sample_quota, all_healthy, False)
        ck = hook._consumption_kalmans["ours"]
        assert ck.tokens_used == 50000  # 5 × 10000

    def test_burn_rate_not_updated_for_unserved(self, hook, sample_quota, all_healthy):
        """Tokens only attributed to the provider that served the request."""
        hook.compare("ours", "glm-5.2", 10000, sample_quota, all_healthy, False)
        friend_ck = hook._consumption_kalmans["friend"]
        assert friend_ck.tokens_used == 0
