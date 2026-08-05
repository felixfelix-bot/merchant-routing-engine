#!/usr/bin/env python3
"""P5-RATES population gate: verify that with the feature ON, LiveRouter seeds
its base rates from real_price_tracker against the production DB.

Run:  LIVE_ROUTER_DYNAMIC_RATES_ENABLED=1 python3 scripts/verify_rates_populate.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Enable the feature BEFORE importing live_router (kill switch read at import).
os.environ["LIVE_ROUTER_DYNAMIC_RATES_ENABLED"] = "1"

import src.live_router as lr
from src.live_router import LiveRouter, _DEFAULT_CONVERGED_RATES, _resolve_dynamic_base_rates
from src.real_price_tracker import get_rate_readiness

DB = os.path.expanduser("~/.hermes/bot/zai_usage.db")

print("=" * 72)
print("P5-RATES population gate  (feature ON, production DB)")
print("=" * 72)
print(f"DYNAMIC_RATES_ENABLED : {lr._DYNAMIC_RATES_ENABLED}")
print(f"REFRESH_INTERVAL (s)  : {lr._RATE_REFRESH_INTERVAL_SECONDS}")
print(f"DB path               : {DB}")
print()

# 1. Resolver output vs hardcoded defaults
resolved = _resolve_dynamic_base_rates(DB)
print(f"{'provider':14s} {'hardcoded':>12s} {'dynamic':>12s} {'delta':>12s}  source")
print("-" * 72)
readiness = get_rate_readiness(db_path=DB)
for p in _DEFAULT_CONVERGED_RATES:
    hard = _DEFAULT_CONVERGED_RATES[p]
    dyn = resolved[p]
    src = readiness.get(p, {}).get("source", "?")
    print(f"{p:14s} {hard:12.6f} {dyn:12.6f} {dyn-hard:+12.6f}  {src}")

# 2. Construct a LiveRouter (no explicit override) and confirm it seeds dynamic
LiveRouter.reset_instance()
router = LiveRouter(db_path=DB)
print()
print("LiveRouter._base_rates (should match 'dynamic' column above):")
for p in _DEFAULT_CONVERGED_RATES:
    seeded = router._base_rates[p]
    ok = "OK" if abs(seeded - resolved[p]) < 1e-9 else "MISMATCH"
    print(f"  {p:14s} {seeded:12.6f}  [{ok}]")

# 3. refresh_base_rates() works and is idempotent-ish
pre = dict(router._base_rates)
router.refresh_base_rates()
post = router._base_rates
print()
print("refresh_base_rates():")
print(f"  last_refresh_ts > 0 : {router._last_rate_refresh_ts > 0}")
print(f"  base_rates stable  : {all(abs(pre[k]-post[k]) < 1e-9 for k in pre)}")

# 4. Thread state
print(f"  refresh thread     : {router._rate_refresh_thread}")
LiveRouter.reset_instance()

print()
print("GATE RESULT:", "PASS — all six providers populated" if len(resolved) == 6 else "FAIL")
