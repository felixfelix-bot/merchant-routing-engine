#!/usr/bin/env python3
"""src/routing_gate1.py - routstr-readiness-check Gate 1 data feed.

ADR-007 gate 1 ("router MAPE < 15%") reads *live* MAPE off routing decision
data instead of an idle static gate. This module is that feed:

  * ensure_decision_schema() idempotently creates/upgrades the two decision
    tables so each carries a caller_class column (default "internal");
  * log_live_decision() persists a routed decision with its caller_class so
    the live logging on decision data is complete;
  * load_live_decisions() reads decision rows back (caller-class filtered);
  * compute_live_mape() turns (predicted, realized) cost series into the
    weighted mean-absolute-percentage-error that Gate 1 (<15%) consumes;
  * exhaust_trust() validates a reported hours-to-exhaustion is internally
    consistent -- the trustability check the sold-safety gate depends on;
  * gate1_feed() + a CLI produce the machine-readable Gate 1 verdict the
    routstr-readiness checker calls.

Never raises on DB trouble: the feed degrades to the "UNVERIFIED" (n=0,
mape=null) state the readiness checker already knows to leave silent.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

CALLER_CLASS_INTERNAL = "internal"
CALLER_CLASS_SOLD = "sold"
GATE1_MAPE_THRESHOLD_PCT = 15.0
_DEFAULT_LOOKBACK_HOURS = 48.0


_LIVE_DDL = """CREATE TABLE IF NOT EXISTS routing_live_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, live_provider TEXT, live_model TEXT, shadow_provider TEXT, shadow_model TEXT, shadow_cost REAL, live_cost REAL, tokens INTEGER, agree INTEGER, reason TEXT, pace_mults TEXT, caller_class TEXT DEFAULT 'internal')"""
_SHADOW_DDL = """CREATE TABLE IF NOT EXISTS flat_router_shadow_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, best_key_choice TEXT, flat_router_top TEXT, flat_router_top_cost REAL, agreement INTEGER, model TEXT, candidate_list TEXT, caller_class TEXT DEFAULT 'internal')"""


def _table_exists(conn, name):
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None
    except Exception:
        return False


def _safe_add(conn, table, col_sql):
    """Add a column only if absent (idempotent)."""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(" + table + ")")}
    except Exception:
        cols = set()
    col = col_sql.split()[0]
    if col in cols:
        return
    try:
        conn.execute("ALTER TABLE " + table + " ADD COLUMN " + col_sql)
    except Exception:
        pass


def ensure_decision_schema(db_path):
    """Create/upgrade the decision tables with the caller_class column."""
    conn = sqlite3.connect(db_path, timeout=5)
    if _table_exists(conn, "routing_live_decisions"):
        _safe_add(conn, "routing_live_decisions", "caller_class TEXT DEFAULT 'internal'")
    else:
        conn.execute(_LIVE_DDL)
    if _table_exists(conn, "flat_router_shadow_decisions"):
        _safe_add(conn, "flat_router_shadow_decisions", "caller_class TEXT DEFAULT 'internal'")
    else:
        conn.execute(_SHADOW_DDL)
    try:
        conn.commit()
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass



def _column_set(conn, table):
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(" + table + ")")}
    except Exception:
        return set()


def log_live_decision(*, db_path, provider, model=None,
                      caller_class=CALLER_CLASS_INTERNAL,
                      predicted_cost=None, reason=""):
    """Persist one routed decision (with caller_class). Never raises."""
    sql = "INSERT INTO routing_live_decisions (ts, live_provider, live_model, live_cost, agree, reason, caller_class) VALUES (?,?,?,?,?,?,?)"
    try:
        ensure_decision_schema(db_path)
    except Exception:
        return
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        conn.execute(sql, (time.time(), provider, model, predicted_cost, 1,
                        ("routed_" + str(provider)), caller_class))
        conn.commit()
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass



def load_live_decisions(db_path, caller_class=None, lookback_hours=_DEFAULT_LOOKBACK_HOURS):
    """Read live decision rows (caller-class filtered if given).

    Returns a list of dicts with provider/model/predicted_cost/ts/caller_class.
    Never raises -- an unavailable DB yields [] (fail-safe).
    """
    out = []
    since = time.time() - lookback_hours * 3600.0
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        q = ("SELECT live_provider, live_model, live_cost, ts, caller_class FROM routing_live_decisions WHERE ts >= ?")
        args = [since]
        if caller_class is not None:
            q += " AND caller_class = ?"
            args.append(caller_class)
        cur = conn.execute(q, args)
        for row in cur:
            out.append({"provider": row[0], "model": row[1], "predicted_cost": row[2],
                        "ts": row[3], "caller_class": row[4]})
        conn.close()
    except Exception:
        pass
    return out


def compute_live_mape(series):
    """Weighted MAPE over per-model (predicted, realized) [$/M] series.

    Each aligned (pred, real) pair is one observation. Returns the mean
    absolute percentage error weighted over observations (a well-measured
    model is not diluted or amplified by a thin one). Zero-realized and
    None-predicted points are dropped. Returns None when nothing useable
    (caller treats that as UNVERIFIED, not 0%).
    """
    if not series:
        return None
    total = 0.0
    n = 0
    for preds, reals in series.values():
        # accept either ([pred...],[real...]) lists or a single (pred, real) pair
        if isinstance(preds, (int, float)) or preds is None:
            preds = [preds]
        if isinstance(reals, (int, float)) or reals is None:
            reals = [reals]
        for p, r in zip(preds, reals):
            if r is None or r <= 0.0:
                continue
            if p is None:
                continue
            try:
                total += abs(float(p) - float(r)) / float(r)
                n += 1
            except (TypeError, ValueError):
                continue
    if n == 0:
        return None
    return total / n * 100.0


def exhaust_trust(remaining_tokens, burn_rate_tph, exhausts_in_hours, tol_frac=0.10):
    """Is a reported hours-to-exhaustion forecast trustable?

    Trustable iff internally consistent with its inputs: the forecast equals
    remaining_tokens / burn_rate_tph within tol_frac, and both inputs are sane.
    Returns (ok, note).
    """
    try:
        if remaining_tokens <= 0:
            return False, "remaining_tokens must be positive"
        if burn_rate_tph <= 0:
            return False, "burn_rate_tph must be positive"
        computed = float(remaining_tokens) / float(burn_rate_tph)
        if abs(computed - float(exhausts_in_hours)) <= tol_frac * max(1.0, float(exhausts_in_hours)):
            return True, "consistent"
        return False, "inconsistent"
    except Exception:
        return False, "malformed prediction"



def _realized_by_model(db_path, lookback_hours=48.0):
    """Realized $/M per model from api_calls within the lookback window."""
    realized = {}
    since = time.time() - lookback_hours * 3600.0
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        q = ("SELECT model, SUM(cost_usd), SUM(total_tokens) FROM api_calls WHERE ts >= ? AND status_code = 200 AND total_tokens > 0 GROUP BY model")
        for model, cost, toks in conn.execute(q, (since,)):
            if cost is None or toks is None or toks <= 0:
                continue
            realized[model] = cost / (toks / 1e6)   # $/M
        conn.close()
    except Exception:
        pass
    return realized


def gate1_feed(db_path, caller_class=None, lookback_hours=_DEFAULT_LOOKBACK_HOURS):
    """Compute the Gate 1 (MAPE) verdict from live decision data.

    Reads the caller-class decision rows, pairs each decision's predicted
    cost with a realized cost for the same model, and reports weighted
    MAPE plus sample count -- the exact numbers the readiness checker
    consumes. Returns a dict with a "gate1" key.
    """
    try:
        ensure_decision_schema(db_path)
    except Exception:
        pass
    series = {}
    realized = _realized_by_model(db_path, lookback_hours)
    for d in load_live_decisions(db_path, caller_class=caller_class, lookback_hours=lookback_hours):
        m = d["model"] or d["provider"]
        series.setdefault(m, ([], []))
        if d["predicted_cost"] is not None:
            series[m][0].append(d["predicted_cost"])
        if m in realized:
            series[m][1].append(realized[m])
    mape = compute_live_mape(series)
    n = sum(len(v[1]) for v in series.values())
    return {"gate1": {
        "mape_pct": mape,
        "n": n,
        "threshold_pct": GATE1_MAPE_THRESHOLD_PCT,
        "pass": (mape is not None and mape < GATE1_MAPE_THRESHOLD_PCT),
    }}


def _cli_main(argv=None):
    """Minimal CLI: gate1_feed --db <path> --class {internal,sold}."""
    import argparse as _ap
    p = _ap.ArgumentParser(prog="routing_gate1")
    p.add_argument("--db", default=None, help="SQLite path (default ~/.hermes/bot/zai_usage.db)")
    p.add_argument("--caller-class", default=None, choices=(CALLER_CLASS_INTERNAL, CALLER_CLASS_SOLD))
    a = p.parse_args(argv)
    db = a.db or os.path.expanduser("~/.hermes/bot/zai_usage.db")
    print(json.dumps(gate1_feed(db, caller_class=a.caller_class)))
    return 0


def main(argv=None):
    return _cli_main(argv)


if __name__ == "__main__":
    import sys
    sys.exit(main())
