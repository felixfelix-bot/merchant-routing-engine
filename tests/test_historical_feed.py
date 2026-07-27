"""Tests for feed_historical_costs — historical daily_spend → PriceKalman.

Verifies:
  - Effective $/M is computed correctly from spend and token_count.
  - Provider name mapping (tier → provider) works correctly.
  - 'unknown' tier is skipped.
  - Rows with token_count=0 are skipped.
  - base_rate moves away from seed toward the real cost.
  - load_historical_rates falls back to seeds when DB is missing.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.feed_historical_costs import (
    TIER_MAP,
    DailyObservation,
    compute_effective_rates,
    feed_historical,
    load_daily_spend,
    load_historical_rates,
)
from src.price_kalman import PriceKalman


# ── Fixtures ────────────────────────────────────────────────────────────────


SAMPLE_ROWS = [
    # (date, tier, spend_usd, call_count, token_count)
    # 'manager' → 'ours', real spend early, then $0 (key expired)
    ("2026-07-12", "manager", 0.50, 4803, 400_000_000),
    ("2026-07-13", "manager", 1.75, 3015, 200_000_000),
    ("2026-07-14", "manager", 0.00, 1244, 100_000_000),
    # 'worker' → 'ours'
    ("2026-07-12", "worker",  0.15, 12521, 120_000_000),
    ("2026-07-13", "worker",  0.48, 7958, 60_000_000),
    ("2026-07-14", "worker",  0.00, 2310, 10_000_000),
    # 'friend' — real spend
    ("2026-07-25", "friend",  1.82, 2390, 62_766_461),
    ("2026-07-26", "friend",  8.80, 9927, 303_559_728),
    ("2026-07-27", "friend",  2.80, 3711, 96_569_428),
    # 'ollama_cloud'
    ("2026-07-27", "ollama_cloud", 0.78, 383, 32_502_554),
    # 'deepinfra'
    ("2026-07-26", "deepinfra",  2.66, 29, 2_046_047),
    ("2026-07-27", "deepinfra",  3.22, 25, 2_479_055),
    # 'unknown' → skip
    ("2026-07-26", "unknown",   0.00, 68, 6_183_563),
    # token_count=0 → skip
    ("2026-07-20", "worker",    0.00, 5, 0),
    ("2026-07-20", "friend",    0.00, 0, 0),
]


@pytest.fixture
def temp_db():
    """Create a temporary SQLite DB with sample daily_spend data."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE daily_spend (
            date        TEXT,
            tier        TEXT,
            spend_usd   REAL,
            call_count  INTEGER,
            token_count INTEGER
        )
    """)
    conn.executemany(
        "INSERT INTO daily_spend VALUES (?, ?, ?, ?, ?)",
        SAMPLE_ROWS,
    )
    conn.commit()
    conn.close()
    yield path
    os.unlink(path)


SEED_COSTS = {
    "ours":         0.31,
    "friend":       0.375,
    "ollama_cloud":  0.50,
    "deepinfra":     1.30,
    "ppq":          0.14,
    "openrouter":   0.135,
}


# ── Tier mapping ───────────────────────────────────────────────────────────


class TestTierMapping:
    def test_ours_maps_to_ours(self):
        assert TIER_MAP["ours"] == "ours"

    def test_friend_maps_to_friend(self):
        assert TIER_MAP["friend"] == "friend"

    def test_manager_maps_to_ours(self):
        """'manager' tier uses the 'ours' z.ai key."""
        assert TIER_MAP["manager"] == "ours"

    def test_worker_maps_to_ours(self):
        """'worker' tier uses the 'ours' z.ai key."""
        assert TIER_MAP["worker"] == "ours"

    def test_unknown_maps_to_none(self):
        assert TIER_MAP["unknown"] is None

    def test_ollama_cloud_maps_to_ollama_cloud(self):
        assert TIER_MAP["ollama_cloud"] == "ollama_cloud"

    def test_deepinfra_maps_to_deepinfra(self):
        assert TIER_MAP["deepinfra"] == "deepinfra"


# ── Effective rate computation ─────────────────────────────────────────────


class TestComputeEffectiveRates:
    def test_basic_computation(self):
        """$1.00 spend / 500K tokens = $2.0/M."""
        rows = [("2026-07-15", "friend", 1.0, 10, 500_000)]
        obs = compute_effective_rates(rows)
        assert "friend" in obs
        assert len(obs["friend"]) == 1
        assert obs["friend"][0].effective_rate == pytest.approx(2.0, rel=1e-6)

    def test_zero_tokens_skipped(self):
        """Rows with token_count=0 should be skipped."""
        rows = [
            ("2026-07-15", "friend", 1.0, 10, 0),
            ("2026-07-16", "friend", 2.0, 20, 500_000),
        ]
        obs = compute_effective_rates(rows)
        assert len(obs["friend"]) == 1
        assert obs["friend"][0].date == "2026-07-16"

    def test_unknown_tier_skipped(self):
        """'unknown' tier should not appear in any provider's observations."""
        rows = [("2026-07-15", "unknown", 5.0, 100, 1_000_000)]
        obs = compute_effective_rates(rows)
        assert obs == {}

    def test_manager_and_worker_both_map_to_ours(self):
        """Both 'manager' and 'worker' tiers feed the 'ours' provider."""
        rows = [
            ("2026-07-12", "manager", 1.0, 10, 500_000),
            ("2026-07-12", "worker",  1.0, 10, 500_000),
        ]
        obs = compute_effective_rates(rows)
        assert "ours" in obs
        assert len(obs["ours"]) == 2

    def test_chronological_order(self):
        """Observations should be sorted by date."""
        rows = [
            ("2026-07-16", "friend", 1.0, 10, 500_000),
            ("2026-07-14", "friend", 1.0, 10, 500_000),
            ("2026-07-15", "friend", 1.0, 10, 500_000),
        ]
        obs = compute_effective_rates(rows)
        dates = [o.date for o in obs["friend"]]
        assert dates == ["2026-07-14", "2026-07-15", "2026-07-16"]


