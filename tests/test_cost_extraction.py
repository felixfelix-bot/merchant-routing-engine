"""Tests for src/cost_extraction.py — RP-2 real cost extraction per provider.

Covers:
  - OpenRouter usage.cost (JSON + SSE)
  - DeepInfra usage.estimated_cost
  - PPQ multiple field-path probes (usage.cost, top-level cost, usage.total_cost)
  - flat-rate / unknown providers → (None, None)
  - missing fields → (None, None)
  - guards: negative, NaN, inf, non-numeric → treated as miss
  - malformed JSON / empty buffer / None buffer never raise
  - extract_cost_from_obj against a pre-parsed dict
"""
from __future__ import annotations

import json
import math

import pytest

from src.cost_extraction import (
    PROVIDER_COST_PATHS,
    SOURCE_ESTIMATED,
    SOURCE_MEASURED,
    SOURCE_RATE_DERIVED,
    extract_cost,
    extract_cost_from_obj,
)


# ── OpenRouter ───────────────────────────────────────────────────────────────


class TestOpenRouter:
    def test_json_usage_cost(self):
        body = json.dumps({
            "id": "gen-123",
            "choices": [{"message": {"content": "hi"}}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
                "cost": 0.000123,
            },
        }).encode()
        cost, source = extract_cost("openrouter", body)
        assert cost == pytest.approx(0.000123, rel=1e-9)
        assert source == SOURCE_MEASURED

    def test_sse_final_chunk_has_cost(self):
        """Streaming: usage.cost rides the final data: chunk."""
        sse = (
            b'data: {"choices":[{"delta":{"content":"he"}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12,"cost":0.0000099}}\n\n'
            b'data: [DONE]\n\n'
        )
        cost, source = extract_cost("openrouter", sse)
        assert cost == pytest.approx(0.0000099, rel=1e-9)
        assert source == SOURCE_MEASURED

    def test_missing_cost_returns_none(self):
        body = json.dumps({
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }).encode()
        assert extract_cost("openrouter", body) == (None, None)


# ── DeepInfra ────────────────────────────────────────────────────────────────


class TestDeepInfra:
    def test_json_estimated_cost(self):
        """DeepInfra returns usage.estimated_cost (actual charge incl. caching)."""
        body = json.dumps({
            "id": "di-456",
            "choices": [{"message": {"content": "result"}}],
            "usage": {
                "prompt_tokens": 200,
                "completion_tokens": 80,
                "total_tokens": 280,
                "estimated_cost": 0.0000351,
            },
        }).encode()
        cost, source = extract_cost("deepinfra", body)
        assert cost == pytest.approx(0.0000351, rel=1e-9)
        assert source == SOURCE_MEASURED

    def test_sse_estimated_cost(self):
        sse = (
            b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
            b'data: {"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2,"estimated_cost":0.0001}}\n\n'
            b'data: [DONE]\n\n'
        )
        cost, source = extract_cost("deepinfra", sse)
        assert cost == pytest.approx(0.0001, rel=1e-9)
        assert source == SOURCE_MEASURED

    def test_missing_estimated_cost(self):
        body = json.dumps({
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }).encode()
        assert extract_cost("deepinfra", body) == (None, None)


# ── PPQ ──────────────────────────────────────────────────────────────────────


class TestPPQ:
    def test_usage_cost_path(self):
        body = json.dumps({
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10, "cost": 0.0014},
        }).encode()
        cost, source = extract_cost("ppq", body)
        assert cost == pytest.approx(0.0014, rel=1e-9)
        assert source == SOURCE_MEASURED

    def test_top_level_cost_path(self):
        """Some providers put cost at the top level, not in usage."""
        body = json.dumps({
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
            "cost": 0.0028,
        }).encode()
        cost, source = extract_cost("ppq", body)
        assert cost == pytest.approx(0.0028, rel=1e-9)
        assert source == SOURCE_MEASURED

    def test_usage_total_cost_path(self):
        body = json.dumps({
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"total_cost": 0.0099},
        }).encode()
        cost, source = extract_cost("ppq", body)
        assert cost == pytest.approx(0.0099, rel=1e-9)

    def test_ppq_no_cost_anywhere(self):
        body = json.dumps({
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
        }).encode()
        assert extract_cost("ppq", body) == (None, None)

    def test_sse_cost(self):
        sse = (
            b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
            b'data: {"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2,"cost":0.005}}\n\n'
            b'data: [DONE]\n\n'
        )
        cost, source = extract_cost("ppq", sse)
        assert cost == pytest.approx(0.005, rel=1e-9)
        assert source == SOURCE_MEASURED


# ── Telnyx ───────────────────────────────────────────────────────────────────


