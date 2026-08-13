#!/usr/bin/env python3
"""export_kalman_pricing.py — Export Kalman effective prices for Routstr integration.

Bridge module: reads the merchant routing engine's Kalman-smoothed effective
prices and writes a JSON file that the Routstr daemon (routstrd) consumes to
overlay sats_pricing on its model cache, so the SDK's provider price ranking
uses Kalman-adjusted prices instead of static provider-published sats rates.

Pipeline:
  1. RealtimePricing singleton → snapshot of measured $/M per provider
  2. pricing_engine.compute_effective_price() → effective $/M with multipliers
  3. Convert $/M → sats/token (using configurable BTC price)
  4. Write JSON to ~/.routstrd/kalman_pricing.json (atomic, watched by routstrd)

Output shape:
  {
    "generated_at": "2026-08-13T19:00:00Z",
    "btc_price_usd": 95000.0,
    "sat_per_usd": 1052.63,
    "providers": {
      "ours":          {"effective_rate_per_m": 0.013, "sat_per_token": 0.0000137, "source": "zai_amortized"},
      "friend":        {"effective_rate_per_m": 0.001, "sat_per_token": 0.0000011, "source": "zai_amortized"},
      "ollama_cloud":  {"effective_rate_per_m": 0.016, "sat_per_token": 0.0000168, "source": "ollama_billing_api"},
      "ppq":           {"effective_rate_per_m": 0.14,  "sat_per_token": 0.0001474, "source": "ppq_ledger"},
      "openrouter":    {"effective_rate_per_m": 0.135, "sat_per_token": 0.0001421, "source": "openrouter_actual"},
      "deepinfra":     {"effective_rate_per_m": 1.30,  "sat_per_token": 0.0013684, "source": "deepinfra_actual"}
    }
  }

Usage:
  python3 export_kalman_pricing.py
  python3 export_kalman_pricing.py --out /path/to/output.json
  python3 export_kalman_pricing.py --btc-price 95000
  python3 export_kalman_pricing.py --watch   # continuous mode, refresh every 5 min
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Any

# Add merchant-routing-engine to path
_MRE_PATH = os.path.expanduser("~/merchant-routing-engine")
if _MRE_PATH not in sys.path:
    sys.path.insert(0, _MRE_PATH)
_MRE_SRC = os.path.join(_MRE_PATH, "src")
if _MRE_SRC not in sys.path:
    sys.path.insert(0, _MRE_SRC)

_log = logging.getLogger("export_kalman_pricing")

# ── Defaults ─────────────────────────────────────────────────────────────────
DEFAULT_OUTPUT = os.path.expanduser("~/.routstrd/kalman_pricing.json")
DEFAULT_BTC_PRICE = 95000.0  # USD per BTC — configurable via --btc-price or env
DEFAULT_REFRESH_INTERVAL = 300  # 5 minutes (matches RealtimePricing.DEFAULT_REFRESH_SECONDS)
DEFAULT_DB_PATH = os.path.expanduser("~/.hermes/bot/zai_usage.db")
DEFAULT_BURN_DB_PATH = os.path.expanduser("~/.hermes/bot/api_burn.db")

# Provider name → merchant routing engine canonical name
# This maps the provider identifiers used in the export JSON
ALL_PROVIDERS = ("ours", "friend", "ollama_cloud", "ppq", "openrouter", "deepinfra")


def _sat_per_usd(btc_price_usd: float) -> float:
    """Convert BTC price (USD/BTC) to satoshis per USD."""
    if btc_price_usd <= 0:
        return 1.0  # fail-safe: 1 sat per dollar (very cheap, but non-zero)
    return 100_000_000.0 / btc_price_usd  # 100M sats per BTC / price


def _dollars_per_m_to_sat_per_token(rate_per_m: float, sat_per_usd: float) -> float:
    """Convert $/M tokens to sats/token.

    $/M × (1M tokens / 1M) = $/token
    $/token × sat_per_usd = sat/token

    So: sat_per_token = rate_per_m * sat_per_usd / 1_000_000
    """
    if rate_per_m <= 0 or math.isinf(rate_per_m) or math.isnan(rate_per_m):
        return 0.0
    return (rate_per_m * sat_per_usd) / 1_000_000.0


def _write_atomic(path: str, data: str) -> None:
    """Write data to path atomically (temp file + rename)."""
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".kalman_pricing.", suffix=".json", dir=d or None)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def compute_kalman_pricing(
    btc_price_usd: float | None = None,
    db_path: str | None = None,
    burn_db_path: str | None = None,
) -> dict[str, Any]:
    """Compute Kalman effective prices for all providers and return the
    Routstr-consumable JSON dict.

    This is the core function. It:
      1. Refreshes the RealtimePricing singleton (collects measured rates)
      2. Reads the Kalman-smoothed base rate per provider
      3. Applies deterministic multipliers (peak, scarcity, health)
         via pricing_engine.compute_effective_price()
      4. Converts $/M → sats/token

    Never raises — a failure in one provider degrades to cold-start.
    """
    btc = btc_price_usd or float(
        os.environ.get("KALMAN_BTC_PRICE_USD", str(DEFAULT_BTC_PRICE))
    )
    sat_per_usd = _sat_per_usd(btc)

    # Import MRE modules (deferred so --help works without deps)
    try:
        from realtime_pricing import RealtimePricing
        from pricing_engine import compute_effective_price, MIN_EFFECTIVE_PRICE
    except ImportError as e:
        _log.error("Cannot import merchant routing engine modules: %s", e)
        _log.error("Ensure ~/merchant-routing-engine/src is accessible")
        raise

    # Get the RealtimePricing singleton and refresh
    pricing = RealtimePricing.get_instance(
        zai_db_path=db_path or DEFAULT_DB_PATH,
        burn_db_path=burn_db_path or DEFAULT_BURN_DB_PATH,
    )
    try:
        pricing.refresh()
    except Exception:
        _log.warning("RealtimePricing.refresh() failed — using cached snapshot", exc_info=True)

    snapshot = pricing.snapshot()
    now = time.time()

    providers_out: dict[str, dict[str, Any]] = {}

    for prov_name in ALL_PROVIDERS:
        try:
            obs = pricing.get_rate(prov_name)
            base_rate = float(obs.rate_per_m)
            source = obs.source

            # Compute effective price with deterministic multipliers.
            # We use a simplified version: just the base rate × peak × scarcity.
            # The full live_router computes quota pressure, health, pace — but
            # those require live quota/health state that the export doesn't have
            # at export time. The Kalman-smoothed base rate IS the core signal;
            # the multipliers are applied at routing time by the daemon's
            # KalmanPricingBridge based on live state.
            #
            # For the export, we produce the Kalman-smoothed effective rate
            # (base_rate with minimal multipliers). The daemon can further
            # adjust at routing time.
            effective_rate = compute_effective_price(
                base_rate=base_rate,
                provider=prov_name,
                quota_pct=0.0,  # no live quota state at export time
                failure_count=0,  # no live health state at export time
                breaker_tripped=False,
            )

            # Guard against inf/NaN (e.g. from a tripped breaker in the data)
            if math.isinf(effective_rate) or math.isnan(effective_rate):
                effective_rate = MIN_EFFECTIVE_PRICE

            sat_per_token = _dollars_per_m_to_sat_per_token(effective_rate, sat_per_usd)

            providers_out[prov_name] = {
                "effective_rate_per_m": round(effective_rate, 6),
                "base_rate_per_m": round(base_rate, 6),
                "sat_per_token": round(sat_per_token, 10),
                "sat_per_m": round(sat_per_token * 1_000_000, 4),
                "source": source,
                "is_measured": obs.is_measured,
                "confidence": round(obs.confidence, 3),
                "velocity": round(obs.velocity, 6),
                "kalman_updates": getattr(pricing, "_refresh_count", 0),
            }
        except Exception:
            _log.warning("Failed to compute pricing for %s", prov_name, exc_info=True)
            # Cold-start fallback
            cold_rates = {
                "ours": 0.001, "friend": 0.001, "ollama_cloud": 0.0155,
                "ppq": 0.14, "openrouter": 0.135, "deepinfra": 1.30,
            }
            rate = cold_rates.get(prov_name, MIN_EFFECTIVE_PRICE)
            sat_per_token = _dollars_per_m_to_sat_per_token(rate, sat_per_usd)
            providers_out[prov_name] = {
                "effective_rate_per_m": round(rate, 6),
                "base_rate_per_m": round(rate, 6),
                "sat_per_token": round(sat_per_token, 10),
                "sat_per_m": round(sat_per_token * 1_000_000, 4),
                "source": "cold_start_fallback",
                "is_measured": False,
                "confidence": 0.0,
                "velocity": 0.0,
                "kalman_updates": 0,
            }

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "btc_price_usd": btc,
        "sat_per_usd": round(sat_per_usd, 2),
        "providers": providers_out,
    }


def export_pricing(
    output_path: str = DEFAULT_OUTPUT,
    btc_price: float | None = None,
    db_path: str | None = None,
    burn_db_path: str | None = None,
    pretty: bool = True,
) -> dict[str, Any]:
    """Compute and write the Kalman pricing JSON to output_path. Returns the dict."""
    data = compute_kalman_pricing(btc_price, db_path, burn_db_path)
    text = json.dumps(data, indent=2 if pretty else None, sort_keys=False)
    _write_atomic(output_path, text + "\n")
    _log.info("Wrote Kalman pricing to %s (%d providers)", output_path, len(data["providers"]))
    return data


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="export_kalman_pricing",
        description="Export Kalman effective prices for Routstr integration.",
    )
    p.add_argument("--out", metavar="PATH", default=DEFAULT_OUTPUT,
                   help=f"output JSON path (default: {DEFAULT_OUTPUT})")
    p.add_argument("--btc-price", type=float, default=None,
                   help=f"BTC price in USD (default: {DEFAULT_BTC_PRICE})")
    p.add_argument("--db", metavar="PATH", default=None,
                   help=f"zai_usage.db path (default: {DEFAULT_DB_PATH})")
    p.add_argument("--burn-db", metavar="PATH", default=None,
                   help=f"api_burn.db path (default: {DEFAULT_BURN_DB_PATH})")
    p.add_argument("--pretty", action="store_true", default=True,
                   help="pretty-print JSON (default: true)")
    p.add_argument("--watch", action="store_true",
                   help=f"continuous mode: refresh every {DEFAULT_REFRESH_INTERVAL}s")
    p.add_argument("--interval", type=int, default=DEFAULT_REFRESH_INTERVAL,
                   help=f"refresh interval in seconds (default: {DEFAULT_REFRESH_INTERVAL})")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="enable debug logging")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        data = export_pricing(
            output_path=args.out,
            btc_price=args.btc_price,
            db_path=args.db,
            burn_db_path=args.burn_db,
            pretty=args.pretty,
        )
        print(json.dumps(data, indent=2))
    except Exception as e:
        print(f"export_kalman_pricing: error: {e}", file=sys.stderr)
        return 1

    if args.watch:
        interval = max(60, args.interval)
        _log.info("Watch mode: refreshing every %ds", interval)
        while True:
            try:
                time.sleep(interval)
                export_pricing(
                    output_path=args.out,
                    btc_price=args.btc_price,
                    db_path=args.db,
                    burn_db_path=args.burn_db,
                    pretty=args.pretty,
                )
            except KeyboardInterrupt:
                _log.info("Watch mode stopped.")
                break
            except Exception:
                _log.warning("Refresh cycle failed", exc_info=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())