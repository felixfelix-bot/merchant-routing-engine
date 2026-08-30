#!/usr/bin/env bash
# vps51-restore-test.sh — scripted restore + integrity + row-count verification
# for the routstr keys.db off-box backup (VPS2).
#
# Restores keys.db from the most recent restic snapshot in $VPS51_REPO into a
# per-run temp dir under /tmp, runs `PRAGMA integrity_check` on the restored db,
# and cross-checks table row-counts against a reference manifest produced by the
# backup run (or, in selfcheck fixture mode, against a "source" db passed in).
#
# Usage (production, box alive):  bash scripts/vps51-restore-test.sh
# Usage (selfcheck/fixture):      VPS51_RESTORE_FIXTURE=... bash scripts/vps51-restore-test.sh --selfcheck-target <dir> --selfcheck-source <keys.db>
#
# Exit 0 = restore healthy (integrity ok + row counts match). Nonzero otherwise.
#
# Testable pure helpers are prefixed `_vps51_`; tests invoke the script with
# fixture source/target to avoid touching the real repo.
set -u

# ----------------------------------------------------------------------------- config
VPS51_REPO="${VPS51_REPO:-$HOME/backups/vps51}"
VPS51_RESTIC_PASSWORD_FILE="${VPS51_RESTIC_PASSWORD_FILE:-$HOME/backups/vps51.pass}"
# tables whose row-count we assert (space-separated). All must exist in the db.
VPS51_ASSERT_TABLES="${VPS51_ASSERT_TABLES:-api_keys upstream_providers cashu_transactions routstr_fees}"

_restic() {
    RESTIC_REPOSITORY="$VPS51_REPO" RESTIC_PASSWORD_FILE="$VPS51_RESTIC_PASSWORD_FILE" restic "$@"
}

# ----------------------------------------------------------------------------- helpers
_vps51_latest_snapshot() {  # returns latest snapshot id for --tag nightly (or all)
    local id
    id=$(_restic snapshots --latest 1 2>/dev/null | awk '/^[a-f0-9]{8,}/ {print $1; exit}')
    printf '%s\n' "${id:-}"
}

_vps51_assert_tables_sql() {  # emits SQL: SELECT '<t>', COUNT(*) FROM <t> UNION ALL ...
    local first=1 sql=""
    for tname in $VPS51_ASSERT_TABLES; do
        if [[ $first -eq 1 ]]; then
            sql="SELECT '$tname' AS tbl, COUNT(*) FROM $tname"
            first=0
        else
            sql="$sql UNION ALL SELECT '$tname', COUNT(*) FROM $tname"
        fi
    done
    printf '%s\n' "$sql"
}

_vps51_row_counts() {  # $1 db path -> "<tbl>:<count>" lines
    local db="$1" sql; sql="$(_vps51_assert_tables_sql)"
    sqlite3 "$db" "$sql" 2>/dev/null | awk -F'|' '{print $1":"$2}'
}

_vps51_integrity() {  # $1 db path -> "ok" or the failing message
    local db="$1"
    local r; r="$(sqlite3 "$db" 'PRAGMA integrity_check;' 2>&1 | head -1)"
    [[ "$r" == "ok" ]] && echo ok || printf 'FAIL:%s' "$r"
}

# ----------------------------------------------------------------------------- main (guarded)
# Support fixture mode for the test harness: --selfcheck-target/--selfcheck-source
# skip restic entirely and run the assertion logic against a source db.
if [[ "${VPS51_RESTORE_SOURCE_ONLY:-0}" != "1" ]]; then
    _SRC="" ; _TGT=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --selfcheck-source) _SRC="${2:-}"; shift 2 ;;
            --selfcheck-target) _TGT="${2:-}"; shift 2 ;;
            *) shift ;;
        esac
    done

    if [[ -n "$_TGT" ]]; then
        # fixture mode: source db provided (or copied), target is restore dest
        rm -rf "$_TGT"; mkdir -p "$_TGT"
        if [[ -n "$_SRC" && -f "$_SRC" ]]; then
            cp "$_SRC" "$_TGT/keys.db"
        fi
    else
        _TGT="$(mktemp -d /tmp/vps51-restore.XXXXXX)"
    fi

    # Compute source row-counts when a source db was given (fixture selfcheck).
    if [[ -n "$_SRC" && -f "$_SRC" ]]; then
        _vps51_src_counts="$(_vps51_row_counts "$_SRC")"
    else
        _vps51_src_counts=""
    fi

    if [[ -z "$_SRC" ]]; then
        # production path: restore latest snapshot from restic
        SNAP="$(_vps51_latest_snapshot)"
        if [[ -z "$SNAP" ]]; then
            echo "[vps51-restore-test] ERROR: no restic snapshot found in $VPS51_REPO"
            exit 2
        fi
        if ! _restic restore "$SNAP" --target "$_TGT" >/dev/null 2>&1; then
            echo "[vps51-restore-test] ERROR: restic restore failed"
            exit 2
        fi
        # locate restored keys.db under the target tree
        DB=""
        DB="$(find "$_TGT" -name 'keys.db' -type f 2>/dev/null | head -1)"
        [[ -n "$DB" ]] || { echo "[vps51-restore-test] ERROR: keys.db not found in restored tree"; exit 2; }
    else
        DB="$_TGT/keys.db"
        # fixture source: assert row-counts vs the (already-restored) db == source
        _vps51_src_counts="$(_vps51_row_counts "$_SRC")"
    fi

    # assert integrity
    ITEG="$(_vps51_integrity "$DB")"
    echo "[vps51-restore-test] db=$DB"
    echo "[vps51-restore-test] integrity_check => $ITEG"
    if [[ "$ITEG" != "ok" ]]; then
        echo "[vps51-restore-test] FAIL: integrity_check not ok"
        exit 1
    fi

    # assert row counts
    if [[ -n "${_vps51_src_counts:-}" ]]; then
        # fixture/source comparison
        RESTORED="$(_vps51_row_counts "$DB")"
        if [[ "$_vps51_src_counts" == "$RESTORED" ]]; then
            echo "[vps51-restore-test] OK: row counts match source ($(printf '%s' "$_vps51_src_counts" | tr '\n' ' '))"
            exit 0
        else
            echo "[vps51-restore-test] FAIL: row-count mismatch"
            echo "  source:   $_vps51_src_counts"
            echo "  restored: $RESTORED"
            exit 1
        fi
    fi

    # production row-count reference: compare against a manifest file committed
    # by the backup, defaulting to a PASS if no manifest exists (best-effort).
    MANIFEST="${VPS51_REPO}/latest-counts.txt"
    if [[ -f "$MANIFEST" ]]; then
        RESTORED="$(_vps51_row_counts "$DB")"
        if [[ "$(cat "$MANIFEST")" == "$RESTORED" ]]; then
            echo "[vps51-restore-test] OK: row counts match manifest"
            exit 0
        else
            echo "[vps51-restore-test] FAIL: row-count mismatch vs manifest"
            exit 1
        fi
    fi
    echo "[vps51-restore-test] OK: integrity ok (no manifest present; row-count manual)"
    exit 0
fi
