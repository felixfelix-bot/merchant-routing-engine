"""key_health_tracker.py — Track z.ai key quota health.

When a key returns an error response (quota exhausted, auth failure),
it's marked exhausted for 5 minutes. best_key() skips exhausted keys.
When both are exhausted, the caller should failover to external providers.

Empty content (reasoning model didn't produce output) does NOT mark
a key as exhausted — the key works, the model just didn't produce
output for that specific request.

Extracted from ~/.hermes/bot/zai_proxy.py (Phase 1 — standalone copy).
"""
from __future__ import annotations
import time

_EXHAUSTED_RETRY_SECONDS = 300  # retry exhausted key after 5 min

_zai_key_health: dict[str, dict] = {}


def is_key_healthy(name: str) -> bool:
    """Check if a z.ai key has quota remaining."""
    h = _zai_key_health.get(name)
    if not h or h.get("healthy", True):
        return True
    return time.time() >= h.get("retry_after", 0)


def mark_key_exhausted(name: str) -> None:
    """Mark a z.ai key as out of quota (error response or 429).

    Do NOT call this for empty content responses — those are model
    behavior issues, not quota issues.
    """
    _zai_key_health[name] = {
        "healthy": False,
        "last_empty": time.time(),
        "retry_after": time.time() + _EXHAUSTED_RETRY_SECONDS,
    }


def mark_key_healthy(name: str) -> None:
    """Mark a z.ai key as healthy (successful response with content)."""
    _zai_key_health[name] = {"healthy": True}


def select_healthy_key(chosen: str | None) -> str | None:
    """Given a preferred key, return it if healthy. Otherwise try the other.
    Return None if both are exhausted.

    Phase 4 of best_key() — called after Kalman/quota selection.
    """
    if chosen and is_key_healthy(chosen):
        return chosen
    other = "friend" if chosen == "ours" else "ours"
    if is_key_healthy(other):
        return other
    return None
