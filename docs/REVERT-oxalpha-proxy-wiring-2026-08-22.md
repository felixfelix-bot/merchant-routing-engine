# REVERT — oxalpha proxy wiring (OX-2, 2026-08-22)

Task t_2ed46556 · docs/PLAN-oxalpha-promo-2026-08-21.md §5 (OX-2) ·
EMERGENCY directive 2026-08-22 ~16:00 · acceleration plan §4/§5.

## What changed in PRODUCTION (~/.hermes/bot)

Two files, both additive:

1. `~/.hermes/bot/config/providers.yaml`
   - NEW top-level `oxalpha:` block (mirrors repo fixture incl. the OX-2
     `failover:` + `preferred_for:` sub-blocks; `failover.enabled: true`
     per EMERGENCY, `preferred_for.enabled: false` until OX-3b).
   - NEW `strategy.model_map.oxalpha` cells (`vision`, `bulk_summarize`
     → `stealth/ox-alpha`) — opt-in only; no generic cells.
   - Nothing else touched (zai/external/strategy rows byte-identical).

2. `~/.hermes/bot/zai_proxy.py`
   - import of repo `src.oxalpha_tier` (via the existing `_MRE_PATH`
     sys.path hook), guarded — import failure ⇒ tier absent ⇒ fail-closed.
   - `_load_external_keys()`: reads `OPENROUTER_OXALPHA_KEY` into
     `keys["oxalpha"]` (first-wins order unchanged; the 2026-08-22 15:50
     OXALPHA→openrouter stopgap is preserved verbatim — see notes).
   - `_try_external_failover()`: when `_OXALPHA_TIER.failover_eligible()`,
     oxalpha is attempted FIRST (after z.ai keys — the only way this
     function is reached — and before every paid candidate). Single
     attempt, `timeout=90`, forced `model=stealth/ox-alpha`,
     `reasoning_effort=low`, `max_completion_tokens≤8192`. On 429: ALL
     `x-ratelimit-*`/`retry-after` headers logged, 60/120/300 s backoff
     via `note_429()`; 402 → guard disable + fall-through; timeout/5xx →
     `note_failure()` (breaker at 5 / 300 s). ANY error falls through to
     the existing paid chain — zero regression. On success: usage.cost fed
     to `observe_response_cost()` (any cost>0 ⇒ tier killed + anomaly
     row), spend logged at actual cost, 429-never-bubbles preserved
     (terminal 503 path unchanged).
   - alias pre-chain hook in `_proxy()` (armed, inert: config
     `preferred_for.enabled: false`) + 5-min `/api/v1/key` usage-delta
     poller daemon (kill on cumulative-usage INCREASE; first sample is a
     baseline, never a kill; state in `~/.hermes/bot/.oxalpha_usage_state.json`).
   - Committed in the bot repo as TWO commits: (a) the pre-existing
     uncommitted 15:50 stopgap isolated for provenance, (b) the OX-2
     wiring — so `git revert` of (b) returns the proxy to the exact
     pre-OX-2 working-tree state.

## Key handling (fail-closed by design)

- `OPENROUTER_OXALPHA_KEY` absent / placeholder / invalid ⇒ tier never
  attempted (401 also just falls through). No probe of key material was
  performed by OX-2.
- No OpenRouter provisioning API exists on this host (only
  `OPENROUTER_OXALPHA_KEY` in `~/.hermes/.env`). Creating/verifying the
  $0-spend-limit key is MANUAL: dashboard → Keys → create with
  limit $0 → paste into `~/.hermes/.env:514` → `systemctl --user restart
  zai-proxy`. Until then the wiring is live but inert (fail-closed).

## Revert steps (production)

1. `git -C ~/.hermes/bot revert <OX-2 wiring commit>`  (zai_proxy.py +
   providers.yaml both restored; the isolated stopgap commit stays)
2. `systemctl --user restart zai-proxy`
3. verify: `journalctl --user -u zai-proxy -n 50 | grep -i oxalpha`
   → no oxalpha lines after restart.

Full tier removal: also delete the `oxalpha:` block + model_map cells and
restart (plan §6).

## Scope guard rails honored

- EXTERNAL_PROVIDERS dict, LiveRouter candidates, price sort, Kalman: NOT
  touched — oxalpha is a guarded first-position candidate in the failover
  attempt order only. (The acceleration plan's "never in
  EXTERNAL_PROVIDERS" invariant is preserved for the dict/alias surface;
  the EMERGENCY directive governs the failover attempt order.)
- zai routing/model_map cells byte-identical (regression test
  `test_model_map_additions_are_scoped_to_oxalpha_only`).
- glm-5.2/glm-5.3 hard-excluded from the alias in code (§5.2).
- Repo-side: 31/31 contract tests green; 32 pre-existing telnyx/CG-surface
  failures reproduce identically with OX-2 changes removed (proven via
  snapshot run) — not introduced by OX-2.

## Open items (NOT done by OX-2)

- Manual $0-limit key provisioning (Felix) — see above.
- OX-3b: rung-1 flip (`preferred_for.enabled: true` + digester lanes).
- Felix D7 review of the alias/opt-in surface.
