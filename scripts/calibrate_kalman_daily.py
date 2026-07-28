"""calibrate_kalman_daily.py — Daily PriceKalman calibration cron job.

Queries the ``daily_spend`` table for yesterday's effective $/M rates per
provider, feeds them to per-provider :class:`~src.price_kalman.PriceKalman`
instances via ``.update()``, and logs the convergence state to the
``kalman_samples`` table for auditability.

This is the Phase 2.2 self-correction mechanism: the PriceKalman filters
receive one observation per day (per provider) so their base rates drift
toward the real amortized cost over time.  Seeds from
:mod:`scripts.feed_historical_costs` give instant convergence on cold start;
this cron keeps the filters accurate as pricing changes.

Usage (standalone)::

    python3 scripts/calibrate_kalman_daily.py
    python3 scripts/calibrate_kalman_daily.py --db /path/to/zai_usage.db
    python3 scripts/calibrate_kalman_daily.py --days-back 7   # catch up after downtime

Importable (for testing or chaining)::

    from scripts.calibrate_kalman_daily import calibrate_daily
    results = calibrate_daily(db_path, days_back=1)
    # results = {"ours": {"base_rate": 0.31, "velocity": 0.0, "update_count": 5}, ...}

Cron setup (daily at 02:00 UTC, after midnight spend rollover)::

    0 2 * * * cd ~/merchant-routing-engine && python3 scripts/calibrate_kalman_daily.py >> /tmp/kalman_calibration.log 2>&1
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Any

# ── Path bootstrap so `from src.price_kalman` works standalone ──────────────
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from src.price_kalman import PriceKalman
from src.provider_names import normalize_provider_name

__all__ = [
    "SEED_COSTS",
    "calibrate_daily",
    "query_yesterday_rates",
    "main",
]

# ── Seed costs (mirrors primary_router._SEED_COSTS + live_router defaults) ────
# Used to initialize PriceKalman for providers with no historical data.
SEED_COSTS: dict[str, float] = {
    "ours":          0.001,
    "friend":        0.028983,
    "ollama_cloud":  0.023952,
    "ppq":           0.14,
    "openrouter":    0.135,
    "deepinfra":     1.30,
}

# ── Tiers that should not be attributed to any provider ─────────────────────
_SKIP_TIERS: frozenset[str] = frozenset({"unknown", ""})


def _tier_to_provider(tier: str | None) -> str | None:
    """Map a daily_spend tier to a canonical provider name.

    Returns None for tiers that should be skipped (unknown, empty).
    """
    if tier is None or tier in _SKIP_TIERS:
        return None
    return normalize_provider_name(tier)


# ── DB schema ─────────────────────────────────────────────────────────────────

_KALMAN_SAMPLES_DDL = """\
CREATE TABLE IF NOT EXISTS kalman_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    provider TEXT NOT NULL,
    base_rate REAL NOT NULL,
    velocity REAL DEFAULT 0,
    update_count INTEGER DEFAULT 0,
    source TEXT DEFAULT 'daily_calibration'
)
"""


def _ensure_kalman_samples_table(conn: sqlite3.Connection) -> None:
    """Create the kalman_samples table if it doesn't exist."""
    try:
        conn.execute(_KALMAN_SAMPLES_DDL)
        conn.commit()
    except Exception:
        pass


# ── Query daily_spend ──────────────────────────────────────────────────────────


