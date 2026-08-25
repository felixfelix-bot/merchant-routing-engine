"""Tests for src/model_mapping.py — (provider, task_type) → model_name.

Covers:
* Exact (provider, task_type) lookups
* z.ai key alias collapsing (ours/friend/manager/worker → zai)
* Default/fallback resolution (unknown task_type, unknown provider, None)
* Config-driven loading from providers.yaml (and graceful fallbacks)
* get_models_for_provider introspection helper
"""
from __future__ import annotations

import textwrap

import pytest

from src.model_mapping import (
    DEFAULT_MODEL,
    DEFAULT_TASK_TYPE,
    MODEL_MAP,
    TASK_TYPES,
    get_model,
    get_models_for_provider,
    load_model_map,
    normalize_service,
)


# ── normalize_service ────────────────────────────────────────────────────────


class TestNormalizeService:
    """z.ai key names collapse to ``zai``; everything else passes through."""

    @pytest.mark.parametrize("alias", [
        "ours", "friend", "zai", "zai_ours", "zai_friend", "manager", "worker",
    ])
    def test_zai_aliases_collapse_to_zai(self, alias):
        assert normalize_service(alias) == "zai"

    @pytest.mark.parametrize("provider", [
        "deepinfra", "ppq", "openrouter", "ollama_cloud",
    ])
    def test_external_providers_pass_through(self, provider):
        assert normalize_service(provider) == provider

    def test_none_becomes_unknown(self):
        assert normalize_service(None) == "unknown"

    def test_unrecognised_name_passes_through(self):
        assert normalize_service("some_new_provider") == "some_new_provider"

    def test_empty_string_passes_through(self):
        assert normalize_service("") == ""

    def test_idempotent_on_zai(self):
        assert normalize_service("zai") == "zai"

    def test_ours_and_friend_are_same_service(self):
        """Both z.ai keys must resolve to the same service for lookups."""
        assert normalize_service("ours") == normalize_service("friend") == "zai"


# ── Exact lookups (task P4.5a spec examples) ──────────────────────────────────


class TestExactLookups:
    """The mappings called out in the task spec resolve correctly."""

    def test_zai_coding_is_glm52(self):
        assert get_model("zai", "coding") == "glm-5.2"

    def test_zai_simple_is_glm45_flash(self):
        assert get_model("zai", "simple") == "glm-4.5-flash"

    def test_deepinfra_coding_is_deepseek_v4_pro(self):
        assert get_model("deepinfra", "coding") == "deepseek-v4-pro"

    def test_ppq_coding_is_kimi_k3(self):
        assert get_model("ppq", "coding") == "kimi-k3"


class TestAllCanonicalMappings:
    """Every entry in MODEL_MAP resolves via get_model when injected."""

    @pytest.mark.parametrize("provider,task_type,model", [
        (p, t, m) for (p, t), m in MODEL_MAP.items()
    ])
    def test_get_model_matches_table(self, provider, task_type, model):
        # Inject the raw table to bypass config-file loading.
        assert get_model(provider, task_type, model_map=MODEL_MAP) == model


# ── z.ai key aliasing through get_model ───────────────────────────────────────


class TestZaiKeyAliasing:
    """All z.ai key names yield the zai model for the same task type."""

    ZAI_KEYS = ["ours", "friend", "zai", "zai_ours", "zai_friend",
                "manager", "worker"]

    @pytest.mark.parametrize("key", ZAI_KEYS)
    def test_every_zai_key_coding_is_glm52(self, key):
        assert get_model(key, "coding", model_map=MODEL_MAP) == "glm-5.2"

    @pytest.mark.parametrize("key", ZAI_KEYS)
    def test_every_zai_key_simple_is_flash(self, key):
        assert get_model(key, "simple", model_map=MODEL_MAP) == "glm-4.5-flash"

    def test_keys_agree_across_task_types(self):
        for tt in TASK_TYPES:
            models = {
                get_model(k, tt, model_map=MODEL_MAP) for k in self.ZAI_KEYS
            }
            assert len(models) == 1, f"keys disagree for task={tt}: {models}"


# ── Default / fallback resolution ────────────────────────────────────────────