# ── Kalman feeding ──────────────────────────────────────────────────────────


class TestFeedHistorical:
    def test_base_rate_moves_toward_real_cost(self):
        """After feeding observations, base_rate should move away from seed
        toward the real cost."""
        # Real rate: $2.0/M, seed: $0.50/M → should move upward toward 2.0
        rows = [("2026-07-15", "friend", 10.0, 100, 5_000_000)]  # $2.0/M
        obs = compute_effective_rates(rows)
        kalmans = feed_historical(obs, {"friend": 0.50})
        pk, n = kalmans["friend"]
        assert n == 1
        # After one observation at $2.0/M, base_rate should move upward
        assert pk.base_rate > 0.50, "base_rate should increase toward $2.0/M"

    def test_multiple_observations_converge(self):
        """Multiple observations at the same rate should converge closely."""
        # 5 rows all at $2.0/M
        rows = [
            (f"2026-07-{d:02d}", "friend", 2.0, 100, 1_000_000)
            for d in range(15, 20)
        ]
        obs = compute_effective_rates(rows)
        kalmans = feed_historical(obs, {"friend": 0.50})
        pk, n = kalmans["friend"]
        assert n == 5
        # After 5 observations at $2.0/M, should be close to 2.0
        assert pk.base_rate > 1.0, "Should converge well above seed toward $2.0/M"

    def test_providers_without_data_keep_seed(self):
        """Providers with no historical data should have 0 observations
        and base_rate == seed."""
        rows = [("2026-07-15", "friend", 1.0, 10, 500_000)]
        obs = compute_effective_rates(rows)
        kalmans = feed_historical(obs, SEED_COSTS)
        pk_ppq, n_ppq = kalmans["ppq"]
        assert n_ppq == 0
        assert pk_ppq.base_rate == pytest.approx(0.14, rel=1e-6)

    def test_observations_count_correct(self):
        """The returned count should match the number of valid observations."""
        rows = SAMPLE_ROWS
        obs = compute_effective_rates(rows)
        kalmans = feed_historical(obs, SEED_COSTS)

        # 'ours' gets both manager + worker rows (minus zero-token rows)
        # manager: 3 rows (all have tokens), worker: 3 rows (one at 07-20
        # has token_count=0, skipped) → 3 + 3 = 6
        _, n_ours = kalmans["ours"]
        assert n_ours == 6

        # 'friend': 3 rows with tokens
        _, n_friend = kalmans["friend"]
        assert n_friend == 3

        # 'ollama_cloud': 1 row
        _, n_ollama = kalmans["ollama_cloud"]
        assert n_ollama == 1

        # 'deepinfra': 2 rows
        _, n_deepinfra = kalmans["deepinfra"]
        assert n_deepinfra == 2


# ── Full integration: load_historical_rates ────────────────────────────────


class TestLoadHistoricalRates:
    def test_returns_converged_rates(self, temp_db):
        """load_historical_rates should return converged rates for providers
        with historical data, and seed rates for providers without."""
        rates = load_historical_rates(db_path=temp_db, seed_costs=SEED_COSTS)

        # 'friend' has real spend at $0.029/M — should be way below seed
        assert "friend" in rates
        assert rates["friend"] < SEED_COSTS["friend"]
        assert rates["friend"] > 0

        # 'ppq' has no data → should keep seed
        assert rates["ppq"] == pytest.approx(SEED_COSTS["ppq"], rel=1e-6)

        # 'ours' had $0 spend → should be below seed (near floor)
        assert rates["ours"] < SEED_COSTS["ours"]
        assert rates["ours"] >= 0.001  # MIN_EFFECTIVE_PRICE floor

        # 'deepinfra' should be close to $1.30/M (its actual rate)
        assert rates["deepinfra"] == pytest.approx(1.30, rel=0.1)

    def test_falls_back_to_seeds_when_db_missing(self):
        """If the DB path doesn't exist, should return seed costs unchanged."""
        rates = load_historical_rates(
            db_path="/nonexistent/path/to/db.db",
            seed_costs=SEED_COSTS,
        )
        assert rates == SEED_COSTS

    def test_falls_back_to_seeds_when_empty_db(self):
        """If the DB has no rows, should return seed costs unchanged."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE daily_spend (
                date TEXT, tier TEXT, spend_usd REAL,
                call_count INTEGER, token_count INTEGER
            )
        """)
        conn.commit()
        conn.close()

        try:
            rates = load_historical_rates(db_path=path, seed_costs=SEED_COSTS)
            assert rates == SEED_COSTS
        finally:
            os.unlink(path)

    def test_never_returns_negative(self, temp_db):
        """Converged rates should never be negative (floor at MIN_EFFECTIVE_PRICE)."""
        rates = load_historical_rates(db_path=temp_db, seed_costs=SEED_COSTS)
        for provider, rate in rates.items():
            assert rate >= 0.001, f"{provider} rate {rate} is below floor"


# ── DB reader ──────────────────────────────────────────────────────────────


class TestLoadDailySpend:
    def test_reads_all_rows(self, temp_db):
        """load_daily_spend should read all rows from the DB."""
        rows = load_daily_spend(temp_db)
        assert len(rows) == len(SAMPLE_ROWS)

    def test_ordered_by_date(self, temp_db):
        """Rows should be ordered by date."""
        rows = load_daily_spend(temp_db)
        dates = [r[0] for r in rows]
        assert dates == sorted(dates)