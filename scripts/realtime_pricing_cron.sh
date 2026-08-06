#!/usr/bin/env bash
# realtime_pricing_cron.sh — every-5min RealtimePricing collector (RP-5 STEP 1).
#
# Drives RealtimePricing.get_instance().refresh(): collects fresh $/M
# observations from all 5 sources (z.ai amortized, ollama billing, ppq ledger,
# deepinfra spend, openrouter spend), feeds the per-(provider,model) Kalman
# grid, persists to price_observations, and atomically swaps the snapshot.
#
# SHADOW-ONLY: RealtimePricing is NOT wired into the routing hot path
# (no LiveRouter/pricing_engine call site reads it yet), so running this
# collector cannot change routing — it only populates the measured-rate
# snapshot for offline analysis and the 48h shadow validation. The
# REALTIME_PRICING_ENABLED kill switch (default true) gates collection;
# setting it falsy makes refresh() a no-op that returns the cold-start
# snapshot (reproducing old static-rate behaviour — design doc §6).
#
# Watchdog semantics (matches operator "silent when healthy" preference,
# cf. ppq_balance_cron.sh):
#   * healthy refresh                 → SILENT (empty stdout)
#   * refresh raised / returned no    → one-line alert
#     measured obs at all
#   * stale snapshot (>30 min old)    → one-line alert
#
# Cron (no_agent=true) delivers stdout verbatim; empty stdout == silent.
set -u

REPO="/home/c03rad0r/merchant-routing-engine"

# STEP 4: enable collection (shadow only — see header). Kill switch default is
# already "true"; we export it explicitly so the intent is unambiguous and the
# cron is self-documenting.
export REALTIME_PRICING_ENABLED="${REALTIME_PRICING_ENABLED:-true}"

cd "$REPO" || { echo "⚠️ RP collector: cannot cd to $REPO"; exit 0; }

# Delegate all logic + alerting to Python so the decision is testable and
# avoids fragile bash JSON parsing. Prints nothing on healthy refresh.
PYTHONPATH="$REPO" python3 - <<'PY'
import os, sys, time, math, json

try:
    from src.realtime_pricing import RealtimePricing
except Exception as e:
    print(f"⚠️ RealtimePricing import FAILED: {e!r} — collectors cannot run.")
    sys.exit(0)

try:
    rp = RealtimePricing.get_instance()
    before = rp.snapshot()
    snap = rp.refresh()
except Exception as e:
    print(f"⚠️ RealtimePricing.refresh() raised: {e!r} — no snapshot this cycle.")
    sys.exit(0)

now = time.time()
age = now - snap.ts

# NaN / non-finite guard (criterion e).
nan_keys = []
for key, ob in snap.by_provider_model.items():
    r = getattr(ob, "rate_per_m", None)
    if r is None or r != r or math.isinf(r):
        nan_keys.append(key)
if nan_keys:
    print(f"⚠️ RealtimePricing: NaN/inf rate_per_m for {len(nan_keys)} keys: {nan_keys[:5]}")
    sys.exit(0)

# Stale guard (criterion e: stale > 30 min).
if age > 1800:
    print(f"⚠️ RealtimePricing snapshot STALE: {age/60:.0f} min old (threshold 30 min).")
    sys.exit(0)

# All-good: silent (healthy refresh, finite rates, fresh snapshot).
sys.exit(0)
PY
