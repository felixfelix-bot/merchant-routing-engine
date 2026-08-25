"""balance_collectors.py — unified credit-balance collectors for all providers.

This is the single shared home for every per-token provider's credit-balance
collector. Each provider exposes (or doesn't) a way to read the authoritative
remaining balance; this module knows how to ask each one.

Providers
══════════

DeepInfra (verified 2026-08-05 against the live account, OpenAPI spec at
https://api.deepinfra.com/openapi.json):
    The billing surface lives under ``/payment/*`` (NOT ``/v1/user/balance`` —
    that path does not exist). It is reachable with the standard inference
    API key via HTTP bearer auth.

    /payment/usage?from=YYYY.MM          (single month; also "current" / "current-N")
    /payment/usage?from=<unix>&to=<unix>  (range, capped at 31 days, not too far back)
        -> {"months": [{"period": "2026.07",
                         "items": [{"model": {...}, "units": 1595024,
                                    "rate": 1.300e-4, "cost": 207,
                                    "pricing_type": "input_tokens", ...},
                                   ...]}],
            "initial_month": "2026.07"}

        * ``cost`` is in **cents**. Confirmed: ``cost == units * rate`` for every
          nonzero item, and DeepSeek-V4-Pro input at ``rate=1.300e-4`` cents/token
          == $1.30/M tokens (the exact seed cost used elsewhere in this codebase).
          So total_spent_usd = sum(item.cost for all items) / 100.

        * Range queries are capped at 31 days, so lifetime spend is gathered
          month-by-month from ``initial_month`` to the current month (one cheap
          GET per month).

    /payment/checklist
        -> {"stripe_balance": 0.49, "recent": 0.0, "suspended": bool,
            "suspend_reason": str|None, "billing_type": "balance", ...}

        * ``stripe_balance`` sign is **inverted** vs intuition (DeepInfra spec):
            negative => prepaid credit ready to spend
            positive => money owed
          We expose both the raw value and the sign-resolved
          ready_to_spend / money_owed pair so callers never have to reason
          about the polarity.

    remaining = starting_balance - total_spent_usd
        The task formula. ``starting_balance`` is the caller's concept of the
        funded budget (e.g. ``DEEPINFRA_STARTING_BALANCE`` env, default $5.0 in
        the proxy). ``stripe_balance`` is the authoritative balance *position*
        and is returned alongside for cross-check / direct use.

PPQ (api.ppq.ai) — verified 2026-08-05 against the live endpoint:
    POST https://api.ppq.ai/credits/balance  (Bearer auth, empty ``{}`` body)
    →  {"balance": <float>}   (bare float; may be NEGATIVE = credit overrun)
    →  stored as one row in the shared ``provider_balances`` table
       (provider='ppq'), with ``usage_fraction`` in [0,1] ready to feed
       ``pricing_engine.quota_pressure_factor``.

    PPQ exposes ONLY the remaining balance — there is no limit/usage pair like
    OpenRouter. The starting (top-up) balance comes from ``PPQ_STARTING_BALANCE``
    env (default $20):
        usage_fraction = clamp(1 - balance / starting, 0, 1)

OpenRouter (openrouter.ai) — verified 2026-08-05 against the live endpoint:
    GET https://openrouter.ai/api/v1/key   (HTTP bearer auth)
      → {"data": {"usage": <float>, "limit": <float|null>,
                  "limit_remaining": <float|null>, "limit_reset": <str|null>,
                  "is_free_tier": <bool>, "label": <str>, ...}}
      → stored as one row in the shared ``provider_balances`` table
        (provider='openrouter'), with ``usage_fraction`` in [0,1].

    Unlike PPQ/DeepInfra (which only expose a remaining balance and need a
    STARTING_BALANCE env), OpenRouter reports the credit cap directly:
        * limit is null (docs) or <= 0 (defensive: -1 has historically meant
          unlimited too)  →  is_unlimited=True, usage_fraction = 0.0
        * limit_remaining <= 0 (or absent and usage >= limit) → exhausted → 1.0
        * else  u = 1 - limit_remaining / limit   (preferred)
          fallback u = usage / limit               (when limit_remaining absent)
        * clamped to [0, 1]

Design rules (mirror src/cost_extraction.py):
    * **NEVER raises.** Intended to run inside the proxy's request path and in
      background reconcilers. Any error — no key, network failure, HTTP 4xx/5xx,
      malformed JSON, bogus types — is swallowed and yields a result whose
      ``error`` field names the problem while numeric fields stay ``None``.
    * **Dependency-light.** Stdlib ``urllib`` only; no ``requests``/``httpx``
      added to the project.
    * **Testable.** The entire network surface is the ``_http_get`` function
      (DeepInfra) or ``urllib.request.urlopen`` (PPQ/OpenRouter, monkeypatched
      in tests).

Shared infrastructure
    All three collectors share:
      - ``default_db_path()`` — resolves the usage DB
      - ``_connect_db()`` — WAL-mode connection with busy_timeout
      - ``_ensure_table()`` — idempotent ``provider_balances`` table creation
      - ``_as_float()`` — best-effort finite-float coercion
      - ``PROVIDER_BALANCES_TABLE`` — the shared table name

Public API
    DeepInfra:
      ``DeepInfraBalance`` — dataclass result.
      ``collect_deepinfra_balance(api_key, starting_balance=5.0, ...) -> DeepInfraBalance``
      ``COST_FIELD_UNIT`` — cents per cost unit (100.0).
    PPQ:
      ``PPQBalance``, ``parse_ppq_balance``, ``collect_ppq_balance``,
      ``store_ppq_balance``, ``get_latest_ppq_balance``,
      ``collect_and_store_ppq``, ``ppq_quota_entry``.
    OpenRouter:
      ``OpenRouterBalance``, ``parse_openrouter_key``,
      ``collect_openrouter_balance``, ``store_openrouter_balance``,
      ``get_latest_openrouter_balance``, ``collect_and_store_openrouter``,
      ``openrouter_quota_entry``.
    CLI:
      ``main()`` — dispatch to the right collector via ``--provider ppq|openrouter|deepinfra``.
"""
from __future__ import annotations

import calendar
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional

__all__ = [
    # ── shared ──
    "PROVIDER_BALANCES_TABLE",
    "default_db_path",
    # ── DeepInfra ──
    "DeepInfraBalance",
    "collect_deepinfra_balance",
    "COST_FIELD_UNIT",
    "DEEPINFRA_API_BASE",
    # ── PPQ ──
    "PPQBalance",
    "parse_ppq_balance",
    "collect_ppq_balance",
    "store_ppq_balance",
    "get_latest_ppq_balance",
    "collect_and_store_ppq",
    "ppq_quota_entry",
    "PPQ_BALANCE_ENDPOINT",
    "PPQ_DEFAULT_STARTING_BALANCE",
    # ── OpenRouter ──
    "OpenRouterBalance",
    "parse_openrouter_key",
    "collect_openrouter_balance",
    "store_openrouter_balance",
    "get_latest_openrouter_balance",
    "collect_and_store_openrouter",
    "openrouter_quota_entry",
    "OPENROUTER_KEY_ENDPOINT",
    "OPENROUTER_DEFAULT_TIMEOUT",
    "OPENROUTER_KEY_ENV",
    # ── Telnyx ──
    "TelnyxBalance",
    "collect_telnyx_balance",
    "fetch_routstr_balance_sats",
    "collect_routstr_balance",
    "store_routstr_balance",
    "get_latest_routstr_balance",
    "routstr_quota_entry",
    "store_telnyx_balance",
    "get_latest_telnyx_balance",
    "collect_and_store_telnyx",
    "telnyx_quota_entry",
    "TELNYX_DEFAULT_STARTING_BALANCE",
    "TELNYX_KEY_ENV",
    "TELNYX_STARTING_ENV",
    # ── NeuralWatt ──
    "NeuralWattBalance",
    "collect_neuralwatt_balance",
    "store_neuralwatt_balance",
    "get_latest_neuralwatt_balance",
    "collect_and_store_neuralwatt",
    "neuralwatt_quota_entry",
    "get_neuralwatt_cost_correction_factor",
    "_neuralwatt_http_get",
    "_fetch_neuralwatt_daily_spend",
    "NEURALWATT_DEFAULT_STARTING_BALANCE",
    "NEURALWATT_DEFAULT_DAILY_CAP",
    "NEURALWATT_STARTING_ENV",
    "NEURALWATT_DAILY_CAP_ENV",
    "NEURALWATT_KEY_ENV",
    "NEURALWATT_API_BASE",
    "NEURALWATT_QUOTA_ENDPOINT",
    "NEURALWATT_USAGE_SUMMARY_URL",
    "NEURALWATT_DEFAULT_TIMEOUT",
    "default_usage_db_path",
    # ── CLI ──
    "main",
]

# ═════════════════════════════════════════════════════════════════════════════
# SHARED INFRASTRUCTURE
# ═════════════════════════════════════════════════════════════════════════════

PROVIDER_BALANCES_TABLE = "provider_balances"


def default_db_path() -> str:
    """Resolve the usage DB: ``API_BURN_DB`` env → ``~/.hermes/bot/api_burn.db``."""
    return os.environ.get("API_BURN_DB") or os.path.expanduser(
        "~/.hermes/bot/api_burn.db"
    )


def default_usage_db_path() -> str:
    """Resolve the request-logging DB: ``ZAI_USAGE_DB`` env → ``~/.hermes/bot/zai_usage.db``.

    zai_proxy.py writes every AI request to ``api_calls`` in zai_usage.db.
    The NeuralWatt collector (which has no balance API) reads cumulative
    spend from that table here. Kept separate from :func:`default_db_path`
    (api_burn.db, which holds provider_balances snapshots) because they are
    genuinely two different files in the proxy.
    """
    return os.environ.get("ZAI_USAGE_DB") or os.path.expanduser(
        "~/.hermes/bot/zai_usage.db"
    )


