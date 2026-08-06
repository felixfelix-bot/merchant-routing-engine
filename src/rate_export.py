"""rate_export.py — export real measured $/M rates from real_price_tracker for the CVM dashboard.

RP-5a of docs/plan-consolidated-remaining.md — "CVM dashboard: show real
measured rates".

Why this module exists
    The CVM dashboard (``demo/cvm-server``, TypeScript) previously re-implemented
    the measured-rate math in ``computeRealRates()``. That copy drifts from the
    Python module the proxy hot path actually consults
    (:func:`src.real_price_tracker.get_rate_with_fallback`). This module is the
    single source-of-truth bridge: it calls the *same* ``get_rate_with_fallback()``
    the router uses and emits a stable JSON shape the dashboard reads directly,
    so what the operator sees on the dashboard is exactly what the router paid.

Output shape (one entry per provider, exactly the format RP-5a asks for):

    [
      {"provider": "ours", "rate_per_m": 0.0134, "source": "measured",
       "measured": true,  "window_hours": 8760, "fallback_reason": null},
      {"provider": "ollama_cloud", "rate_per_m": 0.0155, "source": "fallback",
       "measured": false, "window_hours": 2160, "fallback_reason": "seed"},
      ...
    ]

* ``rate_per_m`` — the value :func:`get_rate_with_fallback` returns (always a float).
* ``source`` — ``"measured"`` when real ``cost_usd`` / billing-API data backs it,
  else ``"fallback"`` (cold-start seed, last-resort estimate, or unknown).
* ``measured`` — convenience boolean (``source == "measured"``).
* ``fallback_reason`` — finer classification for operators: ``"seed"``,
  ``"last_resort"``, ``"unknown"``, or ``null`` when measured.

Usage
    python3 -m src.rate_export               # print JSON to stdout
    python3 -m src.rate_export --out FILE    # write JSON to FILE (atomic)
    python3 -m src.rate_export --all         # include deepinfra + openrouter
    python3 -m src.rate_export --pretty      # pretty-print (indent=2)

The dashboard consumes the ``--out`` file; RP-5b's cron refresh job refreshes it
on a schedule so the dashboard always shows live rates without a Python
round-trip per snapshot.

Design rules (mirror real_price_tracker / cost_extraction)
    * NEVER raises — a bad/missing DB degrades every provider to its fallback so
      the dashboard always renders a complete 4-row table.
    * Pure function of (providers, db, now) — no hidden global state.
    * Cheap — reuses real_price_tracker's 5-min cache.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from typing import Any

from src import real_price_tracker as rpt

# The four providers the CVM dashboard renders as pricing tiles
# (ours/friend/ollama/ppq — see computeEstimatedVsMeasured keyMap in
# cvm-server.ts). This is the RP-5a acceptance set: "all 4 providers".
DASHBOARD_PROVIDERS: tuple[str, ...] = ("ours", "friend", "ollama_cloud", "ppq")

# Full set tracked by real_price_tracker (adds the two pay-per-token externals).
ALL_PROVIDERS: tuple[str, ...] = tuple(rpt.PROVIDER_WINDOW_HOURS)


def _is_measured(provider: str, *, db_path: str | None, _now: float | None) -> bool:
    """True iff :func:`get_rate_with_fallback`'s result is backed by a real
    measurement (not a seed/last-resort fallback).

    We mirror :func:`get_rate_with_fallback`'s OWN resolution path so the
    reported ``source`` is always consistent with the reported ``rate``:

    * branch 1 — :func:`get_real_rate` over the default 168h window. Any
      non-``None`` return (including ``0.0`` for a flat-rate subscription whose
      marginal cost is genuinely ~$0) counts as measured.
    * branch 2 — the Ollama billing API (``ollama_cloud`` only) is also a
      measurement; :func:`get_trailing_rate` incorporates it, so we accept a
      positive trailing rate as a measured signal for ``ollama_cloud``.

    This deliberately does NOT use :func:`get_rate_readiness`, which classifies
    over a different (per-provider trailing) window and can disagree with
    ``get_rate_with_fallback`` — e.g. reporting "seed" while the router actually
    uses a measured 0.0 amortized rate. Classifying on the same path the rate
    came from guarantees the two never contradict.
    """
    try:
        if rpt.get_real_rate(provider, None, db_path=db_path, _now=_now) is not None:
            return True
    except Exception:  # pragma: no cover - documented never-raise
        pass
    if provider == "ollama_cloud":
        try:
            trailing = rpt.get_trailing_rate(provider, db_path=db_path, _now=_now)
            if trailing is not None and trailing > 0:
                return True
        except Exception:  # pragma: no cover
            pass
    return False


def _fallback_reason(provider: str) -> str:
    """Finer classification for non-measured rows, for operators."""
    if provider in rpt.LAST_RESORT_RATES:
        return "last_resort"
    return "unknown"


def export_rates(
    providers: tuple[str, ...] | list[str] | None = None,
    *,
    db_path: str | None = None,
    _now: float | None = None,
) -> list[dict[str, Any]]:
    """Build the RP-5a rate table — one dict per provider.

    Each entry: ``{provider, rate_per_m, source, measured, window_hours,
    fallback_reason}``:

    * ``rate_per_m`` — :func:`real_price_tracker.get_rate_with_fallback` (the
      exact value the router consults).
    * ``source`` — ``"measured"`` / ``"fallback"`` — classified on the SAME
      resolution path that produced the rate, so the two are always consistent.
    * ``measured`` — ``source == "measured"``.
    * ``window_hours`` — the provider's trailing window from
      :data:`real_price_tracker.PROVIDER_WINDOW_HOURS`.
    * ``fallback_reason`` — ``"last_resort"`` / ``"unknown"`` / ``None``.

    Defaults to :data:`DASHBOARD_PROVIDERS`. Never raises: a per-provider query
    failure degrades that single row to its fallback rather than aborting the
    whole table — the dashboard must always render all rows.
    """
    provs = tuple(DASHBOARD_PROVIDERS) if providers is None else tuple(providers)

    out: list[dict[str, Any]] = []
    for p in provs:
        # The rate the router actually uses — get_rate_with_fallback, never raises.
        try:
            rate = rpt.get_rate_with_fallback(p, db_path=db_path, _now=_now)
        except Exception:  # pragma: no cover - documented never-raise
            rate = float(rpt.LAST_RESORT_RATES.get(p, rpt.UNKNOWN_PROVIDER_FALLBACK))

        # Classify source on the same path the rate came from.
        try:
            measured = _is_measured(p, db_path=db_path, _now=_now)
        except Exception:  # pragma: no cover
            measured = False

        if measured:
            source, reason = "measured", None
        else:
            source, reason = "fallback", _fallback_reason(p)

        out.append(
            {
                "provider": p,
                "rate_per_m": float(rate),
                "source": source,
                "measured": measured,
                "window_hours": rpt.PROVIDER_WINDOW_HOURS.get(p),
                "fallback_reason": reason,
            }
        )
    return out


def _write_atomic(path: str, data: str) -> None:
    """Write ``data`` to ``path`` atomically (temp file + rename).

    Atomic so a concurrent dashboard read never sees a half-written file.
    """
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".rate_export.", suffix=".json", dir=d or None)
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


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code (0 on success)."""
    p = argparse.ArgumentParser(
        prog="rate_export",
        description="Export real measured $/M rates from real_price_tracker (RP-5a).",
    )
    p.add_argument(
        "--out",
        metavar="PATH",
        help="write JSON to PATH (atomic) instead of stdout",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help=f"include all {len(ALL_PROVIDERS)} tracked providers (default: the 4 dashboard providers)",
    )
    p.add_argument(
        "--db",
        metavar="PATH",
        default=None,
        help=f"sqlite DB path (default: {rpt.DEFAULT_DB_PATH})",
    )
    p.add_argument(
        "--pretty",
        action="store_true",
        help="pretty-print JSON with indent=2",
    )
    args = p.parse_args(argv)

    providers = ALL_PROVIDERS if args.all else DASHBOARD_PROVIDERS
    rates = export_rates(providers, db_path=args.db)
    payload = {
        "generated_at": _now_iso(),
        "source": "real_price_tracker.get_rate_with_fallback",
        "providers": rates,
    }
    text = json.dumps(payload, indent=2 if args.pretty else None, sort_keys=False)

    if args.out:
        try:
            _write_atomic(args.out, text + "\n")
        except Exception as e:  # pragma: no cover - filesystem error
            print(f"rate_export: failed to write {args.out}: {e}", file=sys.stderr)
            return 1
    else:
        print(text)
    return 0


def _now_iso() -> str:
    """ISO-8601 UTC timestamp for the ``generated_at`` field. Never raises."""
    try:
        import datetime as _dt

        return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:  # pragma: no cover
        return ""


__all__ = [
    "DASHBOARD_PROVIDERS",
    "ALL_PROVIDERS",
    "export_rates",
    "main",
]


if __name__ == "__main__":
    sys.exit(main())
