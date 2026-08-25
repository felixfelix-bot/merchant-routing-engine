# OX-3a oxalpha tier eval — report

Date: 2026-08-22  ·  campaign: `campaign-20260822-210133.json`

## Verdict: **ABORTED — usage delta after full set: 17.895519683**

| # | criterion | pass | evidence |
|---|---|---|---|
| 1 | quality_noninferiority | PASS | no pairs |
| 2 | deterministic_floor | FAIL | json_validity 0.6666666666666666, verdict ox/base {'ox': 0.6666666666666666, 'base': 0.4666666666666667, 'n': 15}, outcome ox {'ox': 0.9333333333333333, 'n': 15} |
| 3 | refusals | PASS | primary 0 (must be 0); probes 0/10  |
| 4 | latency | PASS | micro-set p50 9.305669171037152s, p95 12.179388873220885s (caps 25.0/60.0) |
| 5 | effort_ab | FAIL | low None vs max None |
| 6 | spend | FAIL | usage deltas [0.00797694] |

## Burst probe

```json
null
```

## Spend evidence

```json
{
  "key_before": {
    "usage": 17.887542738,
    "limit": null
  },
  "key_after_canary": {
    "usage": 17.887542738,
    "limit": null
  },
  "key_after_full_set": {
    "usage": 17.895519683,
    "limit": null
  }
}
```

## Per-shape rubric means

| shape | ox mean | base mean | gap | within-1 | n |
|---|---|---|---|---|---|

## Latency breakdown (ox arm)

| set | n | p50 (s) | p95 (s) |
|---|---|---|---|
| micro (1-3k digests) | 10 | 9.3 | 12.2 |
| shape: build_summary | 15 | 4.8 | 7.4 |
| shape: code_review | 15 | 8.5 | 14.7 |
| shape: doc_writing | 15 | 7.3 | 15.7 |
| shape: json_extract | 15 | 1.9 | 6.3 |
| aggregate | 70 | 6.4 | 13.4 |

Criterion 4 is evaluated on the micro set (p50 <= 25.0s, p95 <= 60.0s).
