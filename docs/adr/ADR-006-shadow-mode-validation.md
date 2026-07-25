# ADR-006: Shadow Mode Validation Before Production Routing

## Status

Proposed

## Date

2025-07-25

## Related

- `zai_proxy.py` lines 1061-1162 (`best_key()` — current production routing)
- ADR-001 (price-first routing)
- `docs/price-first-kalman-plan.md` §5 (shadow mode)

## Context

The price-first routing engine (ADR-001) fundamentally changes how provider selection works. The current proxy uses a hardcoded cascade with `best_key()` for ours-vs-friend selection. Replacing this directly is high-risk: if the new engine produces bad decisions under real load, every API request is affected.

Previous incidents have shown that routing bugs cascade quickly: a bad key selection → 429 storm → circuit breaker trips on both keys → PPQ/OpenRouter credit burn ($5+/day in 2024). The proxy is the sole gatekeeper for all LLM traffic.

## Decision

**The price-first engine runs in shadow mode for a minimum of 48 hours before any production routing changes. Shadow mode logs what the engine WOULD have chosen alongside what the live system actually chose, without affecting live routing.**

Shadow mode implementation:

```python
# In zai_proxy.py do_POST, AFTER the live routing decision is made:
def _log_shadow_decision(live_key, live_model, request_body):
    """Read-only hook. Does NOT affect routing."""
    try:
        shadow = routing_optimizer.route(request_body)
        db.execute(
            "INSERT INTO routing_shadow_decisions ...",
            (live_key, live_model, shadow.provider, ...)
        )
    except Exception:
        pass  # Shadow failures must NEVER affect live routing
```

Three-phase promotion:

1. **Phase 1 — Shadow (48h minimum)**: Engine runs alongside, logs decisions. Live routing untouched. Validated by: decision coverage >99%, no shadow crashes, shadow decisions are sane.
2. **Phase 2 — Advisor (72h minimum)**: Engine is primary signal, `best_key()` is exception fallback. Validated by: zero increase in failed requests, zero increase in PPQ spend.
3. **Phase 3 — Primary**: `best_key()` removed. Engine is sole router.

## Invariants

1. Shadow logging runs in a separate thread and NEVER blocks the live request path.
2. Shadow failures are swallowed silently. A shadow crash must not affect production.
3. No phase advances without meeting its validation criteria.
4. Phase 2 (advisor mode) has an automatic exception fallback to `best_key()`.
5. Every phase is reversible:
   - Phase 1 → revert: remove shadow hook (zero production impact)
   - Phase 2 → revert: `git checkout HEAD~1 -- zai_proxy.py && systemctl restart`
   - Phase 3 → revert: same as Phase 2 (best_key() re-added temporarily)

## Consequences

### Positive
- Zero production risk during validation (shadow mode is read-only)
- Real data validates the model (not theoretical testing)
- Quantitative comparison: "shadow engine would have saved $X over 48h"
- Each promotion is a conscious decision with data backing it
- Reversible at every step

### Costs
- Total validation time: ~5 days (48h shadow + 72h advisor) before full deployment
- Shadow logging adds minor overhead (<1ms per request, measured)
- Requires storage for shadow decision logs (new DB table)
