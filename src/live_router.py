"""live_router.py — Live routing wrapper around RoutingOptimizer.

Phase 3.1: Wraps the deterministic :class:`~src.routing_optimizer.RoutingOptimizer`
for live production use.  Unlike :class:`~src.shadow_hook.ShadowHook` (which runs
in read-only shadow mode), LiveRouter's ``select_failover`` decision is intended
to drive real traffic when both z.ai keys are exhausted.

Design:
  - Persistent Kalman state (PriceKalman + ConsumptionKalman per provider)
    kept across calls so they converge over time.
  - Thread-safe via a ``threading.Lock`` — called from
    ``ThreadingHTTPServer`` handler threads.
  - NEVER raises.  Every public method wraps in try/except.  Routing failure
    cannot break production; ``select_failover`` returns ``(None, None)``.

Usage (from the production proxy's failover path)::

    router = LiveRouter.get_instance()
    (chosen, chosen_model), (fallback, fallback_model) = router.select_failover(
        quota_state=_snapshot_quota(),
        health_state=_snapshot_health(),
        peak=peak,
        failure_counts=_snapshot_failures(),
        pace_windows=_snapshot_pace(),
        task_type="coding",           # P4.5d: worker profile task type
    )
    if chosen is None:
        # All routing failed — return 503 or use hard-coded fallback
        ...
"""
from __future__ import annotations

import os
import sys
import math
import threading
import time
from typing import Any

# ── Path bootstrap (same as shadow_hook) ────────────────────────────────────
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from src.price_kalman import MIN_EFFECTIVE_PRICE, PriceKalman
from src.consumption_kalman import ConsumptionKalman
from src.routing_optimizer import RoutingOptimizer
from src.provider_names import normalize_provider_name
from src.pricing_engine import pace_factor_multi, extra_usage_multiplier, EXTRA_USAGE_MULTIPLIER
from src.quota_window_extractor import _KNOWN_WINDOW_NAMES, _ERROR_SENTINEL_PCT
from src.cpvo_calculator import CPVOCalculator
from src.model_mapping import get_model
from src.ollama_quota_tracker import get_quota_status, DEFAULT_SESSION_LIMIT
from src.ollama_extra_usage import fetch_ollama_usage, get_extra_usage_status

__all__ = ["LiveRouter"]

# ── Kill switch (EU-R3): extra-usage pricing is disabled by default ──────────
# Until shadow mode validates the extra-usage multiplier, the multiplier is
# NOT applied unless OLLAMA_EXTRA_USAGE_ENABLED=true is set in the env.
# When disabled, the regime is always treated as "included" (no penalty).
_EXTRA_USAGE_ENABLED: bool = (
    os.environ.get("OLLAMA_EXTRA_USAGE_ENABLED", "false").lower() in ("1", "true", "yes")
)

# ── Ollama-exclusive models (EU-R3 Step 3) ───────────────────────────────────
# These models are ONLY served by ollama_cloud — no other provider has them.
# They MUST always route to ollama_cloud regardless of price or quota regime.
# PPQ/OpenRouter/DeepInfra do not serve kimi or gpt-oss models.
_OLLAMA_EXCLUSIVE_MODELS: frozenset[str] = frozenset({
    "kimi-k2.7-code",
    "kimi-k3:cloud",
    "gpt-oss:120b",
    "gemma4:31b",
    "qwen3.5:397b",
})

# Backward-compatible alias (older tests/code may reference _OLLAMA_ONLY_MODELS).
_OLLAMA_ONLY_MODELS = _OLLAMA_EXCLUSIVE_MODELS

# ── Converged rates (from replay_converged_rates.CONVERGED_COSTS) ────────────
# These are the Kalman-converged base rates proven in the replay. When
# ``converged_rates`` is passed to ``__init__``, they override these defaults.
_DEFAULT_CONVERGED_RATES: dict[str, float] = {
    "ours":          0.001,    # clamped from -0.000968
    "friend":        0.028983,
    "ollama_cloud":  0.023952,
    "ppq":           0.14,
    "openrouter":    0.135,
    "deepinfra":     1.30,
}

