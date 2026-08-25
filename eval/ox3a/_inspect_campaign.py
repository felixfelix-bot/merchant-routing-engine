#!/usr/bin/env python3
"""Inspect campaign data — not committed."""
import json, sys
from collections import Counter

path = 'eval/ox3a/results/campaign-20260822-210133.json'
with open(path) as f:
    d = json.load(f)

print('aborted:', d.get('aborted'))
print('abort_reason:', d.get('abort_reason'))
print()

# Stages
print('Stages:', json.dumps(d.get('stages', {}), indent=2)[:500])
print()

# Usage evidence
print('Usage evidence:', json.dumps(d.get('usage_evidence', {}), indent=2))
print()

# Pricing evidence
print('Pricing evidence:', json.dumps(d.get('pricing_evidence', {}), indent=2)[:300])
print()

# Anomaly row
print('Anomaly row:', json.dumps(d.get('anomaly_row', {}), indent=2)[:500])
print()

# Paired records
pr = d.get('paired_records', [])
print(f'paired_records: {len(pr)}')
if pr:
    print('  first keys:', list(pr[0].keys()))
    arms = Counter(r.get('arm') for r in pr)
    shapes = Counter(r.get('shape') for r in pr)
    print('  arms:', dict(arms))
    print('  shapes:', dict(shapes))
    # Sample
    s = pr[0]
    print('  sample:', json.dumps({k: v for k, v in s.items() if k != 'response'}, indent=2)[:500])

# Refusal probes
rp = d.get('refusal_probe_records', [])
print(f'\nrefusal_probe_records: {len(rp)}')
if rp:
    print('  first keys:', list(rp[0].keys()))

# Latency micro
lm = d.get('latency_micro_records', [])
print(f'\nlatency_micro_records: {len(lm)}')
if lm:
    print('  first keys:', list(lm[0].keys()))

# Effort max
em = d.get('effort_max_records', [])
print(f'\neffort_max_records: {len(em)}')
if em:
    print('  first keys:', list(em[0].keys()))
