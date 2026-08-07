#!/usr/bin/env python3
"""Migration P4.5b: add the ``model`` column to ``provider_telemetry``.

WHY
---
``cpvo_calculator.CPVOCalculator`` is model-aware (Phase 4.5b): when the
``provider_telemetry`` table has a ``model`` column it tracks quality per
``(provider, model)`` pair, distinguishing e.g. ``glm-5.2`` (reliable) from
``glm-4.5-flash`` (flaky) on the same provider.  Without the column the
calculator silently downgrades to provider-level aggregation (it does a
``PRAGMA table_info`` check and only filters on model when the column
exists).

This migration adds the column so the model-aware path is live.  It is
idempotent and safe to run repeatedly.

SCOPE
-----
* Adds ``model TEXT`` (nullable) to ``provider_telemetry``.  Existing rows
  are backfilled to NULL — the model was never recorded historically, so
  retroactive backfill is impossible; per-model quality accrues going
  forward as the proxy starts logging the served model.
* Adds an index on ``(provider, model)`` to keep the 24h-window quality
  queries (``compute_cpvo`` / ``get_quality_score``) fast as the table
  grows.

NOTE: this only adds the column.  The production proxy
(``~/.hermes/bot/zai_proxy.py``) must also INSERT the model value — that
wiring lives in ``_log_provider_telemetry`` and is applied separately
(revert-safe, backward compatible — ``model`` defaults to ``None``).

USAGE
-----
    # default DB: ~/.hermes/bot/zai_usage.db
    python3 scripts/migrate_telemetry_model_column.py

    # explicit DB path
    python3 scripts/migrate_telemetry_model_column.py /path/to/zai_usage.db

REVERT
------
SQLite >= 3.35 supports dropping a column directly:

    ALTER TABLE provider_telemetry DROP COLUMN model;

Dropping the column is safe — the CPVO calculator falls back to
provider-level quality when the column is absent (its ``PRAGMA table_info``
guard handles both cases).  Re-running this migration re-adds it.
"""
from __future__ import annotations

import os
import sqlite3
import sys

DEFAULT_DB = os.path.expanduser("~/.hermes/bot/zai_usage.db")


def migrate(db_path: str = DEFAULT_DB) -> dict:
    """Add the ``model`` column + ``(provider, model)`` index. Idempotent.

    Returns a status dict describing what happened.  Never raises — any
    SQLite error is captured and reported so the migration can be run
    unattended.
    """
    if not os.path.exists(db_path):
        return {"db_path": db_path, "ok": False, "reason": "db not found"}
    try:
        conn = sqlite3.connect(db_path, timeout=30)
        try:
            cols = {
                r[1]
                for r in conn.execute(
                    "PRAGMA table_info(provider_telemetry)"
                ).fetchall()
            }
            if not cols:
                return {
                    "db_path": db_path,
                    "ok": False,
                    "reason": "provider_telemetry table missing",
                }
            added = False
            if "model" not in cols:
                conn.execute(
                    "ALTER TABLE provider_telemetry ADD COLUMN model TEXT"
                )
                added = True
            # Idempotent index (helps the 24h-window per-model quality query).
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_telemetry_provider_model "
                "ON provider_telemetry(provider, model)"
            )
            conn.commit()
            after = [
                r[1]
                for r in conn.execute(
                    "PRAGMA table_info(provider_telemetry)"
                ).fetchall()
            ]
            n = conn.execute(
                "SELECT COUNT(*) FROM provider_telemetry"
            ).fetchone()[0]
            return {
                "db_path": db_path,
                "ok": True,
                "column_added": added,
                "has_model": "model" in after,
                "columns": after,
                "row_count": n,
            }
        finally:
            conn.close()
    except Exception as exc:  # never raise on unattended runs
        return {"db_path": db_path, "ok": False, "reason": repr(exc)}


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB
    result = migrate(db)
    print(result)
    sys.exit(0 if result.get("ok") else 1)
