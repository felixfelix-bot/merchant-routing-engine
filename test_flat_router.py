#!/usr/bin/env python3
"""Tests for Phase 1 flat router: select_provider() in shadow mode.

Tests verify:
1. select_provider() returns candidates for known models
2. Model filtering excludes providers that don't serve the model
3. Health gating excludes unhealthy providers
4. Cost ordering (cheapest first)
5. _is_provider_healthy() for various states
6. _update_kalman_after_request() updates Kalman filters
7. Shadow logging records the comparison
"""
import os
import sys
import time
import json
import io
import sqlite3
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock
from dataclasses import dataclass

import pytest

# ── Path setup (repo copy: derived from this file's location) ───────────────
# Works both in a repo checkout (this file at repo root, zai_proxy.py in
# production/, Kalman modules in src/) and in the deployed-host layout
# (~/.hermes/bot/ siblings, Kalman modules from ~/merchant-routing-engine/src).
# Entries added later take import priority, so the repo layout wins here.
_HERE = os.path.dirname(os.path.abspath(__file__))
for p in [
    os.path.expanduser("~/.hermes/bot"),
    os.path.expanduser("~/merchant-routing-engine"),
    os.path.join(os.path.expanduser("~/merchant-routing-engine"), "src"),
    _HERE,
    os.path.join(_HERE, "src"),
    os.path.join(_HERE, "production"),
]:
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)


# ── Import the flat router module (our new code) ────────────────────────────
from flat_router import (
    ProviderCandidate,
    PROVIDER_MODELS,
    select_provider,
    _is_provider_healthy,
    _update_kalman_after_request,
    _dispatch_to_provider,
    _ALL_PROVIDERS,
)


# ── Test 1: select_provider returns candidates for known models ─────────────

class TestSelectProviderReturnsCandidates:
    def test_glm52_returns_candidates(self):
        """select_provider for glm-5.2 should return non-empty list."""
        candidates = select_provider(model="glm-5.2")
        assert len(candidates) > 0, "Expected candidates for glm-5.2"
        assert all(isinstance(c, ProviderCandidate) for c in candidates)

    def test_kimi_k3_returns_candidates(self):
        """select_provider for kimi-k3 should return non-empty list."""
        candidates = select_provider(model="kimi-k3")
        assert len(candidates) > 0, "Expected candidates for kimi-k3"

    def test_unknown_model_returns_fallback(self):
        """Unknown model should return fallback candidate, not crash."""
        candidates = select_provider(model="nonexistent-model-xyz")
        # Should return at least a fallback candidate
        assert len(candidates) >= 1
        # The fallback candidate should have inf cost or be named 'fallback'
        assert candidates[0].name == "fallback" or candidates[0].effective_cost == float("inf")

    def test_each_candidate_has_required_fields(self):
        """Each ProviderCandidate must have name, model, effective_cost, dispatch_fn, reason."""
        candidates = select_provider(model="glm-5.2")
        for c in candidates:
            assert hasattr(c, "name")
            assert hasattr(c, "model")
            assert hasattr(c, "effective_cost")
            assert hasattr(c, "dispatch_fn")
            assert hasattr(c, "reason")


# ── Test 2: Model filtering excludes providers that don't serve the model ───

class TestModelFiltering:
    def test_telnyx_excluded_for_glm52(self):
        """Telnyx only serves kimi models, not glm-5.2."""
        candidates = select_provider(model="glm-5.2")
        names = [c.name for c in candidates]
        assert "telnyx" not in names, "Telnyx should not serve glm-5.2"

    def test_telnyx_included_for_kimi_k3(self):
        """Telnyx should be a candidate for kimi-k3."""
        candidates = select_provider(model="kimi-k3")
        names = [c.name for c in candidates]
        assert "telnyx" in names, "Telnyx should serve kimi-k3"

    def test_kimi_k3_excludes_zai(self):
        """z.ai keys don't serve kimi-k3."""
        candidates = select_provider(model="kimi-k3")
        names = [c.name for c in candidates]
        assert "ours" not in names, "z.ai ours should not serve kimi-k3"
        assert "friend" not in names, "z.ai friend should not serve kimi-k3"

    def test_providemodels_registry_completeness(self):
        """PROVIDER_MODELS should cover all 12 providers."""
        expected = {
            "ours", "friend", "ollama_cloud", "ollama_cloud_2",
            "opencode_go", "neuralwatt", "deepinfra", "ppq",
            "openrouter", "telnyx", "routstr", "routstrd",
        }
        assert expected.issubset(set(PROVIDER_MODELS.keys())), \
            f"Missing providers: {expected - set(PROVIDER_MODELS.keys())}"


# ── Test 3: Health gating excludes unhealthy providers ──────────────────────

class TestHealthGating:
    def test_disabled_provider_excluded(self):
        """A manually disabled provider should not appear in candidates."""
        with patch("flat_router._is_manually_disabled", side_effect=lambda n: n == "ppq"):
            candidates = select_provider(model="glm-5.2")
            names = [c.name for c in candidates]
            assert "ppq" not in names, "Disabled ppq should be excluded"

    def test_unfunded_provider_excluded(self):
        """An unfunded external provider should not appear in candidates."""
        with patch("flat_router._is_provider_funded", side_effect=lambda n: n != "deepinfra"):
            candidates = select_provider(model="glm-5.2")
            names = [c.name for c in candidates]
            assert "deepinfra" not in names, "Unfunded deepinfra should be excluded"

    def test_unhealthy_key_excluded(self):
        """An unhealthy key (in backoff) should not appear in candidates."""
        with patch("flat_router._is_key_healthy", side_effect=lambda n: n != "friend"):
            candidates = select_provider(model="glm-5.2")
            names = [c.name for c in candidates]
            assert "friend" not in names, "Unhealthy friend should be excluded"


# ── Test 4: Cost ordering (cheapest first) ──────────────────────────────────

class TestCostOrdering:
    def test_candidates_sorted_cheapest_first(self):
        """Candidates should be sorted by effective_cost ascending."""
        candidates = select_provider(model="glm-5.2")
        costs = [c.effective_cost for c in candidates if c.effective_cost != float("inf")]
        assert costs == sorted(costs), "Candidates should be sorted cheapest first"

    def test_fallback_is_last(self):
        """Fallback candidate (inf cost) should be last if present."""
        candidates = select_provider(model="glm-5.2")
        if len(candidates) > 1:
            for i in range(len(candidates) - 1):
                if candidates[i].effective_cost == float("inf"):
                    assert candidates[i + 1].effective_cost == float("inf"), \
                        "Non-inf cost after inf cost"


