"""provider_names.py — Canonical provider name normalization.

Single source of truth for mapping legacy/alias provider names to their
canonical form.  All modules that receive provider names from external
sources (proxy, DB, API) should normalize via :func:`normalize_provider_name`
before using the name as a dict key, logging it, or comparing it.

Canonical names:
    ours, friend, ollama_cloud, ppq, openrouter, deepinfra, unknown

Legacy/alias names that map to canonical:
    zai_ours  → ours
    zai_friend → friend
    manager   → ours   (daily_spend tier — both manager & worker use our z.ai key)
    worker    → ours
    unknown   → unknown
"""
from __future__ import annotations

__all__ = ["normalize_provider_name", "CANONICAL_PROVIDERS"]

#: The set of canonical provider names recognised across the codebase.
CANONICAL_PROVIDERS: frozenset[str] = frozenset({
    "ours", "friend", "ollama_cloud", "ppq", "openrouter", "deepinfra",
    "unknown",
})

# Legacy / alias → canonical mapping.  Names not in this dict pass through
# unchanged (they may be providers we haven't seen yet).
_PROVIDER_MAPPINGS: dict[str, str] = {
    "zai_ours":  "ours",
    "zai_friend": "friend",
    "manager":   "ours",
    "worker":    "ours",
    "unknown":   "unknown",
}


def normalize_provider_name(name: str | None) -> str:
    """Normalize a provider name to its canonical form.

    Maps legacy and alias names to their canonical equivalents::

        zai_ours  → ours
        zai_friend → friend
        manager   → ours
        worker    → ours
        unknown   → unknown

    Names already in canonical form pass through unchanged.  Unrecognised
    names also pass through unchanged (they may be new providers not yet
    in the mapping table).

    Args:
        name: Provider name as received from the proxy, DB, or API.
            ``None`` is treated as ``"unknown"``.

    Returns:
        The canonical provider name.
    """
    if name is None:
        return "unknown"
    return _PROVIDER_MAPPINGS.get(name, name)