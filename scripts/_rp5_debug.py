"""Focused debug for (b) ollama aggregate + (f) kill-switch toggle."""
import os, sys, math
sys.path.insert(0, "/home/c03rad0r/merchant-routing-engine")
os.chdir("/home/c03rad0r/merchant-routing-engine")
from unittest.mock import patch

from src.realtime_pricing import RealtimePricing, is_realtime_pricing_enabled, SRC_COLD_START, MEASURED_SOURCES

rp = RealtimePricing.get_instance()
snap = rp.refresh()
print("=== by_provider_model entries (ollama*) ===")
for k, ob in sorted(snap.by_provider_model.items()):
    if k[0].startswith("ollama") or k[0] in ("ppq","openrouter","deepinfra","ours","friend"):
        print(f"  {k}: rate=${ob.rate_per_m:.6f} src={ob.source} vel={getattr(ob,'velocity',None)}")
print()
print("=== by_provider (aggregates) ===")
for p, ob in sorted(snap.by_provider.items()):
    print(f"  {p}: rate=${ob.rate_per_m:.6f} src={ob.source}")
print()
print("=== (f) kill switch toggle ===")
print("enabled now:", is_realtime_pricing_enabled())
before = rp.snapshot()
print("before id:", id(before), "ts:", before.ts, "refresh_count:", before.refresh_count)
with patch.dict(os.environ, {"REALTIME_PRICING_ENABLED": "false"}):
    print("enabled under patch:", is_realtime_pricing_enabled())
    try:
        ret = rp.refresh()
        print("ret id:", id(ret), "ts:", ret.ts, "refresh_count:", ret.refresh_count)
        print("ret is before:", ret is before)
        print("ret.ts == before.ts:", ret.ts == before.ts)
    except Exception as e:
        print("refresh raised:", repr(e))
