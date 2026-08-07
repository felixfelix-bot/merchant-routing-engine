"""Tests for provider telemetry table — success/fail/latency per request.

Phase 2.5.1 (Gate 1, TDD): written BEFORE the implementation.
Every test uses a throwaway SQLite file — the production usage DB
(``~/.hermes/bot/zai_usage.db``) is never touched.

The module under test is ``zai_proxy`` (the production proxy), specifically:
  - ``_ensure_telemetry_table(conn)``  — schema migration (CREATE TABLE IF NOT EXISTS)
  - ``_log_provider_telemetry(...)``    — INSERT one row per request

Both functions are designed to NEVER raise — telemetry failure is silent and
must never break request handling.  The ``test_never_raises`` test verifies this
explicitly by monkey-patching the DB connection to raise.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone

import pytest

# ── Import path setup ──────────────────────────────────────────────────────
# zai_proxy.py lives in ~/.hermes/bot/ and is NOT in the merchant-routing-engine
# repo.  We add its directory to sys.path so we can import it as a module.
_BOT_DIR = os.path.expanduser("~/.hermes/bot")
if _BOT_DIR not in sys.path:
    sys.path.insert(0, _BOT_DIR)

# The tests import the *functions* directly from zai_proxy.  Because zai_proxy
# has heavy module-level side effects (loading keys, starting shadow hooks,
# connecting to the production DB), we must be careful.  We only need two
# functions: _ensure_telemetry_table and _log_provider_telemetry.  We import
# the module — its side effects are wrapped in try/except internally so they
# won't crash even in a test environment (keys won't load, shadow stays None).
import zai_proxy


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_db_path():
    """A fresh temp file path for an isolated SQLite DB. Cleaned up after."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="telemetry_test_")
    os.close(fd)
    os.unlink(path)  # let the test create it fresh
    yield path
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


