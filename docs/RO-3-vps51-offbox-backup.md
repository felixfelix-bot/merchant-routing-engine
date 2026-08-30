# RO-3 — Off-box backup pipeline for VPS2 (23.182.128.51)

Status: **Phase A complete (authored + locally tested)** — Phase B deferred pending box recovery.
Owner: worker-dq05 (task `t_2ebb70c3`). Urgency: SOON (production outage recovery).

## Why this exists

Incident 2026-08-28/30: the whole `23.182.128.0/24` went dark (AS401933 / Hara
Partners Systems withdrew all three prefixes from BGP between Aug 28 21:30Z and
Aug 29 08:00Z; RIPEstat 1/327 peers). VPS2 = `23.182.128.51` holds **all**
production: routstr-public + routstr-proxy containers, strfry relays, blossom,
buzz, tollgate, caddy TLS, and three Hermes tenants.

The only off-box copy was `routstr/keys.db` via the daily P&L collector (last
known Aug 28). The Syncthing off-box mirrors were found dead since May 20
(empty husks). This pipeline replaces that with a real, scripted restic backup
plus a restore/integrity test.

## Deliverables (Phase A — commit `t_2ebb70c3`)

| File | Purpose |
|---|---|
| `scripts/vps51-backup.sh` | Off-box restic backup pipeline (nightly + weekly, auto-selected by weekday). |
| `scripts/vps51-restore-test.sh` | Scripted restore → `/tmp` + `PRAGMA integrity_check` + row-count assertion. |
| `scripts/tests/test_vps51_backup.sh` | Test harness (fixture/fake-tools, 18 asserts). TDD Gate 1. |
| `scripts/tests/test_vps51_restore.sh` | Restore/integrity/row-count harness (4 asserts). TDD Gate 1. |
| `docs/RO-3-vps51-offbox-backup.md` | This document. |

## Script behaviour

`scripts/vps51-backup.sh` runs in **pull mode** over SSH
(`debian@23.182.128.51`, key `~/.ssh/id_ed25519`), staging data under
`~/backups/vps51/.staging` and snapshotting into restic repo `~/backups/vps51`.

Mode auto-selected by weekday (Sunday → `weekly`, else `nightly`):

- **Nightly** (all runs, kept 14 daily):
  - `keys.db` from each of `routstr-public`, `routstr-proxy` — **WAL-safe**
    pull: `docker cp` the `db` + `-wal` + `-shm` to box `/tmp`, open a
    read-write copy that replays/`wal_checkpoint(TRUNCATE)` the WAL and removes
    the stale `-wal`/`-shm`, then rsync the single consistent `.clean` db back.
    (Pattern adapted from `routstr-pnl-collect.py`.)
  - state dirs: buzz, tollgate, Hermes tenants → plain `rsync --delete`.
  - config: routstr `docker-compose.yml`, caddy `Caddyfile` → `rsync`.
- **Weekly** (Sunday, kept 8 weekly) — everything nightly **plus**:
  - **strfry LMDB** via `mdb_copy` **on-box first** (never copy a live LMDB),
    then rsync the copy back.
  - **blossom** blob directory via incremental `rsync --delete`.

Retention: `_vps51_restic_backup` issues `restic backup` + `restic forget
--prune` with `--keep-daily=14` (nightly) or `--keep-daily=14 --keep-weekly=8`
(weekly).

### restic repo password

Stored in `$VPS51_RESTIC_PASSWORD_FILE` (default `~/backups/vps51.pass`, mode
`0600`), auto-generated as 48 random alphanumeric chars if absent. **This file
is the only key to decrypt the repo — it must be backed up alongside it.**
Overridable via `VPS51_RESTIC_PASSWORD`.

### Configuration knobs (all overridable via env)

`VPS51_REPO`, `VPS51_BACKUP_STAGING`, `VPS51_HOST`, `VPS51_USER`, `VPS51_KEY`,
`VPS51_SSH_PORT`, `VPS51_KEEP_DAILY`, `VPS51_KEEP_WEEKLY`, `VPS51_WEEKLY_DOW`,
`VPS51_CONNECT_TIMEOUT`, `VPS51_SQLITE_JOBS`, `VPS51_LMDB_JOBS`,
`VPS51_STATE_DIRS`, `VPS51_CONFIG_PATHS`, `VPS51_BLOSSOM_DIR`.

## `scripts/vps51-restore-test.sh`

Restores the latest snapshot from the restic repo into a temp dir under `/tmp`,
finds `keys.db`, runs `PRAGMA integrity_check`, and compares table row-counts
against a reference (manifest `$VPS51_REPO/latest-counts.txt`, or the source db
in fixture mode). Exit 0 only when integrity is `ok` **and** counts match.

- Production: `bash scripts/vps51-restore-test.sh`
- Fixture/selfcheck (used by the test harness): supply `--selfcheck-target` and
  `--selfcheck-source`.

### Tested tables
`api_keys upstream_providers cashu_transactions routstr_fees` (overridable via
`VPS51_ASSERT_TABLES`).

## Test evidence (Phase A)

- **Gate 1 (TDD RED→GREEN):** both harnesses returned `exit 2` with
  `RED-GATE-1: <script> does not exist yet` BEFORE implementation; both returned
  `PASS, FAIL=0` after. Backup harness `PASS=18 FAIL=0`; restore harness
  `PASS=4 FAIL=0`.
- **Gate 2 (task suite):** both bash harnesses pass (22 asserts, zero failures).
- Repo-wide pytest: `2419 passed, 143 failed`. The 143 failures are **pre-existing
  and unrelated** — proven by moving all RO-3 files out of the tree and re-running
  a sample (`test_telnyx_provider`, `test_tier_cleanup`), which still fail
  identically. RO-3 adds only new untracked `scripts/` + `docs/` files; no tracked
  source/test is modified and nothing is imported by pytest.

## Phase B (blocked — box dark)

Confirmed dark at authoring time (`nc -zw3 23.182.128.51 22` → exit 1, 100% packet
loss, `vps51-watchdog.state` status `dark`). Phase B (deploy cron, first real
backup, real restore run) is blocked by the RO-3 GATE until `nc -zw3
23.182.128.51 22` succeeds **and** RO-2 completes.

### Proposed cron schedule (manager to register)

- `vps51-backup.sh` nightly: **03:30 UTC** (staggered vs P&L collector 09:00).
- `vps51-backup.sh` weekly: **Sunday 03:30 UTC** (same schedule; script
  auto-selects `weekly` on Sundays).
- `vps51-restore-test.sh`: **Sunday 04:30 UTC**, to validate the weekly snapshot
  (or daily, if preferred).

## Notes / caveats

- Disk on DQ05 (`/dev/nvme0n1p2`) currently shows ~47G available (task context
  said 212G, but the live `df` on 2026-08-30 is 79% used). Blossom + strfry LMDB
  sizes are unknown while the box is dark; the manager should confirm capacity
  before the first weekly run and consider pruning older snapshots if LMDB/blossom
  are large.
- The backup never leaves `keys.db` WAL/shm un-consistent at rest: on-box
  checkpoint + `.clean` copy means the restored db is always a single valid file.
- Secrets never cross the wire; only data + config over SSH.
