"""routing_advisor.py — Hot-swappable optimizer-first routing advisor.

Phase 2 "Advisor Mode" (docs/execution-schedule.md §Phase 2): the
``routing_optimizer`` leaves shadow mode and is consulted BEFORE the legacy
``best_key()`` path, gated by a feature flag so it can be turned off instantly
without a redeploy. This is the half-step between "shadow (log only)" and
"primary (replace best_key entirely)".

Decision contract (validated by ``tests/test_advisor_integration.py``):

* flag OFF              → ``best_key()`` is used; the optimizer is never called
* flag ON               → ``optimizer.route()`` is called first
* optimizer raises      → fall back to ``best_key()`` (the advisor never breaks
                          routing — same discipline as ``ShadowHook``)
* optimizer returns an  → fall back to ``best_key()`` (e.g. the optimizer's own
  unknown provider        ``"fallback"`` sentinel, which means "nothing viable")
* optimizer returns     → route directly to ollama (self-hosted cloud, does not
  ``ollama_cloud``        go through the z.ai path)

Scope note
----------
This module is the standalone decision LOGIC only. It is deliberately
dependency-injectable (``optimizer`` + ``best_key_fn``) so it is unit-testable
with fakes and has zero coupling to the live proxy. Wiring it into
``~/.hermes/bot/zai_proxy.py`` (the actual hot-swap behind the feature flag) is
the remaining work of task **P2.2**.
"""
from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

# Add parent dir so `from src.xxx import` works when imported from outside
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from src.provider_names import normalize_provider_name

__all__ = ["RoutingAdvisor", "AdvisorDecision", "KNOWN_PROVIDERS"]

#: Providers the advisor will honour from the optimizer. Anything else
#: (including the optimizer's own ``"fallback"`` sentinel, which signals
#: "no viable provider") is treated as invalid and falls back to best_key().
#: Uses canonical names — the optimizer may return legacy aliases like
#: "zai_ours" but they are normalized before this check.
KNOWN_PROVIDERS = frozenset(
    {"ours", "friend", "ollama_cloud", "ppq", "openrouter"}
)

_OLLAMA_CLOUD = "ollama_cloud"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


class _OptimizerLike(Protocol):  # structural type — any object with .route(...)
    def route(
        self,
        difficulty: str = ...,
        estimated_tokens: int = ...,
        hour: Optional[int] = ...,
    ) -> Any:
        ...


@dataclass
class AdvisorDecision:
    """The advisor's routing decision — what the proxy should act on."""

    provider: str
    model: str
    key: Optional[str] = None
    source: str = "best_key"  # "optimizer" | "best_key"
    reason: str = ""
    routed_directly_to_ollama: bool = False
    effective_cost_per_1m: Optional[float] = None


class RoutingAdvisor:
    """Optimizer-first, best_key-fallback routing decision layer.

    Parameters
    ----------
    optimizer:
        Anything with a ``route(difficulty, estimated_tokens, hour)`` method
        returning a dict shaped like :meth:`RoutingOptimizer.route` (i.e. with
        ``chosen_provider`` / ``chosen_model`` / ``effective_cost_per_1m``).
    best_key_fn:
        Zero-arg callable returning the legacy :class:`AdvisorDecision` (the
        production ``best_key()`` path). Kept injectable so the advisor is
        testable without the production Kalman stack.
    providers:
        Whitelist of provider names the advisor trusts from the optimizer.
        Defaults to :data:`KNOWN_PROVIDERS`.
    enabled:
        Hard override for the feature flag. ``None`` (default) reads the
        ``env_var`` environment variable so operators can flip it live.
    env_var:
        Name of the feature-flag environment variable (default
        ``ROUTING_ADVISOR_ENABLED``).
    """

    def __init__(
        self,
        optimizer: _OptimizerLike,
        best_key_fn: Callable[[], AdvisorDecision],
        *,
        providers: frozenset[str] = KNOWN_PROVIDERS,
        enabled: Optional[bool] = None,
        env_var: str = "ROUTING_ADVISOR_ENABLED",
    ) -> None:
        self._optimizer = optimizer
        self._best_key_fn = best_key_fn
        self._providers = frozenset(providers)
        self._enabled = enabled
        self._env_var = env_var

    # ── Feature flag ────────────────────────────────────────────────────

    def enabled(self) -> bool:
        """True if the advisor should consult the optimizer."""
        if self._enabled is not None:
            return bool(self._enabled)
        return os.environ.get(self._env_var, "").strip().lower() in _TRUTHY

    # ── Decision ────────────────────────────────────────────────────────

    def decide(
        self,
        difficulty: str = "medium",
        estimated_tokens: int = 0,
        hour: Optional[int] = None,
    ) -> AdvisorDecision:
        """Return the routing decision for one request.

        Never raises: any optimizer failure or unrecognised result degrades
        gracefully to ``best_key()`` so the advisor can never break routing.
        """
        # Flag OFF — the optimizer is not consulted at all.
        if not self.enabled():
            return self._best_key_fn()

        # Flag ON — try the optimizer first, fall back on any problem.
        try:
            result = self._optimizer.route(
                difficulty=difficulty,
                estimated_tokens=estimated_tokens,
                hour=hour,
            )
        except Exception as exc:  # optimizer must never break routing
            return self._fallback(f"optimizer raised {type(exc).__name__}: {exc}")

        provider = (result or {}).get("chosen_provider")
        # Normalize legacy aliases (e.g. "zai_ours" → "ours") to canonical form
        if provider is not None:
            provider = normalize_provider_name(provider)
        if provider not in self._providers:
            return self._fallback(
                f"optimizer returned invalid provider {provider!r}"
            )

        model = (result or {}).get("chosen_model", "") or ""
        cost = self._finite((result or {}).get("effective_cost_per_1m"))

        # ollama_cloud is self-hosted — bypass the z.ai path entirely.
        if provider == _OLLAMA_CLOUD:
            return AdvisorDecision(
                provider=_OLLAMA_CLOUD,
                model=model or "ollama",
                source="optimizer",
                reason="optimizer chose ollama_cloud — routing directly",
                routed_directly_to_ollama=True,
                effective_cost_per_1m=cost,
            )

        # Map z.ai provider names back to their key handle.
        # After normalization, canonical names are "ours" and "friend".
        key = "ours" if provider == "ours" else (
            "friend" if provider == "friend" else None
        )
        return AdvisorDecision(
            provider=provider,
            model=model,
            key=key,
            source="optimizer",
            reason=(result or {}).get("reason", "optimizer decision"),
            effective_cost_per_1m=cost,
        )

    # ── Helpers ─────────────────────────────────────────────────────────

    def _fallback(self, reason: str) -> AdvisorDecision:
        """Invoke best_key() and tag why we fell back to it."""
        dec = self._best_key_fn()
        dec.reason = reason
        return dec

    @staticmethod
    def _finite(cost: Any) -> Optional[float]:
        """Coerce a cost to a finite float, else None (inf/NaN/None/str)."""
        try:
            c = float(cost)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        if math.isinf(c) or math.isnan(c):
            return None
        return c
