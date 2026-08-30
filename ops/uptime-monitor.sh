#!/usr/bin/env bash
# uptime-monitor.sh — permanent production uptime monitor (RO-1).
#
# Generalizes the 2026-08-28 revival-only bootstrap watchdog
# (vps51-watchdog.sh) into a full per-endpoint uptime monitor:
#   - Probes EVERY production endpoint every 5 min (cron, no_agent).
#   - Alerts ONLY on transitions; non-empty stdout is delivered to the Signal
#     merchant-module group by cron deliver=origin:
#       * >=2 CONSECUTIVE failures of an endpoint that was previously up
#         -> outage alert ONCE (dark transition).
#       * down -> up -> recovery alert ONCE.
#     NO repeated alerts while stably down (daily P&L cron reports outages).
#   - Isolation: an endpoint never observed up does not alert on failures
#     (a box that boots dark is baseline, not an outage) — matches bootstrap
#     semantics where only confirmed revival or fall-after-alive alert.
#   - Preserves bootstrap behavior: on box revival (>=2 consecutive up probes
#     of the box endpoint) auto-unblock the staged recovery kanban tasks.
#
# State: JSON at $STATE_FILE (default
#   ~/.hermes/profiles/manager/cron/state/uptime-monitor.state), shape:
#     { "endpoints": { "<id>": {"status":"up|down|unknown","fails":N,
#                                "ever_up":bool,"was_down":bool,
#                                "last_change":epoch,"display":S} },
#       "box_unblocked":bool, "box_confirm":N, "updated":epoch }
#
# Testability: UPTIME_ENDPOINTS_JSON, UPTIME_STATE_FILE, UPTIME_BOARD,
#   UPTIME_STAGED_TASKS, UPTIME_BOX_ENDPOINT_ID override production defaults;
#   UPTIME_PROBE points at a fake probe (called `<PROBE> <id>`, exit 0 = up).
#   See ops/tests/test_uptime_monitor.sh.
set -u

STATE_FILE="${UPTIME_STATE_FILE:-$HOME/.hermes/profiles/manager/cron/state/uptime-monitor.state}"
BOARD="${UPTIME_BOARD:-merchant-module}"
STAGED_TASKS="${UPTIME_STAGED_TASKS:-t_9c25b7d9 t_5f69c815}"   # RO-2, RO-4 (preserved)
BOX_ENDPOINT_ID="${UPTIME_BOX_ENDPOINT_ID:-vps51-ssh}"
BOX_CONFIRM_REQUIRED=2   # consecutive up probes of the box endpoint before unblock
PROBE="${UPTIME_PROBE:-}"   # fake probe <id> when set (tests)

# Default production endpoint list (JSON). `box:true` gates kanban unblock.
# Hardcoded until the box is back and the /etc/caddy route list can be re-read.
read -r -d '' DEFAULT_ENDPOINTS <<'JSON' || true
[
  {"id":"vps51-ssh",   "display":"VPS2 23.182.128.51:22",           "kind":"tcp",   "host":"23.182.128.51", "port":22,  "box":true},
  {"id":"vps219-ssh",  "display":"VPS 23.182.128.219:22",           "kind":"tcp",   "host":"23.182.128.219","port":22,  "box":false},
  {"id":"routstr-info","display":"routstr.orangesync.tech/v1/info", "kind":"https", "host":"routstr.orangesync.tech", "path":"/v1/info"},
  {"id":"ai-web",      "display":"ai.orangesync.tech",              "kind":"https", "host":"ai.orangesync.tech"},
  {"id":"friends-web", "display":"friends.orangesync.tech",         "kind":"https", "host":"friends.orangesync.tech"},
  {"id":"relay2-web",  "display":"relay2.orangesync.tech",          "kind":"https", "host":"relay2.orangesync.tech"},
  {"id":"blossom2-web","display":"blossom2.orangesync.tech",        "kind":"https", "host":"blossom2.orangesync.tech"}
]
JSON
ENDPOINTS_JSON="${UPTIME_ENDPOINTS_JSON:-$DEFAULT_ENDPOINTS}"

