#!/usr/bin/env python3
"""Validate providers.yaml ollama_cloud section per EUv2-1 gate requirements."""
import yaml
import sys

with open("config/providers.yaml") as f:
    d = yaml.safe_load(f)

oc = d["ollama_cloud"]
print("=== ollama_cloud section ===")
for k, v in oc.items():
    print(f"  {k}: {v}")

print()
print("=== model_map ollama_cloud ===")
mm = d["strategy"]["model_map"]["ollama_cloud"]
for k, v in mm.items():
    print(f"  {k}: {v}")

print()
# Gate checks
errors = []
if oc["key_env"] != "OLLAMA_CLOUD_API_KEY":
    errors.append(f"key_env is {oc['key_env']}, expected OLLAMA_CLOUD_API_KEY")
if oc.get("quota_windows") != ["5h", "weekly"]:
    errors.append(f"quota_windows is {oc.get('quota_windows')}, expected [5h, weekly]")
if oc.get("included_quota_tokens_session") != 500000000:
    errors.append(f"session quota is {oc.get('included_quota_tokens_session')}, expected 500000000")
if oc.get("included_quota_tokens_weekly") != 3500000000:
    errors.append(f"weekly quota is {oc.get('included_quota_tokens_weekly')}, expected 3500000000")
if oc.get("extra_usage_rate_per_m") != 0.15:
    errors.append(f"extra_usage_rate_per_m is {oc.get('extra_usage_rate_per_m')}, expected 0.15")
if "rate_limit_resets_every_hours" in oc:
    errors.append("old field rate_limit_resets_every_hours still present")
if not all(v == "glm-5.2" for v in mm.values()):
    errors.append(f"model_map not all glm-5.2: {mm}")

if errors:
    print("GATE FAILED:")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
else:
    print("ALL GATE CHECKS PASSED")
    sys.exit(0)