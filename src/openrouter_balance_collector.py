"""OpenRouter (openrouter.ai) credit-balance collector — standalone, cron-compatible.

Task 3 of the universal-pricing plan (docs/plan-remaining-v2.md §Phase 1 Task 3).

WHY A DEDICATED FILE
    ``src/balance_collectors.py`` is the intended shared home for all credit
    collectors, but the PPQ / DeepInfra / OpenRouter collector tasks were
    dispatched in parallel and each rewrites that file from scratch, clobbering
    the others (lost-update storm on an untracked file). To deliver an
    OpenRouter collector that *survives*, it lives here alongside
    ``src/ppq_balance_collector.py`` until the sibling tasks are serialized;
    merging both into ``balance_collectors.py`` later is trivial (the public
    functions and the ``provider_balances`` table schema are identical).

WHAT IT DOES
    GET https://openrouter.ai/api/v1/key   (HTTP bearer auth)
      → {"data": {"usage": <float>, "limit": <float|null>,
                  "limit_remaining": <float|null>, "limit_reset": <str|null>,
                  "is_free_tier": <bool>, "label": <str>, ...}}
      → stored as one row in the shared ``provider_balances`` table
        (provider='openrouter'), with ``usage_fraction`` in [0,1] ready to feed
        ``pricing_engine.quota_pressure_factor(u, onset=0.80, asymptote=1.5,
        hard_limit=True)``.

Unlike PPQ/DeepInfra (which only expose a remaining balance and need a
``STARTING_BALANCE`` env), OpenRouter reports the credit cap directly, so the
usage fraction is derived from the API's own fields:

    * limit is null (docs) or <= 0 (defensive: -1 has historically meant
      unlimited too)  →  is_unlimited=True, usage_fraction = 0.0
    * limit_remaining <= 0 (or absent and usage >= limit) → exhausted → 1.0
    * else  u = 1 - limit_remaining / limit   (preferred — survives resets/top-ups)
      fallback u = usage / limit               (when limit_remaining is absent)
    * clamped to [0, 1]

The limit_remaining path matters: ``usage`` is *lifetime* credits while
``limit`` is the (often daily-resetting) per-key cap, so ``usage/limit`` would
falsely report exhaustion. Verified live 2026-08-05: usage=10.00, limit=5.0
(daily), limit_remaining=5.0 → correctly yields usage_fraction=0.0.

The contract mirrors src/ppq_balance_collector.py (dataclass + parse/collect/
store/get_latest/collect_and_store, stdlib-only, NEVER raises — these run in
cron and the request path).

CONFIG (env)
    OPENROUTER_API_KEY    bearer token for openrouter.ai (required for live)
    API_BURN_DB           override DB path (default ~/.hermes/bot/api_burn.db)

CRON
    python3 -m src.openrouter_balance_collector [--db PATH]
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
    "OpenRouterBalance",
    "parse_openrouter_key",
    "collect_openrouter_balance",
    "store_openrouter_balance",
    "get_latest_openrouter_balance",
    "collect_and_store_openrouter",
    "openrouter_quota_entry",
    "default_db_path",
    "main",
    "OPENROUTER_KEY_ENDPOINT",
    "OPENROUTER_DEFAULT_TIMEOUT",
    "OPENROUTER_KEY_ENV",
]

# ── Config ───────────────────────────────────────────────────────────────────
OPENROUTER_KEY_ENDPOINT = "https://openrouter.ai/api/v1/key"
OPENROUTER_DEFAULT_TIMEOUT = 10.0  # seconds — collectors must be fast
OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"

# App-identity headers (mirror config/providers.yaml → external.openrouter.headers;
# harmless on this endpoint, kept as constants to avoid a yaml import in the path).
_OPENROUTER_APP_HEADERS = {
    "HTTP-Referer": "https://hermes.local",
    "X-Title": "Hermes Agent",
}

# Shared multi-provider table (same schema/name as the PPQ collector so rows from
# both modules coexist; the table is created idempotently here).
_PROVIDER_BALANCES_TABLE = "provider_balances"


def default_db_path() -> str:
    """Resolve the usage DB: ``API_BURN_DB`` env → ``~/.hermes/bot/api_burn.db``.

    Identical to ``ppq_balance_collector.default_db_path`` so both collectors
    share one ``provider_balances`` table.
    """
    return os.environ.get("API_BURN_DB") or os.path.expanduser(
        "~/.hermes/bot/api_burn.db"
    )


def _as_float(v: Any) -> Optional[float]:
    """Coerce to a finite float. ``None`` on None/bool/NaN/inf/non-numeric.

    Negative values pass through (OpenRouter can report a negative balance).
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
class OpenRouterBalance:
    """Parsed OpenRouter key balance, ready to feed the pricing engine.

    usage           credits used, all time (USD); None if absent/unparseable
    limit           per-key credit cap (USD); None when unlimited
    limit_remaining remaining credits for the key (USD); None when unlimited
    usage_fraction  fraction of the cap consumed, clamped to [0,1]; 0.0 for
                    unlimited keys, 1.0 when exhausted. Feed directly to
                    ``quota_pressure_factor(u, onset, asymptote, hard_limit=True)``.
    is_unlimited    True when the key has no spending cap
    is_free_tier    whether the account has ever paid for credits (from API)
    limit_reset     reset policy string (e.g. "daily"), or None
    label           key label from the API, or None
    collected_at    time.time() when collected/parsed
    raw             raw ``data`` dict from the API response (debugging)
    """

    usage: Optional[float]
    limit: Optional[float]
    limit_remaining: Optional[float]
    usage_fraction: float
    is_unlimited: bool
    is_free_tier: Optional[bool] = None
    limit_reset: Optional[str] = None
    label: Optional[str] = None
    collected_at: float = field(default_factory=time.time)
    raw: dict = field(default_factory=dict)

    @property
    def remaining(self) -> Optional[float]:
        """Alias for ``limit_remaining`` (pricing-engine vocabulary)."""
        return self.limit_remaining

    @property
    def used_pct(self) -> float:
        """Usage as 0–100 % — what live_router reads as ``quota_entry['used_pct']``."""
        return self.usage_fraction * 100.0

    @property
    def is_exhausted(self) -> bool:
        """True when the key is funded but out of credits."""
        if self.is_unlimited:
            return False
        if self.limit_remaining is not None:
            return self.limit_remaining <= 0.0
        if self.limit is not None and self.usage is not None:
            return self.usage >= self.limit
        return False


