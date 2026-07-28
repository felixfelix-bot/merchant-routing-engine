"""cpvo_calculator.py — Cost Per Valid Output calculator (Phase 2.5.2).

CPVO (Cost Per Valid Output) is the quality-aware cost metric that makes the
routing optimizer penalise providers with low success rates instead of just
picking the cheapest sticker price.

Definition
----------
::

    CPVO = SUM(cost) / SUM(success)

where:
  - **cost** = ``billed_tokens * base_rate`` per request (total dollars spent,
    including wasted spend on requests that failed)
  - **success** = ``COUNT(response_valid = True)`` (the denominator — NOT total
    request count)

This is the critical correctness invariant: the denominator is **success_count**,
not **total_count**.  Dividing by total would understate the cost of failures and
defeat the entire purpose of quality-aware pricing.

Effective Rate
--------------
The *effective rate* is the quality-adjusted $/M that the optimizer actually
uses for routing decisions::

    effective_rate = base_rate / success_rate    (when success_rate < 0.95)
    effective_rate = base_rate                   (when success_rate >= 0.95 or
                                                  insufficient data)

Example (from docs/PLAN-live-kalman-routing.md §2.5.2):
  - Provider A: $0.001/M, 92% success → $0.001/0.92 = $0.00109/M
  - Provider B: $0.029/M, 99.9% success → $0.029/M (no penalty)

If A drops to 80% success → $0.00125/M.  The optimizer now sees A as more
expensive and routes accordingly.

Consumers
---------
  - ``LiveRouter.select_failover()`` — calls ``get_effective_rates()`` to
    adjust base rates before optimization.
  - Calibration cron — feeds quality-adjusted rates to PriceKalman.

Safety
------
EVERY public method wraps in try/except and NEVER raises.  A telemetry or DB
failure must not break routing.  On error, methods return the unadjusted base
rate (as if the provider had no quality penalty).
"""
from __future__ import annotations

import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

__all__ = ["CPVOCalculator"]

# ── Constants ───────────────────────────────────────────────────────────────

#: Minimum sample count for a statistically meaningful CPVO.
#: Below this, we don't have enough data to judge quality — return base rate.
MIN_SAMPLES = 100

#: Success rate at or above which no quality penalty is applied.
#: Below this, the effective rate is inflated by 1/success_rate.
SUCCESS_THRESHOLD = 0.95

#: Epsilon for success_rate when computing 1/success_rate, to avoid
#: division by zero when a provider has 0 successes.  With 0% success,
#: effective = base_rate / 1e-6 = base_rate * 1_000_000 — a very large
#: number that makes the optimizer avoid this provider entirely.
_SUCCESS_EPSILON = 1e-6


