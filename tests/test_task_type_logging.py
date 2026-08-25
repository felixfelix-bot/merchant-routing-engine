"""Contract tests for CG-5 task_type logging (cost-gate-reform-v2 §CG-5, Q5).

Production surface under test — ``zai_proxy`` (the production proxy at
``~/.hermes/bot/zai_proxy.py``), specifically:

  - ``api_calls.task_type`` column: nullable TEXT, added idempotently,
    NO backfill (history stays NULL)
  - ``_resolve_task_type(headers, body)``: X-Task-Type header wins over the
    body ``task_type`` field; unset/unknown -> None (NEVER guessed)
  - ``_log_api_call(..., task_type=...)``: threads the value into every
    api_calls row, including external failover hops, with a fallback chain
    that never breaks request logging on a pre-migration DB
  - no behavior change when the field is absent (row shape and forwarding
    are unchanged; the body's task_type field passes through untouched)

Every test uses a throwaway SQLite file — the production usage DB
(``~/.hermes/bot/zai_usage.db``) is never touched.
"""
from __future__ import annotations

import ast
import json
import os
import sqlite3
import sys
import tempfile
import time

import pytest

# ── Import path setup ──────────────────────────────────────────────────────
# zai_proxy.py lives in ~/.hermes/bot/ and is NOT in the merchant-routing-engine
# repo (it is the production source of truth per AGENTS.md). Same pattern as
# tests/test_provider_telemetry.py.
_BOT_DIR = os.path.expanduser("~/.hermes/bot")
if _BOT_DIR not in sys.path:
    sys.path.insert(0, _BOT_DIR)

import zai_proxy  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────────


class _Hdrs:
    """Case-insensitive stand-in for BaseHTTPRequestHandler.headers .get()."""

    def __init__(self, d=None):
        self._d = {k.lower(): v for k, v in (d or {}).items()}

    def get(self, name, default=None):
        return self._d.get(name.lower(), default)


