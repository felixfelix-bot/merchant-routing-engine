"""PPQ (api.ppq.ai) credit-balance collector — standalone, cron-compatible.

Task 2A of the universal-pricing plan
(docs/plan-remaining-implementation.md §2A,
docs/remaining-steps-plan-v2.md Task 3).

WHY A DEDICATED FILE
    ``src/balance_collectors.py`` is the intended shared home for all credit
    collectors, but the PPQ / DeepInfra / OpenRouter collector tasks were
    dispatched in parallel and each rewrites that file from scratch, clobbering
    the others (lost-update storm on an untracked file). To deliver a PPQ
    collector that *survives*, it lives here until the sibling tasks are
    serialized; merging this into ``balance_collectors.py`` later is trivial
    (the public functions and the ``provider_balances`` table schema are
    identical to the OpenRouter collector's contract).

WHAT IT DOES
    POST https://api.ppq.ai/credits/balance  (Bearer auth, empty ``{}`` body)
    →  {"balance": <float>}   (bare float; may be NEGATIVE = credit overrun)
    →  stored as one row in the shared ``provider_balances`` table
       (provider='ppq'), with ``usage_fraction`` in [0,1] ready to feed
       ``pricing_engine.quota_pressure_factor(u, onset=0.80, asymptote=1.5,
       hard_limit=True)``.

PPQ exposes ONLY the remaining balance — there is no limit/usage pair like
OpenRouter. The starting (top-up) balance therefore comes from the
``PPQ_STARTING_BALANCE`` env var (default $20):

    usage_fraction = clamp(1 - balance / starting, 0, 1)

The contract mirrors the OpenRouter collector (dataclass + parse/collect/
store/get_latest, stdlib-only, NEVER raises — these run in cron and the
request path). The API call was verified against the LIVE endpoint on
2026-08-05 (POST /credits/balance, Bearer ``PPQ_API_KEY``, empty ``{}``
body → ``200 {"balance": <float>}``; GET variants 404) and cross-checked
with the production collector at ``~/.hermes/bot/ppq_data_collector.py``
(same host/api.ppq.ai, same Bearer auth from ``PPQ_API_KEY``).
``scripts/dq05_monitor_mcp.py`` / a ``dq05_ppq`` tool do not exist in this
tree — the earlier "verified against the DQ05 monitor" note referred to a
remote MCP tool that is not present here; the live probe above is the
authoritative source.

CONFIG (env)
    PPQ_API_KEY             bearer token for api.ppq.ai (required for live)
    PPQ_STARTING_BALANCE    initial credit balance USD (default 20)
    API_BURN_DB             override DB path (default ~/.hermes/bot/api_burn.db)

CRON
    python3 -m src.ppq_balance_collector [--db PATH]
    → prints one JSON status line, exits 0 on success / 1 on failure.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = [
    "PPQBalance",
    "parse_ppq_balance",
    "collect_ppq_balance",
    "store_ppq_balance",
    "get_latest_ppq_balance",
    "collect_and_store_ppq",
    "ppq_quota_entry",
    "default_db_path",
    "main",
    "PPQ_BALANCE_ENDPOINT",
    "PPQ_DEFAULT_STARTING_BALANCE",
]

# ── Config ───────────────────────────────────────────────────────────────────
PPQ_BALANCE_ENDPOINT = "https://api.ppq.ai/credits/balance"
PPQ_DEFAULT_TIMEOUT = 10.0           # seconds — matches DQ05 monitor
PPQ_DEFAULT_STARTING_BALANCE = 20.0  # USD — $20 initial top-up per plan
PPQ_KEY_ENV = "PPQ_API_KEY"
PPQ_STARTING_ENV = "PPQ_STARTING_BALANCE"

# Shared multi-provider table (same schema/name as the OpenRouter collector so
# rows from both modules coexist; the table is created idempotently here).
_PROVIDER_BALANCES_TABLE = "provider_balances"


def default_db_path() -> str:
    """Resolve the usage DB: ``API_BURN_DB`` env → ``~/.hermes/bot/api_burn.db``."""
    return os.environ.get("API_BURN_DB") or os.path.expanduser(
        "~/.hermes/bot/api_burn.db"
    )


def _resolve_starting(explicit: Optional[float]) -> float:
    """Resolve starting balance: explicit arg → ``PPQ_STARTING_BALANCE`` env → 20."""
    if explicit is not None:
        return float(explicit)
    raw = os.environ.get(PPQ_STARTING_ENV)
    if raw is None or raw.strip() == "":
        return PPQ_DEFAULT_STARTING_BALANCE
    try:
        return float(raw)
    except (TypeError, ValueError):
        return PPQ_DEFAULT_STARTING_BALANCE


def _as_float(v: Any) -> Optional[float]:
    """Coerce to a finite float. ``None`` on None/bool/NaN/inf/non-numeric.

    Negative values pass through (a negative balance means credit overshoot).
    """
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


# ── Schema (shared table, created idempotently) ──────────────────────────────

def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_PROVIDER_BALANCES_TABLE} (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            provider         TEXT    NOT NULL,
            collected_at     REAL    NOT NULL,
            usage            REAL,
            limit_credits    REAL,
            limit_remaining  REAL,
            usage_fraction   REAL    NOT NULL,
            is_unlimited     INTEGER NOT NULL,
            is_free_tier     INTEGER,
            raw_json         TEXT
        )
        """
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{_PROVIDER_BALANCES_TABLE}_provider_time "
        f"ON {_PROVIDER_BALANCES_TABLE} (provider, collected_at DESC)"
    )


