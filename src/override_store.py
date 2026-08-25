"""override_store.py — cost-gate override mechanism (CG-4, plan v2 §3).

Implements Felix's Q6 override design: a scoped, TTL-bound, audited escape
hatch for the percentile cost gate, and the Q10 escape for strict
infra-down DENY.  Three properties, enforced structurally:

  SCOPED   one scope per grant, from :data:`src.cost_gate.OVERRIDE_SCOPES`
           (``budget | price_history | infra_down | paid_ceiling``).  A grant
           never disables more than one gate row, and the freeze marker /
           dead-or-locked-key hard blocks are override-IMMUNE (enforced in
           ``src.cost_gate.evaluate_cost_gate``, rows 1–2 — this store cannot
           weaken them because the decision function reads the same single
           grant this module writes).
  TTL-BOUND  ``expires_ts`` is mandatory; default 4 h, hard max 24 h.
           Expired grants read back as inactive.  There is no unlimited
           override.
  AUDITED   every grant is written to the append-only ``cost_gate_overrides``
           table and mirrored as an ``anomaly_events`` INFO row — on issue,
           on every consumption, and on revocation.  The audit row is
           committed BEFORE the marker file appears: if the audit DB is
           unreachable, issuing fails and no override exists (fail-closed
           ordering — an unaudited override must never be grantable).

Principals (plan §3 "Who"): Felix, the merchant-routing CW, and the
orchestrator CW (manager profile).  Workers: no.  The allowlist is
:data:`ALLOWED_PRINCIPALS`; anything else raises before any I/O.

Marker file (plan §3 "How"): ``~/.hermes/bot/.cost_gate_override`` holding
EXACTLY the four plan fields ``{scope, expires_ts, issued_by, reason}`` as
JSON, written atomically (temp file + ``os.replace``) with mode 0600.
There is no raw-file-editing path in code — grants flow through
:func:`OverrideStore.issue_override` (the CG-7 CLI's ``--override`` flag
calls it).  Reads fail CLOSED: absent, unreadable, corrupt, wrong-shape,
multi-scope or expired markers all behave as "no override" and never raise
out of :func:`OverrideStore.load_override`.

Audit consumers: the gate's verdict carries ``override_consumed`` (the
CG-1 ``_consume_override`` record); callers pass it to
:func:`OverrideStore.consume_override` which persists the plan §3 row
``(issued_by, scope, ttl, consumed_at, task)`` plus the anomaly INFO entry.

PURITY/I-O CONTRACT: unlike the CG-1 decision core this module DOES I/O by
design (marker file + sqlite), but every path and every timestamp is
injectable — production defaults are :data:`MARKER_PATH_DEFAULT` and
:data:`DB_PATH_DEFAULT`; tests point both at tmp paths and drive ``now_ts``
explicitly.  The module never reads the wall clock and never touches the
network.

Schema (created lazily, ``CREATE TABLE IF NOT EXISTS`` so the production
``zai_usage.db`` and fresh test DBs both work; inserts name columns
explicitly to stay compatible with the live table shape)::

    cost_gate_overrides(id, ts, kind ∈ {issued, consumed, revoked},
                        issued_by, scope, ttl_seconds, expires_ts, reason,
                        consumed_at_ts, task, detail-JSON)

    anomaly_events(ts, severity, category='cost_gate_override', title,
                   detail-JSON)          -- same table promo_tier/oxalpha use
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import tempfile
from typing import Any, Mapping

from src.cost_gate import OVERRIDE_SCOPES, is_override_active

__all__ = [
    "ALLOWED_PRINCIPALS",
    "DEFAULT_TTL_SECONDS",
    "MAX_TTL_SECONDS",
    "MARKER_PATH_DEFAULT",
    "DB_PATH_DEFAULT",
    "ANOMALY_CATEGORY",
    "OverrideError",
    "UnauthorizedPrincipal",
    "parse_ttl",
    "OverrideStore",
]


# ── constants (plan §3) ──────────────────────────────────────────────────────

#: §3 "Who" — Felix, the merchant-routing CW, the orchestrator CW (manager).
#: Workers: no.  Compared case-insensitively after strip().
ALLOWED_PRINCIPALS: frozenset[str] = frozenset(
    {"felix", "merchant-routing-cw", "orchestrator-cw"}
)

#: §3 "How" — TTL mandatory, default 4 h, hard max 24 h.
DEFAULT_TTL_SECONDS: float = 4.0 * 3600.0
MAX_TTL_SECONDS: float = 24.0 * 3600.0

#: Marker file (plan §3) — one active grant, JSON, four fields exactly.
MARKER_PATH_DEFAULT: str = os.path.expanduser("~/.hermes/bot/.cost_gate_override")

#: Shared proxy usage DB hosting ``cost_gate_overrides`` + ``anomaly_events``.
DB_PATH_DEFAULT: str = os.path.expanduser("~/.hermes/bot/zai_usage.db")

#: anomaly_events category for every row this module emits.
ANOMALY_CATEGORY: str = "cost_gate_override"

#: marker file permission — principal-scoped secret-ish state, keep tight.
_MARKER_MODE: int = 0o600

_TTL_RE = re.compile(r"(\d+(?:\.\d+)?)(h|m|s)?")

_DDL_OVERRIDES = """
CREATE TABLE IF NOT EXISTS cost_gate_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('issued','consumed','revoked')),
    issued_by TEXT,
    scope TEXT,
    ttl_seconds REAL,
    expires_ts REAL,
    reason TEXT,
    consumed_at_ts REAL,
    task TEXT,
    detail TEXT
)
"""

_DDL_ANOMALY = """
CREATE TABLE IF NOT EXISTS anomaly_events (
    ts REAL NOT NULL,
    severity TEXT NOT NULL,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT
)
"""


# ── errors ───────────────────────────────────────────────────────────────────


class OverrideError(Exception):
    """Validation / audit failure while issuing, consuming or revoking.

    Raised BEFORE any state change when a grant violates the §3 contract
    (bad scope, bad TTL, missing reason, second active grant, unknown
    principal, audit DB unreachable, consuming an expired grant).
    """


class UnauthorizedPrincipal(OverrideError):
    """The principal is not in :data:`ALLOWED_PRINCIPALS` (workers: no)."""


# ── TTL parsing (CG-7 CLI support: ``--ttl 4h``) ─────────────────────────────


def parse_ttl(text: str) -> float:
    """Parse a CLI TTL spec into seconds — ``"4h"``, ``"30m"``, ``"90s"``,
    or a bare second count ``"3600"``.

    Raises :class:`OverrideError` on anything else (unknown unit, spaces,
    sign, zero, non-numeric) or on values over :data:`MAX_TTL_SECONDS`
    (24 h) — the §3 hard ceiling applies at parse time too.
    """
    if not isinstance(text, str):
        raise OverrideError(f"ttl must be a string, got {type(text).__name__}")
    m = _TTL_RE.fullmatch(text.strip())
    if m is None:
        raise OverrideError(f"unparsable ttl: {text!r} (use e.g. 4h, 30m, 90s)")
    value = float(m.group(1))
    unit = m.group(2) or "s"
    seconds = value * {"h": 3600.0, "m": 60.0, "s": 1.0}[unit]
    if not math.isfinite(seconds) or seconds <= 0.0:
        raise OverrideError(f"ttl must be positive, got {text!r}")
    if seconds > MAX_TTL_SECONDS:
        raise OverrideError(
            f"ttl {text!r} exceeds the 24h hard max (plan §3)")
    return seconds


# ── the store ────────────────────────────────────────────────────────────────


class OverrideStore:
    """Marker-file + audit-DB front-end for cost-gate overrides (CG-4).

    All paths and timestamps are injectable; defaults target production.
    Every mutating method validates the full §3 contract first, writes the
    audit rows in one transaction, and only then touches the marker file.
    """

    #: single source of truth — the same frozenset the CG-1 gate validates
    #: against (composition, no duplication).
    OVERRIDE_SCOPES = OVERRIDE_SCOPES

    def __init__(
        self,
        marker_path: str = MARKER_PATH_DEFAULT,
        db_path: str = DB_PATH_DEFAULT,
    ) -> None:
        self.marker_path = str(marker_path)
        self.db_path = str(db_path)

    # ── internals ────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(self.db_path, timeout=5.0)
            conn.execute(_DDL_OVERRIDES)
            conn.execute(_DDL_ANOMALY)
            conn.commit()
            return conn
        except sqlite3.Error as exc:
            raise OverrideError(f"audit db unreachable: {exc}") from exc

    def _anomaly(
        self,
        conn: sqlite3.Connection,
        now_ts: float,
        severity: str,
        title: str,
        detail: Mapping[str, Any],
    ) -> None:
        conn.execute(
            "INSERT INTO anomaly_events (ts, severity, category, title, detail)"
            " VALUES (?,?,?,?,?)",
            (float(now_ts), str(severity), ANOMALY_CATEGORY, title,
             json.dumps(dict(detail), sort_keys=True)),
        )

    def _write_marker(self, grant: Mapping[str, Any]) -> None:
        """Atomic marker write: temp file in the same dir, 0600, replace."""
        parent = os.path.dirname(os.path.abspath(self.marker_path))
        os.makedirs(parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=parent, prefix=".cost_gate_override.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(dict(grant), fh, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, _MARKER_MODE)
            os.replace(tmp, self.marker_path)
        except OSError as exc:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise OverrideError(f"marker write failed: {exc}") from exc

    def _read_marker_raw(self) -> tuple[Any, str | None]:
        """Return (parsed, error).  Never raises for malformed content."""
        try:
            with open(self.marker_path, "r", encoding="utf-8") as fh:
                return json.load(fh), None
        except FileNotFoundError:
            return None, None
        except OSError as exc:
            return None, f"marker unreadable: {exc}"
        except json.JSONDecodeError as exc:
            return None, f"marker corrupt (not JSON): {exc}"

    @staticmethod
    def _validate_principal(principal: str) -> str:
        if not isinstance(principal, str):
            raise UnauthorizedPrincipal(
                f"issued_by must be a string, got {type(principal).__name__}")
        norm = principal.strip().lower()
        if norm not in ALLOWED_PRINCIPALS:
            raise UnauthorizedPrincipal(
                f"principal {principal!r} may not issue cost-gate overrides "
                f"(allowed: {sorted(ALLOWED_PRINCIPALS)}; workers: no)")
        return norm

    @staticmethod
    def _validate_ttl(ttl_seconds: float | None) -> float:
        ttl = DEFAULT_TTL_SECONDS if ttl_seconds is None else ttl_seconds
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)):
            raise OverrideError(f"ttl must be numeric, got {ttl!r}")
        ttl = float(ttl)
        if not math.isfinite(ttl):
            raise OverrideError("ttl must be finite")
        if ttl <= 0.0:
            raise OverrideError(f"ttl must be positive, got {ttl}")
        if ttl > MAX_TTL_SECONDS:
            raise OverrideError(
                f"ttl {ttl}s exceeds the 24h hard max (plan §3)")
        return ttl

    @staticmethod
    def _validate_reason(reason: str) -> str:
        if not isinstance(reason, str) or not reason.strip():
            raise OverrideError(
                "reason is mandatory (plan §3: --reason \"…\") — "
                "an unauditable override must not exist")
        return reason.strip()

    def _validate_scope(self, scope: Any) -> str:
        if not isinstance(scope, str):
            raise OverrideError(
                f"scope must be a single string from {sorted(OVERRIDE_SCOPES)}"
                f" — one scope per grant, never {scope!r}")
        if scope not in OVERRIDE_SCOPES:
            raise OverrideError(
                f"unknown scope {scope!r} (allowed: {sorted(OVERRIDE_SCOPES)})")
        return scope

    # ── public API ───────────────────────────────────────────────────────

    def issue_override(
        self,
        *,
        scope: str,
        issued_by: str,
        reason: str,
        ttl_seconds: float | None = None,
        now_ts: float,
    ) -> dict[str, Any]:
        """Issue a scoped, TTL-bound, audited override grant.

        Order of operations (fail-closed):
          1. validate principal / scope / TTL / reason (no I/O);
          2. reject if an active grant already exists (single grant);
          3. commit the ``issued`` audit row + anomaly INFO row;
          4. atomically write the marker.

        Returns the grant dict — exactly the four §3 fields.
        """
        principal = self._validate_principal(issued_by)
        the_scope = self._validate_scope(scope)
        ttl = self._validate_ttl(ttl_seconds)
        the_reason = self._validate_reason(reason)
        now = float(now_ts)

        existing = self.load_override(now_ts=now)
        if existing["active"]:
            raise OverrideError(
                "an active override already exists "
                f"(scope={existing['grant']['scope']!r}, expires_ts="
                f"{existing['grant']['expires_ts']}); revoke or let it expire "
                "first — one grant at a time (plan §3 single-scope rule)")

        grant = {
            "scope": the_scope,
            "expires_ts": now + ttl,
            "issued_by": principal,
            "reason": the_reason,
        }

        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO cost_gate_overrides"
                " (ts, kind, issued_by, scope, ttl_seconds, expires_ts,"
                "  reason, consumed_at_ts, task, detail)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (now, "issued", principal, the_scope, ttl, grant["expires_ts"],
                 the_reason, None, None,
                 json.dumps({"marker": self.marker_path}, sort_keys=True)),
            )
            self._anomaly(
                conn, now, "INFO",
                f"cost-gate override issued: scope={the_scope}",
                {"scope": the_scope, "issued_by": principal,
                 "ttl_seconds": ttl, "expires_ts": grant["expires_ts"],
                 "reason": the_reason},
            )
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            raise OverrideError(f"audit write failed: {exc}") from exc
        finally:
            conn.close()

        self._write_marker(grant)  # audit committed first — §3 fail-closed
        return grant

    def load_override(self, *, now_ts: float) -> dict[str, Any]:
        """Read the marker fail-CLOSED.

        Returns ``{"active": bool, "grant": dict | None, "error": str | None}``.
        ``grant`` (when active) is normalized to exactly the four §3 fields,
        shaped for :func:`src.cost_gate.evaluate_cost_gate`'s ``override``
        parameter.  Absent / corrupt / wrong-shape / multi-scope / expired
        markers all yield ``active=False`` — never an exception.  Validity
        is decided by the SAME :func:`src.cost_gate.is_override_active` the
        gate uses (composition, no duplication).
        """
        now = float(now_ts)
        parsed, error = self._read_marker_raw()
        if parsed is None and error is None:
            return {"active": False, "grant": None, "error": None}
        if error is not None or not isinstance(parsed, Mapping):
            return {"active": False, "grant": None,
                    "error": error or "marker is not a JSON object"}

        scope = parsed.get("scope")
        if not isinstance(scope, str) or scope not in OVERRIDE_SCOPES:
            return {"active": False, "grant": None,
                    "error": f"marker scope invalid: {scope!r}"}
        expires_ts = parsed.get("expires_ts")
        if (isinstance(expires_ts, bool)
                or not isinstance(expires_ts, (int, float))):
            return {"active": False, "grant": None,
                    "error": f"marker expires_ts invalid: {expires_ts!r}"}

        if not is_override_active(parsed, now):
            if float(expires_ts) <= now:
                return {"active": False, "grant": None,
                        "error": f"expired at ts {expires_ts}"}
            return {"active": False, "grant": None,
                    "error": "marker rejected by cost_gate.is_override_active"}

        grant = {
            "scope": scope,
            "expires_ts": float(expires_ts),
            "issued_by": parsed.get("issued_by"),
            "reason": parsed.get("reason"),
        }
        return {"active": True, "grant": grant, "error": None}

    def consume_override(
        self,
        grant: Mapping[str, Any] | None,
        *,
        now_ts: float,
        task: str | None = None,
        would_have_been: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Persist the audit record for a gate invocation that consumed an
        override (plan §3: "every gate invocation that consumed an override
        logs a cost_gate_overrides table row ... + an anomaly_events INFO").

        ``grant`` is the ``override_consumed`` block of a CG-1 verdict (or
        the loaded marker grant).  ``None`` is a no-op returning ``None``
        (nothing consumed, nothing audited).  Consuming an expired or
        malformed grant raises — the gate must not have used it.
        The marker is NOT removed: the TTL governs the grant's lifetime and
        every consuming invocation during that lifetime is audited.
        """
        if grant is None:
            return None
        if not isinstance(grant, Mapping) or not is_override_active(
                grant, float(now_ts)):
            raise OverrideError(
                "cannot audit consumption of an expired/invalid grant — "
                "the gate must not have used it")
        now = float(now_ts)
        scope = grant.get("scope")
        issued_by = grant.get("issued_by")
        expires_ts = grant.get("expires_ts")
        reason = grant.get("reason")

        # ttl for the §3 consumed-row fields: from the grant's issue audit
        # row when findable, else expires - now is NOT the ttl — use NULL.
        ttl = None
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT ttl_seconds FROM cost_gate_overrides"
                " WHERE kind='issued' AND scope IS ? AND issued_by IS ?"
                " ORDER BY id DESC LIMIT 1",
                (scope, issued_by),
            )
            row = cur.fetchone()
            if row is not None and row[0] is not None:
                ttl = float(row[0])
            detail = {
                "task": task,
                "would_have_been": dict(would_have_been or {}),
            }
            conn.execute(
                "INSERT INTO cost_gate_overrides"
                " (ts, kind, issued_by, scope, ttl_seconds, expires_ts,"
                "  reason, consumed_at_ts, task, detail)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (now, "consumed", issued_by, scope, ttl, expires_ts, reason,
                 now, task, json.dumps(detail, sort_keys=True)),
            )
            self._anomaly(
                conn, now, "INFO",
                f"cost-gate override consumed: scope={scope} task={task}",
                detail,
            )
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            raise OverrideError(f"audit write failed: {exc}") from exc
        finally:
            conn.close()
        return {
            "scope": scope,
            "issued_by": issued_by,
            "reason": reason,
            "expires_ts": expires_ts,
            "consumed_at_ts": now,
            "task": task,
            "would_have_been": dict(would_have_been or {}),
        }

    def revoke(
        self,
        *,
        now_ts: float,
        revoked_by: str,
        reason: str,
    ) -> bool:
        """Revoke the current grant (the ``--revoke`` counterpart so nobody
        ever edits the marker by hand).

        Principal-checked like issuing.  Returns True if a marker was
        removed, False when none existed (no audit rows either).  The audit
        row is committed before the marker disappears — same fail-closed
        ordering as issuing, read backwards.
        """
        principal = self._validate_principal(revoked_by)
        the_reason = self._validate_reason(reason)
        now = float(now_ts)

        parsed, _err = self._read_marker_raw()
        marker_exists = os.path.exists(self.marker_path)
        if not marker_exists and parsed is None:
            return False

        scope = parsed.get("scope") if isinstance(parsed, Mapping) else None
        issued_by = (parsed.get("issued_by")
                     if isinstance(parsed, Mapping) else None)
        expires_ts = (parsed.get("expires_ts")
                      if isinstance(parsed, Mapping) else None)

        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO cost_gate_overrides"
                " (ts, kind, issued_by, scope, ttl_seconds, expires_ts,"
                "  reason, consumed_at_ts, task, detail)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (now, "revoked", principal, scope, None, expires_ts,
                 the_reason, None, None,
                 json.dumps({"revoked_grant_issued_by": issued_by,
                             "marker": self.marker_path}, sort_keys=True)),
            )
            self._anomaly(
                conn, now, "INFO",
                f"cost-gate override revoked: scope={scope}",
                {"scope": scope, "revoked_by": principal, "reason": the_reason,
                 "revoked_grant_issued_by": issued_by},
            )
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            raise OverrideError(f"audit write failed: {exc}") from exc
        finally:
            conn.close()

        try:
            os.unlink(self.marker_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise OverrideError(f"marker removal failed: {exc}") from exc
        return True
