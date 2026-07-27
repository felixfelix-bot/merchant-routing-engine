"""Tests for src/provider_names.py — canonical provider name normalization."""
from __future__ import annotations

import pytest

from src.provider_names import CANONICAL_PROVIDERS, normalize_provider_name


# ── Legacy alias mapping ────────────────────────────────────────────────────


class TestLegacyAliases:
    """Every legacy/alias name maps to the correct canonical name."""

    @pytest.mark.parametrize("alias,canonical", [
        ("zai_ours", "ours"),
        ("zai_friend", "friend"),
        ("manager", "ours"),
        ("worker", "ours"),
        ("unknown", "unknown"),
    ])
    def test_alias_maps_to_canonical(self, alias, canonical):
        assert normalize_provider_name(alias) == canonical

    def test_zai_ours_and_ours_are_same(self):
        """The two names that historically referred to our z.ai key
        must normalize to the same canonical name."""
        assert normalize_provider_name("zai_ours") == normalize_provider_name("ours") == "ours"

    def test_manager_and_worker_are_ours(self):
        """daily_spend tiers 'manager' and 'worker' both refer to our key."""
        assert normalize_provider_name("manager") == "ours"
        assert normalize_provider_name("worker") == "ours"
        assert normalize_provider_name("manager") == normalize_provider_name("worker")


# ── Canonical names pass through ────────────────────────────────────────────


class TestCanonicalPassthrough:
    """Canonical names are returned unchanged (idempotent)."""

    @pytest.mark.parametrize("name", sorted(CANONICAL_PROVIDERS))
    def test_canonical_name_unchanged(self, name):
        assert normalize_provider_name(name) == name

    def test_ours_passes_through(self):
        assert normalize_provider_name("ours") == "ours"

    def test_friend_passes_through(self):
        assert normalize_provider_name("friend") == "friend"

    def test_ollama_cloud_passes_through(self):
        assert normalize_provider_name("ollama_cloud") == "ollama_cloud"

    def test_ppq_passes_through(self):
        assert normalize_provider_name("ppq") == "ppq"

    def test_openrouter_passes_through(self):
        assert normalize_provider_name("openrouter") == "openrouter"

    def test_deepinfra_passes_through(self):
        assert normalize_provider_name("deepinfra") == "deepinfra"


# ── Unrecognised names pass through ─────────────────────────────────────────


class TestUnrecognisedPassthrough:
    """Names not in the mapping table pass through unchanged."""

    def test_new_provider_passes_through(self):
        assert normalize_provider_name("some_new_provider") == "some_new_provider"

    def test_empty_string_passes_through(self):
        assert normalize_provider_name("") == ""

    def test_arbitrary_string_passes_through(self):
        assert normalize_provider_name("anything-at-all") == "anything-at-all"


# ── None handling ───────────────────────────────────────────────────────────


class TestNoneHandling:
    def test_none_returns_unknown(self):
        assert normalize_provider_name(None) == "unknown"

    def test_none_is_not_ours(self):
        """None should not be confused with 'ours'."""
        assert normalize_provider_name(None) != "ours"


# ── Idempotency ──────────────────────────────────────────────────────────────


class TestIdempotency:
    """Normalizing an already-normalized name is a no-op."""

    @pytest.mark.parametrize("name", [
        "ours", "friend", "ollama_cloud", "ppq", "openrouter",
        "deepinfra", "unknown",
        "zai_ours", "zai_friend", "manager", "worker",
    ])
    def test_double_normalize_is_stable(self, name):
        once = normalize_provider_name(name)
        twice = normalize_provider_name(once)
        assert once == twice