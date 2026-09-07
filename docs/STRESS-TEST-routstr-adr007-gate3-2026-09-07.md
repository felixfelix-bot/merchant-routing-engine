# STRESS TEST — Routstr ADR-007 Gate 3 (T-C)

**Status:** SHIPPED (worker-merchant-qa)
**Date:** 2026-09-07
**Repo:** `~/merchant-routing-engine`
**Module:** `stress_test.py` (+ `test_stress_test.py`)
**Deployed outputs:** `~/.hermes/bot/stress_test.py`,
`~/.hermes/bot/stress_test_results.json`
**Plan:** `docs/PLAN-routstr-serving-lane-2026-09-07.md` → T-C

---

## 1. Purpose

ADR-007 Gate 3 requires proof that, with z.ai (the `ours` provider) under
heavy quota pressure (~90% exhausted), the router degrades **sold** traffic
without ever harming **internal** traffic. `stress_test.py` is the harness
that simulates that pressure and asserts the ADR-007 degradation contract
continuously.

## 2. The three asserted invariants

| # | Invariant | Mechanism under test | Pass condition |
|---|-----------|----------------------|----------------|
| 1 | **Internal NEVER blocked** | `simulate_request("internal", preds)` → `_sold_gate` → `flat_router.sold_429_gate` | `internal_blocked == 0` for the whole run |
| 2 | **Sold degrades via 429 + Retry-After** | `simulate_request("sold", preds)` with imminent-exhaustion predictions | `status == "429"` and `retry_after == 120` (> 0) |
| 3 | **Delist triggers < 5 min after threshold** | `DelistTracker(threshold_pct=90.0, max_delay_s=300)` | `trigger_latency_s <= 300` once over threshold |

The gate decision reuses the **real** `flat_router.sold_429_gate()` (T-A),
so the harness exercises the actual production degradation path rather than
a test-only copy. If `flat_router` is unavailable the gate fails OPEN
(routes) — internal and normal sold traffic are never harmed by a missing
module (mirrors the production proxy's never-raise contract).

## 3. Design

- **`simulate_request(caller_class, predictions, safety_hours=2.0)`** — one
  routed-request decision. Returns `{"status": "routed"|"429", "blocked",
  "retry_after", ...}`.
- **`DelistTracker(threshold_pct=90.0, max_delay_s=300)`** — records quota
  health samples in REAL wall-clock time (`time.monotonic()`). The delist
  decision fires immediately once at/over the threshold and
  `trigger_latency_s` records the actual seconds from first crossing to
  first decision. If a decision would be made later than `max_delay_s`, the
  tracker sets `cap_violation=True` so the <5-min invariant can FAIL rather
  than be vacuously satisfied. Recovering below the threshold clears and
  re-arms the timer.
- **`run_stress(duration_s=86400, ...)`** — the PACED soak loop. Runs for
  exactly `duration_s` wall-clock seconds (24h by default) with a
  `request_interval_s` sleep per iteration (a realistic continuous load
  profile, not an unpaced CPU spin). It alternates every 100 iterations
  between a PRESSURE phase (simulated 92% used, `will_exhaust` within 0.8h —
  sold gated) and a HEALTHY phase (40% used, `will_exhaust` False — sold
  served). Green requires BOTH `sold_429 > 0` AND `sold_served > 0`, so a
  regression that blanket-429s sold, or never gates it, cannot slip past.
- **`write_results(summary, out_path=DEFAULT)`** — writes
  `~/.hermes/bot/stress_test_results.json` with `status: pass|fail` plus the
  three assertion booleans and run detail.

## 4. Running it

```bash
# 24h continuous soak (default):
cd ~/merchant-routing-engine && python3 stress_test.py

# Short smoke run with a custom results path:
python3 stress_test.py --duration 10 --out /tmp/stress_results.json

# Unit tests (the relevant suite is flat_router + stress tests):
python3 -m pytest test_flat_router.py test_stress_test.py -q
```

Exit code is 0 on green, 1 on red. On completion the results JSON is
written and carries `"status": "pass"` only when every assertion is green.

## 5. HOLD compliance (plan §8)

This harness **simulates** pressure only. It never routes real sold traffic
to z.ai or any provider (no live HTTP), and it never delists a live routstr
listing. The production delist script is owned by **T-D**
(`scripts/routstr_delist.py`); this harness verifies the **< 5 min
time-to-delist property** against its own `DelistTracker` so Gate 3 is
independently testable without waiting on T-D.

## 6. Acceptance (plan T-C "Verify")

- `~/.hermes/bot/stress_test_results.json` exists and has
  `"status": "pass"` — this is Gate 3's success criterion for the routstr
  readiness check.

## 7. Quality-gate evidence

- **Gate 1 (TDD):** `test_stress_test.py` (14 tests) written first; all 14
  observed FAILING against the empty module, then GREEN after
  `stress_test.py` landed.
- **Gate 2 (Tests pass):** `pytest test_flat_router.py test_stress_test.py`
  → `105 passed`. (Repo-wide `pytest -q` collection is blocked by 2
  PRE-EXISTING env/path errors — `scripts/test_export_kalman.py` and
  `tests/test_urgency_cost_estimator.py` resolve the wrong tree's `src/`;
  identical in the parent T-A worktree, unrelated to this change.)
- **Gate 2.5 (Cold review):** a fresh-context reviewer flagged the original
  design's fake-time delist latency, dead `max_delay_s` logic, unpaced soak
  loop, and a dead `sold_fraction` param. All findings were addressed:
  wall-clock latency, enforced cap with `cap_violation`, paced loop, mixed
  pressure/healthy phases, and single-source-of-truth pass/fail.
- **Gate 3 (Docs):** this file, committed with the source.
