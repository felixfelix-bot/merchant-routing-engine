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

def ppq_live():
    remote = ("K=$(cat ~/routstr-public/secrets/ppq_key.txt 2>/dev/null); "
              "A=\"Authorization: Bearer ${K}\"; "
              "curl -s -X POST https://api.ppq.ai/credits/balance "
              "-H \"$A\" -H \"Content-Type: application/json\" "
              "-d '{}' --max-time 12")
    out, rc = ssh(remote, timeout=30)
    if rc != 0 or not out.strip():
        return None
    try:
        bal = float(json.loads(out.strip().splitlines()[-1]).get("balance"))
    except Exception:
        return None
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    s = f"ppq balance LIVE: ${bal:.2f} (pulled {ts})"
    if bal < 1.00:
        s += " LOW — top up soon"
    return s

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
    # R1-R4 chain-pricing model (30% margin on revenue => fee = 1.43). The margin
    # lives ONCE on the selling hop; downstream passthrough hops normalize to 1.0.
    # So a valid chain totals 1.43, and a provisional fee below ~1.27 on a
    # passthrough/1.0 hop is EXPECTED (not a margin violation) under R4. The flag
    # below only lists provider_fee values that exceed the 30% cap (nothing > 1.43).
    bad = [p for p in provs if isinstance(p, list) and len(p) > 2 and p[2] and p[1] is not None and float(p[1]) > 1.43]
    print("providers:", provs, "| MARGIN OK" if not bad else f"| !! ENABLED FEE ABOVE 1.43 (over-margin): {bad}")
    enabled = [p for p in provs if isinstance(p, list) and len(p) > 2 and p[2]]
    if not enabled:
        print("SERVING: DEAD — ALL PROVIDERS DISABLED (node cannot serve customers)")
    else:
        print(f"serving: {len(enabled)} provider(s) enabled")
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

# R1-R4 CHAIN SUMMARY — compounded end-to-end markup across both nodes
# (public selling hop x friends passthrough hop; settings blobs now normalized
# to exchange_fee x upstream_provider_fee = 1.0 x 1.0). Docs: ROUTSTR-COMPETITIVE-PRICING.md.
try:
    pub_fe = {p[0]: float(p[1]) for p in state.get("routstr-public", {}).get("providers", []) if len(p) > 1 and p[1] is not None}
    prox_fe = {p[0]: float(p[1]) for p in state.get("routstr-proxy", {}).get("providers", []) if len(p) > 1 and p[1] is not None}
    selling = pub_fe.get("zai-proxy", None)
    passthru = prox_fe.get("zai-proxy-tunnel", None)
    if selling is not None and passthru is not None:
        chain = selling * passthru
        print(f"\nchain: public zai-proxy {selling} x friends zai-proxy-tunnel {passthru} = {chain:.3f} (settings 1.0x1.0) | "
              f"{'MARGIN OK (1.43)' if abs(chain-1.43) < 0.01 else f'!! NOT 1.43 ({chain:.3f})'}")
    else:
        print("\nchain: could not compute (selling/passthrough hop missing)")
except Exception as e:
    print("\nchain: compute error:", str(e)[:80])

print("\n--- INFRA ---")
print("TLS routstr.orangesync.tech/v1/models:", https_code("https://routstr.orangesync.tech/v1/models"))
print("hermes2 64.188.7.239 ssh:", tcp_ok("64.188.7.239"))
print("hermes 23.182.128.219 ssh:", tcp_ok("23.182.128.219"))
live = ppq_live()
if live:
    print(live)
else:
    print("ppq LIVE pull FAILED, cache fallback:", ppq_cache())

os.makedirs(os.path.dirname(STATE), exist_ok=True)
tmp = STATE + ".tmp"
json.dump(state, open(tmp, "w"))
os.replace(tmp, STATE)
print("\n[state snapshot saved]")