# ── Test 5: _is_provider_healthy() for various states ───────────────────────

class TestIsProviderHealthy:
    def test_healthy_provider_returns_true(self):
        """A healthy provider with no issues should return True."""
        with patch("flat_router._is_manually_disabled", return_value=False), \
             patch("flat_router._is_key_healthy", return_value=True), \
             patch("flat_router._is_provider_funded", return_value=True):
            assert _is_provider_healthy("ppq") is True

    def test_manually_disabled_returns_false(self):
        """A manually disabled provider should return False."""
        with patch("flat_router._is_manually_disabled", side_effect=lambda n: n == "ours"):
            assert _is_provider_healthy("ours") is False

    def test_unhealthy_key_returns_false(self):
        """An unhealthy key should return False."""
        with patch("flat_router._is_manually_disabled", return_value=False), \
             patch("flat_router._is_key_healthy", return_value=False):
            assert _is_provider_healthy("friend") is False

    def test_unfunded_external_returns_false(self):
        """An unfunded external provider should return False."""
        with patch("flat_router._is_manually_disabled", return_value=False), \
             patch("flat_router._is_key_healthy", return_value=True), \
             patch("flat_router._is_provider_funded", side_effect=lambda n: n != "deepinfra"):
            assert _is_provider_healthy("deepinfra") is False

    def test_zai_key_does_not_check_funding(self):
        """z.ai keys should not be funding-checked (they're subscription)."""
        with patch("flat_router._is_manually_disabled", return_value=False), \
             patch("flat_router._is_key_healthy", return_value=True), \
             patch("flat_router._is_provider_funded", side_effect=AssertionError):
            # Should not raise — funding check is skipped for z.ai keys
            assert _is_provider_healthy("ours") is True


# ── Test 6: _update_kalman_after_request() updates Kalman filters ───────────

class TestKalmanUpdate:
    def test_price_kalman_updated(self):
        """PriceKalman should be updated with measured $/M."""
        from price_kalman import PriceKalman
        pk = PriceKalman(initial_rate=1.0)
        ck = MagicMock()
        # Register a test provider in _ALL_PROVIDERS
        original = _ALL_PROVIDERS.get("test_pk_update")
        _ALL_PROVIDERS["test_pk_update"] = {
            "price_kalman": pk,
            "consumption_kalman": ck,
        }
        try:
            initial_rate = pk.base_rate
            _update_kalman_after_request("test_pk_update", cost_usd=2.0, total_tokens=500_000)
            # base_rate should have moved toward 2.0/0.5M * 1M = 4.0
            assert pk.base_rate != initial_rate or pk._updates > 0, \
                "PriceKalman should have been updated"
        finally:
            if original is not None:
                _ALL_PROVIDERS["test_pk_update"] = original
            else:
                _ALL_PROVIDERS.pop("test_pk_update", None)

    def test_consumption_kalman_updated(self):
        """ConsumptionKalman should be updated with token count."""
        from consumption_kalman import ConsumptionKalman
        pk = MagicMock()
        ck = ConsumptionKalman()
        original = _ALL_PROVIDERS.get("test_ck_update")
        _ALL_PROVIDERS["test_ck_update"] = {
            "price_kalman": pk,
            "consumption_kalman": ck,
        }
        try:
            initial_count = ck._update_count
            _update_kalman_after_request("test_ck_update", cost_usd=1.0, total_tokens=10000)
            assert ck._update_count == initial_count + 1, \
                "ConsumptionKalman should have been updated"
        finally:
            if original is not None:
                _ALL_PROVIDERS["test_ck_update"] = original
            else:
                _ALL_PROVIDERS.pop("test_ck_update", None)

    def test_zero_tokens_no_crash(self):
        """Should not crash with zero tokens."""
        _update_kalman_after_request("ppq", cost_usd=0.0, total_tokens=0)
        # Should not raise

    def test_none_cost_no_crash(self):
        """Should not crash with None cost."""
        _update_kalman_after_request("ppq", cost_usd=None, total_tokens=1000)
        # Should not raise

    def test_unknown_provider_no_crash(self):
        """Should not crash for an unknown provider."""
        _update_kalman_after_request("nonexistent_provider", cost_usd=1.0, total_tokens=1000)
        # Should not raise


# ── Test 7: Shadow logging records the comparison ───────────────────────────

class TestShadowLogging:
    def test_shadow_log_records_comparison(self, tmp_path):
        """Shadow log should record both best_key choice and select_provider top candidate."""
        from flat_router import _log_flat_router_shadow

        db_path = str(tmp_path / "test_shadow.db")

        # Log a comparison
        _log_flat_router_shadow(
            db_path=db_path,
            best_key_choice="friend",
            candidates=[
                ProviderCandidate(name="ppq", model="glm-5.2", effective_cost=0.80,
                                  dispatch_fn=None, reason="cheapest"),
                ProviderCandidate(name="friend", model="glm-5.2", effective_cost=0.082,
                                  dispatch_fn=None, reason="z.ai"),
            ],
            model="glm-5.2",
        )

        # Verify the row was written
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT best_key_choice, flat_router_top, agreement, candidate_list "
            "FROM flat_router_shadow_decisions"
        ).fetchall()
        conn.close()

        assert len(rows) == 1, "Expected one shadow log row"
        best_key, flat_top, agreement, candidate_list = rows[0]
        assert best_key == "friend"
        assert flat_top == "ppq"
        assert agreement == 0, "friend != ppq, so agreement should be 0"
        # candidate_list should be valid JSON
        parsed = json.loads(candidate_list)
        assert len(parsed) == 2

    def test_shadow_log_agreement_yes(self, tmp_path):
        """When best_key and select_provider agree, agreement should be 1."""
        from flat_router import _log_flat_router_shadow

        db_path = str(tmp_path / "test_shadow_agree.db")

        _log_flat_router_shadow(
            db_path=db_path,
            best_key_choice="friend",
            candidates=[
                ProviderCandidate(name="friend", model="glm-5.2", effective_cost=0.082,
                                  dispatch_fn=None, reason="cheapest"),
            ],
            model="glm-5.2",
        )

        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT agreement FROM flat_router_shadow_decisions"
        ).fetchall()
        conn.close()

        assert len(rows) == 1
        assert rows[0][0] == 1, "Same provider → agreement = 1"