@pytest.fixture
def tmp_usage_db(monkeypatch, tmp_path):
    """Point zai_proxy._usage_db() at a throwaway DB (real migration code runs).

    Yields the path; guarantees the module singleton is reset afterwards so
    no other test (and never production code) sees our connection.
    """
    path = str(tmp_path / "task_type_test.db")
    monkeypatch.setattr(zai_proxy, "USAGE_DB", __import__("pathlib").Path(path))
    monkeypatch.setattr(zai_proxy, "_usage_db_conn", None)
    yield path
    conn = getattr(zai_proxy, "_usage_db_conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        monkeypatch.setattr(zai_proxy, "_usage_db_conn", None)


# Production api_calls DDL as of the PRE-CG-5 era (session_id present) —
# used to build legacy DBs the migration must handle.
_DDL_WITH_SESSION_ID = """
CREATE TABLE api_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    key_name TEXT,
    key_suffix TEXT,
    model TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    tier TEXT,
    cache_hit INTEGER DEFAULT 0,
    ollama_hit INTEGER DEFAULT 0,
    ppq_hit INTEGER DEFAULT 0,
    status_code INTEGER,
    error TEXT,
    duration_ms INTEGER,
    cost_usd REAL DEFAULT NULL,
    cost_source TEXT DEFAULT NULL,
    session_id TEXT DEFAULT NULL
)
"""

# Older still: before the §1.4 session_id migration (cost columns present).
_DDL_PRE_SESSION_ID = _DDL_WITH_SESSION_ID.replace(
    ",\n    session_id TEXT DEFAULT NULL\n", "").replace(
    "session_id TEXT DEFAULT NULL\n)", "")


def _columns(conn, table="api_calls"):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _mk_legacy_db(path, ddl, rows=0):
    conn = sqlite3.connect(path)
    conn.execute(ddl)
    for _ in range(rows):
        conn.execute(
            "INSERT INTO api_calls (ts, key_name, model, status_code) "
            "VALUES (?, 'ours', 'm', 200)", (time.time(),))
    conn.commit()
    conn.close()


# ── Schema migration ────────────────────────────────────────────────────────


class TestSchemaMigration:
    def test_fresh_db_has_task_type_column(self, tmp_usage_db):
        zai_proxy._usage_db()  # creates + migrates
        conn = sqlite3.connect(tmp_usage_db)
        cols = _columns(conn)
        conn.close()
        assert "task_type" in cols

    def test_legacy_db_gets_task_type_added(self, tmp_usage_db):
        _mk_legacy_db(tmp_usage_db, _DDL_WITH_SESSION_ID)
        zai_proxy._usage_db()  # ALTERs task_type in
        conn = sqlite3.connect(tmp_usage_db)
        cols = _columns(conn)
        conn.close()
        assert "task_type" in cols

    def test_pre_session_id_db_also_gets_task_type(self, tmp_usage_db):
        _mk_legacy_db(tmp_usage_db, _DDL_PRE_SESSION_ID)
        zai_proxy._usage_db()
        conn = sqlite3.connect(tmp_usage_db)
        cols = _columns(conn)
        conn.close()
        assert {"task_type", "session_id"} <= cols

    def test_no_backfill_history_stays_null(self, tmp_usage_db):
        """CG-5 contract: ALTER only — legacy rows NEVER get a task_type."""
        _mk_legacy_db(tmp_usage_db, _DDL_WITH_SESSION_ID, rows=3)
        zai_proxy._usage_db()
        conn = sqlite3.connect(tmp_usage_db)
        vals = [r[0] for r in conn.execute("SELECT task_type FROM api_calls")]
        conn.close()
        assert vals == [None, None, None]

    def test_reinit_idempotent(self, tmp_usage_db):
        zai_proxy._usage_db()
        zai_proxy._usage_db_conn = None  # simulate process restart
        zai_proxy._usage_db()  # must not raise on the now-migrated DB
        conn = sqlite3.connect(tmp_usage_db)
        cols = _columns(conn)
        conn.close()
        assert "task_type" in cols


# ── Extraction: header wins, body fallback, never guessed ──────────────────


class TestResolveTaskType:
    def test_header_wins_over_body(self):
        v = zai_proxy._resolve_task_type(
            _Hdrs({"X-Task-Type": "translate"}),
            b'{"task_type": "code-review", "model": "m"}')
        assert v == "translate"

    def test_body_used_when_header_absent(self):
        v = zai_proxy._resolve_task_type(
            _Hdrs({}), b'{"task_type": "code-review", "model": "m"}')
        assert v == "code-review"

    def test_none_when_both_absent(self):
        v = zai_proxy._resolve_task_type(_Hdrs({}), b'{"model": "m"}')
        assert v is None

    def test_header_value_stripped(self):
        v = zai_proxy._resolve_task_type(
            _Hdrs({"X-Task-Type": "  summarize  "}), b"{}")
        assert v == "summarize"

    def test_empty_header_falls_back_to_body(self):
        v = zai_proxy._resolve_task_type(
            _Hdrs({"X-Task-Type": ""}), b'{"task_type": "digest"}')
        assert v == "digest"

    def test_whitespace_header_falls_back_to_body(self):
        v = zai_proxy._resolve_task_type(
            _Hdrs({"X-Task-Type": "   "}), b'{"task_type": "digest"}')
        assert v == "digest"

    def test_whitespace_header_and_no_body_field_is_none(self):
        assert zai_proxy._resolve_task_type(_Hdrs({"X-Task-Type": "  "}), b"{}") is None

    def test_header_case_insensitive(self):
        v = zai_proxy._resolve_task_type(
            _Hdrs({"x-task-type": "digest"}), b"{}")
        assert v == "digest"

    def test_body_value_stripped(self):
        v = zai_proxy._resolve_task_type(
            _Hdrs({}), b'{"task_type": "  digest "}')
        assert v == "digest"

    def test_empty_body_value_is_none(self):
        assert zai_proxy._resolve_task_type(_Hdrs({}), b'{"task_type": ""}') is None

    def test_non_string_body_value_is_none(self):
        """Never guessed / never coerced: 42, null, lists -> NULL."""
        for bad in (b'{"task_type": 42}', b'{"task_type": null}',
                    b'{"task_type": ["x"]}', b'{"task_type": {"a": 1}}'):
            assert zai_proxy._resolve_task_type(_Hdrs({}), bad) is None

    def test_non_json_body_is_none(self):
        assert zai_proxy._resolve_task_type(_Hdrs({}), b"not json at all") is None

    def test_empty_body_is_none(self):
        assert zai_proxy._resolve_task_type(_Hdrs({}), b"") is None

    def test_never_raises_on_garbage(self):
        # whatever comes in, extraction must not break request handling
        assert zai_proxy._resolve_task_type(_Hdrs({"X-Task-Type": None}), b"\xff\xfe") is None


# ── _log_api_call round-trip + fallback chain ───────────────────────────────


class TestLogApiCallRoundTrip:
    def test_task_type_round_trips(self, tmp_usage_db):
        zai_proxy._log_api_call(
            key_name="ours", key_suffix="ab12", model="glm-4.5-flash",
            prompt_tokens=10, completion_tokens=5, total_tokens=15,
            tier="zai", status_code=200, duration_ms=42,
            session_id="s-1", task_type="code-review")
        conn = sqlite3.connect(tmp_usage_db)
        row = conn.execute(
            "SELECT key_name, session_id, task_type FROM api_calls").fetchone()
        conn.close()
        assert row == ("ours", "s-1", "code-review")

    def test_null_when_not_provided(self, tmp_usage_db):
        zai_proxy._log_api_call(key_name="ours", model="m", status_code=200)
        conn = sqlite3.connect(tmp_usage_db)
        row = conn.execute("SELECT task_type FROM api_calls").fetchone()
        conn.close()
        assert row == (None,)

    def test_explicit_none_is_null(self, tmp_usage_db):
        zai_proxy._log_api_call(key_name="ours", model="m", status_code=200,
                                task_type=None)
        conn = sqlite3.connect(tmp_usage_db)
        row = conn.execute("SELECT task_type FROM api_calls").fetchone()
        conn.close()
        assert row == (None,)

    def test_legacy_conn_without_task_type_column_still_logs(self, tmp_path):
        """Fallback chain: long-lived process predating the migration must
        not lose rows — task_type is silently dropped, everything else kept."""
        path = str(tmp_path / "legacy.db")
        _mk_legacy_db(path, _DDL_WITH_SESSION_ID)
        legacy = sqlite3.connect(path, isolation_level=None)
        saved = zai_proxy._usage_db_conn
        zai_proxy._usage_db_conn = legacy  # bypass migration on purpose
        try:
            zai_proxy._log_api_call(  # must not raise
                key_name="ours", model="m", status_code=200,
                session_id="s-9", task_type="translate")
        finally:
            zai_proxy._usage_db_conn = saved
            legacy.close()
        conn = sqlite3.connect(path)
        row = conn.execute(
            "SELECT key_name, session_id, status_code FROM api_calls").fetchone()
        conn.close()
        assert row == ("ours", "s-9", 200)

    def test_legacy_conn_without_session_id_column_still_logs(self, tmp_path):
        path = str(tmp_path / "legacy2.db")
        _mk_legacy_db(path, _DDL_PRE_SESSION_ID)
        legacy = sqlite3.connect(path, isolation_level=None)
        saved = zai_proxy._usage_db_conn
        zai_proxy._usage_db_conn = legacy
        try:
            zai_proxy._log_api_call(
                key_name="ours", model="m", status_code=200, task_type="digest")
        finally:
            zai_proxy._usage_db_conn = saved
            legacy.close()
        conn = sqlite3.connect(path)
        row = conn.execute(
            "SELECT key_name, status_code FROM api_calls").fetchone()
        conn.close()
        assert row == ("ours", 200)


# ── Failover hop: behavioral test through _try_external_failover ───────────


class _FakeResp:
    """Minimal stand-in for the urlopen() context manager result."""

    def __init__(self, payload: bytes):
        self.status = 200
        self.headers = {}  # http.client.HTTPMessage-like: .items() -> iterable
        self._stream = [payload, b""]
        self._i = 0

    def items(self):
        return []

    def read(self, n=-1):
        if self._i >= len(self._stream):
            return b""
        chunk = self._stream[self._i]
        self._i += 1
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestFailoverHop:
    def test_external_failover_row_carries_task_type(
            self, monkeypatch, tmp_usage_db):
        """The crown-jewel contract: a request that fails over to an external
        provider logs its api_calls row WITH the originating task_type."""
        payload = json.dumps({
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                      "total_tokens": 15},
        }).encode()
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = req.data
            captured["url"] = req.full_url
            return _FakeResp(payload)

        monkeypatch.setattr(
            "urllib.request.urlopen", fake_urlopen)
        monkeypatch.setattr(
            zai_proxy, "EXTERNAL_PROVIDERS",
            {"fakeprov": {"key": "sk-fake-0001", "base_url": "http://fake.local"}})
        monkeypatch.setattr(zai_proxy, "_is_provider_funded", lambda name: True)
        monkeypatch.setattr(zai_proxy, "_get_provider_cost",
                            lambda name, model: 0.0001)
        monkeypatch.setattr(zai_proxy, "_PROFIT_TRACKER", None)

        # Handler instance without socket/init — only the attributes the
        # failover path touches are needed.
        h = object.__new__(zai_proxy.Handler)
        h._session_id = "s-hop"
        h._task_type = "translate"
        sent = []
        h.send_response = lambda code: sent.append(("status", code))
        h.send_header = lambda k, v: sent.append((k, v))
        h.end_headers = lambda: None

        class _W:
            def write(self, b):
                return None

            def flush(self):
                return None

        h.wfile = _W()

        buf = bytearray()
        ok = h._try_external_failover(
            b'{"model": "glm-4.5-flash", "task_type": "translate", "messages": []}',
            "glm-4.5-flash", buf, time.time())
        assert ok is True, "fake provider should have succeeded"

        conn = sqlite3.connect(tmp_usage_db)
        rows = conn.execute(
            "SELECT key_name, tier, status_code, session_id, task_type "
            "FROM api_calls").fetchall()
        conn.close()
        assert len(rows) == 1
        key_name, tier, status_code, session_id, task_type = rows[0]
        assert key_name == "fakeprov"
        assert tier == "fakeprov"
        assert status_code == 200
        assert session_id == "s-hop"
        assert task_type == "translate"  # <- survives the failover hop

        # No behavior change to the upstream request: the body's task_type
        # field passes through untouched (still present, value preserved).
        fwd = json.loads(captured["body"])
        assert fwd["task_type"] == "translate"

    def test_failover_row_null_when_task_type_absent(
            self, monkeypatch, tmp_usage_db):
        payload = json.dumps({
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                      "total_tokens": 2},
        }).encode()
        monkeypatch.setattr(
            "urllib.request.urlopen", lambda req, timeout=None: _FakeResp(payload))
        monkeypatch.setattr(
            zai_proxy, "EXTERNAL_PROVIDERS",
            {"fakeprov": {"key": "sk-fake-0001", "base_url": "http://fake.local"}})
        monkeypatch.setattr(zai_proxy, "_is_provider_funded", lambda name: True)
        monkeypatch.setattr(zai_proxy, "_get_provider_cost",
                            lambda name, model: 0.0001)
        monkeypatch.setattr(zai_proxy, "_PROFIT_TRACKER", None)

        h = object.__new__(zai_proxy.Handler)
        h._session_id = None
        h._task_type = None  # caller sent nothing
        h.send_response = lambda code: None
        h.send_header = lambda k, v: None
        h.end_headers = lambda: None

        class _W:
            def write(self, b):
                return None

            def flush(self):
                return None

        h.wfile = _W()

        ok = h._try_external_failover(
            b'{"model": "glm-4.5-flash", "messages": []}',
            "glm-4.5-flash", bytearray(), time.time())
        assert ok is True
        conn = sqlite3.connect(tmp_usage_db)
        row = conn.execute("SELECT task_type FROM api_calls").fetchone()
        conn.close()
        assert row == (None,)


