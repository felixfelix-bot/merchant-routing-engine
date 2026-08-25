"""promo_tier.py — pure guard module for promo (free-window) provider tiers.

OX-1 of docs/PLAN-oxalpha-promo-2026-08-21.md (Felix-approved 2026-08-21,
decisions D1–D7). Guards the `oxalpha` tier (stealth/ox-alpha free promo on
OpenRouter) against the routstrd-$18.81 class of failure: a promo price that
silently turns paid while config still believes the stale cheap rate.

Design (plan §2 / §3):
  - §2.3 expiry flip — `expires_at` (default 2026-08-28T00:00Z, D3) is a HARD
    deadline. Before: tier active at $0 marginal cash (effective price sits at
    the ADR-004 floor, never $0.00). At/after: auto-disable AND effective
    price flips to the pessimistic post-promo estimate ($10/$30 per M —
    deliberately above the CG-6 $0.10/M ceiling) so the tier is priced OUT of
    every path even if a bug leaves it enabled.
  - §2.4 spend guard — ANY observed nonzero charge (`usage.cost > 0`, or a
    negative wallet delta) → immediate disable + ONE anomaly_events-shaped
    row. The kill fires exactly once; there is no in-process re-enable.
  - §2.4 402 path — a $0 model demanding credits means the promo terms
    changed: disable for the promo remainder (+ warning anomaly row).
  - §2.5 allowlist — only vision / bulk_summarize / shadow_eval may reach the
    tier (own-UI screenshots, public docs, eval fixtures — §2.5 policy).
  - §3.1/§3.2 promo tag — oxalpha price rows carry source='promo' at the
    $0.001 floor, and promo-tagged rows are excluded from the cost-gate p20
    percentile history (by source column when CG-2 ships one, else by the
    provider-name registry below). Promo rows stay stored for audit but never
    shape the band — during or after the promo.
  - ADR-004 — effective price is never below the floor that price_observations
    uses elsewhere (strategy.min_effective_price, default $0.001/M).
  - §2.6 rate-limit backoff policy — 60/120/300 s sequence on 429, circuit
    breaker 5 consecutive failures / 300 s cooldown. Pure constants/helpers;
    this module never sleeps.

PURITY CONTRACT: no DB, no network, no filesystem, no clock reads at import.
All now-times are injected; anomaly events are RETURNED as dicts shaped for
the shared anomaly_events table (ts/severity/category/title/detail-JSON) and
collected on the guard — the caller (OX-2 proxy wiring) performs inserts:

    ev = guard.observe_charge(usage.get("cost"))
    if ev:
        conn.execute("INSERT INTO anomaly_events (ts, severity, category, "
                     "title, detail) VALUES (?,?,?,?,?)",
                     (ev["ts"], ev["severity"], ev["category"],
                      ev["title"], ev["detail"]))

The module is inert without config (plan §6): delete the `oxalpha:` block and
this code never runs.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

# ── Tier identity (plan §0.1/§2.1) ──────────────────────────────────────────

PROVIDER_NAME = "oxalpha"
MODEL_NAME = "stealth/ox-alpha"

# Provider-name registry for the p20 filter (plan §3.1/§3.2): CG-2 has not
# shipped a `source` column, so the promo tag is this code-side set; once a
# source column exists rows also carry source='promo' and BOTH styles filter.
PROMO_PROVIDERS = frozenset({PROVIDER_NAME})
PROMO_SOURCE_TAG = "promo"

# ── Defaults (mirror the providers.yaml `oxalpha:` fixture, plan §2.1) ──────

PROMO_END_DEFAULT = "2026-08-28T00:00:00Z"  # D3: conservative hard deadline
POST_PROMO_PESSIMISTIC_PER_M_DEFAULT = {"input": 10.0, "output": 30.0}
VERIFIED_RATE_PER_M_DEFAULT = {"input": 0.0, "output": 0.0}  # §0.3 probe
BUDGET_USD_DEFAULT = 0.0

# ADR-004: effective price is always positive. $0.001/M floor, identical to
# strategy.min_effective_price in config/providers.yaml.
MIN_EFFECTIVE_PRICE_DEFAULT = 0.001

# §2.5 data-sensitivity allowlist (Felix D2, as written).
ALLOWED_TASK_TYPES_DEFAULT = frozenset({"vision", "bulk_summarize", "shadow_eval"})

# §2.6 rate-limit backoff: limits are unannounced (§0.1) — start at 60 s and
# escalate; the circuit breaker reuses the strategy defaults (5 / 300 s).
RATE_LIMIT_BACKOFF_SEQUENCE_S = (60.0, 120.0, 300.0)
CIRCUIT_BREAKER_THRESHOLD = 5
CIRCUIT_BREAKER_COOLDOWN_S = 300.0

# Disable reasons (embedded in anomaly detail + status()).
REASON_PROMO_EXPIRED = "promo_expired"
REASON_NONZERO_CHARGE = "nonzero_charge"
REASON_HTTP_402 = "http_402_unfunded"


# ── Time helpers ────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_promo_end(value) -> datetime:
    """Parse `promo.expires_at` into a UTC datetime.

    Accepts an ISO-8601 string (with or without a trailing ``Z``) or a
    datetime (naive datetimes are assumed UTC). Raises ValueError on anything
    else — a bad deadline must be loud, never guessed.
    """
    if isinstance(value, datetime):
        return _coerce_utc(value)
    if isinstance(value, str):
        s = value.strip()
        if s.endswith(("Z", "z")):
            s = s[:-1] + "+00:00"
        try:
            return _coerce_utc(datetime.fromisoformat(s))
        except ValueError as exc:
            raise ValueError(f"invalid promo expires_at: {value!r}") from exc
    raise ValueError(f"invalid promo expires_at: {value!r}")


# ── §2.6 pure backoff helper ────────────────────────────────────────────────

def rate_limit_backoff_s(consecutive_429s: int) -> float:
    """Backoff seconds after N consecutive 429s: 60 → 120 → 300 (cap).

    Never sleeps — the caller (OX-2) owns scheduling. 0/negative inputs mean
    "no 429 observed" and return 0.
    """
    n = int(consecutive_429s)
    if n <= 0:
        return 0.0
    return float(RATE_LIMIT_BACKOFF_SEQUENCE_S[min(n - 1, len(RATE_LIMIT_BACKOFF_SEQUENCE_S) - 1)])


# ── §3.2 p20 filter helpers (module-level: usable without a guard) ─────────

def is_promo_row(row) -> bool:
    """True if a price_observations row is promo-sourced.

    Matches BOTH tagging styles (plan §3.1): an explicit ``source='promo'``
    column (once CG-2 ships it) OR membership in the provider-name registry
    (covers untagged/legacy oxalpha rows).
    """
    try:
        if row.get("source") == PROMO_SOURCE_TAG:
            return True
        return row.get("provider") in PROMO_PROVIDERS
    except AttributeError:
        return False


def filter_promo_rows(rows) -> list:
    """Exclude promo rows from a price_observations window (the p20 input).

    Promo rows remain stored upstream for audit — this filter only shapes the
    percentile band, before/during/after the promo (plan §3.2). Non-promo
    rows are returned in original order, untouched.
    """
    return [r for r in rows if not is_promo_row(r)]


def promo_exclusion_sql(source_col: str | None = "source",
                        provider_col: str = "provider",
                        providers: frozenset | set = PROMO_PROVIDERS) -> str:
    """SQL WHERE fragment excluding promo rows, for CG-2's p20 query.

    Pass ``source_col=None`` when the table has no source column yet —
    the registry-based provider clause still applies. Composable via AND.
    """
    names = ", ".join(f"'{p}'" for p in sorted(providers))
    clauses = []
    if source_col:
        clauses.append(f"({source_col} IS NULL OR {source_col} != '{PROMO_SOURCE_TAG}')")
    clauses.append(f"{provider_col} NOT IN ({names})")
    return " AND ".join(clauses)


# ── The guard ───────────────────────────────────────────────────────────────

@dataclass
class PromoTierGuard:
    """Stateful-but-pure guard for one promo tier instance.

    Lifecycle: construct (or from_config) → evaluate status()/pricing at
    request time → feed observed charges / wallet deltas / HTTP statuses in.
    Once ANY disable fires, the guard is dead for the process lifetime —
    there is intentionally NO re-enable method (plan §2.4: human-only).
    """

    promo_end: datetime = field(default_factory=lambda: parse_promo_end(PROMO_END_DEFAULT))
    post_promo_per_m: dict = field(
        default_factory=lambda: dict(POST_PROMO_PESSIMISTIC_PER_M_DEFAULT))
    min_effective_price: float = MIN_EFFECTIVE_PRICE_DEFAULT
    budget_usd: float = BUDGET_USD_DEFAULT
    allowed_task_types: frozenset = ALLOWED_TASK_TYPES_DEFAULT
    verified_rate_per_m: dict = field(
        default_factory=lambda: dict(VERIFIED_RATE_PER_M_DEFAULT))

    # ── runtime state (not config) ──
    disabled_reason: str | None = None
    anomaly_events: list = field(default_factory=list)  # audit trail; caller inserts
    _nonzero_kill_fired: bool = False
    _402_fired: bool = False

    def __post_init__(self) -> None:
        # ADR-004 invariant 2: the floor is the global minimum; zero floor
        # would resurrect $0.00 pricing. Fail loud at construction.
        if not self.min_effective_price > 0:
            raise ValueError(
                f"ADR-004 violation: min_effective_price must be > 0, "
                f"got {self.min_effective_price!r}")
        self.promo_end = _coerce_utc(self.promo_end)
        self.post_promo_per_m = {
            "input": float(self.post_promo_per_m.get("input", 0.0)),
            "output": float(self.post_promo_per_m.get("output", 0.0)),
        }

    # ── construction from providers.yaml ──

    @classmethod
    def from_config(cls, cfg: dict | None,
                    strategy_cfg: dict | None = None) -> "PromoTierGuard":
        """Build from the `oxalpha:` block of providers.yaml (+ strategy)."""
        cfg = cfg or {}
        promo = cfg.get("promo") or {}
        return cls(
            promo_end=parse_promo_end(promo.get("expires_at", PROMO_END_DEFAULT)),
            post_promo_per_m=dict(promo.get("post_promo_pessimistic_per_m")
                                  or POST_PROMO_PESSIMISTIC_PER_M_DEFAULT),
            min_effective_price=float((strategy_cfg or {}).get(
                "min_effective_price", MIN_EFFECTIVE_PRICE_DEFAULT)),
            budget_usd=float(cfg.get("budget_usd", BUDGET_USD_DEFAULT)),
            allowed_task_types=frozenset(cfg.get("allowlist_task_types")
                                         or ALLOWED_TASK_TYPES_DEFAULT),
            verified_rate_per_m=dict(promo.get("verified_rate")
                                     or VERIFIED_RATE_PER_M_DEFAULT),
        )

    # ── §2.3 expiry + pricing ──

    def _in_promo(self, now: datetime) -> bool:
        return now < self.promo_end

    def check_expiry(self, now: datetime | None = None) -> bool:
        """Flip state at the hard deadline. Returns enabled-after-check."""
        now = _coerce_utc(now) if now is not None else _utcnow()
        if self.disabled_reason is None and now >= self.promo_end:
            self.disabled_reason = REASON_PROMO_EXPIRED  # expected: no anomaly
        return self.disabled_reason is None

    def effective_price_per_m(self, now: datetime | None = None) -> dict:
        """Effective {input, output} $/M — NEVER below the ADR-004 floor.

        While enabled and in-promo: the floor ($0.001/M) — $0 marginal cash,
        but never a $0.00 row (ADR-004 invariant 1). Any other state (post
        deadline, or killed early by §2.4): the pessimistic post-promo
        estimate, clamped to the floor.
        """
        now = _coerce_utc(now) if now is not None else _utcnow()
        self.check_expiry(now)
        if self.disabled_reason is None and self._in_promo(now):
            pair = {"input": self.min_effective_price,
                    "output": self.min_effective_price}
        else:
            floor = self.min_effective_price
            pair = {"input": max(self.post_promo_per_m["input"], floor),
                    "output": max(self.post_promo_per_m["output"], floor)}
        return pair

    def effective_rate_per_m(self, now: datetime | None = None) -> float:
        """Conservative single-scalar rate for single-column contexts:
        max(input, output) of the effective pair (priced OUT stays priced out).
        """
        pair = self.effective_price_per_m(now)
        return max(pair["input"], pair["output"])

    def status(self, now: datetime | None = None) -> dict:
        """Full tier status; performs the expiry check (request-time path)."""
        now = _coerce_utc(now) if now is not None else _utcnow()
        self.check_expiry(now)
        return {
            "provider": PROVIDER_NAME,
            "model": MODEL_NAME,
            "enabled": self.disabled_reason is None,
            "in_promo": self._in_promo(now),
            "disable_reason": self.disabled_reason,
            "effective_price_per_m": self.effective_price_per_m(now),
            "budget_usd": self.budget_usd,
            "task_types_allowed": sorted(self.allowed_task_types),
        }

    # ── §3.1 price_observations row ──

    def price_observation_row(self, now: datetime | None = None) -> dict:
        """Promo-tagged price row for CG-2 (never $0.00, always source=promo)."""
        now = _coerce_utc(now) if now is not None else _utcnow()
        return {
            "provider": PROVIDER_NAME,
            "model": MODEL_NAME,
            "rate_per_m": self.effective_rate_per_m(now),
            "is_measured": False,  # catalog/promo rate, not a market measurement
            "source": PROMO_SOURCE_TAG,
            "ts": now.timestamp(),
        }

    # ── §2.4 kill paths ──

    def _emit_event(self, now: datetime, severity: str, category: str,
                    title: str, payload: dict) -> dict:
        event = {
            "ts": now.timestamp(),
            "severity": severity,
            "category": category,
            "title": title,
            # detail mirrors zai_proxy._log_anomaly: JSON object in a TEXT col
            "detail": json.dumps(payload),
        }
        self.anomaly_events.append(event)
        return event

    def _kill_nonzero(self, detector: str, value: float,
                      now: datetime) -> dict | None:
        """Shared §2.4 kill: disable + ONE anomaly row, whichever detector
        fires first (usage.cost or wallet delta). Fires exactly once."""
        if self._nonzero_kill_fired:
            return None
        self._nonzero_kill_fired = True
        self.disabled_reason = REASON_NONZERO_CHARGE
        return self._emit_event(
            now,
            severity="critical",
            category="promo_spend",
            title=f"{PROVIDER_NAME} promo tier charged — auto-disabled",
            payload={
                "detail": (
                    f"budget is ${self.budget_usd:.2f}; observed nonzero spend "
                    f"on a $0-promo tier (anti-routstrd guard). Tier disabled; "
                    f"re-enable requires human action (config + restart)."),
                "provider": PROVIDER_NAME,
                "reason": REASON_NONZERO_CHARGE,
                "source": detector,  # which detector fired: usage.cost | wallet_delta
                "cost_usd": float(value),  # raw observed value (signed for deltas)
                "budget_usd": self.budget_usd,
                "in_promo": self._in_promo(now),
            },
        )

    def observe_charge(self, cost_usd, now: datetime | None = None) -> dict | None:
        """Feed a response's `usage.cost` through the spend guard.

        cost <= 0 / None (the verified promo behavior, §0.3) → no-op.
        Any nonzero charge → immediate disable + one anomaly row.
        """
        now = _coerce_utc(now) if now is not None else _utcnow()
        if cost_usd is None or not float(cost_usd) > 0:
            return None
        return self._kill_nonzero("usage.cost", float(cost_usd), now)

    def observe_wallet_delta(self, delta_usd,
                             now: datetime | None = None) -> dict | None:
        """Feed the 5-min balance-collector delta through the same kill:
        a negative OpenRouter wallet delta while oxalpha is the only enabled
        consumer is the same bleed, detected independently (plan §2.4).
        """
        now = _coerce_utc(now) if now is not None else _utcnow()
        if delta_usd is None or not float(delta_usd) < 0:
            return None
        return self._kill_nonzero("wallet_delta", float(delta_usd), now)

    def observe_http_status(self, status_code: int,
                            now: datetime | None = None) -> dict | None:
        """Feed upstream HTTP status. 402 → disable for the promo remainder
        (a $0 model demanding credits = promo terms changed). One row."""
        now = _coerce_utc(now) if now is not None else _utcnow()
        if int(status_code) != 402:
            return None
        if self._402_fired:
            self.disabled_reason = REASON_HTTP_402  # stay dead, no dup row
            return None
        self._402_fired = True
        self.disabled_reason = REASON_HTTP_402
        return self._emit_event(
            now,
            severity="warning",
            category="promo_tier",
            title=f"{PROVIDER_NAME}: HTTP 402 on $0-promo tier — disabled for promo remainder",
            payload={
                "detail": (
                    "A $0 model demanding credits means the promo terms "
                    "changed; tier disabled until human review."),
                "provider": PROVIDER_NAME,
                "reason": REASON_HTTP_402,
                "status_code": 402,
                "in_promo": self._in_promo(now),
            },
        )

    # ── §2.5 allowlist ──

    def task_type_allowed(self, task_type) -> bool:
        """Exact-match allowlist check (vision/bulk_summarize/shadow_eval).
        Everything else — None, unknown, case variants, non-strings — is
        rejected; never raises."""
        return (isinstance(task_type, str)
                and task_type in self.allowed_task_types)
