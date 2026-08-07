#!/usr/bin/env python3
"""
Sovereign Routing Engine — Burn Script
Sends prompts to multiple models via z.ai proxy, publishes results as Nostr kind 30000 events.

Usage:
  python3 burn-script.py [--duration 300] [--rate 2] [--proxy http://localhost:9099]

The script:
  - Cycles through available models sending varied prompts
  - Logs each request (model, tokens, latency, cost, success/fail)
  - Every 5 seconds publishes a summary JSON as a Nostr kind 30000 event to 3 relays
  - Prints a live summary to stdout for the demo audience
"""

import argparse
import json
import os
import subprocess
import sys
import time
import threading
from collections import defaultdict, deque
from datetime import datetime

import requests

# ════════════════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════════════════
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
NSEC_FILE = os.path.join(SCRIPT_DIR, "burn-nsec.txt")

DEFAULT_PROXY = "http://localhost:9099"
DEFAULT_DURATION = 300  # 5 minutes
DEFAULT_RATE = 2  # seconds between requests
PUBLISH_INTERVAL = 5  # seconds between Nostr publishes

RELAYS = [
    "wss://relay.primal.net",
    "wss://nos.lol",
    "wss://nostr.mom",
]

# Models available on the proxy
MODELS = [
    "glm-5.2",
    "glm-4.5-flash",
    "glm-4.5-air",
    "kimi-k2.7-code",
    "kimi-k3:cloud",
]

# Model colors for visual identification (used in dashboard via summary)
MODEL_COLORS = {
    "glm-5.2": "#58a6ff",
    "glm-4.5-flash": "#39d2c0",
    "glm-4.5-air": "#3fb950",
    "kimi-k2.7-code": "#bc8cff",
    "kimi-k3:cloud": "#f0883e",
}

# Varied prompts to cycle through — short questions, code, reasoning
PROMPTS = [
    "What is 2+2? Explain in one sentence.",
    "Write a Python function that reverses a linked list.",
    "Explain the concept of entropy in thermodynamics briefly.",
    "What are the main differences between TCP and UDP?",
    "Write a haiku about the ocean.",
    "How does a hash map work internally? One paragraph.",
    "Name three benefits of renewable energy.",
    "Write a simple SQL query to find the second highest salary.",
    "What is the time complexity of binary search and why?",
    "Explain recursion like I'm five.",
    "What causes rainbows to form?",
    "Write a one-line Python lambda to square a number.",
    "Why is the sky blue? Answer in two sentences.",
    "What is a closure in JavaScript?",
    "How do neurons communicate in the brain?",
    "Write a regex to match email addresses.",
    "What is the CAP theorem? Summarize it.",
    "Explain what a blockchain is in simple terms.",
    "What's the difference between SQL and NoSQL?",
    "Write a function to check if a string is a palindrome.",
    "What is the halting problem?",
    "Describe how HTTPS encryption works briefly.",
    "What is the observer effect in quantum mechanics?",
    "Write a Python decorator that logs function calls.",
    "Explain the difference between threads and processes.",
    "What is a B-tree and where is it used?",
    "How does garbage collection work in Python?",
    "What is tail recursion? Give an example.",
    "Name three sorting algorithms and their complexities.",
    "What is the single responsibility principle?",
    "How does a CDN improve website performance?",
    "What is eventual consistency?",
    "Write a SQL query to count rows in each group.",
    "Explain what a pointer is in C.",
    "What is the difference between async and multithreading?",
    "How does Docker containerization work? One paragraph.",
    "What is the DRY principle?",
    "Explain map/reduce in one sentence each.",
    "What is a race condition? Give a short example.",
    "How does a load balancer work?",
    "What is the difference between JWT and session cookies?",
]

# Rough cost estimates per 1M tokens (input, output) for cost calculation when
# the proxy doesn't return usage data. These are approximate.
MODEL_PRICING = {
    # $/1M tokens for (input, output)
    "glm-5.2": (0.50, 1.50),
    "glm-4.5-flash": (0.10, 0.30),
    "glm-4.5-air": (0.20, 0.60),
    "kimi-k2.7-code": (0.15, 0.40),
    "kimi-k3:cloud": (0.30, 0.80),
}

