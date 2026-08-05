"""One-off live gate check for src/openrouter_balance_collector.py.

Loads OPENROUTER_API_KEY read-only from ~/.hermes/.env (never printed/committed),
calls the live /api/v1/key endpoint, prints the parsed balance + a cron-style
status line. Manual validation only; safe to delete.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_env_path = os.path.expanduser("~/.hermes/.env")
if os.path.isfile(_env_path):
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("OPENROUTER_API_KEY="):
                os.environ["OPENROUTER_API_KEY"] = (
                    line.split("=", 1)[1].strip().strip('"').strip("'")
                )
                break

from src.openrouter_balance_collector import (  # noqa: E402
    OPENROUTER_KEY_ENDPOINT,
    collect_openrouter_balance,
)

if not os.environ.get("OPENROUTER_API_KEY"):
    print("NO KEY LOADED — skipping live gate")
    raise SystemExit(0)

print("Key loaded (len=%d). Running LIVE gate..." % len(os.environ["OPENROUTER_API_KEY"]))
t0 = time.time()
b = collect_openrouter_balance(timeout=12.0)
dt = time.time() - t0
if b is None:
    print("LIVE RESULT: None (collection failed)")
else:
    print("LIVE RESULT OK in %.2fs:" % dt)
    for k in ("usage", "limit", "limit_remaining", "usage_fraction", "used_pct",
              "is_unlimited", "is_exhausted", "is_free_tier", "limit_reset", "label"):
        print("  %-16s: %s" % (k, getattr(b, k)))
    gate_ok = b.usage is not None and (b.limit is not None or b.is_unlimited)
    print("  GATE (valid usage/limit from API):", gate_ok)
