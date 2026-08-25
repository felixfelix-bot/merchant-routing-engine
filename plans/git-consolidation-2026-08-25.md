# Plan: Git Consolidation + Runtime-State History — merchant-routing-engine

**Date:** 2026-08-25
**Status:** DONE
**Scope:** `~/merchant-routing-engine` (primary), `~/.hermes/bot` (import reconcile + state untracking), `~/.hermes/state-history` (new), remote `github`

## Context / Why

- `main` and `master` DIVERGED; `master` is the GitHub default but `main` holds the live forward work (live-router, tollgate, tier-cleanup, 41 CG-2/CG-3 commits).
- Working tree: 5 modified `src/*.py` + 6 untracked items.
- One stash `other-workers-inflight`; only the CG-3 `realtime_pricing` amortization module (plus small deltas) is unique — must be committed.

## Locked decisions

- [x] Q1: seed prices non-contentious. Commit WORKTREE `real_price_tracker.py` (amortized `ours:0.03`/`friend:0.015`). `$0.001` live routing floor is a separate flat_router path, untouched.
- [x] Q2: commit `README.md`+`SCHEMA.sql` to git; publish 14.9 MB `scrubbed.db.gz` as a GitHub **Release asset**.
- [x] Stash: commit the `realtime_pricing` amortization module.
- [x] Default branch -> `main`; **delete `master` AFTER unifying, with a `backup/master-pre-delete-20260825` branch pushed to remote first.**
- [x] `state-history` snapshot cadence: **daily** (cron/systemd).

---

## Phase 0 — Safety checkpoints

- [x] Tag `backup/pre-consolidate-20260825` on engine @ `c2c0d86` and on `~/.hermes/bot`; push engine tag.
- [x] Push `backup/master-pre-delete-20260825` branch (copy of master @ `79d2e45`) to `github` BEFORE any master deletion.
- [x] Confirm wt branch mirrored on `github/wt/...` (it is: `c2c0d86`).

## Phase 1 — Reconcile prod<->engine import prefix

- [x] Normalize engine `src/*.py` bare imports -> `src.`-prefixed.
- [x] Re-run 3-way diff (HEAD/bot/worktree) for the 5 files; confirm remaining deltas are semantic ONLY.
- [x] Verify `$0.001` flat_router floor unaffected by `real_price_tracker` work.

## Phase 2 — Commit working-tree WIP on wt branch

- [x] (a) `src/balance_collectors.py` — NeuralWatt expansion.
- [x] (b) `src/live_router.py` + `src/primary_router.py` — provider-set expansion.
- [x] (c) `src/real_price_tracker.py` — amortized seed refactor.
- [x] (d) `src/shadow_hook.py` — add `neuralwatt`/`opencode_go`/`ollama_cloud_2`; keep WORKTREE canonical (no import of bot drift into seeds).
- [x] (e) 3 docs + `eval/ox3a/_inspect_campaign.py` + `scripts/_rebuild_fixtures.py`.
- [x] `.gitignore`: `eval/ox3a/results/` (untrack).

## Phase 3 — Stash: extract + drop

- [x] `git stash branch`/apply to scratch off wt tip.
- [x] Skip no-ops (identical to HEAD): `token_predictor.py`, `seed_token_stats.py`, `test_token_predictor.py`.
- [x] **Commit unique `realtime_pricing.py` CG-3 amortization module** + `providers.yaml` delta.
- [x] Decide `routstrd_funding_guard.py` (44) / `ox3a_build_fixtures.py` (11): commit if referenced, else drop with stash.
- [x] `git stash drop` after above lands.

## Phase 4 — Merge branches into `main`

- [x] `wt/glm53-quota-cleanup-t_da1b7c10` -> `main`.
- [x] `feature/ecash-issuance-and-onboarding-doc` -> `main`.
- [x] `converged-rate-replay`: manual port of `aa1c14c` `_fetch_telnyx_balance_api()` (GET `https://api.telnyx.com/v2/balance`) into current `balance_collectors.py` + add its test as new-file commit (no blind cherry-pick).
- [x] Cherry-pick 1 doc each from `range-tests` + `review/phase2-findings`.

## Phase 5 — master -> main + telemetry binary

- [x] Bring master's friend-handover doc (`79d2e45`) into `main`.
- [x] Commit ONLY `datasets/routing-telemetry/README.md` + `SCHEMA.sql` to `main`.
- [x] Create GitHub Release `routing-telemetry-2026-08-22`; upload `scrubbed.db.gz`; link URL in README.

## Phase 6 — Unify default + prune

- [x] Set GitHub default -> `main`.
- [x] Merge `main` -> `master`, push; confirm `backup/master-pre-delete-20260825` exists on remote.
- [x] Delete `master` (local + `github/master`).
- [x] Delete `fix/live-router-none-on-failover`, `wt/shadow-drop-ours`; delete `range-tests`/`review/phase2-findings` after docs picked.
- [x] Push `main`; `git remote prune github`.

## Phase 7 — Runtime-state history repo (daily)

- [x] Untrack + `.gitignore` from `~/.hermes/bot`: `zai_state.json`, `cashu_state.json`, `vision_health.json`, `completion_watch_state.json`, `btc_usd_cache.json`, `api_burn_collector.log`. KEEP `handovers/`, `docs/` tracked.
- [x] Create `~/.hermes/state-history/` private git repo.
- [x] Write `state-snapshot.sh`: content-change (hash) check; snapshot `kalman_price_state.json`, `kalman_tuning.json`, `synergy_map.json`, `session_registry.json` (weekly variant ok).
- [x] Wire into cron (daily) or systemd timer.

## Verification gate

- [x] `git status` clean both repos; `git ls-files | grep node_modules` = 0.
- [x] No stash; no 0-ahead dead branches.
- [x] GitHub default = `main`; fresh clone resolves divergence.
- [x] zai-proxy serves; spot-check one `/v1/chat/completions`.

## Rollback

- Tag `backup/pre-consolidate-20260825` (both repos) + `backup/master-pre-delete-20260825` (engine, remote); stash retained until Phase 3 confirmed; wt branch on remote.