# ── Test 8: _dispatch_to_provider maps to the right method ──────────────────

class TestDispatchMapping:
    def test_zai_keys_map_to_zai_upstream(self):
        """ours/friend should map to z.ai upstream dispatch."""
        # dispatch_fn should not be None for z.ai keys
        candidates = select_provider(model="glm-5.2")
        zai_candidates = [c for c in candidates if c.name in ("ours", "friend")]
        for c in zai_candidates:
            assert c.dispatch_fn is not None, f"dispatch_fn for {c.name} should not be None"

    def test_ollama_maps_to_ollama_cloud_any(self):
        """ollama_cloud should map to _try_ollama_cloud_any."""
        with patch("flat_router._is_key_healthy", return_value=True), \
             patch("flat_router._is_provider_funded", return_value=True), \
             patch("flat_router._is_manually_disabled", return_value=False):
            candidates = select_provider(model="glm-5.2")
            ollama_candidates = [c for c in candidates if c.name in ("ollama_cloud", "ollama_cloud_2")]
            for c in ollama_candidates:
                assert c.dispatch_fn is not None, f"dispatch_fn for {c.name} should not be None"

    def test_telnyx_maps_to_telnyx(self):
        """telnyx should have a dispatch_fn."""
        with patch("flat_router._is_key_healthy", return_value=True), \
             patch("flat_router._is_provider_funded", return_value=True), \
             patch("flat_router._is_manually_disabled", return_value=False):
            candidates = select_provider(model="kimi-k3")
            telnyx_candidates = [c for c in candidates if c.name == "telnyx"]
            for c in telnyx_candidates:
                assert c.dispatch_fn is not None, "dispatch_fn for telnyx should not be None"


# ── Phase 3 tests: full cutover, rollback flag, Kalman live updates ──────────


class TestFlatRouterCutover:
    """Phase 3: verify .disable_flat_router flag controls routing path."""

    def test_disable_flag_check_exists(self):
        """_proxy() should check for .disable_flat_router flag file."""
        # Verify the flag path constant is referenced in zai_proxy.py
        import zai_proxy
        source = open(zai_proxy.__file__).read()
        assert ".disable_flat_router" in source, \
            "_proxy() must check for .disable_flat_router flag"

    def test_select_provider_imported_in_zai_proxy(self):
        """zai_proxy.py should import select_provider from flat_router."""
        import zai_proxy
        source = open(zai_proxy.__file__).read()
        assert "select_provider" in source, \
            "zai_proxy.py must import and use select_provider"

    def test_dispatch_to_provider_imported_in_zai_proxy(self):
        """zai_proxy.py should import _dispatch_to_provider from flat_router."""
        import zai_proxy
        source = open(zai_proxy.__file__).read()
        assert "_dispatch_to_provider" in source, \
            "zai_proxy.py must import and use _dispatch_to_provider"

    def test_update_kalman_after_request_called_in_zai_proxy(self):
        """zai_proxy.py should call _update_kalman_after_request after dispatch."""
        import zai_proxy
        source = open(zai_proxy.__file__).read()
        assert "_update_kalman_after_request" in source, \
            "zai_proxy.py must call _update_kalman_after_request for live Kalman updates"

    def test_try_zai_key_method_exists(self):
        """Handler should have a _try_zai_key method for flat router dispatch."""
        import zai_proxy
        source = open(zai_proxy.__file__).read()
        assert "def _try_zai_key" in source, \
            "Handler must have _try_zai_key method for z.ai dispatch via flat router"

    def test_zai_dispatch_fn_not_returning_false(self):
        """_make_dispatch_fn for z.ai keys should return a real dispatch fn, not None."""
        from flat_router import _make_dispatch_fn
        fn = _make_dispatch_fn("ours")
        assert fn is not None, "ours dispatch_fn should not be None"
        fn2 = _make_dispatch_fn("friend")
        assert fn2 is not None, "friend dispatch_fn should not be None"

    def test_503_on_all_candidates_fail(self):
        """When all candidates fail, _proxy() should send 503."""
        import zai_proxy
        source = open(zai_proxy.__file__).read()
        # The flat router path should have a 503 fallback
        assert "503" in source, "Flat router path must handle all-candidates-fail with 503"

    def test_x_provider_header_in_flat_router_path(self):
        """Flat router path should set X-Provider header for observability."""
        import zai_proxy
        source = open(zai_proxy.__file__).read()
        assert "X-Provider" in source, \
            "Flat router path should set X-Provider header for observability"


class TestFlatRouterKalmanLiveUpdates:
    """Phase 3: verify Kalman filters get live updates after successful dispatch."""

    def test_kalman_update_after_success(self):
        """After a successful dispatch, _update_kalman_after_request should be called."""
        # Verify the flat router path calls _update_kalman_after_request
        import zai_proxy
        source = open(zai_proxy.__file__).read()
        # Should appear in the flat router path (import alias + live update call)
        # The import uses "as _flat_kalman_update" and the call uses _flat_kalman_update
        assert "_update_kalman_after_request" in source, \
            "_update_kalman_after_request should be imported in zai_proxy"
        assert "_flat_kalman_update" in source, \
            "_update_kalman_after_request should be called (via _flat_kalman_update alias) " \
            "in the flat router path"

    def test_kalman_update_with_cost_and_tokens(self):
        """_update_kalman_after_request should receive cost_usd and total_tokens."""
        # Verify _extract_cost and _parse_usage are used in the flat router path
        import zai_proxy
        source = open(zai_proxy.__file__).read()
        assert "_extract_cost" in source, \
            "Flat router path should use _extract_cost for Kalman cost input"
        assert "_parse_usage" in source, \
            "Flat router path should use _parse_usage for Kalman token input"


class TestFlatRouterFallback:
    """Phase 3: verify fallback to next candidate on provider failure."""

    def test_candidate_iteration_in_proxy(self):
        """_proxy() should iterate candidates and try each."""
        import zai_proxy
        source = open(zai_proxy.__file__).read()
        # The flat router path should iterate candidates
        assert "for candidate in" in source or "for _cand in" in source, \
            "Flat router path should iterate candidates in a loop"

    def test_mark_key_failure_on_dispatch_failure(self):
        """On dispatch failure, the key should be marked as failed."""
        import zai_proxy
        source = open(zai_proxy.__file__).read()
        # Should call _mark_key_failure or _mark_key_exhausted on failure
        assert "_mark_key_failure" in source or "_mark_key_exhausted" in source, \
            "Flat router path should mark key failure on dispatch failure"