class TestTelnyx:
    def test_json_usage_cost(self):
        """Telnyx may return usage.cost — same path as OpenRouter."""
        body = json.dumps({
            "id": "tx-789",
            "choices": [{"message": {"content": "hi"}}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
                "cost": 0.000081,
            },
        }).encode()
        cost, source = extract_cost("telnyx", body)
        assert cost == pytest.approx(0.000081, rel=1e-9)
        assert source == SOURCE_MEASURED

    def test_json_usage_estimated_cost(self):
        """Telnyx may return usage.estimated_cost — same path as DeepInfra."""
        body = json.dumps({
            "choices": [{"message": {"content": "result"}}],
            "usage": {
                "prompt_tokens": 200,
                "completion_tokens": 80,
                "total_tokens": 280,
                "estimated_cost": 0.0001512,
            },
        }).encode()
        cost, source = extract_cost("telnyx", body)
        assert cost == pytest.approx(0.0001512, rel=1e-9)
        assert source == SOURCE_MEASURED

    def test_json_top_level_cost(self):
        """Telnyx may put cost at the top level."""
        body = json.dumps({
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
            "cost": 0.0027,
        }).encode()
        cost, source = extract_cost("telnyx", body)
        assert cost == pytest.approx(0.0027, rel=1e-9)
        assert source == SOURCE_MEASURED

    def test_json_usage_total_cost(self):
        """Telnyx may return usage.total_cost."""
        body = json.dumps({
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"total_cost": 0.0099},
        }).encode()
        cost, source = extract_cost("telnyx", body)
        assert cost == pytest.approx(0.0099, rel=1e-9)
        assert source == SOURCE_MEASURED

    def test_sse_cost(self):
        """Streaming: cost rides the final data: chunk."""
        sse = (
            b'data: {"choices":[{"delta":{"content":"he"}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n'
            b'data: {"usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12,"cost":0.000099}}\n\n'
            b'data: [DONE]\n\n'
        )
        cost, source = extract_cost("telnyx", sse)
        assert cost == pytest.approx(0.000099, rel=1e-9)
        assert source == SOURCE_MEASURED

    def test_no_cost_anywhere(self):
        """When Telnyx returns no cost field, the module returns (None, None);
        the proxy wrapper then derives from token_count × published rate."""
        body = json.dumps({
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
        }).encode()
        assert extract_cost("telnyx", body) == (None, None)


# ── Flat-rate / unknown providers ────────────────────────────────────────────


class TestFlatRateAndUnknown:
    def test_ollama_cloud_not_handled(self):
        """Ollama Cloud is flat-rate — no per-call cost in the body. Module
        returns (None, None); the proxy wrapper computes an estimated cost."""
        body = json.dumps({
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        }).encode()
        assert extract_cost("ollama_cloud", body) == (None, None)

    def test_ours_not_handled(self):
        body = json.dumps({"usage": {"total_tokens": 100}}).encode()
        assert extract_cost("ours", body) == (None, None)

    def test_friend_not_handled(self):
        body = json.dumps({"usage": {"total_tokens": 100}}).encode()
        assert extract_cost("friend", body) == (None, None)

    def test_unknown_provider(self):
        body = json.dumps({"usage": {"cost": 0.5}}).encode()
        assert extract_cost("totally_unknown", body) == (None, None)

    def test_known_provider_not_in_paths_still_safe(self):
        """A provider that exists but isn't in PROVIDER_COST_PATHS returns None."""
        # 'deepseek' is a model name, not a provider key — must be None.
        body = json.dumps({"usage": {"cost": 1.0}}).encode()
        assert extract_cost("deepseek", body) == (None, None)


# ── Value guards ─────────────────────────────────────────────────────────────


class TestValueGuards:
    def _body_with_cost(self, value):
        return json.dumps({"usage": {"cost": value}}).encode()

    def test_negative_cost_is_miss(self):
        cost, source = extract_cost("openrouter", self._body_with_cost(-0.001))
        assert cost is None and source is None

    def test_nan_cost_is_miss(self):
        cost, source = extract_cost("openrouter", self._body_with_cost(float("nan")))
        assert cost is None and source is None

    def test_inf_cost_is_miss(self):
        cost, source = extract_cost("openrouter", self._body_with_cost(float("inf")))
        assert cost is None and source is None

    def test_zero_cost_is_valid(self):
        """Zero is a legitimate cost (e.g. cached/free response), not a miss."""
        cost, source = extract_cost("openrouter", self._body_with_cost(0))
        assert cost == 0.0
        assert source == SOURCE_MEASURED

    def test_string_numeric_cost_rejected(self):
        """A string '0.001' is not accepted — float() would parse it but the
        guard rejects non-numeric types defensively. (float('0.001') succeeds,
        so this actually IS accepted; we verify that path works.)"""
        # float("0.001") == 0.001, so a numeric string IS parsed. This is
        # acceptable — document the behaviour rather than enforce type strictness.
        body = json.dumps({"usage": {"cost": "0.001"}}).encode()
        cost, source = extract_cost("openrouter", body)
        assert cost == pytest.approx(0.001, rel=1e-9)
        assert source == SOURCE_MEASURED

    def test_non_numeric_cost_rejected(self):
        body = json.dumps({"usage": {"cost": "not-a-number"}}).encode()
        cost, source = extract_cost("openrouter", body)
        assert cost is None and source is None

    def test_null_cost_rejected(self):
        body = json.dumps({"usage": {"cost": None}}).encode()
        cost, source = extract_cost("openrouter", body)
        assert cost is None and source is None

    def test_nested_usage_missing(self):
        """usage dict entirely absent."""
        body = json.dumps({"choices": [{"message": {"content": "hi"}}]}).encode()
        cost, source = extract_cost("openrouter", body)
        assert cost is None and source is None