# ── Dataclass ────────────────────────────────────────────────────────────────

@dataclass
class PPQBalance:
    """Parsed PPQ credit balance, ready to feed the pricing engine.

    balance         remaining credits (USD) from the API (None if unparseable)
    starting        starting/top-up balance (USD) used to derive usage
    usage_fraction  fraction of starting balance consumed, clamped to [0,1];
                    1.0 when exhausted. Feed directly to quota_pressure_factor.
    is_exhausted    True when balance <= 0 (no credits → +inf under hard_limit)
    collected_at    time.time() when collected/parsed
    raw             raw API response dict (debugging)
    """

    balance: Optional[float]
    starting: float
    usage_fraction: float
    is_exhausted: bool
    collected_at: float = field(default_factory=time.time)
    raw: dict = field(default_factory=dict)

    @property
    def remaining(self) -> Optional[float]:
        """Alias for ``balance`` (pricing-engine vocabulary)."""
        return self.balance

    @property
    def used_pct(self) -> float:
        """Usage as 0–100 % — what live_router._compute_ppq_pressure reads as
        ``quota_entry['used_pct']``. Makes the live-router bridge a one-liner."""
        return self.usage_fraction * 100.0


# ── Pure parsing ─────────────────────────────────────────────────────────────

def _usage_fraction(balance: Optional[float], starting: float) -> float:
    """Derive a [0,1] usage fraction.

    * starting <= 0 (misconfig) → 0.0 (cold-start path handles conservatism)
    * balance unknown → 0.0
    * balance <= 0 → exhausted → 1.0
    * else 1 - balance/starting, clamped to [0,1]
    """
    if starting <= 0.0:
        return 0.0
    if balance is None:
        return 0.0
    if balance <= 0.0:
        return 1.0
    return max(0.0, min(1.0, 1.0 - (balance / starting)))


def parse_ppq_balance(obj: Any, starting: float) -> Optional[PPQBalance]:
    """Parse a PPQ POST /credits/balance response. None (never raises) on bad shape."""
    if not isinstance(obj, dict):
        return None
    balance = _as_float(obj.get("balance"))
    if balance is None:
        return None
    starting_f = float(starting)
    return PPQBalance(
        balance=balance,
        starting=starting_f,
        usage_fraction=_usage_fraction(balance, starting_f),
        is_exhausted=balance <= 0.0,
        collected_at=time.time(),
        raw=dict(obj),
    )


