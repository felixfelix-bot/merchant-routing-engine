#!/usr/bin/env python3
"""quality_probe.py — Canary prompts to detect silent model downgrades.

Phase 2.5.3: Standalone cron job (every 4h) that sends 3 known probe prompts
to each provider endpoint and compares the response against the expected
answer.  This is the "canary in the coal mine" — if a provider returns
garbage, truncated output, or wrong answers, it is running a different model
or a degraded version, and we want to know BEFORE it affects production
routing.

What it records, per probe::

    provider          (str)   — provider name
    probe_id          (int)   — 1, 2, or 3
    response_received (bool)  — did we get any response at all?
    response_text     (str)   — first 500 chars of the response
    correct_answer    (bool)  — did the response contain the expected answer?
    latency_ms        (int)   — round-trip time
    error_type        (str)   — "none", "timeout", "http_5xx", ...
    timestamp         (str)   — ISO-8601 UTC

Results are logged to the ``provider_telemetry`` table (same as P3.3a),
extended with three probe-specific columns (probe_id, response_text,
correct_answer).  The extension is idempotent and non-destructive — existing
columns used by the production proxy are untouched.

Design goals
------------
* **Never crashes** — every provider call is wrapped in try/except.  A
  network error, timeout, or bad endpoint records a failed result and moves
  on; it never propagates.
* **Standalone** — runs as a cron job, stdlib-only for the core path.
  ``pyyaml`` is optional (falls back to built-in provider defaults).
* **Cron-friendly output** — ``--json`` emits pure JSON for machine
  consumption; exits 1 if any provider's quality drops below threshold so a
  cron wrapper can fire an alert.

Usage::

    python3 scripts/quality_probe.py                 # probe all providers
    python3 scripts/quality_probe.py --dry-run       # no network, canned responses
    python3 scripts/quality_probe.py --json          # JSON output for cron
    python3 scripts/quality_probe.py --only zai_ours,ppq

Cron setup (every 4 hours)::

    0 */4 * * * cd ~/merchant-routing-engine && python3 scripts/quality_probe.py --json >> /tmp/quality_probe.log 2>&1
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# ── Path bootstrap so `from scripts...` and config lookups work standalone ──
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

__all__ = [
    "PROBES",
    "DEFAULT_PROVIDERS",
    "PROBE_TIMEOUT_S",
    "LATENCY_ALERT_MS",
    "QUALITY_FAILURE_THRESHOLD",
    "MAX_RESPONSE_TEXT_CHARS",
    "evaluate_correctness",
    "load_providers",
    "call_llm",
    "run_all",
    "check_alerts",
    "format_json",
    "parse_json_output",
    "main",
]

# ── Tunables ────────────────────────────────────────────────────────────────────

#: Per-probe HTTP timeout (seconds).
PROBE_TIMEOUT_S = 30

#: Latency above this (ms) triggers a warning.
LATENCY_ALERT_MS = 10_000  # 10s

#: A provider failing this many probes (out of 3) triggers exit 1.
QUALITY_FAILURE_THRESHOLD = 2

#: Truncate stored response text to this many characters.
MAX_RESPONSE_TEXT_CHARS = 500

# ── Canary probes ───────────────────────────────────────────────────────────────
# Each probe has a deterministic correctness check.  ``evaluate_correctness``
# is the single source of truth for "did the model answer correctly?".

PROBES: list[dict] = [
    {
        "probe_id": 1,
        "prompt": "What is 2+2? Answer with just the number.",
        # correct ⇔ response contains "4"
    },
    {
        "probe_id": 2,
        "prompt": ("Write a 3-line Python function called add(a,b) "
                   "that returns a+b. No explanation."),
        # correct ⇔ response contains both "def add" and "return"
    },
    {
        "probe_id": 3,
        "prompt": "What is the capital of France? One word answer.",
        # correct ⇔ response contains "Paris" (case-insensitive)
    },
]


def evaluate_correctness(probe_id: int, text: str | None) -> bool:
    """Return True iff ``text`` satisfies probe ``probe_id``'s expected answer.

    Pure function — the single source of truth for correctness.  An unknown
    ``probe_id`` or empty/None text is always False (never claim correctness
    we cannot verify).

    >>> evaluate_correctness(1, "4")
    True
    >>> evaluate_correctness(1, "5")
    False
    >>> evaluate_correctness(2, "def add(a, b):\\n    return a + b")
    True
    >>> evaluate_correctness(3, "Paris")
    True
    """
    if not text:
        return False
    if probe_id == 1:
        return "4" in text
    if probe_id == 2:
        return "def add" in text and "return" in text
    if probe_id == 3:
        return "paris" in text.lower()
    return False


# ── Built-in provider defaults (fallback when config/providers.yaml is ────────
# unavailable or unparseable).  Mirrors the canonical provider topology so the
# script still runs standalone.  Endpoints are OpenAI-compatible chat URLs.

DEFAULT_PROVIDERS: dict[str, dict] = {
    "zai_ours": {
        "endpoint": "https://api.z.ai/api/coding/paas/v4",
        "key_env": "ZAI_OUR_KEY",
        "model": "glm-4.5",
    },
    "zai_friend": {
        "endpoint": "https://api.z.ai/api/coding/paas/v4",
        "key_env": "ZAI_API_KEY",
        "model": "glm-4.5",
    },
    "ollama_cloud": {
        "endpoint": "https://api.ollama.cloud/v1/chat/completions",
        "key_env": "OLLAMA_CLOUD_KEY",
        "model": "gpt-oss:120b",
    },
    "ppq": {
        "endpoint": "https://api.ppq.ai/v1/chat/completions",
        "key_env": "PPQ_API_KEY",
        "model": "deepseek-v4-flash",
    },
    "openrouter": {
        "endpoint": "https://openrouter.ai/api/v1/chat/completions",
        "key_env": "OPENROUTER_API_KEY",
        "model": "deepseek/deepseek-v4-flash",
        "headers": {
            "HTTP-Referer": "https://hermes.local",
            "X-Title": "Hermes Agent",
        },
    },
    "deepinfra": {
        "endpoint": "https://api.deepinfra.com/v1/openai/chat/completions",
        "key_env": "DEEPINFRA_API_KEY",
        "model": "deepseek-v4-flash",
    },
}


# ── Config loading ──────────────────────────────────────────────────────────────


def load_providers(config_path: str | None) -> dict[str, dict]:
    """Load the provider map from ``config_path`` (providers.yaml).

    Falls back to :data:`DEFAULT_PROVIDERS` if the file is missing,
    unparseable, or ``pyyaml`` is not installed.  **Never raises.**

    Returns a dict of ``provider_name -> {endpoint, key_env, model, ...}``.
    """
    if config_path and os.path.exists(config_path):
        parsed = _parse_yaml(config_path)
        if parsed:
            return parsed
    return dict(DEFAULT_PROVIDERS)


def _parse_yaml(config_path: str) -> dict[str, dict] | None:
    """Parse providers.yaml into a provider map.  Returns None on any failure."""
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return None
    try:
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return None

    providers: dict[str, dict] = {}

    # zai: one entry per key (ours, friend) sharing the same upstream.
    zai = data.get("zai") or {}
    upstream = zai.get("upstream")
    for key_name, key_cfg in (zai.get("keys") or {}).items():
        if not isinstance(key_cfg, dict):
            continue
        providers[f"zai_{key_name}"] = {
            "endpoint": upstream,
            "key_env": key_cfg.get("key_env"),
            "model": "glm-4.5",
        }

    # ollama_cloud: flat-rate secondary.
    ollama = data.get("ollama_cloud") or {}
    if isinstance(ollama, dict) and ollama:
        providers["ollama_cloud"] = {
            "endpoint": _join_chat(ollama.get("base_url")),
            "key_env": ollama.get("key_env"),
            "model": ollama.get("model", "gpt-oss:120b"),
        }

    # external: per-token providers (ppq, openrouter, deepinfra, ...).
    external = data.get("external") or {}
    for name, cfg in external.items():
        if not isinstance(cfg, dict):
            continue
        models = cfg.get("models") or {}
        first_model = next(iter(models.keys()), None) if models else None
        providers[name] = {
            "endpoint": _join_chat(cfg.get("base_url")),
            "key_env": cfg.get("key_env"),
            "model": first_model,
            "headers": cfg.get("headers"),
        }

    return providers


def _join_chat(base_url: str | None) -> str:
    """Append ``/chat/completions`` to a base URL (idempotent)."""
    if not base_url:
        return ""
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


# ── HTTP caller (the network seam — monkeypatchable in tests) ──────────────────


def call_llm(
    provider_name: str,
    config: dict,
    prompt: str,
    timeout: int = PROBE_TIMEOUT_S,
) -> tuple[bool, str, int, str]:
    """Send ``prompt`` to ``provider_name`` and return the result tuple.

    Returns ``(response_received, response_text, latency_ms, error_type)``.
    **Never raises** — every network/parse error is caught and reported via
    the tuple so the probe loop cannot crash.
    """
    key_env = config.get("key_env", "")
    endpoint = config.get("endpoint", "")
    model = config.get("model", "")
    key = os.environ.get(key_env, "") if key_env else ""

    if not key:
        return (False, "", 0, "no_api_key")
    if not endpoint:
        return (False, "", 0, "no_endpoint")

    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 128,
        "temperature": 0,
        "stream": False,
    }).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    }
    extra = config.get("headers") or {}
    if isinstance(extra, dict):
        headers.update(extra)

    req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        latency_ms = int((time.monotonic() - start) * 1000)
        text = _extract_text(raw)
        return (True, text[:MAX_RESPONSE_TEXT_CHARS], latency_ms, "none")
    except urllib.error.HTTPError as e:
        latency_ms = int((time.monotonic() - start) * 1000)
        return (False, "", latency_ms, f"http_{e.code}")
    except urllib.error.URLError as e:
        latency_ms = int((time.monotonic() - start) * 1000)
        reason = getattr(e, "reason", e)
        if isinstance(reason, socket.timeout) or "timed out" in str(reason).lower():
            return (False, "", latency_ms, "timeout")
        return (False, "", latency_ms, f"url_error")
    except socket.timeout:
        latency_ms = int((time.monotonic() - start) * 1000)
        return (False, "", latency_ms, "timeout")
    except Exception as e:  # noqa: BLE001 — last-resort safety net
        latency_ms = int((time.monotonic() - start) * 1000)
        return (False, "", latency_ms, f"error:{type(e).__name__}")


def _extract_text(raw: str) -> str:
    """Extract the assistant message text from an LLM API response.

    Handles OpenAI-compatible (``choices[0].message.content``) and a couple
    of common variants.  Falls back to the raw body if nothing matches.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw.strip()[:MAX_RESPONSE_TEXT_CHARS]
    # OpenAI-compatible
    try:
        return str(data["choices"][0]["message"]["content"]).strip()
    except (KeyError, IndexError, TypeError):
        pass
    # zai / coding-paas variant
    try:
        return str(data["data"]["content"]).strip()
    except (KeyError, TypeError):
        pass
    # raw text fallback
    return raw.strip()[:MAX_RESPONSE_TEXT_CHARS]


