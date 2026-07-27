"""Integration tests for src/routing_advisor.py — the Phase 2 feature-flag logic.

The advisor is the half-step between shadow mode (log only) and primary mode
(replace best_key entirely): when the flag is on, the optimizer is consulted
first and ``best_key()`` becomes the fallback. These tests pin that contract
with injectable fakes — no production Kalman stack, no live proxy.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.routing_advisor import (
    KNOWN_PROVIDERS,
    AdvisorDecision,
    RoutingAdvisor,
)


# ── Fakes ───────────────────────────────────────────────────────────────────


class FakeOptimizer:
    """Stand-in for RoutingOptimizer with controllable route() behaviour."""

    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.calls = 0
        self.last_kwargs = None

    def route(self, difficulty="medium", estimated_tokens=0, hour=None):
        self.calls += 1
        self.last_kwargs = {
            "difficulty": difficulty,
            "estimated_tokens": estimated_tokens,
            "hour": hour,
        }
        if self.exc is not None:
            raise self.exc
        return self.result


def make_best_key(provider="ours", model="glm-5.2", key="ours"):
    """Return (best_key_fn, calls_dict). fn() returns a legacy decision."""
    calls = {"n": 0}

    def best_key_fn() -> AdvisorDecision:
        calls["n"] += 1
        return AdvisorDecision(
            provider=provider,
            model=model,
            key=key,
            source="best_key",
            reason="legacy best_key selection",
        )

    return best_key_fn, calls


def _opt_result(provider, model="glm-5.2", cost=0.31):
    return {
        "chosen_provider": provider,
        "chosen_model": model,
        "effective_cost_per_1m": cost,
        "reason": f"cheapest viable: {provider}",
        "candidates": [],
    }


# ── Feature flag OFF → best_key is used, optimizer never called ─────────────


class TestFlagOff:
    def test_uses_best_key_optimizer_not_called(self):
        opt = FakeOptimizer(result=_opt_result("ours"))
        best_key, bk_calls = make_best_key()
        advisor = RoutingAdvisor(opt, best_key, enabled=False)

        dec = advisor.decide(difficulty="medium", estimated_tokens=5000)

        assert opt.calls == 0  # optimizer never consulted
        assert bk_calls["n"] == 1
        assert dec.source == "best_key"
        assert dec.provider == "ours"

    def test_flag_off_does_not_read_env_by_default(self, monkeypatch):
        # Even if the env var says "on", an explicit enabled=False wins.
        monkeypatch.setenv("ROUTING_ADVISOR_ENABLED", "true")
        opt = FakeOptimizer(result=_opt_result("ollama_cloud"))
        best_key, _ = make_best_key()
        advisor = RoutingAdvisor(opt, best_key, enabled=False)

        dec = advisor.decide()
        assert opt.calls == 0
        assert dec.source == "best_key"


# ── Feature flag ON → optimizer is called first ─────────────────────────────


class TestFlagOn:
    def test_optimizer_called_first_and_used(self):
        opt = FakeOptimizer(result=_opt_result("ours", cost=0.31))
        best_key, bk_calls = make_best_key()
        advisor = RoutingAdvisor(opt, best_key, enabled=True)

        dec = advisor.decide(difficulty="high", estimated_tokens=8000, hour=8)

        assert opt.calls == 1
        assert opt.last_kwargs == {
            "difficulty": "high",
            "estimated_tokens": 8000,
            "hour": 8,
        }
        assert bk_calls["n"] == 0  # no fallback — best_key untouched
        assert dec.source == "optimizer"
        assert dec.provider == "ours"
        assert dec.key == "ours"
        assert dec.effective_cost_per_1m == pytest.approx(0.31)
        assert dec.routed_directly_to_ollama is False

    def test_env_var_enables_flag(self, monkeypatch):
        monkeypatch.setenv("ROUTING_ADVISOR_ENABLED", "1")
        opt = FakeOptimizer(result=_opt_result("friend"))
        best_key, _ = make_best_key()
        advisor = RoutingAdvisor(opt, best_key)  # enabled=None → reads env

        dec = advisor.decide()
        assert advisor.enabled() is True
        assert opt.calls == 1
        assert dec.source == "optimizer"
        assert dec.provider == "friend"
        assert dec.key == "friend"

    @pytest.mark.parametrize("val", ["TRUE", "Yes", "on", "1"])
    def test_truthy_env_values_enable(self, monkeypatch, val):
        monkeypatch.setenv("ROUTING_ADVISOR_ENABLED", val)
        opt = FakeOptimizer(result=_opt_result("ours"))
        advisor = RoutingAdvisor(opt, make_best_key()[0])
        assert advisor.enabled() is True

    @pytest.mark.parametrize("val", ["", "0", "false", "no", "off", "  "])
    def test_falsy_env_values_disable(self, monkeypatch, val):
        monkeypatch.setenv("ROUTING_ADVISOR_ENABLED", val)
        opt = FakeOptimizer(result=_opt_result("ours"))
        advisor = RoutingAdvisor(opt, make_best_key()[0])
        assert advisor.enabled() is False


# ── Optimizer failure → fall back to best_key ───────────────────────────────


class TestOptimizerFallback:
    def test_optimizer_exception_falls_back(self):
        opt = FakeOptimizer(exc=RuntimeError("optimizer exploded"))
        best_key, bk_calls = make_best_key()
        advisor = RoutingAdvisor(opt, best_key, enabled=True)

        dec = advisor.decide()

        assert opt.calls == 1
        assert bk_calls["n"] == 1  # fell back
        assert dec.source == "best_key"
        assert "optimizer raised" in dec.reason
        assert "RuntimeError" in dec.reason

    def test_optimizer_exception_type_preserved_in_reason(self):
        opt = FakeOptimizer(exc=ValueError("bad state"))
        best_key, _ = make_best_key()
        advisor = RoutingAdvisor(opt, best_key, enabled=True)

        dec = advisor.decide()
        assert dec.source == "best_key"
        assert "ValueError" in dec.reason
        assert "bad state" in dec.reason

    def test_optimizer_returns_unknown_provider_falls_back(self):
        # "fallback" is the optimizer's own sentinel for "nothing viable" —
        # it is NOT a real provider, so we must not route to it.
        opt = FakeOptimizer(result=_opt_result("fallback"))
        best_key, bk_calls = make_best_key()
        advisor = RoutingAdvisor(opt, best_key, enabled=True)

        dec = advisor.decide()

        assert opt.calls == 1
        assert bk_calls["n"] == 1
        assert dec.source == "best_key"
        assert "invalid provider" in dec.reason
        assert "fallback" in dec.reason

    def test_optimizer_returns_bogus_provider_falls_back(self):
        opt = FakeOptimizer(result=_opt_result("totally-made-up"))
        best_key, _ = make_best_key()
        advisor = RoutingAdvisor(opt, best_key, enabled=True)

        dec = advisor.decide()
        assert dec.source == "best_key"
        assert "invalid provider" in dec.reason

    def test_optimizer_returns_none_falls_back(self):
        opt = FakeOptimizer(result=None)
        best_key, _ = make_best_key()
        advisor = RoutingAdvisor(opt, best_key, enabled=True)

        dec = advisor.decide()
        assert dec.source == "best_key"

    def test_optimizer_inf_cost_normalised_to_none(self):
        # A viable provider but infinite cost (shouldn't happen, but be safe)
        # is normalised rather than propagated as inf.
        opt = FakeOptimizer(
            result=_opt_result("ours", cost=float("inf"))
        )
        best_key, _ = make_best_key()
        advisor = RoutingAdvisor(opt, best_key, enabled=True)

        dec = advisor.decide()
        assert dec.source == "optimizer"  # still a valid provider
        assert dec.effective_cost_per_1m is None  # inf normalised away


# ── Optimizer returns ollama_cloud → route directly ─────────────────────────


class TestOllamaDirectRoute:
    def test_ollama_routes_directly(self):
        opt = FakeOptimizer(result=_opt_result("ollama_cloud", model="ollama", cost=0.50))
        best_key, bk_calls = make_best_key()
        advisor = RoutingAdvisor(opt, best_key, enabled=True)

        dec = advisor.decide()

        assert opt.calls == 1
        assert bk_calls["n"] == 0  # no fallback — this is a valid, honoured choice
        assert dec.source == "optimizer"
        assert dec.provider == "ollama_cloud"
        assert dec.routed_directly_to_ollama is True
        assert dec.effective_cost_per_1m == pytest.approx(0.50)

    def test_ollama_decision_has_no_zai_key(self):
        opt = FakeOptimizer(result=_opt_result("ollama_cloud"))
        advisor = RoutingAdvisor(opt, make_best_key()[0], enabled=True)
        dec = advisor.decide()
        # ollama bypasses the z.ai path — there is no ours/friend key.
        assert dec.key is None


# ── Known-providers whitelist ───────────────────────────────────────────────


class TestProviderWhitelist:
    def test_every_known_provider_accepted(self):
        for prov in KNOWN_PROVIDERS:
            opt = FakeOptimizer(result=_opt_result(prov))
            advisor = RoutingAdvisor(opt, make_best_key()[0], enabled=True)
            dec = advisor.decide()
            assert dec.source == "optimizer", f"{prov} should be accepted"
            assert dec.provider == prov

    def test_custom_whitelist_restricts(self):
        # Advisor told to only trust ours: ollama_cloud now falls back.
        opt = FakeOptimizer(result=_opt_result("ollama_cloud"))
        best_key, bk_calls = make_best_key()
        advisor = RoutingAdvisor(
            opt, best_key, providers=frozenset({"ours"}), enabled=True
        )
        dec = advisor.decide()
        assert dec.source == "best_key"
        assert bk_calls["n"] == 1


# ── Advisor never raises ────────────────────────────────────────────────────


class TestNeverRaises:
    def test_decide_never_raises(self):
        # A barrage of bad configs must all degrade to best_key, never raise.
        bk = make_best_key()[0]
        advisors = [
            RoutingAdvisor(FakeOptimizer(exc=Exception("boom")), bk, enabled=True),
            RoutingAdvisor(FakeOptimizer(result={}), bk, enabled=True),
            RoutingAdvisor(
                FakeOptimizer(result={"chosen_provider": 123}), bk, enabled=True
            ),
            RoutingAdvisor(FakeOptimizer(result=None), bk, enabled=True),
        ]
        for advisor in advisors:
            dec = advisor.decide()  # must not raise
            assert isinstance(dec, AdvisorDecision)