# --- live probe dispatch --------------------------------------------------------
probe_one() { # probe_one <json-endpoint> -> exit 0 = up
  local ep="$1"
  if [ -n "$PROBE" ]; then
    local __id; __id=$(printf '%s' "$ep" | python3 -c 'import json,sys;print(json.loads(sys.argv[1])["id"])' "$ep")
    "$PROBE" "$__id" >/dev/null 2>&1; return $?
  fi
  # Decode endpoint fields WITHOUT eval: python emits tab-separated values, we read
  # them into separate variables. No shell re-evaluation of endpoint content.
  local kind host port path
  IFS=$'\t' read -r kind host port path <<<"$(printf '%s' "$ep" | python3 -c 'import json,sys;e=json.loads(sys.argv[1]);print("\t".join(map(str,[e["kind"],e["host"],e.get("port",0),e.get("path","")])))' "$ep")"
  case "$kind" in
    tcp) nc -zw5 "$host" "$port" >/dev/null 2>&1; return $? ;;
    https)
      local code
      code=$(curl -s --max-time 8 -o /dev/null -w '%{http_code}' "https://${host}${path}" 2>/dev/null)
      [ -n "$code" ] && [ "$code" != "000" ]; return $?
      ;;
    *) return 1 ;;
  esac
}

# --- probe all endpoints -> RESULTS json [{id,state,box}] ------------------------
RESULTS="["
first=1
while IFS= read -r ep; do
  [ -z "$ep" ] && continue
  if probe_one "$ep"; then state=up; else state=down; fi
  id=$(printf '%s' "$ep" | python3 -c 'import json,sys;print(json.loads(sys.argv[1])["id"])' "$ep")
  box=$(printf '%s' "$ep" | python3 -c 'import json,sys;print(1 if json.loads(sys.argv[1]).get("box") else 0)' "$ep")
  [ "$first" = 1 ] || RESULTS="$RESULTS,"
  RESULTS="$RESULTS{\"id\":$(printf '%s' "$id" | python3 -c 'import json,sys;print(json.dumps(sys.argv[1]))' "$id"),\"state\":\"$state\",\"box\":$box}"
  first=0
done < <(printf '%s\n' "$ENDPOINTS_JSON" | python3 -c 'import json,sys;print("\n".join(json.dumps(e) for e in json.loads(sys.stdin.read())))')
RESULTS="$RESULTS]"

# --- python state engine: transitions -> alerts + unblock decision --------------
OUT=$(STATE_FILE="$STATE_FILE" BOARD="$BOARD" STAGED_TASKS="$STAGED_TASKS" \
      BOX_ENDPOINT_ID="$BOX_ENDPOINT_ID" BOX_CONFIRM_REQUIRED="$BOX_CONFIRM_REQUIRED" \
      RESULTS="$RESULTS" python3 - <<'PY'
import json, os, time
sf     = os.environ["STATE_FILE"]
boxid  = os.environ["BOX_ENDPOINT_ID"]
confirm_req = int(os.environ["BOX_CONFIRM_REQUIRED"])
results = json.loads(os.environ["RESULTS"])
now = int(time.time())

def load():
    if os.path.exists(sf):
        try:
            return json.load(open(sf))
        except Exception:
            pass
    return {"endpoints": {}, "box_unblocked": False, "box_confirm": 0}

data = load()
eps     = data.setdefault("endpoints", {})
confirm = int(data.get("box_confirm", 0))
unblocked = bool(data.get("box_unblocked", False))

alerts = []
need_unblock = False
display = {r["id"]: r.get("display","") for r in results}

def alert(kind, r, e):
    return "ALERT: [%s] %s (%s)" % (kind, e.get("display") or r["id"], r["id"])

for r in results:
    eid = r["id"]; up = (r["state"] == "up")
    e = eps.setdefault(eid, {"status":"unknown","fails":0,"ever_up":False,
                             "was_down":False,"last_change":0,"display":display.get(eid,"")})
    e["display"] = display.get(eid, e.get("display",""))
    was_down = bool(e.get("was_down"))

    if up:
        if was_down:
            alerts.append(alert("BACK", r, e))
        e["was_down"] = False
        e["fails"] = 0
        e["ever_up"] = True
        e["status"] = "up"
        e["last_change"] = now
        if eid == boxid:
            confirm += 1
            if not unblocked and confirm >= confirm_req:
                need_unblock = True
                unblocked = True
    else:
        e["fails"] = int(e.get("fails",0)) + 1
        e["status"] = "down"
        e["last_change"] = now
        if eid == boxid:
            confirm = 0
        if e.get("ever_up"):
            if e["fails"] >= 2 and not e["was_down"]:
                alerts.append(alert("DOWN", r, e))
                e["was_down"] = True
        # never-observed-up endpoint: isolated, no alert (baseline)

print(json.dumps({"alerts":alerts,"need_unblock":need_unblock,
                  "confirm":confirm,"unblocked":unblocked,
                  "endpoints":eps}, sort_keys=True))
PY
)

