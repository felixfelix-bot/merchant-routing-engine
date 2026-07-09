"""provider_funding_tracker.py — Track PPQ/OpenRouter credit status.

When a provider returns 402 (out of credits), it's marked unfunded
for 1 hour. The failover logic only tries funded providers, sorted
by cost. No hardcoded order — dynamic cheapest-funded selection.

Extracted from ~/.hermes/bot/zai_proxy.py (Phase 1 — standalone copy).
"""
from __future__ import annotations
import time

_UNFUNDED_RETRY_SECONDS = 3600  # retry unfunded provider after 1 hour

_provider_health: dict[str, dict] = {}


def is_provider_funded(name: str) -> bool:
    """Check if a provider has credits remaining."""
    h = _provider_health.get(name)
    if not h or h.get("funded", True):
        return True
    return time.time() >= h.get("retry_after", 0)


def mark_unfunded(name: str) -> None:
    """Mark a provider as out of credits (after receiving 402)."""
    _provider_health[name] = {
        "funded": False,
        "last_402": time.time(),
        "retry_after": time.time() + _UNFUNDED_RETRY_SECONDS,
    }


def mark_funded(name: str) -> None:
    """Mark a provider as funded again (successful response)."""
    _provider_health[name] = {"funded": True}


def get_funded_providers(providers: dict) -> list[tuple[str, dict]]:
    """Return list of (name, provider_config) for funded providers only."""
    return [
        (name, config) for name, config in providers.items()
        if config.get("key") and is_provider_funded(name)
    ]