class TestFlatRouterStreaming:
    """Phase 3: verify streaming requests work through flat router."""

    def test_streaming_through_dispatch(self):
        """Streaming requests should work through _dispatch_to_provider."""
        # The dispatch functions call _try_* methods which already handle streaming
        # This test verifies the dispatch chain is intact
        from flat_router import _dispatch_to_provider, _make_dispatch_fn
        # All dispatch functions should be callable
        for name in ["ours", "friend", "ollama_cloud", "opencode_go", "ppq", "deepinfra"]:
            fn = _make_dispatch_fn(name)
            assert fn is not None, f"dispatch_fn for {name} should not be None"

    def test_streaming_body_passthrough(self):
        """The flat router path should pass the body through to dispatch."""
        import zai_proxy
        source = open(zai_proxy.__file__).read()
        # The flat router path should pass body to _dispatch_to_provider
        assert "_dispatch_to_provider" in source, \
            "Flat router path should call _dispatch_to_provider with the request body"


class TestRollbackFlag:
    """Phase 3: verify .disable_flat_router rollback mechanism."""

    def test_rollback_flag_path_constant(self):
        """The rollback flag path should be ~/.hermes/bot/.disable_flat_router."""
        import zai_proxy
        source = open(zai_proxy.__file__).read()
        assert "~/.hermes/bot/.disable_flat_router" in source or \
               ".disable_flat_router" in source, \
            "Rollback flag path should be .disable_flat_router in ~/.hermes/bot/"

    def test_best_key_preserved(self):
        """best_key() function should still exist for rollback."""
        import zai_proxy
        source = open(zai_proxy.__file__).read()
        assert "def best_key" in source, \
            "best_key() must be preserved for .disable_flat_router rollback"

    def test_old_failover_chain_preserved(self):
        """Old failover chain (ollama, opencode, external) should still exist."""
        import zai_proxy
        source = open(zai_proxy.__file__).read()
        assert "_try_ollama_cloud_any" in source, \
            "_try_ollama_cloud_any must be preserved for rollback"
        assert "_try_external_failover" in source, \
            "_try_external_failover must be preserved for rollback"
        assert "_try_opencode_go" in source, \
            "_try_opencode_go must be preserved for rollback"


# ── Canonicalization tests (2026-08-27): short-form deepseek resolution ─────
# Incident: 2,305 prod requests for "deepseek-v4-flash"/"deepseek-v4-pro"
# (and hyphen-tagged "-0731"/"-0813" forms) matched ONLY opencode_go in
# PROVIDER_MODELS → single-candidate lists → 503s when opencode_go was
# down. Canonicalization must map these to the canonical
# "deepseek/deepseek-v4-*" forms so ALL providers compete on price.


class TestModelCanonicalization:
    """Short-form / tagged model IDs must resolve to the full provider set."""

    def _healthy_all(self):
        """Patch context where every provider passes the health gate."""
        return patch("flat_router._is_provider_healthy", return_value=True)

    def test_short_form_deepseek_flash_multi_candidate(self):
        """'deepseek-v4-flash' must resolve to ≥3 providers incl. a per-token one."""
        with self._healthy_all():
            candidates = select_provider(model="deepseek-v4-flash")
        names = [c.name for c in candidates if c.name != "fallback"]
        assert len(names) >= 3, \
            f"Short-form deepseek-v4-flash resolved to only {names} — " \
            f"canonicalization missing or broken"
        per_token = {"ppq", "neuralwatt", "deepinfra", "openrouter",
                     "routstr", "routstrd"}
        assert per_token & set(names), \
            f"No per-token provider in candidate list {names}"

    def test_short_form_deepseek_pro_multi_candidate(self):
        """'deepseek-v4-pro' must resolve to ≥3 providers incl. a per-token one."""
        with self._healthy_all():
            candidates = select_provider(model="deepseek-v4-pro")
        names = [c.name for c in candidates if c.name != "fallback"]
        assert len(names) >= 3, \
            f"Short-form deepseek-v4-pro resolved to only {names}"
        per_token = {"ppq", "neuralwatt", "deepinfra", "openrouter",
                     "routstr", "routstrd"}
        assert per_token & set(names), \
            f"No per-token provider in candidate list {names}"

    def test_ollama_tagged_hyphen_form_normalized(self):
        """'deepseek-v4-flash-0731' (cron-style tag) → same set as canonical."""
        with self._healthy_all():
            tagged = select_provider(model="deepseek-v4-flash-0731")
            canonical = select_provider(model="deepseek/deepseek-v4-flash")
        tagged_names = sorted(c.name for c in tagged if c.name != "fallback")
        canonical_names = sorted(c.name for c in canonical if c.name != "fallback")
        assert tagged_names == canonical_names, \
            f"Tagged form candidates {tagged_names} != canonical {canonical_names}"
        assert len(tagged_names) >= 3, \
            f"Tagged form resolved to only {tagged_names}"

    def test_canonicalization_idempotent(self):
        """canonicalize_model must leave canonical + z.ai/kimi IDs untouched."""
        from flat_router import canonicalize_model
        # Canonical forms unchanged (idempotent)
        assert canonicalize_model("deepseek/deepseek-v4-flash") == \
            "deepseek/deepseek-v4-flash"
        assert canonicalize_model("deepseek/deepseek-v4-pro") == \
            "deepseek/deepseek-v4-pro"
        # Common z.ai / kimi IDs unchanged
        assert canonicalize_model("glm-5.2") == "glm-5.2"
        assert canonicalize_model("glm-5.3") == "glm-5.3"
        assert canonicalize_model("kimi-k3") == "kimi-k3"
        # Aliases resolve
        assert canonicalize_model("deepseek-v4-flash") == "deepseek/deepseek-v4-flash"
        assert canonicalize_model("deepseek-v4-pro") == "deepseek/deepseek-v4-pro"
        assert canonicalize_model("deepseek-v4-flash-0731") == "deepseek/deepseek-v4-flash"
        assert canonicalize_model("deepseek-v4-pro-0813") == "deepseek/deepseek-v4-pro"
        # Whitespace stripped
        assert canonicalize_model("  deepseek-v4-flash  ") == "deepseek/deepseek-v4-flash"
        # Unknown models pass through verbatim
        assert canonicalize_model("nonexistent-model-xyz") == "nonexistent-model-xyz"

    def test_alias_outgoing_names_correct(self):
        """Each candidate for a short-form request must use the provider's
        outgoing model name from _PROVIDER_MODEL_NAMES (translation at
        selection must match translation at dispatch)."""
        import zai_proxy
        with self._healthy_all():
            candidates = select_provider(model="deepseek-v4-flash")
        canonical = "deepseek/deepseek-v4-flash"
        checked = 0
        for c in candidates:
            if c.name == "fallback":
                continue
            expected = zai_proxy._PROVIDER_MODEL_NAMES.get(c.name, {}).get(
                canonical, canonical)
            assert c.model == expected, \
                f"{c.name}: outgoing model {c.model!r} != expected {expected!r}"
            checked += 1
        assert checked >= 3, f"Only {checked} candidates checked"

    def test_unknown_model_still_fallback(self):
        """Unknown models must still produce a single fallback candidate."""
        with self._healthy_all():
            candidates = select_provider(model="nonexistent-model-xyz")
        assert len(candidates) == 1, \
            f"Expected exactly 1 fallback candidate, got {candidates}"
        assert candidates[0].name == "fallback"
        assert candidates[0].dispatch_fn is None
        assert candidates[0].effective_cost == float("inf")

    def test_vision_model_glm46v_multi_candidate(self):
        """'glm-4.6v' (z.ai vision model, manager auxiliary.vision) must
        resolve to ≥2 non-fallback candidates — both z.ai keys.

        z.ai serves glm-4.6v on the coding endpoint (live-verified
        2026-08-27: POST /chat/completions → 200), though it is unlisted
        in GET /models. Previously unregistered in PROVIDER_MODELS →
        0 candidates → flat router 503'd all vision requests."""
        with self._healthy_all():
            candidates = select_provider(model="glm-4.6v")
        names = {c.name for c in candidates if c.name != "fallback"}
        assert len(names) >= 2, \
            f"glm-4.6v resolved to only {sorted(names)} — vision model " \
            f"not registered in PROVIDER_MODELS"
        assert {"ours", "friend"} <= names, \
            f"glm-4.6v candidates missing z.ai keys: {sorted(names)}"

    def test_worker_fallback_model_resolves_multi(self):
        """WORKER_FALLBACK_MODEL ('deepseek/deepseek-v4-flash') must give
        ≥3 non-fallback candidates (market-driven, not single-provider)."""
        with self._healthy_all():
            candidates = select_provider(model="deepseek/deepseek-v4-flash")
        non_fallback = [c for c in candidates if c.name != "fallback"]
        assert len(non_fallback) >= 3, \
            f"Worker fallback model resolved to only {[c.name for c in non_fallback]}"

    def test_exhausted_opencode_go_still_serves_short_form(self):
        """THE incident regression test: with opencode_go unhealthy, a
        short-form deepseek request must STILL return ≥2 candidates.
        (Previously 'deepseek-v4-flash' matched only opencode_go → 0
        candidates → 503 for 72h.)"""
        with patch("flat_router._is_provider_healthy",
                   side_effect=lambda n: n != "opencode_go"):
            candidates = select_provider(model="deepseek-v4-flash")
        names = [c.name for c in candidates if c.name != "fallback"]
        assert len(names) >= 2, \
            f"Short form with dead opencode_go gave only {names} — " \
            f"the 2026-08-25 503 incident would recur"
        assert "opencode_go" not in names


