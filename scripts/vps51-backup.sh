#!/usr/bin/env bash
# vps51-backup.sh — off-box restic backup pipeline for VPS2 (23.182.128.51).
#
# Pull-mode over SSH using the operator's key (~/.ssh/id_ed25519). Two cadences,
# auto-selected by weekday (weekly = Sunday, else nightly):
#
#   NIGHTLY:
#     * routstr keys.db x2 (routstr-public + routstr-proxy) via a WAL-safe docker
#       cp sequence: docker cp db+wal+shm to box /tmp, open a read-write copy
#       on-box (replays the WAL, removes -wal/-shm), rsync the single consistent
#       db back. (Pattern adapted from routstr-pnl-collect.py.)
#     * buzz / tollgate / hermes-tenant state dirs -> plain rsync
#     * docker-compose + /etc/caddy config         -> rsync
#     Retention: 14 daily (restic --keep-daily=14).
#
#   WEEKLY (Sunday): everything nightly PLUS
#     * strfry LMDB via mdb_copy ON-BOX FIRST (never copy a live LMDB), then
#       rsync the copy back.
#     * blossom blob dir via incremental rsync.
#     Retention: 8 weekly (restic --keep-weekly=8).
#
# All secrets stay on-box; only data + config cross the wire via SSH. Idempotent.
# The restic repo password comes from $VPS51_RESTIC_PASSWORD_FILE (auto-generated
# with a random value if absent) — that file is the ONLY key to decrypt the repo;
# it must be backed up alongside the repo.
#
# Testable pure helpers are prefixed `_vps51_`; tests source this file without
# running main (see scripts/tests/test_vps51_backup.sh).
set -u

# ----------------------------------------------------------------------------- config
VPS51_REPO="${VPS51_REPO:-$HOME/backups/vps51}"
VPS51_BACKUP_STAGING="${VPS51_BACKUP_STAGING:-$HOME/backups/vps51/.staging}"
VPS51_RESTIC_PASSWORD_FILE="${VPS51_RESTIC_PASSWORD_FILE:-$HOME/backups/vps51.pass}"
VPS51_HOST="${VPS51_HOST:-23.182.128.51}"
VPS51_USER="${VPS51_USER:-debian}"
VPS51_KEY="${VPS51_KEY:-$HOME/.ssh/id_ed25519}"
VPS51_SSH_PORT="${VPS51_SSH_PORT:-22}"
VPS51_KEEP_DAILY="${VPS51_KEEP_DAILY:-14}"
VPS51_KEEP_WEEKLY="${VPS51_KEEP_WEEKLY:-8}"
VPS51_WEEKLY_DOW="${VPS51_WEEKLY_DOW:-0}"   # 0 = Sunday
VPS51_CONNECT_TIMEOUT="${VPS51_CONNECT_TIMEOUT:-10}"

# containers holding keys.db (WAL-safe pull). name:path pairs.
VPS51_SQLITE_JOBS="${VPS51_SQLITE_JOBS:-routstr-public:/app/data/keys.db routstr-proxy:/app/data/keys.db}"
# LMDB trees (weekly, mdb_copy on-box)  -- label:remotepath:localname
VPS51_LMDB_JOBS="${VPS51_LMDB_JOBS:-strfry:/var/lib/strfry:strfry}"
# plain state dirs (nightly + weekly) -- remote:localname
VPS51_STATE_DIRS="${VPS51_STATE_DIRS:-/opt/buzz/state:buzz /opt/tollgate/state:tollgate /opt/hermes-tenants:hermes-tenants}"
# config files to pull every run -- remote:localname
VPS51_CONFIG_PATHS="${VPS51_CONFIG_PATHS:-/opt/routstr/docker-compose.yml:routstr-compose.yml /etc/caddy/Caddyfile:caddy-Caddyfile}"
# blossom blob dir (weekly incremental)
VPS51_BLOSSOM_DIR="${VPS51_BLOSSOM_DIR:-/var/lib/blossom}"

SSH=(ssh -i "$VPS51_KEY" -o StrictHostKeyChecking=no -o "ConnectTimeout=$VPS51_CONNECT_TIMEOUT" \
     -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -p "$VPS51_SSH_PORT")
SSH_TARGET="${VPS51_USER}@${VPS51_HOST}"

# ----------------------------------------------------------------------------- helpers
_weekday() { date +%w; }   # 0=Sunday .. 6=Saturday
_vps51_is_weekly() { [[ "${1:-$(_weekday)}" == "$VPS51_WEEKLY_DOW" ]] && echo 1 || echo 0; }
_vps51_mode() {
    local d="${1:-$(_weekday)}"
    [[ "$(_vps51_is_weekly "$d")" == "1" ]] && echo weekly || echo nightly
}

