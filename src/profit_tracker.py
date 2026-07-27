"""profit_tracker.py — Per-request savings tracker for the routing engine.

For every routing decision we record how much we *saved* by picking the
cheapest reachable provider instead of the next-best alternative. In consumer
mode "profit" *is* savings (ADR-12: Profit = traffic x margin; for consumer
mode, margin = savings vs. the price we would otherwise have paid).

Each row lands in the ``routing_profit`` table of ``zai_usage.db``:

    provider_used         — who we routed to
    effective_price       — $/M tokens we actually pay (from pricing_engine)
    next_best_price       — $/M tokens of the next-cheapest alternative (NULL ok)
    savings_per_1m        — next_best_price - effective_price (>= 0; 0 if unknown)
    estimated_tokens      — token count for this call (0 if unknown)
    estimated_savings_usd — savings_per_1m * estimated_tokens / 1e6
    is_peak_hour          — 1 if the z.ai peak window was active, else 0
    mode                  — 'consumer' (default) | 'arbitrage' | 'dual'

Design notes:
  - **Fire-and-forget writes.** ``record_decision`` computes the derived fields
    inline (so the caller's exact values are captured) then hands the row to a
    single background *daemon* thread that drains a queue and does the SQLite
    INSERTs. The hot path never blocks on disk I/O — this is the "runs in a
    daemon thread" behaviour requested in the spec, layered on the same DB
    conventions as ``ShadowLogger`` (WAL, synchronous=NORMAL, one persistent
    connection, lock-guarded).
  - **Reads are consistent.** The read methods (``get_daily_summary``,
    ``get_weekly_trend``) call ``flush()`` first so any queued writes are
    durable before aggregation, then query under the same lock the writer uses.
  - **Defensive.** ``record_decision`` never raises: a bad price/token is
    coerced, a NULL ``next_best_price`` yields ``savings_per_1m = 0`` (we never
    claim savings we can't prove), and a writer-thread exception is logged to
    stderr and swallowed so the consumer keeps routing.
  - Standalone: stdlib only. No numpy, no hermes internals, no provider code —
    matches the separation discipline of the Kalman / pricing modules.

Phase 2.3 of the merchant routing engine. Feeds the dashboard's
"How much money has the optimizer saved us?" panel.
"""
from __future__ import annotations

import os
import queue
import math
import sqlite3
import sys
import threading
import time
from datetime import date, datetime, timezone, timedelta
from typing import Optional, Union

__all__ = ["ProfitTracker"]

# ── Schema (mirrors the spec in the task body) ───────────────────────────────

_CREATE_SQL = """\
CREATE TABLE IF NOT EXISTS routing_profit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    provider_used TEXT NOT NULL,
    effective_price REAL NOT NULL,
    next_best_price REAL,
    savings_per_1m REAL,
    estimated_tokens INTEGER,
    estimated_savings_usd REAL,
    is_peak_hour INTEGER,
    mode TEXT DEFAULT 'consumer'
);
"""

# Column order MUST match the tuple built by _build_payload.
_INSERT_SQL = (
    "INSERT INTO routing_profit "
    "(ts, provider_used, effective_price, next_best_price, savings_per_1m, "
    " estimated_tokens, estimated_savings_usd, is_peak_hour, mode) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);"
)

# Queue item kinds. A row is ("row", tuple); a flush is ("flush", Event).
# FIFO ordering is what makes flush() correct: every row enqueued before a
# flush token is dequeued, inserted and committed before the token is reached.
_QUEUE_TIMEOUT = 0.5  # how long the writer blocks on get() before re-checking stop

DateLike = Optional[Union[str, date]]


