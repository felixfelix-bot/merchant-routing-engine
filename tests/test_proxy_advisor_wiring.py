"""Tests for the production proxy advisor wiring (Phase 2.2 / task P2.2).

The core RoutingAdvisor decision logic is covered by
``test_advisor_integration.py`` (20 cases). This module pins the two
PROXY-SPECIFIC pieces that ``~/.hermes/bot/zai_proxy.py`` adds on top:

1. ``_ProxyRoutingAdvisor`` — a RoutingAdvisor subclass that ALSO honours a
   ``.optimizer_advisor_mode`` file marker so operators can hot-swap advisor
   mode without a restart or env-var change (``touch``/``rm``).
2. ``_best_key_adapter`` — adapts the production ``best_key()`` (returns a
   bare ``str | None``) into the ``AdvisorDecision`` contract.

These are mirrored here (not imported from the 2600-line production script,
which is not import-light in the test environment) so the wiring contract is
pinned against drift.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.routing_advisor import AdvisorDecision, RoutingAdvisor


# ── Fakes (same shape as test_advisor_integration.py) ────────────────────────


class FakeOptimizer:
    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.calls = 0

    def route(self, difficulty="medium", estimated_tokens=0, hour=None):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.result


def _opt_result(provider, model="glm-5.2", cost=0.31):
    return {
        "chosen_provider": provider,
        "chosen_model": model,
        "effective_cost_per_1m": cost,
        "reason": f"cheapest viable: {provider}",
        "candidates": [],
    }


# ── Mirror of the production wiring ──────────────────────────────────────────


def make_proxy_advisor(optimizer, flag_path, *,
                       best_key_value: "str | None" = "ours",
                       env_var="ROUTING_ADVISOR_ENABLED"):
    """Build a (ProxyRoutingAdvisor, best_key_fn) pair mirroring zai_proxy.py.

    The subclass honours BOTH the file marker and the env var; the adapter maps
    a bare ``best_key()`` string (or None) into an AdvisorDecision.
    """
    bk_calls: dict = {"n": 0}

    def best_key():
        bk_calls["n"] += 1
        return best_key_value

    def best_key_adapter():
        _k = best_key()
        _prov = ("ours" if _k == "ours"
                 else "friend" if _k == "friend"
                 else "fallback")
        return AdvisorDecision(provider=_prov, model="", key=_k,
                               source="best_key")

    class ProxyRoutingAdvisor(RoutingAdvisor):
        def enabled(self):
            if os.path.exists(flag_path):
                return True
            return super().enabled()

    advisor = ProxyRoutingAdvisor(optimizer, best_key_adapter, env_var=env_var)
    return advisor, bk_calls


# ── 1. File-marker flag (hot-swap via touch / rm) ────────────────────────────


class TestFileMarkerFlag:
    def test_file_marker_enables(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ROUTING_ADVISOR_ENABLED", raising=False)
        flag = tmp_path / ".optimizer_advisor_mode"
        opt = FakeOptimizer(result=_opt_result("ours"))
        advisor, _ = make_proxy_advisor(opt, str(flag))
        assert advisor.enabled() is False  # nothing set yet

        flag.touch()
        assert advisor.enabled() is True

        flag.unlink()
        assert advisor.enabled() is False

    def test_file_marker_wins_over_env_var_off(self, tmp_path, monkeypatch):
        """If the env var is unset but the marker exists, advisor is ON."""
        monkeypatch.delenv("ROUTING_ADVISOR_ENABLED", raising=False)
        flag = tmp_path / ".optimizer_advisor_mode"
        flag.touch()
        opt = FakeOptimizer(result=_opt_result("ours"))
        advisor, _ = make_proxy_advisor(opt, str(flag))
        assert advisor.enabled() is True

    def test_env_var_still_works_without_marker(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ROUTING_ADVISOR_ENABLED", "1")
        flag = tmp_path / ".optimizer_advisor_mode"  # does NOT exist
        opt = FakeOptimizer(result=_opt_result("ours"))
        advisor, _ = make_proxy_advisor(opt, str(flag))
        assert advisor.enabled() is True


# ── 2. best_key adapter mapping ──────────────────────────────────────────────


class TestBestKeyAdapter:
    def test_adapter_maps_known_keys(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ROUTING_ADVISOR_ENABLED", raising=False)
        for kv, prov in [("ours", "ours"), ("friend", "friend")]:
            opt = FakeOptimizer(result=_opt_result("ours"))
            advisor, _ = make_proxy_advisor(opt, str(tmp_path / "f"),
                                            best_key_value=kv)
            dec = advisor.decide()  # flag off → best_key adapter path
            assert dec.source == "best_key"
            assert dec.key == kv
            assert dec.provider == prov

    def test_adapter_maps_none_to_fallback(self, tmp_path, monkeypatch):
        """best_key() returning None (both keys exhausted) → provider 'fallback'."""
        monkeypatch.delenv("ROUTING_ADVISOR_ENABLED", raising=False)
        opt = FakeOptimizer(result=_opt_result("ours"))
        advisor, _ = make_proxy_advisor(opt, str(tmp_path / "f"),
                                        best_key_value=None)
        dec = advisor.decide()
        assert dec.key is None
        assert dec.provider == "fallback"
        assert dec.source == "best_key"


# ── 3. End-to-end decision flow (flag off / on / fallback / ollama) ───────────


class TestProxyDecisionFlow:
    def test_flag_off_uses_best_key_optimizer_unconsulted(self, tmp_path,
                                                          monkeypatch):
        monkeypatch.delenv("ROUTING_ADVISOR_ENABLED", raising=False)
        opt = FakeOptimizer(result=_opt_result("ours"))
        advisor, bk = make_proxy_advisor(opt, str(tmp_path / "f"))
        dec = advisor.decide()
        assert opt.calls == 0
        assert bk["n"] == 1
        assert dec.source == "best_key"

    def test_flag_on_optimizer_first_best_key_fallback(self, tmp_path,
                                                       monkeypatch):
        monkeypatch.delenv("ROUTING_ADVISOR_ENABLED", raising=False)
        flag = tmp_path / ".optimizer_advisor_mode"
        flag.touch()
        opt = FakeOptimizer(result=_opt_result("friend"))
        advisor, bk = make_proxy_advisor(opt, str(flag))
        dec = advisor.decide()
        assert opt.calls == 1
        assert bk["n"] == 0  # optimizer succeeded, no fallback
        assert dec.source == "optimizer"
        assert dec.key == "friend"

    def test_flag_on_optimizer_exception_falls_back(self, tmp_path,
                                                    monkeypatch):
        monkeypatch.delenv("ROUTING_ADVISOR_ENABLED", raising=False)
        flag = tmp_path / ".optimizer_advisor_mode"
        flag.touch()
        opt = FakeOptimizer(exc=RuntimeError("boom"))
        advisor, bk = make_proxy_advisor(opt, str(flag))
        dec = advisor.decide()
        assert opt.calls == 1
        assert bk["n"] == 1  # fallback invoked
        assert dec.source == "best_key"
        assert "boom" in dec.reason

    def test_flag_on_ollama_routes_directly(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ROUTING_ADVISOR_ENABLED", raising=False)
        flag = tmp_path / ".optimizer_advisor_mode"
        flag.touch()
        opt = FakeOptimizer(result=_opt_result("ollama_cloud", cost=0.024))
        advisor, _ = make_proxy_advisor(opt, str(flag))
        dec = advisor.decide()
        assert dec.source == "optimizer"
        assert dec.routed_directly_to_ollama is True
        assert dec.provider == "ollama_cloud"

    def test_flag_on_invalid_provider_falls_back(self, tmp_path, monkeypatch):
        """Optimizer returns its 'fallback' sentinel → best_key fallback."""
        monkeypatch.delenv("ROUTING_ADVISOR_ENABLED", raising=False)
        flag = tmp_path / ".optimizer_advisor_mode"
        flag.touch()
        opt = FakeOptimizer(result=_opt_result("fallback"))
        advisor, bk = make_proxy_advisor(opt, str(flag))
        dec = advisor.decide()
        assert dec.source == "best_key"
        assert bk["n"] == 1
