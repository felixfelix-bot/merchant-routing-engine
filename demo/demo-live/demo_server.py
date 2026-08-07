#!/usr/bin/env python3
"""
demo_server.py — Standalone HTTP server for the live demo dashboard.

Serves:
  /                  → demo-dashboard.html
  /data.json         → live JSON from zai_usage.db
  /vendor/*          → vendor JS files (plotly etc.)
  /snapshot.json     → burn_demo.py's snapshot (if available)

Uses only Python stdlib. Runs on port 8181.
"""

import json
import os
import sqlite3
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

PORT = 8181
BASE_DIR = Path(__file__).parent.resolve()
DB_PATH = Path.home() / ".hermes" / "bot" / "zai_usage.db"
SNAPSHOT_PATH = Path.home() / "merchant-routing-engine" / "demo" / "demo-snapshot.json"
VENDOR_DIR = BASE_DIR / "vendor"


def build_data_json() -> dict:
    """Query the DB for live data and return the dashboard JSON structure."""
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=3)
        conn.row_factory = sqlite3.Row

        # ─── Recent api_calls (last 5 min or 200 rows) ────────────────────
        cutoff = time.time() - 600  # last 10 min
        rows = conn.execute(
            """SELECT id, ts, key_name, model, prompt_tokens, completion_tokens,
                      total_tokens, tier, status_code, duration_ms, cost_usd,
                      cost_source, cache_hit, ollama_hit, ppq_hit
               FROM api_calls
               WHERE ts >= ?
               ORDER BY id DESC
               LIMIT 200""",
            (cutoff,),
        ).fetchall()

        recent = []
        for r in rows:
            provider = r["key_name"] or r["tier"] or "unknown"
            recent.append({
                "id": r["id"],
                "ts": r["ts"],
                "model": r["model"] or "unknown",
                "provider": provider,
                "key_name": r["key_name"] or "",
                "tier": r["tier"] or "",
                "prompt_tokens": r["prompt_tokens"] or 0,
                "completion_tokens": r["completion_tokens"] or 0,
                "total_tokens": r["total_tokens"] or 0,
                "cost_usd": r["cost_usd"] or 0.0,
                "cost_source": r["cost_source"] or "",
                "status_code": r["status_code"] or 0,
                "duration_ms": r["duration_ms"] or 0,
                "cache_hit": bool(r["cache_hit"]),
                "ollama_hit": bool(r["ollama_hit"]),
                "ppq_hit": bool(r["ppq_hit"]),
                "status": "success" if (r["status_code"] or 0) == 200 else f"error_{r['status_code']}",
            })

        # ─── Aggregate stats ─────────────────────────────────────────────
        # Full-session aggregate (from all data in the window)
        agg_rows = conn.execute(
            """SELECT
                model,
                key_name,
                COUNT(*) as requests,
                COALESCE(SUM(total_tokens), 0) as tokens,
                COALESCE(SUM(cost_usd), 0) as cost
               FROM api_calls
               WHERE ts >= ? AND total_tokens IS NOT NULL
               GROUP BY model""",
            (cutoff,),
        ).fetchall()

        per_model = {}
        per_provider = {}
        total_tokens = 0
        total_cost = 0.0
        total_requests = 0

        for r in agg_rows:
            model = r["model"] or "unknown"
            tokens = r["tokens"]
            cost = r["cost"]
            total_tokens += tokens
            total_cost += cost
            total_requests += r["requests"]

            if model not in per_model:
                per_model[model] = {"requests": 0, "tokens": 0, "cost": 0.0}
            per_model[model]["requests"] += r["requests"]
            per_model[model]["tokens"] += tokens
            per_model[model]["cost"] += cost

            provider = r["key_name"] or "unknown"
            if provider not in per_provider:
                per_provider[provider] = {"requests": 0, "tokens": 0, "cost": 0.0, "rate_per_m": 0.0}
            per_provider[provider]["requests"] += r["requests"]
            per_provider[provider]["tokens"] += tokens
            per_provider[provider]["cost"] += cost

        # Calculate $/M per provider
        for p, v in per_provider.items():
            v["rate_per_m"] = round(v["cost"] / max(v["tokens"], 1) * 1_000_000, 4) if v["tokens"] > 0 else 0
            v["cost"] = round(v["cost"], 8)

        # Round per-model
        for m, v in per_model.items():
            v["cost"] = round(v["cost"], 8)

        # ─── Routing shadow decisions (last 5 min) ────────────────────────
        shadow_rows = conn.execute(
            """SELECT ts, live_provider, live_model, shadow_provider,
                      shadow_model, live_cost, shadow_cost, tokens, agree, reason
               FROM routing_shadow_decisions
               WHERE ts >= ?
               ORDER BY id DESC LIMIT 50""",
            (cutoff,),
        ).fetchall()

        routing_decisions = [dict(r) for r in shadow_rows]

        # ─── Quota pressure (from model_decisions) ────────────────────────
        try:
            q_rows = conn.execute(
                """SELECT key_name, model, tier, hint, reason, peak, hours_left, active_key
                   FROM model_decisions
                   WHERE ts >= ?
                   ORDER BY id DESC LIMIT 20""",
                (cutoff,),
            ).fetchall()
            quota_pressure = [dict(r) for r in q_rows]
        except Exception:
            quota_pressure = []

        # ─── Rate limit samples (for pressure indicator) ──────────────────
        try:
            rl_rows = conn.execute(
                """SELECT key_name, is_429_limited, consecutive_429_count,
                          requests_remaining, tokens_remaining, updated_at
                   FROM api_rate_limits
                   ORDER BY updated_at DESC LIMIT 10""",
            ).fetchall()
            rate_limits = [dict(r) for r in rl_rows]
        except Exception:
            rate_limits = []

        conn.close()

        return {
            "timestamp": time.time(),
            "recent_requests": recent,
            "routing_decisions": routing_decisions,
            "quota_pressure": quota_pressure,
            "rate_limits": rate_limits,
            "aggregate": {
                "total_tokens": total_tokens,
                "total_cost": round(total_cost, 8),
                "total_requests": total_requests,
                "per_model": per_model,
                "per_provider": per_provider,
            },
        }

    except Exception as e:
        return {
            "timestamp": time.time(),
            "error": str(e),
            "recent_requests": [],
            "routing_decisions": [],
            "quota_pressure": [],
            "rate_limits": [],
            "aggregate": {
                "total_tokens": 0,
                "total_cost": 0.0,
                "total_requests": 0,
                "per_model": {},
                "per_provider": {},
            },
        }


