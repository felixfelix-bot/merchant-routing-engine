"""Tests for DeepInfra as a per-token external provider.

Verifies that DeepInfra is correctly integrated into:
  - config/providers.yaml (external: section)
  - primary_router.py (_SEED_COSTS, _QUOTA_TOTALS)
  - shadow_hook.py (_SEED_COSTS, _QUOTA_TOTALS)
  - provider_names.py (CANONICAL_PROVIDERS)
  - external_failover.py (works via generic providers dict)
  - routing decisions (appears as a candidate, is selectable)
"""
from __future__ import annotations

import math
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.primary_router import PrimaryRouter, _SEED_COSTS as PR_SEEDS, _QUOTA_TOTALS as PR_QUOTAS
from src.shadow_hook import ShadowHook, _SEED_COSTS as SH_SEEDS, _QUOTA_TOTALS as SH_QUOTAS
from src.provider_names import CANONICAL_PROVIDERS, normalize_provider_name
from src.external_failover import get_cheapest_funded, try_external_failover
from src.routing_optimizer import RoutingOptimizer
from src.price_kalman import PriceKalman
from src.consumption_kalman import ConsumptionKalman


# ── Config / static data tests ───────────────────────────────────────────────


class TestDeepInfraInSeedCosts:
    """DeepInfra must appear in _SEED_COSTS in both primary_router and shadow_hook."""

    def test_deepinfra_in_primary_router_seed_costs(self):
        assert "deepinfra" in PR_SEEDS
        assert PR_SEEDS["deepinfra"] == 1.30

    def test_deepinfra_in_shadow_hook_seed_costs(self):
        assert "deepinfra" in SH_SEEDS
        assert SH_SEEDS["deepinfra"] == 1.30

    def test_seed_costs_match_between_modules(self):
        """Primary router and shadow hook must have the same seed for deepinfra."""
        assert PR_SEEDS["deepinfra"] == SH_SEEDS["deepinfra"]

    def test_deepinfra_seed_is_positive(self):
        assert PR_SEEDS["deepinfra"] > 0
        assert SH_SEEDS["deepinfra"] > 0


class TestDeepInfraInQuotaTotals:
    """DeepInfra must appear in _QUOTA_TOTALS with inf (per-token, no hard quota)."""

    def test_deepinfra_in_primary_router_quota_totals(self):
        assert "deepinfra" in PR_QUOTAS
        assert PR_QUOTAS["deepinfra"] == float("inf")

    def test_deepinfra_in_shadow_hook_quota_totals(self):
        assert "deepinfra" in SH_QUOTAS
        assert SH_QUOTAS["deepinfra"] == float("inf")

    def test_quota_totals_match_between_modules(self):
        assert PR_QUOTAS["deepinfra"] == SH_QUOTAS["deepinfra"]


class TestDeepInfraInProviderNames:
    """DeepInfra must be a recognised canonical provider name."""

    def test_deepinfra_in_canonical_providers(self):
        assert "deepinfra" in CANONICAL_PROVIDERS

    def test_deepinfra_passes_through_normalization(self):
        assert normalize_provider_name("deepinfra") == "deepinfra"

    def test_deepinfra_normalization_idempotent(self):
        once = normalize_provider_name("deepinfra")
        twice = normalize_provider_name(once)
        assert once == twice == "deepinfra"


class TestDeepInfraInConfig:
    """Verify config/providers.yaml has the deepinfra section."""

    def test_config_has_deepinfra_section(self):
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config", "providers.yaml",
        )
        with open(config_path) as f:
            content = f.read()
        assert "deepinfra:" in content
        assert "DEEPINFRA_API_KEY" in content
        assert "api.deepinfra.com" in content
        assert "per_token" in content


# ── Routing decision tests ───────────────────────────────────────────────────


