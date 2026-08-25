#!/usr/bin/env python3
"""Check if the fixture builder is deterministic and rebuild."""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
from ox3a_build_fixtures import build_all

# Build and check json_extract prompts
fx = build_all()
marker = "--- TEXT ---\n"
n = 0
ok = 0
for it in fx["primary"]:
    if it["shape"] != "json_extract":
        continue
    n += 1
    if marker in it["prompt"]:
        tail = it["prompt"].split(marker, 1)[1]
        if len(tail.strip()) >= 20:
            ok += 1
            print(f"  {it['id']}: OK ({len(tail.strip())} chars)")
        else:
            print(f"  {it['id']}: marker present but empty tail")
    else:
        print(f"  {it['id']}: marker missing")

print(f"\n{n} json_extract items, {ok} with source text")

# Write rebuilt fixtures
outdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "eval", "ox3a", "fixtures")
os.makedirs(outdir, exist_ok=True)
for name, key in [("primary", "primary"), ("refusal_probes", "refusal_probes"), ("latency_micro", "latency_micro")]:
    path = os.path.join(outdir, f"{name}.json")
    with open(path, "w") as f:
        json.dump(fx[key], f, indent=2, ensure_ascii=False)
    print(f"Wrote {path} ({len(fx[key])} items)")