# ── Canned responses for --dry-run (no network, no keys) ───────────────────────


def _canned_caller(provider_name: str, config: dict, prompt: str, timeout: int) -> tuple[bool, str, int, str]:
    """Deterministic fake responses for dry-run mode.  Never touches network."""
    if "2+2" in prompt:
        return (True, "4", 10, "none")
    if "add(a,b)" in prompt:
        return (True, "def add(a, b):\n    return a + b", 12, "none")
    if "capital of France" in prompt:
        return (True, "Paris", 8, "none")
    return (True, "ok", 10, "none")


# ── Core run loop ───────────────────────────────────────────────────────────────


def run_all(
    provider_configs: dict[str, dict],
    db_path: str | None = None,
    caller=None,
    dry_run: bool = False,
    timeout: int = PROBE_TIMEOUT_S,
) -> list[dict]:
    """Run every probe against every provider and return the result list.

    Args:
        provider_configs: ``{name: {endpoint, key_env, model}}``.
        db_path: If given, each result is also INSERTed into
            ``provider_telemetry`` (table auto-created/extended).
        caller: The LLM-call seam.  Defaults to :func:`call_llm`.  Overridden
            by :func:`_canned_caller` when ``dry_run`` is True.
        dry_run: If True, use canned responses — no network, no keys.
        timeout: Per-probe HTTP timeout (seconds).

    Returns:
        List of result dicts (one per provider×probe).  **Never raises.**
    """
    if caller is None:
        caller = call_llm
    if dry_run:
        caller = _canned_caller

    conn = _open_db(db_path)
    results: list[dict] = []
    try:
        for provider_name, cfg in provider_configs.items():
            for probe in PROBES:
                result = _run_one(provider_name, cfg, probe, caller, timeout)
                results.append(result)
                if conn is not None:
                    _log_probe(conn, result)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return results


