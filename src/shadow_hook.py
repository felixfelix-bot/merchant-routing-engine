"""shadow_hook.py — Live shadow-mode integration for zai_proxy.

Bridges the production proxy (zai_proxy.py) to the merchant-routing-engine's
RoutingOptimizer + ShadowLogger. Runs in READ-ONLY mode: the optimizer's
decision is logged but never affects routing.

Design:
  - Persistent singleton. PriceKalman + ConsumptionKalman instances are kept
    across calls so they converge over time.
  - Each call: feed live quota/burn data → optimizer decides → ShadowLogger
    records both live and shadow decisions.
  - NEVER raises. All paths wrapped. Shadow failure cannot break production.

Usage in zai_proxy.py (in _proxy finally block):

    try:
        _shadow_hook.compare(
            live_provider=key_used,
            live_model=model,
            tokens=int(usage.get("total_tokens") or 0),
            quota_state=_snapshot_quota(),
            health_state=_snapshot_health(),
            peak=peak,
        )
    except Exception:
        pass  # shadow must never break production
"""
from __future__ import annotations

import os
import sys
import time
from typing import Any

# Add parent dir so `from src.xxx import` works when imported from outside
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from src.price_kalman import PriceKalman
from src.consumption_kalman import ConsumptionKalman
from src.routing_optimizer import RoutingOptimizer
from src.shadow_logger import ShadowLogger
from src.provider_names import normalize_provider_name
from src.pricing_engine import pace_factor_multi

__all__ = ["ShadowHook"]


# ── Seed costs ($/M) — reasonable starting points; Kalman converges ──────────
_SEED_COSTS = {
    "ours":          0.31,   # €155/mo, ~500M tokens/mo
    "friend":        0.375,  # 21% premium over ours
    "ollama_cloud":  0.50,   # $100/mo, ~200M tokens/mo
    "ppq":           0.14,   # avg of $0.09 input + $0.19 output
    "openrouter":    0.135,  # avg of $0.09 input + $0.18 output
    "deepinfra":     1.30,   # historical effective rate from daily_spend DB
}

# Quota totals (approximate, for scarcity factor)
_QUOTA_TOTALS = {
    "ours":         2_000_000,    # ~2M tokens per 5h window
    "friend":       2_000_000,
    "ollama_cloud": 1_000_000,    # rate-limited daily
    "ppq":          float("inf"),  # pay-per-token, no hard quota
    "openrouter":   float("inf"),
    "deepinfra":    float("inf"),  # pay-per-token, no hard quota
}

# z.ai peak hours (UTC) — Ollama/PPQ/OpenRouter/DeepInfra have no peak
_ZAI_PEAK = (6, 10)