_vps51_retention_args() {  # $1 keep-daily  $2 keep-weekly
    echo "--keep-daily=$1 --keep-weekly=$2"
}

_ssh() { "${SSH[@]}" "$SSH_TARGET" "$1"; }

_vps51_host_reachable() { _ssh "true" >/dev/null 2>&1; }

_vps51_ensure_password() {
    local f="$VPS51_RESTIC_PASSWORD_FILE"
    if [[ ! -s "$f" ]]; then
        mkdir -p "$(dirname "$f")"
        if [[ -n "${VPS51_RESTIC_PASSWORD:-}" ]]; then
            printf '%s\n' "$VPS51_RESTIC_PASSWORD" > "$f"
        else
            < /dev/urandom tr -dc 'A-Za-z0-9' | head -c 48 > "$f" 2>/dev/null || return 2
        fi
        chmod 600 "$f" 2>/dev/null || true
        echo "[vps51-backup] generated restic password file: $f" >&2
        echo "[vps51-backup]   BACK THIS UP — it is the only key to decrypt the repo." >&2
    fi
    printf '%s\n' "$f"
}

_restic_run() {  # run restic with the repo + password env; args passed through
    local pwf; pwf="$(_vps51_ensure_password)" || return 2
    RESTIC_REPOSITORY="$VPS51_REPO" RESTIC_PASSWORD_FILE="$pwf" restic "$@"
}

_vps51_probe_repo() { _restic_run cat config >/dev/null 2>&1; }

_vps51_init_repo() { _restic_run init >/dev/null 2>&1; }

# --- WAL-safe pull of a container sqlite db ------------------------------------
# docker cp db+wal+shm on-box → open read-write copy (replays WAL, removes the
# stale -wal/-shm) → rsync single consistent db back.
_vps51_pull_sqlite_container() {  # $1 container  $2 remote db path
    local cont="$1" rpath="$2" name
    name="$(basename "$rpath")"
    local rtmp="/tmp/vps51-wal-$cont-$$"
    local onbox
    onbox="mkdir -p $rtmp && \
        (docker cp $cont:$rpath $rtmp/$name || docker cp $cont:/app/$name $rtmp/$name || exit 1) && \
        (docker cp $cont:$rpath-wal $rtmp/$name-wal 2>/dev/null || true) && \
        (docker cp $cont:$rpath-shm $rtmp/$name-shm 2>/dev/null || true) && \
        python3 -c \"
import sqlite3,sys,os
src=sys.argv[1]
con=sqlite3.connect(src)
con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
con.close()
for suf in ('-wal','-shm'):
    p=src+suf
    if os.path.exists(p): os.remove(p)
\" $rtmp/$name && \
        cp $rtmp/$name $rtmp/$name.clean && echo WALDONE"
    if ! _ssh "$onbox" >/dev/null 2>&1; then
        echo "[warn] sqlite pull failed for $cont:$rpath" >&2
        return 1
    fi
    mkdir -p "$VPS51_BACKUP_STAGING/$cont"
    rsync -az -e "${SSH[*]}" "$SSH_TARGET:$rtmp/${name}.clean" \
        "$VPS51_BACKUP_STAGING/$cont/keys.db" 2>/dev/null
    local rc=$?
    _ssh "rm -rf $rtmp" >/dev/null 2>&1
    return $rc
}

# --- LMDB: mdb_copy ON-BOX first, then rsync the copy back ---------------------
_vps51_pull_lmdb_dir() {  # $1 label  $2 remote lmdb dir  $3 local name
    local label="$1" rdir="$2" lname="$3"
    local rtmp="/tmp/vps51-lmdb-$label-$$"
    if _ssh "rm -rf $rtmp && mdb_copy $rdir $rtmp && touch $rtmp.done && echo MDB-DONE" >/dev/null 2>&1; then
        mkdir -p "$VPS51_BACKUP_STAGING/$lname"
        rsync -az --delete -e "${SSH[*]}" "$SSH_TARGET:$rtmp/" \
            "$VPS51_BACKUP_STAGING/$lname/" 2>/dev/null
        local rc=$?
        _ssh "rm -rf $rtmp $rtmp.done" >/dev/null 2>&1
        return $rc
    fi
    echo "[warn] LMDB mdb_copy failed for $label" >&2
    return 1
}

