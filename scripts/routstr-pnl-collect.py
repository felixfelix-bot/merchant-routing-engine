#!/usr/bin/env python3
"""routstr-pnl-collect.py — daily data pull for the routstr P&L cron job.
Collects: per-node earnings/usage (VPS testserver2 routstr-public + routstr-proxy),
fee multipliers, TLS health, VPS reachability, PPQ balance cache, day-over-day deltas.
Output: plain-text report for the LLM cron agent to summarize. Never prints secrets/tokens.
"""
import json, os, socket, sqlite3, subprocess, sys, tempfile, urllib.request, urllib.error
from datetime import datetime, timezone

VPS = "debian@23.182.128.51"
KEY = os.path.expanduser("~/.ssh/id_ed25519")
STATE = os.path.expanduser("~/.hermes/profiles/manager/cron/state/routstr-pnl-state.json")
CONTAINERS = ["routstr-public", "routstr-proxy"]
now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def ssh(cmd, timeout=60):
    r = subprocess.run(["ssh", "-i", KEY, "-o", "StrictHostKeyChecking=no",
                        "-o", "ConnectTimeout=10", VPS, cmd],
                       capture_output=True, text=True, timeout=timeout)
    return r.stdout, r.returncode

SQL = """
import sqlite3, json
con = sqlite3.connect('/tmp/pnl.db')
con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
c = con.cursor()
def rows(q):
    try: return c.execute(q).fetchall()
    except Exception as e: return [["ERR", str(e)[:80]]]
out = {}
out['providers'] = rows("SELECT slug, provider_fee, enabled FROM upstream_providers")
out['keys'] = rows("SELECT COUNT(*), ROUND(SUM(balance),1), ROUND(SUM(total_spent),3), SUM(total_requests) FROM api_keys")
out['top_keys'] = rows("SELECT substr(hashed_key,1,8), ROUND(total_spent,2), total_requests, ROUND(balance,1) FROM api_keys ORDER BY total_spent DESC LIMIT 5")
out['tx_types'] = rows("SELECT type, COUNT(*), ROUND(SUM(amount),1) FROM cashu_transactions GROUP BY type")
out['fees'] = rows("SELECT accumulated_msats, total_paid_msats FROM routstr_fees")
out['tx_recent'] = rows("SELECT created_at, type, ROUND(amount,1) FROM cashu_transactions ORDER BY created_at DESC LIMIT 10")
out['key_activity'] = rows("SELECT substr(hashed_key,1,8), total_requests, ROUND(total_spent,3), reserved_at FROM api_keys WHERE total_requests > 0 ORDER BY reserved_at DESC LIMIT 8")
print(json.dumps(out))
"""

def collect_container(name):
    # copy db+wal+shm to VPS /tmp, open copy read-write (replays WAL), query, cleanup
    q = SQL.replace("'", "'\\''")
    cmd = (f"docker cp {name}:/app/data/keys.db /tmp/pnl.db 2>/dev/null && "
           f"docker cp {name}:/app/data/keys.db-wal /tmp/pnl.db-wal 2>/dev/null; "
           f"docker cp {name}:/app/data/keys.db-shm /tmp/pnl.db-shm 2>/dev/null; "
           f"python3 -c '{q}'; rc=$?; rm -f /tmp/pnl.db*; exit $rc")
    out, rc = ssh(cmd)
    try:
        return json.loads(out.strip().splitlines()[-1]), rc
    except Exception:
        return {"error": out.strip()[:200] or "no output", "rc": rc}, rc

def https_code(url):
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=12) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        return f"FAIL {str(e)[:60]}"

def tcp_ok(ip, port=22, t=6):
    try:
        with socket.create_connection((ip, port), timeout=t):
            return "up"
    except Exception as e:
        return f"DOWN {str(e)[:40]}"

def ppq_cache():
    db = os.path.expanduser("~/hermes-bot/api_burn.db")
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        r = con.execute("SELECT ts, balance_usd, error FROM balance_snapshots WHERE provider='ppq' AND balance_usd IS NOT NULL ORDER BY ts DESC LIMIT 1").fetchone()
        if r:
            return f"ppq balance ${r[1]} as of {datetime.fromtimestamp(r[0], tz=timezone.utc).strftime('%m-%d %H:%M')}"
        r2 = con.execute("SELECT error FROM balance_snapshots WHERE provider='ppq' ORDER BY ts DESC LIMIT 1").fetchone()
        return f"ppq cache: no balance rows; last err: {(r2[0] if r2 else 'n/a')[:60]}"
    except Exception as e:
        return f"ppq cache unreadable: {str(e)[:60]}"

print(f"=== ROUTSTR PNL DATA {now_iso} ===")
state = {}
try:
    state = json.load(open(STATE))
except Exception:
    pass

for c in CONTAINERS:
    data, rc = collect_container(c)
    print(f"\n--- {c} ---")
    prev = state.get(c, {})
    if "error" in data:
        print("COLLECT ERROR:", data["error"]); continue
    provs = data.get("providers", [])
    bad = [p for p in provs if isinstance(p, list) and len(p) > 1 and p[1] is not None and float(p[1]) < 1.27]
    print("providers:", provs, "| MARGIN OK" if not bad else f"| !! FEE BELOW 1.27: {bad}")
    k = data.get("keys", [[]])[0]
    if k and k[0] != "ERR":
        _pk = prev.get("keys") or []
        pk = _pk[0] if (_pk and isinstance(_pk[0], list) and len(_pk[0]) >= 4) else [None, None, None, None]
        print(f"keys: {k[0]} | held {k[1]} sat | lifetime spent {k[2]} sat | {k[3]} reqs")
        if pk and pk[2] is not None:
            print(f"delta since last run: spent +{round(k[2]-pk[2],3)} sat, reqs +{(k[3] or 0)-(pk[3] or 0)}")
    print("top keys (hash8, spent, reqs, balance):", data.get("top_keys"))
    print("cashu tx by type (type,n,amount):", data.get("tx_types"))
    print("operator fee pool msats:", data.get("fees"))
    print("recent tx:", data.get("tx_recent"))
    state[c] = data

print("\n--- INFRA ---")
print("TLS routstr.orangesync.tech/v1/models:", https_code("https://routstr.orangesync.tech/v1/models"))
print("hermes2 64.188.7.239 ssh:", tcp_ok("64.188.7.239"))
print("hermes 23.182.128.219 ssh:", tcp_ok("23.182.128.219"))
print(ppq_cache())

os.makedirs(os.path.dirname(STATE), exist_ok=True)
tmp = STATE + ".tmp"
json.dump(state, open(tmp, "w"))
os.replace(tmp, STATE)
print("\n[state snapshot saved]")
