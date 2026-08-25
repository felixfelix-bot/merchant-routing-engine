# ADR-012: Runtime & Learned State — Daily Snapshot History Repo, Not the Main Repo

- **Status:** Accepted (2026-08-25)
- **Context:** `~/.hermes/bot` continuously writes mutable runtime/learned JSON + an append-only log, producing a permanently-dirty `git status`. Some state (learned Kalman priors/tuning, curated synergy map) is worth versioning; the rest is ephemeral status/cache. We rejected `assume-unchanged` (fragile, loses deltas).
- **Decision:** Untrack + `.gitignore` mutable runtime state and `*.log` from the main/`bot` repos. Create a separate private repo `~/.hermes/state-history/` that snapshots ONLY valuable learned state on a content-change (hash) basis via `state-snapshot.sh`, run **daily** by cron/systemd.
  - Snapshot: `kalman_price_state.json`, `kalman_tuning.json`, `synergy_map.json`, optionally `session_registry.json` (weekly).
  - Gitignore only: `zai_state.json`, `cashu_state.json`, `vision_health.json`, `completion_watch_state.json`, `btc_usd_cache.json`, `api_burn_collector.log`.
  - Keep tracked: `handovers/`, `docs/`.
- **Consequences:**
  - (+) Main repo `git status` reflects only intentional changes.
  - (+) Learned Kalman/tuning evolution becomes queryable git history (meaningful deltas).
  - (-) Learned state lives outside the code repo; restoring a prior requires pulling from `state-history`.