class ProfitTracker:
    """Per-request savings tracker. Writes are async via a daemon thread.

    Args:
        db_path: SQLite file path. ``~`` is expanded, parent dirs created.
            Defaults to the production usage DB (same one ``ShadowLogger``
            uses) so the dashboard reads a single file.
    """

    def __init__(self, db_path: str = "~/.hermes/bot/zai_usage.db"):
        self.db_path = os.path.expanduser(db_path)
        parent = os.path.dirname(self.db_path)
        os.makedirs(parent or ".", exist_ok=True)

        # check_same_thread=False: the proxy calls record_decision from worker
        # threads; the writer daemon is yet another thread. _lock serialises
        # every DB touch so only one statement runs at a time.
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute(_CREATE_SQL)
        self._conn.commit()

        # Pending writes + lifecycle for the daemon writer.
        self._queue: "queue.Queue" = queue.Queue()
        self._stopping = threading.Event()
        self._writer = threading.Thread(
            target=self._writer_loop, name="ProfitTracker-writer", daemon=True
        )
        self._writer.start()

    # ── Write path ───────────────────────────────────────────────────────

    def record_decision(
        self,
        provider: str,
        effective_price: float,
        next_best_price: Optional[float] = None,
        tokens: int = 0,
        is_peak: bool = False,
        mode: str = "consumer",
        ts: Optional[float] = None,
    ) -> None:
        """Record one routing decision and its derived savings.

        Computes the savings fields inline (capturing the caller's exact
        values), then enqueues the row for the daemon writer. Returns
        immediately — the INSERT happens off the hot path.

        Derived fields:
            savings_per_1m = next_best_price - effective_price   (>= 0)
                0 when next_best_price is None or not greater than effective
            estimated_savings_usd = savings_per_1m * tokens / 1e6

        Never raises: bad numeric inputs are coerced, and a NULL
        ``next_best_price`` simply means we cannot prove savings (recorded as
        0). Logging must not break routing.
        """
        row = self._build_payload(
            provider=provider,
            effective_price=effective_price,
            next_best_price=next_best_price,
            tokens=tokens,
            is_peak=is_peak,
            mode=mode,
            ts=ts,
        )
        # Unbounded queue: put_nowait cannot block (only fails on memory
        # exhaustion, which we do not handle here).
        self._queue.put_nowait(("row", row))

    def _build_payload(
        self,
        *,
        provider: str,
        effective_price: float,
        next_best_price: Optional[float],
        tokens: int,
        is_peak: bool,
        mode: str,
        ts: Optional[float],
    ) -> tuple:
        """Build the INSERT row tuple. Pure / side-effect free / never raises."""
        if ts is None:
            ts = time.time()
        # Coerce defensively — a stray None / NaN / inf must not violate
        # NOT NULL (SQLite stores NaN as NULL) or poison an aggregate later.
        try:
            eff = float(effective_price)
            if not math.isfinite(eff):
                eff = 0.0
        except (TypeError, ValueError):
            eff = 0.0
        try:
            toks = int(tokens)
        except (TypeError, ValueError):
            toks = 0

        if next_best_price is None:
            savings_per_1m = 0.0
            nbp_sql: Optional[float] = None
        else:
            try:
                nbp = float(next_best_price)
                if not math.isfinite(nbp):
                    nbp = None
            except (TypeError, ValueError):
                nbp = None
            if nbp is None:
                savings_per_1m = 0.0
                nbp_sql = None
            else:
                # Savings is only ever positive — we never claim we "saved" by
                # being more expensive than the alternative. A tie or a loss
                # rounds to 0.
                savings_per_1m = nbp - eff if nbp > eff else 0.0
                nbp_sql = nbp

        estimated_savings_usd = savings_per_1m * toks / 1_000_000.0

        return (
            float(ts),
            str(provider) if provider is not None else "",
            eff,
            nbp_sql,
            savings_per_1m,
            toks,
            estimated_savings_usd,
            1 if is_peak else 0,
            str(mode) if mode is not None else "consumer",
        )

    def _writer_loop(self) -> None:
        """Daemon: drain the queue, one insert+commit per row, until close().

        Flush tokens are satisfied strictly after every row that preceded them
        (FIFO), so a flush Event firing is a durability guarantee for all rows
        enqueued before it.
        """
        while True:
            try:
                item = self._queue.get(timeout=_QUEUE_TIMEOUT)
            except queue.Empty:
                if self._stopping.is_set():
                    return
                continue

            kind, payload = item
            if kind == "row":
                try:
                    with self._lock:
                        self._conn.execute(_INSERT_SQL, payload)
                        self._conn.commit()
                except Exception as exc:  # pragma: no cover — best-effort
                    self._log_error(exc)
            else:  # "flush"
                # All rows queued before this token are already committed.
                payload.set()

    @staticmethod
    def _log_error(exc: Exception) -> None:
        # A logger import would couple us to the proxy; stderr keeps the module
        # standalone while still leaving a breadcrumb for the operator.
        try:
            print(f"[profit_tracker] write failed: {exc!r}", file=sys.stderr)
        except Exception:
            pass

    def flush(self, timeout: float = 5.0) -> bool:
        """Block until every record_decision() queued so far is durable.

        Read methods call this internally so their aggregates reflect all
        pending writes. Returns True if the drain completed within ``timeout``,
        False on timeout (the writer keeps going regardless).

        Correctness rests on FIFO: we enqueue a flush token *after* our rows,
        and the writer only sets the token's Event once every preceding row has
        been dequeued, inserted and committed.
        """
        if not self._writer.is_alive():
            return self._queue.empty()
        ev = threading.Event()
        self._queue.put_nowait(("flush", ev))
        return ev.wait(timeout)

    # ── Read paths ───────────────────────────────────────────────────────

    def get_daily_summary(self, date_like: DateLike = None) -> dict:
        """Aggregate savings for a single UTC day.

        Args:
            date_like: A ``datetime.date`` or ``YYYY-MM-DD`` string. None →
                today (UTC). Day bucketing uses ``date(ts,'unixepoch')`` so it
                is UTC-aligned, matching the pricing engine's UTC convention.

        Returns a dict with overall totals plus a per-provider breakdown:

            {
              "date": "2026-07-25",
              "total_requests": int,
              "total_savings_usd": float,
              "avg_savings_per_1m": float,
              "by_provider": {
                  "ours": {"requests": int,
                           "savings_usd": float,
                           "avg_savings_per_1m": float},
                  ...
              }
            }

        A day with no rows returns zeros and an empty ``by_provider``.
        ``avg_savings_per_1m`` is request-weighted across providers.
        """
        self.flush()
        day = self._normalize_day(date_like)
        with self._lock:
            per_provider = self._conn.execute(
                "SELECT provider_used, COUNT(*), "
                "       COALESCE(SUM(estimated_savings_usd), 0), "
                "       COALESCE(AVG(savings_per_1m), 0) "
                "FROM routing_profit "
                "WHERE date(ts, 'unixepoch') = ? "
                "GROUP BY provider_used;",
                (day,),
            ).fetchall()

        total_requests = 0
        total_savings = 0.0
        weighted_savings_sum = 0.0
        by_provider: dict[str, dict] = {}
        for prov, cnt, sav_usd, avg_s1m in per_provider:
            cnt = int(cnt or 0)
            sav_usd = float(sav_usd or 0.0)
            avg_s1m = float(avg_s1m or 0.0)
            total_requests += cnt
            total_savings += sav_usd
            weighted_savings_sum += avg_s1m * cnt
            by_provider[prov] = {
                "requests": cnt,
                "savings_usd": sav_usd,
                "avg_savings_per_1m": avg_s1m,
            }

        avg_s1m_overall = (
            weighted_savings_sum / total_requests if total_requests else 0.0
        )
        return {
            "date": day,
            "total_requests": total_requests,
            "total_savings_usd": total_savings,
            "avg_savings_per_1m": avg_s1m_overall,
            "by_provider": by_provider,
        }

    def get_weekly_trend(self) -> list[dict]:
        """Daily summaries for the last 7 UTC days, oldest → newest.

        Returns a list of 7 dicts, one per day, each with the headline numbers
        the trend chart needs (no per-provider breakdown). Days with no rows
        are filled with zeros so the chart always plots 7 points.
        """
        self.flush()
        today = datetime.now(timezone.utc).date()
        days = [today - timedelta(days=i) for i in range(6, -1, -1)]

        with self._lock:
            rows = self._conn.execute(
                "SELECT date(ts, 'unixepoch') AS day, "
                "       COUNT(*), "
                "       COALESCE(SUM(estimated_savings_usd), 0), "
                "       COALESCE(AVG(savings_per_1m), 0) "
                "FROM routing_profit "
                "WHERE date(ts, 'unixepoch') BETWEEN ? AND ? "
                "GROUP BY day;",
                (days[0].isoformat(), days[-1].isoformat()),
            ).fetchall()

        by_day = {r[0]: r for r in rows}
        trend = []
        for d in days:
            iso = d.isoformat()
            r = by_day.get(iso)
            if r is None:
                cnt, sav, avg = 0, 0.0, 0.0
            else:
                cnt = int(r[1] or 0)
                sav = float(r[2] or 0.0)
                avg = float(r[3] or 0.0)
            trend.append(
                {
                    "date": iso,
                    "total_requests": cnt,
                    "total_savings_usd": sav,
                    "avg_savings_per_1m": avg,
                }
            )
        return trend

    # ── Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_day(date_like: DateLike) -> str:
        """Coerce a date arg to a validated YYYY-MM-DD string (UTC today if None)."""
        if date_like is None:
            return datetime.now(timezone.utc).date().isoformat()
        if isinstance(date_like, date):
            return date_like.isoformat()
        s = str(date_like).strip()
        # Validate shape YYYY-MM-DD by round-tripping; raises ValueError on bad
        # input, which is appropriate for an explicit caller-provided date.
        datetime.strptime(s, "%Y-%m-%d")
        return s

    # ── Lifecycle ────────────────────────────────────────────────────────

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def close(self) -> None:
        """Flush pending writes, stop the writer thread, close the DB.

        Thread-safe and idempotent.
        """
        if getattr(self, "_stopping", None) is not None and not self._stopping.is_set():
            self.flush(timeout=2.0)
            self._stopping.set()
            # The writer will exit on its next empty-timeout check (<=0.5s).
            self._writer.join(timeout=2.0)
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    def __del__(self):
        # Guard against partial init — _stopping may not exist if __init__ failed.
        if hasattr(self, "_stopping"):
            try:
                self.close()
            except Exception:
                pass
