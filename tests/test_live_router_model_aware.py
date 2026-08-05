"""Tests for model-aware failover in LiveRouter (P4.5c + P4.5d).

Covers:
- select_failover returns (provider, model) tuples, not bare provider strings
- Model is resolved via model_mapping.get_model() for each chosen provider
- task_type parameter selects different models for the same provider
- Default task_type is "coding"
- When chosen is None, model is also None
- Fallback tuple also carries its model
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.live_router import LiveRouter
from src.model_mapping import get_model


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_singleton():
    LiveRouter.reset_instance()
    yield
    LiveRouter.reset_instance()


@pytest.fixture
def tmp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture
def router(tmp_db):
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
    return {
        "ours":         {"used_pct": 100.0, "remaining": 0, "total": 2_000_000},
        "friend":       {"used_pct": 100.0, "remaining": 0, "total": 2_000_000},
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
def only_externals_healthy():
    """Only the low-tier pay-per-token externals are healthy."""
    return {
        "ours": False, "friend": False, "ollama_cloud": False,
        "ppq": True, "openrouter": True, "deepinfra": True,
    }


@pytest.fixture
def quota_all_high_tier_dead():
    return {
        "ours":         {"used_pct": 100.0, "remaining": 0, "total": 2_000_000},
        "friend":       {"used_pct": 100.0, "remaining": 0, "total": 2_000_000},
        "ollama_cloud": {"used_pct": 100.0, "remaining": 0, "total": 500_000_000},
        "ppq":          {"used_pct": 0.0, "remaining": float("inf")},
        "openrouter":   {"used_pct": 0.0, "remaining": float("inf")},
        "deepinfra":    {"used_pct": 0.0, "remaining": float("inf")},
    }


# ── P4.5c: return shape is (provider, model) tuples ─────────────────────────


class TestModelAwareReturnShape:
    """select_failover returns ((prov, model), (prov, model)), not (prov, prov)."""

    def test_chosen_is_provider_model_tuple(
        self, router, quota_both_exhausted, all_healthy
    ):
        result = router.select_failover(
            quota_state=quota_both_exhausted,
            health_state=all_healthy,
            peak=False,
        )
        chosen, fallback = result
        # chosen must be a 2-tuple (provider, model), not a bare string
        assert isinstance(chosen, tuple)
        assert len(chosen) == 2
        assert chosen[0] == "ollama_cloud"
        assert chosen[1] is not None
        assert isinstance(chosen[1], str)

    def test_chosen_model_matches_get_model(
        self, router, quota_both_exhausted, all_healthy
    ):
        """The model in the result must equal get_model(provider, 'coding')."""
        (chosen, chosen_model), _ = router.select_failover(
            quota_state=quota_both_exhausted,
            health_state=all_healthy,
            peak=False,
        )
        assert chosen == "ollama_cloud"
        # ollama_cloud coding → glm-5.2
        assert chosen_model == get_model("ollama_cloud", "coding")
        assert chosen_model == "glm-5.2"

    def test_fallback_is_provider_model_tuple(
        self, router, quota_both_exhausted, all_healthy
    ):
        """When fallback exists, it's also a (provider, model) tuple."""
        result = router.select_failover(
            quota_state=quota_both_exhausted,
            health_state=all_healthy,
            peak=False,
        )
        _, fallback = result
        # fallback is always a 2-tuple — either (provider, model) or (None, None)
        assert isinstance(fallback, tuple)
        assert len(fallback) == 2
        if fallback[0] is not None:
            assert fallback[1] is not None
            assert isinstance(fallback[1], str)


# ── P4.5d: task_type selects different models ───────────────────────────────