# ── Pure helpers ─────────────────────────────────────────────────────────────
def _is_unlimited_limit(limit: Optional[float]) -> bool:
    """True when the limit value means "no spending cap".

    OpenRouter documents ``null`` as unlimited; ``-1`` has been used
    historically. We treat ``None`` and any value ``<= 0`` as unlimited.
    """
    if limit is None:
        return True
    return limit <= 0.0


def _compute_usage_fraction(
    usage: Optional[float],
    limit: Optional[float],
    limit_remaining: Optional[float],
) -> float:
    """Derive a [0,1] usage fraction from the OpenRouter fields."""
    if _is_unlimited_limit(limit):
        return 0.0
    # Beyond here ``limit`` is a real, positive credit cap.
    cap: float = float(limit)  # type: ignore[arg-type]  # guaranteed > 0 here
    if limit_remaining is not None:
        if limit_remaining <= 0.0:
            return 1.0
        u = 1.0 - (limit_remaining / cap)
    elif usage is not None:
        if usage >= cap:
            return 1.0
        u = usage / cap
    else:
        return 0.0
    return max(0.0, min(1.0, u))


def parse_openrouter_key(obj: Any) -> Optional[OpenRouterBalance]:
    """Parse an OpenRouter ``GET /api/v1/key`` response object.

    Accepts either the full ``{"data": {...}}`` envelope or the inner ``data``
    dict directly. Returns ``None`` (never raises) on a bad shape.
    """
    if not isinstance(obj, dict):
        return None
    data = obj.get("data") if isinstance(obj.get("data"), dict) else obj
    if not isinstance(data, dict):
        return None

    usage = _as_float(data.get("usage"))
    limit = _as_float(data.get("limit"))
    limit_remaining = _as_float(data.get("limit_remaining"))
    is_free = data.get("is_free_tier")
    if not isinstance(is_free, bool):
        is_free = None
    limit_reset = data.get("limit_reset")
    if not isinstance(limit_reset, str):
        limit_reset = None
    label = data.get("label")
    if not isinstance(label, str):
        label = None

    is_unlimited = _is_unlimited_limit(limit)
    usage_fraction = _compute_usage_fraction(usage, limit, limit_remaining)

    return OpenRouterBalance(
        usage=usage,
        limit=limit,
        limit_remaining=limit_remaining,
        usage_fraction=usage_fraction,
        is_unlimited=is_unlimited,
        is_free_tier=is_free,
        limit_reset=limit_reset,
        label=label,
        collected_at=time.time(),
        raw=dict(data),
    )


