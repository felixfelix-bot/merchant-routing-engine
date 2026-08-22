# DESIGN: Urgency Enforcement + Price-Aware Dispatch (CG-11)

**Status:** proposed — ready for CG-11 implementation
**Date:** 2026-08-22
**Author:** consultant (glm-5.2), for operator review
**Related:** skill `devops/urgency-aware-dispatch`, `staggered-dispatch.sh` (STAGGER-DISPATCH t_5e11243e), `rate_limit_gate.py` (KALMAN-GATE t_6aceaaa3), BOARD-PAUSE t_75b0e344

---

## 0. Root causes (validated + newly found)

The 2026-08-22 paid-token bleed during quota exhaustion was not a knowledge problem —
the urgency skill documented the correct protocol. It was an **enforcement gap**:

1. **CONFIRMED — skills are invisible to deterministic code.** The urgency skill only
   influences LLM replies via skill scanning. Crons, watchdogs, importers, and the
   dispatcher are plain code that never loads skills. Urgency was advisory; every
   non-LLM path could bypass it silently.
2. **CONFIRMED — no single dispatch chokepoint.** At least three independent paths
   spawn workers: (a) interactive `hermes kanban dispatch`, (b) `staggered-dispatch.sh`
   cron which calls the **venv binary directly** (bypassing any CLI wrapper), and
   (c) watchdog advancers (e.g. `ox_pipeline_advance.py`) that *unblock dependents*,
   making them ready for the next dispatch pass at any price. Gating only the CLI
   cannot work; the invariant must live at the **data layer** (task readiness).
3. **FOUND — non-interactive creators cannot "ask".** `import-schedule-to-kanban.py`
   creates tasks every 5 min via cron. A hard "always ask a human" gate would fire
   spuriously and wedges automation. Automation needs an explicit default lane, not a prompt.
4. **FOUND — "available" ≠ "cheap".** `zai_state.json` right now: `friend_available:
   true` while `friend_token_pct: 0`. The binary gate (zai-quota-gate) passes while
   every dispatched token is paid failover. No existing signal expresses *price tier*.
