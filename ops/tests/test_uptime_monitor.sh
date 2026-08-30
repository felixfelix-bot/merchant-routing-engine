#!/usr/bin/env bash
# test_uptime_monitor.sh — TDD harness for ops/uptime-monitor.sh.
#
# Asserts the state-machine transitions the uptime monitor must guarantee:
#   1. up -> >=2 consecutive down  => outage ALERT exactly once (dark->alert-once)
#   2. stable-down                 => NO repeated alerts (flap suppression)
#   3. down -> up                  => revival ALERT exactly once + kanban unblock
#   4. never-up (isolated)         => NO alert ever (endpoint not "previously up")
#
# Isolation:
#   - UPTIME_PROBE env redirects probing to a fake probe (exit 0 = up).
#   - hermes is shadowed in PATH so the kanban-unblock side effect can be asserted.
#   - UPTIME_STATE_FILE + UPTIME_BOARD + UPTIME_STAGED_TASKS sandbox all state.
#
# Invocation:
#   UPTIME_BIN=/path/to/uptime-monitor.sh bash test_uptime_monitor.sh
#   (UPTIME_BIN defaults to ../uptime-monitor.sh relative to this file)
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UPTIME_BIN="${UPTIME_BIN:-$HERE/../uptime-monitor.sh}"

# ---- counters -----------------------------------------------------------------
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  PASS: $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }

# ---- sandbox ------------------------------------------------------------------
WORK="$(mktemp -d /tmp/uptime-test.XXXXXX)"
STATE="$WORK/state.json"
FAKE_BIN_DIR="$WORK/bin"
FAKE_PROBE="$WORK/fake-probe.sh"
FAKE_HERMES="$FAKE_BIN_DIR/hermes"
mkdir -p "$FAKE_BIN_DIR"
touch "$STATE"   # start empty (fresh install)

cat > "$FAKE_PROBE" <<'PROBE'
#!/usr/bin/env bash
# fake probe: exit 0 iff $MAP/$1 says 1
[ -f "$MAP" ] || exit 1
python3 -c "import sys,os,json;m=json.load(open(os.environ['MAP']));sys.exit(0 if m.get('$1',0) else 1)"
PROBE
chmod +x "$FAKE_PROBE"

# fake `hermes` shadows real CLI inside the sandbox PATH
cat > "$FAKE_HERMES" <<'HERMES'
#!/usr/bin/env bash
# capture kanban unblock invocations
if [ "$1" = "kanban" ]; then
  echo "CALLED: kanban ${*:2}" >> "$HERMES_LOG"
fi
exit 0
HERMES
chmod +x "$FAKE_HERMES"

# ---- scenario helpers ---------------------------------------------------------
# map: JSON dict id -> 0(down)/1(up). Default all down so fresh install probes stay dark.
MAP="$WORK/map.json"
echo '{}' > "$MAP"

# endpoints JSON used for every test run (single box endpoint is enough for
# transition assertions; the multi-endpoint list is a production concern).
export UPTIME_ENDPOINTS_JSON='
[{"id":"vps51-ssh","display":"VPS2 TCP/22","kind":"tcp","host":"23.182.128.51","port":22,"box":true}]
'
export UPTIME_PROBE="$FAKE_PROBE"
export PATH="$FAKE_BIN_DIR:$PATH"
export HERMES_LOG="$WORK/hermes.log"
rm -f "$HERMES_LOG"

set_map() { # set_map '{"vps51-ssh":1}'
  printf '%s' "$1" > "$MAP"
}
# reset map + state before each scenario
reset_env() {
  echo '{}' > "$MAP"
  rm -f "$STATE"
  rm -f "$HERMES_LOG"
  touch "$STATE"
}

run() {
  MAP="$MAP" UPTIME_STATE_FILE="$STATE" UPTIME_BOARD="merchant-module" UPTIME_STAGED_TASKS="t_AAA t_BBB" \
    bash "$UPTIME_BIN"
}

