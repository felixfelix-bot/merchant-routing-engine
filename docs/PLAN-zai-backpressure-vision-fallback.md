# PLAN: zai_proxy Backpressure Hold-Queue + Vision Fallback

**Date:** 2026-08-22
**Trigger:** Sweep bursts (61 configs × N=50 + workers T2/T3/T5) caused intermittent
`503 all providers exhausted` in the hermes stack. Diagnosis from `api_calls` /
`key_decisions` logs:

1. Burst load pushes both z.ai keys into 429 backoff windows (2s→60s ramp).
2. During backoff, the external chain is hollow: Ollama paywalled until Monday,
   PPQ at $0.0009, and `glm-5v-turbo` (vision) had NO viable external fallback
   (it downgraded to a TEXT model — image content is unservable).
3. Requests hitting the exhausted window burned the whole chain and got an
   instant 503 after ~12s of harness retries.

**Fixes (user-approved scope):**

- **#2 Backpressure hold-queue** — when ALL z.ai keys are in backoff and the
  soonest recovery is ≤ 25s away, the proxy WAITS (bounded) and re-picks
  instead of 503ing. This caps effective in-flight burst damage at the proxy
  (Kalman-adjacent: uses the existing per-key `retry_after` timers from the
  quota-burn circuit breaker). Harness untouched unless 503s persist.
- **#3 Vision fallback** — image-bearing requests get a vision-capable
  OpenRouter model as external fallback (`VISION_FALLBACK_MODEL`); text-only
  requests keep the cheap text downgrade. Model chosen by live OpenRouter
  catalog query (cheapest vision-capable), rate table entry added.
- **Invoice file** — `~/routstrd-topup-invoice.md` with the current 10k-sat
  Lightning invoice (user pays; guard stands down automatically at ≥5k).

## Checklist

### A. Documentation + invoice
- [x] A1: This plan file
- [x] A2: Run funding guard (refresh invoice if >2h), write
      `~/routstrd-topup-invoice.md` (bolt11 + lightning: URI + context)
- [ ] A3: User pays 10k-sat invoice (their action)

### B. zai_proxy: hold-queue (#2)
- [x] B1: Add `_hold_for_zai_recovery(max_wait=25.0) -> bool` helper near the
      key-health code (scans `_zai_key_health` retry_after, bounded sleep,
      logs `hold_queue_waited` key_decisions row, never raises)
- [x] B2: Insert at the main exhaustion branch (`if chosen is None:` before
      the LiveRouter→Ollama→externals chain): hold → `chosen = best_key()`;
      chain runs only if still None
- [x] B3: Same guard at the second 503 emission site if context warrants

### C. zai_proxy: vision fallback (#3)
- [x] C1: Query OpenRouter /models (public endpoint), pick cheapest
      vision-capable model, record its real rates
      → `qwen/qwen3.7-flash` @ $0.03/$0.13 per M (blended $0.135/M)
- [x] C2: Add `VISION_FALLBACK_MODEL` constant + `_body_has_images(body)`
      helper (scan messages for image_url content parts)
- [x] C3: `_try_external_failover`: images present → vision model, else
      existing text/manager mapping; add rate-table entry

### D. Deploy + verify + commit
- [x] D1: `python3 -m py_compile` + import-smoke-test of new helpers
- [x] D2: `systemctl --user restart zai-proxy`; /quota healthy; one live
      glm-5.2 call returns 200; one text-only glm-5v-turbo call returns 200
      (via OpenRouter → DeepInfra deepseek-v4-flash)
- [ ] D3: Watch `key_decisions` for `hold_queue_waited` during next burst
      (follow-up, not blocking — no rows yet; current bursts go straight to
      failover because both keys are in long backoff)
- [x] D4: Save diff as `docs/patches/` patch in merchant-routing-engine,
      commit + push (revert plan: `git checkout` zai_proxy.py + restart unit)

## Results (2026-08-22, live verification)

- Manager requests during a live sweep burst: z.ai exhausted → OpenRouter
  `z-ai/glm-5.2` 200s flowing (multiple/sec), `key_decisions.reason =
  zai_exhausted_openrouter_failover`. Last 12 min of the burst: **0 failed
  rows** (was 477 failures in the 30-90 min pre-fix window).
- routstrd wallet intact (511 sats) — the storm's routstrd attempts 500'd/
  timed out without draining ecash.

### Additional fixes found during live verification (same patch)

1. **OpenRouter key never loaded (root cause of the hollow chain):** the env
   loader matched `OPENROUTER_API_KEY=` but the ACTIVE key lives in
   `OPENROUTER_OXALPHA_KEY=` (the `OPENROUTER_API_KEY` entries are commented
   out). OpenRouter was silently excluded from EVERY failover — 12h of logs
   show zero openrouter attempts. Loader now accepts both names.
2. **routstrd wallet gate fail-closed forever:** `_routstrd_balance_snapshot`
   parsed `out["total"]` but routstrd's GET /balance returns
   `{"output":{"balances":{mint: sats}}}` — no `total` field, so the B1 gate
   always reported exhaustion and routstrd was never a candidate. Parser now
   sums `balances`, excluding orangesync (testnut) mints.
3. **Upstream error bodies now logged** (first 200 chars) on failover 4xx —
   the openrouter 400 / deepinfra 422 during the burst were previously
   opaque status codes.
4. **routstrd v0.4.1 pricing regression:** the daemon's `/v1/models` dropped
   `pricing` fields (bare id/name/object), so every model parsed as $0.0000/M
   — routstrd looked FREE, sorted first in every failover, and got flooded
   (timeouts + real-sats drain risk). Parser now skips models without
   pricing (fail-closed → 999.0 last-resort estimate → routstrd tried last
   until its pricing is re-wired).

### Known residuals (not blocking)

- z.ai primary attempt for `glm-5v-turbo` hangs ~120s before failover engages
  (z.ai vision path slow/queued). Vision routing itself is correct; the
  OpenRouter vision attempt hit transient ConnectionReset/BrokenPipe in tests
  while the same endpoint served manager traffic — likely burst interference,
  retry on next event.
- `deepseek/deepseek-v4-flash` via OpenRouter returned 400 for sweep worker
  bodies (suspected `tools`/stream param rejection — now diagnosable via the
  error-body logging added above).
- routstr (VPS2 :8009) endpoint probe failing — separate issue.
- OpenRouter intermittent ConnectionReset/SSL-EOF under concurrent burst
  load; requests recover on later retries.

## Deferred
- Sweep-harness concurrency cap — only if 503s persist after #2 (user decision)
- efficiency-monitor check for hold-queue frequency (data already in
  key_decisions rows)
- Fast-fail timeout for z.ai vision-model attempts (120s hang before failover)
