#!/usr/bin/env python3
"""routstr_sold_canary.py - routstr serving-lane canary tag (Phase B, ADR-007).

Wires customer requests from the routstr public node through the T470 flat
router while staying in SHADOW canary mode: every routstr-sourced request is
labelled X-Priority: sold at the entry and one canary decision-log line is
emitted per request. This NEVER alters routing decisions (Phase B) - it only
records the would-be sold route so the MARGIN / Gate-1 readiness feed can
observe that sold traffic is reaching the router.

A few small, pure, testable helpers:

  * sold_headers(orig) -> dict
      Header map for a forwarded request, with X-Priority: sold injected and
      the hop-scoped headers (Host/Authorization) preserved handling.

  * append_sold_canary(log_path, *, ts, model, status) -> dict
      Appends one JSON-lines decision record for a sold request. Never
      raises; on failure returns {"wrote": False, "error": ...} so a canary
      logging glitch can never take down the request path.

  * log_sold_decision(db_path, *, model) -> dict
      Mirrors one sold decision into routing_live_decisions with
      caller_class='sold' (the row the P&L / Gate-1 readiness feed reads).

Plus the P0-4 canary calibration snapshot:

  * build_calibr(log_path, db_path, out_path, lookback_hours) -> dict
      Rolls the canary JSONL + decision DB into calibr.json
      (~/.hermes/bot/routstr_sold_calibr.json): shadow-mode assertion,
      request counts by status, sold chat vs probe split, sold decision rows
      and would-be routes. Run with `--calibr` on the CLI.

The canary line is also mirrored into the zai_usage decision tables via
routing_gate1.log_live_decision(caller_class=CALLER_CLASS_SOLD) when a DB
path is provided, so the routstr readiness checker and P&L collector can
observe sold traffic without new plumbing.
"""
from __future__ import annotations

import json
import os
import time

SOLD_PRIORITY_HEADER = "X-Priority"
SOLD_PRIORITY_VALUE = "sold"
CANARY_KEY = "routstr_sold_canary"
CALIBR_KEY = "routstr_sold_calibr"
CALIBR_MODE_SHADOW = "shadow"
_DEFAULT_LOG = os.path.expanduser("~/.hermes/bot/routstr_sold_canary.jsonl")
_DEFAULT_CALIBR = os.path.expanduser("~/.hermes/bot/routstr_sold_calibr.json")
_DEFAULT_LOOKBACK_HOURS = 48.0


def sold_headers(orig_headers: "dict[str, str] | None" = None) -> "dict[str, str]":
    """Return the forwarding header map with X-Priority: sold injected.

    Keeps values of ``orig_headers`` (lower-cased keys) except Host, which the
    hop rewrites to the router target. The ``X-Priority`` key is set to ``sold``.
    ``X-Task-Type`` set elsewhere (tag-sidecar) is preserved so both the routstr
    attribution and the canary label travel together.

    Pure and deterministic - unit-testable without any network/DB.
    """
    headers: "dict[str, str]" = {}
    if orig_headers:
        for k, v in orig_headers.items():
            lk = k.lower()
            if lk in ("host", "authorization", "content-length", "connection",
                      "transfer-encoding", "x-priority"):
                continue
            headers[k] = v
    headers[SOLD_PRIORITY_HEADER] = SOLD_PRIORITY_VALUE
    return headers


def _extract_model(body: "str | bytes | None") -> "str | None":
    """Pull the ``model`` field out of a chat-completions JSON body.

    Returns None (never raises) when the body is empty or not parseable -
    the canary line is still written, just without a model annotation.
    """
    if not body:
        return None
    try:
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        payload = json.loads(body)
        m = payload.get("model")
        return str(m) if m else None
    except Exception:
        return None


def append_sold_canary(log_path: str, *, ts: "float | None" = None,
                       model: "str | None" = None, status: "int | None" = None) -> dict:
    """Append one JSON-lines canary decision record for a sold request.

    Record shape:
        {"canary": "routstr_sold_canary", "ts": <unix>, "model": ..., "status": ...,
         "caller_class": "sold", "lane": "routstr"}

    Never raises. Missing/unwritable log path yields {"wrote": False, ...}.
    """
    row = {
        "canary": CANARY_KEY,
        "ts": ts if ts is not None else time.time(),
        "caller_class": "sold",
        "lane": "routstr",
    }
    if model is not None:
        row["model"] = model
    if status is not None:
        row["status"] = status
    try:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
            fh.flush()
        return {"wrote": True, "path": log_path}
    except Exception as exc:  # canary must never break the request path
        return {"wrote": False, "error": str(exc)}


def log_sold_decision(db_path: "str | None", *, model: "str | None" = None) -> dict:
    """Mirror one sold decision into the decision tables (caller_class=sold).

    Uses routing_gate1.log_live_decision so the P&L / Gate-1 readiness feed can
    read sold traffic through load_live_decisions(caller_class='sold'). Never
    raises; returns {"wrote": False, "error": ...} when db_path is None or the
    write fails.
    """
    if not db_path:
        return {"wrote": False, "error": "no db_path"}
    try:
        from src import routing_gate1  # repo layout: ~/merchant-routing-engine/src
        routing_gate1.log_live_decision(
            db_path=db_path,
            provider="routstr",
            model=model,
            caller_class=routing_gate1.CALLER_CLASS_SOLD,
            reason="sold_canary",
        )
        return {"wrote": True, "db": db_path}
    except Exception as exc:
        return {"wrote": False, "error": str(exc)}


# ── calibr.json rollup (P0-4 deliverable) ────────────────────────────────────