# ── HTTP collection ──────────────────────────────────────────────────────────
def collect_openrouter_balance(
    api_key: Optional[str] = None,
    timeout: float = OPENROUTER_DEFAULT_TIMEOUT,
    endpoint: str = OPENROUTER_KEY_ENDPOINT,
) -> Optional[OpenRouterBalance]:
    """Query OpenRouter ``GET /api/v1/key`` and return the parsed balance.

    Reads the key from ``api_key`` or the ``OPENROUTER_API_KEY`` env var.
    Returns ``None`` (never raises) when: no key, request fails, non-200, or
    body unparseable.
    """
    key = api_key if api_key is not None else os.environ.get(OPENROUTER_KEY_ENV)
    if not key:
        return None
    key = key.strip()
    headers = {"Authorization": f"Bearer {key}"}
    headers.update(_OPENROUTER_APP_HEADERS)

    try:
        req = urllib.request.Request(endpoint, headers=headers, method="GET")
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
    return parse_openrouter_key(obj)


# ── Persistence (shared provider_balances table) ─────────────────────────────
def store_openrouter_balance(
    db_path: str, balance: Optional[OpenRouterBalance]
) -> bool:
    """Append one OpenRouter snapshot to the shared table. True on success,
    False (never raises) on DB error or None balance."""
    if balance is None:
        return False
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
                    "openrouter",
                    balance.collected_at,
                    balance.usage,
                    balance.limit,
                    balance.limit_remaining,
                    float(balance.usage_fraction),
                    1 if balance.is_unlimited else 0,
                    (1 if balance.is_free_tier else 0)
                    if balance.is_free_tier is not None
                    else None,
                    json.dumps(balance.raw, default=str),
                ),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return False


def get_latest_openrouter_balance(db_path: str) -> Optional[OpenRouterBalance]:
    """Most recent stored OpenRouter balance, or None (never raises) if none."""
    try:
        conn = sqlite3.connect(db_path)
        try:
            _ensure_table(conn)
            row = conn.execute(
                f"""
                SELECT usage, limit_credits, limit_remaining, usage_fraction,
                       is_unlimited, is_free_tier, collected_at, raw_json
                FROM {_PROVIDER_BALANCES_TABLE}
                WHERE provider = 'openrouter'
                ORDER BY collected_at DESC LIMIT 1
                """
            ).fetchone()
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return None

    if not row:
        return None
    (usage, limit, limit_remaining, usage_fraction, is_unlimited,
     is_free_tier, collected_at, raw_json) = row
    try:
        raw = json.loads(raw_json) if isinstance(raw_json, str) else {}
    except (ValueError, TypeError):
        raw = {}
    return OpenRouterBalance(
        usage=usage,
        limit=limit,
        limit_remaining=limit_remaining,
        usage_fraction=float(usage_fraction) if usage_fraction is not None else 0.0,
        is_unlimited=bool(is_unlimited),
        is_free_tier=bool(is_free_tier) if is_free_tier is not None else None,
        collected_at=float(collected_at) if collected_at is not None else time.time(),
        raw=raw if isinstance(raw, dict) else {},
    )