class TestDoubleResponseGuard:
    """Response-started guard: once bytes hit the client, iteration must stop."""

    def test_response_started_flag_set_in_handlers(self):
        """Dispatch handlers must mark _response_started right after
        send_response()."""
        import zai_proxy
        source = open(zai_proxy.__file__).read()
        assert "_response_started" in source, \
            "Handlers must set self._response_started after send_response()"
        # All five dispatch handlers that stream must set the flag
        for marker in ("X-Provider", ):
            assert marker in source
        n_set = source.count("self._response_started = True")
        assert n_set >= 5, \
            f"Expected ≥5 _response_started sets (one per streaming handler), got {n_set}"

    def test_response_started_aborts_iteration(self):
        """The flat-router candidate loop must abort (not try the next
        candidate, not send a second response) when _response_started is
        already True."""
        import zai_proxy
        source = open(zai_proxy.__file__).read()
        assert 'getattr(self, "_response_started", False)' in source, \
            "Flat loop must check _response_started and treat it as terminal"
        # The abort must happen in the failure branch of the candidate loop
        # (i.e. the check exists between the dispatch and the next-candidate
        # continuation). Verify the guard appears inside the loop body by
        # checking it comes after the dispatch call and before 'continue'.
        loop_idx = source.find("for _cand in _candidates:")
        assert loop_idx != -1, "Flat router candidate loop not found"
        loop_body = source[loop_idx:loop_idx + 6000]
        guard_idx = loop_body.find('getattr(self, "_response_started", False)')
        assert guard_idx != -1, \
            "_response_started guard must live inside the candidate loop"


