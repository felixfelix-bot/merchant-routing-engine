#!/usr/bin/env python3
"""scripts/add_cost_column.py — RP-1: add cost tracking columns to api_calls + backfill from daily_spend.

Adds two columns to the ``api_calls`` table::

    cost_usd    REAL  DEFAULT NULL   -- measured/approximate USD cost of the call
    cost_source TEXT  DEFAULT NULL   -- 'measured' | 'backfilled' | 'estimated'

Then backfills ``cost_usd`` for existing rows using the ``daily_spend``
aggregate table as an approximation::

    rate = daily_spend.spend_usd / daily_spend.token_count   ($ per token)
    api_calls.cost_usd = rate * api_calls.total_tokens

JOIN semantics (must match how the proxy records spend):

    daily_spend.tier  == api_calls.key_name     (NOT api_calls.tier — see below)
    daily_spend.date  == date(api_calls.ts, 'unixepoch', 'localtime')

The proxy's ``_spend_tier(key_name)`` writes ``daily_spend.tier`` from the
*key name* (ours / friend / ollama_cloud / deepinfra / unknown), while
``api_calls.tier`` holds the *provider* (zai / ollama_cloud / ppq / ...).
So the backfill joins on ``key_name``, not ``tier``.

Rows that have no matching ``daily_spend`` row (pre-2026-07-12 history, plus
``ppq``/``openrouter`` keys that were never aggregated) are left with
``cost_usd = NULL`` — there is no honest basis to estimate them.

The script is IDEMPOTENT: it checks for the columns before adding them and
only backfills rows where ``cost_source IS NULL``, so re-running is safe and
skips already-processed rows. Once RP-2 starts writing ``cost_source =
'measured'`` at request time, those rows are never touched by the backfill.

Usage::

    python3 scripts/add_cost_column.py                      # default DB
    python3 scripts/add_cost_column.py --db /path/to.db     # custom DB
    python3 scripts/add_cost_column.py --skip-backfill      # columns only
    python3 scripts/add_cost_column.py --dry-run            # report only

Exit codes: 0 success, 2 DB/table not found, 3 SQL error.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from typing import Any

DEFAULT_DB = os.path.expanduser("~/.hermes/bot/zai_usage.db")

# cost_source vocabulary (documented; not enforced by CHECK to keep migrations cheap)
SOURCE_MEASURED = "measured"      # real cost parsed from the provider's API response (RP-2)
SOURCE_BACKFILLED = "backfilled"  # approximated from daily_spend aggregate (this script)
SOURCE_ESTIMATED = "estimated"    # derived from a rate-card estimate (future use)


# ── Schema helpers ───────────────────────────────────────────────────────────


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """True if ``column`` exists on ``table``."""
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def add_columns(
    db_path: str,
    table: str = "api_calls",
) -> dict[str, bool]:
    """Add ``cost_usd`` and ``cost_source`` columns if missing.

    Returns a dict ``{"cost_usd": bool, "cost_source": bool}`` where True means
    the column was newly added on this run (False = already existed).
    Idempotent.
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"DB not found: {db_path}")

    conn = sqlite3.connect(db_path)
    try:
        if not _table_exists(conn, table):
            raise RuntimeError(f"Table '{table}' does not exist in {db_path}")

        added: dict[str, bool] = {}
        if not _column_exists(conn, table, "cost_usd"):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN cost_usd REAL DEFAULT NULL")
            added["cost_usd"] = True
        else:
            added["cost_usd"] = False

        if not _column_exists(conn, table, "cost_source"):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN cost_source TEXT DEFAULT NULL")
            added["cost_source"] = True
        else:
            added["cost_source"] = False

        conn.commit()
        return added
    finally:
        conn.close()


# ── Backfill ─────────────────────────────────────────────────────────────────


def _count_backfillable(conn: sqlite3.Connection, table: str, spend_table: str) -> int:
    """Count rows that *would* be backfilled on this run (cost_source IS NULL and matchable)."""
    return conn.execute(
        f"""
        SELECT COUNT(*)
        FROM {table} AS a
        WHERE a.cost_source IS NULL
          AND a.key_name IS NOT NULL
          AND EXISTS (
            SELECT 1 FROM {spend_table} AS d
            WHERE d.date = date(a.ts, 'unixepoch', 'localtime')
              AND d.tier = a.key_name
              AND d.token_count > 0
          )
        """
    ).fetchone()[0]


