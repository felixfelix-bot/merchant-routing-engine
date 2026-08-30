#!/usr/bin/env bash
# test_vps51_restore.sh — test harness for scripts/vps51-restore-test.sh
#
# Gate 1 (TDD) harness for the restore pathway. Generates a real SQLite fixture
# (the "source" db), snapshots its row-counts for the asserted tables, then runs
# the restore-test script against it via fake restic that hands the fixture to
# the restore routine. Asserts: restore lands in target dir, sqlite
# integrity_check passes, and row counts match the source snapshot.
#
# Run: bash scripts/tests/test_vps51_restore.sh
# Exit 0 = all assertions pass.

set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="$REPO/scripts/vps51-restore-test.sh"
PASS=0; FAIL=0

t() { local name="$1" got="$2" want="$3"
    if [[ "$got" == "$want" ]]; then
        PASS=$((PASS+1)); echo "  ok  $name"
    else
        FAIL=$((FAIL+1)); echo "FAIL  $name"
        echo "      want: $want"
        echo "      got:  $got"
    fi
}

FAKE_DIR="$(mktemp -d /tmp/vps51-restore.XXXXXX)"
trap 'rm -rf "$FAKE_DIR"' EXIT
export PATH="$FAKE_DIR/bin:$PATH"
mkdir -p "$FAKE_DIR/bin"

# ---- build a real SQLite fixture ---------------------------------------------
FIX_SRC="$FAKE_DIR/fixture_keys.db"
sqlite3 "$FIX_SRC" <<'SQL'
CREATE TABLE api_keys (hashed_key TEXT PRIMARY KEY, balance REAL, total_spent REAL, total_requests INT);
CREATE TABLE upstream_providers (provider_name TEXT PRIMARY KEY, provider_fee REAL, enabled INT);
INSERT INTO api_keys VALUES ('abc12345', 100.5, 250.0, 42);
INSERT INTO api_keys VALUES ('def67890', 55.25, 70.1, 7);
INSERT INTO upstream_providers VALUES ('z_ai', 0.0, 1);
INSERT INTO upstream_providers VALUES ('openai-compatible', 0.15, 1);
SQL

# Table set we assert row-counts over (subset guaranteed present).
ASSERTED_TABLES="${VPS51_ASSERT_TABLES:-api_keys upstream_providers}"

# ---- fake restic: on `restore <id> --target <dir>` copy fixture there ---------
cat > "$FAKE_DIR/bin/restic" <<'FAKERESTIC'
#!/usr/bin/env bash
if [[ "${1:-}" == "restore" ]]; then
    # locate --target arg
    tgt=""
    prev=""
    for a in "$@"; do
        [[ "$prev" == "--target" ]] && tgt="$a"
        prev="$a"
    done
    mkdir -p "$tgt"
    cp "${VPS51_RESTORE_FIXTURE:-/nonexistent}" "$tgt/keys.db"
    exit 0
fi
exit 0
FAKERESTIC
chmod +x "$FAKE_DIR/bin/restic"

# ---- run the restore-test script ----------------------------------------------
if [[ ! -f "$SCRIPT" ]]; then
    echo "RED-GATE-1: $SCRIPT does not exist yet — restore-test harness present,"
    echo "implementation missing. Expected RED before implementation."
    exit 2
fi

TARGET="$FAKE_DIR/target"
rm -rf "$TARGET"; mkdir -p "$TARGET"

# The restore TEST reads a source rowcount snapshot. We approximate by giving
# the script a source db to compare against (it computes counts itself).
# Run it with the fixture as the "restic restore" source and a temp target.
VPS51_RESTORE_FIXTURE="$FIX_SRC" \
VPS51_ASSERT_TABLES="$ASSERTED_TABLES" \
    bash "$SCRIPT" --selfcheck-target "$TARGET" --selfcheck-source "$FIX_SRC" \
    > "$FAKE_DIR/run.out" 2>&1
rc=$?

echo "== Gate 1 harness: vps51-restore-test.sh == exit=$rc =="

# The script must exit 0 on a clean restore (integrity ok + counts match).
t "clean_restore_exit_0" "$rc" "0"

# restored file must actually exist and be a valid sqlite db
[[ -f "$TARGET/keys.db" ]] && restored_ok=yes || restored_ok=no
t "restored_db_exists" "$restored_ok" "yes"
chk="$(sqlite3 "$TARGET/keys.db" 'PRAGMA integrity_check;' 2>&1 | head -1)"
t "integrity_check_ok" "$chk" "ok"

# row counts match (source vs restored) for every asserted table
count_ok=yes
for tname in $ASSERTED_TABLES; do
    sc="$(sqlite3 "$FIX_SRC" "SELECT COUNT(*) FROM $tname;")"
    rc_="$(sqlite3 "$TARGET/keys.db" "SELECT COUNT(*) FROM $tname;" 2>/dev/null)"
    if [[ "$sc" != "$rc_" ]]; then count_ok=no; fi
done
t "row_counts_match" "$count_ok" "yes"

echo
echo "== RESULT: PASS=$PASS FAIL=$FAIL =="
[[ "$FAIL" -eq 0 ]]