# ── Source contract: every _log_api_call site threads task_type ────────────


class TestCallSiteContract:
    """AST-level guard so no current or FUTURE logging site forgets task_type."""

    def _source(self):
        return os.path.join(_BOT_DIR, "zai_proxy.py")

    def test_every_log_api_call_site_passes_task_type(self):
        with open(self._source()) as f:
            tree = ast.parse(f.read())
        sites = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "_log_api_call"):
                sites.append(node)
        assert len(sites) >= 4, "expected the 4 known _log_api_call sites"
        missing = [
            n for n in sites
            if not any(kw.arg == "task_type" for kw in n.keywords)]
        assert not missing, (
            f"{len(missing)} _log_api_call site(s) missing task_type= "
            f"(lines {[n.lineno for n in missing]})")

    def test_proxy_method_assigns_self_task_type(self):
        with open(self._source()) as f:
            tree = ast.parse(f.read())
        assigns = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Attribute) and t.attr == "_task_type"
                    for t in n.targets)]
        assert assigns, "_proxy() must set self._task_type before any logging"


# ── No behavior change when the field is absent ─────────────────────────────


class TestNoBehaviorChangeWhenAbsent:
    def test_resolve_returns_none_for_plain_request(self):
        assert zai_proxy._resolve_task_type(
            _Hdrs({"Content-Type": "application/json"}),
            b'{"model": "m", "messages": []}') is None

    def test_row_shape_unchanged_without_task_type(self, tmp_usage_db):
        """A row written with no task_type has identical legacy columns."""
        zai_proxy._log_api_call(
            key_name="ours", key_suffix="ab12", model="glm-4.5-flash",
            prompt_tokens=10, completion_tokens=5, total_tokens=15,
            tier="zai", status_code=200, error=None, duration_ms=42,
            cost_usd=0.0, cost_source="flat_rate", session_id="s-1")
        conn = sqlite3.connect(tmp_usage_db)
        row = conn.execute(
            "SELECT ts, key_name, key_suffix, model, prompt_tokens, "
            "completion_tokens, total_tokens, tier, cache_hit, ollama_hit, "
            "ppq_hit, status_code, error, duration_ms, cost_usd, "
            "cost_source, session_id, task_type FROM api_calls").fetchone()
        conn.close()
        assert row[1:17] == ("ours", "ab12", "glm-4.5-flash", 10, 5, 15,
                             "zai", 0, 0, 0, 200, None, 42, 0.0,
                             "flat_rate", "s-1")
        assert row[17] is None  # task_type NULL — nothing guessed
