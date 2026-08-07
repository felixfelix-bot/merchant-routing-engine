#!/usr/bin/env python3
"""
burn_demo.py — Sovereign Engineering Token Burn Demo

Sends varied prompts to the routing proxy at localhost:9099 using all 5 models,
logs results, and writes a JSON snapshot after each request.
"""

import json
import os
import random
import sqlite3
import sys
import time
import urllib.request
import urllib.error

# ─── Config ───────────────────────────────────────────────────────────────────
PROXY_URL = "http://localhost:9099/v1/chat/completions"
MODELS = ["glm-5.2", "glm-4.5-flash", "glm-4.5-air", "kimi-k2.7-code", "kimi-k3:cloud"]
SNAPSHOT_PATH = os.path.expanduser("~/merchant-routing-engine/demo/demo-snapshot.json")
DB_PATH = os.path.expanduser("~/.hermes/bot/zai_usage.db")
MAX_TOKENS = 200
MIN_DELAY = 2.0
MAX_DELAY = 3.0

# ─── Curated Prompts (15 varied) ──────────────────────────────────────────────
PROMPTS = [
    # Coding questions
    "Write a Python function that reverses a linked list. Explain the algorithm briefly.",
    "What's the difference between async/await and threading in Python? Give a short code example.",
    "Explain Big O notation with 3 examples of different complexities.",
    "Write a SQL query to find the top 5 customers by total order value in the last 30 days.",
    # Math
    "What is the derivative of f(x) = x^3 * ln(x)? Show the steps using product rule.",
    "Solve: if 3x + 7 = 22, what is x? Then find x^2 + 2x.",
    # Creative writing
    "Write a 3-sentence sci-fi story about an AI that discovers it's running inside a simulation.",
    "Compose a haiku about distributed systems and network partitions.",
    # Simple chat
    "Hello! How are you doing today? What's something interesting you can tell me?",
    "What's the weather like in a parallel universe where it rains diamonds?",
    # Philosophical
    "If consciousness is an emergent property of computation, does a sufficiently complex neural network have subjective experience?",
    "Is it better to be a satisfied pig or a dissatisfied Socrates? Defend your position in 2 sentences.",
    # Technical / systems
    "Explain the CAP theorem and why it matters for distributed databases.",
    "What are the trade-offs between eventual consistency and strong consistency in microservices?",
    "Describe how a Kalman filter works for predicting a linear system with noise.",
]

# ─── State ────────────────────────────────────────────────────────────────────
recent_requests = []
total_tokens = 0
total_cost = 0.0
per_model = {}  # model -> {requests, tokens, cost}
per_provider = {}  # provider -> {requests, tokens, cost}


