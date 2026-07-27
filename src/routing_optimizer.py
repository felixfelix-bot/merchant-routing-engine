"""routing_optimizer.py — Deterministic cost-minimizing router.

Collects effective prices from every registered provider (each backed by its
own :class:`~src.price_kalman.PriceKalman` and
:class:`~src.consumption_kalman.ConsumptionKalman`), filters out providers
that are exhausted, unhealthy (circuit breaker tripped), or below the
requested quality tier, and returns the cheapest viable one.

This is the deterministic core described in ADR-003/004/005:

* The Kalman filters supply the **smoothed base rate**.
* Deterministic multipliers — peak / scarcity / health — supply the
  **instant step changes** on top (ADR-003).
* The router just collects, filters, and sorts (ADR-004: effective price is
  always > 0; ADR-005: a tripped breaker yields infinite cost, not zero).

There is no randomness, no learning loop, and no cross-provider coupling in
this module — it is a pure function of its registered provider state.
"""
from __future__ import annotations

import math
from typing import Any

from src.price_kalman import (
    MIN_EFFECTIVE_PRICE,
    PriceKalman,
    health_pricing_factor,
    peak_multiplier,
    scarcity_factor,
)

__all__ = ["RoutingOptimizer", "TIER_RANK", "DIFFICULTY_TO_TIER", "TIER_MODELS"]


# ── Quality tiers (mirrors config/providers.yaml → strategy.quality_tiers) ────

#: Numeric rank per tier. Higher rank beats lower rank.
TIER_RANK = {"low": 0, "standard": 1, "high": 2}

#: Difficulty level → minimum tier a provider must meet to be viable.
DIFFICULTY_TO_TIER = {
    "high": "high",        # only top-tier models (glm-5.2 / glm-4.5)
    "medium": "standard",  # glm-4.5-air and above are acceptable
    "low": "low",          # anything goes, including glm-4.5-flash
}

#: Representative model name per tier (first model of each tier list).
TIER_MODELS = {
    "high": "glm-5.2",
    "standard": "glm-4.5-air",
    "low": "glm-4.5-flash",
}

#: Model used when no provider is viable (config/providers.yaml → fallback_model).
FALLBACK_MODEL = "deepseek/deepseek-v4-flash"