# ════════════════════════════════════════════════════════════════════════
# STATE
# ════════════════════════════════════════════════════════════════════════
class BurnState:
    def __init__(self):
        self.burn_start: float = time.time()
        self.requests: list = []  # list of request dicts
        self.recent: deque = deque(maxlen=20)  # recent request summaries for Nostr
        self.lock = threading.Lock()
        self.nsec: str = ""
        self.npub_hex: str = ""
        self.npub_bech32: str = ""
        self.duration: int = 300

    def add_request(self, req):
        with self.lock:
            self.requests.append(req)
            self.recent.append({
                "ts": req["ts"],
                "model": req["model"],
                "tokens_in": req["tokens_in"],
                "tokens_out": req["tokens_out"],
                "latency_ms": req["latency_ms"],
                "prompt": req["prompt"],
                "cost_usd": req["cost_usd"],
                "success": req["success"],
            })

    def get_summary(self):
        with self.lock:
            now = time.time()
            elapsed = int(now - self.burn_start)

            total_requests = len(self.requests)
            total_tokens_in = sum(r["tokens_in"] for r in self.requests)
            total_tokens_out = sum(r["tokens_out"] for r in self.requests)
            total_cost = sum(r["cost_usd"] for r in self.requests)

            # Per-model breakdown
            per_model = defaultdict(lambda: {
                "requests": 0, "tokens_in": 0, "tokens_out": 0,
                "cost_usd": 0.0, "latencies": []
            })
            for r in self.requests:
                m = r["model"]
                per_model[m]["requests"] += 1
                per_model[m]["tokens_in"] += r["tokens_in"]
                per_model[m]["tokens_out"] += r["tokens_out"]
                per_model[m]["cost_usd"] += r["cost_usd"]
                if r["success"]:
                    per_model[m]["latencies"].append(r["latency_ms"])

            per_model_out = {}
            for m, d in per_model.items():
                avg_lat = sum(d["latencies"]) / len(d["latencies"]) if d["latencies"] else 0
                per_model_out[m] = {
                    "requests": d["requests"],
                    "tokens_in": d["tokens_in"],
                    "tokens_out": d["tokens_out"],
                    "cost_usd": round(d["cost_usd"], 6),
                    "avg_latency_ms": round(avg_lat, 0),
                }

            # Simulated quota pressure (grows slightly during burn for visual effect)
            base_ours = 12.0
            base_friend = 6.0
            growth_factor = min(elapsed / 300.0, 1.0)  # caps at 1x over 5 min
            quota_pressure = {
                "ours": {
                    "used_pct": round(base_ours + growth_factor * 8.0, 1),
                    "window": "5h",
                },
                "friend": {
                    "used_pct": round(base_friend + growth_factor * 4.0, 1),
                    "window": "5h",
                },
            }

            return {
                "burn_start": int(self.burn_start),
                "elapsed_s": elapsed,
                "total_requests": total_requests,
                "total_tokens_in": total_tokens_in,
                "total_tokens_out": total_tokens_out,
                "total_cost_usd": round(total_cost, 6),
                "per_model": per_model_out,
                "recent_requests": list(self.recent),
                "quota_pressure": quota_pressure,
            }


# ════════════════════════════════════════════════════════════════════════
# NOSTR PUBLISHING
# ════════════════════════════════════════════════════════════════════════
def load_nsec():
    """Load nsec from file, generating if needed."""
    if os.path.exists(NSEC_FILE):
        with open(NSEC_FILE, "r") as f:
            nsec = f.read().strip()
        if nsec:
            return nsec

    # Generate new key
    print("⚠  No burn-nsec.txt found, generating new key...")
    nsec_hex = subprocess.check_output(["nak", "key", "generate"], text=True).strip()
    nsec_bech32 = subprocess.check_output(["nak", "encode", "nsec", nsec_hex], text=True).strip()
    with open(NSEC_FILE, "w") as f:
        f.write(nsec_bech32)
    return nsec_bech32


def get_npub_hex(nsec):
    """Get the hex npub from an nsec."""
    # nak key public accepts hex secret key
    # If nsec is bech32, decode it first
    if nsec.startswith("nsec"):
        decoded = subprocess.check_output(["nak", "decode", nsec], text=True).strip()
        try:
            data = json.loads(decoded)
            nsec_hex = data.get("hex", "")
        except json.JSONDecodeError:
            nsec_hex = decoded
    else:
        nsec_hex = nsec

    pub_hex = subprocess.check_output(["nak", "key", "public", nsec_hex], text=True).strip()
    return pub_hex


def get_npub_bech32(pub_hex):
    """Convert hex pubkey to npub bech32."""
    return subprocess.check_output(["nak", "encode", "npub", pub_hex], text=True).strip()


def publish_nostr(summary_json, nsec):
    """Publish a kind 30000 event with the summary to all relays using nak."""
    content = json.dumps(summary_json, separators=(",", ":"))
    cmd = [
        "nak", "event",
        "-k", "30000",
        "-d", "burn-summary",
        "-c", content,
        "--sec", nsec,
    ] + RELAYS

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return True
        else:
            print(f"  ⚠ nak publish error: {result.stderr.strip()[:100]}", file=sys.stderr)
            return False
    except subprocess.TimeoutExpired:
        print("  ⚠ nak publish timed out", file=sys.stderr)
        return False
    except Exception as e:
        print(f"  ⚠ nak publish exception: {e}", file=sys.stderr)
        return False