# ==============================================================================
echo "=== Scenario A: dark -> alert-once, flap suppression, revival ==="
reset_env
set_map '{"vps51-ssh":1}'          # endpoint comes up fresh -> initialize up, silent
out=$(run); [ -z "$out" ] && ok "A1 init-up: silent on first healthy observation" || bad "A1 expected empty stdout, got: [$out]"

# now it goes dark
set_map '{"vps51-ssh":0}'; out=$(run)
[ -n "$out" ] && bad "A2 fail#1 (fails<2): must stay silent" || ok "A2 fail#1 silent (needs >=2 consecutive)"
set_map '{"vps51-ssh":0}'; out=$(run)
echo "$out" | grep -qiE 'ALERT.*(down|dark|unreachable|outage)' \
  && ok "A3 fail#2: outage alert fired once" || bad "A3 expected outage alert, got: [$out]"
set_map '{"vps51-ssh":0}'; out=$(run)
[ -z "$out" ] && ok "A4 stable-down: NO repeated alert (flap suppression)" || bad "A4 expected silence while stably dark, got: [$out]"
set_map '{"vps51-ssh":0}'; out=$(run)
[ -z "$out" ] && ok "A5 stable-down again: still silent" || bad "A5 expected silence, got: [$out]"

# revival
set_map '{"vps51-ssh":1}'; out=$(run)
echo "$out" | grep -qiE 'ALERT.*(back|recover|revival)' \
  && ok "A6 revival: recovery alert fired once" || bad "A6 expected revival alert, got: [$out]"
set_map '{"vps51-ssh":1}'; out=$(run)   # 2nd consecutive up -> box confirm>=2 triggers unblock
[ -n "$out" ] \
  && ok "A7 2nd up-run: box-unblock message on revival (confirm>=2)" \
  || bad "A7 expected box-unblock message on 2nd consecutive up, got: [$out]"
grep -q "CALLED: kanban --board merchant-module unblock t_AAA" "$HERMES_LOG" \
  && ok "A7b revival unblocks staged task t_AAA" || bad "A7b expected kanban unblock t_AAA on revival; log: $(cat "$HERMES_LOG" 2>/dev/null)"
grep -q "CALLED: kanban --board merchant-module unblock t_BBB" "$HERMES_LOG" \
  && ok "A8 revival unblocks staged task t_BBB" || bad "A8 expected kanban unblock t_BBB on revival"
set_map '{"vps51-ssh":1}'; out=$(run)
[ -z "$out" ] && ok "A9 post-revival stable up: silent" || bad "A9 expected silence, got: [$out]"

# ==============================================================================
echo "=== Scenario B: never-up endpoint must never alert (isolation) ==="
reset_env
set_map '{"vps51-ssh":0}'
out=$(run); [ -z "$out" ] || bad "B1 never-up run1: silent" ; [ -z "$out" ] && ok "B1 never-up run1 silent"
set_map '{"vps51-ssh":0}'
out=$(run); [ -z "$out" ] || bad "B2 never-up run2: silent (not previously up)" ; [ -z "$out" ] && ok "B2 never-up run2 silent"
set_map '{"vps51-ssh":0}'
out=$(run); [ -z "$out" ] || bad "B3 never-up run3: still silent" ; [ -z "$out" ] && ok "B3 never-up run3 silent"

# ==============================================================================
echo "=== Scenario C: state file is JSON with per-endpoint fields ==="
reset_env
set_map '{"vps51-ssh":0}'; run >/dev/null 2>&1
python3 - "$STATE" <<'PY' && ok "C1 state file is valid JSON" || bad "C1 state is not valid JSON"
import json,sys
json.load(open(sys.argv[1]))
PY
python3 - "$STATE" <<'PY' && ok "C2 per-endpoint status+fails present" || bad "C2 missing endpoint fields"
import json,sys
d=json.load(open(sys.argv[1]))
assert "vps51-ssh" in d.get("endpoints",{})
e=d["endpoints"]["vps51-ssh"]
assert "status" in e and "fails" in e
PY

# ==============================================================================
echo
echo "RESULT: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ] && rm -rf "$WORK"
exit "$FAIL"