def backfill_from_daily_spend(
    db_path: str,
    table: str = "api_calls",
    spend_table: str = "daily_spend",
) -> dict[str, int]:
    """Backfill ``cost_usd`` from ``daily_spend`` for all matchable, unprocessed rows.

    Only rows with ``cost_source IS NULL`` are touched, so this is idempotent
    and never clobbers values written by RP-2 ('measured').

    Returns ``{"backfilled": N, "remaining_null": M}`` where ``remaining_null``
    is the count of rows left without a cost (no daily_spend match available).
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"DB not found: {db_path}")

    conn = sqlite3.connect(db_path)
    try:
        if not _table_exists(conn, table):
            raise RuntimeError(f"Table '{table}' does not exist in {db_path}")
        if not _table_exists(conn, spend_table):
            # No spend table -> nothing to backfill; not an error.
            total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            return {"backfilled": 0, "remaining_null": total}

        before = _count_backfillable(conn, table, spend_table)

        # SQLite >= 3.33 UPDATE...FROM. Correlated rate = spend / tokens * call tokens.
        # date() uses 'localtime' to match how the proxy wrote daily_spend.date
        # (via datetime.date.today().isoformat() in _record_spend).
        conn.execute(
            f"""
            UPDATE {table} AS a
            SET cost_usd = d.spend_usd * 1.0 / d.token_count * a.total_tokens,
                cost_source = ?
            FROM {spend_table} AS d
            WHERE date(a.ts, 'unixepoch', 'localtime') = d.date
              AND a.key_name = d.tier
              AND d.token_count > 0
              AND a.cost_source IS NULL
            """,
            (SOURCE_BACKFILLED,),
        )
        conn.commit()

        remaining_null = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE cost_usd IS NULL"
        ).fetchone()[0]
        return {"backfilled": before, "remaining_null": remaining_null}
    finally:
        conn.close()


# ── Verification ─────────────────────────────────────────────────────────────


def verify(db_path: str, table: str = "api_calls") -> dict[str, Any]:
    """Return a snapshot of the cost columns for sanity-checking.

    Keys: cost_usd_exists, cost_source_exists, total_rows, rows_with_cost,
    rows_null, by_source (counts per cost_source), sample (a few backfilled rows).
    """
    conn = sqlite3.connect(db_path)
    try:
        has_usd = _column_exists(conn, table, "cost_usd")
        has_src = _column_exists(conn, table, "cost_source")
        result: dict[str, Any] = {
            "cost_usd_exists": has_usd,
            "cost_source_exists": has_src,
            "total_rows": conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
        }
        if has_usd:
            result["rows_with_cost"] = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE cost_usd IS NOT NULL"
            ).fetchone()[0]
            result["rows_null"] = result["total_rows"] - result["rows_with_cost"]
            row = conn.execute(
                f"SELECT MIN(cost_usd), MAX(cost_usd), SUM(cost_usd) FROM {table} "
                f"WHERE cost_usd IS NOT NULL"
            ).fetchone()
            result["cost_min"], result["cost_max"], result["cost_sum"] = row
        if has_src:
            result["by_source"] = {
                (s or "(null)"): n
                for s, n in conn.execute(
                    f"SELECT cost_source, COUNT(*) FROM {table} GROUP BY cost_source"
                ).fetchall()
            }
        return result
    finally:
        conn.close()


# ── CLI ──────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Add cost_usd + cost_source to api_calls and backfill from daily_spend.",
    )
    parser.add_argument("--db", default=DEFAULT_DB, help=f"path to usage DB (default: {DEFAULT_DB})")
    parser.add_argument("--table", default="api_calls")
    parser.add_argument("--spend-table", default="daily_spend")
    parser.add_argument("--skip-backfill", action="store_true", help="only add columns")
    parser.add_argument("--dry-run", action="store_true", help="report what would happen, change nothing")
    args = parser.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"error: DB not found: {args.db}", file=sys.stderr)
        return 2

    print(f"DB: {args.db}")
    if args.dry_run:
        snap = verify(args.db, args.table)
        print(f"[dry-run] cost_usd column present : {snap.get('cost_usd_exists')}")
        print(f"[dry-run] cost_source column present: {snap.get('cost_source_exists')}")
        if snap.get("cost_usd_exists"):
            print(f"[dry-run] rows with cost: {snap.get('rows_with_cost')} / {snap.get('total_rows')}")
            print(f"[dry-run] by_source: {snap.get('by_source')}")
        else:
            # estimate how many rows WOULD be backfilled if columns existed
            try:
                conn = sqlite3.connect(args.db)
                # simulate: count matchable rows ignoring the not-yet-existing column
                n = conn.execute(
                    f"SELECT COUNT(*) FROM {args.table} AS a WHERE a.key_name IS NOT NULL "
                    f"AND EXISTS (SELECT 1 FROM {args.spend_table} AS d "
                    f"WHERE d.date = date(a.ts,'unixepoch','localtime') "
                    f"AND d.tier = a.key_name AND d.token_count > 0)"
                ).fetchone()[0]
                print(f"[dry-run] rows that WOULD be backfilled: {n}")
                conn.close()
            except sqlite3.Error as e:
                print(f"[dry-run] could not estimate backfill count: {e}", file=sys.stderr)
        return 0

    t0 = time.time()
    added = add_columns(args.db, table=args.table)
    print(f"columns: cost_usd added={added['cost_usd']}, cost_source added={added['cost_source']}")

    if not args.skip_backfill:
        res = backfill_from_daily_spend(args.db, table=args.table, spend_table=args.spend_table)
        print(f"backfill: wrote {res['backfilled']} rows, "
              f"{res['remaining_null']} remain NULL (no daily_spend match)")

    snap = verify(args.db, args.table)
    print(f"verify: cost_usd_exists={snap['cost_usd_exists']} "
          f"cost_source_exists={snap['cost_source_exists']}")
    if snap.get("cost_usd_exists"):
        print(f"        rows_with_cost={snap.get('rows_with_cost')}/{snap['total_rows']} "
              f"sum=${snap.get('cost_sum', 0) or 0:.4f} "
              f"min=${snap.get('cost_min', 0) or 0:.6f} max=${snap.get('cost_max', 0) or 0:.4f}")
    if snap.get("by_source"):
        print(f"        by_source={snap['by_source']}")
    print(f"done in {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
