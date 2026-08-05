#!/usr/bin/env python3
"""Quick smoke check: compile + import + resolver output."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import py_compile
py_compile.compile("src/live_router.py", doraise=True)
print("COMPILE OK")
import src.live_router as lr
print("import OK; DYNAMIC_ENABLED:", lr._DYNAMIC_RATES_ENABLED)
print("REFRESH_INTERVAL:", lr._RATE_REFRESH_INTERVAL_SECONDS)
rates = lr._resolve_dynamic_base_rates()
print("resolved rates (production DB):")
for k, v in rates.items():
    print(f"  {k:14s} {v:.6f}")