class CPVOCalculator:
    """Cost-Per-Valid-Output calculator.

    Queries the ``provider_telemetry`` table (populated by Phase 2.5.1) to
    compute quality-adjusted effective rates per provider.

    Thread-safe by design: each public method opens its own short-lived
    SQLite connection (read-only queries), so concurrent calls from
    ``ThreadingHTTPServer`` handler threads don't interfere.

    Args:
        db_path: Path to the SQLite database containing the
            ``provider_telemetry`` table (typically
            ``~/.hermes/bot/zai_usage.db``).
    """

    def __init__(self, db_path: str | None):
        self.db_path = db_path

    # ── Internal: telemetry query ──────────────────────────────────────

    def _query_aggregates(
        self, provider: str, window_hours: float = 24.0
    ) -> dict[str, Any]:
        """Query aggregated telemetry for a provider in the time window.

        Returns a dict with::

            total_count          — total rows in window
            success_count        — COUNT(response_valid = 1)
            total_billed_tokens  — SUM(billed_tokens)
            total_latency_ms     — SUM(latency_ms)
            mismatch_count       — COUNT(token_mismatch = 1)

        Returns an empty dict (all zeros) on any error or if the table
        doesn't exist.  NEVER raises.
        """
        empty = {
            "total_count": 0,
            "success_count": 0,
            "total_billed_tokens": 0,
            "total_latency_ms": 0,
            "mismatch_count": 0,
        }
        if not self.db_path or not os.path.exists(self.db_path):
            return empty
        try:
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=window_hours)
            ).isoformat()
            conn = sqlite3.connect(
                self.db_path, timeout=5, isolation_level=None
            )
            try:
                row = conn.execute(
                    "SELECT"
                    "  COUNT(*) AS total_count,"
                    "  SUM(CASE WHEN response_valid = 1 THEN 1 ELSE 0 END)"
                    "    AS success_count,"
                    "  COALESCE(SUM(billed_tokens), 0) AS total_billed_tokens,"
                    "  COALESCE(SUM(latency_ms), 0) AS total_latency_ms,"
                    "  SUM(CASE WHEN token_mismatch = 1 THEN 1 ELSE 0 END)"
                    "    AS mismatch_count "
                    "FROM provider_telemetry "
                    "WHERE provider = ? AND ts >= ?",
                    (provider, cutoff),
                ).fetchone()
            finally:
                conn.close()

            if row is None:
                return empty
            return {
                "total_count": int(row[0] or 0),
                "success_count": int(row[1] or 0),
                "total_billed_tokens": int(row[2] or 0),
                "total_latency_ms": int(row[3] or 0),
                "mismatch_count": int(row[4] or 0),
            }
        except Exception:
            return empty

    # ── Public API ──────────────────────────────────────────────────────

    def compute_cpvo(
        self,
        provider: str,
        window_hours: float = 24.0,
        base_rate: float | None = None,
    ) -> float | None:
        """Compute Cost-Per-Valid-Output for a provider.

        CPVO = SUM(billed_tokens * base_rate) / COUNT(success)

        - If ``base_rate`` is given (in $/M), the result is the average dollar
          cost per successful request: ``total_billed_tokens / 1e6 * base_rate
          / success_count``.
        - If ``base_rate`` is ``None``, the result is billed-tokens-per-success
          (a rate-free quality proxy): ``total_billed_tokens / success_count``.

        Edge cases:
          - **Insufficient data** (< ``MIN_SAMPLES`` total requests): returns
            ``base_rate`` if provided, else ``None``.
          - **Zero successes** (all requests failed): returns ``float('inf')``
            — cost per valid output is unbounded.

        Args:
            provider: Provider name (matches the ``provider`` column in
                ``provider_telemetry``).
            window_hours: Look-back window in hours (default 24).
            base_rate: Optional base $/M rate for dollar-denominated CPVO.

        Returns:
            CPVO value, ``base_rate`` (insufficient data), ``None`` (no base_rate
            + insufficient data), or ``float('inf')`` (zero successes).
        """
        try:
            agg = self._query_aggregates(provider, window_hours)
            total_count = agg["total_count"]

            # Insufficient data — can't judge quality
            if total_count < MIN_SAMPLES:
                return base_rate  # None if not provided

            success_count = agg["success_count"]

            # Zero successes → cost is unbounded
            if success_count == 0:
                return float("inf")

            total_billed = float(agg["total_billed_tokens"])

            if base_rate is not None:
                # total_cost = tokens → millions → * $/M = total $
                total_cost = total_billed / 1_000_000.0 * float(base_rate)
            else:
                # Without a base rate, use raw token count as cost proxy
                total_cost = total_billed

            # CPVO = total_cost / success_count  (NOT / total_count!)
            return total_cost / success_count
        except Exception:
            return base_rate

    def get_effective_rates(self, base_rates: dict[str, float]) -> dict[str, float]:
        """Adjust base rates with quality penalty.

        For each provider in ``base_rates``:
          - Query CPVO for the last 24 hours.
          - If success_rate < ``SUCCESS_THRESHOLD`` (0.95):
            ``effective = base_rate / success_rate``
          - If success_rate >= 0.95: ``effective = base_rate`` (no penalty)
          - If insufficient data (< 100 samples): ``effective = base_rate``

        This is the method that makes the routing optimizer quality-aware:
        a provider with 80% success sees its effective rate inflated by 25%,
        making it less attractive than a pricier but more reliable provider.

        Args:
            base_rates: ``{provider_name: base_$/M_rate}``.

        Returns:
            ``{provider_name: effective_$/M_rate}`` — same keys as input,
            with low-success providers penalised.  NEVER raises; on error,
            returns the input ``base_rates`` unchanged.
        """
        result: dict[str, float] = {}
        for provider, base_rate in base_rates.items():
            try:
                agg = self._query_aggregates(provider, 24.0)
                total_count = agg["total_count"]

                if total_count < MIN_SAMPLES:
                    # Not enough data to judge — trust the base rate
                    result[provider] = base_rate
                    continue

                success_count = agg["success_count"]
                if success_count == 0:
                    # 0% success → massive penalty (avoid this provider)
                    result[provider] = float(base_rate) / _SUCCESS_EPSILON
                    continue

                success_rate = success_count / total_count
                if success_rate < SUCCESS_THRESHOLD:
                    # Quality penalty: inflate rate by 1/success_rate
                    result[provider] = float(base_rate) / success_rate
                else:
                    # Reliable enough — no penalty
                    result[provider] = base_rate
            except Exception:
                result[provider] = base_rate
        return result

    def get_quality_score(
        self,
        provider: str,
        window_hours: float = 24.0,
        base_rate: float | None = None,
    ) -> dict[str, Any]:
        """Return quality metrics for a provider.

        Args:
            provider: Provider name.
            window_hours: Look-back window in hours (default 24).
            base_rate: Optional base $/M rate.  When provided,
                ``effective_rate`` is the quality-adjusted $/M.  When
                ``None``, ``effective_rate`` is a penalty multiplier
                (1.0 = no change, 1.25 = +25%).

        Returns:
            Dict with::

                success_rate         — fraction of valid responses
                avg_latency_ms       — mean latency in milliseconds
                token_mismatch_rate  — fraction of rows with billing mismatch
                sample_count         — total requests in window
                cpvo                 — Cost Per Valid Output
                effective_rate       — quality-adjusted rate or multiplier
        """
        empty = {
            "success_rate": 0.0,
            "avg_latency_ms": 0.0,
            "token_mismatch_rate": 0.0,
            "sample_count": 0,
            "cpvo": None,
            "effective_rate": base_rate if base_rate is not None else 1.0,
        }
        try:
            agg = self._query_aggregates(provider, window_hours)
            total_count = agg["total_count"]

            if total_count == 0:
                return empty

            success_count = agg["success_count"]
            success_rate = success_count / total_count
            avg_latency = agg["total_latency_ms"] / total_count
            mismatch_rate = agg["mismatch_count"] / total_count
            cpvo = self.compute_cpvo(provider, window_hours, base_rate)

            # Effective rate
            if total_count < MIN_SAMPLES:
                # Insufficient data — no adjustment
                effective = base_rate if base_rate is not None else 1.0
            elif success_count == 0:
                effective = (
                    float(base_rate) / _SUCCESS_EPSILON
                    if base_rate is not None
                    else 1.0 / _SUCCESS_EPSILON
                )
            elif success_rate < SUCCESS_THRESHOLD:
                if base_rate is not None:
                    effective = float(base_rate) / success_rate
                else:
                    effective = 1.0 / success_rate  # penalty multiplier
            else:
                effective = base_rate if base_rate is not None else 1.0

            return {
                "success_rate": success_rate,
                "avg_latency_ms": avg_latency,
                "token_mismatch_rate": mismatch_rate,
                "sample_count": total_count,
                "cpvo": cpvo,
                "effective_rate": effective,
            }
        except Exception:
            return empty
