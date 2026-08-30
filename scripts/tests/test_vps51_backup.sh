#!/usr/bin/env bash
# test_vps51_backup.sh — test harness for scripts/vps51-backup.sh
#
# Gate 1 (TDD) harness for the off-box backup pipeline. Sources vps51-backup.sh
# (which must expose pure functions and a guarded main) and asserts on its
# behaviour under FAKE ssh/rsync/restic/mdb_copy binaries injected via PATH,
# so the test runs 100% locally with zero network / zero on-box access.
#
# Run: bash scripts/tests/test_vps51_backup.sh
# Exit 0 = all assertions pass; nonzero = at least one failed.

set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="$REPO/scripts/vps51-backup.sh"
PASS=0
FAIL=0

t() { # t NAME EXPRESSION
    local name="$1" got="$2" want="$3"
    if [[ "$got" == "$want" ]]; then
        PASS=$((PASS+1)); echo "  ok  $name"
    else
        FAIL=$((FAIL+1)); echo "FAIL  $name"
        echo "      want: $want"
        echo "      got:  $got"
    fi
}

# ----- build a fake-tools root -------------------------------------------------
FAKE_DIR="$(mktemp -d /tmp/vps51-fake.XXXXXX)"
trap 'rm -rf "$FAKE_DIR"' EXIT
mkdir -p "$FAKE_DIR/bin"
export PATH="$FAKE_DIR/bin:$PATH"

# A fake `ssh` that logs its remote command(s) to a file. The LAST arg of
# ssh is the remote command string. Remote commands may contain spaces.
cat > "$FAKE_DIR/bin/ssh" <<'FAKESSH'
#!/usr/bin/env bash
{
  printf 'ssh'; printf ' <%s>' "$@"
  printf '\n'
} >> "${VPS51_FAKE_LOG:-/dev/null}"
# Simulate the remote docker cp existing: nothing to do for fake.
exit 0
FAKESSH
chmod +x "$FAKE_DIR/bin/ssh"

cat > "$FAKE_DIR/bin/rsync" <<'FAKERSYNC'
#!/usr/bin/env bash
{ printf 'rsync'; printf ' <%s>' "$@"; printf '\n'; } >> "${VPS51_FAKE_LOG:-/dev/null}"
exit 0
FAKERSYNC
chmod +x "$FAKE_DIR/bin/rsync"

# fake restic: record calls; honour init subcommand implicitly.
cat > "$FAKE_DIR/bin/restic" <<'FAKERESTIC'
#!/usr/bin/env bash
{ printf 'restic'; printf ' <%s>' "$@"; printf '\n'; } >> "${VPS51_FAKE_LOG:-/dev/null}"
if [[ "$1" == "cat" ]]; then
  exit 0
fi
exit 0
FAKERESTIC
chmod +x "$FAKE_DIR/bin/restic"

cat > "$FAKE_DIR/bin/mdb_copy" <<'FAKEMDB'
#!/usr/bin/env bash
{ printf 'mdb_copy'; printf ' <%s>' "$@"; printf '\n'; } >> "${VPS51_FAKE_LOG:-/dev/null}"
exit 0
FAKEMDB
chmod +x "$FAKE_DIR/bin/mdb_copy"

# ----- helpers to inspect fake call log ---------------------------------------
fake_log() { cat "${VPS51_FAKE_LOG?"set VPS51_FAKE_LOG"}" 2>/dev/null; }
log_has() { grep -q -- "$1" "${VPS51_FAKE_LOG}"; }

# ----- load the script (sourced; main not run) --------------------------------
if [[ ! -f "$SCRIPT" ]]; then
    echo "RED-GATE-1: $SCRIPT does not exist yet — test harness present but"
    echo "implementation missing. Expected RED before implementation."
    exit 2
fi
export VPS51_SOURCE_ONLY=1   # prevent the script's guarded main from running on source
# shellcheck source=../../scripts/vps51-backup.sh
. "$SCRIPT"

echo "== Gate 1 harness: vps51-backup.sh == (fixture: fakes injected into PATH)"