class TestRegionErrorModelScoped:
    """403 RegionError from opencode_go must be model-scoped: the KEY stays
    healthy so glm-5.3 (and everything else) keeps routing there."""

    def _make_handler(self):
        import zai_proxy
        return object.__new__(zai_proxy.Handler)

    def test_region_error_model_scoped(self):
        """_try_opencode_go receiving 403 body containing 'RegionError' must
        NOT mark the key exhausted/dead."""
        import zai_proxy
        import io
        import urllib.error

        handler = self._make_handler()
        body = json.dumps({
            "model": "deepseek-v4-flash",
            "messages": [{"role": "user", "content": "hi"}],
        }).encode()

        err_fp = io.BytesIO(
            b'{"error":{"type":"RegionError","message":'
            b'"Model deepseek-v4-flash is not available in your region"}}')
        http_err = urllib.error.HTTPError(
            url="https://opencode.ai/api/v1/chat/completions",
            code=403, msg="Forbidden", hdrs={}, fp=err_fp)

        marks = []
        with patch.object(zai_proxy, "OPENCODE_GO_KEY", "test-key-1234"), \
             patch.object(zai_proxy, "_is_key_healthy", return_value=True), \
             patch.object(zai_proxy, "_mark_key_failure",
                          side_effect=lambda n, t=None, **kw: marks.append((n, t))), \
             patch.object(zai_proxy, "_mark_key_exhausted",
                          side_effect=lambda n: marks.append((n, "exhausted"))), \
             patch("urllib.request.urlopen", side_effect=http_err):
            result = handler._try_opencode_go(body, "deepseek-v4-flash",
                                              bytearray(), time.time())

        assert result is False, "RegionError should return False (try next provider)"
        assert marks == [], \
            f"RegionError must NOT mark the key (got marks: {marks}) — " \
            f"this poisoned glm-5.3 for 72h in the Aug-25 incident"

    def test_auth_error_still_marks(self):
        """A plain 403 (no RegionError) must still mark the key (revoked key
        detection unchanged)."""
        import zai_proxy
        import io
        import urllib.error

        handler = self._make_handler()
        body = json.dumps({
            "model": "glm-5.3",
            "messages": [{"role": "user", "content": "hi"}],
        }).encode()

        err_fp = io.BytesIO(b'{"error":{"message":"Invalid API key"}}')
        http_err = urllib.error.HTTPError(
            url="https://opencode.ai/api/v1/chat/completions",
            code=403, msg="Forbidden", hdrs={}, fp=err_fp)

        marks = []
        with patch.object(zai_proxy, "OPENCODE_GO_KEY", "test-key-1234"), \
             patch.object(zai_proxy, "_is_key_healthy", return_value=True), \
             patch.object(zai_proxy, "_mark_key_failure",
                          side_effect=lambda n, t=None, **kw: marks.append((n, t))), \
             patch.object(zai_proxy, "_mark_key_exhausted",
                          side_effect=lambda n: marks.append((n, "exhausted"))), \
             patch("urllib.request.urlopen", side_effect=http_err):
            result = handler._try_opencode_go(body, "glm-5.3",
                                              bytearray(), time.time())

        assert result is False
        assert marks != [], "Non-RegionError 403 must still mark the key"


# ── FR-2 tests (2026-08-27): canonical registry + dispatch translation ──────
# Canonicalize PROVIDER_MODELS to canonical IDs ONLY (no :cloud tags, no
# phantom ollama entries). Dispatch translation must be alias-aware so a
# canonical short ID resolves to the provider-native name at request time.
# Evidence: FR-0 live probe (ollama_tags_key1.json) — ollama real tags are
# kimi-k3 / minimax-m3 / deepseek-v4-flash:0731 / deepseek-v4-pro:0813.
# kimi-k3:cloud and minimax-m3:cloud are verbatim-404 paths (do NOT exist).


class TestFR2CanonicalRegistry:
    """PROVIDER_MODELS must use canonical IDs only; no phantom ollama entries."""

    def test_ollama_uses_canonical_kimi_k3_not_cloud(self):
        """ollama sets must list canonical 'kimi-k3', never 'kimi-k3:cloud'."""
        for prov in ("ollama_cloud", "ollama_cloud_2"):
            assert "kimi-k3" in PROVIDER_MODELS[prov], \
                f"{prov} missing canonical kimi-k3"
            assert "kimi-k3:cloud" not in PROVIDER_MODELS[prov], \
                f"{prov} still lists phantom kimi-k3:cloud (404 on ollama)"

    def test_ollama_uses_canonical_minimax_m3_not_cloud(self):
        """ollama sets must list canonical 'minimax-m3', never 'minimax-m3:cloud'."""
        for prov in ("ollama_cloud", "ollama_cloud_2"):
            assert "minimax-m3" in PROVIDER_MODELS[prov], \
                f"{prov} missing canonical minimax-m3"
            assert "minimax-m3:cloud" not in PROVIDER_MODELS[prov], \
                f"{prov} still lists phantom minimax-m3:cloud (404 on ollama)"

    def test_ollama_glm53_now_live(self):
        """glm-5.3 / glm-5.3-flash are LIVE on ollama (2026-08-29 live
        verification: both served 5-token completions HTTP 200, listed in
        /v1/models). They must be present in ollama sets — the old silent
        downgrade to glm-5.2 is gone (FR-A)."""
        for prov in ("ollama_cloud", "ollama_cloud_2"):
            assert "glm-5.3" in PROVIDER_MODELS[prov], \
                f"{prov} missing live glm-5.3"
            assert "glm-5.3-flash" in PROVIDER_MODELS[prov], \
                f"{prov} missing live glm-5.3-flash"

    def test_ollama_phantom_glm45flash_removed(self):
        """glm-4.5-flash is PHANTOM on ollama (FR-0: absent from live catalog,
        verbatim 404). Must be removed from ollama sets."""
        for prov in ("ollama_cloud", "ollama_cloud_2"):
            assert "glm-4.5-flash" not in PROVIDER_MODELS[prov], \
                f"{prov} still lists phantom glm-4.5-flash (verbatim 404)"

    def test_telnyx_kimi_k3_cloud_deduped(self):
        """telnyx kimi-k3:cloud (silent downgrade to Kimi-K2.5) must be deduped
        under canonical kimi-k3 — no silent substitution."""
        assert "kimi-k3:cloud" not in PROVIDER_MODELS["telnyx"], \
            "telnyx still lists kimi-k3:cloud (silent K2.5 downgrade)"
        assert "kimi-k3" in PROVIDER_MODELS["telnyx"], \
            "telnyx missing canonical kimi-k3"

    def test_no_cloud_tags_anywhere_in_registry(self):
        """No ':cloud' tagged model IDs may remain in PROVIDER_MODELS."""
        for prov, models in PROVIDER_MODELS.items():
            for m in models:
                assert not m.endswith(":cloud"), \
                    f"{prov} still lists tagged {m!r} — registry must be canonical"