class TestDeepInfraInRouting:
    """DeepInfra should appear as a routing candidate and be selectable."""

    def test_deepinfra_is_routing_candidate(self):
        """When all providers are healthy, deepinfra appears in optimizer candidates."""
        opt = RoutingOptimizer(peak_hours_utc=(6, 10), peak_mult=3.0)
        opt.add_provider(
            "ours", PriceKalman(initial_rate=0.31), ConsumptionKalman(),
            quota_remaining=1_400_000, model_tier="high", quota_total=2_000_000,
            peak_hours_utc=(6, 10), peak_mult=3.0,
        )
        opt.add_provider(
            "deepinfra", PriceKalman(initial_rate=1.30), ConsumptionKalman(),
            quota_remaining=float("inf"), model_tier="low",
        )
        result = opt.route(difficulty="low", estimated_tokens=5000, hour=15)
        candidate_names = {c["provider"] for c in result["candidates"]}
        assert "deepinfra" in candidate_names

    def test_deepinfra_is_viable_when_healthy(self):
        """DeepInfra should be viable (not filtered) when healthy with inf quota."""
        opt = RoutingOptimizer(peak_hours_utc=(6, 10), peak_mult=3.0)
        opt.add_provider(
            "deepinfra", PriceKalman(initial_rate=1.30), ConsumptionKalman(),
            quota_remaining=float("inf"), model_tier="low",
        )
        result = opt.route(difficulty="low", estimated_tokens=5000, hour=15)
        deepinfra_candidate = next(
            c for c in result["candidates"] if c["provider"] == "deepinfra"
        )
        assert deepinfra_candidate["viable"] is True

    def test_deepinfra_filtered_for_high_difficulty(self):
        """DeepInfra is low-tier → should not be viable for high-difficulty requests."""
        opt = RoutingOptimizer(peak_hours_utc=(6, 10), peak_mult=3.0)
        opt.add_provider(
            "deepinfra", PriceKalman(initial_rate=1.30), ConsumptionKalman(),
            quota_remaining=float("inf"), model_tier="low",
        )
        result = opt.route(difficulty="high", estimated_tokens=5000, hour=15)
        deepinfra_candidate = next(
            c for c in result["candidates"] if c["provider"] == "deepinfra"
        )
        assert deepinfra_candidate["viable"] is False

    def test_deepinfra_not_chosen_when_cheaper_available(self):
        """Off-peak, ours ($0.31/M) should beat deepinfra ($1.30/M)."""
        opt = RoutingOptimizer(peak_hours_utc=(6, 10), peak_mult=3.0)
        opt.add_provider(
            "ours", PriceKalman(initial_rate=0.31), ConsumptionKalman(),
            quota_remaining=1_400_000, model_tier="high", quota_total=2_000_000,
            peak_hours_utc=(6, 10), peak_mult=3.0,
        )
        opt.add_provider(
            "deepinfra", PriceKalman(initial_rate=1.30), ConsumptionKalman(),
            quota_remaining=float("inf"), model_tier="low",
        )
        result = opt.route(difficulty="low", estimated_tokens=5000, hour=15)
        assert result["chosen_provider"] == "ours"

    def test_deepinfra_chosen_when_only_viable(self):
        """If only deepinfra is healthy and difficulty is low, it should be chosen."""
        opt = RoutingOptimizer(peak_hours_utc=(6, 10), peak_mult=3.0)
        opt.add_provider(
            "ours", PriceKalman(initial_rate=0.31), ConsumptionKalman(),
            quota_remaining=0, model_tier="high", quota_total=2_000_000,
            breaker_tripped=True,
            peak_hours_utc=(6, 10), peak_mult=3.0,
        )
        opt.add_provider(
            "deepinfra", PriceKalman(initial_rate=1.30), ConsumptionKalman(),
            quota_remaining=float("inf"), model_tier="low",
        )
        result = opt.route(difficulty="low", estimated_tokens=5000, hour=15)
        assert result["chosen_provider"] == "deepinfra"


# ── PrimaryRouter integration tests ──────────────────────────────────────────


class TestDeepInfraInPrimaryRouter:
    """DeepInfra should be initialised in PrimaryRouter's Kalman dicts."""

    def test_deepinfra_has_price_kalman(self, monkeypatch):
        PrimaryRouter._instance = None
        monkeypatch.setattr(PrimaryRouter, "_load_converged_rates", staticmethod(lambda: {}))
        router = PrimaryRouter()
        assert "deepinfra" in router._price_kalmans

    def test_deepinfra_has_consumption_kalman(self, monkeypatch):
        PrimaryRouter._instance = None
        monkeypatch.setattr(PrimaryRouter, "_load_converged_rates", staticmethod(lambda: {}))
        router = PrimaryRouter()
        assert "deepinfra" in router._consumption_kalmans

    def test_deepinfra_burn_rate_tracking(self, monkeypatch):
        """update_burn_rate should work for deepinfra."""
        PrimaryRouter._instance = None
        monkeypatch.setattr(PrimaryRouter, "_load_converged_rates", staticmethod(lambda: {}))
        router = PrimaryRouter()
        router.update_burn_rate("deepinfra", 10000)
        assert router._consumption_kalmans["deepinfra"].tokens_used == 10000

    def test_deepinfra_routes_to_none(self, monkeypatch):
        """PrimaryRouter should return None (not raise) when optimizer considers deepinfra.

        DeepInfra is a low-tier external provider → maps to None in the proxy's
        key namespace, same as ppq/openrouter.
        """
        PrimaryRouter._instance = None
        monkeypatch.setattr(PrimaryRouter, "_load_converged_rates", staticmethod(lambda: {}))
        router = PrimaryRouter()

        quota = {
            "ours":          {"used_pct": 99.0, "remaining": 0, "total": 2_000_000},
            "friend":        {"used_pct": 99.0, "remaining": 0, "total": 2_000_000},
            "ollama_cloud":  {"used_pct": 99.0, "remaining": 0, "total": 1_000_000},
            "ppq":           {"used_pct": 0.0, "remaining": float("inf")},
            "openrouter":    {"used_pct": 0.0, "remaining": float("inf")},
            "deepinfra":     {"used_pct": 0.0, "remaining": float("inf")},
        }
        health = {k: True for k in quota}
        # All z.ai + ollama exhausted → optimizer picks cheapest per-token
        # (ppq at $0.14 or openrouter at $0.135, both cheaper than deepinfra).
        # Either way, the result maps to None for the proxy.
        result = router.route(model="glm-4.5-flash", tokens=5000,
                             quota_state=quota, health_state=health)
        assert result is None

    def test_deepinfra_never_breaks_router(self, monkeypatch):
        """Router with deepinfra should handle edge cases without raising."""
        PrimaryRouter._instance = None
        monkeypatch.setattr(PrimaryRouter, "_load_converged_rates", staticmethod(lambda: {}))
        router = PrimaryRouter()
        # Empty/garbage inputs
        r = router.route()
        assert r is None or isinstance(r, str)
        r = router.route(None, 0, None, None, None)
        assert r is None or isinstance(r, str)


