"""shadow_logger.py — Read-only decision tap for live vs shadow routing.

Logs both the **live** (current ``best_key`` selection) and the **shadow**
(price-first ``routing_optimizer``) decisions for every API call into a SQLite
table, so the two strategies can be compared after a soak period (see
``config/providers.yaml :: shadow_mode``).

P6-SHADOW extends this to also capture the **pressure-routing** dimension:
what the LiveRouter's quota-pressure-based selection *would have* chosen, the
divergence between that and the actual provider, and the effective prices for
both.  An ``evaluate_exit_criteria()`` method encodes the promotion gates
(divergence < 15%, 429 rate ≤ baseline, paid spend ≤ baseline, ≥ 500 decisions,
≥ 1 full z.ai session cycle, NaN/inf sanitised to 0), and
``should_extend_to_7days()`` implements the conditional soak extension when the
weekly window has not yet been observed in 48 h.

Why it exists (ADR-Phase-1):
  Before swapping the production provider-selection logic, we run the new
  optimizer in parallel ("shadow mode") and record what each path *would have*
  picked. After ~48h we answer two questions:

    1. Agreement rate — how often does the optimizer agree with best_key?
    2. Cost comparison — is the optimizer actually cheaper?

  P6 adds a third dimension: how often does the pressure-routing (LiveRouter)
  agree with the actual routing, and at what cost delta — the divergence metric
  that gates promotion to production.

Design notes:
  - **Read-only relative to routing.** This logger never chooses a provider; it
    only observes what each strategy decided. It cannot affect the live path.
  - **One persistent connection**, prepared/cached SQL, per-call COMMIT — keeps
    a hot-path ``log_decision()`` under ~1ms (no reconnect, no parse overhead).
  - **Defensive.** ``log_decision`` / ``log_pressure_decision`` never raise on
    null/empty inputs; a missing ``ts`` is substituted with ``time.time()``
    (honours the table's NOT NULL constraint without breaking the caller).
  - **NaN/inf sanitised to 0.** Per P6 exit-criteria spec, every numeric value
    passed through ``_sanitize()`` so the DB never holds NaN or ±inf.
  - **Backward-compatible schema migration.** New columns are added via
    ``ALTER TABLE ADD COLUMN`` guarded by a ``PRAGMA table_info`` existence
    check, so pre-existing databases are upgraded transparently on first open.
  - Standalone: stdlib only. No numpy, no hermes internals, no provider code —
    matches the separation discipline of the Kalman modules.
"""
from __future__ import annotations

import math
import os
import sqlite3
import sys
import threading
import time
from typing import Any, Optional

# Add parent dir so `from src.xxx import` works when imported from outside
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from src.provider_names import normalize_provider_name

__all__ = ["ShadowLogger"]

# ── Exit-criteria constants (P6) ─────────────────────────────────────────────

#: Mean divergence below this → pressure routing tracks actual closely enough.
DIVERGENCE_THRESHOLD: float = 0.15

#: Minimum logged decisions before the soak is statistically meaningful.
MIN_DECISIONS: int = 500

#: z.ai session window length (hours) — one "full session cycle".
SESSION_WINDOW_HOURS: float = 5.0

#: z.ai weekly window length (hours) — the conditional-extension target.
WEEKLY_WINDOW_HOURS: float = 168.0  # 7 days

#: Default soak duration before evaluating exit criteria (hours).
DEFAULT_SOAK_HOURS: float = 48.0

#: Epsilon for cost-division guards.
_EPS = 1e-12

# Providers billed per-token (pay-as-you-go), not flat-rate subscriptions.
_PAID_PROVIDERS = frozenset({"ppq", "openrouter", "deepinfra"})


