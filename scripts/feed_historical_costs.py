"""feed_historical_costs.py — Feed recorded daily_spend data to PriceKalman.

Reads the daily_spend table from zai_usage.db, computes effective $/M per
provider per day, and feeds the observations chronologically to PriceKalman
instances.  This achieves instant convergence without waiting 24–48h for
live observations to accumulate.

Usage (standalone):

    python3 scripts/feed_historical_costs.py
    python3 scripts/feed_historical_costs.py --db /path/to/zai_usage.db

Importable (called by primary_router on startup):

    from scripts.feed_historical_costs import load_historical_rates
    converged = load_historical_rates(db_path, seed_costs=_SEED_COSTS)
    # converged = {"ours": 0.31, "friend": 0.029, ...}
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from dataclasses import dataclass, field

# ── Path bootstrap so `from src.price_kalman` works standalone ──────────────
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from src.price_kalman import PriceKalman
from src.provider_names import normalize_provider_name

__all__ = [
    "TIER_MAP",
    "DailyObservation",
    "load_daily_spend",
    "compute_effective_rates",
    "feed_historical",
    "load_historical_rates",
]

# ── Tier → provider name mapping ────────────────────────────────────────────
# 'manager' and 'worker' both use the 'ours' z.ai key (same subscription).
# 'unknown' is skipped — we can't attribute cost to a specific provider.
# 'zai_ours' and 'zai_friend' are legacy aliases that also need mapping.
# Uses normalize_provider_name for canonical mapping, with an explicit
# skip list for tiers that should not be attributed to any provider.
_SKIP_TIERS: frozenset[str] = frozenset({"unknown", ""})


def _tier_to_provider(tier: str | None) -> str | None:
    """Map a daily_spend tier to a canonical provider name.

    Returns None for tiers that should be skipped (unknown, empty).
    """
    if tier is None or tier in _SKIP_TIERS:
        return None
    return normalize_provider_name(tier)


# Kept for backward compatibility — tests import TIER_MAP directly.
TIER_MAP: dict[str, str | None] = {
    "ours":         "ours",
    "friend":       "friend",
    "ollama_cloud": "ollama_cloud",
    "deepinfra":     "deepinfra",
    "manager":      "ours",
    "worker":       "ours",
    "zai_ours":     "ours",
    "zai_friend":   "friend",
    "unknown":      None,
}


@dataclass
class DailyObservation:
    """One row of effective $/M for a provider on a given date."""
    date: str
    provider: str
    effective_rate: float   # $/M
    spend_usd: float
    token_count: int


# ── DB reader ───────────────────────────────────────────────────────────────


def load_daily_spend(db_path: str) -> list[tuple[str, str, float, int, int]]:
    """Read all rows from daily_spend table, ordered by date then tier.

    Returns list of (date, tier, spend_usd, call_count, token_count).
    """
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "SELECT date, tier, spend_usd, call_count, token_count "
            "FROM daily_spend ORDER BY date, tier"
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    return rows


# ── Effective rate computation ──────────────────────────────────────────────


def compute_effective_rates(
    rows: list[tuple[str, str, float, int, int]],
) -> dict[str, list[DailyObservation]]:
    """Convert raw DB rows into per-provider chronological observations.

    For each row:
      - Map tier → provider via TIER_MAP (skip if None).
      - effective $/M = spend_usd / (token_count / 1e6)  if token_count > 0.
      - Skip rows where token_count == 0 (no tokens → no observable rate).

    Returns:
        {provider: [DailyObservation, ...]} sorted chronologically.
    """
    by_provider: dict[str, list[DailyObservation]] = {}

    for date, tier, spend_usd, call_count, token_count in rows:
        provider = _tier_to_provider(tier)
        if provider is None:
            continue

        if token_count <= 0:
            continue

        effective_rate = spend_usd / (token_count / 1e6)

        obs = DailyObservation(
            date=date,
            provider=provider,
            effective_rate=effective_rate,
            spend_usd=spend_usd,
            token_count=token_count,
        )
        by_provider.setdefault(provider, []).append(obs)

    # Ensure chronological order (rows are already ordered by date from SQL,
    # but multiple tiers on the same date could interleave for the same
    # provider after mapping, so we sort explicitly).
    for provider in by_provider:
        by_provider[provider].sort(key=lambda o: o.date)

    return by_provider


# ── Kalman feeding ──────────────────────────────────────────────────────────


def feed_historical(
    observations: dict[str, list[DailyObservation]],
    seed_costs: dict[str, float],
    process_noise: float = 1e-6,
    measurement_noise: float = 1e-4,
) -> dict[str, tuple[PriceKalman, int]]:
    """Create PriceKalman instances seeded with seed_costs and feed observations.

    For each provider that has historical data:
      1. Create a PriceKalman with the seed rate (or a default).
      2. Feed each DailyObservation's effective_rate via .update().
      3. Return the converged Kalman + count of observations fed.

    Providers in seed_costs with no historical data get a Kalman at the seed
    rate with 0 observations.

    Args:
        observations: Output of compute_effective_rates().
        seed_costs: Dict of provider → seed $/M (e.g. _SEED_COSTS).
        process_noise: Kalman process noise (Q).
        measurement_noise: Kalman measurement noise (R).

    Returns:
        {provider: (PriceKalman, num_observations)}
    """
    result: dict[str, tuple[PriceKalman, int]] = {}

    # All providers from seed_costs plus any found in historical data
    all_providers = set(seed_costs.keys()) | set(observations.keys())

    for provider in all_providers:
        seed = seed_costs.get(provider, 0.50)  # default fallback seed
        pk = PriceKalman(
            initial_rate=seed,
            process_noise=process_noise,
            measurement_noise=measurement_noise,
        )

        obs_list = observations.get(provider, [])
        for obs in obs_list:
            pk.update(obs.effective_rate)

        result[provider] = (pk, len(obs_list))

    return result


# ── High-level convenience: load + compute + feed ──────────────────────────


def load_historical_rates(
    db_path: str | None = None,
    seed_costs: dict[str, float] | None = None,
) -> dict[str, float]:
    """Load historical daily_spend data and return converged base rates.

    This is the primary entry point for primary_router to call on startup.
    It reads the DB, computes effective rates, feeds them to PriceKalman,
    and returns the converged base_rate for each provider.

    If the DB is unavailable or has no data, falls back to seed_costs.

    Args:
        db_path: Path to zai_usage.db. Defaults to ~/.hermes/bot/zai_usage.db.
        seed_costs: Dict of provider → seed $/M. If None, uses built-in defaults.

    Returns:
        {provider: converged_base_rate ($/M)}
    """
    if seed_costs is None:
        # Import here to avoid circular import when called from primary_router
        from src.primary_router import _SEED_COSTS as DEFAULT_SEEDS
        seed_costs = DEFAULT_SEEDS

    if db_path is None:
        db_path = os.path.expanduser("~/.hermes/bot/zai_usage.db")

    # Default to seed costs
    converged: dict[str, float] = dict(seed_costs)

    if not os.path.exists(db_path):
        return converged

    try:
        rows = load_daily_spend(db_path)
        if not rows:
            return converged

        observations = compute_effective_rates(rows)
        kalmans = feed_historical(observations, seed_costs)

        for provider, (pk, n) in kalmans.items():
            if n > 0:
                # Clamp to MIN_EFFECTIVE_PRICE — base_rate can go slightly
                # negative when the Kalman velocity overshoots toward zero
                # (e.g. 'ours' has $0 spend for many days).
                rate = max(pk.base_rate, 0.001)
                converged[provider] = rate

    except Exception:
        # Any DB/read error → keep seed costs
        pass

    return converged


# ── Standalone CLI ──────────────────────────────────────────────────────────


def _format_rate(rate: float) -> str:
    """Format $/M for display."""
    if rate < 0.01:
        return f"${rate:.6f}/M"
    return f"${rate:.4f}/M"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Feed historical daily_spend data to PriceKalman for instant convergence."
    )
    parser.add_argument(
        "--db",
        default=os.path.expanduser("~/.hermes/bot/zai_usage.db"),
        help="Path to zai_usage.db (default: ~/.hermes/bot/zai_usage.db)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show per-observation detail.",
    )
    args = parser.parse_args()

    # Built-in seed costs (same as primary_router / shadow_hook)
    seed_costs = {
        "ours":          0.31,
        "friend":        0.375,
        "ollama_cloud":  0.50,
        "ppq":           0.14,
        "openrouter":    0.135,
        "deepinfra":     1.30,
    }

    print("=" * 72)
    print("  Historical Cost Feed → PriceKalman Convergence")
    print("=" * 72)
    print(f"  DB: {args.db}")
    print()

    if not os.path.exists(args.db):
        print(f"  ✗ DB not found — using seed costs only.")
        for provider, seed in seed_costs.items():
            print(f"    {provider:15s}  seed={_format_rate(seed)}  (no historical data)")
        return 1

    rows = load_daily_spend(args.db)
    print(f"  Rows in daily_spend: {len(rows)}")
    print()

    observations = compute_effective_rates(rows)

    if args.verbose:
        print("  Per-day observations:")
        for provider in sorted(observations):
            for obs in observations[provider]:
                print(
                    f"    {obs.date}  {obs.provider:15s}  "
                    f"spend=${obs.spend_usd:>10.4f}  "
                    f"tokens={obs.token_count:>12,}  "
                    f"rate={_format_rate(obs.effective_rate)}"
                )
        print()

    kalmans = feed_historical(observations, seed_costs)

    # Summary table
    print(f"  {'Provider':<15s}  {'Obs':>4s}  {'Seed':>12s}  {'Converged':>12s}  {'Delta':>12s}")
    print("  " + "-" * 68)

    for provider in sorted(kalmans):
        pk, n_obs = kalmans[provider]
        seed = seed_costs.get(provider, 0.50)
        converged = pk.base_rate
        delta = converged - seed
        delta_str = f"{delta:+.6f}/M" if abs(delta) > 1e-8 else "—"
        print(
            f"  {provider:<15s}  {n_obs:>4d}  "
            f"{_format_rate(seed):>12s}  "
            f"{_format_rate(converged):>12s}  "
            f"{delta_str:>12s}"
        )

    print()
    print("  Converged rates (for copy into _SEED_COSTS if desired):")
    print("  {")
    for provider in sorted(kalmans):
        pk, n = kalmans[provider]
        print(f"      {provider!r:<18s}: {pk.base_rate:.6f},")
    print("  }")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())