# --- 1. config defaults --------------------------------------------------------
t "default_weekly_day" "${VPS51_WEEKLY_DOW:-<unset>}" "0"     # Sunday
t "default_keep_daily" "${VPS51_KEEP_DAILY:-<unset>}" "14"
t "default_keep_weekly" "${VPS51_KEEP_WEEKLY:-<unset>}" "8"
t "default_host" "${VPS51_HOST:-<unset>}" "23.182.128.51"
t "default_user" "${VPS51_USER:-<unset>}" "debian"
t "default_key" "${VPS51_KEY:-<unset>}" "$HOME/.ssh/id_ed25519"
t "script_exists" "$( [[ -f "$SCRIPT" ]] && echo yes || echo no )" "yes"

# --- 2. retention computation --------------------------------------------------
# 14 daily = keep last 14 daily snapshots; 8 weekly = keep last 8 weekly.
r="$(_vps51_retention_args 14 8)"
t "retention_args_daily_14_weekly_8" "$r" "--keep-daily=14 --keep-weekly=8"

# --- 3. weekly-determination ---------------------------------------------------
t "is_weekly_sunday" "$(_vps51_is_weekly 0)" "1"
t "is_weekly_monday" "$(_vps51_is_weekly 1)" "0"

# --- 4. restic repo init guard -------------------------------------------------
# repo must be initialised before first run; the script should probe with restic
# cat config. Assert the fake restic IS called.
_restic_repo="$FAKE_DIR/repo"
mkdir -p "$_restic_repo"
export VPS51_FAKE_LOG="${_restic_repo}.repo.log"
rm -f "$VPS51_FAKE_LOG"
_vps51_probe_repo "$_restic_repo" || true   # fake always exits 0
t "repo_probe_invokes_restic" "$( log_has 'restic' && echo yes || echo no )" "yes"
unset VPS51_FAKE_LOG

# --- 5. remote snapshot (WAL-safe) helper --------------------------------------
# Pull function must issue an on-box docker cp of db+wal+shm THEN rsync.
export VPS51_FAKE_LOG="$FAKE_DIR/log5"
rm -f "$VPS51_FAKE_LOG"
stage="$FAKE_DIR/stage5"; mkdir -p "$stage"
VPS51_BACKUP_STAGING="$stage"
out="$(_vps51_pull_sqlite_container routstr-public "/app/data/keys.db" 2>&1 | tr '\n' '|')"
t "sqlite_pull_runs_remote_docker_cp" "$( log_has 'docker cp routstr-public:/app/data/keys.db' && echo yes || echo no )" "yes"
t "sqlite_pull_rsyncs_back" "$( log_has 'rsync' && echo yes || echo no )" "yes"
unset VPS51_FAKE_LOG

# --- 6. weekly LMDB must use mdb_copy ON-BOX (never copy live LMDB) ------------
export VPS51_FAKE_LOG="$FAKE_DIR/log6"
rm -f "$VPS51_FAKE_LOG"
out="$(_vps51_pull_lmdb_dir "strfry" "/var/lib/strfry" "strfry" 2>&1)"
t "lmdb_pull_runs_mdb_copy_onbox" "$( log_has 'mdb_copy' && echo yes || echo no )" "yes"
t "lmdb_pull_rsyncs_back" "$( log_has 'rsync' && echo yes || echo no )" "yes"
unset VPS51_FAKE_LOG

# --- 7. mode auto-select: weekly on Sunday, nightly otherwise ------------------
t "auto_mode_weekly" "$(_vps51_mode 0)" "weekly"
t "auto_mode_nightly" "$(_vps51_mode 3)" "nightly"

# --- 8. summary line shape ------------------------------------------------------
s="$(_vps51_summary nightly 3 2 "$(hostname)" 5)"
t "summary_contains_mode" "$( printf '%s' "$s" | grep -q nightly && echo yes || echo no )" "yes"

echo
echo "== RESULT: PASS=$PASS FAIL=$FAIL =="
[[ "$FAIL" -eq 0 ]]
