"""model_mapping.py — Maps ``(provider, task_type) → model_name``.

Single lookup table that determines *which model* to send to each provider
for a given task type.  Config-driven: reads
``config/providers.yaml → strategy.model_map`` when present, falling back
to the hardcoded :data:`MODEL_MAP` defaults below.

Provider names follow the canonical names from :mod:`src.provider_names`
(``ours``, ``friend``, ``ollama_cloud``, ``ppq``, ``openrouter``,
``deepinfra``).  z.ai key names (``ours``, ``friend``, ``zai_ours``,
``manager``, ``worker``) all collapse to the ``zai`` *service* before
lookup, since every z.ai key hits the same endpoint with the same model
catalogue.

Example::

    >>> get_model("ours", "coding")
    'glm-5.2'
    >>> get_model("friend", "simple")
    'glm-4.5-flash'
    >>> get_model("deepinfra", "coding")
    'deepseek-v4-pro'
    >>> get_model("ppq", "coding")
    'kimi-k3'
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

__all__ = [
    "MODEL_MAP",
    "TASK_TYPES",
    "DEFAULT_TASK_TYPE",
    "DEFAULT_MODEL",
    "get_model",
    "get_models_for_provider",
    "load_model_map",
    "normalize_service",
]


# ── Service aliases (z.ai key names → "zai" service) ──────────────────────────

#: All names that refer to a z.ai key.  They share one model catalogue,
#: so they collapse to the ``"zai"`` service before model-map lookup.
_ZAI_ALIASES: frozenset[str] = frozenset({
    "zai", "zai_ours", "zai_friend", "ours", "friend", "manager", "worker",
})

#: Known external / flat-rate providers (pass-through, no aliasing).
_KNOWN_PROVIDERS: frozenset[str] = frozenset({
    "zai", "ollama_cloud", "ppq", "openrouter", "deepinfra",
})


def normalize_service(provider: str | None) -> str:
    """Map a provider name to its *service* name for model-map lookup.

    All z.ai key names (``ours``, ``friend``, ``zai_ours``, ``manager``,
    ``worker``) collapse to ``"zai"`` because they share the same model
    catalogue.  ``None`` becomes ``"unknown"``.  Every other name passes
    through unchanged.

    >>> normalize_service("ours")
    'zai'
    >>> normalize_service("manager")
    'zai'
    >>> normalize_service("deepinfra")
    'deepinfra'
    >>> normalize_service(None)
    'unknown'
    """
    if provider is None:
        return "unknown"
    if provider in _ZAI_ALIASES:
        return "zai"
    return provider


# ── Hardcoded defaults (mirrors config/providers.yaml → strategy.model_map) ────

#: Task types recognised by the routing engine.
TASK_TYPES: frozenset[str] = frozenset({"coding", "reasoning", "chat", "simple"})

#: Task type used when the caller doesn't specify one (or gives an
#: unrecognised value).
DEFAULT_TASK_TYPE: str = "coding"

#: Model returned when nothing better is known (global last-resort).
DEFAULT_MODEL: str = "glm-4.5-flash"

#: The canonical ``(provider, task_type) → model_name`` mapping.
#:
#: These mirror the production model catalogue per provider.  The
#: ``strategy.model_map`` section of ``config/providers.yaml`` overrides
#: them at runtime (see :func:`load_model_map`).
MODEL_MAP: dict[tuple[str, str], str] = {
    # ── z.ai (ours + friend keys share the same catalogue) ───────────────
    ("zai", "coding"):    "glm-5.2",
    ("zai", "reasoning"): "glm-4.5",
    ("zai", "chat"):      "glm-4.5-air",
    ("zai", "simple"):    "glm-4.5-flash",

    # ── DeepInfra ────────────────────────────────────────────────────────
    ("deepinfra", "coding"):    "deepseek-v4-pro",
    ("deepinfra", "reasoning"): "deepseek-v4-pro",
    ("deepinfra", "chat"):      "deepseek-v4-flash",
    ("deepinfra", "simple"):    "deepseek-v4-flash",

    # ── PPQ ──────────────────────────────────────────────────────────────
    ("ppq", "coding"):    "kimi-k3",
    ("ppq", "reasoning"): "kimi-k3",
    ("ppq", "chat"):      "deepseek-v4-flash",
    ("ppq", "simple"):    "deepseek-v4-flash",

    # ── OpenRouter ───────────────────────────────────────────────────────
    ("openrouter", "coding"):    "deepseek-v4-pro",
    ("openrouter", "reasoning"): "deepseek-v4-pro",
    ("openrouter", "chat"):      "deepseek-v4-flash",
    ("openrouter", "simple"):    "deepseek-v4-flash",

    # ── Ollama Cloud ─────────────────────────────────────────────────────
    ("ollama_cloud", "coding"):    "llama3.3-70b",
    ("ollama_cloud", "reasoning"): "llama3.3-70b",
    ("ollama_cloud", "chat"):      "llama3.3-8b",
    ("ollama_cloud", "simple"):    "llama3.3-8b",
}

#: Per-provider default model (used for unknown task types within a
#: known provider).  Falls back to :data:`DEFAULT_MODEL` if the provider
#: has no entry at all.
_PROVIDER_DEFAULTS: dict[str, str] = {
    "zai":          "glm-4.5-flash",
    "deepinfra":    "deepseek-v4-flash",
    "ppq":          "deepseek-v4-flash",
    "openrouter":   "deepseek-v4-flash",
    "ollama_cloud": "llama3.3-8b",
}

#: Path to the providers config, relative to this file.
_CONFIG_PATH: Path = Path(__file__).resolve().parent.parent / "config" / "providers.yaml"


# ── Config loading ────────────────────────────────────────────────────────────


def load_model_map(
    config_path: str | os.PathLike | None = None,
) -> dict[tuple[str, str], str]:
    """Load the ``(provider, task_type) → model`` mapping from config.

    Reads the ``strategy.model_map`` section of the YAML config and
    flattens it from ``{provider: {task_type: model}}`` into the
    ``{(provider, task_type): model}`` shape used at runtime.

    Falls back to the hardcoded :data:`MODEL_MAP` when:

    * the config file is missing,
    * PyYAML is not importable,
    * the YAML cannot be parsed, or
    * the ``strategy.model_map`` section is absent / empty.

    Parameters
    ----------
    config_path:
        Override path to the config file.  Defaults to
        ``config/providers.yaml`` relative to the repo root.

    Returns
    -------
    dict[tuple[str, str], str]
        A fresh dict of ``(provider, task_type) → model_name`` entries.
    """
    path = Path(config_path) if config_path else _CONFIG_PATH
    if not path.is_file():
        return dict(MODEL_MAP)
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return dict(MODEL_MAP)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        # Corrupt or unreadable YAML — never crash the router.
        return dict(MODEL_MAP)

    raw_map = (data.get("strategy") or {}).get("model_map") or {}
    if not isinstance(raw_map, dict) or not raw_map:
        return dict(MODEL_MAP)

    # Flatten {provider: {task_type: model}} → {(provider, task_type): model}
    flat: dict[tuple[str, str], str] = {}
    for provider, tasks in raw_map.items():
        if not isinstance(tasks, dict):
            continue
        for task_type, model in tasks.items():
            if model is None:
                continue
            flat[(str(provider), str(task_type))] = str(model)

    return flat or dict(MODEL_MAP)


@lru_cache(maxsize=16)
def _cached_load(config_path_str: str) -> dict[tuple[str, str], str]:
    """Memoised loader keyed by config path string (avoids re-reading)."""
    return load_model_map(config_path_str)


def _default_table() -> dict[tuple[str, str], str]:
    """Return the model map for the default config path (cached)."""
    return _cached_load(str(_CONFIG_PATH))


# ── Public API ────────────────────────────────────────────────────────────────


def get_model(
    provider: str | None,
    task_type: str | None = None,
    *,
    model_map: dict[tuple[str, str], str] | None = None,
) -> str:
    """Return the model name for a ``(provider, task_type)`` pair.

    Resolution order:

    1. **Normalise** the provider — z.ai key names (``ours``, ``friend``,
       ``manager``…) collapse to ``"zai"``.
    2. **Exact match** ``(service, task_type)`` in the model map.
    3. **Per-provider default** — the provider's model for
       :data:`DEFAULT_TASK_TYPE` (``"coding"``).
    4. **Any model** defined for that provider.
    5. **Static per-provider default** from ``_PROVIDER_DEFAULTS``.
    6. **Global fallback** :data:`DEFAULT_MODEL`.

    Parameters
    ----------
    provider:
        Provider name — canonical (``ours``), alias (``manager``), or
        service-level (``zai``).
    task_type:
        One of :data:`TASK_TYPES`.  ``None`` or an unrecognised value
        falls back to :data:`DEFAULT_TASK_TYPE`.
    model_map:
        Override the lookup table (mainly for testing).  Defaults to the
        table loaded from config via :func:`load_model_map`.

    Returns
    -------
    str
        The resolved model name.
    """
    service = normalize_service(provider)
    tt = task_type if task_type in TASK_TYPES else DEFAULT_TASK_TYPE
    table = model_map if model_map is not None else _default_table()

    # 1. Exact (service, task_type) match.
    exact = table.get((service, tt))
    if exact is not None:
        return exact

    # 2. Per-provider default (DEFAULT_TASK_TYPE for this service).
    default_for_service = table.get((service, DEFAULT_TASK_TYPE))
    if default_for_service is not None:
        return default_for_service

    # 3. Any model defined for this provider.
    for (prov, _task), model in table.items():
        if prov == service:
            return model

    # 4. Static per-provider default.
    if service in _PROVIDER_DEFAULTS:
        return _PROVIDER_DEFAULTS[service]

    # 5. Global fallback.
    return DEFAULT_MODEL


def get_models_for_provider(
    provider: str | None,
    *,
    model_map: dict[tuple[str, str], str] | None = None,
) -> dict[str, str]:
    """Return ``{task_type: model}`` for a single provider.

    Useful for introspection / debugging which models a provider offers.

    Parameters
    ----------
    provider:
        Provider name (normalised via :func:`normalize_service`).
    model_map:
        Override the lookup table (mainly for testing).

    Returns
    -------
    dict[str, str]
        Mapping of every ``task_type → model`` for that provider.  Empty
        dict for an unknown provider.
    """
    service = normalize_service(provider)
    table = model_map if model_map is not None else _default_table()
    return {
        task_type: model
        for (prov, task_type), model in table.items()
        if prov == service
    }