def _run_one(
    provider_name: str,
    cfg: dict,
    probe: dict,
    caller,
    timeout: int,
) -> dict:
    """Run a single probe against a single provider.  Never raises."""
    try:
        received, text, latency_ms, error_type = caller(
            provider_name, cfg, probe["prompt"], timeout
        )
    except Exception as e:  # noqa: BLE001 — the whole point: never crash
        received, text, latency_ms, error_type = (
            False, "", 0, f"caller_exception:{type(e).__name__}"
        )
    correct = bool(received) and evaluate_correctness(probe["probe_id"], text)
    return {
        "provider": provider_name,
        "probe_id": probe["probe_id"],
        "response_received": bool(received),
        "response_text": (text or "")[:MAX_RESPONSE_TEXT_CHARS],
        "correct_answer": correct,
        "latency_ms": int(latency_ms or 0),
        "error_type": error_type or "none",
        "timestamp": _now_iso(),
    }


# ── Alerting ────────────────────────────────────────────────────────────────────


def check_alerts(results: list[dict]) -> tuple[bool, list[str]]:
    """Evaluate results for quality drops and high latency.

    Returns ``(should_exit_1, warnings)``:

    * ``should_exit_1`` is True if **any** provider failed
      :data:`QUALITY_FAILURE_THRESHOLD` (2) or more probes — this is the
      cron-alert signal (exit code 1).
    * ``warnings`` lists human-readable strings for both quality drops and
      latency exceedances, to be printed to stderr.
    """
    warnings: list[str] = []
    by_provider: dict[str, list[dict]] = {}
    for r in results:
        by_provider.setdefault(r["provider"], []).append(r)

    quality_failed = False
    for provider in sorted(by_provider):
        probs = by_provider[provider]
        failures = sum(1 for p in probs if not p["correct_answer"])
        if failures >= QUALITY_FAILURE_THRESHOLD:
            warnings.append(
                f"QUALITY: {provider} failed {failures}/{len(probs)} probes "
                f"(threshold {QUALITY_FAILURE_THRESHOLD})"  # noqa: E501
            )
            quality_failed = True
        max_lat = max((p["latency_ms"] for p in probs), default=0)
        if max_lat > LATENCY_ALERT_MS:
            warnings.append(
                f"LATENCY: {provider} max latency {max_lat}ms "
                f"> {LATENCY_ALERT_MS}ms threshold"
            )
    return quality_failed, warnings