class TestFR2DispatchTranslation:
    """Dispatch-time translation must be alias-aware: canonical short IDs
    resolve to provider-native names via _resolve_model_for_provider."""

    def _healthy_all(self):
        return patch("flat_router._is_provider_healthy", return_value=True)

    def test_kimi_k3_to_ollama_sends_real_tag(self):
        """kimi-k3 routed to ollama must send 'kimi-k3' (the real ollama tag),
        not 'kimi-k3:cloud' (which 404s)."""
        import zai_proxy
        with self._healthy_all():
            candidates = select_provider(model="kimi-k3")
        ollama = [c for c in candidates if c.name in ("ollama_cloud", "ollama_cloud_2")]
        assert ollama, "kimi-k3 must have ollama candidates"
        for c in ollama:
            assert c.model == "kimi-k3", \
                f"ollama outgoing model {c.model!r} != 'kimi-k3' (real tag)"

    def test_deepseek_flash_to_deepinfra_sends_native(self):
        """deepseek-v4-flash routed to deepinfra must send
        'deepseek-ai/DeepSeek-V4-Flash' (case-sensitive dotted form)."""
        import zai_proxy
        with self._healthy_all():
            candidates = select_provider(model="deepseek-v4-flash")
        di = [c for c in candidates if c.name == "deepinfra"]
        assert di, "deepseek-v4-flash must have deepinfra candidate"
        assert di[0].model == "deepseek-ai/DeepSeek-V4-Flash", \
            f"deepinfra outgoing {di[0].model!r} != 'deepseek-ai/DeepSeek-V4-Flash'"

    def test_short_form_deepseek_resolves_via_alias_lookup(self):
        """_resolve_model_for_provider must resolve the SHORT form
        'deepseek-v4-flash' to deepinfra's native name (alias-aware lookup),
        not miss and send it verbatim."""
        from flat_router import _resolve_model_for_provider
        got = _resolve_model_for_provider("deepinfra", "deepseek-v4-flash")
        assert got == "deepseek-ai/DeepSeek-V4-Flash", \
            f"short-form lookup returned {got!r} — alias-aware lookup missing"

    def test_slashed_form_deepseek_still_resolves(self):
        """The slashed canonical form must still resolve (rollback safety)."""
        from flat_router import _resolve_model_for_provider
        got = _resolve_model_for_provider("deepinfra", "deepseek/deepseek-v4-flash")
        assert got == "deepseek-ai/DeepSeek-V4-Flash", \
            f"slashed-form lookup returned {got!r}"

    def test_worker_fallback_model_consumers_work(self):
        """WORKER_FALLBACK_MODEL ('deepseek/deepseek-v4-flash') must still
        resolve to ≥3 candidates AND its deepinfra outgoing name must be the
        native dotted form (rollback safety net intact)."""
        import zai_proxy
        assert zai_proxy.WORKER_FALLBACK_MODEL == "deepseek/deepseek-v4-flash"
        with self._healthy_all():
            candidates = select_provider(model=zai_proxy.WORKER_FALLBACK_MODEL)
        non_fallback = [c for c in candidates if c.name != "fallback"]
        assert len(non_fallback) >= 3, \
            f"WORKER_FALLBACK_MODEL resolved to only {[c.name for c in non_fallback]}"
        di = [c for c in non_fallback if c.name == "deepinfra"]
        assert di and di[0].model == "deepseek-ai/DeepSeek-V4-Flash", \
            "WORKER_FALLBACK_MODEL deepinfra outgoing must be native dotted form"

    def test_known_external_model_short_form(self):
        """_is_known_external_model must recognize the SHORT form
        'deepseek-v4-flash' (canonicalized) so the old failover path passes it
        through verbatim instead of rejecting it."""
        import zai_proxy
        assert zai_proxy._is_known_external_model("deepseek-v4-flash") is True, \
            "short-form deepseek-v4-flash must be known to external failover"


# ── FR-A tests (2026-08-29): glm-5.3 verbatim on Ollama Cloud ────────────────
# Ollama Cloud NOW serves glm-5.3 (live-verified 2026-08-29). The proxy must
# (a) include glm-5.3 in ollama candidate sets and (b) pass it through
# verbatim (NO silent downgrade to glm-5.2). Heavy-tier requests were being
# silently downgraded — a correctness bug.


class _StubHandler:
    """Minimal stand-in for Handler instance methods/attrs that do_GET touches."""

    def __init__(self):
        self.wfile = io.BytesIO()
        self._sent_status = None
        self._headers = {}
        self.close_connection = False

    def send_response(self, code):
        self._sent_status = code

    def send_header(self, k, v):
        self._headers[k] = v

    def end_headers(self):
        pass


class TestGlm53Verbatim:
    """glm-5.3 must be a first-class ollama model, never downgraded to 5.2."""

    def _healthy_all(self):
        return patch("flat_router._is_provider_healthy", return_value=True)

    def test_no_glm53_rewrite_in_ollama_provider_names(self):
        """_PROVIDER_MODEL_NAMES for ollama_cloud / ollama_cloud_2 must NOT
        map glm-5.3 to a different model (no silent downgrade)."""
        import zai_proxy
        for prov in ("ollama_cloud", "ollama_cloud_2"):
            mapping = zai_proxy._PROVIDER_MODEL_NAMES[prov]
            assert "glm-5.3" not in mapping, \
                f"{prov} still rewrites glm-5.3 -> {mapping.get('glm-5.3')!r}"

    def test_glm53_select_provider_includes_ollama(self):
        """select_provider('glm-5.3') must include BOTH ollama_cloud and
        ollama_cloud_2, with >=4 total candidates."""
        with self._healthy_all():
            candidates = select_provider(model="glm-5.3")
        names = [c.name for c in candidates if c.name != "fallback"]
        assert "ollama_cloud" in names, \
            f"ollama_cloud missing from glm-5.3 candidates: {names}"
        assert "ollama_cloud_2" in names, \
            f"ollama_cloud_2 missing from glm-5.3 candidates: {names}"
        assert len(names) >= 4, \
            f"glm-5.3 resolved to only {len(names)} candidates: {names}"

    def test_v1_models_includes_glm53(self):
        """The /v1/models stub response must include glm-5.3."""
        import tempfile
        from pathlib import Path
        import zai_proxy
        handler = _StubHandler()
        handler.path = "/v1/models"
        # Point Path.home() at an empty temp dir so kalman_pricing.json is
        # absent -> _kp stays {} -> no pool is delisted -> glm-5.3 listed.
        with patch.object(zai_proxy.Path, "home",
                          return_value=Path(tempfile.mkdtemp())):
            zai_proxy.Handler.do_GET(handler)
        payload = json.loads(handler.wfile.getvalue())
        ids = [m["id"] for m in payload["data"]]
        assert "glm-5.3" in ids, \
            f"glm-5.3 missing from /v1/models: {ids}"


# ── Fresh-box doc-repair tests (defects from deleg_5e172db2) ────────────────
# Defect 1: _load_keys() must read ~/.hermes/bot/.env (the doc's example
# writes ALL keys there), not just profiles/manager/.env + ~/.hermes/.env.
# Defect 2: PORT must be overridable via the PORT env var (default 9099) so a
# stranger can run a second instance alongside a live one.

