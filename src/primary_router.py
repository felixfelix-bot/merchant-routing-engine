"""primary_router.py — Phase 3: optimizer as PRIMARY routing decision.

Drop-in replacement for best_key(). Uses the same persistent Kalman state as
ShadowHook, but returns the routing decision instead of just logging it.

Design:
  - Maintains Kalman state across calls (same singleton as ShadowHook).
  - Each call: feed live quota/burn data → optimizer decides → return key name.
  - Maps optimizer's provider names to the proxy's key namespace.
  - NEVER raises. On any error, falls back to None (proxy handles gracefully).

Return contract (identical to best_key()):
  - "ours"       → use our z.ai key
  - "friend"     → use friend's z.ai key
  - None         → skip z.ai, go to ollama_cloud / external failover

The optimizer may choose "ollama_cloud" or "ppq" — these map to None because
the proxy's existing failover path will reach them naturally. The optimizer's
value is in the z.ai key selection (ours vs friend) and in signaling when to
skip z.ai entirely (go straight to ollama during peak).

Safety:
  - If the optimizer fails for ANY reason, primary_router returns None.
    The proxy's existing failover handles None correctly.
  - The optimizer never returns a dead/exhausted provider (filtered by
    health + exhaustion gates in _evaluate_provider).
  - Quality tier gate ensures the provider can serve the requested difficulty.
"""
from __future__ import annotations

import os
import sys
import time
from typing import Any

_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from src.price_kalman import PriceKalman, peak_multiplier
from src.consumption_kalman import ConsumptionKalman
from src.routing_optimizer import RoutingOptimizer
from src.provider_names import normalize_provider_name
from src.pricing_engine import pace_factor, pace_factor_multi
from src.quota_window_extractor import extract_quota_windows

__all__ = ["PrimaryRouter"]


# Seed costs ($/M) — same as ShadowHook, kept in sync
_SEED_COSTS = {
    "ours":          0.31,
    "friend":        0.375,
    "ollama_cloud":  0.024,
    "ppq":           0.14,
    "openrouter":    0.135,
    "deepinfra":     1.30,   # historical effective rate from daily_spend DB
}

_QUOTA_TOTALS = {
    "ours":         2_000_000,
    "friend":       2_000_000,
    "ollama_cloud": 500_000_000,
    "ppq":          float("inf"),
    "openrouter":   float("inf"),
    "deepinfra":    float("inf"),
}

_ZAI_PEAK = (6, 10)

# Providers that are z.ai keys (returnable as string)
_ZAI_KEYS = {"ours", "friend"}