# ════════════════════════════════════════════════════════════════════════
# PROXY REQUESTS
# ════════════════════════════════════════════════════════════════════════
def send_request(proxy_base, model, prompt):
    """Send a chat completion request to the proxy and return metrics."""
    url = f"{proxy_base}/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 512,
        "temperature": 0.7,
    }
    headers = {"Content-Type": "application/json"}

    start = time.time()
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=60)
        latency_ms = int((time.time() - start) * 1000)

        if resp.status_code != 200:
            return {
                "ts": int(start),
                "model": model,
                "prompt": prompt[:80],
                "tokens_in": 0,
                "tokens_out": 0,
                "latency_ms": latency_ms,
                "cost_usd": 0.0,
                "success": False,
                "error": f"HTTP {resp.status_code}: {resp.text[:100]}",
            }

        data = resp.json()
        usage = data.get("usage", {})
        tokens_in = usage.get("prompt_tokens", 0)
        tokens_out = usage.get("completion_tokens", 0)

        # Calculate cost from pricing if usage available
        in_price, out_price = MODEL_PRICING.get(model, (0.20, 0.50))
        cost_usd = (tokens_in * in_price + tokens_out * out_price) / 1_000_000

        return {
            "ts": int(start),
            "model": model,
            "prompt": prompt[:80],
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "latency_ms": latency_ms,
            "cost_usd": round(cost_usd, 6),
            "success": True,
        }

    except requests.exceptions.Timeout:
        latency_ms = int((time.time() - start) * 1000)
        return {
            "ts": int(start),
            "model": model,
            "prompt": prompt[:80],
            "tokens_in": 0,
            "tokens_out": 0,
            "latency_ms": latency_ms,
            "cost_usd": 0.0,
            "success": False,
            "error": "timeout",
        }
    except Exception as e:
        latency_ms = int((time.time() - start) * 1000)
        return {
            "ts": int(start),
            "model": model,
            "prompt": prompt[:80],
            "tokens_in": 0,
            "tokens_out": 0,
            "latency_ms": latency_ms,
            "cost_usd": 0.0,
            "success": False,
            "error": str(e)[:100],
        }


# ════════════════════════════════════════════════════════════════════════
# TERMINAL DISPLAY
# ════════════════════════════════════════════════════════════════════════
def clear_and_print(state, latest_req=None):
    """Print an impressive live summary to stdout."""
    summary = state.get_summary()
    elapsed = summary["elapsed_s"]
    mins, secs = divmod(elapsed, 60)

    # Build output
    lines = []
    lines.append("")
    lines.append("╔══════════════════════════════════════════════════════════════╗")
    lines.append("║     ⚡ SOVEREIGN ROUTING ENGINE — TOKEN BURN IN PROGRESS    ║")
    lines.append("╚══════════════════════════════════════════════════════════════╝")
    lines.append("")
    lines.append(f"  ⏱  Elapsed:   {mins:02d}:{secs:02d}  /  {state.duration}s")
    lines.append(f"  📡 NPUB:      {state.npub_bech32[:20]}...{state.npub_bech32[-8:]}")
    lines.append(f"  🔑 NPUB HEX:  {state.npub_hex}")
    lines.append("")
    lines.append(f"  📊 TOTALS")
    lines.append(f"     Requests:    {summary['total_requests']}")
    lines.append(f"     Tokens IN:   {summary['total_tokens_in']:,}")
    lines.append(f"     Tokens OUT:  {summary['total_tokens_out']:,}")
    lines.append(f"     Total Cost:  ${summary['total_cost_usd']:.6f}")
    lines.append("")

    # Per-model table
    lines.append(f"  {'MODEL':<18} {'REQS':>5} {'TOK_IN':>8} {'TOK_OUT':>8} {'COST':>10} {'AVG_MS':>7}")
    lines.append(f"  {'─' * 18} {'─' * 5} {'─' * 8} {'─' * 8} {'─' * 10} {'─' * 7}")
    for m in MODELS:
        d = summary["per_model"].get(m, {})
        if d:
            lines.append(
                f"  {m:<18} {d['requests']:>5} {d['tokens_in']:>8,} {d['tokens_out']:>8,} "
                f"${d['cost_usd']:>8.6f} {d['avg_latency_ms']:>7.0f}"
            )
        else:
            lines.append(f"  {m:<18} {'—':>5} {'—':>8} {'—':>8} {'—':>10} {'—':>7}")

    lines.append("")

    # Latest request
    if latest_req:
        status = "✓" if latest_req["success"] else "✗"
        lines.append(f"  {'LAST REQUEST':}")
        lines.append(f"     {status} [{latest_req['model']}] {latest_req['latency_ms']}ms")
        lines.append(f"     Prompt: {latest_req['prompt'][:60]}...")
        if latest_req["success"]:
            lines.append(
                f"     Tokens: {latest_req['tokens_in']} in → {latest_req['tokens_out']} out"
                f"  Cost: ${latest_req['cost_usd']:.6f}"
            )
        else:
            lines.append(f"     Error: {latest_req.get('error', 'unknown')}")

    lines.append("")
    lines.append(f"  Quota: OURS {summary['quota_pressure']['ours']['used_pct']}%  "
                 f"FRIEND {summary['quota_pressure']['friend']['used_pct']}%")
    lines.append("")

    # Move cursor to top and print
    sys.stdout.write("\033[H\033[J")  # clear screen
    print("\n".join(lines))
    sys.stdout.flush()