def _read_canary_rows(log_path: str) -> "list[dict]":
    """Tolerant read of the canary JSONL: bad lines are skipped, never raise."""
    rows: "list[dict]" = []
    try:
        with open(log_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        rows.append(obj)
                except Exception:
                    continue
    except Exception:
        return []
    return rows


def _sold_db_counts(db_path: "str | None", since_ts: float) -> dict:
    """Query sold-class evidence from the decision DB, fail-safe to zeros.

    Returns {"sold_decision_rows": int, "would_be_routes": {key_name: count}}.
    would_be_routes = the upstream provider keys that actually served
    routstr-sold chat calls (task_type='routstrd_sale') in the window. In
    shadow mode routing is never altered, so the route each sold request took
    IS the route the router would have taken — hence "would-be routes".
    """
    zero = {"sold_decision_rows": 0, "would_be_routes": {}}
    if not db_path:
        return zero
    try:
        import sqlite3
        conn = sqlite3.connect(db_path, timeout=5)
        rows = conn.execute(
            "SELECT COUNT(*) FROM routing_live_decisions "
            "WHERE caller_class = ? AND ts >= ?",
            ("sold", since_ts),
        ).fetchone()
        sold_rows = int(rows[0]) if rows else 0
        routes = {}
        try:
            for key_name, cnt in conn.execute(
                "SELECT key_name, COUNT(*) FROM api_calls "
                "WHERE task_type = 'routstrd_sale' AND ts >= ? GROUP BY key_name",
                (since_ts,),
            ):
                routes[str(key_name)] = int(cnt)
        except Exception:
            routes = {}
        conn.close()
        return {"sold_decision_rows": sold_rows, "would_be_routes": routes}
    except Exception:
        return zero


def build_calibr(log_path: "str | None" = None, db_path: "str | None" = None,
                 out_path: "str | None" = None,
                 lookback_hours: float = _DEFAULT_LOOKBACK_HOURS) -> dict:
    """Build (and write) the canary calibr.json — the P0-4 shadow-canary state.

    Rolls up the canary JSONL + the decision DB into one machine-readable
    calibration snapshot the router/readiness checks can compare against:

      * mode: "shadow" — this canary NEVER alters routing (asserted by
        routing_changed: false on every snapshot);
      * requests_total / requests_by_status — every tunnel request observed;
      * sold_chat_requests — requests that carried a chat-completions model
        (true routstr-sold inference); probe/health GETs have no model and are
        counted separately as probe_or_other so they never pollute the
        sold-traffic signal;
      * sold_decision_rows — caller_class='sold' rows mirrored into
        routing_live_decisions (the "sold-class request row" the P&L and
        Gate-1 readiness feed read);
      * would_be_routes — upstream provider keys that served sold calls in the
        window (actual route under shadow == would-be route).

    Never raises; a missing log/DB yields zeroed counters. Writes the snapshot
    to out_path (default ~/.hermes/bot/routstr_sold_calibr.json) and returns
    the dict.
    """
    import time as _time
    log_path = log_path or _DEFAULT_LOG
    out_path = out_path or _DEFAULT_CALIBR
    now = _time.time()
    since = now - max(0.0, lookback_hours) * 3600.0

    rows = _read_canary_rows(log_path)
    windowed = [r for r in rows if isinstance(r.get("ts"), (int, float)) and r["ts"] >= since]
    by_status: "dict[str, int]" = {}
    chat = 0
    for r in windowed:
        st = str(r.get("status", ""))
        by_status[st] = by_status.get(st, 0) + 1
        if r.get("model"):
            chat += 1

    db_counts = _sold_db_counts(db_path, since)

    calibr = {
        "canary": CALIBR_KEY,
        "mode": CALIBR_MODE_SHADOW,
        "priority_header": SOLD_PRIORITY_HEADER + ": " + SOLD_PRIORITY_VALUE,
        "lane": "routstr",
        "routing_changed": False,  # shadow invariant: canary never routes
        "generated_at": now,
        "lookback_hours": float(lookback_hours),
        "window_started_ts": since,
        "window_ended_ts": now,
        "requests_total": len(windowed),
        "requests_by_status": by_status,
        "sold_chat_requests": chat,
        "probe_or_other": len(windowed) - chat,
        "sold_decision_rows": db_counts["sold_decision_rows"],
        "would_be_routes": db_counts["would_be_routes"],
        "log_file": log_path,
    }
    try:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(calibr, fh, indent=2)
            fh.write("\n")
    except Exception:
        pass
    return calibr


def main(argv=None):
    """CLI for manual canary probe: append a test sold-canary line.

    ``--calibr`` builds the calibr.json snapshot instead (P0-4 deliverable):
        python3 -m src.routstr_sold_canary --calibr [--calibr-db zai_usage.db]
    """
    import argparse
    ap = argparse.ArgumentParser(description="routstr sold canary (test write)")
    ap.add_argument("--log", default=_DEFAULT_LOG)
    ap.add_argument("--model", default=None)
    ap.add_argument("--db", default=None, help="zai_usage.db path for decision mirror")
    ap.add_argument("--calibr", action="store_true",
                    help="build calibr.json snapshot and exit")
    ap.add_argument("--calibr-out", default=None, help="calibr.json output path")
    ap.add_argument("--calibr-db", default=None,
                    help="decision DB path for calibr (default: none -> zeros)")
    a = ap.parse_args(argv)
    if a.calibr:
        print(json.dumps(build_calibr(log_path=a.log, db_path=a.calibr_db,
                                      out_path=a.calibr_out), indent=2))
        return 0
    print(append_sold_canary(a.log, model=a.model, status=200))
    if a.db:
        print(log_sold_decision(a.db, model=a.model))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())