def send_request(model: str, prompt: str) -> dict:
    """Send a single chat completion request to the proxy."""
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": MAX_TOKENS,
    }).encode("utf-8")

    req = urllib.request.Request(
        PROXY_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            duration_ms = int((time.time() - start) * 1000)

            usage = body.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            total_toks = usage.get("total_tokens", prompt_tokens + completion_tokens)

            # Try to get provider/key_name from response headers
            key_name = resp.headers.get("X-Provider-Key", resp.headers.get("X-Key-Name", ""))
            provider = resp.headers.get("X-Provider", key_name or "unknown")

            # Try to get cost from headers
            cost_usd = float(resp.headers.get("X-Cost-USD", 0.0))

            return {
                "model": model,
                "provider": provider,
                "key_name": key_name or provider,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_toks,
                "cost_usd": cost_usd,
                "duration_ms": duration_ms,
                "status": "success",
                "error": None,
                "timestamp": time.time(),
            }
    except urllib.error.HTTPError as e:
        duration_ms = int((time.time() - start) * 1000)
        error_body = ""
        try:
            error_body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        return {
            "model": model,
            "provider": "unknown",
            "key_name": "",
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "duration_ms": duration_ms,
            "status": f"error_{e.code}",
            "error": error_body or str(e),
            "timestamp": time.time(),
        }
    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        return {
            "model": model,
            "provider": "unknown",
            "key_name": "",
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "duration_ms": duration_ms,
            "status": "error",
            "error": str(e),
            "timestamp": time.time(),
        }


def enrich_from_db(result: dict):
    """Try to enrich the result with cost/provider data from the DB."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=3)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        # Find the most recent api_call matching this model around this timestamp
        cur.execute(
            """SELECT key_name, cost_usd, tier, status_code, duration_ms
               FROM api_calls
               WHERE model = ? AND ts >= ? - 5
               ORDER BY id DESC LIMIT 1""",
            (result["model"], result["timestamp"]),
        )
        row = cur.fetchone()
        if row:
            if not result["key_name"]:
                result["key_name"] = row["key_name"] or ""
                result["provider"] = row["key_name"] or row["tier"] or "unknown"
            if not result["cost_usd"]:
                result["cost_usd"] = row["cost_usd"] or 0.0
        conn.close()
    except Exception:
        pass


def update_stats(result: dict):
    """Update aggregate stats."""
    global total_tokens, total_cost

    total_tokens += result["total_tokens"]
    total_cost += result["cost_usd"]

    model = result["model"]
    if model not in per_model:
        per_model[model] = {"requests": 0, "tokens": 0, "cost": 0.0}
    per_model[model]["requests"] += 1
    per_model[model]["tokens"] += result["total_tokens"]
    per_model[model]["cost"] += result["cost_usd"]

    provider = result["provider"] or "unknown"
    if provider not in per_provider:
        per_provider[provider] = {"requests": 0, "tokens": 0, "cost": 0.0}
    per_provider[provider]["requests"] += 1
    per_provider[provider]["tokens"] += result["total_tokens"]
    per_provider[provider]["cost"] += result["cost_usd"]


def write_snapshot():
    """Write JSON snapshot to disk."""
    snapshot = {
        "timestamp": time.time(),
        "recent_requests": recent_requests[-100:],
        "aggregate": {
            "total_tokens": total_tokens,
            "total_cost": round(total_cost, 8),
            "total_requests": len(recent_requests),
            "per_model": {
                m: {
                    "requests": v["requests"],
                    "tokens": v["tokens"],
                    "cost": round(v["cost"], 8),
                    "avg_tokens_per_req": round(v["tokens"] / max(v["requests"], 1), 1),
                    "avg_cost_per_req": round(v["cost"] / max(v["requests"], 1), 8),
                }
                for m, v in per_model.items()
            },
            "per_provider": {
                p: {
                    "requests": v["requests"],
                    "tokens": v["tokens"],
                    "cost": round(v["cost"], 8),
                    "rate_per_m": round(v["cost"] / max(v["tokens"], 1) * 1_000_000, 4) if v["tokens"] > 0 else 0,
                }
                for p, v in per_provider.items()
            },
        },
    }

    tmp = SNAPSHOT_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(snapshot, f, indent=2)
    os.rename(tmp, SNAPSHOT_PATH)


def main():
    print("🔥 Sovereign Engineering — Token Burn Demo")
    print(f"   Proxy: {PROXY_URL}")
    print(f"   Models: {', '.join(MODELS)}")
    print(f"   Snapshot: {SNAPSHOT_PATH}")
    print(f"   Delay: {MIN_DELAY}-{MAX_DELAY}s between requests")
    print()

    iteration = 0
    try:
        while True:
            iteration += 1
            model = random.choice(MODELS)
            prompt = random.choice(PROMPTS)

            print(f"  [{iteration:4d}] → {model:20s} | {prompt[:60]}...", flush=True)
            result = send_request(model, prompt)
            enrich_from_db(result)
            recent_requests.append(result)
            update_stats(result)

            status_icon = "✓" if result["status"] == "success" else "✗"
            print(
                f"         {status_icon} {result['status']:12s} | "
                f"tok={result['total_tokens']:5d} | "
                f"cost=${result['cost_usd']:.8f} | "
                f"{result['duration_ms']:5d}ms | "
                f"prov={result['provider']}",
                flush=True,
            )

            write_snapshot()
            delay = random.uniform(MIN_DELAY, MAX_DELAY)
            time.sleep(delay)

    except KeyboardInterrupt:
        print("\n\nStopped. Final snapshot written.")
        write_snapshot()
        print(f"Total requests: {len(recent_requests)}")
        print(f"Total tokens: {total_tokens}")
        print(f"Total cost: ${total_cost:.8f}")


if __name__ == "__main__":
    main()