# ── Quota totals (approximate, for scarcity factor) ──────────────────────────
_QUOTA_TOTALS: dict[str, float] = {
    "ours":         2_000_000,    # ~2M tokens per 5h window
    "friend":       2_000_000,
    "ollama_cloud": 500_000_000,  # 500M tokens per 5h session (EUv2-4)
    "ppq":          float("inf"),  # pay-per-token, no hard quota
    "openrouter":   float("inf"),
    "deepinfra":    float("inf"),  # pay-per-token, no hard quota
}

# z.ai peak hours (UTC) — Ollama/PPQ/OpenRouter/DeepInfra have no peak
_ZAI_PEAK: tuple[int, int] = (6, 10)

# All providers that are NOT z.ai — these are the failover candidates
_EXTERNAL_PROVIDERS = ("ollama_cloud", "ppq", "openrouter", "deepinfra")

# ── CPVO cache (Phase 2.5.4) ─────────────────────────────────────────────────
# Effective-rate lookups query the telemetry DB; cache them so a hot failover
# path stays well under the 10 ms budget.  Refreshed every 5 minutes.
_CPVO_CACHE_TTL = 300.0  # seconds


class LiveRouter:
    """Live routing wrapper. Thread-safe, never raises.

    Maintains per-provider Kalman state (PriceKalman + ConsumptionKalman)
    across calls. ``select_failover`` builds a fresh RoutingOptimizer with
    all external providers + the friend key and returns the cheapest
    viable provider + its fallback.

    Singleton pattern (like ShadowHook) — use :meth:`get_instance`.
    """

    _instance: "LiveRouter | None" = None
    _instance_lock = threading.Lock()

    @classmethod
    def get_instance(
        cls,
        db_path: str | None = None,
        converged_rates: dict[str, float] | None = None,
    ) -> "LiveRouter":
        """Get or create the singleton instance. Thread-safe."""
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(db_path, converged_rates)
            return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton (for testing)."""
        with cls._instance_lock:
            cls._instance = None

    def __init__(
        self,
        db_path: str | None = None,
        converged_rates: dict[str, float] | None = None,
    ) -> None:
        """Initialize per-provider Kalman filters.

        Args:
            db_path: Reserved for future persistent state (currently unused;
                Kalman state lives in memory). Accepted for API symmetry
                with ShadowHook.
            converged_rates: Optional override for converged base rates.
                Defaults to _DEFAULT_CONVERGED_RATES.
        """
        self._db_path = db_path
        rates = converged_rates if converged_rates is not None else dict(_DEFAULT_CONVERGED_RATES)

        # Keep the unadjusted base rates for CPVO queries and fallback.
        self._base_rates: dict[str, float] = dict(rates)

        # Per-provider Kalman instances (persist across calls)
        self._price_kalmans: dict[str, PriceKalman] = {}
        self._consumption_kalmans: dict[str, ConsumptionKalman] = {}
        self._burn_history: dict[str, list[float]] = {}

        for name, rate in rates.items():
            self._price_kalmans[name] = PriceKalman(
                initial_rate=max(rate, 0.001),  # floor at MIN_EFFECTIVE_PRICE
                process_noise=1e-6,
                measurement_noise=1e-4,
            )
            self._consumption_kalmans[name] = ConsumptionKalman(
                process_noise=1.0,
                measurement_noise=1e6,
            )
            self._burn_history[name] = []

        # Thread safety lock — all public methods acquire this
        self._lock = threading.Lock()

        # Track initialized providers for introspection
        self._provider_names = list(rates.keys())

        # Last per-provider pace multipliers computed by _do_select_failover.
        # Exposed via the ``last_pace_mults`` property so the production proxy
        # can log the ACTUAL multipliers used in a failover decision to
        # routing_live_decisions (P3.4, Fix 2). Single source of truth.
        self._last_pace_mults: dict[str, float] = {}

        # ── EU-R3: quota regime from the last routing decision ──────────
        # Set inside _do_select_failover under self._lock. Read via the
        # ``last_quota_regime`` property so the production proxy can log it
        # to the key_decisions / routing_live_decisions table.
        self._last_quota_regime: str = "included"
        self._last_quota_status: dict | None = None

        # ── CPVO quality-aware effective rates (Phase 2.5.4) ───────────────
        # Queries the provider_telemetry table to inflate the base rate of
        # low-success providers (effective = base / success_rate).  Cached so
        # the hot failover path stays fast; falls back to base rates on any
        # error.  ``db_path`` is the same usage DB the proxy writes to.
        self._cpvo: CPVOCalculator = CPVOCalculator(db_path)
        self._cpvo_cache: dict[str, float] | None = None
        self._cpvo_cache_ts: float = 0.0

    # ── Public API ───────────────────────────────────────────────────────

    def select_failover(
        self,
        quota_state: dict[str, Any],
        health_state: dict[str, bool],
        peak: bool,
        failure_counts: dict[str, int] | None = None,
        pace_windows: dict[str, list[tuple[float, float, float, float, float]]] | None = None,
        task_type: str = "coding",
        model: str | None = None,
    ) -> tuple[tuple[str | None, str | None], tuple[str | None, str | None]]:
        """Choose a failover provider when BOTH z.ai keys are exhausted.

        Builds a RoutingOptimizer with all external providers + the friend
        key (in case friend has recovered) and routes a high-difficulty
        request to find the cheapest viable provider.

        Args:
            quota_state: Dict of provider -> {
                'used_pct': float (0-100),
                'remaining': float (tokens),
                'total': float (tokens),
            }
            health_state: Dict of provider -> bool (True=healthy).
            peak: Whether current hour is z.ai peak.
            failure_counts: Optional dict of provider -> failure_count.
            pace_windows: Optional dict of provider -> list of pace
                window tuples.
            task_type: Task type for model selection (P4.5d). One of
                ``"coding"``, ``"reasoning"``, ``"chat"``, ``"simple"``.
                Defaults to ``"coding"``.
            model: The model name being requested (EUv2-4). When this is
                an Ollama-only model (kimi-k3:cloud, kimi-k2.7-code,
                gpt-oss:120b, gemma4:31b, qwen3.5:397b), the router
                always returns ollama_cloud regardless of quota regime.
                When it is a non-exclusive model (e.g. glm-5.2) and the
                regime is "extra", the router reroutes to a cheaper
                per-token provider.

        Returns:
            ((chosen_provider, chosen_model), (fallback_provider, fallback_model))
            — each element is a ``(name, model)`` tuple, or ``(None, None)``
            on error. The fallback is the second-cheapest viable provider
            (or ``(None, None)`` if no second viable). Model names are
            resolved via :func:`src.model_mapping.get_model`.
        """
        try:
            with self._lock:
                return self._do_select_failover(
                    quota_state, health_state, peak,
                    failure_counts, pace_windows, task_type, model,
                )
        except Exception:
            return ((None, None), (None, None))

    def select_primary(
        self,
        model: str | None,
        tokens: int,
        quota_state: dict[str, Any],
        health_state: dict[str, bool],
        peak: bool = False,
        failure_counts: dict[str, int] | None = None,
        pace_windows: dict[str, list[tuple[float, float, float, float, float]]] | None = None,
    ) -> str | None:
        """Choose the primary provider (Phase 4 — not yet active).

        Currently a stub. Returns None (no decision). Will be implemented
        in Phase 4 when the LiveRouter takes over primary routing from
        the production proxy's key-rotation logic.
        """
        try:
            # Phase 4 stub — not yet implemented
            return None
        except Exception:
            return None

    def record_request(
        self,
        provider: str,
        tokens: int,
        cost_estimate: float | None = None,
    ) -> None:
        """Record a completed request to update Kalman state.

        Updates the ConsumptionKalman for the given provider with the
        token count. Optionally updates the PriceKalman if a cost
        estimate is provided.

        Args:
            provider: Canonical provider name (will be normalized).
            tokens: Total tokens consumed by the request.
            cost_estimate: Optional measured $/M for this request.
                When provided, updates the PriceKalman.
        """
        try:
            provider = normalize_provider_name(provider)
            with self._lock:
                if provider in self._consumption_kalmans:
                    self._consumption_kalmans[provider].update(float(tokens))
                    self._burn_history[provider].append(float(tokens))
                    # Keep last 100 observations for adaptive retraining
                    if len(self._burn_history[provider]) > 100:
                        self._burn_history[provider] = self._burn_history[provider][-100:]

                if cost_estimate is not None and provider in self._price_kalmans:
                    self._price_kalmans[provider].update(float(cost_estimate))
        except Exception:
            pass  # recording must never break production

    # ── Internal ─────────────────────────────────────────────────────────

    def _get_effective_rates(self) -> dict[str, float]:
        """Return CPVO-adjusted effective rates, cached for ``_CPVO_CACHE_TTL``.

        Quality-aware routing entry point (Phase 2.5.4).  A provider with a
        low success rate sees its base rate inflated by ``1 / success_rate``
        (via :meth:`CPVOCalculator.get_effective_rates`) so the optimizer
        picks it only with eyes open about the quality risk.

        * Cached so the hot failover path stays < 10 ms; the cache refreshes
          every 5 minutes.
        * On ANY error (DB locked, corrupt, missing table) the unadjusted
          base rates are returned — quality-awareness must never break routing.

        Never raises.
        """
        try:
            now = time.time()
            if (
                self._cpvo_cache is not None
                and (now - self._cpvo_cache_ts) < _CPVO_CACHE_TTL
            ):
                return self._cpvo_cache
            effective = self._cpvo.get_effective_rates(self._base_rates)
            # Guard against a pathological empty/None return.
            if not effective:
                effective = dict(self._base_rates)
            self._cpvo_cache = effective
            self._cpvo_cache_ts = now
            return effective
        except Exception:
            return dict(self._base_rates)

    def _do_select_failover(
        self,
        quota_state: dict[str, Any],
        health_state: dict[str, bool],
        peak: bool,
        failure_counts: dict[str, int] | None,
        pace_windows: dict[str, list[tuple[float, float, float, float, float]]] | None,
        task_type: str = "coding",
        model: str | None = None,
    ) -> tuple[tuple[str | None, str | None], tuple[str | None, str | None]]:
        """Internal failover selection — may raise (wrapped by caller)."""

        # ── EU-R3: Query Ollama Cloud quota regime ───────────────────────
        # On each routing decision, query the quota tracker to determine
        # whether ollama_cloud is in "included", "extra", or "exhausted"
        # regime. The regime drives the extra-usage pricing multiplier
        # and the rerouting logic for non-exclusive models.
        #
        # Kill switch: when OLLAMA_EXTRA_USAGE_ENABLED is not true (default),
        # the regime is always forced to "included" — no extra-usage penalty
        # is applied. This allows shadow-mode validation before going live.
        quota_regime = "included"
        quota_status: dict | None = None
        extra_usage_status = None
        try:
            db_path = self._db_path
            if db_path and _EXTRA_USAGE_ENABLED:
                # Primary: query the quota tracker (DB-based token counting)
                quota_status = get_quota_status(db_path)
                quota_regime = quota_status.get("regime", "included")

                # Secondary: fetch usage fractions from ollama.com/api/usage
                # and build a full ExtraUsageStatus for real quota fractions.
                try:
                    api_response = fetch_ollama_usage()
                    if api_response is not None:
                        extra_usage_status = get_extra_usage_status(
                            api_response, db_path=db_path,
                        )
                except Exception:
                    # API fetch failure is non-fatal — DB regime is enough.
                    pass
        except Exception:
            # Quota tracker failure must never break routing — default
            # to "included" (no extra-usage penalty).
            quota_regime = "included"
            quota_status = None

        # Stash for the last_quota_regime / last_quota_status properties.
        self._last_quota_regime = quota_regime
        self._last_quota_status = quota_status

        # ── EU-R3: Ollama-exclusive model short-circuit ──────────────────
        # If the requested model is Ollama-exclusive (kimi, gpt-oss, etc.),
        # it MUST route to ollama_cloud regardless of regime. No other
        # provider serves these models.
        if model is not None and model in _OLLAMA_EXCLUSIVE_MODELS:
            # Check if ollama_cloud is healthy and has quota
            oc_healthy = health_state.get("ollama_cloud", True)
            oc_quota = quota_state.get("ollama_cloud", {})
            oc_remaining = float(oc_quota.get("remaining", _QUOTA_TOTALS.get("ollama_cloud", 1e9)))
            if oc_healthy and oc_remaining > 0:
                return (
                    ("ollama_cloud", model),
                    (None, None),
                )
            # ollama_cloud is down — no alternative for exclusive models
            return ((None, None), (None, None))

        # ── EU-R3: Compute extra-usage multiplier for ollama_cloud ──────
        # When the regime is "extra", ollama_cloud's effective rate gets
        # multiplied by EXTRA_USAGE_MULTIPLIER (≈4.17x → $0.024 * 4.17 =
        # $0.10/M). When "exhausted", the multiplier is +inf —
        # ollama_cloud becomes unreachable to the optimizer.
        # When the kill switch is off, quota_regime is "included" so
        # extra_mult = 1.0 (no penalty).
        extra_mult = extra_usage_multiplier(quota_regime)

        # ── EU-R3: Set real quota_total and quota_remaining from API ─────
        # When we have usage fractions from the Ollama API, override the
        # static _QUOTA_TOTALS with real values derived from the API.
        # session_usage (0-1 fraction) tells us what fraction of the
        # included quota is used; remaining = total * (1 - usage).
        oc_quota_override: dict[str, float] = {}
        if extra_usage_status is not None:
            session_total = DEFAULT_SESSION_LIMIT
            session_usage_frac = extra_usage_status.session_usage
            oc_quota_override["total"] = float(session_total)
            oc_quota_override["remaining"] = float(
                session_total * max(0.0, 1.0 - session_usage_frac)
            )

        optimizer = RoutingOptimizer(
            peak_hours_utc=_ZAI_PEAK,
            peak_mult=3.0,
            exhaustion_horizon=1,
        )

        # ── Quality-aware effective rates (Phase 2.5.4) ───────────────────
        # Adjust each provider's base rate with the CPVO quality penalty
        # (effective = base / success_rate for low-success providers) BEFORE
        # handing rates to the optimizer.  A throwaway PriceKalman seeded with
        # the effective rate is used so the optimizer's peak/scarcity/health
        # multipliers still apply on top of the quality-adjusted base.  On any
        # CPVO failure this falls back to the unadjusted base rates.
        effective_rates = self._get_effective_rates()

        # Register ALL providers (ours + friend + externals).
        # ours is expected to be exhausted (breaker tripped), but we include
        # it anyway — the optimizer's health gate will filter it out.
        for name in self._provider_names:
            qs = quota_state.get(name, {})
            remaining = float(qs.get("remaining", _QUOTA_TOTALS.get(name, 1e9)))
            total = float(qs.get("total", _QUOTA_TOTALS.get(name, 1e9)))
            healthy = health_state.get(name, True)

            # ── EU-R3: Override ollama_cloud quota from API usage fractions ─
            # When we have real usage data from ollama.com/api/usage, use
            # the API-derived quota_total and quota_remaining instead of
            # the static _QUOTA_TOTALS defaults.
            if name == "ollama_cloud" and oc_quota_override:
                remaining = oc_quota_override.get("remaining", remaining)
                total = oc_quota_override.get("total", total)

            # Determine model tier and peak config
            if name in ("ours", "friend"):
                tier = "high"
                prov_model = "glm-5.2"
                prov_peak = _ZAI_PEAK
                prov_peak_mult = 3.0
            elif name == "ollama_cloud":
                # ── EU-R3: In "extra" regime, lower ollama_cloud's tier to
                # "low" so it competes with per-token externals (ppq,
                # openrouter) on PRICE rather than being auto-chosen at
                # "high" difficulty.  In "included" regime it stays "high"
                # (cheapest high-tier, preferred over externals).  In
                # "exhausted" it gets filtered (breaker tripped below).
                if quota_regime == "extra":
                    tier = "low"
                else:
                    tier = "high"
                prov_model = "glm-5.2"
                prov_peak = None
                prov_peak_mult = 1.0
            else:
                tier = "low"
                prov_model = "deepseek/deepseek-v4-flash"
                prov_peak = None
                prov_peak_mult = 1.0

            # Graduated health pricing
            if failure_counts is not None:
                fc = int(failure_counts.get(name, 0))
            else:
                fc = 0 if healthy else 999

            # ── EU-R3: Apply extra-usage multiplier to ollama_cloud ──────
            # In "extra" regime, ollama_cloud's base rate is multiplied by
            # EXTRA_USAGE_MULTIPLIER (≈4.17x) so the optimizer sees $0.10/M
            # instead of $0.024/M. In "exhausted" regime, extra_mult is
            # +inf, which makes the effective price infinite — the
            # optimizer filters it out as unreachable.
            # When the kill switch is off, extra_mult = 1.0 (no change).
            base_rate = float(effective_rates.get(
                name, self._base_rates.get(name, 0.001)))
            if name == "ollama_cloud" and extra_mult != 1.0:
                if math.isinf(extra_mult):
                    # Exhausted: force breaker tripped so optimizer filters it
                    healthy = False
                else:
                    base_rate = base_rate * extra_mult

            optimizer.add_provider(
                name=name,
                # Quality-aware base rate: a throwaway PriceKalman seeded with
                # the CPVO effective rate (further adjusted by the extra-usage
                # multiplier for ollama_cloud).  Keeps the optimizer's
                # multiplier pipeline (peak/scarcity/health/pace) intact
                # while making the base reflect provider quality and quota
                # regime.  Falls back to the live PriceKalman when CPVO has
                # no entry for this provider.
                price_kalman=PriceKalman(
                    initial_rate=max(base_rate, MIN_EFFECTIVE_PRICE),
                    process_noise=1e-6,
                    measurement_noise=1e-4,
                ),
                consumption_kalman=self._consumption_kalmans[name],
                quota_remaining=remaining,
                breaker_tripped=not healthy,
                model_tier=tier,
                model=prov_model,
                quota_total=total if total != float("inf") else None,
                peak_hours_utc=prov_peak,
                peak_mult=prov_peak_mult,
                failure_count=fc,
            )

        # Compute per-provider pace multipliers.
        # NOTE: pace_factor_multi can raise on a malformed window tuple. Wrap
        # each call so one bad provider's window can never abort the whole
        # failover (which would surface as a swallowed (None, None) upstream).
        pace_mults: dict[str, float] = {}
        if pace_windows:
            for name in self._provider_names:
                windows = pace_windows.get(name)
                if windows:
                    try:
                        pace_mults[name] = pace_factor_multi(windows)
                    except Exception:
                        pass  # skip this provider's pace, never break routing

        # Stash the computed multipliers so the production proxy can log the
        # ACTUAL values used in this failover decision (P3.4, Fix 2). Read via
        # the ``last_pace_mults`` property immediately after select_failover.
        self._last_pace_mults = dict(pace_mults)

        # Route the request. We prefer the HIGHEST quality tier that has a
        # viable provider, then relax downward (high → medium → low). This
        # guarantees we never return None when a healthy external provider
        # exists: when both z.ai keys AND ollama_cloud are dead (the 48h soak
        # scenario — ollama is rate-limited daily), the pay-per-token externals
        # (ppq/openrouter/deepinfra, registered as low tier) still take over at
        # the "low" step. Without this relaxation, the "high" tier gate filters
        # them out and select_failover wrongly returns (None, None).
        hour = 8 if peak else 12  # match shadow_hook convention
        result: dict = {}
        for _difficulty in ("high", "medium", "low"):
            result = optimizer.route(
                difficulty=_difficulty,
                estimated_tokens=10000,
                hour=hour,
                pace_mults=pace_mults,
            )
            if result.get("chosen_provider") not in (None, "fallback"):
                break  # found a viable provider at this tier

        chosen = result.get("chosen_provider")
        if chosen == "fallback":
            chosen = None

        # ── EU-R3: Log quota regime in the reason field ─────────────────
        # Augment the routing reason with the current quota regime so it
        # appears in the key_decisions / routing_live_decisions table when
        # the production proxy logs this decision.
        if "reason" in result and quota_regime != "included":
            result["reason"] = f"{result['reason']} (quota_regime={quota_regime})"

        # Find fallback: second viable provider from candidates
        fallback = None
        candidates = result.get("candidates", [])
        viable = [c for c in candidates if c.get("viable")]
        if len(viable) >= 2:
            fallback = viable[1].get("provider")

        # ── Model-aware return (P4.5c) ───────────────────────────────────
        # Resolve the model for each chosen provider via model_mapping.
        # When the provider is None (no viable route), the model is None too.
        chosen_model = get_model(chosen, task_type) if chosen is not None else None
        fallback_model = get_model(fallback, task_type) if fallback is not None else None

        return ((chosen, chosen_model), (fallback, fallback_model))

    @property
    def last_quota_regime(self) -> str:
        """Quota regime from the most recent failover decision (EUv2-4).

        Returns one of ``"included"``, ``"extra"``, or ``"exhausted"``.
        Defaults to ``"included"`` before any failover has been attempted.
        Read by the production proxy to log the regime alongside the
        routing decision in ``key_decisions`` / ``routing_live_decisions``.
        """
        try:
            return self._last_quota_regime
        except Exception:
            return "included"

    @property
    def last_quota_status(self) -> dict | None:
        """Full quota status dict from the most recent failover (EUv2-4).

        Returns the dict from ``ollama_quota_tracker.get_quota_status()``
        or None if no failover has been attempted or the tracker failed.
        Includes ``regime``, ``session_used_pct``, ``weekly_used_pct``,
        ``session_tokens``, ``weekly_tokens``.
        """
        try:
            return self._last_quota_status
        except Exception:
            return None

    @property
    def last_pace_mults(self) -> dict[str, float]:
        """Per-provider pace multipliers used by the most recent failover.

        Set inside ``_do_select_failover`` under ``self._lock``. Read by the
        production proxy immediately after ``select_failover`` to log the
        ACTUAL multipliers to ``routing_live_decisions`` (P3.4, Fix 2).
        Returns a copy so callers cannot mutate internal state.
        """
        try:
            return dict(self._last_pace_mults)
        except Exception:
            return {}

    # ── Introspection (for monitoring / tests) ────────────────────────────

    def get_kalman_state(self) -> dict[str, dict[str, Any]]:
        """Return current Kalman state for all providers. For monitoring."""
        try:
            with self._lock:
                state = {}
                for name in self._provider_names:
                    pk = self._price_kalmans[name]
                    ck = self._consumption_kalmans[name]
                    entry = {
                        "base_rate": pk.base_rate,
                        "burn_rate": ck.burn_rate,
                        "tokens_used": ck.tokens_used,
                        "updates": ck.update_count,
                    }
                    # ── Quality score (Phase 2.5.4) ───────────────────────
                    # Per-provider quality metrics from the telemetry table:
                    # success_rate, avg_latency_ms, token_mismatch_rate.
                    # Used for monitoring/debugging.  Wrapped so a CPVO/DB
                    # failure never empties the whole state report.
                    try:
                        qs = self._cpvo.get_quality_score(
                            name, 24.0, base_rate=self._base_rates.get(name),
                        )
                        entry["quality_score"] = {
                            "success_rate": qs.get("success_rate", 0.0),
                            "avg_latency_ms": qs.get("avg_latency_ms", 0.0),
                            "token_mismatch_rate": qs.get("token_mismatch_rate", 0.0),
                            "sample_count": qs.get("sample_count", 0),
                            "effective_rate": qs.get("effective_rate"),
                        }
                    except Exception:
                        entry["quality_score"] = None
                    state[name] = entry
                return state
        except Exception:
            return {}

    @property
    def provider_names(self) -> list[str]:
        """List of registered provider names."""
        return list(self._provider_names)

    # ── Pace window computation (Phase 2.4) ──────────────────────────────

    def compute_pace_windows(
        self,
        quota_cache: dict[str, tuple[list[dict], float]] | None,
    ) -> dict[str, list[tuple[float, float, float, float, float]]]:
        """Convert the proxy's quota_cache into pace_factor input tuples.

        Iterates over the proxy's ``quota_cache`` dict (mapping key names to
        ``(windows_list, timestamp)`` tuples), parses each window dict, and
        returns a dict of provider → list of tuples in the format expected by
        :func:`src.pricing_engine.pace_factor_multi`::

            (quota_used, quota_total, time_elapsed_pct, burn_rate,
             window_duration_hours)

        The burn_rate for each provider is pulled from that provider's
        :class:`~src.consumption_kalman.ConsumptionKalman` instance.

        Never raises. Malformed/missing entries are silently skipped.

        Args:
            quota_cache: The proxy's ``quota_cache`` dict, or None.

        Returns:
            Dict of provider → list of pace_factor tuples. Empty if no
            valid windows found or on any error.
        """
        try:
            if not quota_cache or not isinstance(quota_cache, dict):
                return {}

            now = time.time()
            result: dict[str, list[tuple[float, float, float, float, float]]] = {}

            with self._lock:
                for key_name, cache_entry in quota_cache.items():
                    # Only process providers we know about
                    if key_name not in self._consumption_kalmans:
                        continue

                    # Unpack (windows_list, timestamp)
                    if not isinstance(cache_entry, (tuple, list)) or len(cache_entry) < 1:
                        continue
                    windows = cache_entry[0]
                    if not isinstance(windows, list):
                        continue

                    # Get burn_rate from this provider's ConsumptionKalman
                    ck = self._consumption_kalmans[key_name]
                    burn_rate = ck.burn_rate

                    # Get quota_total for this provider
                    quota_total = _QUOTA_TOTALS.get(key_name, 2_000_000.0)

                    for window in windows:
                        if not isinstance(window, dict):
                            continue
                        tup = self._parse_pace_window(
                            window, quota_total, burn_rate, now
                        )
                        if tup is not None:
                            result.setdefault(key_name, []).append(tup)

            return result

        except Exception:
            return {}

    @staticmethod
    def _parse_pace_window(
        window: dict,
        quota_total: float,
        burn_rate: float,
        now: float,
    ) -> tuple[float, float, float, float, float] | None:
        """Parse a single window dict into a pace_factor tuple.

        Returns None if the window should be skipped (missing fields,
        unknown name, error sentinel, zero window_hours, resets_at==0).
        """
        # ── Required fields ───────────────────────────────────────────
        name = window.get("name")
        if name is None or name not in _KNOWN_WINDOW_NAMES:
            return None

        used_pct = window.get("used_pct")
        if used_pct is None:
            return None

        resets_at = window.get("resets_at")
        if resets_at is None or resets_at == 0:
            return None

        window_hours = window.get("window_hours")
        if window_hours is None or window_hours <= 0:
            return None

        # ── Skip error sentinel ──────────────────────────────────────
        try:
            used_pct = int(used_pct)
        except (TypeError, ValueError):
            return None

        if used_pct == _ERROR_SENTINEL_PCT:
            return None

        # ── Compute quota_used from percentage ────────────────────────
        clamped_pct = max(0, min(used_pct, 100))
        quota_used = quota_total * (clamped_pct / 100.0)

        # ── Compute time_elapsed_pct ──────────────────────────────────
        window_seconds = float(window_hours) * 3600.0
        window_start = float(resets_at) - window_seconds
        elapsed_raw = (now - window_start) / window_seconds
        elapsed_pct = max(0.0, min(elapsed_raw, 1.0))

        return (
            quota_used,
            quota_total,
            elapsed_pct,
            float(burn_rate),
            float(window_hours),
        )