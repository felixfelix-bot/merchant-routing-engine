#!/usr/bin/env python3
"""Ollama /api/usage shape canary — sunset + credit-pool drift detector.

Silent cron watchdog (empty stdout = nothing to deliver). Ollama's migration
to credit-pool plans may change /api/usage — the data.limits.session/weekly
shape consumed by src/ollama_extra_usage.py — without notice. Each run
fingerprints the response (HTTP status, sorted top-level keys, session/weekly
presence, and any credit|pool|allowance|balance|entitlement key name: the
credit-pool sunset signal) and diffs it against the previous run in the
state file ($OLLAMA_CANARY_STATE else
~/.merchant-routing/ollama-usage-shape-state.json).

Always exit 0. First run -> "baseline recorded: <fingerprint>"; no drift ->
empty stdout; drift -> old/new shape + timestamp report; fetch failure ->
silent, consecutive-failure counter++ (no diff, no false drift), and only
2 IN A ROW -> "unreachable N runs in a row: <err>". Never prints the API
key, auth header, or raw body — fingerprints only. Stdlib only.
"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

API_URL = "https://ollama.com/api/usage"
DEFAULT_STATE = Path("~/.merchant-routing/ollama-usage-shape-state.json").expanduser()
# Key names that would signal new credit-pool plans leaking into the API.
CREDIT_TOKENS = ("credit", "pool", "allowance", "balance", "entitlement")


def fetch_usage(api_key=None):
    """GET /api/usage with Bearer auth -> (http_status, parsed_json)."""
    api_key = api_key or os.environ.get("OLLAMA_CLOUD_API_KEY", "")
    if not api_key:
        raise RuntimeError("OLLAMA_CLOUD_API_KEY not set")
    req = urllib.request.Request(API_URL, headers={"Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=5) as resp:  # 5s watchdog timeout
        return getattr(resp, "status", 200), json.loads(resp.read())


def shape_fingerprint(status, data):
    """Shape only — usage values never enter the fingerprint (they change
    every run; only status/keys/presence/credit-fields = interface change)."""
    credit = []  # dotted paths of any credit-like key, recursive (case-blind)

    def walk(node, path=""):
        if isinstance(node, dict):
            for k, v in node.items():
                p = f"{path}.{k}" if path else k
                if any(t in str(k).lower() for t in CREDIT_TOKENS):
                    credit.append(p)
                walk(v, p)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(data)
    d = data if isinstance(data, dict) else {}
    inner = d.get("data") if isinstance(d.get("data"), dict) else {}
    limits = next((x for x in (d.get("limits"), inner.get("limits"))
                   if isinstance(x, dict)), {})
    return {"http_status": status, "top_level_keys": sorted(d),
            "session_limit": "session" in limits,
            "weekly_limit": "weekly" in limits,
            "credit_like_keys": credit}


def _oneline(fp):
    c = ",".join(fp["credit_like_keys"]) or "none"
    return (f"status={fp['http_status']} keys={fp['top_level_keys']} "
            f"session={fp['session_limit']} weekly={fp['weekly_limit']} credit_like={c}")


def _save(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True))


def run(state_path, fetch=None):
    """One canary run -> message to print ("" = stay silent). Never raises."""
    try:
        state = json.loads(state_path.read_text())
        if not isinstance(state, dict):
            raise ValueError
    except (OSError, ValueError):
        state = {"last_fingerprint": None, "consecutive_failures": 0}
    try:
        if fetch is None:                       # late lookup: patchable in tests
            fetch = fetch_usage
        status, data = fetch()
    except Exception as e:                      # network/HTTP/JSON: no diff
        state["consecutive_failures"] = n = state.get("consecutive_failures", 0) + 1
        _save(state_path, state)
        return f"unreachable {n} runs in a row: {e}" if n >= 2 else ""

    fp = shape_fingerprint(status, data)
    prev, iso = state.get("last_fingerprint"), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state.update(last_fingerprint=fp, consecutive_failures=0, last_success_iso=iso)
    _save(state_path, state)
    if prev is None:
        return f"baseline recorded: {_oneline(fp)}"
    if fp != prev:
        return (f"OLLAMA USAGE API SHAPE DRIFT at {iso}\n"
                f"  old: {_oneline(prev)}\n  new: {_oneline(fp)}\n"
                f"  (ollama.com may be migrating /api/usage to credit-pool plans —\n"
                f"   check the src/ollama_extra_usage.py consumer)")
    return ""


def main():
    msg = run(Path(os.environ.get("OLLAMA_CANARY_STATE") or DEFAULT_STATE))
    if msg:
        print(msg)
    sys.exit(0)


if __name__ == "__main__":
    main()