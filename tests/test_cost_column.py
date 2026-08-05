"""Tests for scripts/add_cost_column.py — RP-1 cost_usd column migration + backfill.

Covers:
  - columns added and idempotent (re-run is a no-op)
  - backfill math: rate = daily_spend.spend_usd / daily_spend.token_count * total_tokens
  - join on (local date, key_name == daily_spend.tier), NOT api_calls.tier
  - rows with no daily_spend match stay NULL
  - division-by-zero guard (daily_spend.token_count == 0)
  - re-run / idempotency of the backfill (cost_source IS NULL guard)
  - new 'measured' rows inserted by future code are never clobbered
  - new calls CAN store a real measured cost
"""
from __future__ import annotations

import datetime
import importlib.util
import os
import sqlite3
import sys
import tempfile

import pytest

# scripts/ is not a Python package — load the migration module by path so the
# test does not depend on a particular layout or sys.path state.
_SCRIPT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts",
    "add_cost_column.py",
)
_spec = importlib.util.spec_from_file_location("add_cost_column", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
add_cost_column = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(add_cost_column)

# constants re-exposed for readability
SOURCE_MEASURED = add_cost_column.SOURCE_MEASURED
SOURCE_BACKFILLED = add_cost_column.SOURCE_BACKFILLED


# ── Fixtures / helpers ───────────────────────────────────────────────────────

# Production api_calls DDL (mirrors ~/.hermes/bot/zai_proxy.py + test_ollama_quota_tracker).
_API_CALLS_DDL = """
CREATE TABLE api_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    key_name TEXT,
    key_suffix TEXT,
    model TEXT,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    tier TEXT,
    cache_hit INTEGER DEFAULT 0,
    ollama_hit INTEGER DEFAULT 0,
    ppq_hit INTEGER DEFAULT 0,
    status_code INTEGER,
    error TEXT,
    duration_ms INTEGER
)
"""

_DAILY_SPEND_DDL = """
CREATE TABLE daily_spend (
    date TEXT NOT NULL,
    tier TEXT NOT NULL,
    spend_usd REAL DEFAULT 0,
    call_count INTEGER DEFAULT 0,
    token_count INTEGER DEFAULT 0,
    PRIMARY KEY (date, tier)
)
"""


def _mkdb(api_rows, spend_rows=None):
    """Build a temp DB with the production schema, seed it, return (path, cleanup)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute(_API_CALLS_DDL)
    conn.execute(_DAILY_SPEND_DDL)
    conn.executemany(
        "INSERT INTO api_calls "
        "(ts, key_name, tier, model, total_tokens, status_code) "
        "VALUES (?,?,?,?,?,200)",
        api_rows,
    )
    if spend_rows:
        conn.executemany(
            "INSERT INTO daily_spend (date, tier, spend_usd, call_count, token_count) "
            "VALUES (?,?,?,?,?)",
            spend_rows,
        )
    conn.commit()
    conn.close()

    def _cleanup():
        try:
            os.unlink(path)
        except OSError:
            pass

    return path, _cleanup


def _utc_ts(date_str):
    """'YYYY-MM-DD 12:00:00' UTC -> unix timestamp."""
    dt = datetime.datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
    return dt.replace(tzinfo=datetime.timezone.utc).timestamp()


# ── Column addition ──────────────────────────────────────────────────────────


class TestAddColumns:
    def test_adds_both_columns(self):
        path, cleanup = _mkdb(api_rows=[(_utc_ts("2026-07-15 00:00:00"), "ours", "zai", "m", 100)])
        try:
            added = add_cost_column.add_columns(path)
            assert added == {"cost_usd": True, "cost_source": True}
            conn = sqlite3.connect(path)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(api_calls)")}
            conn.close()
            assert "cost_usd" in cols
            assert "cost_source" in cols
        finally:
            cleanup()

    def test_idempotent_second_run_reports_false(self):
        path, cleanup = _mkdb(api_rows=[(_utc_ts("2026-07-15 00:00:00"), "ours", "zai", "m", 100)])
        try:
            add_cost_column.add_columns(path)
            added = add_cost_column.add_columns(path)  # second run
            assert added == {"cost_usd": False, "cost_source": False}
        finally:
            cleanup()

    def test_missing_db_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            add_cost_column.add_columns(str(tmp_path / "nope.db"))

    def test_missing_table_raises(self, tmp_path):
        path = str(tmp_path / "empty.db")
        sqlite3.connect(path).close()  # create empty DB
        with pytest.raises(RuntimeError, match="does not exist"):
            add_cost_column.add_columns(path, table="api_calls")


# ── Backfill math ────────────────────────────────────────────────────────────


class TestBackfillMath:
    def test_basic_backfill_proportional(self):
        """cost = (spend / token_count) * total_tokens, joined on local date + key_name."""
        # one daily_spend row: $10 spent, 1_000_000 tokens -> $1e-5/token
        # one api_call: 50_000 tokens -> expected $0.50
        ts = _utc_ts("2026-07-15 12:00:00")
        api_rows = [(ts, "ours", "zai", "glm-4.5-flash", 50_000)]
        # daily_spend.date must match the LOCAL date of ts, exactly as the
        # proxy records it (datetime.date.today().isoformat()). Derive it the
        # same way SQLite's date(ts,'unixepoch','localtime') will on this host.
        local_date = datetime.datetime.fromtimestamp(ts).date().isoformat()
        spend_rows = [(local_date, "ours", 10.0, 1, 1_000_000)]
        path, cleanup = _mkdb(api_rows, spend_rows)
        try:
            add_cost_column.add_columns(path)
            res = add_cost_column.backfill_from_daily_spend(path)
            assert res["backfilled"] == 1
            conn = sqlite3.connect(path)
            row = conn.execute(
                "SELECT cost_usd, cost_source FROM api_calls WHERE total_tokens=50000"
            ).fetchone()
            conn.close()
            assert row[0] == pytest.approx(0.50, rel=1e-9)
            assert row[1] == SOURCE_BACKFILLED
        finally:
            cleanup()

    def test_two_calls_share_daily_spend_proportionally(self):
        """Two calls on the same day+key split the day's spend by token share."""
        ts = _utc_ts("2026-07-15 12:00:00")
        local_date = datetime.datetime.fromtimestamp(ts).date().isoformat()
        api_rows = [
            (ts, "ours", "zai", "m", 30_000),
            (ts, "ours", "zai", "m", 70_000),
        ]
        spend_rows = [(local_date, "ours", 10.0, 2, 100_000)]  # $1e-4/token
        path, cleanup = _mkdb(api_rows, spend_rows)
        try:
            add_cost_column.add_columns(path)
            add_cost_column.backfill_from_daily_spend(path)
            conn = sqlite3.connect(path)
            costs = sorted(
                r[0] for r in conn.execute("SELECT cost_usd FROM api_calls").fetchall()
            )
            conn.close()
            # 30000*1e-4=3.0, 70000*1e-4=7.0
            assert costs == pytest.approx([3.0, 7.0], rel=1e-9)
        finally:
            cleanup()

    def test_flat_rate_zero_spend_gives_zero_cost(self):
        """ours (z.ai flat-rate) has $0 spend -> backfilled cost is exactly $0."""
        ts = _utc_ts("2026-07-15 12:00:00")
        local_date = datetime.datetime.fromtimestamp(ts).date().isoformat()
        api_rows = [(ts, "ours", "zai", "glm-4.5-flash", 999_999)]
        spend_rows = [(local_date, "ours", 0.0, 5, 5_000_000)]
        path, cleanup = _mkdb(api_rows, spend_rows)
        try:
            add_cost_column.add_columns(path)
            add_cost_column.backfill_from_daily_spend(path)
            conn = sqlite3.connect(path)
            row = conn.execute("SELECT cost_usd, cost_source FROM api_calls").fetchone()
            conn.close()
            assert row[0] == 0.0
            assert row[1] == SOURCE_BACKFILLED
        finally:
            cleanup()

    def test_division_by_zero_token_count_guarded(self):
        """daily_spend row with token_count=0 must be skipped, not crash."""
        ts = _utc_ts("2026-07-15 12:00:00")
        local_date = datetime.datetime.fromtimestamp(ts).date().isoformat()
        api_rows = [(ts, "ours", "zai", "m", 1000)]
        spend_rows = [(local_date, "ours", 5.0, 1, 0)]  # token_count=0
        path, cleanup = _mkdb(api_rows, spend_rows)
        try:
            add_cost_column.add_columns(path)
            res = add_cost_column.backfill_from_daily_spend(path)  # must not raise
            assert res["backfilled"] == 0
            conn = sqlite3.connect(path)
            row = conn.execute("SELECT cost_usd, cost_source FROM api_calls").fetchone()
            conn.close()
            assert row[0] is None
            assert row[1] is None
        finally:
            cleanup()


# ── Join semantics ───────────────────────────────────────────────────────────


class TestJoinSemantics:
    def test_joins_on_key_name_not_tier(self):
        """api_calls.tier='zai' but daily_spend.tier='ours' (the key_name).

        The backfill must match via key_name, otherwise this row would be missed.
        """
        ts = _utc_ts("2026-07-15 12:00:00")
        local_date = datetime.datetime.fromtimestamp(ts).date().isoformat()
        api_rows = [(ts, "ours", "zai", "glm-4.5-flash", 100_000)]
        spend_rows = [(local_date, "ours", 4.0, 1, 1_000_000)]
        path, cleanup = _mkdb(api_rows, spend_rows)
        try:
            add_cost_column.add_columns(path)
            res = add_cost_column.backfill_from_daily_spend(path)
            assert res["backfilled"] == 1
            conn = sqlite3.connect(path)
            cost = conn.execute("SELECT cost_usd FROM api_calls").fetchone()[0]
            conn.close()
            assert cost == pytest.approx(0.40, rel=1e-9)
        finally:
            cleanup()

    def test_unmatched_key_stays_null(self):
        """ppq key has no daily_spend row -> cost stays NULL (no honest estimate)."""
        ts = _utc_ts("2026-07-15 12:00:00")
        local_date = datetime.datetime.fromtimestamp(ts).date().isoformat()
        api_rows = [(ts, "ppq", "ppq", "deepseek-v4-pro", 10_000)]
        spend_rows = [(local_date, "ours", 1.0, 1, 100_000)]  # only 'ours'
        path, cleanup = _mkdb(api_rows, spend_rows)
        try:
            add_cost_column.add_columns(path)
            res = add_cost_column.backfill_from_daily_spend(path)
            assert res["backfilled"] == 0
            conn = sqlite3.connect(path)
            row = conn.execute("SELECT cost_usd, cost_source FROM api_calls").fetchone()
            conn.close()
            assert row[0] is None
            assert row[1] is None
        finally:
            cleanup()

    def test_wrong_date_stays_null(self):
        """An api_call on a date with no daily_spend row is not backfilled."""
        ts = _utc_ts("2026-07-15 12:00:00")
        local_date = datetime.datetime.fromtimestamp(ts).date().isoformat()
        # call on the 15th, but spend only exists for the 16th
        api_rows = [(ts, "ours", "zai", "m", 10_000)]
        spend_rows = [("2026-07-16", "ours", 1.0, 1, 100_000)]
        path, cleanup = _mkdb(api_rows, spend_rows)
        try:
            add_cost_column.add_columns(path)
            res = add_cost_column.backfill_from_daily_spend(path)
            assert res["backfilled"] == 0
            assert res["remaining_null"] == 1
            _ = local_date  # noqa: F841 (kept for documentation)
        finally:
            cleanup()

    def test_null_key_name_skipped(self):
        """Rows with NULL key_name cannot match any tier -> skipped safely."""
        ts = _utc_ts("2026-07-15 12:00:00")
        local_date = datetime.datetime.fromtimestamp(ts).date().isoformat()
        api_rows = [(ts, None, "zai", "m", 10_000)]
        spend_rows = [(local_date, "ours", 1.0, 1, 100_000)]
        path, cleanup = _mkdb(api_rows, spend_rows)
        try:
            add_cost_column.add_columns(path)
            res = add_cost_column.backfill_from_daily_spend(path)
            assert res["backfilled"] == 0
        finally:
            cleanup()


# ── Idempotency & forward-compat ─────────────────────────────────────────────


class TestIdempotency:
    def test_backfill_rerun_is_noop(self):
        """Running backfill twice does not re-write or duplicate work."""
        ts = _utc_ts("2026-07-15 12:00:00")
        local_date = datetime.datetime.fromtimestamp(ts).date().isoformat()
        api_rows = [(ts, "ours", "zai", "m", 100_000)]
        spend_rows = [(local_date, "ours", 4.0, 1, 1_000_000)]
        path, cleanup = _mkdb(api_rows, spend_rows)
        try:
            add_cost_column.add_columns(path)
            first = add_cost_column.backfill_from_daily_spend(path)
            second = add_cost_column.backfill_from_daily_spend(path)
            assert first["backfilled"] == 1
            assert second["backfilled"] == 0  # already has cost_source set
            assert second["remaining_null"] == 0
        finally:
            cleanup()

    def test_measured_rows_not_clobbered(self):
        """A row already tagged 'measured' (future RP-2 insert) is never overwritten."""
        ts = _utc_ts("2026-07-15 12:00:00")
        local_date = datetime.datetime.fromtimestamp(ts).date().isoformat()
        api_rows = [(ts, "ours", "zai", "m", 100_000)]
        spend_rows = [(local_date, "ours", 4.0, 1, 1_000_000)]
        path, cleanup = _mkdb(api_rows, spend_rows)
        try:
            add_cost_column.add_columns(path)
            # simulate RP-2 having already written a real measured cost
            conn = sqlite3.connect(path)
            conn.execute(
                "UPDATE api_calls SET cost_usd=0.123, cost_source=?",
                (SOURCE_MEASURED,),
            )
            conn.commit()
            conn.close()
            res = add_cost_column.backfill_from_daily_spend(path)
            assert res["backfilled"] == 0
            conn = sqlite3.connect(path)
            row = conn.execute("SELECT cost_usd, cost_source FROM api_calls").fetchone()
            conn.close()
            assert row[0] == 0.123
            assert row[1] == SOURCE_MEASURED
        finally:
            cleanup()

    def test_full_pipeline_columns_then_backfill(self):
        """add_columns + backfill + verify work together; verify() reflects state."""
        ts = _utc_ts("2026-07-15 12:00:00")
        local_date = datetime.datetime.fromtimestamp(ts).date().isoformat()
        api_rows = [
            (ts, "ours", "zai", "m", 100_000),       # backfillable
            (ts, "ppq", "ppq", "deepseek", 5_000),    # no match -> NULL
        ]
        spend_rows = [(local_date, "ours", 2.0, 1, 1_000_000)]
        path, cleanup = _mkdb(api_rows, spend_rows)
        try:
            add_cost_column.add_columns(path)
            add_cost_column.backfill_from_daily_spend(path)
            snap = add_cost_column.verify(path)
            assert snap["cost_usd_exists"] is True
            assert snap["cost_source_exists"] is True
            assert snap["total_rows"] == 2
            assert snap["rows_with_cost"] == 1
            assert snap["rows_null"] == 1
            assert snap["by_source"].get(SOURCE_BACKFILLED) == 1
            assert snap["cost_sum"] == pytest.approx(0.20, rel=1e-9)
        finally:
            cleanup()


# ── New calls can store real cost ────────────────────────────────────────────


class TestNewMeasuredRows:
    def test_new_row_can_store_measured_cost(self):
        """After migration, a fresh INSERT with a measured cost round-trips."""
        ts = _utc_ts("2026-07-15 12:00:00")
        api_rows = [(ts, "ours", "zai", "m", 100)]
        path, cleanup = _mkdb(api_rows)
        try:
            add_cost_column.add_columns(path)
            conn = sqlite3.connect(path)
            conn.execute(
                "INSERT INTO api_calls (ts, key_name, tier, model, total_tokens, "
                "status_code, cost_usd, cost_source) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (ts, "ollama_cloud", "ollama_cloud", "glm-5.2", 2000, 200, 0.000031, SOURCE_MEASURED),
            )
            conn.commit()
            row = conn.execute(
                "SELECT cost_usd, cost_source FROM api_calls WHERE key_name='ollama_cloud'"
            ).fetchone()
            conn.close()
            assert row[0] == pytest.approx(0.000031, rel=1e-9)
            assert row[1] == SOURCE_MEASURED
        finally:
            cleanup()