class DemoHandler(SimpleHTTPRequestHandler):
    """Custom handler for the demo server."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._serve_file(BASE_DIR / "demo-dashboard.html", "text/html")
        elif self.path == "/data.json":
            self._serve_json(build_data_json())
        elif self.path == "/snapshot.json":
            if SNAPSHOT_PATH.exists():
                self._serve_file(SNAPSHOT_PATH, "application/json")
            else:
                self._serve_json(build_data_json())
        elif self.path.startswith("/vendor/"):
            # Serve from vendor dir
            rel = self.path[len("/vendor/"):]
            fpath = VENDOR_DIR / rel
            if fpath.exists() and fpath.is_file():
                ctype = "application/javascript" if rel.endswith(".js") else "application/octet-stream"
                self._serve_file(fpath, ctype)
            else:
                self.send_error(404, "Vendor file not found")
        else:
            # Fallback to SimpleHTTPRequestHandler for static files
            super().do_GET()

    def _serve_file(self, fpath: Path, content_type: str):
        try:
            data = fpath.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_error(500, str(e))

    def _serve_json(self, data: dict):
        body = json.dumps(data, indent=None, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Minimal logging
        if args and "data.json" not in str(args):
            super().log_message(format, *args)


def main():
    print(f"🖥️  Demo server starting on http://localhost:{PORT}")
    print(f"   Dashboard: http://localhost:{PORT}/")
    print(f"   Data API:  http://localhost:{PORT}/data.json")
    print(f"   DB:        {DB_PATH}")
    print(f"   Vendor:    {VENDOR_DIR}")
    print()

    server = HTTPServer(("0.0.0.0", PORT), DemoHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        server.server_close()


if __name__ == "__main__":
    main()