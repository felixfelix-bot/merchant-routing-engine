"""Dump the REAL ollama usage API shape (masked) to understand correct parsing."""
import os, sys, json
sys.path.insert(0, "/home/c03rad0r/merchant-routing-engine")
os.chdir("/home/c03rad0r/merchant-routing-engine")
from src.ollama_extra_usage import fetch_ollama_usage

def mask(d, depth=0):
    if isinstance(d, dict):
        return {k: mask(v, depth+1) for k, v in d.items()}
    if isinstance(d, list):
        return [mask(x, depth+1) for x in d[:3]] + (["...+%d more" % (len(d)-3)] if len(d) > 3 else [])
    if isinstance(d, str) and len(d) > 40:
        return d[:15] + "...(len %d)" % len(d)
    return d

data = fetch_ollama_usage()
print(json.dumps(mask(data), indent=2, default=str)[:3000])
