"""TDD test for the routstr sold-canary tag (Phase B).

Gate 1 (TDD): these tests are written FIRST, observed FAILING, then the
implementation (src/routstr_sold_canary.py) is added to make them pass.

Scope: prove that a routstr-sourced request, once labelled, produces an
X-Priority: sold header set AND one canary decision-log line (JSONL) and
optionally a caller_class='sold' row in the decision tables. This is the
shadow canary from ADR-007 + the routstr-serving-lane plan (task T-B) -
it labels and logs, never changes routing.
"""
import json
import os
import tempfile

from src.routstr_sold_canary import (
    CANARY_KEY,
    SOLD_PRIORITY_HEADER,
    SOLD_PRIORITY_VALUE,
    _extract_model,
    append_sold_canary,
    sold_headers,
)


def _mktmp():
    d = tempfile.mkdtemp(prefix="soldcanary-")
    return d


# ── sold_headers() ──────────────────────────────────────────────────────────
def test_sold_headers_inject_priority_sold():
    out = sold_headers({})
    assert out[SOLD_PRIORITY_HEADER] == SOLD_PRIORITY_VALUE


def test_sold_headers_preserves_task_header():
    out = sold_headers({"X-Task-Type": "routstrd_sale", "X-Something": "keep"})
    assert out.get("X-Task-Type") == "routstrd_sale"
    assert out.get("X-Something") == "keep"
    assert out.get("X-Priority") == "sold"


def test_sold_headers_strips_hop_headers():
    out = sold_headers({
        "Host": "evil.example",
        "Authorization": "Bearer sk-secret",
        "Content-Length": "42",
        "Connection": "keep-alive",
        "X-Custom": "kept",
    })
    assert "Host" not in out and "Authorization" not in out
    assert "Content-Length" not in out and "Connection" not in out
    assert out.get("X-Custom") == "kept"


def test_sold_headers_none_input():
    out = sold_headers(None)
    assert out[SOLD_PRIORITY_HEADER] == SOLD_PRIORITY_VALUE


# ── append_sold_canary() ────────────────────────────────────────────────────
def test_append_sold_canary_writes_jsonl(tmp_path):
    lp = str(tmp_path / "canary.jsonl")
    res = append_sold_canary(lp, model="glm-5.2", status=200)
    assert res["wrote"] is True
    with open(lp, encoding="utf-8") as fh:
        line = json.loads(fh.readline())
    assert line["canary"] == CANARY_KEY
    assert line["caller_class"] == "sold"
    assert line["lane"] == "routstr"
    assert line["model"] == "glm-5.2"
    assert line["status"] == 200


def test_append_sold_canary_never_raises_on_bad_path():
    res = append_sold_canary("/nonexistent/root/canary.jsonl", model="x")
    assert res["wrote"] is False
    assert "error" in res


def test_append_sold_canary_no_model_no_status_ok():
    lp = os.path.join(_mktmp(), "c.jsonl")
    res = append_sold_canary(lp)
    assert res["wrote"] is True
    with open(lp, encoding="utf-8") as fh:
        row = json.loads(fh.readline())
    assert row["canary"] == CANARY_KEY
    assert row["caller_class"] == "sold"


# ── _extract_model() ─────────────────────────────────────────────────────────
def test_extract_model_from_chat_body():
    assert _extract_model('{"model":"glm-5.2","messages":[]}') == "glm-5.2"


def test_extract_model_empty_or_bad_returns_none():
    assert _extract_model(None) is None
    assert _extract_model("") is None
    assert _extract_model("{not json") is None


# ── log_sold_decision() (decision-tables mirror) ────────────────────────────
def test_log_sold__db_is_none_writes_false():
    from src.routstr_sold_canary import log_sold_decision
    assert log_sold_decision(None, model="x")["wrote"] is False


