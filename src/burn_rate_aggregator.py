"""burn_rate_aggregator.py — 5-minute windowed burn-rate aggregation.

Implements the ADR-008 observation-frequency contract for the
ConsumptionKalman:

    every 5 minutes:
        hourly_rate = tokens_in_last_5min × 12
        consumption_kalman.update(hourly_rate)

Instead of feeding per-request token counts to the Kalman (which are
extremely noisy — one request = 500 tokens, next = 50,000), this module
accumulates observations in a sliding time window and feeds the aggregated
hourly rate at a fixed interval. This reduces measurement noise dramatically
and lets the Kalman converge within 6-12 observations (30-60 minutes) instead
of thousands of per-request updates.

Design:
  - Thread-safe via a ``threading.Lock`` (the proxy handles concurrent
    requests, so ``record()`` is called from multiple threads).
  - Can be driven from a cron job (call ``maybe_feed()`` every minute) or
    from the proxy on each request (the proxy calls ``record()`` then
    ``maybe_feed()`` — the latter is a no-op if the window hasn't elapsed).
  - Observations are pruned on both ``record()`` and ``maybe_feed()`` to
    prevent unbounded memory growth.

Only the ConsumptionKalman uses 5-minute aggregation. The PriceKalman
receives observations on success/failure events (see ``cost_observer.py``).
"""
from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from src.consumption_kalman import ConsumptionKalman

__all__ = ["BurnRateAggregator"]


class BurnRateAggregator:
    """Accumulate token counts per provider and feed hourly rates to Kalman.

    Parameters
    ----------
    window_minutes:
        Length of the aggregation window in minutes (default 5).
    time_fn:
        Callable returning the current time as ``float`` seconds. Defaults
        to ``time.monotonic``. Injected for testing.
    """

    def __init__(
        self,
        window_minutes: int = 5,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        self._window: float = window_minutes * 60.0
        self._hourly_multiplier: float = 60.0 / window_minutes
        self._time_fn = time_fn or time.monotonic
        # provider → list of (timestamp, token_count)
        self._observations: dict[str, list[tuple[float, int]]] = {}
        # provider → last feed timestamp (monotonic)
        self._last_feed: dict[str, float] = {}
        self._lock = threading.Lock()

    # ── Recording ───────────────────────────────────────────────────────────

    def record(self, provider: str, tokens: int) -> None:
        """Record tokens consumed for a request on *provider*.

        Called on every request. Thread-safe.
        """
        if tokens < 0:
            raise ValueError(f"tokens must be non-negative, got {tokens}")

        now = self._time_fn()
        with self._lock:
            if provider not in self._observations:
                self._observations[provider] = []
            self._observations[provider].append((now, tokens))
            self._prune_locked(provider, now)

    # ── Feeding ──────────────────────────────────────────────────────────────

    def maybe_feed(
        self,
        consumption_kalmans: dict[str, "ConsumptionKalman"],
        force: bool = False,
    ) -> dict[str, float]:
        """If the window has elapsed since the last feed, compute the hourly
        rate and feed it to the corresponding ConsumptionKalman.

        Parameters
        ----------
        consumption_kalmans:
            Mapping of provider name → ConsumptionKalman instance.
        force:
            If True, bypass the window-elapsed check and feed immediately.

        Returns
        -------
        dict
            ``{provider: hourly_rate}`` for each provider that was fed.
            Empty if no provider was ready (and ``force=False``).
        """
        now = self._time_fn()
        fed: dict[str, float] = {}

        with self._lock:
            for provider, kalman in consumption_kalmans.items():
                obs = self._observations.get(provider, [])
                self._prune_locked(provider, now, obs)

                last = self._last_feed.get(provider)

                # On the very first feed, we need at least one observation
                # and the window must have elapsed since that first observation.
                if last is not None:
                    elapsed = now - last
                elif obs:
                    elapsed = now - obs[0][0]
                else:
                    elapsed = 0.0

                if not force and elapsed < self._window:
                    continue

                # Skip providers that have never been fed and have no data.
                if last is None and len(obs) == 0:
                    continue

                # Compute hourly rate from observations in the window.
                tokens_in_window = sum(tok for _, tok in obs)
                hourly_rate = tokens_in_window * self._hourly_multiplier

                # Feed the Kalman. A provider that was previously fed but
                # now has zero traffic gets hourly_rate=0 — this is valid
                # and lets the Kalman track the drop in consumption.
                kalman.update(hourly_rate)
                self._last_feed[provider] = now
                fed[provider] = hourly_rate

        return fed

    # ── Introspection ───────────────────────────────────────────────────────

    def observations(self, provider: str) -> list[tuple[float, int]]:
        """Return a *copy* of the current observations for *provider*."""
        with self._lock:
            return list(self._observations.get(provider, []))

    def pending_tokens(self, provider: str) -> int:
        """Total tokens accumulated for *provider* in the current window."""
        with self._lock:
            now = self._time_fn()
            window_start = now - self._window
            obs = self._observations.get(provider, [])
            return sum(tok for ts, tok in obs if ts >= window_start)

    # ── Internal ────────────────────────────────────────────────────────────

    def _prune_locked(
        self,
        provider: str,
        now: float,
        obs: list[tuple[float, int]] | None = None,
    ) -> None:
        """Remove observations older than the window. Must hold ``self._lock``.

        If *obs* is provided, prunes in-place on that list (avoids dict lookup).
        """
        if obs is None:
            obs = self._observations.get(provider)
            if obs is None:
                return

        window_start = now - self._window
        # Observations are appended in chronological order (time advances
        # monotonically), so we can drop from the front.
        while obs and obs[0][0] < window_start:
            obs.pop(0)