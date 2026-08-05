"""cost_extraction.py — extract real $ cost from each provider's API response.

RP-2 of the real-price-tracker plan. Each external provider returns the actual
USD charge for a request in a slightly different place; this module knows where
to look and returns a single ``(cost_usd, cost_source)`` pair.

Why a module?
    The production proxy (~/.hermes/bot/zai_proxy.py) needs this logic in its
    hot request path, but the parsing rules are provider-specific and worth
    unit-testing in isolation. Keeping it here lets the proxy stay thin and lets
    tests exercise every field path against canned JSON / SSE buffers without
    any network or DB.

Design rules (mirror src/token_audit.py)
    * **NEVER raises.** Runs inside the proxy's request-handling path. Any error
      — bogus types, ``None`` buffer, malformed JSON, overflow — is swallowed
      and yields ``(None, None)`` so a logging failure never breaks a request.
    * **Cheap.** One or two JSON passes over a buffer already in memory.
    * **Stateless & side-effect free.** Pure function of its inputs.

Per-provider field paths
    ----------------------
    openrouter:
        ``usage.cost`` — OpenRouter documents this and returns it in *every*
        chat-completion response (streaming and non-streaming). Source:
        measured.
    deepinfra:
        ``usage.estimated_cost`` — DeepInfra's actual charge including
        prompt-caching discounts. Confirmed in production (the proxy's
        ``_deduct_deepinfra_balance`` has been deducting from it). Source:
        measured.
    ppq:
        PPQ's response contract is less documented. We probe several plausible
        paths in priority order — ``usage.cost``, top-level ``cost``,
        ``usage.total_cost``, ``usage.estimated_cost`` — and take the first
        numeric hit. If none match, cost is unknown (caller may estimate).
        Source: measured when found.
    ollama_cloud:
        Flat-rate subscription — **no per-call cost is returned in the body.**
        This module returns ``(None, None)``; the proxy wrapper computes an
        *estimated* cost from the current quota regime and token count.
    ours / friend (z.ai):
        Flat-rate subscription — marginal cost is $0. This module returns
        ``(None, None)``; the proxy wrapper records ``0.0`` with source
        ``flat_rate``.

Public API
    ``extract_cost(provider, response_buffer)``
        → ``(cost_usd, cost_source)`` or ``(None, None)``.
"""
from __future__ import annotations

import json
from typing import Any

__all__ = [
    "extract_cost",
    "extract_cost_from_obj",
    "PROVIDER_COST_PATHS",
    "SOURCE_MEASURED",
    "SOURCE_ESTIMATED",
]

# ── cost_source vocabulary (matches RP-1 migration: scripts/add_cost_column.py) ──
SOURCE_MEASURED = "measured"      # real $ parsed directly from the provider's response
SOURCE_ESTIMATED = "estimated"   # computed from a model/rate, not directly returned
# 'flat_rate' and 'backfilled' are also in the vocabulary but produced by the
# proxy wrapper / RP-1 migration respectively, not by this module.


# ── per-provider field paths (dot-notation, checked in order) ─────────────────
# Each entry lists the JSON paths to probe inside the response object, and the
# ``cost_source`` tag to attribute when a path hits. Paths use dot notation:
# "usage.cost" → obj["usage"]["cost"].
PROVIDER_COST_PATHS: dict[str, dict[str, Any]] = {
    "openrouter": {
        "paths": ["usage.cost"],
        "source": SOURCE_MEASURED,
    },
    "deepinfra": {
        "paths": ["usage.estimated_cost"],
        "source": SOURCE_MEASURED,
    },
    "ppq": {
        # PPQ's cost field location is not firmly documented; probe in priority
        # order and take the first numeric value found.
        "paths": ["usage.cost", "cost", "usage.total_cost", "usage.estimated_cost"],
        "source": SOURCE_MEASURED,
    },
}


def _get_path(obj: Any, dotted: str) -> Any:
    """Resolve a dot-notation path (``a.b.c``) against a dict. Returns None on
    any miss / wrong type. Never raises."""
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    return cur


def extract_cost_from_obj(provider: str, obj: Any) -> tuple[float | None, str | None]:
    """Extract cost from an already-parsed response object (dict).

    Returns ``(cost_usd, cost_source)`` or ``(None, None)`` when the provider is
    unknown or no cost field is present. Never raises.
    """
    spec = PROVIDER_COST_PATHS.get(provider)
    if spec is None or not isinstance(obj, dict):
        return (None, None)
    for path in spec["paths"]:
        val = _get_path(obj, path)
        if val is None:
            continue
        try:
            cost = float(val)
        except (TypeError, ValueError):
            continue
        # Guard against nonsensical values (negative, NaN, inf) — treat as miss.
        if cost != cost or cost in (float("inf"), float("-inf")) or cost < 0:
            continue
        return (cost, spec["source"])
    return (None, None)


def _parse_response_objects(response_buffer: bytes) -> list[dict]:
    """Parse a response buffer into one or more candidate JSON objects.

    Handles:
      * non-streaming: the whole buffer is one JSON object.
      * streaming SSE: each ``data: {...}`` line is a JSON object; we collect
        them all (the usage/cost typically rides the final chunk, but some
        providers spread metadata across chunks).

    Returns a list of dicts (possibly empty). Never raises.
    """
    if not response_buffer:
        return []
    objs: list[dict] = []
    # Non-streaming: whole buffer as one JSON object.
    try:
        obj = json.loads(response_buffer)
        if isinstance(obj, dict):
            objs.append(obj)
            return objs  # a single JSON body is unambiguous — no need to scan SSE
    except Exception:
        pass
    # Streaming SSE: collect every parseable data: payload.
    try:
        for line in response_buffer.decode("utf-8", "ignore").splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            if isinstance(obj, dict):
                objs.append(obj)
    except Exception:
        pass
    return objs


def extract_cost(provider: str, response_buffer: bytes) -> tuple[float | None, str | None]:
    """Extract the real USD cost from a provider's API response buffer.

    Parameters
    ----------
    provider
        Provider key name as used in the proxy (``"openrouter"``,
        ``"deepinfra"``, ``"ppq"``). Flat-rate providers (``"ollama_cloud"``,
        ``"ours"``, ``"friend"``) are not handled here — they return
        ``(None, None)`` and the proxy wrapper computes/zeroes their cost.
    response_buffer
        Raw response bytes (JSON or SSE). May be empty.

    Returns
    -------
    (cost_usd, cost_source)
        ``cost_usd`` is a non-negative float, ``cost_source`` is one of the
        ``SOURCE_*`` constants. ``(None, None)`` when the provider is unknown,
        flat-rate, or no cost field was found in the response. Never raises.
    """
    spec = PROVIDER_COST_PATHS.get(provider)
    if spec is None:
        # Unknown or flat-rate provider — not our job; the proxy wrapper handles it.
        return (None, None)
    for obj in _parse_response_objects(response_buffer):
        cost, source = extract_cost_from_obj(provider, obj)
        if cost is not None:
            return (cost, source)
    return (None, None)
