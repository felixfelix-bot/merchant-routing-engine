"""balance_collectors.py — query provider billing APIs for real spend & balance.

T2 of the real-price-tracker plan. Each per-token provider exposes (or doesn't)
a way to read the authoritative remaining balance. This module knows how to ask.

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

Design rules (mirror src/cost_extraction.py):
    * **NEVER raises.** Intended to run inside the proxy's request path and in
      background reconcilers. Any error — no key, network failure, HTTP 4xx/5xx,
      malformed JSON, bogus types — is swallowed and yields a result whose
      ``error`` field names the problem while numeric fields stay ``None``.
    * **Dependency-light.** Stdlib ``urllib`` only; no ``requests``/``httpx``
      added to the project.
    * **Testable.** The entire network surface is the ``_http_get`` function.
      Callers can pass ``http_get=<fake>`` to inject canned ``(status, body)``
      pairs; the tests never touch the network.

Public API
    ``DeepInfraBalance`` — dataclass result.
    ``collect_deepinfra_balance(api_key, starting_balance=5.0, ...) -> DeepInfraBalance``
    ``COST_FIELD_UNIT`` — cents per cost unit (100.0).
"""
from __future__ import annotations

import calendar
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

__all__ = [
    "DeepInfraBalance",
    "collect_deepinfra_balance",
    "COST_FIELD_UNIT",
    "DEEPINFRA_API_BASE",
]

# ── constants ────────────────────────────────────────────────────────────────

DEEPINFRA_API_BASE = "https://api.deepinfra.com"
COST_FIELD_UNIT = 100.0  # the `cost` field in /payment/usage is in cents

# Type of the HTTP seam: (url, headers, timeout) -> (status_code, body_text).
# status_code is None on transport-level failure (no response from the server).
HttpGetFn = Callable[[str, dict[str, str], float], "tuple[int | None, str]"]


# ── result dataclass ─────────────────────────────────────────────────────────


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


# ── helpers ──────────────────────────────────────────────────────────────────


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
    """Best-effort float coercion. None on non-numeric/None/NaN/inf."""
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


# ── response parsers (pure functions; never raise) ──────────────────────────


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


# ── public collector ─────────────────────────────────────────────────────────


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
    headers = {"Authorization": "Bearer " + api_key}

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


# ── tiny local urlencode (avoid urllib.parse name collisions at import time) ─


def _urlencode(params: dict[str, str]) -> str:
    import urllib.parse as _up

    return _up.urlencode(params)