# ── ShadowHook integration tests ─────────────────────────────────────────────


@pytest.fixture
def tmp_db():
    """Temp DB for ShadowHook tests."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


class TestDeepInfraInShadowHook:
    """DeepInfra should be initialised in ShadowHook's Kalman dicts."""

    def test_deepinfra_has_price_kalman(self, tmp_db):
        hook = ShadowHook(db_path=tmp_db)
        assert "deepinfra" in hook._price_kalmans

    def test_deepinfra_has_consumption_kalman(self, tmp_db):
        hook = ShadowHook(db_path=tmp_db)
        assert "deepinfra" in hook._consumption_kalmans

    def test_deepinfra_compare_never_raises(self, tmp_db):
        """Shadow compare with deepinfra in the mix should never raise."""
        hook = ShadowHook(db_path=tmp_db)
        quota = {
            "ours": {"used_pct": 30.0, "remaining": 1_400_000, "total": 2_000_000},
            "friend": {"used_pct": 45.0, "remaining": 1_100_000, "total": 2_000_000},
            "ollama_cloud": {"used_pct": 20.0, "remaining": 800_000, "total": 1_000_000},
            "ppq": {"used_pct": 0.0, "remaining": float("inf")},
            "openrouter": {"used_pct": 0.0, "remaining": float("inf")},
            "deepinfra": {"used_pct": 0.0, "remaining": float("inf")},
        }
        health = {k: True for k in quota}
        hook.compare("ours", "glm-5.2", 5000, quota, health, False)
        assert hook.get_stats()["total_decisions"] == 1

    def test_deepinfra_burn_rate_tracking(self, tmp_db):
        """ShadowHook should track burn rate for deepinfra."""
        hook = ShadowHook(db_path=tmp_db)
        quota = {
            "ours": {"used_pct": 30.0, "remaining": 1_400_000, "total": 2_000_000},
            "friend": {"used_pct": 45.0, "remaining": 1_100_000, "total": 2_000_000},
            "ollama_cloud": {"used_pct": 20.0, "remaining": 800_000, "total": 1_000_000},
            "ppq": {"used_pct": 0.0, "remaining": float("inf")},
            "openrouter": {"used_pct": 0.0, "remaining": float("inf")},
            "deepinfra": {"used_pct": 0.0, "remaining": float("inf")},
        }
        health = {k: True for k in quota}
        hook.compare("deepinfra", "deepseek/deepseek-v4-flash", 5000, quota, health, False)
        assert hook._consumption_kalmans["deepinfra"].tokens_used == 5000


# ── External failover tests ──────────────────────────────────────────────────


class TestDeepInfraInExternalFailover:
    """DeepInfra should work with external_failover's generic providers dict."""

    def test_deepinfra_in_cheapest_funded(self):
        """DeepInfra should appear in cheapest-funded list when it has a key."""
        # Reset in-memory funding state so all providers are funded
        from src import provider_funding_tracker
        provider_funding_tracker._provider_health.clear()

        providers = {
            "ppq": {"base_url": "https://api.ppq.ai/v1", "key": "test-key"},
            "openrouter": {"base_url": "https://openrouter.ai/api/v1", "key": "test-key"},
            "deepinfra": {"base_url": "https://api.deepinfra.com/v1/openai", "key": "test-key"},
        }
        # Simple cost function: ppq=0.14, openrouter=0.135, deepinfra=1.30
        def cost_fn(name, model):
            return {"ppq": 0.14, "openrouter": 0.135, "deepinfra": 1.30}.get(name, 1.0)

        candidates = get_cheapest_funded(providers, cost_fn)
        names = [c[1] for c in candidates]
        assert "deepinfra" in names
        # DeepInfra should be last (most expensive)
        assert names[-1] == "deepinfra"

    def test_deepinfra_without_key_skipped(self):
        """DeepInfra without a key should be skipped."""
        from src import provider_funding_tracker
        provider_funding_tracker._provider_health.clear()

        providers = {
            "deepinfra": {"base_url": "https://api.deepinfra.com/v1/openai", "key": ""},
        }
        candidates = get_cheapest_funded(providers)
        assert len(candidates) == 0