class PrimaryRouter:
    """Phase 3 primary router. Uses optimizer to select the best provider.

    Singleton — maintains Kalman state across calls. Thread-safe (each call
    builds a fresh optimizer from immutable Kalman instances; the Kalman
    instances themselves are not modified during route()).
    """

    _instance: "PrimaryRouter | None" = None

    @classmethod
    def get_instance(cls) -> "PrimaryRouter":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self._price_kalmans: dict[str, PriceKalman] = {}
        self._consumption_kalmans: dict[str, ConsumptionKalman] = {}
        self._call_count = 0
        self._last_decision = "init"

        # ── Load converged rates from historical daily_spend data ───────
        # Falls back to static _SEED_COSTS if DB is unavailable or empty.
        converged_rates = self._load_converged_rates()

        for name in _SEED_COSTS:
            # Use converged rate if available, otherwise fall back to seed
            initial = converged_rates.get(name, _SEED_COSTS[name])
            self._price_kalmans[name] = PriceKalman(
                initial_rate=initial,
                process_noise=1e-6,
                measurement_noise=1e-4,
            )
            self._consumption_kalmans[name] = ConsumptionKalman(
                process_noise=1.0,
                measurement_noise=1e6,
            )

    @staticmethod
    def _load_converged_rates() -> dict[str, float]:
        """Load converged base rates from historical daily_spend data.

        Calls the feed_historical_costs loader to read zai_usage.db,
        compute effective $/M per provider per day, and feed the
        observations to PriceKalman instances. Returns converged rates.

        Falls back to empty dict (→ static seeds) on any error.
        """
        try:
            from scripts.feed_historical_costs import load_historical_rates
            return load_historical_rates(seed_costs=_SEED_COSTS)
        except Exception:
            return {}

    def route(
        self,
        model: str | None = None,
        tokens: int = 0,
        quota_state: dict[str, Any] | None = None,
        health_state: dict[str, bool] | None = None,
        difficulty: str | None = None,
        failure_counts: dict[str, int] | None = None,
        pace_windows: dict[str, list[tuple[float, float, float, float, float]]] | None = None,
    ) -> str | None:
        """Select the best provider. Drop-in for best_key().

        Returns:
            "ours" or "friend" for z.ai keys.
            None to skip z.ai (ollama/external failover handles it).

        NEVER raises. Falls back to None on any error.

        Args:
            failure_counts: Optional dict of provider → failure_count.
                When provided, enables graduated health pricing. When None,
                falls back to the old boolean health_state (backward compat).
            pace_windows: Optional dict of provider → list of pace window
                tuples ``(quota_used, quota_total, time_elapsed_pct,
                burn_rate, window_duration_hours)``. When provided, the
                pace_factor is computed per-provider and passed to the
                optimizer. When None, pace_windows are automatically
                built from quota_state + ConsumptionKalman burn rates
                via :meth:`_build_pace_windows` (ADR-008).
        """
        try:
            return self._do_route(model, tokens, quota_state, health_state, difficulty,
                                  failure_counts, pace_windows)
        except Exception:
            self._last_decision = "error_fallback_none"
            return None

    def _do_route(
        self,
        model: str | None,
        tokens: int,
        quota_state: dict[str, Any] | None,
        health_state: dict[str, bool] | None,
        difficulty: str | None,
        failure_counts: dict[str, int] | None = None,
        pace_windows: dict[str, list[tuple[float, float, float, float, float]]] | None = None,
    ) -> str | None:
        self._call_count += 1

        if not quota_state:
            quota_state = {}
        if not health_state:
            health_state = {}
        if not failure_counts:
            failure_counts = {}
        if not pace_windows:
            pace_windows = self._build_pace_windows(quota_state)

        if not pace_windows:
            pace_windows = {}

        optimizer = RoutingOptimizer(
            peak_hours_utc=_ZAI_PEAK,
            peak_mult=3.0,
            exhaustion_horizon=1,
        )

        diff = difficulty or self._model_to_difficulty(model)

        for name in _SEED_COSTS:
            qs = quota_state.get(name, {})
            remaining = float(qs.get("remaining", _QUOTA_TOTALS.get(name, 1e9)))
            total = float(qs.get("total", _QUOTA_TOTALS.get(name, 1e9)))
            healthy = health_state.get(name, True)
            fc = failure_counts.get(name, 0)

            # breaker_tripped when explicitly unhealthy OR failure_count > 10
            breaker = (not healthy) or (fc > 10)

            if name in _ZAI_KEYS:
                tier = "high"
                mdl = "glm-5.2"
                prov_peak = _ZAI_PEAK
                prov_peak_mult = 3.0
            elif name == "ollama_cloud":
                tier = "high"
                mdl = "glm-5.2"
                prov_peak = None
                prov_peak_mult = 1.0
            else:
                tier = "low"
                mdl = "deepseek/deepseek-v4-flash"
                prov_peak = None
                prov_peak_mult = 1.0

            optimizer.add_provider(
                name=name,
                price_kalman=self._price_kalmans[name],
                consumption_kalman=self._consumption_kalmans[name],
                quota_remaining=remaining,
                breaker_tripped=breaker,
                model_tier=tier,
                model=mdl,
                quota_total=total if total != float("inf") else None,
                peak_hours_utc=prov_peak,
                peak_mult=prov_peak_mult,
                failure_count=fc,
            )

        # ── Compute per-provider pace multipliers ───────────────────────
        # Each provider may have multiple quota windows (5h + weekly for z.ai).
        # pace_factor_multi takes the worst-case (max) across windows.
        # When no windows are supplied, pace_mult defaults to 1.0 (no change).
        pace_mults: dict[str, float] = {}
        for name in _SEED_COSTS:
            windows = pace_windows.get(name)
            if windows:
                pace_mults[name] = pace_factor_multi(windows)

        result = optimizer.route(
            difficulty=diff,
            estimated_tokens=max(tokens, 1000),
            hour=int(time.gmtime().tm_hour),
            pace_mults=pace_mults,
        )

        chosen = result.get("chosen_provider", "fallback")
        self._last_decision = f"optimizer→{chosen}"

        # Map optimizer's choice to proxy's key namespace
        if chosen in _ZAI_KEYS:
            return chosen
        # ollama_cloud / ppq / openrouter / deepinfra / fallback → None
        # Proxy's failover path will reach the correct provider
        return None

    def _build_pace_windows(
        self,
        quota_state: dict[str, Any],
    ) -> dict[str, list[tuple[float, float, float, float, float]]]:
        """Build pace_windows from quota_state + ConsumptionKalman burn rates.

        When the caller does not supply explicit ``pace_windows``, this method
        automatically constructs them by:

        1. Extracting burn rates from the router's own ``ConsumptionKalman``
           instances (``self._consumption_kalmans[name].burn_rate``).
        2. Converting the proxy's ``quota_state`` format to the
           ``quota_cache`` format expected by ``extract_quota_windows``.
        3. Calling ``extract_quota_windows`` per provider to build the
           pace_factor input tuples.

        Args:
            quota_state: Dict of provider → {used_pct, remaining, total}.
                This is the format produced by the proxy's
                ``_snapshot_quota()`` function.

        Returns:
            Dict mapping provider name → list of
            ``(quota_used, quota_total, time_elapsed_pct, burn_rate,
              window_duration_hours)`` tuples. May be empty if no valid
            windows could be extracted.
        """
        if not quota_state:
            return {}

        if not isinstance(quota_state, dict):
            return {}

        result: dict[str, list[tuple[float, float, float, float, float]]] = {}

        for name, ck in self._consumption_kalmans.items():
            qs = quota_state.get(name, {})
            if not qs:
                continue
            if not isinstance(qs, dict):
                continue

            # Only providers with finite quota totals have meaningful windows
            total = qs.get("total", _QUOTA_TOTALS.get(name, float("inf")))
            if total == float("inf"):
                continue

            used_pct = qs.get("used_pct")
            if used_pct is None:
                continue

            # Safely convert used_pct to int — skip on garbage
            try:
                used_pct = int(used_pct)
            except (TypeError, ValueError):
                continue

            # Convert quota_state entry to the quota_cache format expected
            # by extract_quota_windows:
            #   quota_cache[key] = (windows_list, timestamp)
            # Each window dict needs: name, used_pct, resets_at, window_hours.
            #
            # The proxy's quota_state provides used_pct and total, but not
            # resets_at or window_hours. We synthesize a 5-hour window using
            # the current time as the window midpoint, which gives a
            # reasonable elapsed_pct estimate. The primary signal driving
            # pace_factor is used_pct + burn_rate, not the exact time
            # elapsed, so a rough elapsed estimate is acceptable.
            now = time.time()
            window_hours = 5
            # Assume the window started 2.5h ago (midpoint) → elapsed ≈ 0.5
            # This is a conservative default; the real proxy passes actual
            # resets_at values. When the proxy provides resets_at directly,
            # we use it.
            resets_at = qs.get("resets_at")
            if resets_at is None:
                # Synthetic window: assume 50% elapsed
                resets_at = int(now + window_hours * 3600 * 0.5)

            windows_list = [{
                "name": "5-hour",
                "type": "TOKENS_LIMIT",
                "used_pct": int(used_pct),
                "resets_at": int(resets_at),
                "window_hours": window_hours,
            }]

            # Also add a weekly window if weekly info is present
            weekly_used_pct = qs.get("weekly_used_pct")
            if weekly_used_pct is not None:
                weekly_hours = 168
                weekly_resets_at = qs.get("weekly_resets_at")
                if weekly_resets_at is None:
                    weekly_resets_at = int(now + weekly_hours * 3600 * 0.5)
                windows_list.append({
                    "name": "weekly",
                    "type": "TOKENS_LIMIT",
                    "used_pct": int(weekly_used_pct),
                    "resets_at": int(weekly_resets_at),
                    "window_hours": weekly_hours,
                })

            quota_cache_entry = (windows_list, now)
            burn_rate = ck.burn_rate

            tuples = extract_quota_windows(
                quota_cache={name: quota_cache_entry},
                burn_rate=burn_rate,
                quota_total=float(total),
            )

            if tuples:
                result[name] = tuples

        return result

    @staticmethod
    def _model_to_difficulty(model: str | None) -> str:
        """Map model name to difficulty tier."""
        if not model:
            return "medium"
        m = model.lower()
        if "flash" in m:
            return "low"
        if "air" in m:
            return "low"
        if "5.2" in m or "4.5" in m or "pro" in m:
            return "high"
        return "medium"

    def update_burn_rate(self, provider: str, tokens: int) -> None:
        """Feed actual token usage to the consumption Kalman.

        Called after a request completes to keep burn-rate predictions current.
        The provider name is normalized to canonical form before lookup.
        """
        try:
            provider = normalize_provider_name(provider)
            if provider in self._consumption_kalmans and tokens > 0:
                self._consumption_kalmans[provider].update(float(tokens))
        except Exception:
            pass

    @property
    def stats(self) -> dict:
        """Return routing statistics for monitoring."""
        return {
            "call_count": self._call_count,
            "last_decision": self._last_decision,
            "burn_rates": {
                name: round(ck.tokens_used, 0)
                for name, ck in self._consumption_kalmans.items()
            },
        }