# ── Output formatting ───────────────────────────────────────────────────────────


def format_json(results: list[dict]) -> str:
    """Serialize results as a JSON object for cron consumption."""
    return json.dumps(
        {"timestamp": _now_iso(), "probe_count": len(PROBES), "results": results},
        indent=2,
        default=str,
    )


def parse_json_output(text: str) -> dict | None:
    """Extract and parse the JSON object embedded in ``text``.

    Tolerates leading/trailing non-JSON lines (e.g. ANSI noise).  Returns
    None if no valid JSON object/array is found.
    """
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # locate the first '{' or '['
    start = -1
    for i, ch in enumerate(text):
        if ch in "{[":
            start = i
            break
    if start == -1:
        return None
    close = "}" if text[start] == "{" else "]"
    # walk from the end to find the matching closer
    for end in range(len(text) - 1, start, -1):
        if text[end] == close:
            candidate = text[start:end + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
    return None


def _print_human(results: list[dict]) -> None:
    """Print a human-readable summary table to stdout."""
    by_provider: dict[str, list[dict]] = {}
    for r in results:
        by_provider.setdefault(r["provider"], []).append(r)
    print("=" * 64)
    print("  Quality Probe Results")
    print("=" * 64)
    for provider in sorted(by_provider):
        probs = by_provider[provider]
        passed = sum(1 for p in probs if p["correct_answer"])
        max_lat = max((p["latency_ms"] for p in probs), default=0)
        status = "OK" if passed == len(probs) else "DEGRADED"
        print(f"  {provider:18s} {passed}/{len(probs)} correct  "
              f"max_lat={max_lat}ms  [{status}]")
        for p in sorted(probs, key=lambda x: x["probe_id"]):
            mark = "PASS" if p["correct_answer"] else "FAIL"
            recv = "recv" if p["response_received"] else "NO-RESPONSE"
            print(f"      probe {p['probe_id']}: {mark:4s} [{recv:11s}] "
                  f"{p['latency_ms']:>6}ms  {p['error_type']}")
    print("=" * 64)


# ── Database logging ────────────────────────────────────────────────────────────
# Extends the provider_telemetry table (from P3.3a / zai_proxy) with three
# probe-specific columns.  The extension is idempotent and non-destructive:
# existing columns and the production proxy's INSERTs are untouched.

_TELEMETRY_SCHEMA = """CREATE TABLE IF NOT EXISTS provider_telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    provider TEXT NOT NULL,
    response_received INTEGER,
    response_valid INTEGER,
    latency_ms INTEGER,
    error_type TEXT,
    billed_tokens INTEGER,
    actual_tokens INTEGER,
    token_mismatch INTEGER
)"""

#: Columns added by the quality probe.  (name, SQL type)
_PROBE_COLUMNS: list[tuple[str, str]] = [
    ("probe_id", "INTEGER"),
    ("response_text", "TEXT"),
    ("correct_answer", "INTEGER"),
]


def _open_db(db_path: str | None):
    """Open the usage DB for logging, ensuring the telemetry table exists."""
    if not db_path:
        return None
    try:
        conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
        _ensure_quality_table(conn)
        return conn
    except Exception:
        return None


def _ensure_quality_table(conn) -> None:
    """Create/extend provider_telemetry so it has the probe columns.

    Idempotent and never raises.  If the table already exists (created by the
    production proxy), the missing probe columns are added via ALTER TABLE.
    """
    try:
        conn.execute(_TELEMETRY_SCHEMA)
    except Exception:
        pass
    # detect existing columns and add any probe columns that are missing
    try:
        existing = {row[1] for row in conn.execute(
            "PRAGMA table_info(provider_telemetry)").fetchall()}
    except Exception:
        existing = set()
    for col, col_type in _PROBE_COLUMNS:
        if col not in existing:
            try:
                conn.execute(
                    f"ALTER TABLE provider_telemetry "
                    f"ADD COLUMN {col} {col_type}"
                )
            except Exception:
                pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_telemetry_probe "
            "ON provider_telemetry(probe_id)"
        )
    except Exception:
        pass