def collect_and_store_openrouter(
    db_path: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout: float = OPENROUTER_DEFAULT_TIMEOUT,
) -> Optional[OpenRouterBalance]:
    """Cron-friendly: collect once, optionally persist, return balance (None on
    failure). Never raises."""
    db_path = db_path or default_db_path()
    balance = collect_openrouter_balance(api_key=api_key, timeout=timeout)
    if balance is not None:
        store_openrouter_balance(db_path, balance)
    return balance


# ── Bridge to quota_state['openrouter'] (mirrors ppq_quota_entry) ─────────────
# Until this is wired into _snapshot_quota, OpenRouter credit depletion is
# invisible to the pricing engine (the proxy hardcodes snap['openrouter'] as
# {used_pct:0.0, remaining:inf}). openrouter_quota_entry turns the newest
# 'openrouter' row in the shared provider_balances table into the entry the
# proxy consumes. Cold-start contract matches ppq_quota_entry: no/stale row →
# {} so the proxy falls back to the optimistic {used_pct:0.0, remaining:inf}.
_OPENROUTER_BALANCE_MAX_AGE = 1200.0  # 20 min — 2× the 5-min cadence (slack)


def openrouter_quota_entry(
    db_path: Optional[str] = None,
    *,
    max_age: Optional[float] = _OPENROUTER_BALANCE_MAX_AGE,
) -> dict:
    """Build the ``quota_state['openrouter']`` entry from the latest stored row.

    The bridge from the collector (``provider_balances`` table) to the
    ``quota_state['openrouter']`` dict that the production proxy's
    ``_snapshot_quota`` reads. Mirrors ``ppq_balance_collector.ppq_quota_entry``.

    Cold-start contract (matches the proxy's current hardcoded fallback):
      * no stored row, OR the row is older than ``max_age`` (default 20 min,
        2× the 5-min cadence) → return ``{}`` (no ``used_pct`` key). The proxy
        then falls back to ``{used_pct:0.0, remaining:inf}`` (current behavior).
      * fresh row → ``{'used_pct','remaining','usage','limit','is_unlimited',
        'is_exhausted','is_free_tier','collected_at'}`` with ``used_pct`` in
        0–100. For an unlimited key, ``remaining`` is ``+inf`` and ``used_pct``
        is 0.0 (identical to the current hardcode).

    Pass ``max_age=None`` to use the newest row regardless of age. Never
    raises — any DB/parse error yields the cold-start ``{}`` entry.
    """
    db_path = db_path or default_db_path()
    bal = get_latest_openrouter_balance(db_path)
    if bal is None:
        return {}
    if max_age is not None and (time.time() - bal.collected_at) > max_age:
        return {}
    if bal.is_unlimited or bal.limit_remaining is None:
        remaining = float("inf") if bal.is_unlimited else 0.0
    else:
        remaining = float(bal.limit_remaining)
    return {
        "used_pct": float(bal.used_pct),
        "remaining": remaining,
        "usage": float(bal.usage) if bal.usage is not None else None,
        "limit": float(bal.limit) if bal.limit is not None else None,
        "is_unlimited": bool(bal.is_unlimited),
        "is_exhausted": bool(bal.is_exhausted),
        "is_free_tier": bal.is_free_tier,
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
    balance = collect_and_store_openrouter(db_path=db_path)
    if balance is None:
        key = os.environ.get(OPENROUTER_KEY_ENV, "").strip()
        reason = "OPENROUTER_API_KEY not set" if not key else "API call failed (see logs)"
        print(json.dumps({"provider": "openrouter", "ok": False, "error": reason}))
        return 1
    print(json.dumps({
        "provider": "openrouter",
        "ok": True,
        "usage": balance.usage,
        "limit": balance.limit,
        "limit_remaining": balance.limit_remaining,
        "usage_fraction": balance.usage_fraction,
        "used_pct": balance.used_pct,
        "is_unlimited": balance.is_unlimited,
        "is_exhausted": balance.is_exhausted,
        "is_free_tier": balance.is_free_tier,
        "limit_reset": balance.limit_reset,
        "collected_at": balance.collected_at,
        "db_path": db_path,
    }, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
