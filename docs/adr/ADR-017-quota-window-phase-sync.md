# ADR-017: Quota-Window Phase Synchronization to Authoritative Reset Anchors

- **Status:** Accepted (operator-ratified 2026-09-15)
- **Required by:** ADR-016 (the target-pressure controller needs a true `t_reset`).

## Context

The pacing logic needs to know **how much of each quota window has elapsed**, which
is only correct if the window is anchored to the provider's **actual reset time**.

Inspecting the code showed an inconsistency:

- `live_router.py` anchors z.ai windows on the real reset:
  `window_start = resets_at − window_seconds; elapsed = (now − window_start)/window_seconds`.
- **`ollama_quota_tracker.py` uses rolling windows** (`now − 5h`, `now − 7d`) computed
  from the local `api_calls` table. These are **out of phase** with the provider's
  real reset, so session/weekly pressure — and therefore pacing — is wrong.

When an API does not expose `resets_at`, we still need a stable anchor.

## Decision

1. **All windows are anchored to the authoritative `resets_at`**, for every provider
   (z.ai, Ollama, and any future lane). Elapsed/remaining are computed from that
   anchor, never from a rolling `now − window`.
2. Replace the rolling windows in `ollama_quota_tracker.py` with anchored windows via
   the shared extractor (`quota_window_extractor`).
3. **Infer/observe the anchor** when `resets_at` is absent: detect the **first request
   after a usage drop** (the reset boundary), record it, and persist the anchor
   (`resets_at ≈ observed_reset`); refine as more boundaries are observed. Store
   anchors in a small state file alongside the existing Kalman state.
4. **Clamp** elapsed to `[0, 1]`; skip malformed/error-sentinel windows
   (`used_pct=999`, `resets_at=0`) exactly as the extractor already does.

## Consequences

- (+) Pressure/pacing is computed against the real entitlement clock → ADR-016 can
  actually hit its target.
- (+) One anchoring path for all providers; fewer provider-specific special cases.
- (−) Anchor inference needs ≥1 observed boundary after a cold start (graceful
  degradation: fall back to the reported `used_pct` until then).
- (−) Must persist anchor state per provider/window and handle provider clock skew.
- **Risk if ignored:** pacing aims at the wrong reset, so quotas exhaust early or are
  wasted — the exact failure ADR-016 exists to prevent.

## References

- `src/quota_window_extractor.py`, `src/ollama_quota_tracker.py`,
  `src/live_router.py` (`_zai_window_usages`, reset anchoring),
  `~/.hermes/bot/zai_proxy.py` (`quota_cache`, `_parse_limit_entry`).
- ADR-016; plan §32; hermes `DECISIONS.md` D-143.
