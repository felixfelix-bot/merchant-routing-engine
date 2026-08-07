"""Directly test _measure_ollama_billing to resolve the contradiction."""
import os, sys, traceback
sys.path.insert(0, "/home/c03rad0r/merchant-routing-engine")
os.chdir("/home/c03rad0r/merchant-routing-engine")
from src.realtime_pricing import RealtimePricing

rp = RealtimePricing.get_instance()
print("=== calling _measure_ollama_billing() directly ===")
try:
    result = rp._measure_ollama_billing()
    print("returned WITHOUT raising. keys:", list(result.keys()))
    for k, ob in result.items():
        print(f"  {k}: rate=${ob.rate_per_m:.6f} src={ob.source} measured={ob.is_measured} tokens={ob.sample_tokens}")
except Exception as e:
    print("RAISED:", repr(e))
    traceback.print_exc()

# Also inspect what fetch returns for activity type
from src.ollama_extra_usage import fetch_ollama_usage
d = fetch_ollama_usage()
act = d.get("activity") if d else None
print("\nactivity type:", type(act).__name__)
print("isinstance dict:", isinstance(act, dict))
if isinstance(act, dict):
    print("activity.items() first 3:", [(k, type(v).__name__) for k, v in list(act.items())[:3]])
    # what does iterating give?
    for mn, entry in list(act.items())[:1]:
        print(f"  first item: model_name={mn!r} entry_type={type(entry).__name__} entry={entry!r}")
        print(f"  entry.get exists: {hasattr(entry, 'get')}")