# ── build_calibr() (calibr.json canary rollup — P0-4 scope) ─────────────────
def _mklog(tmp_path, rows):
    """Write canary JSONL rows to a tmp file; return the path."""
    import time as _t
    lp = str(tmp_path / "canary.jsonl")
    with open(lp, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return lp


def _mkdb(tmp_path):
    """Create a decision DB with api_calls + routing_live_decisions tables."""
    import sqlite3
    dbp = str(tmp_path / "zai_usage.db")
    con = sqlite3.connect(dbp)
    con.execute("CREATE TABLE api_calls (id INTEGER PRIMARY KEY, ts REAL, key_name TEXT, "
                "task_type TEXT, model TEXT, status_code INTEGER)")
    con.execute("CREATE TABLE routing_live_decisions (id INTEGER PRIMARY KEY, ts REAL, "
                "live_provider TEXT, live_model TEXT, caller_class TEXT, reason TEXT)")
    con.commit()
    return dbp, con


def test_build_calibr_basic_shape(tmp_path):
    from src.routstr_sold_canary import build_calibr, CALIBR_KEY
    import time
    now = time.time()
    lp = _mklog(tmp_path, [
        {"canary": "routstr_sold_canary", "ts": now, "caller_class": "sold",
         "lane": "routstr", "model": "glm-5.2", "status": 200},   # chat request
        {"canary": "routstr_sold_canary", "ts": now, "caller_class": "sold",
         "lane": "routstr", "status": 200},                        # probe / no model
    ])
    out = str(tmp_path / "calibr.json")
    res = build_calibr(log_path=lp, db_path=None, out_path=out, lookback_hours=1.0)
    assert res["canary"] == CALIBR_KEY
    assert res["mode"] == "shadow"
    assert res["routing_changed"] is False
    assert res["requests_total"] == 2
    assert res["sold_chat_requests"] == 1
    assert res["probe_or_other"] == 1
    assert res["requests_by_status"] == {"200": 2}
    # file written and round-trips
    with open(out, encoding="utf-8") as fh:
        assert json.load(fh) == res


def test_build_calibr_reads_db_for_sold_rows_and_routes(tmp_path):
    from src.routstr_sold_canary import build_calibr
    import time
    now = time.time()
    lp = _mklog(tmp_path, [
        {"canary": "routstr_sold_canary", "ts": now, "caller_class": "sold",
         "lane": "routstr", "model": "glm-5.2", "status": 200},
    ])
    dbp, con = _mkdb(tmp_path)
    con.execute("INSERT INTO api_calls (ts, key_name, task_type, model, status_code) "
                "VALUES (?,?,?,?,?)", (now, "ours", "routstrd_sale", "glm-5.2", 200))
    con.execute("INSERT INTO routing_live_decisions (ts, live_provider, live_model, "
                "caller_class, reason) VALUES (?,?,?,?,?)",
                (now, "routstr", "glm-5.2", "sold", "routed_routstr"))
    con.commit()
    out = str(tmp_path / "calibr.json")
    res = build_calibr(log_path=lp, db_path=dbp, out_path=out, lookback_hours=1.0)
    assert res["sold_decision_rows"] == 1
    assert res["would_be_routes"] == {"ours": 1}
    con.close()


def test_build_calibr_missing_log_and_db_yields_zeros(tmp_path):
    from src.routstr_sold_canary import build_calibr
    out = str(tmp_path / "calibr.json")
    res = build_calibr(log_path="/nonexistent/nope/canary.jsonl", db_path=None,
                       out_path=out, lookback_hours=24.0)
    assert res["requests_total"] == 0
    assert res["sold_decision_rows"] == 0
    assert res["would_be_routes"] == {}
    assert res["routing_changed"] is False
    assert os.path.exists(out)


def test_build_calibr_excludes_rows_outside_lookback(tmp_path):
    from src.routstr_sold_canary import build_calibr
    import time
    now = time.time()
    lp = _mklog(tmp_path, [
        {"canary": "routstr_sold_canary", "ts": now, "caller_class": "sold",
         "lane": "routstr", "model": "glm-5.2", "status": 200},
        {"canary": "routstr_sold_canary", "ts": now - 7200.0, "caller_class": "sold",
         "lane": "routstr", "status": 200},   # 2h old, outside 1h lookback
    ])
    out = str(tmp_path / "calibr.json")
    res = build_calibr(log_path=lp, db_path=None, out_path=out, lookback_hours=1.0)
    assert res["requests_total"] == 1
    assert res["sold_chat_requests"] == 1