class RoutingOptimizer:
    """Deterministic cost minimizer. Collects prices from all providers,
    filters exhausted/unhealthy/low-quality, returns cheapest viable."""

    def __init__(
        self,
        peak_hours_utc: tuple[int, int] = (6, 10),
        peak_mult: float = 3.0,
        fallback_model: str = FALLBACK_MODEL,
        exhaustion_horizon: int = 1,
    ) -> None:
        if len(peak_hours_utc) != 2:
            raise ValueError(
                f"peak_hours_utc must be a (start, end) pair, got {peak_hours_utc}"
            )
        self._providers: dict[str, dict[str, Any]] = {}
        self._peak_hours_utc = (
            int(peak_hours_utc[0]),
            int(peak_hours_utc[1]),
        )
        self._peak_mult = float(peak_mult)
        self._fallback_model = str(fallback_model)
        self._exhaustion_horizon = int(exhaustion_horizon)

    # ── Registration ────────────────────────────────────────────────────

    def add_provider(
        self,
        name: str,
        price_kalman: PriceKalman,
        consumption_kalman: Any,
        quota_remaining: float,
        breaker_tripped: bool = False,
        model_tier: str = "high",
        model: str | None = None,
        quota_total: float | None = None,
        peak_hours_utc: tuple[int, int] | None = None,
        peak_mult: float = 1.0,
        failure_count: int = 0,
    ) -> None:
        """Register a provider with its Kalman instances.

        Parameters
        ----------
        name:
            Human-readable provider identifier (e.g. ``"ours"``).
        price_kalman:
            A trained :class:`PriceKalman` for this provider's smoothed $/M.
        consumption_kalman:
            A :class:`ConsumptionKalman` tracking this provider's token burn.
        quota_remaining:
            Tokens still available before the next quota window resets.
        breaker_tripped:
            ``True`` if the circuit breaker is currently open for this
            provider — makes it unreachable (infinite effective price).
        model_tier:
            One of ``"high"`` / ``"standard"`` / ``"low"``. Used to gate
            providers against the requested ``difficulty``.
        model:
            Optional explicit model name. Defaults to the representative
            model of ``model_tier``.
        quota_total:
            Optional full quota size. When supplied, enables the scarcity
            multiplier; otherwise scarcity defaults to 1.0.
        peak_hours_utc:
            Optional (start, end) UTC hour pair. When None (default), this
            provider has no peak hours — flat rate always. z.ai providers
            should pass their peak window here (ADR-003).
        peak_mult:
            Peak-hour price multiplier (default 1.0 = no peak surcharge).
            z.ai providers typically pass 3.0.
        failure_count:
            Number of consecutive recent failures for this provider. Used
            by the graduated health_pricing_factor to progressively increase
            effective price (1.0x → 1.5x → 3.0x → 10.0x → +inf). Defaults
            to 0 (no penalty).
        """
        if model_tier not in TIER_RANK:
            raise ValueError(
                f"unknown model_tier {model_tier!r}; "
                f"expected one of {sorted(TIER_RANK)}"
            )
        self._providers[name] = {
            "price_kalman": price_kalman,
            "consumption_kalman": consumption_kalman,
            "quota_remaining": float(quota_remaining),
            "quota_total": float(quota_total) if quota_total is not None else None,
            "breaker_tripped": bool(breaker_tripped),
            "failure_count": int(failure_count),
            "model_tier": model_tier,
            "model": model if model is not None else TIER_MODELS[model_tier],
            "peak_hours_utc": tuple(peak_hours_utc) if peak_hours_utc else None,
            "peak_mult": float(peak_mult),
        }

    # ── Core routing ────────────────────────────────────────────────────

    def route(
        self,
        difficulty: str = "medium",
        estimated_tokens: int = 10000,
        hour: int | None = None,
        pace_mults: dict[str, float] | None = None,
    ) -> dict:
        """Return the cheapest viable provider for the requested difficulty.

        Parameters
        ----------
        difficulty:
            ``"high"`` / ``"medium"`` / ``"low"`` — sets the minimum
            acceptable model tier.
        estimated_tokens:
            How many tokens the upcoming request is expected to consume.
            Used together with the ConsumptionKalman's ``will_exhaust`` to
            filter providers that cannot serve the request.
        hour:
            Override for the current UTC hour (mainly for deterministic
            testing of the peak multiplier). ``None`` → use real UTC hour.

        Returns
        -------
        dict
            ``chosen_provider``, ``chosen_model``, ``effective_cost_per_1m``,
            ``reason`` and a ``candidates`` list (one entry per registered
            provider with ``provider`` / ``price`` / ``viable`` / ``reason``).
        """
        if difficulty not in DIFFICULTY_TO_TIER:
            raise ValueError(
                f"unknown difficulty {difficulty!r}; "
                f"expected one of {sorted(DIFFICULTY_TO_TIER)}"
            )
        required_rank = TIER_RANK[DIFFICULTY_TO_TIER[difficulty]]

        # Peak multiplier is per-provider (ADR-003): z.ai has peak hours,
        # Ollama/PPQ/OpenRouter do not. Each provider carries its own
        # peak_hours_utc + peak_mult registered via add_provider().
        pace_mults = pace_mults or {}
        candidates: list[dict] = []
        for name, provider in self._providers.items():
            # Compute this provider's peak multiplier (1.0 if no peak window)
            prov_ph = provider.get("peak_hours_utc")
            if prov_ph:
                prov_peak = peak_multiplier(
                    hour=hour,
                    peak_hours_utc=prov_ph,
                    peak_mult=provider.get("peak_mult", 1.0),
                )
            else:
                prov_peak = 1.0
            # Per-provider pace multiplier (defaults to 1.0 if not supplied)
            prov_pace = pace_mults.get(name, 1.0)
            price, viable, reason = self._evaluate_provider(
                provider, prov_peak, required_rank, estimated_tokens, prov_pace
            )
            candidates.append(
                {
                    "provider": name,
                    "price": price,
                    "viable": viable,
                    "reason": reason,
                }
            )

        # Cheapest first; inf (filtered) entries sort to the end.
        candidates.sort(key=lambda c: c["price"])
        viable = [c for c in candidates if c["viable"]]

        if viable:
            best = viable[0]
            model = self._providers[best["provider"]]["model"]
            return {
                "chosen_provider": best["provider"],
                "chosen_model": model,
                "effective_cost_per_1m": best["price"],
                "reason": (
                    f"cheapest viable provider: {best['provider']} "
                    f"at ${best['price']:.6f}/M "
                    f"(difficulty={difficulty})"
                ),
                "candidates": candidates,
            }

        # No viable provider — fall back to the configured external model.
        return {
            "chosen_provider": "fallback",
            "chosen_model": self._fallback_model,
            "effective_cost_per_1m": float("inf"),
            "reason": (
                f"no viable provider for difficulty={difficulty} "
                f"with ~{estimated_tokens} tokens; "
                f"falling back to {self._fallback_model}"
            ),
            "candidates": candidates,
        }

    # ── Per-provider evaluation ─────────────────────────────────────────

    def _evaluate_provider(
        self,
        provider: dict[str, Any],
        peak_mult: float,
        required_rank: int,
        estimated_tokens: int,
        pace_mult: float = 1.0,
    ) -> tuple[float, bool, str]:
        """Run a single provider through the filter pipeline.

        Returns ``(effective_price, viable, reason)``. Non-viable providers
        get ``effective_price = inf`` and a descriptive ``reason``.
        """
        # 1. Quality tier gate — must meet the difficulty's minimum tier.
        if TIER_RANK[provider["model_tier"]] < required_rank:
            return (
                float("inf"),
                False,
                f"model_tier {provider['model_tier']!r} below "
                f"required tier for this difficulty",
            )

        # 2. Health gate — graduated health pricing. The effective price
        #    increases progressively with failure_count (1.5x → 3.0x → 10.0x)
        #    so the optimizer naturally routes traffic away. Only when
        #    failure_count > 10 or breaker_tripped does price go to infinity
        #    (full circuit breaker — ADR-004 #4).
        health = health_pricing_factor(
            failure_count=provider["failure_count"],
            breaker_tripped=provider["breaker_tripped"],
        )
        if math.isinf(health):
            fc = provider["failure_count"]
            if provider["breaker_tripped"]:
                reason = "circuit breaker tripped — provider unreachable"
            else:
                reason = (
                    f"failure_count={fc} exceeds breaker threshold "
                    f"— provider unreachable"
                )
            return (
                float("inf"),
                False,
                reason,
            )

        # 3. Exhaustion gate — skip if the ConsumptionKalman predicts the
        #    provider will run out before serving our request AND the
        #    remaining quota is already below what we need.
        ck = provider["consumption_kalman"]
        will_exhaust, _eta = ck.will_exhaust(
            provider["quota_remaining"], self._exhaustion_horizon
        )
        if will_exhaust and provider["quota_remaining"] < estimated_tokens:
            return (
                float("inf"),
                False,
                (
                    f"quota will exhaust before request "
                    f"(remaining={provider['quota_remaining']:.0f}, "
                    f"need≈{estimated_tokens})"
                ),
            )

        # 4. Scarcity multiplier — ramps as the quota is consumed.
        quota_total = provider["quota_total"]
        if quota_total and quota_total > 0:
            quota_used_pct = max(
                0.0,
                (1.0 - provider["quota_remaining"] / quota_total) * 100.0,
            )
        else:
            quota_used_pct = 0.0
        scarcity = scarcity_factor(quota_used_pct)

        # 5. Effective price — base × peak × scarcity × health × pace (ADR-003).
        effective_price = provider["price_kalman"].effective_price(
            peak_mult=peak_mult,
            scarcity=scarcity,
            health=health,
            pace_mult=pace_mult,
        )

        # ADR-004: effective_price() already floors at MIN_EFFECTIVE_PRICE,
        # so no additional positivity check needed here. The invariant is
        # enforced inside PriceKalman.effective_price().

        return (
            float(effective_price),
            True,
            (
                f"effective ${effective_price:.6f}/M "
                f"(peak={peak_mult:.1f}, scarcity={scarcity:.2f}, "
                f"health={health:.1f}, pace={pace_mult:.2f})"
            ),
        )
