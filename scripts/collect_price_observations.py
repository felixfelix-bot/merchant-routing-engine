#!/usr/bin/env python3
"""CG-2 price-observation collector (plan v2.1 §2.1, §0.5).

Hourly cron that:

  1. GETs the proxy ``/v1/pricing`` snapshot (``http://127.0.0.1:9099``).
  2. Persists every provider row into ``price_observations`` — derived z.ai
     effective prices (source ``pricing_exposure``, never the $0 floor:
     rows with ``effective_price_usd_per_m=None`` are SKIPPED) and flat
     catalog rates (``catalog:<provider>``, measured flag preserved).
  3. Derives smoothed z.ai capacity estimates
     (trailing-30d tokens ÷ u_month) and writes them, plus the kalman
     convergence verdict from ``kalman_health.build_report()``, into the
     state file the proxy endpoint reads (``.pricing_exposure_state.json``).
  4. Prints a one-line capacity log (hourly cadence) on success; failures
     print a short alert and exit non-zero (silent-when-healthy cron
     convention: the log line is the only steady-state output).

Fixture mode (``--fixture``) seeds N hourly synthetic entitlement-baseline
observations for CG-1 integration (feeds ``evaluate_cost_gate`` history).

Usage:
    collect_price_observations.py [--db PATH] [--state PATH] [--base URL]
                                  [--fixture] [--hours N]

Exit codes: 0 healthy, 1 collection failure.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.request

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from src.pricing_exposure import (  # noqa: E402
    insert_price_observation,
    load_zai_fees,
)

BOT_DIR = os.path.expanduser("~/.hermes/bot")
DEFAULT_DB = os.path.join(BOT_DIR, "zai_usage.db")
DEFAULT_STATE = os.path.join(BOT_DIR, ".pricing_exposure_state.json")
DEFAULT_BASE = "http://127.0.0.1:9099"

# u_month below this is too noisy to imply a monthly capacity.
MIN_MONTH_FRACTION = 0.05

# Fixture-mode synthetic baselines (entitlement $/M, cheap band).
_FIXTURE_PROVIDERS_YAML = os.path.join(_REPO_ROOT, "config", "providers.yaml")


# ── capacity derivation ──────────────────────────────────────────────────────


def derive_capacity_estimate(
    trailing_tokens: float | None, u_month: float | None
) -> float | None:
    """Implied monthly capacity = trailing-30d tokens ÷ u_month (fraction).

    Returns ``None`` when either input is missing or u_month is below
    ``MIN_MONTH_FRACTION`` (early-month noise: a 3% month implies a 33×
    multiplier on measurement error).
    """
    if not trailing_tokens or trailing_tokens <= 0:
        return None
    if u_month is None:
        return None
    u = float(u_month)
    if u < MIN_MONTH_FRACTION:
        return None
    return float(trailing_tokens) / u


# ── snapshot fetch ───────────────────────────────────────────────────────────


def fetch_pricing_snapshot(base: str, timeout: float = 5.0) -> dict:
    """GET ``{base}/v1/pricing`` and return the JSON payload.

    Raises on HTTP/network error or non-dict JSON — caller reports the alert.
    """
    url = base.rstrip("/") + "/v1/pricing"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"non-dict /v1/pricing payload from {url}")
    return payload


# ── persistence ──────────────────────────────────────────────────────────────


def persist_observations(db_path: str, payload: dict) -> int:
    """Insert every usable provider row of a ``/v1/pricing`` payload.

    z.ai rows → source ``pricing_exposure``, ``is_measured=0`` (derived);
    flat rows → source ``catalog:<provider>``, measured flag preserved.
    Rows with a null/non-positive effective price (exhausted keys, fee=0
    cold-start artifacts) are skipped — NEVER persisted as a $0 floor.
    Returns the number of rows inserted.
    """
    n = 0
    for name, row in (payload.get("providers") or {}).items():
        rate = row.get("effective_price_usd_per_m")
        if not isinstance(rate, (int, float)) or not rate > 0:
            continue
        if row.get("kind") == "subscription":
            source = "pricing_exposure"
            measured = False
            windows = row.get("windows") or {}
            note = {
                "kind": row.get("kind"),
                "windows": windows,
                "pressure_mult": row.get("pressure_mult"),
                "peak": row.get("peak"),
            }
            confidence = 0.5 if windows.get("confidence") == "high" else 0.3
        else:
            source = f"catalog:{name}"
            measured = bool(row.get("measured"))
            note = {"kind": row.get("kind")}
            confidence = 0.8 if measured else 0.4
        ok = insert_price_observation(
            db_path,
            provider=name,
            rate_usd_per_m=float(rate),
            source=source,
            is_measured=measured,
            confidence=confidence,
            note=note,
        )
        if ok:
            n += 1
    return n


# ── kalman verdict (from the proxy's kalman_health) ──────────────────────────


def kalman_verdict() -> str:
    """``kalman_health.build_report()['overall_verdict']`` or ``unverified``."""
    try:
        path = os.path.join(BOT_DIR, "kalman_health.py")
        spec = importlib.util.spec_from_file_location("kh_collect", path)
        if spec is None or spec.loader is None:
            return "unverified"
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        report = mod.build_report()
        v = report.get("overall_verdict")
        return v if isinstance(v, str) else "unverified"
    except Exception:
        return "unverified"


def write_state(
    state_path: str, verdict: str, capacity_estimates: dict[str, float]
) -> None:
    os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "generated_ts": time.time(),
                "kalman_verdict": verdict,
                "capacity_estimates": {
                    k: v for k, v in capacity_estimates.items() if v
                },
            },
            fh,
        )
    os.replace(tmp, state_path)


def capacity_estimates_from_payload(payload: dict) -> dict[str, float]:
    """Derive per-key smoothed capacity estimates from a pricing payload."""
    out: dict[str, float] = {}
    for name, row in (payload.get("providers") or {}).items():
        if row.get("kind") != "subscription":
            continue
        windows = row.get("windows") or {}
        denom = (row.get("denominator") or {}).get("tokens")
        # trailing tokens: reconstruct from utilization if the row carries it
        trailing = None
        util_pct = row.get("entitlement_utilization_pct")
        if denom and util_pct:
            trailing = float(denom) * float(util_pct) / 100.0
        est = derive_capacity_estimate(trailing, windows.get("u_month"))
        if est:
            out[name] = est
    return out


# ── fixture mode (CG-1 history seeding) ──────────────────────────────────────


def seed_fixture_history(db_path: str, hours: int = 24) -> int:
    """Seed ``hours`` hourly entitlement-baseline observations per z.ai key."""
    fees = load_zai_fees(_FIXTURE_PROVIDERS_YAML)
    now = time.time()
    n = 0
    for key, cfg in fees.items():
        fee = cfg.get("monthly_fee_usd")
        ent = cfg.get("entitlement_tokens_mo")
        if not fee or not ent:
            continue
        baseline = float(fee) / (float(ent) / 1e6)
        for i in range(hours):
            ok = insert_price_observation(
                db_path,
                provider=key,
                rate_usd_per_m=baseline + (0.0001 if i % 2 else 0),
                source="cg2_entitlement",
                is_measured=False,
                confidence=0.6,
                ts=now - (hours - i) * 3600,
            )
            if ok:
                n += 1
    return n


# ── main ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="CG-2 price-observation collector")
    ap.add_argument("--db", default=DEFAULT_DB, help="usage DB (price_observations)")
    ap.add_argument("--state", default=DEFAULT_STATE, help="collector state JSON")
    ap.add_argument("--base", default=DEFAULT_BASE, help="proxy base URL")
    ap.add_argument("--fixture", action="store_true",
                    help="seed synthetic hourly history (CG-1) and exit")
    ap.add_argument("--hours", type=int, default=24,
                    help="fixture hours (default 24)")
    args = ap.parse_args(argv)

    if args.fixture:
        n = seed_fixture_history(args.db, max(1, args.hours))
        if n == 0:
            print("collector: fixture seeding FAILED (no fees configured)",
                  file=sys.stderr)
            return 1
        print(f"collector: fixture seeded {n} observations "
              f"({args.hours}h) into {args.db}")
        return 0

    try:
        payload = fetch_pricing_snapshot(args.base)
    except Exception as exc:
        print(f"collector ALERT: /v1/pricing fetch failed from {args.base}: "
              f"{exc}", file=sys.stderr)
        return 1

    n = persist_observations(args.db, payload)
    if n == 0:
        print("collector ALERT: no provider rows persisted from /v1/pricing",
              file=sys.stderr)
        return 1

    caps = capacity_estimates_from_payload(payload)
    verdict = kalman_verdict()
    write_state(args.state, verdict, caps)

    cap_str = ", ".join(
        f"{k}={v / 1e9:.2f}B" for k, v in sorted(caps.items())
    ) or "none"
    print(f"collector: {n} observations persisted; capacity estimates: "
          f"{cap_str}; kalman={verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