def _connect_db(db_path: str) -> sqlite3.Connection:
    """Open a WAL-mode connection with busy_timeout for concurrent access.

    The production proxy (zai_proxy.py) opens the same api_burn.db in WAL mode.
    A plain ``sqlite3.connect()`` with no journal_mode pragma can hit a stale
    ``-shm`` file left by the proxy and fail with ``disk I/O error``.  Setting
    WAL + busy_timeout on every connection makes the collectors coexist with
    the proxy's long-lived reader.
    """
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _ensure_table(conn: sqlite3.Connection) -> None:
    """Idempotently create the shared ``provider_balances`` table + index."""
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {PROVIDER_BALANCES_TABLE} (
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
        f"CREATE INDEX IF NOT EXISTS idx_{PROVIDER_BALANCES_TABLE}_provider_time "
        f"ON {PROVIDER_BALANCES_TABLE} (provider, collected_at DESC)"
    )


def _as_float(v: Any) -> Optional[float]:
    """Coerce to a finite float. ``None`` on None/bool/NaN/inf/non-numeric.

    Negative values pass through (a negative balance means credit overshoot,
    or OpenRouter can report a negative remaining).
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


# ═════════════════════════════════════════════════════════════════════════════
# DEEPINFRA COLLECTOR
# ═════════════════════════════════════════════════════════════════════════════

DEEPINFRA_API_BASE = "https://api.deepinfra.com"
COST_FIELD_UNIT = 100.0  # the `cost` field in /payment/usage is in cents

# Type of the HTTP seam: (url, headers, timeout) -> (status_code, body_text).
# status_code is None on transport-level failure (no response from the server).
HttpGetFn = Callable[[str, dict[str, str], float], "tuple[int | None, str]"]


@dataclass
class DeepInfraBalance:
    """Authoritative DeepInfra spend & balance snapshot.

    Numeric fields are ``None`` when they could not be determined (no key,
    network/HTTP error, malformed response). ``error`` is a short human string
    describing why, or ``None`` on full success. A successful call has
    ``error is None`` and ``total_spent_usd is not None``.
    """

    # Spend (from /payment/usage)
    total_spent_usd: float | None = None
    initial_month: str | None = None           # account's first billing month ("YYYY.MM")
    months_covered: list[str] = field(default_factory=list)
    # Balance (from /payment/checklist)
    stripe_balance: float | None = None        # RAW value; sign is inverted (see module doc)
    ready_to_spend_usd: float | None = None    # prepaid credit available (>= 0)
    money_owed_usd: float | None = None        # outstanding debt (>= 0)
    recent_usd: float | None = None            # usage since most recent invoice
    suspended: bool | None = None
    suspend_reason: str | None = None
    billing_type: str | None = None
    # Derived
    remaining_usd: float | None = None         # starting_balance - total_spent_usd
    fetched_at: float = field(default_factory=time.time)
    error: str | None = None

    @property
    def ok(self) -> bool:
        """True when spend was retrieved successfully (balance may still be None)."""
        return self.error is None and self.total_spent_usd is not None


# ── HTTP seam ────────────────────────────────────────────────────────────────

def _default_http_get(url: str, headers: dict[str, str], timeout: float) -> tuple[int | None, str]:
    """Default network implementation using stdlib urllib.

    Returns ``(status_code, body_text)``. On any transport-level failure (DNS,
    connection refused, timeout) returns ``(None, "")`` — never raises.
    """
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — trusted provider URL
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # HTTP-level error (4xx/5xx): still a response from the server — capture it.
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return e.code, body
    except Exception:
        # Transport failure, timeout, SSL, etc.
        return None, ""


# ── DeepInfra helpers ──────────────────────────────────────────────────────────

def _cents_to_usd(cents: float) -> float:
    """Convert a cost value (cents) to dollars."""
    return cents / COST_FIELD_UNIT


def _current_period() -> str:
    """Current month as a 'YYYY.MM' period string (UTC)."""
    t = time.gmtime()
    return "%04d.%02d" % (t.tm_year, t.tm_mon)


def _valid_period(period: Any) -> bool:
    """True iff ``period`` is a well-formed 'YYYY.MM' string (1900 <= Y, 1-12 M)."""
    if not isinstance(period, str):
        return False
    try:
        y, m = (int(x) for x in period.split("."))
    except (ValueError, AttributeError):
        return False
    return len(period.split(".")) == 2 and y >= 1900 and 1 <= m <= 12


def _next_period(period: str) -> str | None:
    """Increment a 'YYYY.MM' period by one month. Returns None on garbage input."""
    if not _valid_period(period):
        return None
    y, m = (int(x) for x in period.split("."))
    m += 1
    if m > 12:
        m, y = 1, y + 1
    return "%04d.%02d" % (y, m)


def _iter_months(initial_period: str, current_period: str, limit: int) -> Iterator[str]:
    """Yield 'YYYY.MM' strings from initial_period up to (and including)
    current_period, capped at ``limit`` months. Defensive against bad input:
    a malformed ``initial_period`` yields nothing."""
    if limit <= 0 or not _valid_period(initial_period):
        return
    seen = 0
    cur = initial_period
    while cur is not None and seen < limit:
        yield cur
        if cur == current_period:
            return
        cur = _next_period(cur)
        seen += 1


def _coerce_float(val: Any) -> float | None:
    """Best-effort float coercion. None on non-numeric/None/NaN/inf.

    Alias for ``_as_float`` — kept under this name for DeepInfra test
    compatibility (the tests import ``_coerce_float`` directly).
    """
    return _as_float(val)


# ── DeepInfra response parsers (pure functions; never raise) ──────────────

def _parse_usage_month(body: str) -> tuple[float, str | None]:
    """Parse one /payment/usage?from=YYYY.MM response.

    Returns ``(month_spent_cents, initial_month)``. ``initial_month`` is None if
    absent. On any parse failure returns ``(0.0, None)``.
    """
    try:
        data = json.loads(body)
    except Exception:
        return 0.0, None
    if not isinstance(data, dict):
        return 0.0, None
    total_cents = 0.0
    for mo in data.get("months", []) or []:
        if not isinstance(mo, dict):
            continue
        for it in mo.get("items", []) or []:
            if not isinstance(it, dict):
                continue
            c = _coerce_float(it.get("cost"))
            if c is not None:
                total_cents += c
    im = data.get("initial_month")
    return total_cents, im if isinstance(im, str) else None


def _parse_checklist(body: str) -> dict[str, Any]:
    """Parse a /payment/checklist response into the balance-relevant fields.

    Returns a dict (possibly empty) on failure. Never raises.
    """
    try:
        data = json.loads(body)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    for k in ("stripe_balance", "recent", "limit"):
        out[k] = _coerce_float(data.get(k))
    suspended = data.get("suspended")
    out["suspended"] = bool(suspended) if isinstance(suspended, bool) else None
    sr = data.get("suspend_reason")
    out["suspend_reason"] = sr if isinstance(sr, str) else None
    bt = data.get("billing_type")
    out["billing_type"] = bt if isinstance(bt, str) else None
    return out


def _resolve_stripe_balance(raw: float | None) -> tuple[float | None, float | None]:
    """Resolve the raw stripe_balance into (ready_to_spend_usd, money_owed_usd).

    Per DeepInfra spec: negative => ready-to-spend credit, positive => owed.
    The API value is already in dollars (not cents). Never raises.
    """
    if raw is None:
        return None, None
    if raw <= 0:
        return -raw, 0.0   # credit available
    return 0.0, raw         # money owed


def _urlencode(params: dict[str, str]) -> str:
    """Tiny local urlencode (avoid urllib.parse name collisions at import time)."""
    return urllib.parse.urlencode(params)


# ── DeepInfra public collector ───────────────────────────────────────────────

def collect_deepinfra_balance(
    api_key: str | None,
    starting_balance: float = 5.0,
    *,
    api_base: str = DEEPINFRA_API_BASE,
    months_limit: int = 24,
    timeout: float = 10.0,
    http_get: HttpGetFn = _default_http_get,
) -> DeepInfraBalance:
    """Query DeepInfra's billing API for real total_spent and balance.

    Parameters
    ----------
    api_key
        DeepInfra API token. If empty/None, returns immediately with
        ``error="no api key"`` and all numeric fields None.
    starting_balance
        The funded budget in USD. ``remaining_usd = starting_balance -
        total_spent_usd``. Defaults to 5.0 to match the proxy's
        ``DEEPINFRA_STARTING_BALANCE`` default.
    api_base
        Override the API host (tests / on-prem).
    months_limit
        Maximum number of monthly /payment/usage queries (one GET each) when
        gathering lifetime spend from ``initial_month`` forward. Caps latency.
    timeout
        Per-request timeout in seconds.
    http_get
        Network seam ``(url, headers, timeout) -> (status, body)``. Tests inject
        canned responses here; production uses ``_default_http_get`` (urllib).

    Returns
    -------
    DeepInfraBalance
        Spend from /payment/usage (total_spent_usd, remaining_usd) and the
        balance position from /payment/checklist (stripe_balance,
        ready_to_spend_usd, money_owed_usd, suspended, ...). On any failure the
        ``error`` field names the problem and numeric fields are None. Never
        raises.
    """
    result = DeepInfraBalance()
    if not api_key:
        result.error = "no api key"
        return result
    headers = {"Authorization": "Bearer " + api_key, "User-Agent": "hermes-balance-collector"}

    # ── 1. lifetime spend via month-by-month /payment/usage queries ──────────
    # Seed with the current month to discover initial_month even if the account
    # is brand new; then walk forward from initial_month to now.
    current = _current_period()
    months_covered: list[str] = []
    total_cents = 0.0
    initial_month: str | None = None
    http_failures = 0

    # First request: current month (also reveals initial_month).
    url = "%s/payment/usage?%s" % (
        api_base.rstrip("/"),
        _urlencode({"from": current}),
    )
    status, body = http_get(url, headers, timeout)
    if status is None:
        # Transport failure on the very first call — we have nothing.
        result.error = "network error contacting DeepInfra billing API"
        return result
    month_cents, im = _parse_usage_month(body)
    if status != 200:
        # Server responded but with an error status; capture and try checklist.
        result.error = "usage API returned HTTP %s" % status
        # Still attempt the checklist below for a partial result.
    else:
        total_cents += month_cents
        months_covered.append(current)
        initial_month = im

    # Walk forward from initial_month (if known) through current, summing each.
    # months_limit caps the TOTAL number of usage GETs (current + forward), so
    # reserve one GET for the current-month call already made above.
    if initial_month and status == 200:
        forward_budget = max(months_limit - 1, 0)
        for period in _iter_months(initial_month, current, forward_budget):
            if period in months_covered:
                continue  # already counted (e.g. current month)
            purl = "%s/payment/usage?%s" % (
                api_base.rstrip("/"),
                _urlencode({"from": period}),
            )
            pstatus, pbody = http_get(purl, headers, timeout)
            if pstatus is None or pstatus != 200:
                # Transport failure OR HTTP error on a historical month: the
                # lifetime total is incomplete — record it as a partial failure.
                http_failures += 1
                continue
            pcents, _ = _parse_usage_month(pbody)
            total_cents += pcents
            months_covered.append(period)

    # ── 2. balance position via /payment/checklist ───────────────────────────
    curl = "%s/payment/checklist" % api_base.rstrip("/")
    cstatus, cbody = http_get(curl, headers, timeout)
    if cstatus == 200:
        cl = _parse_checklist(cbody)
        result.stripe_balance = cl.get("stripe_balance")
        rts, owed = _resolve_stripe_balance(result.stripe_balance)
        result.ready_to_spend_usd = rts
        result.money_owed_usd = owed
        result.recent_usd = cl.get("recent")
        result.suspended = cl.get("suspended")
        result.suspend_reason = cl.get("suspend_reason")
        result.billing_type = cl.get("billing_type")
    # A checklist failure is not fatal — we still have spend. Leave balance None.

    # ── 3. assemble derived fields ───────────────────────────────────────────
    if status == 200:
        result.total_spent_usd = round(_cents_to_usd(total_cents), 6)
        result.remaining_usd = round(starting_balance - result.total_spent_usd, 6)
        result.initial_month = initial_month
        result.months_covered = months_covered
        # Clear a partial error if usage ultimately succeeded.
        if http_failures == 0:
            result.error = None
        elif result.error is None:
            result.error = "%d month query(ies) failed (partial spend)" % http_failures
    else:
        # usage never succeeded; if checklist also failed, error stays set.
        if cstatus != 200 and result.error is None:
            result.error = "usage HTTP %s, checklist HTTP %s" % (status, cstatus)

    return result


# ═════════════════════════════════════════════════════════════════════════════
# PPQ COLLECTOR
# ═════════════════════════════════════════════════════════════════════════════

PPQ_BALANCE_ENDPOINT = "https://api.ppq.ai/credits/balance"
PPQ_DEFAULT_TIMEOUT = 10.0           # seconds
PPQ_DEFAULT_STARTING_BALANCE = 20.0  # USD — $20 initial top-up per plan
PPQ_KEY_ENV = "PPQ_API_KEY"
PPQ_STARTING_ENV = "PPQ_STARTING_BALANCE"


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


# ── PPQ pure helpers ─────────────────────────────────────────────────────────

def _ppq_usage_fraction(balance: Optional[float], starting: float) -> float:
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
        usage_fraction=_ppq_usage_fraction(balance, starting_f),
        is_exhausted=balance <= 0.0,
        collected_at=time.time(),
        raw=dict(obj),
    )


# ── PPQ HTTP collection ───────────────────────────────────────────────────────

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
                "User-Agent": "hermes-balance-collector",
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


# ── PPQ persistence ──────────────────────────────────────────────────────────

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
        conn = _connect_db(db_path)
        try:
            _ensure_table(conn)
            conn.execute(
                f"""
                INSERT INTO {PROVIDER_BALANCES_TABLE}
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
        conn = _connect_db(db_path)
        try:
            _ensure_table(conn)
            row = conn.execute(
                f"""
                SELECT limit_credits, limit_remaining, usage_fraction,
                       collected_at, raw_json
                FROM {PROVIDER_BALANCES_TABLE}
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


# ── PPQ bridge to quota_state['ppq'] ─────────────────────────────────────────

_PPQ_BALANCE_MAX_AGE = 1200.0  # 20 min — 2× the 5-min collection cadence (slack)


def ppq_quota_entry(
    db_path: Optional[str] = None,
    *,
    max_age: Optional[float] = _PPQ_BALANCE_MAX_AGE,
) -> dict:
    """Build the ``quota_state['ppq']`` entry from the latest stored balance.

    The bridge from the collector (``provider_balances`` table) to the
    ``quota_state['ppq']`` dict that ``live_router._compute_ppq_pressure`` reads.

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


# ═════════════════════════════════════════════════════════════════════════════
# OPENROUTER COLLECTOR
# ═════════════════════════════════════════════════════════════════════════════

OPENROUTER_KEY_ENDPOINT = "https://openrouter.ai/api/v1/key"
OPENROUTER_DEFAULT_TIMEOUT = 10.0  # seconds — collectors must be fast
OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"

# App-identity headers (mirror config/providers.yaml → external.openrouter.headers;
# harmless on this endpoint, kept as constants to avoid a yaml import in the path).
_OPENROUTER_APP_HEADERS = {
    "HTTP-Referer": "https://hermes.local",
    "X-Title": "Hermes Agent",
}


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


# ── OpenRouter pure helpers ───────────────────────────────────────────────────

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


# ── OpenRouter HTTP collection ───────────────────────────────────────────────

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
    headers = {"Authorization": f"Bearer {key}", "User-Agent": "hermes-balance-collector"}
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


# ── OpenRouter persistence ───────────────────────────────────────────────────

def store_openrouter_balance(
    db_path: str, balance: Optional[OpenRouterBalance]
) -> bool:
    """Append one OpenRouter snapshot to the shared table. True on success,
    False (never raises) on DB error or None balance."""
    if balance is None:
        return False
    try:
        conn = _connect_db(db_path)
        try:
            _ensure_table(conn)
            conn.execute(
                f"""
                INSERT INTO {PROVIDER_BALANCES_TABLE}
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
        conn = _connect_db(db_path)
        try:
            _ensure_table(conn)
            row = conn.execute(
                f"""
                SELECT usage, limit_credits, limit_remaining, usage_fraction,
                       is_unlimited, is_free_tier, collected_at, raw_json
                FROM {PROVIDER_BALANCES_TABLE}
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


# ── OpenRouter bridge to quota_state['openrouter'] ────────────────────────────

_OPENROUTER_BALANCE_MAX_AGE = 1200.0  # 20 min — 2× the 5-min cadence (slack)


def openrouter_quota_entry(
    db_path: Optional[str] = None,
    *,
    max_age: Optional[float] = _OPENROUTER_BALANCE_MAX_AGE,
) -> dict:
    """Build the ``quota_state['openrouter']`` entry from the latest stored row.

    The bridge from the collector (``provider_balances`` table) to the
    ``quota_state['openrouter']`` dict that the production proxy's
    ``_snapshot_quota`` reads. Mirrors ``ppq_quota_entry``.

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


# ═════════════════════════════════════════════════════════════════════════════
# TELNYX COLLECTOR (self-tracking — no balance API; pitfall #47)
# ═════════════════════════════════════════════════════════════════════════════
#
# Telnyx does not expose a billing/balance API.  We self-track spend by
# summing ``cost_usd`` from the ``api_calls`` table in the same api_burn.db
# where the proxy logs every request:
#
#   1) SELECT SUM(cost_usd) FROM api_calls WHERE key_name='telnyx'
#   2) remaining          = TELNYX_STARTING_BALANCE - sum_spent
#   3) usage_fraction     = 1 - (remaining / TELNYX_STARTING_BALANCE)
#   4) Write to provider_balances table (provider='telnyx')
#
# This mirrors the DeepInfra self-tracking pattern: when there is no external
# balance endpoint, the local usage DB *is* the source of truth.

TELNYX_DEFAULT_STARTING_BALANCE = 10.0  # USD — matches zai_proxy default
TELNYX_KEY_ENV = "TELNYX_API_KEY"
TELNYX_STARTING_ENV = "TELNYX_STARTING_BALANCE"


def _resolve_telnyx_starting(explicit: Optional[float]) -> float:
    """Resolve starting balance: explicit arg → TELNYX_STARTING_BALANCE env → 10.0."""
    if explicit is not None:
        return float(explicit)
    raw = os.environ.get(TELNYX_STARTING_ENV)
    if raw is None or raw.strip() == "":
        return TELNYX_DEFAULT_STARTING_BALANCE
    try:
        return float(raw)
    except (TypeError, ValueError):
        return TELNYX_DEFAULT_STARTING_BALANCE


@dataclass
class TelnyxBalance:
    """Self-tracked Telnyx spend & remaining balance.

    total_spent_usd  SUM(cost_usd) from api_calls WHERE key_name='telnyx'
    starting         funded budget (USD) from env or default
    remaining_usd    starting - total_spent_usd (may go negative on overrun)
    usage_fraction   1 - (remaining / starting), clamped to [0, 1]
    is_exhausted     True when remaining <= 0
    collected_at     time.time() when collected
    error            short human string on failure, None on success
    """

    total_spent_usd: Optional[float] = None
    starting: float = TELNYX_DEFAULT_STARTING_BALANCE
    remaining_usd: Optional[float] = None
    usage_fraction: float = 0.0
    is_exhausted: bool = False
    collected_at: float = field(default_factory=time.time)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        """True when spend was retrieved successfully."""
        return self.error is None and self.total_spent_usd is not None

    @property
    def used_pct(self) -> float:
        """Usage as 0–100 % — what live_router reads as ``quota_entry['used_pct']``."""
        return self.usage_fraction * 100.0


# ── Telnyx self-tracking helpers ──────────────────────────────────────────────

def _telnyx_usage_fraction(remaining: Optional[float], starting: float) -> float:
    """Derive a [0,1] usage fraction.

    * starting <= 0 (misconfig) → 0.0 (cold-start path handles conservatism)
    * remaining unknown → 0.0
    * remaining <= 0 → exhausted → 1.0
    * else 1 - remaining/starting, clamped to [0,1]
    """
    if starting <= 0.0:
        return 0.0
    if remaining is None:
        return 0.0
    if remaining <= 0.0:
        return 1.0
    return max(0.0, min(1.0, 1.0 - (remaining / starting)))


def _query_telnyx_spent(db_path: str) -> tuple[Optional[float], Optional[str]]:
    """Query SUM(cost_usd) FROM api_calls WHERE key_name='telnyx'.

    Returns (sum_spent_usd, error_str). Never raises — on any DB error returns
    (None, "<error description>").
    """
    try:
        conn = _connect_db(db_path)
        try:
            # Ensure the api_calls table exists (it should, since the proxy
            # creates it; but be defensive in case this runs on a fresh DB).
            conn.execute(
                """CREATE TABLE IF NOT EXISTS api_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    key_name TEXT,
                    cost_usd REAL
                )"""
            )
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) FROM api_calls "
                "WHERE key_name = 'telnyx'"
            ).fetchone()
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as exc:
        return None, "db error: %s" % exc
    except Exception as exc:
        return None, "unexpected error: %s" % exc

    if not row or row[0] is None:
        # COALESCE guarantees 0.0, but guard defensively
        return 0.0, None
    spent = _as_float(row[0])
    if spent is None:
        return None, "SUM(cost_usd) returned non-numeric value"
    return round(spent, 6), None


# ── Telnyx public collector ───────────────────────────────────────────────────

def collect_telnyx_balance(
    starting: Optional[float] = None,
    *,
    db_path: Optional[str] = None,
) -> TelnyxBalance:
    """Self-track Telnyx spend from the local api_calls table.

    Parameters
    ----------
    starting
        Funded budget in USD. If None, resolves from
        ``TELNYX_STARTING_BALANCE`` env (default 10.0).
    db_path
        Path to the api_burn.db. If None, uses ``default_db_path()``.

    Returns
    -------
    TelnyxBalance
        Spend, remaining, and usage_fraction. On any failure the ``error``
        field names the problem and numeric fields are None. Never raises.
    """
    result = TelnyxBalance()
    result.starting = _resolve_telnyx_starting(starting)
    db = db_path or default_db_path()

    spent, err = _query_telnyx_spent(db)
    if err is not None or spent is None:
        result.error = err or "unknown error"
        return result

    result.total_spent_usd = spent
    result.remaining_usd = round(result.starting - spent, 6)
    result.usage_fraction = _telnyx_usage_fraction(
        result.remaining_usd, result.starting
    )
    result.is_exhausted = (
        result.remaining_usd is not None and result.remaining_usd <= 0.0
    )
    result.collected_at = time.time()
    return result


# ── Telnyx persistence ───────────────────────────────────────────────────────

def store_telnyx_balance(
    db_path: str, balance: Optional[TelnyxBalance]
) -> bool:
    """Append one Telnyx snapshot to the shared table. True on success,
    False (never raises) on DB error or None balance.

    Maps to shared schema:
    usage = total_spent_usd, limit_credits = starting,
    limit_remaining = remaining_usd, is_unlimited = 0 (always finite).
    """
    if balance is None:
        return False
    try:
        conn = _connect_db(db_path)
        try:
            _ensure_table(conn)
            conn.execute(
                f"""
                INSERT INTO {PROVIDER_BALANCES_TABLE}
                    (provider, collected_at, usage, limit_credits,
                     limit_remaining, usage_fraction, is_unlimited,
                     is_free_tier, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "telnyx",
                    balance.collected_at,
                    balance.total_spent_usd,
                    float(balance.starting),
                    balance.remaining_usd,
                    float(balance.usage_fraction),
                    0,  # Telnyx is always finite (credit top-up)
                    None,
                    json.dumps({
                        "total_spent_usd": balance.total_spent_usd,
                        "starting": balance.starting,
                        "remaining_usd": balance.remaining_usd,
                        "method": "self-tracking",
                    }, default=str),
                ),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return False


def get_latest_telnyx_balance(db_path: str) -> Optional[TelnyxBalance]:
    """Most recent stored Telnyx balance, or None (never raises) if none.

    Starting balance is recovered from the stored limit_credits column.
    """
    try:
        conn = _connect_db(db_path)
        try:
            _ensure_table(conn)
            row = conn.execute(
                f"""
                SELECT usage, limit_credits, limit_remaining, usage_fraction,
                       is_unlimited, collected_at, raw_json
                FROM {PROVIDER_BALANCES_TABLE}
                WHERE provider = 'telnyx'
                ORDER BY collected_at DESC LIMIT 1
                """
            ).fetchone()
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return None

    if not row:
        return None
    (usage, limit_credits, limit_remaining, usage_fraction,
     is_unlimited, collected_at, raw_json) = row
    starting_f = float(limit_credits) if limit_credits is not None else TELNYX_DEFAULT_STARTING_BALANCE
    try:
        raw = json.loads(raw_json) if isinstance(raw_json, str) else {}
    except (ValueError, TypeError):
        raw = {}
    return TelnyxBalance(
        total_spent_usd=usage,
        starting=starting_f,
        remaining_usd=limit_remaining,
        usage_fraction=float(usage_fraction) if usage_fraction is not None else 0.0,
        is_exhausted=(limit_remaining is not None and limit_remaining <= 0.0),
        collected_at=float(collected_at) if collected_at is not None else time.time(),
        error=None,
    )


def collect_and_store_telnyx(
    db_path: Optional[str] = None,
    starting: Optional[float] = None,
) -> Optional[TelnyxBalance]:
    """Cron-friendly: collect once, persist, return balance (None on failure).

    Never raises.
    """
    db_path = db_path or default_db_path()
    balance = collect_telnyx_balance(starting=starting, db_path=db_path)
    if balance.ok:
        store_telnyx_balance(db_path, balance)
        return balance
    return None


# ── Telnyx bridge to quota_state['telnyx'] ───────────────────────────────────

_TELNYX_BALANCE_MAX_AGE = 1200.0  # 20 min — 2× the 5-min cadence (slack)


def telnyx_quota_entry(
    db_path: Optional[str] = None,
    *,
    max_age: Optional[float] = _TELNYX_BALANCE_MAX_AGE,
) -> dict:
    """Build the ``quota_state['telnyx']`` entry from the latest stored row.

    The bridge from the collector (``provider_balances`` table) to the
    ``quota_state['telnyx']`` dict that the proxy's ``_snapshot_quota`` reads.
    Mirrors ``ppq_quota_entry`` and ``openrouter_quota_entry``.

    Cold-start contract (matches the proxy's current hardcoded fallback):
      * no stored row, OR the row is older than ``max_age`` (default 20 min,
        2× the 5-min cadence) → return ``{}`` (no ``used_pct`` key). The proxy
        then falls back to ``{used_pct:0.0, remaining:inf}`` (current behavior).
      * fresh row → ``{'used_pct','remaining','starting','is_exhausted',
        'collected_at'}`` with ``used_pct`` in 0–100.

    Pass ``max_age=None`` to use the newest row regardless of age. Never
    raises — any DB/parse error yields the cold-start ``{}`` entry.
    """
    db_path = db_path or default_db_path()
    bal = get_latest_telnyx_balance(db_path)
    if bal is None:
        return {}
    if max_age is not None and (time.time() - bal.collected_at) > max_age:
        return {}
    return {
        "used_pct": float(bal.used_pct),
        "remaining": float(bal.remaining_usd) if bal.remaining_usd is not None else 0.0,
        "starting": float(bal.starting),
        "is_exhausted": bool(bal.is_exhausted),
        "collected_at": float(bal.collected_at),
    }


# ═════════════════════════════════════════════════════════════════════════════
# NEURALWATT COLLECTOR (real /v1/quota API — NW-API)
# ═════════════════════════════════════════════════════════════════════════════
#
# NeuralWatt (api.neuralwatt.com) exposes a real billing API:
#   * GET /v1/quota           → balance, subscription, kWh allowance
#   * GET /v1/usage/summary    → daily cost time_series (period → today → total)
#   * GET /v1/usage/energy     → energy breakdown (unused here)
#
# 2026-08-23: an earlier worker shipped a self-tracking collector because the
# billing endpoints weren't documented yet. Felix solved that — the API is
# verified working and the data is far more accurate than our local
# cost_usd sums from zai_usage.db.api_calls (which overcounts by ~5.7x because
# NeuralWatt uses ENERGY-based pricing with 94% prompt-cache hits).
#
# So this collector calls the REAL API. The self-tracking helpers were dropped;
# the public API surface (collect_neuralwatt_balance / store_neuralwatt_balance
# / get_latest_neuralwatt_balance / collect_and_store_neuralwatt /
# neuralwatt_quota_entry) stays identical so every existing caller keeps working.
# A new helper, get_neuralwatt_cost_correction_factor(), is exposed as a
# per-request cache for zai_proxy._estimate_cost_usd() to scale our per-token
# estimate down to the real spend ratio. Backward-compatible: any failure →
# old {used_pct:0.0, remaining:inf} cold-start projection.
#
# Verified /v1/quota sample (2026-08-23T16:55Z):
#   balance.credits_remaining_usd = 8.9951 / total_credits_usd = 11.0
#   usage.lifetime.cost_usd       = 51.3019  (real, /v1/usage/summary total)
#   subscription.kwh_included     = 13.3333 / kwh_used = 6.5729 → 49% used
#   subscription.kwh_remaining    = 6.7604  (≈2 days at current burn rate)
#   subscription.status           = "active" / current_period_end = +30d ISO
#   limits.rate_limit_tier        = "pro"
#
# Real cost today (/v1/usage/summary time_series[2026-08-23].cost_usd) ≈ $45.6,
# vs our zai_usage.db shows $258 — the 5.7× overcount is what forces us to use
# the API directly instead of DB self-tracking.

NEURALWATT_API_BASE           = "https://api.neuralwatt.com/v1"
NEURALWATT_QUOTA_ENDPOINT     = NEURALWATT_API_BASE + "/quota"
NEURALWATT_USAGE_SUMMARY_URL  = NEURALWATT_API_BASE + "/usage/summary"
NEURALWATT_DEFAULT_TIMEOUT    = 5.0          # seconds — collector must be fast
NEURALWATT_KEY_ENV            = "NEURALWATT_API_KEY"
NEURALWATT_STARTING_ENV       = "NEURALWATT_STARTING_BALANCE"   # legacy compat
NEURALWATT_DEFAULT_DAILY_CAP   = 10.0          # USD/day — runaway-burn guardrail
NEURALWATT_DAILY_CAP_ENV      = "NEURALWATT_DAILY_CAP"
NEURALWATT_DEFAULT_STARTING_BALANCE = 100.0   # legacy compat, unused by collect


# ── NeuralWatt env resolution (mirrors PPQ pattern) ──────────────────────────

def _resolve_neuralwatt_daily_cap(explicit: Optional[float]) -> float:
    """Resolve daily cap: explicit arg → NEURALWATT_DAILY_CAP env → 10."""
    if explicit is not None:
        return float(explicit)
    raw = os.environ.get(NEURALWATT_DAILY_CAP_ENV)
    if raw is None or raw.strip() == "":
        return NEURALWATT_DEFAULT_DAILY_CAP
    try:
        return float(raw)
    except (TypeError, ValueError):
        return NEURALWATT_DEFAULT_DAILY_CAP


def _resolve_neuralwatt_key(api_key: Optional[str]) -> Optional[str]:
    """Return a stripped API key (env fallback), or None if not set."""
    if api_key is not None and api_key.strip():
        return api_key.strip()
    return os.environ.get(NEURALWATT_KEY_ENV, "").strip() or None


@dataclass
class NeuralWattBalance:
    """Real-API NeuralWatt balance, kWh allowance, and daily-cap status.

    Populated from GET /v1/quota (verified 2026-08-23). The primary usage
    metric is energy (kWh), not per-token cost — kwh_used/kwh_included drives
    ``usage_fraction`` because NeuralWatt's $100/mo Pro plan includes an
    energy allowance, not a token allowance.

    remaining_usd        balance.credits_remaining_usd (None on API failure)
    total_credits_usd    balance.total_credits_usd
    kwh_used             subscription.kwh_used
    kwh_remaining        subscription.kwh_remaining (None on missing field)
    kwh_included         subscription.kwh_included
    usage_fraction       clamp(kwh_used / kwh_included, 0, 1) — 0.0 on missing
    cost_usd             usage.lifetime.cost_usd (real total spend, USD)
    subscription_status  subscription.status ("active" / "canceled" / ...)
    period_end           subscription.current_period_end (ISO 8601 string)
    is_exhausted         True when kwh_remaining <= 0 or in_overage
    daily_spent_usd      real USD spent today (from /v1/usage/summary).
                         0.0 if the summary fetch failed (deterministic).
    daily_cap_usd        configured daily cap (NEURALWATT_DAILY_CAP env, default 10)
    is_daily_cap_exceeded    True when daily_spent_usd > daily_cap_usd
    collected_at         time.time() when collected
    error                short human string on failure, None on success
    raw                  the full JSON dict from /v1/quota (for forensics/logging)
    """

    remaining_usd: Optional[float] = None
    total_credits_usd: Optional[float] = None
    kwh_used: Optional[float] = None
    kwh_remaining: Optional[float] = None
    kwh_included: Optional[float] = None
    usage_fraction: float = 0.0
    cost_usd: Optional[float] = None
    subscription_status: Optional[str] = None
    period_end: Optional[str] = None
    is_exhausted: bool = False
    daily_spent_usd: Optional[float] = None
    daily_cap_usd: float = NEURALWATT_DEFAULT_DAILY_CAP
    is_daily_cap_exceeded: bool = False
    collected_at: float = field(default_factory=time.time)
    error: Optional[str] = None
    raw: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True when the /v1/quota fetch succeeded (subscription block present)."""
        return self.error is None and self.kwh_used is not None

    @property
    def used_pct(self) -> float:
        """Usage as 0–100 % — what live_router reads as ``quota_entry['used_pct']``."""
        return self.usage_fraction * 100.0

    # ── backward-compat properties ──────────────────────────────────────────
    # Older callers (cron consumers, JSON dumps) read total_spent_usd /
    # starting_usd. Map them onto the new fields so they keep working without
    # a separate mapper. ``starting_usd`` -> total_credits_usd (the funded
    # budget); ``total_spent_usd`` -> cost_usd (real lifetime spend).

    @property
    def total_spent_usd(self) -> Optional[float]:
        """Backward-compat alias for ``cost_usd`` (real lifetime spend, USD)."""
        return self.cost_usd

    @property
    def starting_usd(self) -> Optional[float]:
        """Backward-compat alias for ``total_credits_usd`` (funded credit pool)."""
        return self.total_credits_usd


# ── NeuralWatt pure helpers ──────────────────────────────────────────────────

def _neuralwatt_usage_fraction(
    kwh_used: Optional[float], kwh_included: Optional[float]
) -> float:
    """Derive a [0,1] usage fraction from the energy allowance.

    * kwh_included missing / <= 0 → 0.0 (we don't know the budget).
    * kwh_used missing / <= 0 → 0.0 (no real usage or refunds).
    * else clamp(kwh_used / kwh_included, 0, 1).
    """
    if kwh_included is None or kwh_included <= 0.0:
        return 0.0
    if kwh_used is None or kwh_used <= 0.0:
        return 0.0
    return max(0.0, min(1.0, kwh_used / kwh_included))


def _neuralwatt_http_get(
    url: str, api_key: str, timeout: float
) -> tuple[Optional[dict], Optional[str]]:
    """GET ``url`` with Bearer auth, return ``(parsed_json_or_None, error_str)``.

    Never raises — any transport/parse failure returns ``(None, "msg")`` so
    callers can fallback safely. Mirrors the robustness contract of the
    DeepInfra/PPQ collectors.
    """
    try:
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": "hermes-balance-collector",
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            if getattr(resp, "status", 200) != 200:
                return None, f"HTTP {resp.status} from {url}"
            body = resp.read()
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # pragma: no cover — defensive only
            detail = ""
        return None, f"HTTP {exc.code} ({url}): {detail}".strip()
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return None, f"network error ({url}): {exc}"
    except Exception as exc:  # pragma: no cover — defensive only
        return None, f"unexpected error ({url}): {exc}"

    try:
        obj = json.loads(body.decode("utf-8", "ignore"))
    except (ValueError, UnicodeDecodeError) as exc:
        return None, f"json parse error: {exc}"
    if not isinstance(obj, dict):
        return None, f"unexpected response type ({type(obj).__name__})"
    return obj, None


def _fetch_neuralwatt_daily_spend(
    api_key: str,
    *,
    timeout: float = NEURALWATT_DEFAULT_TIMEOUT,
    endpoint: str = NEURALWATT_USAGE_SUMMARY_URL,
    http_get: Optional[Callable[[str, str, float], tuple[Optional[dict], Optional[str]]]] = None,
) -> tuple[Optional[float], Optional[str]]:
    """Fetch today's spend in USD from /v1/usage/summary.time_series.

    Returns ``(daily_spend_usd, error_str)``. On any failure the spend is
    ``None`` and the caller should treat the daily cap as not-yet-determined
    (i.e., NOT exceeded). Never raises.
    """
    get_fn = http_get or _neuralwatt_http_get
    obj, err = get_fn(endpoint, api_key, timeout)
    if err is not None or obj is None:
        return None, err or "unknown error"

    today_iso = time.strftime("%Y-%m-%d", time.gmtime())
    series = obj.get("time_series") or []
    total_today: Optional[float] = None
    if isinstance(series, list):
        for entry in series:
            if not isinstance(entry, dict):
                continue
            if entry.get("date") == today_iso:
                cost = _as_float(entry.get("cost_usd"))
                if cost is not None:
                    total_today = (total_today or 0.0) + cost
                break  # time_series has at most one entry per date
    if total_today is None:
        total_today = 0.0  # no row for today → zero spend, not None
    return round(total_today, 6), None


# ── NeuralWatt public collector ─────────────────────────────────────────────

def collect_neuralwatt_balance(
    api_key: Optional[str] = None,
    *,
    daily_cap: Optional[float] = None,
    timeout: float = NEURALWATT_DEFAULT_TIMEOUT,
    quota_endpoint: str = NEURALWATT_QUOTA_ENDPOINT,
    summary_endpoint: str = NEURALWATT_USAGE_SUMMARY_URL,
    http_get: Optional[Callable[[str, str, float], tuple[Optional[dict], Optional[str]]]] = None,
) -> NeuralWattBalance:
    """Query the NeuralWatt /v1/quota API and return the parsed balance.

    Parameters
    ----------
    api_key
        NeuralWatt API key (``sk-...``). If None, resolves from
        ``NEURALWATT_API_KEY`` env. Returns a balance with ``error`` set and
        all numeric fields None when no key is configured.
    daily_cap
        Daily spend cap in USD. If None, resolves from ``NEURALWATT_DAILY_CAP``
        env (default $10/day). When today's real spend from /v1/usage/summary
        exceeds this, ``is_daily_cap_exceeded`` becomes True — the routing
        layer should remove NeuralWatt from rotation until UTC midnight.
    timeout
        Per-request HTTP timeout in seconds (default 5.0).
    quota_endpoint
        Override the /v1/quota URL (tests use this to inject a mock server).
    summary_endpoint
        Override the /v1/usage/summary URL.
    http_get
        Test seam for the HTTP transport. Defaults to
        :func:`_neuralwatt_http_get`. Signature:
        ``(url, key, timeout) -> (parsed_json_or_None, error_str_or_None)``.

    Returns
    -------
    NeuralWattBalance
        Energy balance, kWh allowance, daily spend, and cap status. On any
        failure the ``error`` field names the problem and numeric fields stay
        None. Never raises.
    """
    result = NeuralWattBalance()
    result.daily_cap_usd = _resolve_neuralwatt_daily_cap(daily_cap)

    key = _resolve_neuralwatt_key(api_key)
    if not key:
        result.error = f"{NEURALWATT_KEY_ENV} not set"
        result.collected_at = time.time()
        return result

    get_fn = http_get or _neuralwatt_http_get

    # 1) /v1/quota — balance + subscription + lifetime cost
    obj, err = get_fn(quota_endpoint, key, timeout)
    if err is not None or obj is None:
        result.error = err or "/v1/quota returned empty body"
        result.collected_at = time.time()
        return result
    result.raw = obj

    balance_block = obj.get("balance") or {}
    sub_block = obj.get("subscription") or {}
    usage_block = obj.get("usage") or {}
    lifetime_block = usage_block.get("lifetime") or {}

    result.remaining_usd = _as_float(balance_block.get("credits_remaining_usd"))
    result.total_credits_usd = _as_float(balance_block.get("total_credits_usd"))
    result.kwh_used = _as_float(sub_block.get("kwh_used"))
    result.kwh_remaining = _as_float(sub_block.get("kwh_remaining"))
    result.kwh_included = _as_float(sub_block.get("kwh_included"))
    result.cost_usd = _as_float(lifetime_block.get("cost_usd"))
    result.subscription_status = (
        sub_block.get("status") if isinstance(sub_block.get("status"), str) else None
    )
    result.period_end = (
        sub_block.get("current_period_end")
        if isinstance(sub_block.get("current_period_end"), str)
        else None
    )
    result.usage_fraction = _neuralwatt_usage_fraction(result.kwh_used, result.kwh_included)
    result.is_exhausted = bool(
        sub_block.get("in_overage")
        or (result.kwh_remaining is not None and result.kwh_remaining <= 0.0)
    )

    # 2) /v1/usage/summary — real daily spend for the cap guardrail.
    # If the summary call fails, daily_spent_usd stays None and the cap stays
    # open (we *don't* mark it exceeded from a missing datapoint — the monthly
    # allowance is the primary exhaustion signal).
    daily_spend, _ = _fetch_neuralwatt_daily_spend(
        key, timeout=timeout, endpoint=summary_endpoint, http_get=get_fn,
    ) if summary_endpoint else (None, None)
    result.daily_spent_usd = daily_spend
    result.is_daily_cap_exceeded = (
        result.daily_cap_usd > 0.0
        and result.daily_spent_usd is not None
        and result.daily_spent_usd > result.daily_cap_usd
    )

    result.collected_at = time.time()
    return result


# ── NeuralWatt persistence ──────────────────────────────────────────────────

def store_neuralwatt_balance(
    db_path: str, balance: Optional[NeuralWattBalance]
) -> bool:
    """Append one NeuralWatt snapshot to the shared provider_balances table.

    Maps to shared schema:
      * usage           = kwh_used  (real energy consumed this period)
      * limit_credits   = kwh_included (energy allowance for the period)
      * limit_remaining = kwh_remaining
      * usage_fraction  = kwh_used / kwh_included (clamped)
      * is_unlimited    = 0 (NeuralWatt is always finite — paid Pro plan)
      * is_free_tier    = 0 (paid subscription)
      * raw_json        = remaining_usd, total_credits_usd, cost_usd_lifetime,
                          subscription_status, period_end, daily_cap_*,
                          in_overage, period_start, key name, full quota dict

    True on success. False (never raises) on DB error, None balance, or a
    failed balance (``error`` set). We never persist a snapshot we know was
    broken — important because ``neuralwatt_quota_entry`` reads the latest row.
    """
    if balance is None or not balance.ok:
        return False
    try:
        conn = _connect_db(db_path)
        try:
            _ensure_table(conn)
            conn.execute(
                f"""
                INSERT INTO {PROVIDER_BALANCES_TABLE}
                    (provider, collected_at, usage, limit_credits,
                     limit_remaining, usage_fraction, is_unlimited,
                     is_free_tier, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "neuralwatt",
                    balance.collected_at,
                    float(balance.kwh_used) if balance.kwh_used is not None else None,
                    float(balance.kwh_included) if balance.kwh_included is not None else None,
                    (float(balance.kwh_remaining)
                     if balance.kwh_remaining is not None else None),
                    float(balance.usage_fraction),
                    0,  # paid plan, always finite
                    0,  # not free-tier
                    json.dumps({
                        "remaining_usd": balance.remaining_usd,
                        "total_credits_usd": balance.total_credits_usd,
                        "kwh_used": balance.kwh_used,
                        "kwh_remaining": balance.kwh_remaining,
                        "kwh_included": balance.kwh_included,
                        "cost_usd_lifetime": balance.cost_usd,
                        "subscription_status": balance.subscription_status,
                        "period_end": balance.period_end,
                        "daily_spent_usd": balance.daily_spent_usd,
                        "daily_cap_usd": balance.daily_cap_usd,
                        "is_daily_cap_exceeded": balance.is_daily_cap_exceeded,
                        "is_exhausted": balance.is_exhausted,
                        "method": "real-api",
                    }, default=str),
                ),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return False


def get_latest_neuralwatt_balance(db_path: str) -> Optional[NeuralWattBalance]:
    """Most recent stored NeuralWatt balance, or None (never raises) if none.

    All API-derived fields are recovered from ``raw_json``; the energy
    fields also live in the shared ``usage`` / ``limit_credits`` /
    ``limit_remaining`` columns (so a non-NeuralWatt consumer reading those
    sees kWh values they don't care about).
    """
    try:
        conn = _connect_db(db_path)
        try:
            _ensure_table(conn)
            row = conn.execute(
                f"""
                SELECT usage, limit_credits, limit_remaining, usage_fraction,
                       is_unlimited, collected_at, raw_json
                FROM {PROVIDER_BALANCES_TABLE}
                WHERE provider = 'neuralwatt'
                ORDER BY collected_at DESC LIMIT 1
                """
            ).fetchone()
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return None

    if not row:
        return None
    (usage, limit_credits, limit_remaining, usage_fraction,
     is_unlimited, collected_at, raw_json) = row
    try:
        raw = json.loads(raw_json) if isinstance(raw_json, str) else {}
    except (ValueError, TypeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}

    kwh_used = _as_float(usage) if usage is not None else raw.get("kwh_used")
    kwh_incl = (_as_float(limit_credits)
                if limit_credits is not None else raw.get("kwh_included"))
    kwh_rem = (_as_float(limit_remaining)
               if limit_remaining is not None else raw.get("kwh_remaining"))
    return NeuralWattBalance(
        remaining_usd=raw.get("remaining_usd"),
        total_credits_usd=raw.get("total_credits_usd"),
        kwh_used=kwh_used,
        kwh_remaining=kwh_rem,
        kwh_included=kwh_incl,
        usage_fraction=(float(usage_fraction) if usage_fraction is not None else 0.0),
        cost_usd=raw.get("cost_usd_lifetime"),
        subscription_status=raw.get("subscription_status"),
        period_end=raw.get("period_end"),
        is_exhausted=bool(raw.get("is_exhausted", False)),
        daily_spent_usd=raw.get("daily_spent_usd"),
        daily_cap_usd=(raw.get("daily_cap_usd",
                               _resolve_neuralwatt_daily_cap(None))
                       if raw.get("daily_cap_usd") is not None
                       else _resolve_neuralwatt_daily_cap(None)),
        is_daily_cap_exceeded=bool(raw.get("is_daily_cap_exceeded", False)),
        collected_at=(float(collected_at)
                      if collected_at is not None else time.time()),
        error=None,
        raw=raw,
    )


def collect_and_store_neuralwatt(
    db_path: Optional[str] = None,
    api_key: Optional[str] = None,
    daily_cap: Optional[float] = None,
    timeout: float = NEURALWATT_DEFAULT_TIMEOUT,
    *,
    usage_db_path: Optional[str] = None,  # legacy compat — ignored by the API path
    balances_db_path: Optional[str] = None,
) -> Optional[NeuralWattBalance]:
    """Cron-friendly: collect once via /v1/quota, persist, return balance.

    Returns None on failure (no key, API error, store failure). Never raises.
    Legacy callers may pass ``usage_db_path`` from the self-tracking era — it
    is accepted for backward compatibility but not used.
    """
    bdb = balances_db_path or db_path or default_db_path()
    balance = collect_neuralwatt_balance(
        api_key=api_key, daily_cap=daily_cap, timeout=timeout,
    )
    if balance.ok:
        store_neuralwatt_balance(bdb, balance)
        return balance
    return None


# ── NeuralWatt bridge to quota_state['neuralwatt'] ───────────────────────────

_NEURALWATT_BALANCE_MAX_AGE = 1200.0  # 20 min — 2× the 5-min cadence (slack)


def neuralwatt_quota_entry(
    db_path: Optional[str] = None,
    *,
    max_age: Optional[float] = _NEURALWATT_BALANCE_MAX_AGE,
) -> dict:
    """Build the ``quota_state['neuralwatt']`` entry from the latest stored row.

    The bridge from the collector (``provider_balances`` table in api_burn.db)
    to the ``quota_state['neuralwatt']`` dict that the proxy's
    ``_snapshot_quota`` reads. Mirrors ``telnyx_quota_entry`` /
    ``ppq_quota_entry``.

    Cold-start contract (back-compat with the old hardcoded fallback):
      * no stored row, OR the row is older than ``max_age`` (default 20 min,
        i.e. 2× the 5-min collection cadence) → return ``{}`` (no ``used_pct``
        key). The proxy then falls back to ``{used_pct:0.0, remaining:inf}``
        (the pre-bridge optimistic behavior) so routing never breaks.
      * fresh row → ``{'used_pct','remaining','total','is_exhausted',
        'is_daily_cap_exceeded','daily_spent_usd','daily_cap_usd',
        'collected_at'}`` with ``used_pct`` in 0–100 derived from
        kwh_used/kwh_included (the real monthly allowance usage).

    Pass ``max_age=None`` to use the newest row regardless of age. Never
    raises — any DB/parse error yields the cold-start ``{}`` entry.
    """
    db_path = db_path or default_db_path()
    bal = get_latest_neuralwatt_balance(db_path)
    if bal is None:
        return {}
    if max_age is not None and (time.time() - bal.collected_at) > max_age:
        return {}
    return {
        "used_pct": float(bal.used_pct),
        "remaining": (float(bal.kwh_remaining)
                      if bal.kwh_remaining is not None else 0.0),
        "total": (float(bal.kwh_included)
                  if bal.kwh_included is not None else 1.0),
        "is_exhausted": bool(bal.is_exhausted),
        "is_daily_cap_exceeded": bool(bal.is_daily_cap_exceeded),
        "daily_spent_usd": (float(bal.daily_spent_usd)
                            if bal.daily_spent_usd is not None else 0.0),
        "daily_cap_usd": float(bal.daily_cap_usd),
        "collected_at": float(bal.collected_at),
        # Extra fields for richer dashboards / debugging:
        "remaining_usd": (float(bal.remaining_usd)
                         if bal.remaining_usd is not None else 0.0),
        "total_credits_usd": (float(bal.total_credits_usd)
                              if bal.total_credits_usd is not None else 0.0),
        "cost_usd_lifetime": (float(bal.cost_usd)
                              if bal.cost_usd is not None else 0.0),
        "subscription_status": bal.subscription_status or "unknown",
        "period_end": bal.period_end or "",
    }


# ── NeuralWatt cost-correction factor for zai_proxy._estimate_cost_usd ──────
#
# Our per-token cost estimate (NEURALWATT_RATES × tokens) overcounts by ~5.7×
# compared to the real API-tracked spend because NeuralWatt uses ENERGY-based
# pricing with 94% prompt-cache hits (389M of 412M tokens cached at a 5×
# discount). For accurate cost_logging in api_calls, the proxy multiplies the
# estimate by this factor: real_api_total / db_sum_cost_usd. Cached 10 minutes.

_NEURALWATT_COST_CORRECTION_TTL = 600.0   # 10 minutes
_neuralwatt_cost_correction_cache: dict[str, Any] = {
    "value": 1.0,
    "ts": 0.0,
}


def get_neuralwatt_cost_correction_factor(
    usage_db_path: Optional[str] = None,
    api_key: Optional[str] = None,
    *,
    refresh: bool = False,
) -> float:
    """Compute the ratio of real NeuralWatt spend to our DB-tracked sum.

    Cached 10 minutes; pass ``refresh=True`` to bypass. The ratio is::

        factor = real_total_cost_usd / db_sum_cost_usd_for_neuralwatt_tier

    When applied to a per-token cost estimate (cost_per_1m × tokens / 1e6),
    the resulting figure is within ~5% of the real NeuroWatt bill — the 5.7×
    overcounting comes from prompt-cache discounts the local pricing tables
    can't see.

    Defaults to 1.0 (no correction) when: cache miss + fresh fetch fails, no
    DB rows, env var ``NEURALWATT_COST_CORRECTION`` override takes precedence,
    NaN/∞ result, etc. Never raises.
    """
    # 1) Env override — operators can pin a constant if the API is down.
    env = os.environ.get("NEURALWATT_COST_CORRECTION", "").strip()
    if env:
        try:
            val = float(env)
            if 0.0 <= val <= 1.0:
                return val
        except (TypeError, ValueError):
            pass  # ignore malformed env value

    now = time.time()
    if (not refresh
            and now - _neuralwatt_cost_correction_cache["ts"]
                < _NEURALWATT_COST_CORRECTION_TTL):
        cached = _neuralwatt_cost_correction_cache["value"]
        return cached if cached is not None else 1.0

    factor: float = 1.0
    try:
        # 1) Real total from /v1/usage/summary
        key = _resolve_neuralwatt_key(api_key)
        real_total: Optional[float] = None
        if key:
            obj, err = _neuralwatt_http_get(
                NEURALWATT_USAGE_SUMMARY_URL, key, NEURALWATT_DEFAULT_TIMEOUT)
            if err is None and isinstance(obj, dict):
                tbl = obj.get("totals") or {}
                real_total = _as_float(tbl.get("total_cost_usd"))

        # 2) DB-summed cost_usd where tier='neuralwatt'
        usage_db = usage_db_path or default_usage_db_path()
        db_total: Optional[float] = None
        try:
            conn = _connect_db(usage_db)
            try:
                # Defensive: CREATE TABLE IF NOT EXISTS is a no-op when the
                # production schema (which has many more columns) exists.
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS api_calls ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "ts REAL NOT NULL, cost_usd REAL, tier TEXT)"
                )
                row = conn.execute(
                    "SELECT COALESCE(SUM(cost_usd), 0.0) FROM api_calls "
                    "WHERE tier = 'neuralwatt'"
                ).fetchone()
                if row is not None and row[0] is not None:
                    db_total = _as_float(row[0])
            finally:
                conn.close()
        except (sqlite3.Error, OSError):
            db_total = None

        # 3) Avoid the obvious edge cases (no DB, no API, zero, NaN).
        if real_total is not None and db_total is not None and db_total > 0:
            raw_factor = real_total / db_total
            # Sanity clamp: avoid absurd multipliers if the API briefly lies.
            if 0.0 <= raw_factor <= 1.0:
                factor = raw_factor
            elif raw_factor > 1.0:
                # If we UNDERcount (rare), don't scale up — just return 1.0
                # so we never over-report spend on the proxy side.
                factor = 1.0
    except Exception:  # pragma: no cover — defensive only
        factor = 1.0

    _neuralwatt_cost_correction_cache["value"] = factor
    _neuralwatt_cost_correction_cache["ts"] = now
    return factor


# ═════════════════════════════════════════════════════════════════════════════
# CLI DISPATCHER
# ═════════════════════════════════════════════════════════════════════════════

def _ppq_main(argv: list[str]) -> int:
    """PPQ cron entrypoint: collect once, print JSON status, exit 0/1."""
    db_override = None
    if "--db" in argv:
        db_override = argv[argv.index("--db") + 1]
    db_path = db_override or default_db_path()
    starting_bal = _resolve_starting(None)
    balance = collect_and_store_ppq(db_path=db_path, starting=starting_bal)
    if balance is None:
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


def _openrouter_main(argv: list[str]) -> int:
    """OpenRouter cron entrypoint: collect once, print JSON status, exit 0/1."""
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


def _telnyx_main(argv: list[str]) -> int:
    """Telnyx cron entrypoint: self-track once, print JSON status, exit 0/1."""
    db_override = None
    if "--db" in argv:
        db_override = argv[argv.index("--db") + 1]
    db_path = db_override or default_db_path()
    starting_bal = _resolve_telnyx_starting(None)
    balance = collect_and_store_telnyx(db_path=db_path, starting=starting_bal)
    if balance is None:
        starting_env = os.environ.get(TELNYX_STARTING_ENV, "").strip()
        reason = ("TELNYX_STARTING_BALANCE not set"
                  if not starting_env
                  else "self-tracking query failed (see logs)")
        print(json.dumps({"provider": "telnyx", "ok": False, "error": reason}))
        return 1
    print(json.dumps({
        "provider": "telnyx",
        "ok": True,
        "total_spent_usd": balance.total_spent_usd,
        "starting": balance.starting,
        "remaining_usd": balance.remaining_usd,
        "usage_fraction": balance.usage_fraction,
        "used_pct": balance.used_pct,
        "is_exhausted": balance.is_exhausted,
        "collected_at": balance.collected_at,
        "db_path": db_path,
    }, default=str))
    return 0


# ═════════════════════════════════════════════════════════════════════════════
# ROUTSTR (our VPS2 node) — sats balance via GET /v1/wallet/balance
# ═════════════════════════════════════════════════════════════════════════════

ROUTSTR_KEY_ENV = "ROUTSTR_API_KEY"
ROUTSTR_BASE_ENV = "ROUTSTR_BASE"
ROUTSTR_STARTING_ENV = "ROUTSTR_STARTING_BALANCE_SATS"
ROUTSTR_DEFAULT_BASE = "http://23.182.128.51:8009"
_ROUTSTR_BALANCE_MAX_AGE = 20 * 60.0  # 2x the 5-min cron cadence

_btc_usd_cache: dict = {"rate": None, "ts": 0.0}


def _btc_usd_rate() -> float:
    """BTC/USD rate: env override → cached CoinGecko fetch → 100000 default.

    Needed to convert the routstr sats balance into USD for the shared
    provider_balances schema. Never raises.
    """
    env_rate = os.environ.get("BTC_USD_RATE", "").strip()
    if env_rate:
        try:
            return float(env_rate)
        except ValueError:
            pass
    if _btc_usd_cache["rate"] is not None and time.time() - _btc_usd_cache["ts"] < 600:
        return _btc_usd_cache["rate"]
    try:
        req = urllib.request.Request(
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=bitcoin&vs_currencies=usd",
            headers={"User-Agent": "hermes-balance-collector"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            rate = float(data["bitcoin"]["usd"])
            _btc_usd_cache["rate"] = rate
            _btc_usd_cache["ts"] = time.time()
            return rate
    except Exception:
        return 100000.0


def fetch_routstr_balance_sats() -> tuple[Optional[int], Optional[str]]:
    """Query our Routstr node's key balance (sats).

    2026-08-22: current routstr proxy exposes GET /v1/balance/info (same
    ``balance`` field). Older builds used GET /v1/wallet/balance — kept as
    fallback. Returns (balance_sats, error_str). Never raises.
    """
    key = os.environ.get(ROUTSTR_KEY_ENV, "").strip()
    if not key:
        return None, "ROUTSTR_API_KEY not set"
    base = os.environ.get(ROUTSTR_BASE_ENV, "").strip() or ROUTSTR_DEFAULT_BASE
    last_err: Optional[str] = None
    for path in ("/v1/balance/info", "/v1/wallet/balance"):
        try:
            req = urllib.request.Request(
                base + path,
                headers={"Authorization": f"Bearer {key}"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            bal = data.get("balance")
            if bal is not None:
                return int(bal), None
            last_err = f"balance field missing in response ({path})"
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code} ({path}): {e.read().decode()[:200]}"
        except Exception as exc:
            last_err = f"error ({path}): {exc}"
    return None, last_err or "unknown error"


def collect_routstr_balance(
    starting_sats: Optional[int] = None,
    *,
    db_path: Optional[str] = None,
) -> dict:
    """Collect our Routstr node wallet balance (sats) and convert to USD.

    Returns a plain dict (mirrors the TelnyxBalance fields used downstream):
    starting / remaining_usd / total_spent_usd / usage_fraction / used_pct /
    is_exhausted / collected_at / balance_sats / btc_usd / error.
    Never raises.
    """
    starting = starting_sats
    if starting is None:
        env = os.environ.get(ROUTSTR_STARTING_ENV, "").strip()
        try:
            starting = int(env) if env else 25000
        except ValueError:
            starting = 25000

    result = {
        "starting": float(starting),
        "balance_sats": None,
        "btc_usd": None,
        "remaining_usd": None,
        "total_spent_usd": None,
        "usage_fraction": None,
        "used_pct": None,
        "is_exhausted": None,
        "collected_at": None,
        "error": None,
    }

    sats, err = fetch_routstr_balance_sats()
    if err is not None or sats is None:
        result["error"] = err or "unknown error"
        return result

    btc_usd = _btc_usd_rate()
    remaining_usd = sats / 1e8 * btc_usd
    # 2026-08-22 fix: `starting` is in SATS — convert to USD before mixing
    # with remaining_usd, otherwise spent/used_pct subtract sats from dollars
    # and every wallet reads ~99.9% used (gate nearly fail-closed).
    starting_usd = result["starting"] / 1e8 * btc_usd
    spent_usd = max(0.0, starting_usd - remaining_usd)
    frac = 1.0 - (remaining_usd / starting_usd) if starting_usd > 0 else 1.0

    result.update({
        "balance_sats": sats,
        "btc_usd": btc_usd,
        "remaining_usd": round(remaining_usd, 6),
        "total_spent_usd": round(spent_usd, 6),
        "usage_fraction": round(frac, 6),
        "used_pct": round(frac * 100.0, 4),
        "is_exhausted": sats <= 0,
        "collected_at": time.time(),
    })
    return result


def store_routstr_balance(db_path: str, balance: Optional[dict]) -> bool:
    """Append one routstr snapshot to provider_balances. True on success."""
    if balance is None or balance.get("error") is not None:
        return False
    try:
        conn = _connect_db(db_path)
        try:
            _ensure_table(conn)
            conn.execute(
                f"""
                INSERT INTO {PROVIDER_BALANCES_TABLE}
                    (provider, collected_at, usage, limit_credits,
                     limit_remaining, usage_fraction, is_unlimited,
                     is_free_tier, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "routstr",
                    balance["collected_at"],
                    balance["total_spent_usd"],
                    balance["starting"],
                    balance["remaining_usd"],
                    float(balance["usage_fraction"]),
                    0,
                    0,
                    json.dumps({
                        "balance_sats": balance["balance_sats"],
                        "btc_usd": balance["btc_usd"],
                    }),
                ),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:
        return False


def get_latest_routstr_balance(db_path: str) -> Optional[dict]:
    """Newest routstr row from provider_balances as the collector dict, or None."""
    try:
        conn = _connect_db(db_path)
        try:
            _ensure_table(conn)
            row = conn.execute(
                f"SELECT collected_at, usage, limit_credits, limit_remaining, "
                f"usage_fraction, raw_json FROM {PROVIDER_BALANCES_TABLE} "
                f"WHERE provider = 'routstr' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            collected_at, usage, limit_credits, limit_remaining, frac, raw = row
            starting = float(limit_credits) if limit_credits is not None else 25000.0
            remaining = float(limit_remaining) if limit_remaining is not None else 0.0
            try:
                extra = json.loads(raw) if raw else {}
            except Exception:
                extra = {}
            out = {
                "starting": starting,
                "balance_sats": extra.get("balance_sats"),
                "btc_usd": extra.get("btc_usd"),
                "remaining_usd": remaining,
                "total_spent_usd": float(usage) if usage is not None else 0.0,
                "usage_fraction": float(frac) if frac is not None else 1.0,
                "used_pct": (float(frac) * 100.0) if frac is not None else 100.0,
                "is_exhausted": remaining <= 0.0,
                "collected_at": float(collected_at) if collected_at is not None else 0.0,
                "error": None,
            }
            return out
        finally:
            conn.close()
    except Exception:
        return None


def routstr_quota_entry(
    db_path: Optional[str] = None,
    *,
    max_age: Optional[float] = _ROUTSTR_BALANCE_MAX_AGE,
) -> dict:
    """Build the ``quota_state['routstr']`` entry from the latest stored row.

    Cold-start contract mirrors telnyx_quota_entry: no fresh row → {} (proxy
    falls back to optimistic). Fresh row → used_pct/remaining/starting/...
    Never raises.
    """
    db_path = db_path or default_db_path()
    bal = get_latest_routstr_balance(db_path)
    if bal is None:
        return {}
    if max_age is not None and (time.time() - bal["collected_at"]) > max_age:
        return {}
    return {
        "used_pct": float(bal["used_pct"]),
        "remaining": float(bal["remaining_usd"]),
        "starting": float(bal["starting"]),
        "is_exhausted": bool(bal["is_exhausted"]),
        "collected_at": float(bal["collected_at"]),
    }


def _routstr_main(argv: list[str]) -> int:
    """Routstr cron entrypoint: collect once, print JSON status, exit 0/1."""
    db_override = None
    if "--db" in argv:
        db_override = argv[argv.index("--db") + 1]
    db_path = db_override or default_db_path()
    balance = collect_routstr_balance()
    stored = store_routstr_balance(db_path, balance) if balance.get("error") is None else False
    if not stored:
        print(json.dumps({
            "provider": "routstr",
            "ok": False,
            "error": balance.get("error") or "store failed",
        }))
        return 1
    print(json.dumps({
        "provider": "routstr",
        "ok": True,
        "balance_sats": balance["balance_sats"],
        "btc_usd": balance["btc_usd"],
        "starting": balance["starting"],
        "remaining_usd": balance["remaining_usd"],
        "used_pct": balance["used_pct"],
        "is_exhausted": balance["is_exhausted"],
        "collected_at": balance["collected_at"],
        "db_path": db_path,
    }, default=str))
    return 0


def _neuralwatt_main(argv: list[str]) -> int:
    """NeuralWatt cron entrypoint: query /v1/quota, print JSON, exit 0/1.

    Reads NEURALWATT_API_KEY from env and writes a provider_balances row into
    api_burn.db (``--db`` or default). ``--usage-db`` is accepted for backward
    compatibility but unused by the real-API path. Prints one JSON status line
    so the cron wrapper can grep ``"ok": true``.
    """
    db_override = None
    if "--db" in argv:
        db_override = argv[argv.index("--db") + 1]
    # Legacy --usage-db flag is accepted but no longer used by the API path.
    if "--usage-db" in argv:
        _ = argv[argv.index("--usage-db") + 1]
    balances_db = db_override or default_db_path()
    balance = collect_and_store_neuralwatt(db_path=balances_db)
    if balance is None:
        key_set = bool(os.environ.get(NEURALWATT_KEY_ENV, "").strip())
        reason = (f"{NEURALWATT_KEY_ENV} not set"
                  if not key_set else "/v1/quota API call failed (see logs)")
        print(json.dumps({"provider": "neuralwatt", "ok": False, "error": reason}))
        return 1
    print(json.dumps({
        "provider": "neuralwatt",
        "ok": True,
        "method": "real-api",
        "remaining_usd": balance.remaining_usd,
        "total_credits_usd": balance.total_credits_usd,
        "kwh_used": balance.kwh_used,
        "kwh_remaining": balance.kwh_remaining,
        "kwh_included": balance.kwh_included,
        "usage_fraction": balance.usage_fraction,
        "used_pct": balance.used_pct,
        "cost_usd_lifetime": balance.cost_usd,
        "subscription_status": balance.subscription_status,
        "period_end": balance.period_end,
        "is_exhausted": balance.is_exhausted,
        "daily_spent_usd": balance.daily_spent_usd,
        "daily_cap_usd": balance.daily_cap_usd,
        "is_daily_cap_exceeded": balance.is_daily_cap_exceeded,
        "collected_at": balance.collected_at,
        "remaining": balance.kwh_remaining,
        "total": balance.kwh_included,
        "balances_db_path": balances_db,
    }, default=str))
    return 0


def main(argv: Optional[list] = None) -> int:
    """Unified cron entrypoint.

    Usage: python3 -m src.balance_collectors --provider <ppq|openrouter|deepinfra|telnyx> [--db PATH]

    Dispatches to the per-provider collector, prints one JSON status line,
    exits 0 on success / 1 on failure.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    provider = None
    if "--provider" in argv:
        idx = argv.index("--provider")
        if idx + 1 < len(argv):
            provider = argv[idx + 1]
            argv = argv[:idx] + argv[idx + 2:]

    if provider == "ppq":
        return _ppq_main(argv)
    elif provider == "openrouter":
        return _openrouter_main(argv)
    elif provider == "telnyx":
        return _telnyx_main(argv)
    elif provider == "routstr":
        return _routstr_main(argv)
    elif provider == "neuralwatt":
        return _neuralwatt_main(argv)
    else:
        print(json.dumps({
            "ok": False,
            "error": f"unknown or missing --provider (got {provider!r}); "
                     f"use --provider ppq|openrouter|deepinfra|telnyx|routstr|neuralwatt",
        }))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())