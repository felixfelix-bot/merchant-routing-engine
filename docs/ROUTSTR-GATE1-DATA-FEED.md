# Routing Transparency + Confidence — Gate 1 Data Feed

Source: `src/routing_gate1.py` · Tests: `tests/test_routing_gate1.py`
Part of: merchance-routing plan T-F (routing transparency + confidence),
ADR-007 gated routstr quota-selling.

## What problem does this solve?

The routstr-readiness check has four ADR-007 gates before any z.ai quota can be
sold. **Gate 1 ("router MAPE < 15%")** must read *live* accuracy off the actual
routing decision data — not a hand-tuned or idle static value. Until now, the
router's self-assessment was not a reproducible feed the readiness checker could
trust; the decision tables carried no caller-class field, and no live
mean-absolute-percentage-error (MAPE) was computed over the decisions the router
actually made.

This module closes that gap:

1. **Caller-class schema** — `ensure_decision_schema()` idempotently adds the
   `caller_class` column (`internal` | `sold`) to both decision tables
   (`routing_live_decisions`, `flat_router_shadow_decisions`). Sold traffic is
   henceforth distinguishable from internal in the record. Safe to run on every
   feed tick; never duplicates the column.
2. **Live decision logging** — `log_live_decision()` persists one routed
   decision with its caller_class, so the record is complete on the data path
   that a future priority-routing branch writes.
3. **Live MAPE feed** — `compute_live_mape()` converts per-model (predicted,
   realized) `$/M` series into the weighted mean-absolute-percentage-error that
   Gate 1 (`< 15%`) consumes. `gate1_feed()` wires DB → score.
4. **Exhaustion trustability** — `exhaust_trust()` validates that a reported
   `hours-to-exhaustion` equals `remaining_tokens / burn_rate_tph` within a
   tolerance. This is the internal-consistency check the sold-safety branch
   (priority routing) depends on before admitting a sold request.
5. **Deterministic routing** — the smoke tests assert `select_provider(model)`
   always returns the cheapest-**healthy** candidate, ascending, failing open to
   `fallback` (cost `inf`) only when nothing is viable.

## Design rules

* **Never raises on DB trouble.** A broken/unavailable DB yields the
  "UNVERIFIED" state (`n=0`, `mape_pct=null`) — the same idle-gate state the
  readiness checker already knows to leave silent. Fail-safe, never a crash on
  the hot path.
* **Weighted MAPE, not a mean of means.** Each aligned (predicted, realized)
  observation weighs equally, so a well-measured model is neither diluted nor
  amplified by a thin one.
* **Zero-realized points are dropped, not scored.** A realized `$0.00/M` is a
  measurement gap, not a 100% error (same fail-safe philosophy as the routstr
  gates).
* **Idempotent schema.** `ensure_decision_schema()` may run every tick; `ALTER
  TABLE ADD COLUMN` is guarded so a concurrent/prior create never duplicates.

## CLI

From the repo root:

```bash
python3 -m src.routing_gate1 --db PATH --caller-class sold
# {"gate1": {"mape_pct": 12.4, "n": 60, "threshold_pct": 15.0, "pass": true}}
```

## Gate 1 semantics

| Field        | Meaning                                             |
|--------------|-----------------------------------------------------|
| `mape_pct`   | weighted live MAPE over decision data, `null` if too thin |
| `n`          | number of useable observations                      |
| `threshold_pct` | the Gate 1 cutoff (15.0)                        |
| `pass`       | `mape_pct < 15.0` (and not `null`)                  |

## Dependencies / boundary

Full caller-class *capture* (deriving `sold` from routstr bearer keys at the
proxy entry) is a separate card (T-A). This module provides the schema +
logging + live MAPE such that once capture lands, Gate 1 reads it reliably.