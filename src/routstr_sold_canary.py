#!/usr/bin/env python3
"""routstr_sold_canary.py - routstr serving-lane canary tag (Phase B, ADR-007).

Wires customer requests from the routstr public node through the T470 flat
router while staying in SHADOW canary mode: every routstr-sourced request is
labelled X-Priority: sold at the entry and one canary decision-log line is
emitted per request. This NEVER alters routing decisions (Phase B) - it only
records the would-be sold route so the MARGIN / Gate-1 readiness feed can
observe that sold traffic is reaching the router.

Two small, pure, testable helpers:

  * sold_headers(orig) -> dict
      Header map for a forwarded request, with X-Priority: sold injected and
      the hop-scoped headers (Host/Authorization) preserved handling.

  * append_sold_canary(log_path, *, ts, model, status) -> dict
      Appends one JSON-lines decision record for a sold request. Never
      raises; on failure returns {"wrote": False, "error": ...} so a canary
      logging glitch can never take down the request path.

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
_DEFAULT_LOG = os.path.expanduser("~/.hermes/bot/routstr_sold_canary.jsonl")


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


def main(argv=None):
    """CLI for manual canary probe: append a test sold-canary line."""
    import argparse
    ap = argparse.ArgumentParser(description="routstr sold canary (test write)")
    ap.add_argument("--log", default=_DEFAULT_LOG)
    ap.add_argument("--model", default=None)
    ap.add_argument("--db", default=None, help="zai_usage.db path for decision mirror")
    a = ap.parse_args(argv)
    print(append_sold_canary(a.log, model=a.model, status=200))
    if a.db:
        print(log_sold_decision(a.db, model=a.model))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())