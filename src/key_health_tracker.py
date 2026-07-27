"""key_health_tracker.py — Track z.ai key quota health.

When a key returns an error response (quota exhausted, auth failure), it's
marked as having failures. failure_count increments on each failure and
resets to 0 on success. The failure_count is exposed for the pricing
engine's graduated health_pricing_factor.

A key is "exhausted" when unhealthy — its failure_count exceeds the
breaker threshold (>10), making its effective price infinite (circuit
breaker). Below that, the graduated penalty makes the key progressively
more expensive so the optimizer naturally routes traffic away.

Empty content (reasoning model didn't produce output) does NOT mark
a key as exhausted — the key works, the model just didn't produce
output for that specific request.

Extracted from ~/.hermes/bot/zai_proxy.py (Phase 1 — standalone copy).
"""
from __future__ import annotations
import time

_EXHAUSTED_RETRY_SECONDS = 300  # retry exhausted key after 5 min

# Matches HEALTH_BREAKER_THRESHOLD in pricing_engine.py — when failure_count
# exceeds this, the key is considered "unhealthy" (circuit breaker tripped).
_FAILURE_BREAKER_THRESHOLD = 10

_zai_key_health: dict[str, dict] = {}


def is_key_healthy(name: str) -> bool:
    """Check if a z.ai key has quota remaining."""
    h = _zai_key_health.get(name)
    if not h or h.get("healthy", True):
        return True
    return time.time() >= h.get("retry_after", 0)


def get_failure_count(name: str) -> int:
    """Get the current failure_count for a key (0 if no failures recorded).

    Used by the pricing engine to compute the graduated health_pricing_factor.
    """
    h = _zai_key_health.get(name)
    if not h:
        return 0
    return h.get("failure_count", 0)


def is_breaker_tripped(name: str) -> bool:
    """Check if the circuit breaker has tripped for a key.

    The breaker trips when failure_count exceeds _FAILURE_BREAKER_THRESHOLD
    (>10) or when the key has been explicitly marked exhausted and the
    retry-after timeout hasn't elapsed yet.
    """
    if get_failure_count(name) > _FAILURE_BREAKER_THRESHOLD:
        return True
    h = _zai_key_health.get(name)
    if not h:
        return False
    if not h.get("healthy", True):
        return time.time() < h.get("retry_after", 0)
    return False


def mark_key_exhausted(name: str) -> None:
    """Mark a z.ai key as out of quota (error response or 429).

    Increments failure_count and sets the key as unhealthy with a 5-minute
    retry window. Do NOT call this for empty content responses — those are
    model behavior issues, not quota issues.
    """
    h = _zai_key_health.get(name, {})
    failure_count = h.get("failure_count", 0) + 1
    _zai_key_health[name] = {
        "healthy": False,
        "failure_count": failure_count,
        "last_empty": time.time(),
        "retry_after": time.time() + _EXHAUSTED_RETRY_SECONDS,
    }


def mark_key_healthy(name: str) -> None:
    """Mark a z.ai key as healthy (successful response with content).

    Resets failure_count to 0 — a single success clears the failure history.
    """
    _zai_key_health[name] = {"healthy": True, "failure_count": 0}


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