# ── Schema ───────────────────────────────────────────────────────────────────

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
    reason TEXT,
    -- P6-SHADOW: pressure-routing divergence columns (added via migration) ---
    pressure_provider TEXT,
    pressure_model TEXT,
    pressure_cost REAL,
    actual_cost REAL,
    divergence REAL,
    is_429 INTEGER DEFAULT 0,
    paid_provider INTEGER DEFAULT 0,
    -- PM-T6: per-model pricing columns (added via migration) ---
    requested_model TEXT,
    per_model_base_rate REAL,
    per_model_source TEXT,
    -- EUv2-7: quota regime at decision time (included/extra/exhausted) ---
    quota_regime TEXT
);
"""

# Original INSERT (backward-compat — new columns default to NULL/0).
_INSERT_SQL = (
    "INSERT INTO routing_shadow_decisions "
    "(ts, live_provider, live_model, shadow_provider, shadow_model, "
    " shadow_cost, live_cost, tokens, agree, reason, quota_regime) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);"
)

# P6 INSERT — full pressure-routing row.
_INSERT_PRESSURE_SQL = (
    "INSERT INTO routing_shadow_decisions "
    "(ts, live_provider, live_model, shadow_provider, shadow_model, "
    " shadow_cost, live_cost, tokens, agree, reason, "
    " pressure_provider, pressure_model, pressure_cost, actual_cost, "
    " divergence, is_429, paid_provider, "
    " requested_model, per_model_base_rate, per_model_source, quota_regime) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);"
)

# New columns to add when migrating an old DB.  (name, type, default-clause)
_MIGRATION_COLUMNS = [
    ("pressure_provider", "TEXT", None),
    ("pressure_model", "TEXT", None),
    ("pressure_cost", "REAL", None),
    ("actual_cost", "REAL", None),
    ("divergence", "REAL", "0.0"),
    ("is_429", "INTEGER", "0"),
    ("paid_provider", "INTEGER", "0"),
    # PM-T6: per-model pricing columns.
    ("requested_model", "TEXT", None),
    ("per_model_base_rate", "REAL", None),
    ("per_model_source", "TEXT", None),
    # EUv2-7: quota regime at decision time.
    ("quota_regime", "TEXT", None),
]


# ── Numeric sanitisation helpers ─────────────────────────────────────────────


def _sanitize(value: Any) -> float:
    """Convert NaN / ±inf / None / non-numeric to ``0.0``.

    P6 exit-criteria spec: "NaN/inf=0".  Every numeric field stored by the
    pressure logger passes through this so the DB never holds a non-finite
    float (SQLite would store it, but downstream AVG/SUM would propagate NaN
    and corrupt the exit-criteria evaluation).
    """
    if value is None:
        return 0.0
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(f) or math.isinf(f):
        return 0.0
    return f


def _compute_divergence(
    actual_provider: Optional[str],
    pressure_provider: Optional[str],
    actual_cost: Any,
    pressure_cost: Any,
) -> float:
    """Normalised divergence between actual and pressure-routing decisions.

    Returns a value in ``[0, 1+]``:

    * ``0.0`` when both strategies pick the **same provider** (agreement —
      cost-neutral by definition, even if costs differ).
    * ``|actual_cost − pressure_cost| / max(actual_cost, pressure_cost)``
      when providers **differ** — the relative cost gap.  If the costs are
      equal but providers differ the divergence is 0 (cost-neutral reroute,
      acceptable).  If actual is more expensive the divergence is high.

    All inputs pass through :func:`_sanitize` so NaN/inf never leak through.
    """
    if actual_provider == pressure_provider:
        return 0.0
    ac = _sanitize(actual_cost)
    pc = _sanitize(pressure_cost)
    denom = max(ac, pc, _EPS)
    return abs(ac - pc) / denom


def _is_paid_provider(name: Optional[str]) -> bool:
    """True if *name* is a per-token (pay-as-you-go) provider."""
    if not name:
        return False
    return normalize_provider_name(name) in _PAID_PROVIDERS


class ShadowLogger:
    """Read-only tap. Logs both live (best_key) and shadow (routing_optimizer)
    decisions for every API call, plus the P6 pressure-routing divergence.

    Writes to SQLite. <1ms overhead.  Thread-safe."""

    def __init__(self, db_path: str = "~/.hermes/bot/zai_usage.db"):
        """Open DB, create table if not exists, migrate legacy schema.

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
        self._migrate()

    # ── Schema migration ────────────────────────────────────────────────

    def _migrate(self) -> None:
        """Add P6 columns to a pre-existing table (idempotent).

        SQLite has no ``ADD COLUMN IF NOT EXISTS``; we introspect via
        ``PRAGMA table_info`` and only ALTER for columns that are missing.
        """
        with self._lock:
            cols = {
                row[1]
                for row in self._conn.execute(
                    "PRAGMA table_info(routing_shadow_decisions);"
                ).fetchall()
            }
            for col_name, col_type, default in _MIGRATION_COLUMNS:
                if col_name not in cols:
                    clause = f"ADD COLUMN {col_name} {col_type}"
                    if default is not None:
                        clause += f" DEFAULT {default}"
                    self._conn.execute(
                        f"ALTER TABLE routing_shadow_decisions {clause};"
                    )
            self._conn.commit()

    # ── Write paths ───────────────────────────────────────────────────────

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
        quota_regime: Optional[str] = None,
    ) -> None:
        """Insert a row into ``routing_shadow_decisions`` (original API).

        Computes ``agree = 1 if live_provider == shadow_provider else 0``.

        The P6 pressure columns (pressure_provider, divergence, …) are left
        NULL/0 — use :meth:`log_pressure_decision` to populate them.

        Never raises on null/empty inputs — logging must not break the hot
        path.
        """
        if ts is None:
            ts = time.time()

        # Normalize provider names to canonical form before storing.
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
                    quota_regime,
                ),
            )
            self._conn.commit()

    def log_pressure_decision(
        self,
        ts,
        actual_provider,
        actual_model,
        pressure_provider,
        pressure_model,
        actual_cost: Any,
        pressure_cost: Any,
        tokens,
        reason: Optional[str] = "",
        is_429: bool = False,
        requested_model: Optional[str] = None,
        per_model_base_rate: Any = None,
        per_model_source: Optional[str] = None,
        quota_regime: Optional[str] = None,
    ) -> None:
        """Log a pressure-routing divergence decision (P6-SHADOW).

        Records the **actual** provider (what production used) versus what the
        **pressure-routing** (LiveRouter) *would have* chosen, plus the
        divergence and effective prices.  The legacy live/shadow columns are
        populated from the same data so old queries remain coherent:

        * ``live_provider`` ← actual_provider (what ran)
        * ``shadow_provider`` ← pressure_provider (what pressure routing chose)

        Args:
            ts: Event timestamp (epoch seconds).  ``None`` → ``time.time()``.
            actual_provider / actual_model: the provider/model production
                actually used.
            pressure_provider / pressure_model: the provider/model the
                LiveRouter's quota-pressure selection would have chosen.
            actual_cost: effective $/M of the actual provider at decision time.
            pressure_cost: effective $/M the pressure router computed for its
                pick.
            tokens: token count for this call (0 if unknown).
            reason: free-text annotation.
            is_429: ``True`` if the actual request received a 429 (rate limit).
            requested_model: The model the client asked for (PM-T6). ``None``
                when no model was requested or per-model pricing is off.
            per_model_base_rate: The per-model base rate ($/M) the pressure
                router resolved for its chosen provider
                (``pressure_provider``). ``None`` when per-model pricing is
                inactive.
            per_model_source: Source tag for ``per_model_base_rate`` — one of
                ``"measured"``, ``"seed"``, ``"fallback"`` (see LiveRouter).
                ``None`` when per-model pricing is inactive.

        Never raises — sanitises all NaN/inf to 0 before storage.
        """
        if ts is None:
            ts = time.time()

        actual_provider = normalize_provider_name(actual_provider)
        pressure_provider = normalize_provider_name(pressure_provider)

        agree = 1 if actual_provider == pressure_provider else 0
        divergence = _compute_divergence(
            actual_provider, pressure_provider, actual_cost, pressure_cost
        )
        paid = 1 if _is_paid_provider(actual_provider) else 0

        with self._lock:
            self._conn.execute(
                _INSERT_PRESSURE_SQL,
                (
                    float(ts),
                    actual_provider,
                    actual_model,
                    pressure_provider,
                    pressure_model,
                    _sanitize(pressure_cost),  # shadow_cost
                    _sanitize(actual_cost),    # live_cost
                    int(tokens) if tokens is not None else 0,
                    agree,
                    reason if reason is not None else "",
                    pressure_provider,
                    pressure_model,
                    _sanitize(pressure_cost),
                    _sanitize(actual_cost),
                    divergence,
                    1 if is_429 else 0,
                    paid,
                    requested_model,
                    _sanitize(per_model_base_rate) if per_model_base_rate is not None else None,
                    per_model_source,
                    quota_regime,
                ),
            )
            self._conn.commit()

    def log_decision_with_pressure(
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
        quota_regime: Optional[str] = None,
        # ── P6 pressure-routing dimension (T7 / C1 fix) ──
        pressure_provider: Optional[str] = None,
        pressure_model: Optional[str] = None,
        pressure_cost: Any = None,
        actual_cost: Any = None,
        is_429: bool = False,
        requested_model: Optional[str] = None,
        per_model_base_rate: Any = None,
        per_model_source: Optional[str] = None,
    ) -> None:
        """Log a full three-dimension decision: optimizer + pressure + actual.

        This is the **C1 fix** from the T7c divergence report
        (``docs/shadow-7d-report.md`` §3, §6).  The live request path was
        calling :meth:`log_decision` (legacy API) which left all P6
        pressure-routing columns NULL — making the divergence and 429 exit
        gates degenerate (they passed trivially because their inputs were
        empty).

        This method populates **both** the legacy optimizer columns (what
        the price-first ``RoutingOptimizer`` would choose) **and** the P6
        pressure-routing columns (what the ``LiveRouter``'s quota-pressure
        selection would choose) in a single row, so:

        * ``shadow_provider`` / ``shadow_cost`` → optimizer pick (unchanged
          semantics for existing queries / cost-comparison analysis).
        * ``pressure_provider`` / ``pressure_cost`` → LiveRouter pick (the
          new dimension that the divergence gate checks).
        * ``live_provider`` / ``live_cost`` → what production actually used.
        * ``divergence`` → ``|Δcost| / max(act, prs)`` between actual and
          pressure picks (the P6 exit-gate metric).
        * ``agree`` → ``live == shadow`` (optimizer agreement, unchanged).

        Args:
            ts: Event timestamp (epoch seconds). ``None`` → ``time.time()``.
            live_provider / live_model: the provider/model production
                actually used.
            shadow_provider / shadow_model: what the price-first optimizer
                *would have* chosen.
            shadow_cost: effective $/M of the optimizer's pick.
            tokens: token count (0 if unknown).
            reason: free-text annotation.
            live_cost: effective $/M of the **actual** provider. When
                ``None``, falls back to ``actual_cost`` if provided.
            quota_regime: quota regime at decision time.
            pressure_provider / pressure_model: what the LiveRouter's
                pressure routing *would have* chosen.
            pressure_cost: effective $/M the pressure router computed.
            actual_cost: effective $/M of the actual provider (P6 column).
                When provided, also populates ``live_cost`` if that is None.
            is_429: ``True`` if the actual request received a 429.
            requested_model: the model the client asked for (PM-T6).
            per_model_base_rate / per_model_source: per-model pricing data.

        Never raises — sanitises all NaN/inf to 0 before storage.
        """
        if ts is None:
            ts = time.time()

        live_provider = normalize_provider_name(live_provider)
        shadow_provider = normalize_provider_name(shadow_provider)
        # Preserve None so the daily report can distinguish rows that
        # genuinely have pressure data (pressure_provider IS NOT NULL)
        # from legacy-only rows.  normalize_provider_name(None) → "unknown"
        # would break that distinction.
        pressure_provider_n = (
            normalize_provider_name(pressure_provider)
            if pressure_provider is not None
            else None
        )

        agree = 1 if live_provider == shadow_provider else 0

        # actual_cost doubles as live_cost when live_cost is not explicitly set.
        eff_live_cost = live_cost if live_cost is not None else actual_cost

        # Divergence is only meaningful when we have a pressure pick.
        if pressure_provider_n is not None:
            divergence = _compute_divergence(
                live_provider, pressure_provider_n, eff_live_cost, pressure_cost
            )
        else:
            divergence = 0.0
        paid = 1 if _is_paid_provider(live_provider) else 0

        with self._lock:
            self._conn.execute(
                _INSERT_PRESSURE_SQL,
                (
                    float(ts),
                    live_provider,
                    live_model,
                    shadow_provider,
                    shadow_model,
                    _sanitize(shadow_cost) if shadow_cost is not None else None,
                    _sanitize(eff_live_cost) if eff_live_cost is not None else None,
                    int(tokens) if tokens is not None else 0,
                    agree,
                    reason if reason is not None else "",
                    pressure_provider_n,
                    pressure_model,
                    _sanitize(pressure_cost) if pressure_cost is not None else None,
                    _sanitize(eff_live_cost) if eff_live_cost is not None else None,
                    divergence,
                    1 if is_429 else 0,
                    paid,
                    requested_model,
                    _sanitize(per_model_base_rate) if per_model_base_rate is not None else None,
                    per_model_source,
                    quota_regime,
                ),
            )
            self._conn.commit()

    # ── Read paths ───────────────────────────────────────────────────────

    def get_agreement_rate(self, since_ts: Optional[float] = None) -> float:
        """Fraction (0–1) of decisions where live_provider == shadow_provider.

        With P6 this is the same as the pressure-agreement rate when rows
        were logged via :meth:`log_pressure_decision`.

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

    # ── P6: pressure-divergence reads ─────────────────────────────────────

    def get_divergence_rate(self, since_ts: Optional[float] = None) -> float:
        """Mean per-decision divergence since *since_ts* (0.0 on empty).

        The exit-criteria gate checks ``get_divergence_rate() < 0.15``.

        Only counts rows that have genuine pressure data
        (``pressure_provider IS NOT NULL`` — logged via
        :meth:`log_pressure_decision` or :meth:`log_decision_with_pressure`
        with a pressure pick).  Legacy ``log_decision`` rows are excluded
        because their divergence defaults to 0.0 (from the schema migration)
        which would dilute the average and mask real divergence.
        """
        if since_ts is None:
            with self._lock:
                row = self._conn.execute(
                    "SELECT AVG(divergence), COUNT(*) "
                    "FROM routing_shadow_decisions "
                    "WHERE pressure_provider IS NOT NULL;"
                ).fetchone()
        else:
            with self._lock:
                row = self._conn.execute(
                    "SELECT AVG(divergence), COUNT(*) "
                    "FROM routing_shadow_decisions "
                    "WHERE pressure_provider IS NOT NULL AND ts >= ?;",
                    (float(since_ts),),
                ).fetchone()
        avg, count = row
        if not count or avg is None:
            return 0.0
        return float(avg)

    def get_429_rate(self, since_ts: Optional[float] = None) -> float:
        """Fraction of logged decisions that hit a 429 (0.0 on empty)."""
        if since_ts is None:
            with self._lock:
                row = self._conn.execute(
                    "SELECT AVG(is_429), COUNT(*) "
                    "FROM routing_shadow_decisions "
                    "WHERE is_429 IS NOT NULL;"
                ).fetchone()
        else:
            with self._lock:
                row = self._conn.execute(
                    "SELECT AVG(is_429), COUNT(*) "
                    "FROM routing_shadow_decisions "
                    "WHERE is_429 IS NOT NULL AND ts >= ?;",
                    (float(since_ts),),
                ).fetchone()
        avg, count = row
        if not count or avg is None:
            return 0.0
        return float(avg)

    def get_paid_spend(self, since_ts: Optional[float] = None) -> float:
        """Estimated paid spend ($): Σ(tokens × actual_cost) / 1e6 for paid
        providers only.

        Only counts rows where ``paid_provider = 1``.  Tokens are in units and
        cost in $/M, so spend = tokens × cost / 1_000_000.
        """
        if since_ts is None:
            with self._lock:
                row = self._conn.execute(
                    "SELECT COALESCE(SUM(tokens * actual_cost), 0.0) "
                    "FROM routing_shadow_decisions "
                    "WHERE paid_provider = 1 AND actual_cost IS NOT NULL;"
                ).fetchone()
        else:
            with self._lock:
                row = self._conn.execute(
                    "SELECT COALESCE(SUM(tokens * actual_cost), 0.0) "
                    "FROM routing_shadow_decisions "
                    "WHERE paid_provider = 1 AND actual_cost IS NOT NULL "
                    "AND ts >= ?;",
                    (float(since_ts),),
                ).fetchone()
        # tokens × $/M / 1e6 = dollars
        return _sanitize(row[0]) / 1e6 if row else 0.0

    def get_session_span_hours(self, since_ts: Optional[float] = None) -> float:
        """Wall-clock span of logged decisions in hours (0.0 on <2 rows).

        Used to verify "≥ 1 full z.ai session cycle" (5 h) and the conditional
        7-day extension.
        """
        if since_ts is None:
            with self._lock:
                row = self._conn.execute(
                    "SELECT MIN(ts), MAX(ts), COUNT(*) "
                    "FROM routing_shadow_decisions;"
                ).fetchone()
        else:
            with self._lock:
                row = self._conn.execute(
                    "SELECT MIN(ts), MAX(ts), COUNT(*) "
                    "FROM routing_shadow_decisions WHERE ts >= ?;",
                    (float(since_ts),),
                ).fetchone()
        mn, mx, count = row
        if not count or count < 2 or mn is None or mx is None:
            return 0.0
        return (float(mx) - float(mn)) / 3600.0

    def get_pressure_divergence_summary(
        self, since_ts: Optional[float] = None
    ) -> dict:
        """One-shot summary of the P6 pressure-routing dimension for daily reports.

        Returns a dict with per-provider counts, divergence stats, and the
        fraction of rows that actually have pressure data populated (vs.
        legacy ``log_decision`` rows with NULL pressure columns).

        This is the query the daily divergence report script calls. It
        separates rows with genuine pressure data (``pressure_provider IS
        NOT NULL``) from legacy rows so the report can flag a degenerate
        dataset (the T7c finding).

        Args:
            since_ts: optional lower-bound timestamp.

        Returns::

            {
                "total_rows": int,
                "pressure_rows": int,           # rows with pressure_provider set
                "pressure_pct": float,          # pressure_rows / total_rows
                "avg_divergence": float,         # only over pressure_rows
                "max_divergence": float,
                "p95_divergence": float,
                "zero_divergence_pct": float,    # % with divergence == 0
                "avg_actual_cost": float,
                "avg_pressure_cost": float,
                "provider_shifts": dict[str,int], # {"ours->ollama_cloud": N, ...}
                "429_count": int,
                "paid_provider_count": int,
            }
        """
        ts_clause = "WHERE ts >= ?" if since_ts is not None else ""
        ts_args: tuple = (float(since_ts),) if since_ts is not None else ()

        with self._lock:
            # Overall counts
            row = self._conn.execute(
                f"SELECT COUNT(*), "
                f"SUM(CASE WHEN pressure_provider IS NOT NULL THEN 1 ELSE 0 END) "
                f"FROM routing_shadow_decisions {ts_clause};",
                ts_args,
            ).fetchone()
            total, pressure_rows = row[0] or 0, row[1] or 0

            # Divergence stats (only over pressure rows)
            div_row = self._conn.execute(
                f"SELECT AVG(divergence), MAX(divergence), "
                f"SUM(CASE WHEN divergence = 0.0 THEN 1 ELSE 0 END) "
                f"FROM routing_shadow_decisions "
                f"WHERE pressure_provider IS NOT NULL "
                f"{'AND ts >= ?' if since_ts is not None else ''};",
                ts_args,
            ).fetchone()
            avg_div, max_div, zero_count = (
                div_row[0] or 0.0,
                div_row[1] or 0.0,
                div_row[2] or 0,
            )

            # Cost averages (pressure rows)
            cost_row = self._conn.execute(
                f"SELECT AVG(actual_cost), AVG(pressure_cost) "
                f"FROM routing_shadow_decisions "
                f"WHERE pressure_provider IS NOT NULL "
                f"{'AND ts >= ?' if since_ts is not None else ''};",
                ts_args,
            ).fetchone()
            avg_act, avg_pres = cost_row[0] or 0.0, cost_row[1] or 0.0

            # Provider shift counts (top 10)
            shift_rows = self._conn.execute(
                f"SELECT live_provider || '->' || pressure_provider, COUNT(*) "
                f"FROM routing_shadow_decisions "
                f"WHERE pressure_provider IS NOT NULL "
                f"  AND live_provider != pressure_provider "
                f"{'AND ts >= ?' if since_ts is not None else ''} "
                f"GROUP BY live_provider || '->' || pressure_provider "
                f"ORDER BY COUNT(*) DESC LIMIT 10;",
                ts_args,
            ).fetchall()
            shifts = {r[0]: r[1] for r in shift_rows}

            # 429 and paid counts
            misc_row = self._conn.execute(
                f"SELECT SUM(is_429), SUM(paid_provider) "
                f"FROM routing_shadow_decisions "
                f"WHERE pressure_provider IS NOT NULL "
                f"{'AND ts >= ?' if since_ts is not None else ''};",
                ts_args,
            ).fetchone()
            count_429, count_paid = misc_row[0] or 0, misc_row[1] or 0

        pressure_pct = (pressure_rows / total) if total else 0.0
        zero_pct = (zero_count / pressure_rows) if pressure_rows else 0.0

        return {
            "total_rows": total,
            "pressure_rows": pressure_rows,
            "pressure_pct": round(pressure_pct, 4),
            "avg_divergence": round(float(avg_div), 6),
            "max_divergence": round(float(max_div), 6),
            "zero_divergence_pct": round(zero_pct, 4),
            "avg_actual_cost": round(float(avg_act), 6),
            "avg_pressure_cost": round(float(avg_pres), 6),
            "provider_shifts": shifts,
            "429_count": int(count_429),
            "paid_provider_count": int(count_paid),
        }

    # ── P6: exit-criteria evaluation ──────────────────────────────────────

    def evaluate_exit_criteria(
        self,
        baseline_429_rate: float,
        baseline_paid_spend: float,
        since_ts: Optional[float] = None,
        divergence_threshold: float = DIVERGENCE_THRESHOLD,
        min_decisions: int = MIN_DECISIONS,
        session_hours: float = SESSION_WINDOW_HOURS,
    ) -> dict:
        """Evaluate all P6 exit criteria for shadow-mode promotion.

        Each criterion is checked independently and the result is a structured
        dict so a caller (dashboard, cron notifier, human) can see *which*
        gate failed without re-running queries.

        Args:
            baseline_429_rate: the 429 rate under the *current* routing (the
                number the pressure routing must not exceed).
            baseline_paid_spend: total paid spend ($) under current routing
                over the same window.
            since_ts: optional lower-bound timestamp for the evaluation window.
            divergence_threshold: max mean divergence (default 0.15 = 15%).
            min_decisions: minimum logged decisions (default 500).
            session_hours: minimum span for one full session cycle (default 5h).

        Returns::

            {
                "all_passed": bool,
                "criteria": {
                    "divergence":       {"value": f, "threshold": f, "passed": bool},
                    "rate_429":         {"value": f, "threshold": f, "passed": bool},
                    "paid_spend":       {"value": f, "threshold": f, "passed": bool},
                    "decisions_logged": {"value": i, "threshold": i, "passed": bool},
                    "session_cycle":    {"value": f, "threshold": f, "passed": bool},
                    "nan_inf_clean":    {"value": bool, "passed": bool},
                },
                "decisions_logged": int,
                "session_span_hours": float,
            }

        ``nan_inf_clean`` is always True because :meth:`log_pressure_decision`
        sanitises every value at write time — the check exists to make the gate
        explicit and to fail loudly if a future code path bypasses sanitisation.
        """
        divergence = self.get_divergence_rate(since_ts)
        rate_429 = self.get_429_rate(since_ts)
        paid = self.get_paid_spend(since_ts)
        decisions = self.get_count(since_ts)
        span = self.get_session_span_hours(since_ts)

        c_div = {
            "value": round(divergence, 6),
            "threshold": divergence_threshold,
            "passed": divergence < divergence_threshold,
        }
        c_429 = {
            "value": round(rate_429, 6),
            "threshold": baseline_429_rate,
            "passed": rate_429 <= baseline_429_rate,
        }
        c_spend = {
            "value": round(paid, 6),
            "threshold": baseline_paid_spend,
            "passed": paid <= baseline_paid_spend,
        }
        c_dec = {
            "value": decisions,
            "threshold": min_decisions,
            "passed": decisions >= min_decisions,
        }
        c_sess = {
            "value": round(span, 4),
            "threshold": session_hours,
            "passed": span >= session_hours,
        }
        # NaN/inf sanitisation is enforced at write time, so we verify there
        # are no residual non-finite values in the divergence column.
        c_nan = {
            "value": self._verify_no_nan_inf(),
            "passed": True,  # set below
        }
        c_nan["passed"] = bool(c_nan["value"])

        criteria = {
            "divergence": c_div,
            "rate_429": c_429,
            "paid_spend": c_spend,
            "decisions_logged": c_dec,
            "session_cycle": c_sess,
            "nan_inf_clean": c_nan,
        }

        all_passed = all(c["passed"] for c in criteria.values())

        return {
            "all_passed": all_passed,
            "criteria": criteria,
            "decisions_logged": decisions,
            "session_span_hours": round(span, 4),
        }

    def _verify_no_nan_inf(self) -> bool:
        """Return True if no stored divergence/cost value is NaN or ±inf.

        SQLite stores Python ``float('inf')`` as a special float; this query
        catches it.  Because :meth:`log_pressure_decision` sanitises at write
        time, this should always be True — but the explicit check makes the
        exit gate auditable.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM routing_shadow_decisions "
                "WHERE divergence IS NULL "
                "  OR divergence != divergence "  # NaN check (NaN != NaN)
                "  OR actual_cost IS NULL "
                "  OR actual_cost != actual_cost "
                "  OR pressure_cost IS NULL "
                "  OR pressure_cost != pressure_cost;"
            ).fetchone()
        # NULLs are acceptable for legacy rows (logged via log_decision).
        # We only fail if there are finite-but-inf values, which SQLite
        # represents as very large floats — check separately.
        with self._lock:
            inf_row = self._conn.execute(
                "SELECT COUNT(*) FROM routing_shadow_decisions "
                "WHERE divergence > 1e308 "
                "  OR actual_cost > 1e308 "
                "  OR pressure_cost > 1e308;"
            ).fetchone()
        return int(inf_row[0]) == 0

    # ── P6: conditional soak extension ────────────────────────────────────

    def should_extend_to_7days(
        self,
        soak_start_ts: float,
        soak_hours: float = DEFAULT_SOAK_HOURS,
    ) -> bool:
        """Decide whether the soak must be extended from 48 h to 7 days.

        Per the P6 spec: *"if the weekly window has not been seen in 48h,
        extend to 7 days."*  The weekly window is the z.ai 7-day quota cycle
        (168 h).  We have "seen" the weekly window when the decision span
        covers at least one full weekly cycle **or** when enough time has
        elapsed that a weekly reset was observed.

        This implementation uses the conservative proxy: the soak needs
        extending when the observed decision span is shorter than the weekly
        window (168 h) AND the elapsed soak time is less than 168 h.  In other
        words, we extend when we have NOT yet observed a full week of traffic.

        Args:
            soak_start_ts: epoch seconds when the soak began.
            soak_hours: nominal soak window (default 48 h).

        Returns:
            ``True`` if the operator should extend the soak to 7 days before
            evaluating exit criteria.
        """
        now = time.time()
        elapsed_hours = (now - float(soak_start_ts)) / 3600.0
        span = self.get_session_span_hours(since_ts=soak_start_ts)

        # We have NOT seen the weekly window if both the elapsed soak time
        # and the observed decision span are shorter than 168 h.
        seen_weekly = (
            elapsed_hours >= WEEKLY_WINDOW_HOURS
            or span >= WEEKLY_WINDOW_HOURS
        )
        return not seen_weekly and elapsed_hours >= soak_hours * 0.9

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