# ── HTTP collection ──────────────────────────────────────────────────────────

def collect_ppq_balance(
    api_key: Optional[str] = None,
    starting: Optional[float] = None,
    timeout: float = PPQ_DEFAULT_TIMEOUT,
    endpoint: str = PPQ_BALANCE_ENDPOINT,
) -> Optional[PPQBalance]:
    """Query PPQ POST /credits/balance and return the parsed balance.

    Reads the key from ``api_key`` or ``PPQ_API_KEY`` env. Returns None (never
    raises) when: no key, request fails, non-200, or body unparseable.
    """
    key = api_key if api_key is not None else os.environ.get(PPQ_KEY_ENV)
    if not key:
        return None
    key = key.strip()
    starting_bal = _resolve_starting(starting)
    try:
        req = urllib.request.Request(
            endpoint,
            data=b"{}",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
            body = resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError):
        return None
    except Exception:
        return None

    try:
        obj = json.loads(body.decode("utf-8", "ignore"))
    except (ValueError, UnicodeDecodeError):
        return None
    return parse_ppq_balance(obj, starting_bal)


# ── Persistence (shared provider_balances table) ─────────────────────────────

def store_ppq_balance(db_path: str, balance: Optional[PPQBalance]) -> bool:
    """Append one PPQ snapshot. Maps to shared schema:
    usage = starting - balance, limit_credits = starting, limit_remaining = balance.
    True on success, False (never raises) on DB error or None balance.
    """
    if balance is None:
        return False
    consumed: Optional[float] = None
    if balance.starting > 0 and balance.balance is not None:
        consumed = balance.starting - balance.balance
    try:
        conn = sqlite3.connect(db_path)
        try:
            _ensure_table(conn)
            conn.execute(
                f"""
                INSERT INTO {_PROVIDER_BALANCES_TABLE}
                    (provider, collected_at, usage, limit_credits,
                     limit_remaining, usage_fraction, is_unlimited,
                     is_free_tier, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "ppq",
                    balance.collected_at,
                    consumed,
                    float(balance.starting),
                    balance.balance,
                    float(balance.usage_fraction),
                    0,  # PPQ is always finite (credit top-up)
                    None,
                    json.dumps(balance.raw, default=str),
                ),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return False


def get_latest_ppq_balance(db_path: str) -> Optional[PPQBalance]:
    """Most recent stored PPQ balance, or None (never raises) if none.

    Starting balance is recovered from the stored limit_credits column.
    """
    try:
        conn = sqlite3.connect(db_path)
        try:
            _ensure_table(conn)
            row = conn.execute(
                f"""
                SELECT limit_credits, limit_remaining, usage_fraction,
                       collected_at, raw_json
                FROM {_PROVIDER_BALANCES_TABLE}
                WHERE provider = 'ppq'
                ORDER BY collected_at DESC LIMIT 1
                """
            ).fetchone()
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return None

    if not row:
        return None
    starting, balance, usage_fraction, collected_at, raw_json = row
    starting_f = float(starting) if starting is not None else PPQ_DEFAULT_STARTING_BALANCE
    balance_f = _as_float(balance)
    try:
        raw = json.loads(raw_json) if isinstance(raw_json, str) else {}
    except (ValueError, TypeError):
        raw = {}
    return PPQBalance(
        balance=balance_f,
        starting=starting_f,
        usage_fraction=float(usage_fraction) if usage_fraction is not None else 0.0,
        is_exhausted=(balance_f is not None and balance_f <= 0.0),
        collected_at=float(collected_at) if collected_at is not None else time.time(),
        raw=raw if isinstance(raw, dict) else {},
    )


def collect_and_store_ppq(
    db_path: Optional[str] = None,
    api_key: Optional[str] = None,
    starting: Optional[float] = None,
    timeout: float = PPQ_DEFAULT_TIMEOUT,
) -> Optional[PPQBalance]:
    """Cron-friendly: collect once, optionally persist, return balance (None on failure)."""
    db_path = db_path or default_db_path()
    balance = collect_ppq_balance(api_key=api_key, starting=starting, timeout=timeout)
    if balance is not None:
        store_ppq_balance(db_path, balance)
    return balance


# ── Bridge to quota_state['ppq'] (P3-PPQ STEP 3) ──────────────────────────────
# A balance row is useless to the pricing engine until it lands in the
# ``quota_state['ppq']`` dict that ``live_router._compute_ppq_pressure`` reads.
# ``ppq_quota_entry`` is that bridge: it turns the newest row in the shared
# ``provider_balances`` table into the ``{'used_pct': float, ...}`` entry the
# pressure function consumes. ``_snapshot_quota`` (production proxy) calls this
# instead of the old hardcoded ``{'used_pct': 0.0, ...}``.

_PPQ_BALANCE_MAX_AGE = 1200.0  # 20 min — 2× the 5-min collection cadence (slack)


def ppq_quota_entry(
    db_path: Optional[str] = None,
    *,
    max_age: Optional[float] = _PPQ_BALANCE_MAX_AGE,
) -> dict:
    """Build the ``quota_state['ppq']`` entry from the latest stored balance.

    The bridge from the collector (``provider_balances`` table) to the
    ``quota_state['ppq']`` dict that ``live_router._compute_ppq_pressure``
    reads. Until this is wired into ``_snapshot_quota``, PPQ credit depletion
    is invisible to the pricing engine (``used_pct`` stays hardcoded at 0.0).

    Cold-start contract (matches ``_compute_ppq_pressure``):
      * no stored row, OR the row is older than ``max_age`` (default 20 min,
        i.e. 2× the 5-min collection cadence) → return ``{}`` (no ``used_pct``
        key). ``_compute_ppq_pressure`` then applies conservative
        ``cold_start_pressure`` (>1.0) so a not-yet-probed PPQ endpoint does
        not look artificially cheap.
      * fresh row → ``{'used_pct', 'remaining', 'starting', 'is_exhausted',
        'collected_at'}`` with ``used_pct`` in 0–100.

    Pass ``max_age=None`` to use the newest row regardless of age. Never
    raises — any DB/parse error yields the cold-start ``{}`` entry.
    """
    db_path = db_path or default_db_path()
    bal = get_latest_ppq_balance(db_path)
    if bal is None:
        return {}
    if max_age is not None and (time.time() - bal.collected_at) > max_age:
        return {}
    return {
        "used_pct": float(bal.used_pct),
        "remaining": float(bal.balance) if bal.balance is not None else 0.0,
        "starting": float(bal.starting),
        "is_exhausted": bool(bal.is_exhausted),
        "collected_at": float(bal.collected_at),
    }


# ── Cron entrypoint ──────────────────────────────────────────────────────────

def main(argv: Optional[list] = None) -> int:
    """Collect once, print a JSON status line, exit 0 on success / 1 on failure."""
    argv = list(sys.argv[1:] if argv is None else argv)
    db_override = None
    if "--db" in argv:
        db_override = argv[argv.index("--db") + 1]
    db_path = db_override or default_db_path()
    starting_bal = _resolve_starting(None)
    balance = collect_and_store_ppq(db_path=db_path, starting=starting_bal)
    if balance is None:
        # Distinguish "no key" from "API failure" for cron diagnostics.
        key = os.environ.get(PPQ_KEY_ENV, "").strip()
        reason = "PPQ_API_KEY not set" if not key else "API call failed (see logs)"
        print(json.dumps({"provider": "ppq", "ok": False, "error": reason}))
        return 1
    print(json.dumps({
        "provider": "ppq",
        "ok": True,
        "balance": balance.balance,
        "starting": balance.starting,
        "usage_fraction": balance.usage_fraction,
        "used_pct": balance.used_pct,
        "is_exhausted": balance.is_exhausted,
        "collected_at": balance.collected_at,
        "db_path": db_path,
    }, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