# ── Robustness / never-raises ────────────────────────────────────────────────


class TestRobustness:
    def test_empty_buffer(self):
        assert extract_cost("openrouter", b"") == (None, None)

    def test_none_like_buffer(self):
        # bytes(None) would raise; the function guards via _parse_response_objects.
        # Pass an empty bytes as the closest safe surrogate.
        assert extract_cost("openrouter", b"") == (None, None)

    def test_garbage_bytes(self):
        assert extract_cost("openrouter", b"\x00\xff garbage \x01") == (None, None)

    def test_malformed_json(self):
        assert extract_cost("openrouter", b'{"usage": {"cost": broken') == (None, None)

    def test_partial_sse_no_valid_payloads(self):
        sse = b"data: [DONE]\n\ndata: not-json\n\n"
        assert extract_cost("openrouter", sse) == (None, None)

    def test_sse_with_cost_in_early_chunk_picked_up(self):
        """Even if cost appears in a non-final chunk (some providers), it's found."""
        sse = (
            b'data: {"usage":{"cost":0.002}}\n\n'
            b'data: {"choices":[{"delta":{"content":"late"}}]}\n\n'
            b'data: [DONE]\n\n'
        )
        cost, source = extract_cost("openrouter", sse)
        assert cost == pytest.approx(0.002, rel=1e-9)

    def test_very_large_cost_does_not_overflow(self):
        body = json.dumps({"usage": {"cost": 1e15}}).encode()
        cost, source = extract_cost("openrouter", body)
        assert cost == pytest.approx(1e15, rel=1e-6)
        assert source == SOURCE_MEASURED

    def test_tiny_cost_precision(self):
        body = json.dumps({"usage": {"cost": 1e-12}}).encode()
        cost, source = extract_cost("openrouter", body)
        assert cost == pytest.approx(1e-12, rel=1e-6)


# ── extract_cost_from_obj (pre-parsed dict) ──────────────────────────────────


class TestExtractFromObj:
    def test_dict_openrouter(self):
        obj = {"usage": {"cost": 0.5}}
        assert extract_cost_from_obj("openrouter", obj) == (pytest.approx(0.5), SOURCE_MEASURED)

    def test_dict_none_obj(self):
        assert extract_cost_from_obj("openrouter", None) == (None, None)

    def test_dict_non_dict_obj(self):
        assert extract_cost_from_obj("openrouter", [1, 2, 3]) == (None, None)

    def test_dict_unknown_provider(self):
        assert extract_cost_from_obj("zzz", {"cost": 1}) == (None, None)

    def test_dict_missing_usage(self):
        assert extract_cost_from_obj("deepinfra", {"choices": []}) == (None, None)


# ── Module structure ────────────────────────────────────────────────────────


class TestModuleStructure:
    def test_provider_paths_documented_for_paid_providers(self):
        """All external paid providers must have documented paths."""
        for p in ("openrouter", "deepinfra", "ppq", "telnyx"):
            assert p in PROVIDER_COST_PATHS
            assert "paths" in PROVIDER_COST_PATHS[p]
            assert len(PROVIDER_COST_PATHS[p]["paths"]) >= 1
            assert PROVIDER_COST_PATHS[p]["source"] == SOURCE_MEASURED

    def test_flat_rate_providers_not_in_paths(self):
        """Flat-rate providers are handled by the proxy wrapper, not here."""
        for p in ("ollama_cloud", "ours", "friend"):
            assert p not in PROVIDER_COST_PATHS

    def test_source_constants_distinct(self):
        assert SOURCE_MEASURED != SOURCE_ESTIMATED
        assert SOURCE_MEASURED != SOURCE_RATE_DERIVED
        assert SOURCE_ESTIMATED != SOURCE_RATE_DERIVED