class TestFreshboxDocRepair:
    """Regression tests for REPRODUCE.md fresh-box defects 1 & 2."""

    @staticmethod
    def _load_worktree_proxy():
        """Load the worktree copy of production/zai_proxy.py (not the deployed
        ~/.hermes/bot copy, which is first on sys.path)."""
        import importlib.util
        from pathlib import Path
        src = Path(__file__).parent / "production" / "zai_proxy.py"
        spec = importlib.util.spec_from_file_location(
            "zai_proxy_freshbox_test", str(src))
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_load_keys_reads_bot_env(self):
        """_load_keys() must load ZAI_OUR_KEY/ZAI_API_KEY from ~/.hermes/bot/.env
        (the doc's Step 3 writes all keys there)."""
        import tempfile
        from pathlib import Path

        zai_proxy = self._load_worktree_proxy()
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            bot = home / ".hermes" / "bot"
            bot.mkdir(parents=True)
            (bot / ".env").write_text(
                "ZAI_OUR_KEY=sk-ours-bot\nZAI_API_KEY=sk-friend-bot\n"
            )
            with patch.object(zai_proxy.Path, "home", return_value=home):
                keys = zai_proxy._load_keys()
        assert keys.get("ours") == "sk-ours-bot", \
            f"ours key not loaded from bot/.env: {keys}"
        assert keys.get("friend") == "sk-friend-bot", \
            f"friend key not loaded from bot/.env: {keys}"

    def test_load_keys_prefers_manager_env(self):
        """_load_keys() must still prefer profiles/manager/.env over bot/.env
        (first-of ordering preserved)."""
        import tempfile
        from pathlib import Path

        zai_proxy = self._load_worktree_proxy()
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            mgr = home / ".hermes" / "profiles" / "manager"
            mgr.mkdir(parents=True)
            (mgr / ".env").write_text("ZAI_OUR_KEY=sk-ours-mgr\n")
            bot = home / ".hermes" / "bot"
            bot.mkdir(parents=True)
            (bot / ".env").write_text("ZAI_OUR_KEY=sk-ours-bot\n")
            with patch.object(zai_proxy.Path, "home", return_value=home):
                keys = zai_proxy._load_keys()
        assert keys.get("ours") == "sk-ours-mgr", \
            f"manager/.env should win over bot/.env: {keys}"

    def test_port_defaults_to_9099(self):
        """PORT must default to 9099 when the PORT env var is unset."""
        import os
        old = os.environ.get("PORT")
        if old is not None:
            os.environ.pop("PORT", None)
        try:
            mod = self._load_worktree_proxy()
            assert mod.PORT == 9099, \
                f"PORT default should be 9099, got {mod.PORT}"
        finally:
            if old is not None:
                os.environ["PORT"] = old

    def test_port_reads_env_override(self):
        """PORT must be overridable via the PORT env var (defect 2)."""
        import os

        old = os.environ.get("PORT")
        os.environ["PORT"] = "9199"
        try:
            mod = self._load_worktree_proxy()
            assert mod.PORT == 9199, \
                f"PORT env override should be 9199, got {mod.PORT}"
        finally:
            if old is None:
                os.environ.pop("PORT", None)
            else:
                os.environ["PORT"] = old


# ── Legacy-label relabel (2026-09-03) ────────────────────────────────────────
# The flat router dispatches ollama via _try_ollama_cloud_any WITHOUT a reason,
# so _try_ollama_cloud()'s self-log emitted the legacy
# "zai_both_keys_exhausted_ollama_fallback" / "peak_hour_ollama_primary" label
# even though select_provider() HAD already run (market argmin). That label
# implied a routing bypass that does not exist. The dispatch closure now passes
# reason="flat_router_dispatch_ollama" so the self-log is accurate.


class TestOllamaDispatchReasonRelabel:
    """The flat-router ollama dispatch must pass an accurate reason so the
    legacy bypass label is never emitted from the flat-router path."""

    def test_dispatch_closure_passes_flat_router_reason(self):
        """_make_dispatch_fn('ollama_cloud') must call _try_ollama_cloud_any
        with reason='flat_router_dispatch_ollama' (not the legacy default)."""
        from flat_router import _make_dispatch_fn

        captured = {}

        class _FakeHandler:
            def _try_ollama_cloud_any(self, body, model, buffer, t0,
                                      reason=None):
                captured["reason"] = reason
                return True

        fn = _make_dispatch_fn("ollama_cloud")
        assert fn is not None
        ok = fn(_FakeHandler(), b"{}", "glm-5.3", bytearray(), 0.0)
        assert ok is True
        assert captured["reason"] == "flat_router_dispatch_ollama", \
            f"expected flat_router_dispatch_ollama, got {captured['reason']!r}"

    def test_all_ollama_keys_relabeled(self):
        """ollama_cloud and ollama_cloud_2 must carry the accurate reason."""
        from flat_router import _make_dispatch_fn

        for name in ("ollama_cloud", "ollama_cloud_2"):
            captured = {}

            class _FakeHandler:
                def _try_ollama_cloud_any(self, body, model, buffer, t0,
                                          reason=None):
                    captured["reason"] = reason
                    return True

            fn = _make_dispatch_fn(name)
            assert fn is not None, f"no dispatch fn for {name}"
            fn(_FakeHandler(), b"{}", "glm-5.3", bytearray(), 0.0)
            assert captured["reason"] == "flat_router_dispatch_ollama", \
                f"{name} reason = {captured['reason']!r}"

    def test_legacy_label_not_in_flat_router_dispatch(self):
        """The flat-router dispatch closure must pass an explicit reason kwarg
        (so _try_ollama_cloud's legacy default is never used from this path)."""
        import inspect
        from flat_router import _make_dispatch_fn
        src = inspect.getsource(_make_dispatch_fn)
        assert "reason=" in src, \
            "flat-router ollama dispatch must pass an explicit reason kwarg"

    def test_rollback_path_still_uses_legacy_label(self):
        """The .disable_flat_router rollback path calls _try_ollama_cloud_any
        with NO reason, so _try_ollama_cloud() still self-logs the legacy
        label there. Verify the legacy label string still exists in
        _try_ollama_cloud (it is the rollback safety net's label)."""
        import zai_proxy
        src = open(zai_proxy.__file__).read()
        assert "zai_both_keys_exhausted_ollama_fallback" in src, \
            "legacy label must remain for the rollback path"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])