class TestDefaultsAndFallbacks:
    """Unknown task_type and unknown provider fall back gracefully."""

    def test_none_task_type_uses_default(self):
        # None → DEFAULT_TASK_TYPE ("coding")
        assert get_model("zai", None, model_map=MODEL_MAP) == get_model(
            "zai", DEFAULT_TASK_TYPE, model_map=MODEL_MAP
        )

    def test_unknown_task_type_falls_back_to_coding(self):
        assert get_model("zai", "bogus", model_map=MODEL_MAP) == "glm-5.2"

    def test_unknown_provider_returns_global_default(self):
        assert get_model("nonsense", "coding") == DEFAULT_MODEL

    def test_unknown_provider_unknown_task_returns_default(self):
        assert get_model("nonsense", "whatever") == DEFAULT_MODEL

    def test_none_provider_returns_default(self):
        assert get_model(None, "coding") == DEFAULT_MODEL

    def test_empty_provider_returns_default(self):
        assert get_model("", "coding") == DEFAULT_MODEL


class TestPerProviderDefaultFallback:
    """A provider with only *some* task types still resolves others."""

    def test_partial_table_falls_back_to_coding(self):
        table = {("zai", "coding"): "glm-5.2"}
        # 'simple' not in table → falls back to DEFAULT_TASK_TYPE (coding)
        assert get_model("zai", "simple", model_map=table) == "glm-5.2"

    def test_provider_only_in_static_defaults(self):
        # No entries in table, but provider is in _PROVIDER_DEFAULTS.
        assert get_model("zai", "coding", model_map={}) == "glm-4.5-flash"

    def test_unknown_provider_empty_table_global_default(self):
        assert get_model("ghost", "coding", model_map={}) == DEFAULT_MODEL


# ── get_models_for_provider ──────────────────────────────────────────────────


class TestGetModelsForProvider:
    def test_returns_all_task_types_for_zai(self):
        models = get_models_for_provider("ours", model_map=MODEL_MAP)
        assert models == {
            "coding": "glm-5.2",
            "reasoning": "glm-5.3",
            "chat": "glm-4.5-air",
            "simple": "glm-4.5-flash",
        }

    def test_empty_for_unknown_provider(self):
        assert get_models_for_provider("ghost") == {}

    def test_keys_all_known_task_types(self):
        models = get_models_for_provider("ppq", model_map=MODEL_MAP)
        assert set(models) == set(TASK_TYPES)

    def test_none_provider_is_unknown_empty(self):
        assert get_models_for_provider(None) == {}


# ── Config-driven loading ────────────────────────────────────────────────────


class TestLoadModelMap:
    """load_model_map reads YAML; falls back to MODEL_MAP on any problem."""

    def test_loads_from_default_config(self):
        # The shipped config/providers.yaml has a model_map section.
        table = load_model_map()
        assert ("zai", "coding") in table
        assert table[("zai", "coding")] == "glm-5.2"
        assert table[("ppq", "coding")] == "kimi-k3"

    def test_missing_file_returns_defaults(self, tmp_path):
        table = load_model_map(tmp_path / "nonexistent.yaml")
        assert table == MODEL_MAP
        # Must be a *copy*, not the module-level dict.
        assert table is not MODEL_MAP

    def test_empty_model_map_section_returns_defaults(self, tmp_path):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("strategy:\n  fallback_model: x\n", encoding="utf-8")
        assert load_model_map(cfg) == MODEL_MAP

    def test_custom_model_map_overrides_defaults(self, tmp_path):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text(textwrap.dedent("""\
            strategy:
              model_map:
                zai:
                  coding: "custom-glm"
        """), encoding="utf-8")
        table = load_model_map(cfg)
        assert table == {("zai", "coding"): "custom-glm"}

    def test_malformed_yaml_returns_defaults(self, tmp_path):
        cfg = tmp_path / "bad.yaml"
        cfg.write_text("strategy: [this: is : broken\n", encoding="utf-8")
        assert load_model_map(cfg) == MODEL_MAP

    def test_returns_fresh_dict_each_call(self):
        a = load_model_map()
        b = load_model_map()
        assert a == b
        assert a is not b

    def test_skips_null_models(self, tmp_path):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text(textwrap.dedent("""\
            strategy:
              model_map:
                zai:
                  coding: "glm-5.2"
                  reasoning: null
        """), encoding="utf-8")
        table = load_model_map(cfg)
        assert table == {("zai", "coding"): "glm-5.2"}


class TestGetModelReadsConfig:
    """get_model uses the config-loaded table by default (end-to-end)."""

    def test_default_config_lookup(self):
        # Without injecting a table, get_model reads config/providers.yaml.
        assert get_model("ours", "coding") == "glm-5.2"
        assert get_model("ppq", "coding") == "kimi-k3"
        assert get_model("deepinfra", "simple") == "deepseek-v4-flash"

    def test_injected_table_overrides_config(self):
        table = {("zai", "coding"): "override"}
        assert get_model("ours", "coding", model_map=table) == "override"