class ShadowHook:
    """Persistent shadow-mode bridge. Singleton pattern.

    Maintains Kalman state across calls. Each compare() call:
    1. Updates Kalman filters with latest quota/burn data
    2. Rebuilds optimizer with current provider states
    3. Calls route() for the shadow decision
    4. Logs both live + shadow decisions to ShadowLogger
    """

    _instance: "ShadowHook | None" = None

    @classmethod
    def get_instance(cls, db_path: str | None = None) -> "ShadowHook":
        """Get or create the singleton instance."""
        if cls._instance is None:
            cls._instance = cls(db_path)
        return cls._instance

    def __init__(
        self,
        db_path: str | None = None,
        converged_rates: dict[str, float] | None = None,
    ):
        """Initialize Kalman filters and shadow logger.

        Args:
            db_path: SQLite path for ShadowLogger. Defaults to production usage DB.
            converged_rates: Converged base rates ($/M) from historical data.
                When provided, PriceKalman instances are seeded with these
                values instead of the static ``_SEED_COSTS`` defaults.  Keys
                not present in the dict fall back to ``_SEED_COSTS``.
        """
        if db_path is None:
            db_path = os.path.expanduser("~/.hermes/bot/zai_usage.db")

        self._logger = ShadowLogger(db_path)
        self._last_update = time.time()

        # Build the effective seed table: converged overrides take precedence
        # over the static _SEED_COSTS defaults.
        effective_seeds: dict[str, float] = dict(_SEED_COSTS)
        if converged_rates:
            for name, rate in converged_rates.items():
                effective_seeds[name] = rate

        # Per-provider Kalman instances (persist across calls)
        self._price_kalmans: dict[str, PriceKalman] = {}
        self._consumption_kalmans: dict[str, ConsumptionKalman] = {}
        self._burn_history: dict[str, list[float]] = {}  # for ConsumptionKalman.from_history

        for name in _SEED_COSTS:
            self._price_kalmans[name] = PriceKalman(
                initial_rate=effective_seeds[name],
                process_noise=1e-6,
                measurement_noise=1e-4,
            )
            self._consumption_kalmans[name] = ConsumptionKalman(
                process_noise=1.0,
                measurement_noise=1e6,
            )
            self._burn_history[name] = []

    def compare(
        self,
        live_provider: str | None,
        live_model: str | None,
        tokens: int,
        quota_state: dict[str, Any],
        health_state: dict[str, bool],
        peak: bool,
        failure_counts: dict[str, int] | None = None,
        pace_windows: dict[str, list[tuple[float, float, float, float, float]]] | None = None,
    ) -> None:
        """Run shadow comparison. NEVER raises.

        Args:
            live_provider: What the proxy actually chose (ours/friend/ollama_cloud/...).
            live_model: Model the proxy actually used.
            tokens: Total tokens for this request.
            quota_state: Dict of provider → {
                'used_pct': float (0-100),
                'remaining': float (tokens),
                'total': float (tokens),
            }
            health_state: Dict of provider → bool (True=healthy).
            peak: Whether current hour is z.ai peak.
            failure_counts: Optional dict of provider → failure_count for
                graduated health pricing. When None, falls back to the old
                boolean health_state (backward compat).
            pace_windows: Optional dict of provider → list of pace window
                tuples ``(quota_used, quota_total, time_elapsed_pct,
                burn_rate, window_duration_hours)``. When provided, the
                pace_factor is computed per-provider and passed to the
                optimizer. When None, pace_mult defaults to 1.0 (no
                adjustment).
        """
        try:
            self._do_compare(
                live_provider, live_model, tokens,
                quota_state, health_state, peak, failure_counts, pace_windows,
            )
        except Exception:
            pass  # shadow must never break production

    def _do_compare(
        self,
        live_provider: str | None,
        live_model: str | None,
        tokens: int,
        quota_state: dict[str, Any],
        health_state: dict[str, bool],
        peak: bool,
        failure_counts: dict[str, int] | None = None,
        pace_windows: dict[str, list[tuple[float, float, float, float, float]]] | None = None,
    ) -> None:
        """Internal compare — may raise (wrapped by compare())."""
        now = time.time()

        # Normalize the live provider name to canonical form.
        # The proxy may send legacy aliases like "zai_ours" or "manager".
        live_provider = normalize_provider_name(live_provider)

        # ── Update burn-rate Kalman with this request's tokens ───────────
        # Attribute tokens to the provider that actually served the request.
        if live_provider and live_provider in self._consumption_kalmans:
            self._consumption_kalmans[live_provider].update(float(tokens))
            self._burn_history[live_provider].append(float(tokens))
            # Keep last 100 observations for adaptive retraining
            if len(self._burn_history[live_provider]) > 100:
                self._burn_history[live_provider] = self._burn_history[live_provider][-100:]

        # ── Update price Kalman with current $/M estimate ────────────────
        # For flat-rate providers, $/M = monthly_fee / monthly_tokens.
        # We use the seed value initially; over time we can refine from
        # daily_spend data. For now, the seed is good enough for comparison.
        for name, pk in self._price_kalmans.items():
            # Only update if we have new data (skip for now — seeds are fine)
            pass

        # ── Build optimizer with current state ───────────────────────────
        optimizer = RoutingOptimizer(
            peak_hours_utc=_ZAI_PEAK,
            peak_mult=3.0,
            exhaustion_horizon=1,
        )

        for name in _SEED_COSTS:
            qs = quota_state.get(name, {})
            remaining = float(qs.get("remaining", _QUOTA_TOTALS.get(name, 1e9)))
            total = float(qs.get("total", _QUOTA_TOTALS.get(name, 1e9)))
            used_pct = float(qs.get("used_pct", 0.0))
            healthy = health_state.get(name, True)

            # Determine model tier
            if name in ("ours", "friend"):
                tier = "high"
                model = "glm-5.2"
                prov_peak = _ZAI_PEAK
                prov_peak_mult = 3.0
            elif name == "ollama_cloud":
                tier = "high"
                model = "glm-5.2"
                prov_peak = None  # no peak
                prov_peak_mult = 1.0
            else:
                tier = "low"
                model = "deepseek/deepseek-v4-flash"
                prov_peak = None
                prov_peak_mult = 1.0

            # Graduated health pricing: use failure_count when available,
            # otherwise infer a count from the boolean health_state.
            if failure_counts is not None:
                fc = int(failure_counts.get(name, 0))
            else:
                fc = 0 if healthy else 999  # large → breaker_tripped level

            optimizer.add_provider(
                name=name,
                price_kalman=self._price_kalmans[name],
                consumption_kalman=self._consumption_kalmans[name],
                quota_remaining=remaining,
                breaker_tripped=not healthy,
                model_tier=tier,
                model=model,
                quota_total=total if total != float("inf") else None,
                peak_hours_utc=prov_peak,
                peak_mult=prov_peak_mult,
                failure_count=fc,
            )

        # ── Get shadow decision ──────────────────────────────────────────
        # Map live model to difficulty tier
        difficulty = self._model_to_difficulty(live_model)
        # Pass actual UTC hour so per-provider peak_multiplier is computed
        # correctly — the optimizer determines peak per-provider via the
        # registered peak_hours_utc + peak_mult (ADR-003).
        # Use the peak flag to control which hour the optimizer sees, so shadow
        # decisions match the conditions the live proxy faced (not the real clock).
        hour = 8 if peak else 12

        # ── Compute per-provider pace multipliers ───────────────────────
        # Each provider may have multiple quota windows (5h + weekly for z.ai).
        # pace_factor_multi takes the worst-case (max) across windows.
        pace_mults: dict[str, float] = {}
        if pace_windows:
            for name in _SEED_COSTS:
                windows = pace_windows.get(name)
                if windows:
                    pace_mults[name] = pace_factor_multi(windows)

        result = optimizer.route(
            difficulty=difficulty,
            estimated_tokens=max(tokens, 1000),
            hour=hour,
            pace_mults=pace_mults,
        )

        shadow_provider = result.get("chosen_provider", "unknown")
        shadow_model = result.get("chosen_model", "unknown")
        shadow_cost = result.get("effective_cost_per_1m", 0.0)

        # Estimate live cost from the provider's current effective price
        live_cost = 0.0
        if live_provider and live_provider in self._price_kalmans:
            pk = self._price_kalmans[live_provider]
            from src.price_kalman import peak_multiplier as pm_fn
            if live_provider in ("ours", "friend"):
                pe = pm_fn(peak_hours_utc=_ZAI_PEAK, peak_mult=3.0) if peak else 1.0
            else:
                pe = 1.0
            live_cost = pk.effective_price(peak_mult=pe)

        # ── Log to shadow table ──────────────────────────────────────────
        reason = result.get("reason", "")
        self._logger.log_decision(
            ts=now,
            live_provider=live_provider or "none",
            live_model=live_model or "unknown",
            shadow_provider=shadow_provider,
            shadow_model=shadow_model,
            shadow_cost=float(shadow_cost) if shadow_cost != float("inf") else None,
            tokens=int(tokens),
            reason=reason,
            live_cost=float(live_cost) if live_cost != float("inf") else None,
        )

    @staticmethod
    def _model_to_difficulty(model: str | None) -> str:
        """Map model name to difficulty tier for the optimizer."""
        if not model:
            return "medium"
        m = model.lower()
        # Check specific sub-strings BEFORE broad version matches
        if "flash" in m:
            return "low"
        if "air" in m:
            return "low"
        if "5.2" in m or "4.5" in m or "pro" in m:
            return "high"
        return "medium"

    def get_stats(self) -> dict:
        """Return shadow mode statistics for monitoring."""
        try:
            count = self._logger.get_count()
            agreement = self._logger.get_agreement_rate()
            live_avg, shadow_avg = self._logger.get_cost_comparison()
            return {
                "total_decisions": count,
                "agreement_rate": round(agreement, 4),
                "avg_live_cost": round(live_avg, 6),
                "avg_shadow_cost": round(shadow_avg, 6),
            }
        except Exception:
            return {"error": "stats unavailable"}