def query_yesterday_rates(
    db_path: str,
    days_back: int = 1,
) -> dict[str, dict[str, float]]:
    """Query daily_spend for the last ``days_back`` days and compute effective rates.

    For each provider found, returns::

        {provider: {"effective_rate": float, "spend_usd": float, "token_count": int, "date": str}}

    The ``effective_rate`` is the blended $/M across all qualifying days:
    total_spend / (total_tokens / 1e6).

    Rows with ``token_count == 0`` are skipped (no observable rate).
    Tiers mapping to ``None`` (unknown) are skipped.

    Args:
        db_path: Path to the zai_usage.db SQLite file.
        days_back: Number of days to look back (1 = yesterday only).

    Returns:
        Dict of provider → rate info. Empty if DB missing or no data.
    """
    if not os.path.exists(db_path):
        return {}

    try:
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            today = datetime.now(timezone.utc).date()
            start_date = (today - timedelta(days=days_back)).isoformat()
            end_date = (today - timedelta(days=1)).isoformat()

            rows = conn.execute(
                "SELECT date, tier, spend_usd, call_count, token_count "
                "FROM daily_spend "
                "WHERE date >= ? AND date <= ? "
                "ORDER BY date, tier",
                (start_date, end_date),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return {}

    # Aggregate per provider across all days in the window
    per_provider: dict[str, dict[str, Any]] = {}
    for date, tier, spend_usd, call_count, token_count in rows:
        provider = _tier_to_provider(tier)
        if provider is None:
            continue
        if token_count is None or token_count <= 0:
            continue

        entry = per_provider.setdefault(provider, {
            "total_spend": 0.0,
            "total_tokens": 0,
            "total_calls": 0,
            "date": date,
        })
        entry["total_spend"] += float(spend_usd or 0.0)
        entry["total_tokens"] += int(token_count or 0)
        entry["total_calls"] += int(call_count or 0)

    # Compute effective rate per provider
    result: dict[str, dict[str, float]] = {}
    for provider, agg in per_provider.items():
        if agg["total_tokens"] <= 0:
            continue
        effective_rate = agg["total_spend"] / (agg["total_tokens"] / 1e6)
        result[provider] = {
            "effective_rate": effective_rate,
            "spend_usd": agg["total_spend"],
            "token_count": agg["total_tokens"],
            "date": agg["date"],
        }

    return result


# ── Core calibration ─────────────────────────────────────────────────────────


def calibrate_daily(
    db_path: str | None = None,
    days_back: int = 1,
    seed_costs: dict[str, float] | None = None,
) -> dict[str, dict[str, float]]:
    """Run daily PriceKalman calibration.

    1. Query daily_spend for the last ``days_back`` days.
    2. For each provider with data, create a PriceKalman seeded with the
       provider's seed cost, then feed the effective rate via ``.update()``.
    3. Log convergence (base_rate, velocity, update_count) to kalman_samples.
    4. Return per-provider convergence info.

    Never raises — any error (missing DB, corrupt table, etc.) returns an
    empty dict. This is a cron job; it must not crash.

    Args:
        db_path: Path to zai_usage.db. Defaults to ~/.hermes/bot/zai_usage.db.
        days_back: Number of days to look back (1 = yesterday only).
        seed_costs: Override seed costs. Defaults to :data:`SEED_COSTS`.

    Returns:
        ``{provider: {"base_rate": float, "velocity": float, "update_count": int}}``
        for each provider that had observations. Empty dict on error.
    """
    if seed_costs is None:
        seed_costs = SEED_COSTS

    if db_path is None:
        db_path = os.path.expanduser("~/.hermes/bot/zai_usage.db")

    if not os.path.exists(db_path):
        return {}

    try:
        conn = sqlite3.connect(db_path, timeout=10)
        _ensure_kalman_samples_table(conn)
        conn.close()

        rates = query_yesterday_rates(db_path, days_back=days_back)
        if not rates:
            return {}

        conn = sqlite3.connect(db_path, timeout=10)
        _ensure_kalman_samples_table(conn)

        now = time.time()
        results: dict[str, dict[str, float]] = {}

        for provider, rate_info in rates.items():
            seed = seed_costs.get(provider, 0.50)
            pk = PriceKalman(
                initial_rate=seed,
                process_noise=1e-6,
                measurement_noise=1e-4,
            )
            pk.update(rate_info["effective_rate"])

            # Log convergence to kalman_samples
            try:
                conn.execute(
                    "INSERT INTO kalman_samples "
                    "(ts, provider, base_rate, velocity, update_count, source) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (now, provider, pk.base_rate, pk.velocity,
                     pk._updates, "daily_calibration"),
                )
            except Exception:
                pass  # logging must not break calibration

            results[provider] = {
                "base_rate": pk.base_rate,
                "velocity": pk.velocity,
                "update_count": pk._updates,
            }

        conn.commit()
        conn.close()
        return results

    except Exception:
        return {}


# ── Standalone CLI ────────────────────────────────────────────────────────────


def _format_rate(rate: float) -> str:
    """Format $/M for display."""
    if rate < 0.01:
        return f"${rate:.6f}/M"
    return f"${rate:.4f}/M"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns 0 on success, 1 on error."""
    parser = argparse.ArgumentParser(
        description="Daily PriceKalman calibration — feed yesterday's effective "
                    "rates from daily_spend to PriceKalman and log convergence."
    )
    parser.add_argument(
        "--db",
        default=os.path.expanduser("~/.hermes/bot/zai_usage.db"),
        help="Path to zai_usage.db (default: ~/.hermes/bot/zai_usage.db)",
    )
    parser.add_argument(
        "--days-back",
        type=int,
        default=1,
        help="Number of days to look back (default: 1 = yesterday only). "
             "Use >1 to catch up after downtime.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show per-provider detail.",
    )
    args = parser.parse_args(argv)

    print("=" * 72)
    print("  Daily PriceKalman Calibration")
    print("=" * 72)
    print(f"  DB: {args.db}")
    print(f"  Days back: {args.days_back}")
    print()

    if not os.path.exists(args.db):
        print("  ✗ DB not found — nothing to calibrate.")
        return 1

    # Ensure kalman_samples table exists (even if no data to calibrate)
    try:
        conn = sqlite3.connect(args.db, timeout=10)
        _ensure_kalman_samples_table(conn)
        conn.close()
    except Exception:
        pass

    rates = query_yesterday_rates(args.db, days_back=args.days_back)
    if not rates:
        print("  No spend data found for the requested period — nothing to calibrate.")
        return 1

    if args.verbose:
        print("  Yesterday's effective rates:")
        for provider in sorted(rates):
            info = rates[provider]
            print(
                f"    {provider:15s}  "
                f"spend=${info['spend_usd']:>10.4f}  "
                f"tokens={info['token_count']:>12,}  "
                f"rate={_format_rate(info['effective_rate'])}"
            )
        print()

    results = calibrate_daily(db_path=args.db, days_back=args.days_back)

    if not results:
        print("  Calibration produced no results.")
        return 1

    # Summary table
    print(f"  {'Provider':<15s}  {'Seed':>12s}  {'Converged':>12s}  {'Updates':>8s}")
    print("  " + "-" * 55)

    for provider in sorted(results):
        info = results[provider]
        seed = SEED_COSTS.get(provider, 0.50)
        print(
            f"  {provider:<15s}  "
            f"{_format_rate(seed):>12s}  "
            f"{_format_rate(info['base_rate']):>12s}  "
            f"{info['update_count']:>8d}"
        )

    print()
    print("  Convergence logged to kalman_samples table.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())