# --- parse + persist + side effects ---------------------------------------------
NEW_EPS=$(printf '%s' "$OUT" | python3 -c 'import json,sys;print(json.dumps(json.loads(sys.argv[1])["endpoints"]))' "$OUT")
NEW_CONFIRM=$(printf '%s' "$OUT" | python3 -c 'import json,sys;print(json.loads(sys.argv[1])["confirm"])' "$OUT")
NEW_UNBLOCKED=$(printf '%s' "$OUT" | python3 -c 'import json,sys;print(1 if json.loads(sys.argv[1])["unblocked"] else 0)' "$OUT")
NEED_UNBLOCK=$(printf '%s' "$OUT" | python3 -c 'import json,sys;print(1 if json.loads(sys.argv[1])["need_unblock"] else 0)' "$OUT")
ALERTS=$(printf '%s' "$OUT" | python3 -c 'import json,sys;print("\n".join(json.loads(sys.argv[1])["alerts"]))' "$OUT")

# persist state (atomic: write temp file, fsync, rename)
STATE_FILE="$STATE_FILE" NEW_EPS="$NEW_EPS" NEW_CONFIRM="$NEW_CONFIRM" NEW_UNBLOCKED="$NEW_UNBLOCKED" python3 - <<'PY'
import json,os,time,tempfile
sf=os.environ["STATE_FILE"]
d={}
if os.path.exists(sf):
    try: d=json.load(open(sf))
    except Exception: d={}
d["endpoints"]=json.loads(os.environ["NEW_EPS"])
d["box_confirm"]=int(os.environ["NEW_CONFIRM"])
d["box_unblocked"]=bool(int(os.environ["NEW_UNBLOCKED"]))
d["updated"]=int(time.time())
os.makedirs(os.path.dirname(sf),exist_ok=True)
fd,tmp=tempfile.mkstemp(dir=os.path.dirname(sf),prefix=".uptime.",suffix=".tmp")
with os.fdopen(fd,"w") as f:
    json.dump(d,f); f.flush(); os.fsync(f.fileno())
os.replace(tmp,sf)
PY

# kanban auto-unblock on box revival (bootstrap behavior preserved)
if [ "$NEED_UNBLOCK" = "1" ]; then
  for t in $STAGED_TASKS; do
    hermes kanban --board "$BOARD" unblock "$t" >/dev/null 2>&1 && ALERTS="$ALERTS
unblocked: $t"
  done
  ALERTS="$ALERTS
Box 23.182.128.51 revived ($BOX_ENDPOINT_ID up x$BOX_CONFIRM_REQUIRED) — staged recovery tasks unblocked."
fi

[ -n "$ALERTS" ] && printf '%s\n' "$ALERTS"
exit 0
