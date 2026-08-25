"""pricing_exposure.py — CG-2 price exposure (plan v2.1 §0.5 / §2.1).

Builds the ``GET /v1/pricing`` payload that exposes the subscription-amortized
baselines (entitlement + realized), per-window usage fractions
(``u_5h`` / ``u_week`` / ``u_month``), the pressure multiplier, the peak-hour
step, and the effective price *now* plus a Kalman-forecast variant at +5/+15/
+60 min (and an extra ``?horizon_min=`` for task-duration pricing).

v2.1 invariant: z.ai is NOT free. The friend key costs $80/mo, so the baseline
is ``monthly_fee ÷ entitlement_tokens``. A fee of zero is a free-tier config
artifact (the pre-v2.1 state) and is flagged as an error — it never silently
yields the ``$0.001`` floor again. The realised (usage-amortised) baseline is
reported for observability but never used as the gate metric.

The pressure curve is *not* re-implemented here — it delegates to
:mod:`src.pricing_engine` (the single source of truth for RP-EXP) so the gate
and the comparator cannot drift.

Pure by construction: every row builder takes a plain snapshot dict; the only
I/O helpers (``load_zai_fees``, ``trailing_usage_tokens``,
``insert_price_observation``, ``latest_observation_ts``) are thin DB/YAML
readers used by the cron collector and the proxy endpoint wiring.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from typing import Any, Iterable, Mapping, Sequence

from src.pricing_engine import (
    PEAK_MULTIPLIER,
    ZAI_QUOTA_PRESSURE_ASYMPTOTE,
    ZAI_QUOTA_PRESSURE_ONSET,
    peak_multiplier,
    quota_pressure_factor,
)

# ── §0.5 reference constants ─────────────────────────────────────────────────

#: Friend-key monthly entitlement estimate (tokens/mo). The §0.5 figure
#: (~18.45B) is provisional until the ours-key entitlement is measured with
#: confidence; until then the denominator falls back to trailing-30d usage.
DEFAULT_ENTITLEMENT_TOKENS_MO: float = 18.45e9

#: Realized-baseline minimum sample (tokens) — below this the realised rate
#: is too noisy to report (a fresh month with a handful of calls yields
#: absurd $/M). Mirrors RealtimePricing.MIN_SAMPLE_TOKENS.
MIN_SAMPLE_TOKENS: int = 1_000_000

#: Forecast horizons always present in the payload (minutes).
FORECAST_HORIZONS_MIN: tuple[int, ...] = (5, 15, 60)

#: Staleness threshold (seconds) — parity with cost_gate.PRICE_STALE_MAX_MIN.
STALENESS_THRESHOLD_S: float = 15.0 * 60.0

#: Error message emitted on a row when ``monthly_fee_usd <= 0`` (free-tier
#: artifact). The effective price is ``None``, never the ``$0.001`` floor.
FEE_UNCONFIGURED_ERROR: str = (
    "monthly_fee_usd <= 0 in providers.yaml — free-tier artifact; "
    "fix config (CG-2)"
)

# z.ai quota window sizes, keyed by ``window_hours`` from the proxy /quota
# shape ``{name, used_pct, resets_at, window_hours}``.
_ZAI_WINDOW_HOURS = {"u_5h": 5, "u_week": 168, "u_month": 720}

# Projection-row window aliases → canonical slot. burn_predictor rows use a
# ``window`` field whose value varies (``"5-hour"``, ``"5h"``, ``"7d"`` …);
# normalize before lookup.
_PROJ_ALIASES = {
    "u_5h": ("5h", "5-hour", "5 hour", "session", "5"),
    "u_week": ("7d", "weekly", "week", "7-day", "7"),
    "u_month": ("30d", "monthly", "month", "30-day", "30"),
}


# ── §0.5 baselines ──────────────────────────────────────────────────────────


def entitlement_baseline_usd_per_m(
    monthly_fee_usd: float,
    entitlement_tokens_mo: float,
) -> float | None:
    """Entitlement baseline: ``fee ÷ (entitlement_tokens / 1e6)``.

    Returns ``None`` (never the ``$0.001`` floor) when the fee is unconfigured
    or the entitlement is non-positive. The gate metric per v2.1.
    """
    if not monthly_fee_usd or monthly_fee_usd <= 0:
        return None
    if not entitlement_tokens_mo or entitlement_tokens_mo <= 0:
        return None
    return float(monthly_fee_usd) / (float(entitlement_tokens_mo) / 1e6)


def realized_baseline_usd_per_m(
    monthly_fee_usd: float,
    trailing_30d_tokens: float,
    min_sample_tokens: int = MIN_SAMPLE_TOKENS,
) -> float | None:
    """Realised (usage-amortised) baseline: ``fee ÷ (trailing_30d / 1e6)``.

    Reported for observability only — NOT gated on. Returns ``None`` when the
    fee is unconfigured or the sample is too small (no floor).
    """
    if not monthly_fee_usd or monthly_fee_usd <= 0:
        return None
    if not trailing_30d_tokens or trailing_30d_tokens < min_sample_tokens:
        return None
    return float(monthly_fee_usd) / (float(trailing_30d_tokens) / 1e6)


def entitlement_denominator(
    capacity_estimate_tokens: float | None,
    trailing_30d_tokens: float | None,
) -> tuple[float, str]:
    """§0.5 denominator rule: ``max(smoothed capacity estimate, trailing-30d
    usage)``.

    Returns ``(tokens, source)`` where source is one of:

    - ``"capacity_estimate"`` — the Kalman-smoothed estimate won (confident).
    - ``"trailing_30d_usage"`` — no capacity estimate, or trailing usage is
      larger (capacity estimate unconfident / cold).
    - ``"default_estimate"`` — neither input is usable; fall back to the §0.5
      reference figure so the baseline stays sane (``$0.0043/M`` for friend).
    """
    cap = float(capacity_estimate_tokens) if capacity_estimate_tokens and capacity_estimate_tokens > 0 else None
    trail = float(trailing_30d_tokens) if trailing_30d_tokens and trailing_30d_tokens > 0 else None
    if cap is None and trail is None:
        return DEFAULT_ENTITLEMENT_TOKENS_MO, "default_estimate"
    if cap is None:
        return trail, "trailing_30d_usage"  # type: ignore[return-value]
    if trail is None or cap >= trail:
        return cap, "capacity_estimate"
    return trail, "trailing_30d_usage"


def entitlement_utilization_pct(
    trailing_30d_tokens: float | None,
    entitlement_tokens_mo: float,
) -> float | None:
    """Trailing-30d usage as a percentage of the monthly entitlement.

    ``None`` (no usage data) → ``None``; zero usage → ``0.0``.
    """
    if trailing_30d_tokens is None:
        return None
    if not entitlement_tokens_mo or entitlement_tokens_mo <= 0:
        return None
    return float(trailing_30d_tokens) / float(entitlement_tokens_mo) * 100.0


# ── window mapping + pressure ──────────────────────────────────────────────


def usage_fractions(windows: Sequence[Mapping[str, Any]]) -> dict[str, float | None]:
    """Map proxy /quota window dicts to ``{u_5h, u_week, u_month}`` fractions.

    Robust to the window-name variance (``"5-hour"``, ``"7d"``, ``"monthly"``)
    by keying off ``window_hours`` (5 / 168 / 720). Windows below the onset or
    unknown (``window_hours == 0``) map to ``None`` (no contribution).
    """
    out: dict[str, float | None] = {"u_5h": None, "u_week": None, "u_month": None}
    for w in windows or ():
        wh = int(w.get("window_hours") or 0)
        pct = float(w.get("used_pct") or 0) / 100.0
        if wh == _ZAI_WINDOW_HOURS["u_5h"]:
            out["u_5h"] = pct
        elif wh == _ZAI_WINDOW_HOURS["u_week"]:
            out["u_week"] = pct
        elif wh == _ZAI_WINDOW_HOURS["u_month"]:
            out["u_month"] = pct
    return out


def zai_pressure_mult(
    u_5h: float | None,
    u_week: float | None,
    u_month: float | None,
) -> float:
    """Pressure multiplier for z.ai — delegates to pricing_engine's RP-EXP curve
    with the z.ai onset/asymptote and ``hard_limit=True``."""
    if u_5h is None and u_week is None and u_month is None:
        return 1.0  # no window data → no penalty (cold start handled upstream)
    return quota_pressure_factor(
        u_5h if u_5h is not None else 0.0,
        u_week,
        u_month,
        onset=ZAI_QUOTA_PRESSURE_ONSET,
        asymptote=ZAI_QUOTA_PRESSURE_ASYMPTOTE,
        hard_limit=True,
    )


def projected_usage_fraction(
    u_now: float,
    projected_total_pct: float | None,
    hours_left: float | None,
    horizon_min: float,
) -> float:
    """Linear interpolation of the usage fraction to ``horizon_min`` minutes
    from now, toward the burn_predictor ``projected_total_pct`` at window end.

    - ``projected_total_pct is None`` → no projection: hold ``u_now``.
    - horizon beyond window end → clamps to the projected total.
    - any value ≥ 1.0 → clamps to 1.0 (hard-limit marker).
    """
    target = (float(projected_total_pct) / 100.0) if projected_total_pct is not None else u_now
    if projected_total_pct is None or not hours_left or hours_left <= 0:
        return max(0.0, min(1.0, target))
    frac = max(0.0, min(1.0, (horizon_min / 60.0) / float(hours_left)))
    u_h = u_now + (target - u_now) * frac
    return max(0.0, min(1.0, u_h))


def is_stale(
    last_obs_ts: float | None,
    now_ts: float,
    threshold_s: float = STALENESS_THRESHOLD_S,
) -> bool:
    """True when the newest observation is older than the threshold (or
    absent). Parity with cost_gate.PRICE_STALE_MAX_MIN (15 min)."""
    if last_obs_ts is None:
        return True
    return (now_ts - float(last_obs_ts)) > threshold_s


def kalman_convergence_green(verdict: str | None) -> bool:
    """Strict green check for the forecast gate (same condition as CG-3).

    Only ``"healthy"`` qualifies — ``"improving"`` is not yet green
    (forecasting prices off an improving-but-not-converged filter is risky).
    """
    return bool(verdict) and str(verdict).strip().lower() == "healthy"


# ── row builders ───────────────────────────────────────────────────────────


def _peak_block(provider: str, hour_utc: int | None) -> dict[str, Any]:
    mult = peak_multiplier(provider, hour_utc)
    return {"active": mult > 1.0, "mult": float(mult)}


def _proj_lookup(
    projections: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Mapping[str, Any]]:
    """Normalize burn_predictor projection rows into ``{u_5h, u_week, u_month}``
    slots, keyed by the canonical alias set."""
    out: dict[str, Mapping[str, Any]] = {}
    if not projections:
        return out
    for row in projections:
        name = str(row.get("window") or row.get("name") or "").strip().lower()
        wh = row.get("window_hours")
        slot = None
        for key, aliases in _PROJ_ALIASES.items():
            if name in aliases:
                slot = key
                break
        if slot is None and wh is not None:
            wh_i = int(wh)
            for key, hours in _ZAI_WINDOW_HOURS.items():
                if wh_i == hours:
                    slot = key
                    break
        if slot is not None:
            out[slot] = row
    return out


def build_zai_pricing_row(
    *,
    provider: str,
    monthly_fee_usd: float,
    entitlement_tokens_mo: float,
    capacity_estimate_tokens: float | None,
    trailing_30d_tokens: float | None,
    windows: Sequence[Mapping[str, Any]],
    projections: Sequence[Mapping[str, Any]] | None = None,
    last_obs_ts: float | None = None,
    now_ts: float = 0.0,
    hour_utc: int | None = None,
    kalman_verdict: str | None = None,
    extra_horizons_min: Iterable[int] = (),
) -> dict[str, Any]:
    """Build a v2.1 z.ai pricing row.

    The composition is:

        effective_price = entitlement_baseline × pressure(u) × peak_mult

    with the forecast variant substituting ``projected_usage_fraction``-driven
    pressure at each horizon (gated on kalman-convergence green).
    """
    now_ts = float(now_ts or time.time())
    fr = usage_fractions(windows)
    u5, uw, um = fr["u_5h"], fr["u_week"], fr["u_month"]
    pressure = zai_pressure_mult(u5, uw, um)
    exhausted = any(v is not None and v >= 1.0 for v in (u5, uw, um))
    peak = _peak_block(provider, hour_utc)
    denom_tokens, denom_source = entitlement_denominator(
        capacity_estimate_tokens, trailing_30d_tokens
    )
    baseline_ent = entitlement_baseline_usd_per_m(monthly_fee_usd, denom_tokens)
    baseline_real = realized_baseline_usd_per_m(monthly_fee_usd, trailing_30d_tokens or 0.0)
    util_pct = entitlement_utilization_pct(trailing_30d_tokens, denom_tokens)
    stale = is_stale(last_obs_ts, now_ts)

    fee_unconfigured = not monthly_fee_usd or monthly_fee_usd <= 0
    eff_now: float | None = None
    if not fee_unconfigured and baseline_ent is not None and not exhausted:
        eff_now = baseline_ent * pressure * peak["mult"]

    row: dict[str, Any] = {
        "provider": provider,
        "kind": "subscription",
        "baseline_entitlement_usd_per_m": baseline_ent,
        "baseline_realized_usd_per_m": baseline_real,
        "entitlement_utilization_pct": util_pct,
        "denominator": {"tokens": denom_tokens, "source": denom_source},
        "windows": {
            "u_5h": u5,
            "u_week": uw,
            "u_month": um,
            "estimated_capacity_tokens": (
                float(capacity_estimate_tokens)
                if capacity_estimate_tokens and capacity_estimate_tokens > 0
                else None
            ),
            "confidence": "high" if capacity_estimate_tokens else "low",
        },
        "pressure_mult": pressure,
        "peak": peak,
        "effective_price_usd_per_m": eff_now,
        "exhausted": exhausted,
        "staleness": {
            "stale": stale,
            "age_s": (now_ts - last_obs_ts) if last_obs_ts is not None else None,
        },
        "error": FEE_UNCONFIGURED_ERROR if fee_unconfigured else None,
        "forecast": None,
    }

    # ── forecast (+5/+15/+60 min + extra ?horizon_min=) ────────────────────
    if fee_unconfigured or baseline_ent is None:
        row["forecast"] = None
        return row

    green = kalman_convergence_green(kalman_verdict)
    proj = _proj_lookup(projections)
    horizons = sorted(set(FORECAST_HORIZONS_MIN) | set(int(h) for h in extra_horizons_min))
    at_horizon: list[dict[str, Any]] = []
    for h in horizons:
        if not green or not proj:
            # Fallback: current price + stale flag (§2.1 forecast gating).
            at_horizon.append({
                "horizon_min": h,
                "u_5h": u5, "u_week": uw, "u_month": um,
                "pressure_mult": pressure,
                "peak_mult": peak["mult"],
                "effective_price_usd_per_m": eff_now,
                "stale": True,
                "exhausted": exhausted,
            })
            continue

        def _proj_u(slot: str, u_now: float | None) -> float:
            r = proj.get(slot)
            if r is None or u_now is None:
                return u_now if u_now is not None else 0.0
            return projected_usage_fraction(
                u_now,
                float(r.get("projected_total_pct")) if r.get("projected_total_pct") is not None else None,
                float(r.get("exhausts_in_hours")) if r.get("exhausts_in_hours") is not None else None,
                h,
            )

        ph5 = _proj_u("u_5h", u5)
        phw = _proj_u("u_week", uw)
        phm = _proj_u("u_month", um)
        p_pressure = zai_pressure_mult(ph5, phw, phm)
        p_exhausted = any(v >= 1.0 for v in (ph5, phw, phm))
        # Peak at the horizon end-hour (conservative across the window).
        end_hour = None if hour_utc is None else (int(hour_utc) + int(h // 60) + (1 if h % 60 else 0)) % 24
        p_peak_mult = peak_multiplier(provider, end_hour)
        p_price: float | None = None
        if not p_exhausted:
            p_price = baseline_ent * p_pressure * p_peak_mult
        at_horizon.append({
            "horizon_min": h,
            "u_5h": ph5, "u_week": phw, "u_month": phm,
            "pressure_mult": p_pressure,
            "peak_mult": float(p_peak_mult),
            "effective_price_usd_per_m": p_price,
            "stale": False,
            "exhausted": p_exhausted,
        })

    row["forecast"] = {
        "kalman_convergence": "green" if green else (kalman_verdict or "unverified"),
        "horizons_min": horizons,
        "at_horizon": at_horizon,
    }
    return row


def build_flat_row(
    *,
    provider: str,
    catalog_price_usd_per_m: float,
    measured: bool,
    last_obs_ts: float | None = None,
    now_ts: float = 0.0,
    note: str | None = None,
) -> dict[str, Any]:
    """Flat-tier row (ollama_cloud tracker-amortized, routstrd pay-per-use).

    Flat tiers have NO pressure multiplier, NO peak window, and NO forecast —
    the catalog rate is the effective price. Only staleness applies.
    """
    now_ts = float(now_ts or time.time())
    kind = "flat_subscription" if provider == "ollama_cloud" else "pay_per_use"
    return {
        "provider": provider,
        "kind": kind,
        "catalog_price_usd_per_m": float(catalog_price_usd_per_m),
        "measured": bool(measured),
        "effective_price_usd_per_m": float(catalog_price_usd_per_m),
        "staleness": {
            "stale": is_stale(last_obs_ts, now_ts),
            "age_s": (now_ts - last_obs_ts) if last_obs_ts is not None else None,
        },
        "note": note,
    }


def build_pricing_payload(
    *,
    rows: Mapping[str, Mapping[str, Any]],
    kalman_verdict: str | None,
    model: str | None = None,
    horizon_min: int | None = None,
    now_ts: float = 0.0,
) -> dict[str, Any]:
    """``/v1/pricing`` envelope: ``{generated_ts, model, horizon_min,
    kalman_convergence, providers}``."""
    now_ts = float(now_ts or time.time())
    green = kalman_convergence_green(kalman_verdict)
    return {
        "generated_ts": now_ts,
        "model": model,
        "horizon_min": horizon_min,
        "kalman_convergence": {
            "green": green,
            "verdict": kalman_verdict or "unverified",
        },
        "providers": dict(rows),
    }


# ── I/O helpers (cron collector + proxy endpoint) ────────────────────────────


_DEFAULT_PROVIDERS_YAML: str = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config",
    "providers.yaml",
)


def load_zai_fees(providers_yaml: str | None = None) -> dict[str, dict[str, Any]]:
    """Load z.ai key inventory from ``config/providers.yaml``.

    Returns ``{key: {monthly_fee_usd, entitlement_tokens_mo}}``. ``friend``'s
    ``monthly_fee_usd=0`` survives (NOT defaulted to a positive number) so the
    row builder can flag the free-tier artifact. Missing file → ``{}``.
    """
    path = providers_yaml or _DEFAULT_PROVIDERS_YAML
    out: dict[str, dict[str, Any]] = {}
    try:
        import yaml  # type: ignore[import-untyped]

        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        keys = (data.get("zai") or {}).get("keys") or {}
        for name, cfg in keys.items():
            if not isinstance(cfg, dict):
                continue
            fee = cfg.get("monthly_fee_usd")
            if fee is None:
                continue
            out[name] = {
                "monthly_fee_usd": float(fee),
                "entitlement_tokens_mo": float(
                    cfg.get("entitlement_tokens_mo") or DEFAULT_ENTITLEMENT_TOKENS_MO
                ),
            }
    except Exception:
        return out
    return out


def trailing_usage_tokens(db_path: str, key_name: str, days: int = 30) -> int:
    """SUM(total_tokens) for *key_name* over the trailing ``days`` window.

    Reads ``api_calls(ts, key_name, total_tokens)``. Never raises — returns 0
    when the table or DB is absent.
    """
    cutoff = time.time() - days * 86400
    try:
        conn = sqlite3.connect(db_path, timeout=2)
        try:
            (total,) = conn.execute(
                "SELECT COALESCE(SUM(total_tokens), 0) FROM api_calls "
                "WHERE key_name = ? AND ts >= ?",
                (key_name, cutoff),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return 0
    return int(total or 0)


def insert_price_observation(
    db_path: str,
    *,
    provider: str,
    rate_usd_per_m: float,
    source: str,
    is_measured: bool = False,
    confidence: float = 0.0,
    sample_tokens: int | None = None,
    sample_cost_usd: float | None = None,
    note: Mapping[str, Any] | str | None = None,
    ts: float | None = None,
) -> bool:
    """Insert one row into ``price_observations`` (schema-compatible with the
    live zai_usage.db table). Returns False on failure (never raises).
    """
    ts = float(ts if ts is not None else time.time())
    note_str = json.dumps(note) if isinstance(note, dict) else note
    try:
        conn = sqlite3.connect(db_path, timeout=2)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS price_observations ("
                " ts REAL NOT NULL, provider TEXT NOT NULL, model TEXT, "
                " rate_per_m REAL NOT NULL, source TEXT NOT NULL, "
                " is_measured INTEGER DEFAULT 0, confidence REAL, "
                " sample_tokens INTEGER, sample_cost_usd REAL, "
                " velocity REAL, note TEXT)"
            )
            conn.execute(
                "INSERT INTO price_observations "
                "(ts, provider, model, rate_per_m, source, is_measured, "
                " confidence, sample_tokens, sample_cost_usd, note) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ts, provider, None, float(rate_usd_per_m), source,
                    1 if is_measured else 0, float(confidence),
                    sample_tokens, sample_cost_usd, note_str,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        return False
    return True


def latest_observation_ts(db_path: str, provider: str) -> float | None:
    """Newest ``ts`` for *provider* in ``price_observations``, or ``None``."""
    try:
        conn = sqlite3.connect(db_path, timeout=2)
        try:
            (ts,) = conn.execute(
                "SELECT MAX(ts) FROM price_observations WHERE provider = ?",
                (provider,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    return float(ts) if ts is not None else None


def latest_observation_rate(
    db_path: str, provider: str
) -> tuple[float | None, float | None, bool]:
    """Newest ``(rate_usd_per_m, ts, is_measured)`` for *provider*.

    Used by the proxy ``/v1/pricing`` endpoint to surface the latest
    tracker-measured rate for flat providers (e.g. ollama_cloud) whose
    amortized $/M is only observable after the fact. Missing table, missing
    provider, or unreadable DB → ``(None, None, False)``.
    """
    try:
        conn = sqlite3.connect(db_path, timeout=2)
        try:
            row = conn.execute(
                "SELECT rate_per_m, ts, is_measured FROM price_observations "
                "WHERE provider = ? ORDER BY ts DESC LIMIT 1",
                (provider,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return (None, None, False)
    if row is None or row[0] is None:
        return (None, None, False)
    return (float(row[0]), float(row[1]) if row[1] is not None else None,
            bool(row[2]))