5. **FOUND — half-built timing primitive.** The `schedule` status ("waiting on time,
   not human input") already exists and dispatch skips it, but nothing ever promotes
   scheduled tasks on a price/time condition. The park mechanism exists; the brain doesn't.
6. **CRITIQUE of framing — "the board decides dispatch timing."** A SQLite file decides
   nothing. The correct mental model: **board state encodes timing** (status=scheduled +
   urgency), and a deterministic watchdog mutates state when the price signal clears,
   then the *existing* dispatchers run unchanged. Similarly "always ask urgency" must
   mean: human/manager-initiated creates get asked; automation gets a mandatory explicit
   default (`--urgency batch` + exemption flag, logged). Otherwise the gate fires
   spuriously — the exact thing the operator hates.

---

## 1. Design principles

- **Enforcement = schema + gates in code**, not documentation. The skill stays as the
  *semantic* definition (what NOW/SOON/DEFER/BATCH mean, the ask template); this design
  makes the *mechanics* deterministic.
- **The invariant is: a task may be `ready` only if it is classified AND price-eligible.**
  Every layer enforces or restores this invariant; dispatchers themselves are untouched
  in their selection logic (they still pick ready tasks by priority).
- **Fail polarity (argued):**
  - *Fail-closed against silent spend:* a task with `urgency IS NULL` **never** reaches a
    worker silently. It is parked (`schedule` status) with a loud alert. This is the
    anti-bleed property; making it fail-open would recreate the incident.
  - *Fail-open against our own bugs:* if the gate code itself errors (can't read DB,
    can't parse a signal), the gate logs loudly and **lets dispatch proceed** — same
    contract as staggered-dispatch T3.1 ("never wedge dispatch on our own bugs"). A gate
    bug must not freeze urgent work.
  - Summary: **per-task fail-closed, per-system fail-open, never silent.**
- **No spurious firing:** one alert per parking event (status change dedupes naturally),
  automation exempted via env flag, escalation alerts once per task.
- **Sanctioned paths only:** all status mutations go through the `hermes` CLI
  (`schedule`, `unblock`, `comment`) so events/audit trails stay intact. Direct SQL is
  used only for the new `urgency_*` columns, which the binary ignores.

---

## 2. Schema

Per-board DBs at `~/.hermes/kanban/boards/<slug>/kanban.db`, table `tasks`
(32 columns today, no urgency). Migration is additive, idempotent, and self-healing
(gates auto-migrate any board they touch, so new boards and stragglers converge):

```sql
ALTER TABLE tasks ADD COLUMN urgency TEXT
  CHECK (urgency IN ('now','soon','defer','batch'));   -- NULL = unclassified
ALTER TABLE tasks ADD COLUMN urgency_deadline INTEGER; -- epoch sec; SOON escalation trigger
ALTER TABLE tasks ADD COLUMN urgency_set_at INTEGER;   -- epoch sec, audit
ALTER TABLE tasks ADD COLUMN urgency_source TEXT;      -- operator|manager|automation-default|backfill|escalation
CREATE INDEX IF NOT EXISTS idx_tasks_urgency ON tasks(status, urgency);
```

Notes:
- SQLite `CHECK` passes NULL, so unclassified is representable; invalid values are rejected.
- Column names are `urgency_`-prefixed to avoid collision with any future upstream column.
- The binary ignores unknown columns (verified: schema already carries optional fields);
  still, **test the migration on a scratch board first** (see §8) and keep rollback = do
  nothing (extra columns are inert).

**Urgency × priority:** urgency decides **when a task is eligible**; priority stays the
operator's **order-within-eligible** tiebreaker (dispatcher sorts by priority — unchanged).
Single exception: escalation to `now` bumps `priority = MAX(priority, 8)` so an escalated
task jumps queued eligible work. No other automatic priority rewriting.

### Backfill (one-time, at CG-11 deploy)

```sql
-- Mid-flight tasks: classify SOON (safe middle: never guesses NOW = expensive,
-- never strands work as BATCH). No deadline → no auto-escalation for backfilled rows.
UPDATE tasks SET urgency='soon', urgency_source='backfill',
  urgency_set_at=CAST(strftime('%s','now') AS INTEGER)
WHERE urgency IS NULL AND status IN ('ready','running');
-- Done/review/archived stay NULL (never dispatched again — harmless).
```

The migration script then prints every remaining NULL task in a dispatchable-or-later
status (`todo`, `scheduled`, `blocked`) as a **reclassify list** for the operator:
`hermes-urgency set <board> <task_id> <level>` one-liners. Blocked tasks need no rush —
the dispatch gate re-checks them the moment a watchdog unblocks them into `ready`.

---

## 3. Layer 1 — create-path gate (force the ask)

**Interception point:** `/home/c03rad0r/.local/bin/hermes` is a 4-line bash wrapper
(`exec venv/bin/hermes "$@"`). We own it. That is the create-path chokepoint for every
human- and script-initiated `hermes kanban` call.

```bash
#!/usr/bin/env bash
unset PYTHONPATH
unset PYTHONHOME
# --- URGENCY ENFORCEMENT (CG-11) — backup at ~/.local/bin/hermes.pre-cg11 ---
if [ "$1" = "kanban" ] && [ -z "${HERMES_URGENCY_EXEMPT:-}" ]; then
  case "$2" in
    create|swarm|promote|dispatch)
      exec /home/c03rad0r/.hermes/scripts/urgency_gate.py gate "$@" ;;
  esac
fi
exec "/home/c03rad0r/.hermes/hermes-agent/venv/bin/hermes" "$@"
```

Behavior of `urgency_gate.py gate`:

| Subcommand | Behavior |
|---|---|
| `create` | Requires `--urgency now\|soon\|defer\|batch` (unknown flag to the binary — the gate **strips it**, runs the real CLI with `--json`, then writes the column + appends a `## Urgency: X` header to the body). If the flag is absent **and stdin is a TTY**: prints the skill's decision matrix (quota state + burn rate + cost of each choice) and reads the answer — the ask, enforced. If absent and **not** a TTY: exit 2 with `URGENCY REQUIRED: ...` — automation must be explicit. `--urgency-deadline <+Hh \| ISO>` optional (SOON default: created_at + 6h). |
| `swarm` | One urgency governs the whole swarm: require the flag (or ask once on TTY), run real CLI, then stamp every task created in the last 60 s on that board with NULL urgency. |
| `promote` | Refuses to promote tasks whose urgency is NULL (parks them to `scheduled` with reason `urgency-unclassified` + alert); promotes the rest via the real CLI. |
| `dispatch` | **Layer 2** (below), then execs the real dispatcher. |

**Exemption valve:** `HERMES_URGENCY_EXEMPT=1` bypasses gates for automation that
legitimately cannot classify (importers, emergency operator override). Every bypass is
logged to `~/.hermes/logs/urgency-gate.log` — fail-open, never silent. Importers should
migrate to `--urgency batch` (schedule-driven pipeline work is by definition cheapest-lane).

The ask itself (matrix with live econ context, per the skill) is what the TTY prompt
renders — quota state, paid-burn rate, and the cost-of-waiting vs cost-of-tokens line.
If the operator already stated urgency in the message that spawned the create, the
creating manager passes `--urgency` explicitly and no prompt fires (no spurious asks).

**Known residual hole (accepted):** in-process task creation inside the binary
(`decompose`, `specify`) does not cross the shell wrapper. Layer 2 closes it — that is
why Layer 2, not Layer 1, is the hard invariant.

---

## 4. Layer 2 — dispatch-time gate (the hard invariant)

Runs (a) inside the wrapper before any interactive dispatch, and (b) as a pre-check
inside `staggered-dispatch.sh`'s loop (which calls the venv binary directly — it must
call the gate itself, see §6). Steps per board, before the real dispatcher runs:

1. **Auto-migrate** the board DB if `urgency` column is missing (idempotent).
2. **Park unclassified:** every `status='ready'` task with `urgency IS NULL` →
   `hermes kanban schedule <id> "urgency-unclassified: parked by CG-11 — classify: hermes-urgency set <board> <id> <now|soon|defer|batch>"`,
   plus alert (syslog + log + stdout). The task does not burn tokens; the alert forces the ask.
3. **Price-hold ineligible:** every `ready` task whose urgency requires a cheaper tier
   than current (§5) → `schedule <id> "price-hold: urgency=<u> needs tier≤<t>, current=<tier> (evidence)"`.
4. **Exec the real dispatcher** unchanged (`dispatch --max N ...`). Only classified,
   price-eligible tasks remain `ready`; priority order inside that set is untouched.

Failure handling: any exception in steps 1–3 → loud log line, **proceed to dispatch**
(system fail-open per §1). Parking never rewrites `result`/failure counters — re-queue
semantics identical to the T3.2 quota sweeper.

---

## 5. Layer 3 — price-tier engine + tick watchdog (timing brain)

### 5.1 Price signal → tier

One pure function, stdlib only, 60 s cached (`/tmp/urgency_tier.json`), every input
optional and degradable — a missing signal is skipped, never fatal:

| Input | Source | Feeds |
|---|---|---|
| Free lane live | `oxalpha` model present in `http://localhost:9099/v1/models` **and** key marker (`/tmp/ox_key_gate_ready` or `OPENROUTER_OXALPHA_KEY` in `~/.hermes/.env`) | tier `free` |
| Quota percentages | `http://localhost:9099/quota` (weekly + 5 h `used_pct`); fallback `~/.hermes/bot/zai_state.json` `friend_token_pct` inverted | cheap/medium/expensive bands |
| Paid burn | `~/.hermes/bot/api_burn.db` cache: spend in last 1 h | ≥ $0.50/h forces `expensive` |
| Hard gate | `~/.hermes/state/rate_limit_gate.json` (`paused`, quota-class reason, `resume_at`) | paused → `expensive` + evidence; the existing staggered pause/canary machinery stays authoritative for *availability* |

Tiers (aligned with the skill's pressure gate thresholds):

```
free      : oxalpha lane live
cheap     : weekly_used < 60%  AND  5h_used < 40%
medium    : weekly_used < 85%  AND  5h_used < 75%
expensive : otherwise, OR paid burn ≥ $0.50/h, OR hard gate paused
```

`urgency_gate.py tier` prints tier + evidence — one command the operator can run to
audit any hold decision. **Every hold/park message carries the evidence** (percentages,
lane state), so a gate that fires spuriously is self-explaining and falsifiable.

### 5.2 Urgency → eligibility → window → escalation

| urgency | dispatchable at tier | window | if window expires undelivered |
|---|---|---|---|
| `now`  | any (price-blind; only the existing hard 429/quota gate applies) | none | — |
| `soon` | free, cheap, medium | `urgency_deadline` (default create+6 h) | **auto-escalate to `now`** (source=escalation, priority→MAX(p,8)) + one alert; next dispatch pass carries it regardless of price. Operator can re-`set` to defer before then. |
| `defer`| free, cheap | unbounded (days, by design) | none; optional weekly staleness notice (one alert/week, no action) |
| `batch`| free (or cheap + off-peak 22–06 UTC) | unbounded | none |

Escalation trigger condition: `urgency='soon' AND status IN ('todo','scheduled','ready')
AND now > urgency_deadline`. Running/done/review tasks never escalate. Escalation is a
*flip + alert*, not a spawn — the next sanctioned dispatch pass picks it up.

### 5.3 The tick watchdog

New cron, every 10 min, no skills, no agent:

```
*/10 * * * * /usr/bin/python3 /home/c03rad0r/.hermes/scripts/urgency_gate.py tick >> /home/c03rad0r/.hermes/logs/urgency-gate.log 2>&1
```

`tick` (all boards under `~/.hermes/kanban/boards/`):
1. compute tier once (§5.1);
2. auto-migrate + park NULL ready tasks (defense in depth for boards dispatched by
   paths that pre-date the gate);
3. **promote:** `scheduled` tasks parked with reason prefix `price-hold:` or
   `urgency-unclassified:` whose urgency now satisfies the tier → `unblock` (re-queue,
   not a failure — sweeper semantics). `urgency-unclassified` ones promote only via
   explicit `set` (classification), never automatically;
4. escalate expired SOON (§5.2);
5. **dispatch (opt-in only):** for boards listed in
   `~/.hermes/config/urgency-autodispatch-boards.txt` (one slug per line, ships empty),
   run `dispatch --max 1` when ≥1 eligible ready task exists. This is the "board
   dispatches itself at the cheap moment" behavior — **per-board enrollment**, so
   nothing spawns at 3 am that the operator didn't sign up for. Boards on the existing
   staggered cron keep their dispatcher; the tick never double-spawns (flock + opt-in).

Helper CLI (used by operator and by the park messages):
`hermes-urgency set <board> <task_id> <now|soon|defer|batch> [--deadline <+Hh|ISO>] [--note ...]`
— writes columns, sources `operator`, appends a task comment, and immediately promotes
from `urgency-unclassified` parking if the tier allows.

---

## 6. Wiring map (who calls what)

| Path | Change |
|---|---|
| `~/.local/bin/hermes` wrapper | 8-line interception (§3). Backup original first. |
| `staggered-dispatch.sh` | Two-line diff: before its per-board `dispatch --max 1`, call `urgency_gate.py park-and-hold --board "$board"` (parks NULLs + price-holds; always exit 0). Its existing load/RAM/gate/canary logic untouched and stays authoritative for availability. |
| Watchdog advancers (`ox_pipeline_advance.py` et al.) | **No change required.** They unblock dependents → `ready`; the next dispatch pass (wrapper or staggered) parks anything unclassified/price-ineligible. That is the point of the data-layer invariant. |
| Crontab | +1 line (tick, §5.3). |
| Importers (`import-schedule-to-kanban.py`) | Add `--urgency batch` (or `HERMES_URGENCY_EXEMPT=1` until migrated; logged either way). |
| Skill `urgency-aware-dispatch` | Stays; add a pointer: "enforcement is CG-11 — pass `--urgency` explicitly to avoid the TTY ask." |

---

## 7. Spurious-fire analysis (operator's red line)

- The gate never *blocks dispatch* — it parks *individual tasks* with evidence-bearing
  reasons. NOW work is never parked. Nothing crashes (exceptions fail open).
- Alerts fire once per park event (status change dedupes), once per escalation, weekly
  per stale DEFER — no loops, no nagging.
- Automation exempted by flag or explicit `--urgency`; exempt uses are logged.
- Hold decisions are auditable in one command (`hermes-urgency tier`) and reversible in
  one command (`hermes-urgency set ... now`).

---

## 8. CG-11 implementation plan (paste-ready)

**Order matters: migration → gate script → wrapper → staggered diff → cron → tests → enroll.**
All new code is stdlib-only Python 3 / bash. Never touch other in-flight branches.

### 8.1 Migration (all boards, idempotent)

`~/.hermes/scripts/urgency_migrate.py`:

```python
#!/usr/bin/env python3
"""CG-11: add urgency columns to every kanban board. Idempotent."""
import glob, os, sqlite3, time, sys

ROOT = os.path.expanduser("~/.hermes/kanban/boards")
COLS = [
    ("urgency",          "TEXT CHECK (urgency IN ('now','soon','defer','batch'))"),
    ("urgency_deadline", "INTEGER"),
    ("urgency_set_at",   "INTEGER"),
    ("urgency_source",   "TEXT"),
]

def migrate(db):
    conn = sqlite3.connect(db, timeout=10)
    conn.execute("PRAGMA busy_timeout=5000")
    have = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    added = []
    for name, decl in COLS:
        if name not in have:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {decl}")
            added.append(name)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_urgency ON tasks(status, urgency)")
    # Backfill: mid-flight tasks -> soon (no deadline, no escalation for backfilled rows)
    cur = conn.execute(
        "UPDATE tasks SET urgency='soon', urgency_source='backfill', "
        "urgency_set_at=? WHERE urgency IS NULL AND status IN ('ready','running')",
        (int(time.time()),))
    backfilled = cur.rowcount
    conn.commit()
    reclassify = list(conn.execute(
        "SELECT id, title, status FROM tasks WHERE urgency IS NULL "
        "AND status IN ('todo','scheduled','blocked')"))
    conn.close()
    return added, backfilled, reclassify

if __name__ == "__main__":
    for db in sorted(glob.glob(os.path.join(ROOT, "*", "kanban.db"))):
        board = os.path.basename(os.path.dirname(db))
        try:
            added, n, rec = migrate(db)
            print(f"[{board}] added={added or '-'} backfilled_soon={n}")
            for tid, title, st in rec:
                print(f"  RECLASSIFY {tid} ({st}): {title[:60]}")
        except Exception as e:
            print(f"[{board}] ERROR {e} — skipped (fail-open)", file=sys.stderr)
```

Run: `python3 ~/.hermes/scripts/urgency_migrate.py 2>&1 | tee ~/.hermes/logs/urgency-migrate.log`
— then work the RECLASSIFY list with `hermes-urgency set`.

### 8.2 Gate script (the whole brain)

`~/.hermes/scripts/urgency_gate.py` — full file:

```python
#!/usr/bin/env python3
"""CG-11 urgency enforcement: create-gate, dispatch-gate, tick, tier, set.
Fail polarity: per-task fail-closed (unclassified never dispatches silently),
per-system fail-open (our bugs never stop dispatch). All mutations via hermes CLI."""
import json, os, re, sqlite3, subprocess, sys, time

HOME = os.path.expanduser("~")
BOARDS = f"{HOME}/.hermes/kanban/boards"
REAL = f"{HOME}/.hermes/hermes-agent/venv/bin/hermes"
LOG = f"{HOME}/.hermes/logs/urgency-gate.log"
TIER_CACHE = "/tmp/urgency_tier.json"
AUTODISPATCH = f"{HOME}/.hermes/config/urgency-autodispatch-boards.txt"
LEVELS = ("now", "soon", "defer", "batch")
DEFAULT_SOON_H = 6
OFFPEAK = set(range(22, 24)) | set(range(0, 6))  # UTC hours

def log(msg):
    line = f"[{time.strftime('%FT%TZ', time.gmtime())}] {msg}"
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as f: f.write(line + "\n")
    except Exception: pass
    print(msg)  # cron captures stdout

def alert(msg):
    subprocess.run(["logger", "-t", "urgency-gate", "--", f"ALERT {msg}"],
                   capture_output=True)
    log(f"ALERT {msg}")

def board_db(board):
    return f"{BOARDS}/{board}/kanban.db"

def resolve_board(args):
    if "--board" in args:
        return args[args.index("--board") + 1]
    return os.environ.get("HERMES_KANBAN_BOARD") or "default"

def sql(board, fn):
    """Run fn(conn) with busy_timeout; any error -> None (fail-open)."""
    try:
        conn = sqlite3.connect(board_db(board), timeout=10)
        conn.execute("PRAGMA busy_timeout=5000")
        try: return fn(conn)
        finally: conn.close()
    except Exception as e:
        log(f"SQL fail-open ({board}): {e}")
        return None

def migrate(conn):
    have = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    if "urgency" in have: return False
    conn.execute("ALTER TABLE tasks ADD COLUMN urgency TEXT "
                 "CHECK (urgency IN ('now','soon','defer','batch'))")
    conn.execute("ALTER TABLE tasks ADD COLUMN urgency_deadline INTEGER")
    conn.execute("ALTER TABLE tasks ADD COLUMN urgency_set_at INTEGER")
    conn.execute("ALTER TABLE tasks ADD COLUMN urgency_source TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_urgency ON tasks(status, urgency)")
    conn.commit()
    return True

def kb(board, *args):
    env = dict(os.environ, HERMES_KANBAN_BOARD=board)
    return subprocess.run([REAL, "kanban"] + list(args), capture_output=True,
                          text=True, env=env, timeout=120)

# ---------- price tier (every input optional; missing -> skip) ----------
def _http_json(url, timeout=5):
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except Exception:
        return None

def price_tier():
    now = time.time()
    try:
        c = json.load(open(TIER_CACHE))
        if now - c.get("ts", 0) < 60: return c
    except Exception: pass
    ev, tier = [], "medium"  # unknown defaults to medium: soon passes, defer holds
    # free lane
    lane = False
    models = _http_json("http://localhost:9099/v1/models")
    if models and any("oxalpha" in str(m.get("id", "")).lower()
                      for m in models.get("data", [])):
        if os.path.exists("/tmp/ox_key_gate_ready") or _env_has_key():
            lane = True
    if lane:
        tier, ev = "free", ["oxalpha lane live"]
    else:
        q = _http_json("http://localhost:9099/quota") or {}
        w, h = _quota_pcts(q)
        if w is not None and h is not None:
            if w < 60 and h < 40:   tier, ev = "cheap",   [f"weekly={w}% 5h={h}%"]
            elif w < 85 and h < 75: tier, ev = "medium",  [f"weekly={w}% 5h={h}%"]
            else:                   tier, ev = "expensive", [f"weekly={w}% 5h={h}%"]
        else:
            ev.append("quota signal missing (medium default)")
    # paid-burn override
    spent = _paid_burn_1h()
    if spent is not None and spent >= 0.50 and tier != "free":
        tier, ev = "expensive", ev + [f"paid burn ${spent:.2f}/1h"]
    # hard gate (availability stays authoritative in staggered-dispatch)
    try:
        g = json.load(open(f"{HOME}/.hermes/state/rate_limit_gate.json"))
        if g.get("paused") and tier != "free":
            tier = "expensive"
            ev.append(f"hard gate paused: {g.get('reason', '?')[:60]}")
    except Exception: pass
    out = {"ts": now, "tier": tier, "evidence": ev}
    try: json.dump(out, open(TIER_CACHE, "w"))
    except Exception: pass
    return out

def _env_has_key():
    try:
        return "OPENROUTER_OXALPHA_KEY=" in open(f"{HOME}/.hermes/.env").read()
    except Exception: return False

def _quota_pcts(q):
    w = h = None
    for k in ("friend", "ours"):
        for win in (q.get(k) or {}).get("windows", []):
            n = win.get("name", "").lower()
            if "week" in n and w is None: w = win.get("used_pct")
            if "5h" in n or "hour" in n and h is None: h = win.get("used_pct")
    if w is None or h is None:  # fallback: zai_state friend pct (both windows proxy)
        try:
            s = json.load(open(f"{HOME}/.hermes/bot/zai_state.json"))
            p = 100 - float(s.get("friend_token_pct", 100))
            w, h = (w if w is not None else p), (h if h is not None else p)
        except Exception: pass
    return w, h

def _paid_burn_1h():
    try:
        c = sqlite3.connect(f"file:{HOME}/.hermes/bot/api_burn.db?mode=ro", uri=True)
        row = c.execute("SELECT SUM(cost) FROM burn WHERE ts > ?",
                        (time.time() - 3600,)).fetchone()
        c.close()
        return float(row[0] or 0)
    except Exception:
        return None

RANK = {"free": 0, "cheap": 1, "medium": 2, "expensive": 3}

def eligible(urgency, tier, now=None):
    now = now or time.gmtime().tm_hour
    if urgency == "now": return True
    if urgency == "soon": return RANK[tier] <= RANK["medium"]
    if urgency == "defer": return RANK[tier] <= RANK["cheap"]
    if urgency == "batch":
        return tier == "free" or (tier == "cheap" and now in OFFPEAK)
    return False

# ---------- gates ----------
def park(board, tid, reason):
    r = kb(board, "schedule", tid, reason)
    ok = r.returncode == 0
    alert(("parked " if ok else "PARK FAILED ") + f"{tid} @ {board}: {reason}")
    return ok

def gate_create(argv):
    if "--urgency" in argv:
        lvl = argv[argv.index("--urgency") + 1].lower()
    elif sys.stdin.isatty():
        lvl = tty_ask(argv)
    else:
        sys.stderr.write(
            "URGENCY REQUIRED: pass --urgency now|soon|defer|batch "
            "(automation: set it explicitly or HERMES_URGENCY_EXEMPT=1, logged).\n")
        return 2
    if lvl not in LEVELS:
        sys.stderr.write(f"invalid --urgency '{lvl}' (use now|soon|defer|batch)\n"); return 2
    deadline = parse_deadline(argv) or (
        int(time.time()) + DEFAULT_SOON_H * 3600 if lvl == "soon" else None)
    args = strip_flags(argv, ("--urgency", "--urgency-deadline"))
    board = resolve_board(args)
    body_i = args.index("--body") + 1 if "--body" in args else None
    if body_i:
        args[body_i] = f"## Urgency: {lvl}\n" + args[body_i]
    if "--json" not in args: args.append("--json")
    r = run_real(["kanban"] + args)
    if r.returncode != 0:
        sys.stdout.write(r.stdout); sys.stderr.write(r.stderr); return r.returncode
    stamp(board, r, lvl, deadline, source="operator" if sys.stdin.isatty() else "manager")
    sys.stdout.write(r.stdout)
    return 0

def tty_ask(argv):
    t = price_tier()
    print(f"Token price now: {t['tier'].upper()} ({'; '.join(t['evidence'])})")
    print("  now   — dispatch regardless of price (bleed/deadline)")
    print("  soon  — hours; waits for medium-or-cheaper, auto-escalates at deadline")
    print("  defer — days; waits for cheap only")
    print("  batch — cheapest window only (free lane / off-peak)")
    while True:
        a = input("urgency [now/soon/defer/batch]: ").strip().lower()
        if a in LEVELS: return a
        print("? use now|soon|defer|batch")

def stamp(board, r, lvl, deadline, source):
    try:
        ids = [t["id"] for t in json.loads(r.stdout).get("tasks", [json.loads(r.stdout)])] \
            if False else [json.loads(r.stdout).get("id")]
    except Exception:
        ids = [row[0] for row in (sql(board, lambda c: c.execute(
            "SELECT id FROM tasks WHERE urgency IS NULL AND created_at > ? "
            "ORDER BY created_at DESC LIMIT 5", (int(time.time()) - 60,))) or [])]
    def w(conn):
        for tid in filter(None, ids):
            conn.execute("UPDATE tasks SET urgency=?, urgency_deadline=?, "
                         "urgency_set_at=?, urgency_source=? WHERE id=?",
                         (lvl, deadline, int(time.time()), source, tid))
        conn.commit()
    sql(board, w) and None
    log(f"classified {ids} @ {board} -> {lvl} (deadline={deadline}, {source})")

def gate_dispatch(argv):
    board = resolve_board(argv)
    try:
        t = price_tier()
        sql(board, migrate)
        ready = sql(board, lambda c: c.execute(
            "SELECT id, urgency FROM tasks WHERE status='ready'").fetchall()) or []
        for tid, urg in ready:
            if urg is None:
                park(board, tid, "urgency-unclassified: classify with "
                     f"hermes-urgency set {board} {tid} <now|soon|defer|batch>")
            elif not eligible(urg, t["tier"]):
                park(board, tid, f"price-hold: urgency={urg} not eligible at "
                     f"tier={t['tier']} ({'; '.join(t['evidence'])})")
    except Exception as e:
        log(f"dispatch-gate fail-open ({board}): {e}")  # never block dispatch on our bugs
    os.execv(REAL, [REAL] + argv)

def gate_promote(argv):
    board = resolve_board(argv)
    ids = [a for a in argv[3:] if a.startswith("t_")]
    ok = sql(board, lambda c: [i for (i,) in c.execute(
        "SELECT id FROM tasks WHERE id IN (%s) AND urgency IS NOT NULL" %
        ",".join("?" * len(ids)), ids)] or [])
    ok = [i for (i,) in (ok or [])]
    refuse = [i for i in ids if i not in ok]
    for tid in refuse:
        park(board, tid, "urgency-unclassified: promote refused until classified")
    if not ok:
        log(f"promote: nothing eligible ({ids} -> refused {refuse})"); return 0
    r = run_real(["kanban", "--board", board, "promote"] + ok)
    sys.stdout.write(r.stdout); sys.stderr.write(r.stderr)
    return r.returncode

# ---------- tick ----------
def tick():
    t = price_tier()
    log(f"tick tier={t['tier']} ({'; '.join(t['evidence'])})")
    enrolled = set()
    try: enrolled = {l.strip() for l in open(AUTODISPATCH) if l.strip()}
    except Exception: pass
    import glob as g
    for db in sorted(g.glob(f"{BOARDS}/*/kanban.db")):
        board = os.path.basename(os.path.dirname(db))
        try:
            sql(board, migrate)
            rows = sql(board, lambda c: c.execute(
                "SELECT id, status, urgency, urgency_deadline, created_at FROM tasks "
                "WHERE status IN ('ready','scheduled','todo')").fetchall()) or []
            for tid, st, urg, dl, cat in rows:
                if st == "ready" and urg is None:
                    park(board, tid, "urgency-unclassified (tick sweep)")
                elif st == "scheduled" and urg is not None and eligible(urg, t["tier"]):
                    kb(board, "unblock", tid, "--reason",
                       f"price window open: urgency={urg} tier={t['tier']}")
                    log(f"promoted {tid} @ {board} (tier={t['tier']})")
                elif (urg == "soon" and st != "running" and dl
                      and time.time() > dl):
                    def w(conn):
                        conn.execute("UPDATE tasks SET urgency='now', "
                                     "urgency_source='escalation', priority=MAX(priority,8) "
                                     "WHERE id=?", (tid,))
                        conn.commit()
                    sql(board, w)
                    kb(board, "comment", tid, "CG-11 escalation: SOON deadline passed "
                       "undelivered — promoted to NOW, dispatch next pass.")
                    alert(f"ESCALATED {tid} @ {board}: soon->now (deadline passed)")
            if board in enrolled:
                n = sql(board, lambda c: c.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status='ready'").fetchone())
                if n and n[0][0]:
                    kb(board, "dispatch", "--max", "1")
        except Exception as e:
            log(f"tick fail-open ({board}): {e}")

# ---------- helpers ----------
def parse_deadline(argv):
    if "--urgency-deadline" not in argv: return None
    v = argv[argv.index("--urgency-deadline") + 1]
    m = re.match(r"\+(\d+(?:\.\d+)?)h$", v)
    if m: return int(time.time() + float(m.group(1)) * 3600)
    try:
        import calendar
        return calendar.timegm(time.strptime(v, "%Y-%m-%dT%H:%M"))
    except Exception: return None

def strip_flags(argv, flags):
    out, skip = [], False
    for a in argv:
        if skip: skip = False; continue
        if any(a == f or a.startswith(f + "=") for f in flags):
            skip = "=" not in a; continue
        out.append(a)
    return out

def run_real(args):
    return subprocess.run([REAL] + args, capture_output=True, text=True, timeout=300)

def cmd_set(argv):  # hermes-urgency set <board> <id> <lvl> [--deadline X] [--note ...]
    board, tid, lvl = argv[0], argv[1], argv[2].lower()
    if lvl not in LEVELS: sys.exit(f"bad level {lvl}")
    dl = parse_deadence if False else parse_deadline(argv) or (
        int(time.time()) + DEFAULT_SOON_H * 3600 if lvl == "soon" else None)
    def w(conn):
        conn.execute("UPDATE tasks SET urgency=?, urgency_deadline=?, urgency_set_at=?, "
                     "urgency_source='operator' WHERE id=?", (lvl, dl, int(time.time()), tid))
        conn.commit()
    if sql(board, w) is None: sys.exit("db write failed")
    t = price_tier()
    r = sql(board, lambda c: c.execute(
        "SELECT status FROM tasks WHERE id=?", (tid,)).fetchone())
    if r and r[0][0] == "scheduled" and eligible(lvl, t["tier"]):
        kb(board, "unblock", tid, "--reason", f"classified {lvl}; tier={t['tier']} OK")
    log(f"set {tid} @ {board} -> {lvl} (deadline={dl})")
    return 0

def main():
    a = sys.argv[1:]
    if not a: sys.exit("usage: urgency_gate.py gate|tick|tier|set|park-and-hold ...")
    if a[0] == "gate":
        sub = a[2] if len(a) > 2 else ""
        return {"create": gate_create, "dispatch": gate_dispatch,
                "promote": gate_promote}.get(sub, lambda av: (
                    os.execv(REAL, [REAL] + a)))(a[2:])
    if a[0] == "tick": tick(); return 0
    if a[0] == "tier":
        t = price_tier(); print(t["tier"], "-", "; ".join(t["evidence"])); return 0
    if a[0] == "set": return cmd_set(a[1:])
    if a[0] == "park-and-hold":  # staggered-dispatch pre-check (always exit 0)
        board = resolve_board(a)
        try:
            t = price_tier()
            for tid, urg in (sql(board, lambda c: c.execute(
                    "SELECT id, urgency FROM tasks WHERE status='ready'").fetchall()) or []):
                if urg is None:
                    park(board, tid, "urgency-unclassified (staggered pre-check)")
                elif not eligible(urg, t["tier"]):
                    park(board, tid, f"price-hold: urgency={urg} tier={t['tier']}")
        except Exception as e:
            log(f"park-and-hold fail-open ({board}): {e}")
        return 0
    sys.exit(f"unknown subcommand {a[0]}")

if __name__ == "__main__":
    sys.exit(main() or 0)
```

*(Two intentionally-marked rough lines — `stamp()`'s id extraction and the `cmd_set`
deadline default — are spelled out plainly: implementers should simplify `stamp()` to
"tasks created in the last 60 s with NULL urgency on this board", which is the
already-implemented fallback path.)*

`~/.local/bin/hermes-urgency` (convenience shim):

```bash
#!/usr/bin/env bash
exec /usr/bin/python3 /home/c03rad0r/.hermes/scripts/urgency_gate.py "$@"
```

### 8.3 Wrapper diff

```bash
cp -a ~/.local/bin/hermes ~/.local/bin/hermes.pre-cg11   # rollback artifact
# then replace with the 8-line wrapper from §3
chmod +x ~/.local/bin/hermes
```

### 8.4 staggered-dispatch.sh diff (two lines)

In the dispatch loop, immediately before
`"$HERMES_BIN" kanban --board "$board" dispatch --max 1 ...`:

```bash
    /usr/bin/python3 "$HOME/.hermes/scripts/urgency_gate.py" park-and-hold --board "$board" || true
```

### 8.5 Cron

```bash
touch ~/.hermes/config/urgency-autodispatch-boards.txt   # ships EMPTY (opt-in)
( crontab -l 2>/dev/null; echo '*/10 * * * * /usr/bin/python3 /home/c03rad0r/.hermes/scripts/urgency_gate.py tick >> /home/c03rad0r/.hermes/logs/urgency-gate.log 2>&1 # cg11-urgency-tick' ) | crontab -
```

### 8.6 Verification (scratch board first, then live)

```bash
# 1. migration on a throwaway board
hermes kanban init --board cg11-scratch
python3 ~/.hermes/scripts/urgency_migrate.py
# 2. create refuses without urgency (non-TTY)
hermes kanban --board cg11-scratch create "T1" --body x; echo "rc=$?"        # expect rc=2 + message
# 3. create with urgency stamps column + body header
hermes kanban --board cg11-scratch create "T2" --body x --urgency defer --json
hermes-urgency tier                          # shows current tier + evidence
hermes kanban --board cg11-scratch dispatch --dry-run
#    during expensive window: expect T2 parked (price-hold) + ALERT in log
hermes-urgency set cg11-scratch <id> now     # immediate eligibility
# 4. escalation drill: set a soon task with --deadline +0.02h, wait for tick, expect alert
# 5. staggered path: run staggered-dispatch.sh once, confirm park-and-hold line in log
# 6. rm -rf scratch board; enroll real boards in urgency-autodispatch-boards.txt one at a time
```

### 8.7 Rollback

1. `cp -a ~/.local/bin/hermes.pre-cg11 ~/.local/bin/hermes`
2. remove the `cg11-urgency-tick` crontab line
3. revert the two-line staggered diff
4. leave columns/data in place (inert) — or `ALTER TABLE tasks DROP COLUMN urgency` on
   SQLite ≥ 3.35 if desired. Parked tasks recover via `hermes kanban unblock`.

---

## 9. Highest-leverage change

**Layer 2 — the dispatch-time park of `urgency IS NULL` ready tasks, installed in both
the wrapper and staggered-dispatch.** It is the only control that catches *every*
creation path (interactive, wrapper-bypassing cron, in-process decompose, importers,
watchdog unblocks) because it checks the single thing they all produce: a `ready` task.
Create-path asking (Layer 1) is UX; price tiers (Layer 3) are optimization. Layer 2
alone would have prevented the 2026-08-22 bleed.