_vps51_pull_state_dir() {  # $1 remote dir  $2 local name
    local rdir="$1" lname="$2"
    mkdir -p "$VPS51_BACKUP_STAGING/$lname"
    rsync -az --delete -e "${SSH[*]}" "$SSH_TARGET:$rdir/" \
        "$VPS51_BACKUP_STAGING/$lname/" 2>/dev/null
    return $?
}

_vps51_pull_config() {  # $1 remote file/dir  $2 local name
    local rpath="$1" lname="$2"
    mkdir -p "$VPS51_BACKUP_STAGING/config"
    rsync -az -e "${SSH[*]}" "$SSH_TARGET:$rpath" \
        "$VPS51_BACKUP_STAGING/config/$lname" 2>/dev/null
    return $?
}

_vps51_restic_backup() {  # $1 source dir  $2 tag
    local src="$1" tag="$2" args
    if [[ "$tag" == "weekly" ]]; then
        args="$(_vps51_retention_args "$VPS51_KEEP_DAILY" "$VPS51_KEEP_WEEKLY")"
    else
        args="--keep-daily=$VPS51_KEEP_DAILY"
    fi
    # shellcheck disable=SC2086  # $args intentionally carries multiple retention flags
    _restic_run backup "$src" --tag "$tag" $args >/dev/null 2>&1 || return $?
    # shellcheck disable=SC2086
    _restic_run forget --tag "$tag" $args --prune >/dev/null 2>&1
    _restic_run snapshots --tag "$tag" >/dev/null 2>&1
    return 0
}

_vps51_summary() {  # $1 mode $2 snapshots $3 bytes_kb $4 host $5 items
    echo "[vps51-backup] mode=$1 host=$4 snapshots=$2 staged_kb=$3 items=$5"
}

# ----------------------------------------------------------------------------- main (guarded)
if [[ "$(basename "${BASH_SOURCE[0]}")" == "vps51-backup.sh" ]] && [[ "${VPS51_SOURCE_ONLY:-0}" != "1" ]]; then

    MODE="$(_vps51_mode)"
    echo "[vps51-backup] mode=$MODE host=$VPS51_HOST repo=$VPS51_REPO"
    _vps51_ensure_password || { echo "ERROR: cannot create restic password file"; exit 2; }

    if ! _vps51_host_reachable; then
        echo "[vps51-backup] ERROR: $SSH_TARGET unreachable — aborting (box likely dark)."
        exit 2
    fi

    mkdir -p "$VPS51_REPO" "$VPS51_BACKUP_STAGING"
    if ! _vps51_probe_repo; then
        echo "[vps51-backup] initialising restic repo"
        _vps51_init_repo || { echo "ERROR: restic init failed"; exit 2; }
    fi

    n_items=0
    # sqlite keys.db containers (always)
    for job in $VPS51_SQLITE_JOBS; do
        c="${job%%:*}"; p="${job##*:}"
        _vps51_pull_sqlite_container "$c" "$p"; n_items=$((n_items+1))
    done
    # state dirs (always)
    for job in $VPS51_STATE_DIRS; do
        rdir="${job%%:*}"; lname="${job##*:}"
        _vps51_pull_state_dir "$rdir" "$lname"; n_items=$((n_items+1))
    done
    # config files (always)
    for job in $VPS51_CONFIG_PATHS; do
        rpath="${job%%:*}"; lname="${job##*:}"
        _vps51_pull_config "$rpath" "$lname"; n_items=$((n_items+1))
    done

    if [[ "$MODE" == "weekly" ]]; then
        # LMDB via mdb_copy on-box (weekly)
        for job in $VPS51_LMDB_JOBS; do
            label="${job%%:*}"; rest="${job#*:}"; rdir="${rest%%:*}"; lname="${rest##*:}"
            _vps51_pull_lmdb_dir "$label" "$rdir" "$lname"; n_items=$((n_items+1))
        done
        # blossom blob incremental (weekly)
        _vps51_pull_state_dir "$VPS51_BLOSSOM_DIR" blossom; n_items=$((n_items+1))
    fi

    if _vps51_restic_backup "$VPS51_BACKUP_STAGING" "$MODE"; then
        nsnap=$(_restic_run snapshots --tag "$MODE" 2>/dev/null | grep -cE '^[a-f0-9]{8,}')
        du_kb=$(du -sk "$VPS51_BACKUP_STAGING" 2>/dev/null | awk '{print $1}')
        _vps51_summary "$MODE" "${nsnap:-0}" "${du_kb:-0}" "$VPS51_HOST" "$n_items"
        echo "[vps51-backup] OK — snapshot tagged $MODE created."
        exit 0
    else
        echo "[vps51-backup] ERROR: restic backup failed."
        exit 2
    fi
fi