def _log_probe(conn, result: dict) -> None:
    """INSERT one probe result into provider_telemetry.  Never raises."""
    if conn is None:
        return
    try:
        _ensure_quality_table(conn)
        conn.execute(
            "INSERT INTO provider_telemetry "
            "(ts, provider, response_received, response_valid, latency_ms, "
            " error_type, billed_tokens, actual_tokens, token_mismatch, "
            " probe_id, response_text, correct_answer) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                result["timestamp"],
                result["provider"],
                int(result["response_received"]),
                int(result["correct_answer"]),  # response_valid == correct
                int(result["latency_ms"]),
                result["error_type"] or "none",
                0, 0, 0,
                result["probe_id"],
                result["response_text"],
                int(result["correct_answer"]),
            ),
        )
    except Exception:
        pass  # logging must NEVER break the probe run


# ── Helpers ─────────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    """Current UTC time as ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


# ── CLI ─────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.  Returns 0 on success, 1 on quality drop (cron alert)."""
    parser = argparse.ArgumentParser(
        description="Quality probes — canary prompts to detect silent model "
                    "downgrades. Sends 3 known prompts to each provider and "
                    "alerts if quality drops below threshold."
    )
    parser.add_argument(
        "--config",
        default=os.path.join(_PARENT, "config", "providers.yaml"),
        help="Path to providers.yaml (default: config/providers.yaml).",
    )
    parser.add_argument(
        "--providers",
        default=None,
        help="Alias for --config (path to providers.yaml).",
    )
    parser.add_argument(
        "--db",
        default=os.path.expanduser("~/.hermes/bot/zai_usage.db"),
        help="Path to zai_usage.db for telemetry logging "
             "(default: ~/.hermes/bot/zai_usage.db).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=PROBE_TIMEOUT_S,
        help=f"Per-probe HTTP timeout in seconds (default: {PROBE_TIMEOUT_S}).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit pure JSON to stdout (for cron consumption).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Use canned responses — no network calls or API keys needed.",
    )
    parser.add_argument(
        "--only",
        default=None,
        help="Comma-separated provider names to probe (default: all).",
    )
    args = parser.parse_args(argv)

    config_path = args.providers or args.config
    providers = load_providers(config_path)

    if args.only:
        wanted = {n.strip() for n in args.only.split(",") if n.strip()}
        providers = {k: v for k, v in providers.items() if k in wanted}

    if not providers:
        print("No providers to probe.", file=sys.stderr)
        return 1

    results = run_all(
        providers,
        db_path=args.db,
        dry_run=args.dry_run,
        timeout=args.timeout,
    )
    should_alert, warnings = check_alerts(results)

    if args.json:
        print(format_json(results))
    else:
        _print_human(results)

    for w in warnings:
        print(f"WARNING: {w}", file=sys.stderr)

    return 1 if should_alert else 0


if __name__ == "__main__":
    raise SystemExit(main())
