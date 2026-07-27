"""shadow_logger.py — Read-only decision tap for live vs shadow routing.

Logs both the **live** (current ``best_key`` selection) and the **shadow**
(price-first ``routing_optimizer``) decisions for every API call into a SQLite
table, so the two strategies can be compared after a soak period (see
``config/providers.yaml :: shadow_mode``).

Why it exists (ADR-Phase-1):
  Before swapping the production provider-selection logic, we run the new
  optimizer in parallel ("shadow mode") and record what each path *would have*
  picked. After ~48h we answer two questions:

    1. Agreement rate — how often does the optimizer agree with best_key?
    2. Cost comparison — is the optimizer actually cheaper?

Design notes:
  - **Read-only relative to routing.** This logger never chooses a provider; it
    only observes what each strategy decided. It cannot affect the live path.
  - **One persistent connection**, prepared/cached SQL, per-call COMMIT — keeps
    a hot-path ``log_decision()`` under ~1ms (no reconnect, no parse overhead).
  - **Defensive.** ``log_decision`` never raises on null/empty inputs; a missing
    ``ts`` is substituted with ``time.time()`` (honours the table's NOT NULL
    constraint without breaking the caller).
  - Standalone: stdlib only. No numpy, no hermes internals, no provider code —
    matches the separation discipline of the Kalman modules.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import threading
import time
from typing import Optional

# Add parent dir so `from src.xxx import` works when imported from outside
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from src.provider_names import normalize_provider_name

__all__ = ["ShadowLogger"]

# ── Schema (mirrors config/providers.yaml :: shadow_mode.log_table) ───────────

_CREATE_SQL = """\
CREATE TABLE IF NOT EXISTS routing_shadow_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    live_provider TEXT,
    live_model TEXT,
    shadow_provider TEXT,
    shadow_model TEXT,
    shadow_cost REAL,
    live_cost REAL,
    tokens INTEGER,
    agree INTEGER,
    reason TEXT
);
"""

# Single prepared statement, bound positionally. Column order MUST match.
_INSERT_SQL = (
    "INSERT INTO routing_shadow_decisions "
    "(ts, live_provider, live_model, shadow_provider, shadow_model, "
    " shadow_cost, live_cost, tokens, agree, reason) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);"
)


class ShadowLogger:
    """Read-only tap. Logs both live (best_key) and shadow (routing_optimizer)
    decisions for every API call. Writes to SQLite. <1ms overhead."""

    def __init__(self, db_path: str = "~/.hermes/bot/zai_usage.db"):
        """Open DB, create table if not exists.

        Args:
            db_path: SQLite file path. ``~`` is expanded. Parent directories
                are created. Defaults to the production usage DB named in
                ``config/providers.yaml :: shadow_mode.db_path``.
        """
        self.db_path = os.path.expanduser(db_path)
        parent = os.path.dirname(self.db_path)
        os.makedirs(parent or ".", exist_ok=True)

        # check_same_thread=False: the proxy may call from worker threads.
        # We guard every DB access with _lock to ensure thread-safe writes.
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        # Durable-enough, fast defaults for a low-volume decision log.
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute(_CREATE_SQL)
        self._conn.commit()

    # ── Write path ───────────────────────────────────────────────────────

    def log_decision(
        self,
        ts,
        live_provider,
        live_model,
        shadow_provider,
        shadow_model,
        shadow_cost,
        tokens,
        reason: Optional[str] = "",
        live_cost: Optional[float] = None,
    ) -> None:
        """Insert a row into ``routing_shadow_decisions``.

        Computes ``agree = 1 if live_provider == shadow_provider else 0``.

        Args:
            ts: Event timestamp (epoch seconds). If ``None``, ``time.time()``
                is substituted (avoids violating the NOT NULL constraint).
            live_provider / live_model: the provider/model the live
                ``best_key`` path actually selected.
            shadow_provider / shadow_model: the provider/model the price-first
                optimizer *would have* selected.
            shadow_cost: effective cost the optimizer used for its pick.
            tokens: token count for this call (0 if unknown).
            reason: free-text annotation (default ``""``).
            live_cost: optional effective cost of the live pick, enabling the
                cost comparison. ``None`` → stored as SQL NULL.

        Never raises on null/empty inputs — logging must not break the hot
        path.
        """
        if ts is None:
            ts = time.time()

        # Normalize provider names to canonical form before storing.
        # This ensures the DB always has consistent names regardless of
        # what the live proxy or shadow optimizer sends.
        live_provider = normalize_provider_name(live_provider)
        shadow_provider = normalize_provider_name(shadow_provider)

        agree = 1 if live_provider == shadow_provider else 0
        with self._lock:
            self._conn.execute(
                _INSERT_SQL,
                (
                    float(ts),
                    live_provider,
                    live_model,
                    shadow_provider,
                    shadow_model,
                    shadow_cost,
                    live_cost,
                    tokens,
                    agree,
                    reason if reason is not None else "",
                ),
            )
            self._conn.commit()

    # ── Read paths ───────────────────────────────────────────────────────

    def get_agreement_rate(self, since_ts: Optional[float] = None) -> float:
        """Fraction (0–1) of decisions where live_provider == shadow_provider.

        Args:
            since_ts: If given, only count rows with ``ts >= since_ts``.

        Returns ``0.0`` when there are no qualifying rows — an empty table is
        reported as 0% agreement, never 1.0, so it can't be mistaken for
        perfect agreement.
        """
        if since_ts is None:
            with self._lock:
                row = self._conn.execute(
                    "SELECT AVG(agree), COUNT(*) FROM routing_shadow_decisions;"
                ).fetchone()
        else:
            with self._lock:
                row = self._conn.execute(
                    "SELECT AVG(agree), COUNT(*) FROM routing_shadow_decisions "
                    "WHERE ts >= ?;",
                    (float(since_ts),),
                ).fetchone()
        avg, count = row
        if not count or avg is None:
            return 0.0
        return float(avg)

    def get_cost_comparison(
        self, since_ts: Optional[float] = None
    ) -> tuple[float, float]:
        """Return ``(avg_live_cost, avg_shadow_cost)`` since ``since_ts``.

        Helps validate: is the optimizer cheaper than ``best_key()``? Rows with
        NULL costs are ignored by SQL ``AVG``. Returns ``(0.0, 0.0)`` on empty
        data so callers can compare unconditionally.
        """
        if since_ts is None:
            with self._lock:
                row = self._conn.execute(
                    "SELECT AVG(live_cost), AVG(shadow_cost), COUNT(*) "
                    "FROM routing_shadow_decisions;"
                ).fetchone()
        else:
            with self._lock:
                row = self._conn.execute(
                    "SELECT AVG(live_cost), AVG(shadow_cost), COUNT(*) "
                    "FROM routing_shadow_decisions WHERE ts >= ?;",
                    (float(since_ts),),
                ).fetchone()
        live_avg, shadow_avg, count = row
        if not count:
            return (0.0, 0.0)
        return (float(live_avg or 0.0), float(shadow_avg or 0.0))

    def get_count(self, since_ts: Optional[float] = None) -> int:
        """Total number of logged decisions (optionally since a timestamp)."""
        if since_ts is None:
            with self._lock:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM routing_shadow_decisions;"
                ).fetchone()
        else:
            with self._lock:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM routing_shadow_decisions WHERE ts >= ?;",
                    (float(since_ts),),
                ).fetchone()
        return row[0] if row else 0

    # ── Lifecycle ────────────────────────────────────────────────────────

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def close(self) -> None:
        """Close the underlying connection. Thread-safe and idempotent."""
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    def __del__(self):
        # Guard against partial init — _lock may not exist if __init__ failed
        if hasattr(self, '_lock'):
            self.close()