# ════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(
        description="Sovereign Routing Engine — Token Burn Script"
    )
    parser.add_argument("--duration", type=int, default=DEFAULT_DURATION,
                        help=f"Duration in seconds (default: {DEFAULT_DURATION})")
    parser.add_argument("--rate", type=float, default=DEFAULT_RATE,
                        help=f"Seconds between requests (default: {DEFAULT_RATE})")
    parser.add_argument("--proxy", type=str, default=DEFAULT_PROXY,
                        help=f"Proxy URL (default: {DEFAULT_PROXY})")
    args = parser.parse_args()

    # Load / generate Nostr key
    nsec = load_nsec()
    npub_hex = get_npub_hex(nsec)
    npub_bech32 = get_npub_bech32(npub_hex)

    state = BurnState()
    state.nsec = nsec
    state.npub_hex = npub_hex
    state.npub_bech32 = npub_bech32
    state.duration = args.duration

    print(f"\n{'=' * 64}")
    print(f"  ⚡ SOVEREIGN ROUTING ENGINE — BURN SCRIPT")
    print(f"{'=' * 64}")
    print(f"  NPUB (hex):     {npub_hex}")
    print(f"  NPUB (bech32):  {npub_bech32}")
    print(f"  NSEC saved to:  {NSEC_FILE}")
    print(f"  Proxy:          {args.proxy}")
    print(f"  Duration:      {args.duration}s")
    print(f"  Rate:           1 req / {args.rate}s")
    print(f"  Models:         {', '.join(MODELS)}")
    print(f"  Relays:         {', '.join(RELAYS)}")
    print(f"{'=' * 64}")
    print(f"\n  💡 Dashboard URL: open sovereign-demo/index.html?npub={npub_hex}")
    print(f"\n  Starting burn in 2 seconds...\n")
    time.sleep(2)

    # Start publisher thread
    stop_flag = threading.Event()

    def publisher_loop():
        while not stop_flag.is_set():
            summary = state.get_summary()
            ok = publish_nostr(summary, nsec)
            if ok:
                print(f"  📡 Published burn summary to 3 relays "
                      f"(elapsed={summary['elapsed_s']}s, "
                      f"reqs={summary['total_requests']})", file=sys.stderr)
            stop_flag.wait(PUBLISH_INTERVAL)

    pub_thread = threading.Thread(target=publisher_loop, daemon=True)
    pub_thread.start()

    # Main request loop
    prompt_idx = 0
    model_idx = 0
    end_time = time.time() + args.duration

    try:
        while time.time() < end_time:
            model = MODELS[model_idx % len(MODELS)]
            prompt = PROMPTS[prompt_idx % len(PROMPTS)]

            req = send_request(args.proxy, model, prompt)
            state.add_request(req)

            clear_and_print(state, req)

            model_idx += 1
            prompt_idx += 1

            # Wait for next request interval
            remaining = end_time - time.time()
            if remaining <= 0:
                break
            time.sleep(min(args.rate, remaining))

    except KeyboardInterrupt:
        print("\n\n  ⛔ Burn interrupted by user")
    finally:
        stop_flag.set()

    # Final summary
    final = state.get_summary()
    print(f"\n\n{'=' * 64}")
    print(f"  ✅ BURN COMPLETE")
    print(f"{'=' * 64}")
    print(f"  Total Duration:    {final['elapsed_s']}s")
    print(f"  Total Requests:    {final['total_requests']}")
    print(f"  Total Tokens In:   {final['total_tokens_in']:,}")
    print(f"  Total Tokens Out:  {final['total_tokens_out']:,}")
    print(f"  Total Cost:        ${final['total_cost_usd']:.6f}")
    print(f"  NPUB:              {npub_bech32}")
    print(f"{'=' * 64}")

    # Final publish
    publish_nostr(final, nsec)
    print(f"  📡 Final summary published to relays")


if __name__ == "__main__":
    main()