class TestTaskTypeModelSelection:
    """Different task_type values yield different model names for the same provider."""

    def test_coding_task_for_ollama(self, router, quota_both_exhausted, all_healthy):
        (chosen, model), _ = router.select_failover(
            quota_state=quota_both_exhausted,
            health_state=all_healthy,
            peak=False,
            task_type="coding",
        )
        assert chosen == "ollama_cloud"
        assert model == "glm-5.2"

    def test_chat_task_for_ollama(self, router, quota_both_exhausted, all_healthy):
        (chosen, model), _ = router.select_failover(
            quota_state=quota_both_exhausted,
            health_state=all_healthy,
            peak=False,
            task_type="chat",
        )
        assert chosen == "ollama_cloud"
        # chat task for ollama → glm-4.5-flash (not glm-5.2)
        assert model == "glm-4.5-flash"

    def test_simple_task_for_ollama(self, router, quota_both_exhausted, all_healthy):
        (chosen, model), _ = router.select_failover(
            quota_state=quota_both_exhausted,
            health_state=all_healthy,
            peak=False,
            task_type="simple",
        )
        assert chosen == "ollama_cloud"
        assert model == "glm-4.5-flash"

    def test_different_task_types_yield_different_models(
        self, router, quota_both_exhausted, all_healthy
    ):
        """coding and chat should pick different models for ollama_cloud."""
        (_, coding_model), _ = router.select_failover(
            quota_state=quota_both_exhausted,
            health_state=all_healthy,
            peak=False,
            task_type="coding",
        )
        (_, chat_model), _ = router.select_failover(
            quota_state=quota_both_exhausted,
            health_state=all_healthy,
            peak=False,
            task_type="chat",
        )
        assert coding_model != chat_model

    def test_external_provider_models_with_task_type(
        self, router, quota_all_high_tier_dead, only_externals_healthy
    ):
        """When an external like openrouter is chosen, its model respects task_type."""
        (chosen, model), (fallback, fallback_model) = router.select_failover(
            quota_state=quota_all_high_tier_dead,
            health_state=only_externals_healthy,
            peak=False,
            task_type="reasoning",
        )
        assert chosen == "openrouter"
        assert model == get_model("openrouter", "reasoning")
        assert model == "deepseek-v4-pro"
        # fallback is ppq
        assert fallback == "ppq"
        assert fallback_model == get_model("ppq", "reasoning")
        assert fallback_model == "kimi-k3"

    def test_task_type_changes_fallback_model(
        self, router, quota_all_high_tier_dead, only_externals_healthy
    ):
        """Fallback model should also reflect the requested task_type."""
        for tt in ("coding", "chat", "simple"):
            (_, _), (fallback, fallback_model) = router.select_failover(
                quota_state=quota_all_high_tier_dead,
                health_state=only_externals_healthy,
                peak=False,
                task_type=tt,
            )
            assert fallback == "ppq"
            assert fallback_model == get_model("ppq", tt)


# ── Default task_type is "coding" ────────────────────────────────────────────


class TestDefaultTaskType:
    def test_default_task_type_is_coding(
        self, router, quota_both_exhausted, all_healthy
    ):
        """When task_type is omitted, it defaults to 'coding'."""
        (chosen, default_model), _ = router.select_failover(
            quota_state=quota_both_exhausted,
            health_state=all_healthy,
            peak=False,
        )
        (chosen2, coding_model), _ = router.select_failover(
            quota_state=quota_both_exhausted,
            health_state=all_healthy,
            peak=False,
            task_type="coding",
        )
        assert chosen == chosen2
        assert default_model == coding_model

    def test_unrecognised_task_type_falls_back_to_coding(
        self, router, quota_both_exhausted, all_healthy
    ):
        """An unrecognised task_type should still produce a valid model."""
        (chosen, model), _ = router.select_failover(
            quota_state=quota_both_exhausted,
            health_state=all_healthy,
            peak=False,
            task_type="nonsense_task",
        )
        assert chosen == "ollama_cloud"
        # model_mapping falls back to coding default for unknown task types
        assert model == get_model("ollama_cloud", "coding")


# ── None handling ─────────────────────────────────────────────────────────────


class TestNoneModelHandling:
    def test_no_viable_provider_returns_none_none_tuples(
        self, router, quota_both_exhausted
    ):
        """When all providers are unhealthy, both tuples should be (None, None)."""
        all_unhealthy = {k: False for k in
                         ["ours", "friend", "ollama_cloud", "ppq", "openrouter", "deepinfra"]}
        result = router.select_failover(
            quota_state=quota_both_exhausted,
            health_state=all_unhealthy,
            peak=False,
        )
        chosen, fallback = result
        assert chosen == (None, None)
        assert fallback == (None, None)

    def test_exception_returns_none_none_tuples(self, router):
        """On exception, select_failover returns ((None, None), (None, None))."""
        result = router.select_failover(
            quota_state=None,
            health_state=None,
            peak="not-a-bool",
        )
        assert result == ((None, None), (None, None))