@pytest.fixture
def conn(tmp_db_path):
    """A fresh SQLite connection (WAL mode, autocommit) — mirrors _usage_db()."""
    c = sqlite3.connect(tmp_db_path, timeout=10, isolation_level=None,
                        check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    yield c
    c.close()


def _row_count(conn: sqlite3.Connection, table: str = "provider_telemetry") -> int:
    """Count rows in the telemetry table. Returns 0 if table doesn't exist."""
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.OperationalError:
        return 0


def _fetch_rows(conn: sqlite3.Connection) -> list[dict]:
    """Fetch all telemetry rows as dicts for easy assertions."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, ts, provider, response_received, response_valid, "
        "latency_ms, error_type, billed_tokens, actual_tokens, "
        "token_mismatch, model "
        "FROM provider_telemetry ORDER BY id"
    ).fetchall()
    conn.row_factory = None  # reset
    return [dict(r) for r in rows]


# ── Tests: schema ───────────────────────────────────────────────────────────


class TestTelemetryTableSchema:
    def test_telemetry_table_created(self, conn):
        """_ensure_telemetry_table creates the table with the correct schema."""
        zai_proxy._ensure_telemetry_table(conn)
        # Verify table exists
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='provider_telemetry'"
        ).fetchall()
        assert len(tables) == 1
        assert tables[0][0] == "provider_telemetry"

        # Verify column names and types
        cols = conn.execute("PRAGMA table_info(provider_telemetry)").fetchall()
        col_map = {row[1]: row[2] for row in cols}  # name -> type

        assert "id" in col_map
        assert "ts" in col_map
        assert "provider" in col_map
        assert "response_received" in col_map
        assert "response_valid" in col_map
        assert "latency_ms" in col_map
        assert "error_type" in col_map
        assert "billed_tokens" in col_map
        assert "actual_tokens" in col_map
        assert "token_mismatch" in col_map
        assert "model" in col_map  # Phase 4.5b — per-model quality tracking

        # ts must be NOT NULL
        ts_col = [c for c in cols if c[1] == "ts"][0]
        assert ts_col[3] == 1  # notnull flag

        # provider must be NOT NULL
        prov_col = [c for c in cols if c[1] == "provider"][0]
        assert prov_col[3] == 1  # notnull flag

    def test_telemetry_table_idempotent(self, conn):
        """Calling _ensure_telemetry_table twice does not error or duplicate."""
        zai_proxy._ensure_telemetry_table(conn)
        zai_proxy._ensure_telemetry_table(conn)  # should not raise
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='provider_telemetry'"
        ).fetchall()
        assert len(tables) == 1


# ── Tests: INSERT on success ───────────────────────────────────────────────


class TestTelemetryInsertOnSuccess:
    def test_insert_on_success(self, conn):
        """Successful response: response_received=True, response_valid=True."""
        zai_proxy._ensure_telemetry_table(conn)
        zai_proxy._log_provider_telemetry(
            conn=conn,
            provider="zai_ours",
            response_received=True,
            response_valid=True,
            latency_ms=250,
            error_type="none",
            billed_tokens=100,
            actual_tokens=100,
            token_mismatch=False,
        )
        rows = _fetch_rows(conn)
        assert len(rows) == 1
        r = rows[0]
        assert r["provider"] == "zai_ours"
        assert r["response_received"] == 1  # SQLite stores bool as 0/1
        assert r["response_valid"] == 1
        assert r["latency_ms"] == 250
        assert r["error_type"] == "none"
        assert r["billed_tokens"] == 100
        assert r["actual_tokens"] == 100
        assert r["token_mismatch"] == 0
        assert r["ts"] is not None


# ── Tests: INSERT on failure ───────────────────────────────────────────────


class TestTelemetryInsertOnFailure:
    def test_insert_on_failure(self, conn):
        """Failed response: response_received=False, error_type set."""
        zai_proxy._ensure_telemetry_table(conn)
        zai_proxy._log_provider_telemetry(
            conn=conn,
            provider="zai_friend",
            response_received=False,
            response_valid=False,
            latency_ms=5000,
            error_type="timeout",
            billed_tokens=0,
            actual_tokens=0,
            token_mismatch=False,
        )
        rows = _fetch_rows(conn)
        assert len(rows) == 1
        r = rows[0]
        assert r["response_received"] == 0
        assert r["response_valid"] == 0
        assert r["error_type"] == "timeout"
        assert r["latency_ms"] == 5000


# ── Tests: INSERT on parse error ───────────────────────────────────────────


class TestTelemetryInsertOnParseError:
    def test_insert_on_parse_error(self, conn):
        """Response received but didn't parse as valid LLM output."""
        zai_proxy._ensure_telemetry_table(conn)
        zai_proxy._log_provider_telemetry(
            conn=conn,
            provider="ollama_cloud",
            response_received=True,
            response_valid=False,
            latency_ms=300,
            error_type="parse_error",
            billed_tokens=0,
            actual_tokens=50,
            token_mismatch=False,
        )
        rows = _fetch_rows(conn)
        assert len(rows) == 1
        r = rows[0]
        assert r["response_received"] == 1
        assert r["response_valid"] == 0
        assert r["error_type"] == "parse_error"


# ── Tests: latency recorded ────────────────────────────────────────────────


class TestTelemetryLatency:
    def test_latency_recorded(self, conn):
        """latency_ms is stored as a positive integer."""
        zai_proxy._ensure_telemetry_table(conn)
        zai_proxy._log_provider_telemetry(
            conn=conn,
            provider="zai_ours",
            response_received=True,
            response_valid=True,
            latency_ms=42,
            error_type="none",
            billed_tokens=10,
            actual_tokens=10,
            token_mismatch=False,
        )
        rows = _fetch_rows(conn)
        assert len(rows) == 1
        assert rows[0]["latency_ms"] == 42
        assert rows[0]["latency_ms"] > 0


# ── Tests: never raises ─────────────────────────────────────────────────────


class TestTelemetryNeverRaises:
    def test_never_raises(self, conn):
        """Telemetry NEVER breaks request handling — all errors are swallowed.

        This is the CRITICAL safety property.  We force the DB connection to
        raise on every call and verify _log_provider_telemetry returns silently
        without propagating the exception.
        """
        # First create the table so the function gets past schema creation
        zai_proxy._ensure_telemetry_table(conn)

        # Now create a connection that raises on execute
        class BadConn:
            def execute(self, *a, **kw):
                raise sqlite3.OperationalError("simulated DB failure")

        bad = BadConn()

        # Must NOT raise
        zai_proxy._log_provider_telemetry(
            conn=bad,
            provider="test",
            response_received=True,
            response_valid=True,
            latency_ms=10,
            error_type="none",
            billed_tokens=0,
            actual_tokens=0,
            token_mismatch=False,
        )

    def test_never_raises_on_none_conn(self):
        """Passing a None connection must not raise."""
        zai_proxy._log_provider_telemetry(
            conn=None,
            provider="test",
            response_received=True,
            response_valid=True,
            latency_ms=10,
            error_type="none",
            billed_tokens=0,
            actual_tokens=0,
            token_mismatch=False,
        )

    def test_never_raises_on_bad_values(self, conn):
        """Passing garbage values must not raise."""
        zai_proxy._ensure_telemetry_table(conn)
        zai_proxy._log_provider_telemetry(
            conn=conn,
            provider=None,
            response_received=None,
            response_valid=None,
            latency_ms=None,
            error_type=None,
            billed_tokens=None,
            actual_tokens=None,
            token_mismatch=None,
        )


# ── Tests: token mismatch detection ────────────────────────────────────────


class TestTelemetryTokenMismatch:
    def test_token_mismatch_detection(self, conn):
        """When billed != actual, token_mismatch is True (fraud signal)."""
        zai_proxy._ensure_telemetry_table(conn)
        zai_proxy._log_provider_telemetry(
            conn=conn,
            provider="ppq",
            response_received=True,
            response_valid=True,
            latency_ms=150,
            error_type="none",
            billed_tokens=500,
            actual_tokens=250,
            token_mismatch=True,
        )
        rows = _fetch_rows(conn)
        assert len(rows) == 1
        assert rows[0]["token_mismatch"] == 1
        assert rows[0]["billed_tokens"] == 500
        assert rows[0]["actual_tokens"] == 250

    def test_token_match_no_mismatch(self, conn):
        """When billed == actual, token_mismatch is False."""
        zai_proxy._ensure_telemetry_table(conn)
        zai_proxy._log_provider_telemetry(
            conn=conn,
            provider="zai_ours",
            response_received=True,
            response_valid=True,
            latency_ms=100,
            error_type="none",
            billed_tokens=300,
            actual_tokens=300,
            token_mismatch=False,
        )
        rows = _fetch_rows(conn)
        assert len(rows) == 1
        assert rows[0]["token_mismatch"] == 0


# ── Tests: multiple inserts ─────────────────────────────────────────────────


class TestTelemetryMultipleInserts:
    def test_multiple_inserts_accumulate(self, conn):
        """Multiple telemetry rows accumulate in order."""
        zai_proxy._ensure_telemetry_table(conn)
        for i in range(5):
            zai_proxy._log_provider_telemetry(
                conn=conn,
                provider=f"provider_{i}",
                response_received=True,
                response_valid=True,
                latency_ms=100 + i,
                error_type="none",
                billed_tokens=10 * i,
                actual_tokens=10 * i,
                token_mismatch=False,
            )
        rows = _fetch_rows(conn)
        assert len(rows) == 5
        for i, r in enumerate(rows):
            assert r["provider"] == f"provider_{i}"
            assert r["latency_ms"] == 100 + i


# ── Tests: model column (Phase 4.5b) ───────────────────────────────────────


class TestTelemetryModelColumn:
    """Phase 4.5b: provider_telemetry carries a ``model`` column so the
    model-aware CPVO calculator can track quality per (provider, model) pair.
    """

    def test_model_column_in_schema(self, conn):
        """_ensure_telemetry_table creates the model column on a fresh DB."""
        zai_proxy._ensure_telemetry_table(conn)
        cols = {
            r[1]
            for r in conn.execute(
                "PRAGMA table_info(provider_telemetry)"
            ).fetchall()
        }
        assert "model" in cols

    def test_model_populated_when_passed(self, conn):
        """Passing model= stores it in the model column."""
        zai_proxy._ensure_telemetry_table(conn)
        zai_proxy._log_provider_telemetry(
            conn=conn,
            provider="zai_ours",
            response_received=True,
            response_valid=True,
            latency_ms=120,
            error_type="none",
            billed_tokens=100,
            actual_tokens=100,
            token_mismatch=False,
            model="glm-5.2",
        )
        rows = _fetch_rows(conn)
        assert len(rows) == 1
        assert rows[0]["model"] == "glm-5.2"

    def test_model_defaults_to_null(self, conn):
        """Omitting model leaves the column NULL (backward compatible)."""
        zai_proxy._ensure_telemetry_table(conn)
        zai_proxy._log_provider_telemetry(
            conn=conn,
            provider="zai_ours",
            response_received=True,
            response_valid=True,
            latency_ms=120,
            error_type="none",
            billed_tokens=100,
            actual_tokens=100,
            token_mismatch=False,
        )
        rows = _fetch_rows(conn)
        assert len(rows) == 1
        assert rows[0]["model"] is None

    def test_ensure_table_self_heals_legacy_db(self, conn):
        """_ensure_telemetry_table ALTER-adds model to a legacy (model-less)
        table — mirrors production, where the live DB predates the column."""
        # Build a legacy table WITHOUT the model column.
        conn.execute(
            "CREATE TABLE provider_telemetry ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, "
            "provider TEXT NOT NULL, response_received INTEGER, "
            "response_valid INTEGER, latency_ms INTEGER, error_type TEXT, "
            "billed_tokens INTEGER, actual_tokens INTEGER, "
            "token_mismatch INTEGER)"
        )
        before = {
            r[1]
            for r in conn.execute(
                "PRAGMA table_info(provider_telemetry)"
            ).fetchall()
        }
        assert "model" not in before
        # Self-heal (and confirm it is idempotent).
        zai_proxy._ensure_telemetry_table(conn)
        zai_proxy._ensure_telemetry_table(conn)
        after = {
            r[1]
            for r in conn.execute(
                "PRAGMA table_info(provider_telemetry)"
            ).fetchall()
        }
        assert "model" in after
        # Logging still works on the healed table.
        zai_proxy._log_provider_telemetry(
            conn=conn,
            provider="zai_friend",
            response_received=True,
            response_valid=True,
            latency_ms=10,
            error_type="none",
            billed_tokens=1,
            actual_tokens=1,
            token_mismatch=False,
            model="glm-4.5-flash",
        )
        rows = _fetch_rows(conn)
        assert len(rows) == 1
        assert rows[0]["model"] == "glm-4.5-flash"