"""Check why fetch_ollama_usage returns nothing usable."""
import os, sys, json
sys.path.insert(0, "/home/c03rad0r/merchant-routing-engine")
os.chdir("/home/c03rad0r/merchant-routing-engine")

# Check for ollama API key in env / .env
env_file = os.path.expanduser("~/.hermes/.env")
key_present = False
if os.path.exists(env_file):
    with open(env_file) as f:
        for line in f:
            if "OLLAMA" in line.upper() and "=" in line and not line.strip().startswith("#"):
                # mask the value
                k = line.split("=")[0]
                print(f"  .env has: {k}=***")
                key_present = True
print("OLLAMA key in .env:", key_present)
print("OLLAMA env vars:", [k for k in os.environ if "OLLAMA" in k.upper()])

try:
    from src.ollama_extra_usage import fetch_ollama_usage
    data = fetch_ollama_usage()
    print("\nfetch_ollama_usage() returned:", type(data).__name__)
    if data is None:
        print("  -> None (API call failed or no data)")
    else:
        # Print structure without secrets
        print("  top keys:", list(data.keys()) if isinstance(data, dict) else "n/a")
        act = data.get("activity") if isinstance(data, dict) else None
        if isinstance(act, dict):
            print(f"  activity models: {len(act)} -> {list(act.keys())[:8]}")
            for m, e in list(act.items())[:3]:
                if isinstance(e, dict):
                    print(f"    {m}: cost={e.get('cost')} tokens={e.get('total_tokens') or e.get('tokens')} reqs={e.get('request_count') or e.get('requests')}")
        else:
            print("  activity:", repr(act)[:200])
except Exception as e:
    print(f"\nfetch_ollama_usage raised: {e!r}")

# Also: what's the actual ollama_cloud token volume this month (drives fallback)?
import sqlite3, time
def month_start(now):
    import calendar
    t = time.gmtime(now)
    return calendar.timegm(time.struct_time((t.tm_year, t.tm_mon, 1, 0,0,0,0,0,0)))
conn = sqlite3.connect(os.path.expanduser("~/.hermes/bot/zai_usage.db"))
row = conn.execute("SELECT COALESCE(SUM(total_tokens),0), COUNT(*) FROM api_calls WHERE key_name='ollama_cloud' AND ts >= ?", (month_start(time.time()),)).fetchone()
print(f"\nollama_cloud this month: {row[0]} tokens across {row[1]} calls")
print(f"  -> $100/mo amortized = ${100/(row[0]/1e6):.5f}/M" if row[0] else "  (no tokens)")
conn.close()
