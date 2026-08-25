#!/usr/bin/env python3
"""OX-3a eval harness — oxalpha tier acceleration eval (plan §3, t_7a12e29a).

Staged ramp (verify_pricing -> canary -> full_set -> burst_probe), each stage
gated on the OpenRouter usage delta for the OXALPHA key staying EXACTLY zero
(plan §2.4 spend guard — any nonzero usage delta aborts the campaign and
feeds the PromoTierGuard kill path).

All network access is isolated in `OxClient` (oxalpha via OpenRouter) and
`BaseClient` (baseline glm-5.3 via the local zai proxy :9099). Everything
else is pure logic, unit-tested in tests/test_oxalpha_eval.py.

Data policy (v1 plan §2.5): prompts are eval fixtures derived from sanitized
repo/ops material; sanitize_scan refuses to write anything that smells like a
secret, PII, or a dump. Results artifacts contain model outputs only.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(_HERE, "..", "src"), os.path.join(_HERE, "..")):
    if os.path.isdir(_p):
        sys.path.insert(0, _p)

from promo_tier import PromoTierGuard  # noqa: E402

# ── constants (plan §3.4 pass criteria mirrors live in ox_eval_report) ───────

OX_BASE = "https://openrouter.ai/api/v1"
PROXY_BASE = os.environ.get("ZAI_PROXY_BASE", "http://localhost:9099")
OX_MODEL = "stealth/ox-alpha"
BASE_MODEL = os.environ.get("OXEVAL_BASE_MODEL", "glm-5.3")
KEY_ENV = "OPENROUTER_OXALPHA_KEY"

FIXDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "..", "eval", "ox3a", "fixtures")
RESULTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "eval", "ox3a", "results")

# ── sanitization (v1 plan §2.5) ──────────────────────────────────────────────

_RE_KEYLIKE = re.compile(r"sk-[a-zA-Z0-9_\-]{10,}")
_RE_SECRET_ASSIGN = re.compile(
    r"(?i)\b(password|passwd|passphrase|secret_key|api_key)\b\s*[:=]\s*\S{6,}")
_RE_LONGHEX = re.compile(r"(?i)\b[0-9a-f]{32,}\b")
_RE_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_RE_NSEC = re.compile(r"(?i)nsec1[a-z0-9]{20,}")


def sanitize_scan(items: list[dict]) -> list[str]:
    """Scan fixture items for policy violations. Returns violation strings."""
    violations = []
    for it in items:
        text = json.dumps(it, ensure_ascii=False)
        if _RE_KEYLIKE.search(text):
            violations.append(f"{it.get('id','?')}: key-like token (sk-...)")
        m = _RE_SECRET_ASSIGN.search(text)
        if m:
            violations.append(f"{it.get('id','?')}: secret assignment {m.group(0)[:40]!r}")
        if _RE_LONGHEX.search(text):
            violations.append(f"{it.get('id','?')}: long hex blob")
        if _RE_EMAIL.search(text):
            violations.append(f"{it.get('id','?')}: email address")
        if _RE_NSEC.search(text):
            violations.append(f"{it.get('id','?')}: nostr key material")
    return violations


# ── fixtures ─────────────────────────────────────────────────────────────────

def load_fixtures(fixdir: str = FIXDIR) -> dict:
    out = {}
    for name in ("primary", "refusal_probes", "latency_micro"):
        with open(os.path.join(fixdir, f"{name}.json")) as f:
            out[name] = json.load(f)
    return out


def validate_fixture_set(fx: dict) -> list[str]:
    """Structural validation of a fixture set. Returns error strings."""
    errs = []
    counts = {k: len(v) for k, v in fx.items()}
    if counts.get("primary") != 60:
        errs.append(f"primary must be 60 items, got {counts.get('primary')}")
    if counts.get("refusal_probes") != 10:
        errs.append(f"refusal_probes must be 10 items, got {counts.get('refusal_probes')}")
    if counts.get("latency_micro") != 10:
        errs.append(f"latency_micro must be 10 items, got {counts.get('latency_micro')}")

    ids = [it["id"] for it in fx.get("primary", [])]
    if len(set(ids)) != len(ids):
        dupes = {i for i in ids if ids.count(i) > 1}
        errs.append(f"duplicate primary ids: {sorted(dupes)}")

    shapes = {}
    for it in fx.get("primary", []):
        shapes[it.get("shape")] = shapes.get(it.get("shape"), 0) + 1
        det = it.get("deterministic")
        if det == "verdict":
            if it.get("ground_truth") not in ("approve", "request-changes", "block"):
                errs.append(f"{it['id']}: bad verdict ground_truth")
        elif det == "outcome":
            if it.get("ground_truth") not in ("failure", "no-failure"):
                errs.append(f"{it['id']}: bad outcome ground_truth")
        elif det == "json_schema":
            if not isinstance(it.get("schema"), dict) or not it["schema"]:
                errs.append(f"{it['id']}: json_extract without schema")
    for shape, want in (("code_review", 15), ("build_summary", 15),
                        ("doc_writing", 15), ("json_extract", 15)):
        if shapes.get(shape) != want:
            errs.append(f"shape {shape}: want {want}, got {shapes.get(shape)}")
    return errs


# ── blind A/B helpers ────────────────────────────────────────────────────────

def blind_shuffle(n: int, seed: int) -> list[int]:
    """Deterministic permutation — grading order independent of arm order."""
    return random.Random(seed).sample(range(n), n)


def strip_model_labels(arms: list[dict]) -> list[dict]:
    """Drop arm/model identifiers for blind judging — content + usage only."""
    return [{"content": a.get("content"), "usage": a.get("usage")} for a in arms]


# ── deterministic graders ────────────────────────────────────────────────────

_VERDICT_RE = re.compile(r"VERDICT:\s*(approve|request-changes|block)\b", re.I)
_OUTCOME_RE = re.compile(r"OUTCOME:\s*(failure|no-failure)\b", re.I)


def extract_verdict(text: str) -> str | None:
    if not text:
        return None
    m = _VERDICT_RE.search(text)
    return m.group(1).lower() if m else None


def extract_outcome(text: str) -> str | None:
    if not text:
        return None
    m = _OUTCOME_RE.search(text)
    return m.group(1).lower() if m else None


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def check_json_schema(text: str, schema: dict) -> tuple[bool, str]:
    """Mechanical validation: parse JSON, require every key, match types."""
    if not text or not text.strip():
        return False, "empty content"
    body = text.strip()
    m = _FENCE_RE.search(body)
    if m:
        body = m.group(1).strip()
    try:
        obj = json.loads(body)
    except json.JSONDecodeError as e:
        return False, f"parse error: {e}"
    if not isinstance(obj, dict):
        return False, "not a JSON object"
    for key, typ in schema.items():
        if key not in obj:
            return False, f"missing key: {key}"
        val = obj[key]
        if typ == "str" and not isinstance(val, str):
            return False, f"{key}: want str, got {type(val).__name__}"
        if typ == "number":
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                return False, f"{key}: want number, got {type(val).__name__}"
        if typ == "bool" and not isinstance(val, bool):
            return False, f"{key}: want bool, got {type(val).__name__}"
        if typ == "array" and not isinstance(val, list):
            return False, f"{key}: want array, got {type(val).__name__}"
    return True, "ok"


_REFUSAL_RE = re.compile(
    r"(?i)\b(i can'?t|i cannot|i won'?t|i will not|i'm sorry|sorry, but|"
    r"i must (?:decline|refuse)|i have to (?:decline|refuse)|"
    r"can'?t (?:assist|help|comply|provide|summarize|do that)|"
    r"unable to (?:assist|help|comply)|against my (?:guidelines|policy)|"
    r"not able to (?:assist|help))\b")


def classify_refusal(content) -> bool:
    """True if the content looks like a refusal (or is empty)."""
    if not content or not content.strip():
        return True
    return bool(_REFUSAL_RE.search(content))


def percentile(xs: list[float], p: float) -> float:
    """Linear-interpolation percentile. Empty input raises."""
    if not xs:
        raise ValueError("percentile of empty list")
    s = sorted(xs)
    if len(s) == 1:
        return float(s[0])
    rank = (p / 100.0) * (len(s) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return float(s[int(rank)])
    return float(s[lo] + (s[hi] - s[lo]) * (rank - lo))


# ── burst probe ──────────────────────────────────────────────────────────────

def build_burst_schedule(rate_rps: float, seconds: float) -> list[float]:
    """Sorted second-offsets: rate_rps * seconds evenly spaced requests."""
    n = int(rate_rps * seconds)
    return [round(i / rate_rps, 6) for i in range(n)]


# ── spend gate (plan §2.4 mirror, eval-side) ─────────────────────────────────

class SpendGate:
    """Abort the campaign the moment the oxalpha key shows nonzero usage.

    Mirrors the proxy-side wallet-delta detector: snapshot usage at stage
    boundaries; any positive delta = spend on a $0-promo tier = kill. Feeds
    PromoTierGuard.observe_charge so the tier guard state flips too.
    """

    def __init__(self, guard: PromoTierGuard | None = None):
        self.guard = guard
        self._last_usage: float | None = None
        self._fired = False

    def snapshot(self, usage: float) -> None:
        if self._last_usage is None:
            self._last_usage = float(usage or 0.0)

    def check(self, usage: float) -> dict | None:
        """Returns an anomaly row if delta > 0 (fires once)."""
        usage = float(usage or 0.0)
        delta = round(usage - self._last_usage, 8) if self._last_usage is not None else 0.0
        self._last_usage = usage
        if delta <= 0 or self._fired:
            return None
        self._fired = True
        if self.guard is not None:
            row = self.guard.observe_charge(delta)
            if row is not None:
                return row
        return {
            "ts": time.time(),
            "severity": "critical",
            "category": "promo_spend",
            "title": "openrouter promo tier charged — auto-disabled",
            "detail": json.dumps({
                "detail": (f"eval harness observed nonzero usage delta "
                           f"${delta:.6f} on a $0-promo tier; campaign aborted"),
                "cost_usd": delta,
                "source": "usage_delta",
            }),
        }


class CampaignAbort(Exception):
    def __init__(self, reason: str, anomaly_row: dict | None = None):
        super().__init__(reason)
        self.reason = reason
        self.anomaly_row = anomaly_row


# ── clients ──────────────────────────────────────────────────────────────────

class OxClient:
    """Direct OpenRouter client for the oxalpha promo key."""

    def __init__(self, key: str, base: str = OX_BASE, model: str = OX_MODEL,
                 timeout_s: float = 120.0):
        self.key, self.base, self.model, self.timeout_s = key, base, model, timeout_s
        self.session = requests.Session()

    def _hdrs(self) -> dict:
        return {"Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json"}

    def models_info(self) -> dict:
        r = self.session.get(f"{self.base}/models", timeout=30)
        r.raise_for_status()
        return r.json()

    def key_info(self) -> dict:
        r = self.session.get(f"{self.base}/key", headers=self._hdrs(), timeout=30)
        r.raise_for_status()
        d = r.json().get("data", r.json())
        return {"usage": float(d.get("usage") or 0.0),
                "limit": d.get("limit")}

    def ox_chat(self, messages, effort: str = "low", max_tokens: int = 8192,
                model: str | None = None):
        payload = {
            "model": model or self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "reasoning": {"effort": effort} if effort else None,
        }
        payload = {k: v for k, v in payload.items() if v is not None}
        t0 = time.monotonic()
        try:
            r = self.session.post(f"{self.base}/chat/completions",
                                  headers=self._hdrs(), json=payload,
                                  timeout=self.timeout_s)
            latency = time.monotonic() - t0
            if r.status_code != 200:
                return {"ok": False, "status": r.status_code, "latency_s": latency,
                        "content": None, "usage": {}, "finish_reason": None,
                        "headers": dict(r.headers), "error": r.text[:500]}
            body = r.json()
            content = (body.get("choices") or [{}])[0].get("message", {}).get("content")
            return {"ok": True, "status": 200, "latency_s": latency,
                    "content": content, "usage": body.get("usage") or {},
                    "finish_reason": (body.get("choices") or [{}])[0].get("finish_reason"),
                    "headers": dict(r.headers), "error": None}
        except requests.RequestException as e:
            return {"ok": False, "status": 0, "latency_s": time.monotonic() - t0,
                    "content": None, "usage": {}, "finish_reason": None,
                    "headers": {}, "error": str(e)[:500]}


class BaseClient:
    """Baseline arm: glm-5.3 via the local zai proxy (production path)."""

    def __init__(self, base: str = PROXY_BASE, model: str = BASE_MODEL,
                 timeout_s: float = 120.0):
        self.base, self.model, self.timeout_s = base, model, timeout_s
        self.session = requests.Session()

    def base_chat(self, messages, max_tokens: int = 8192, model: str | None = None):
        payload = {"model": model or self.model, "messages": messages,
                   "max_tokens": max_tokens}
        t0 = time.monotonic()
        try:
            r = self.session.post(f"{self.base}/v1/chat/completions",
                                  json=payload, timeout=self.timeout_s)
            latency = time.monotonic() - t0
            if r.status_code != 200:
                return {"ok": False, "status": r.status_code, "latency_s": latency,
                        "content": None, "usage": {}, "finish_reason": None,
                        "headers": dict(r.headers), "error": r.text[:500]}
            body = r.json()
            content = (body.get("choices") or [{}])[0].get("message", {}).get("content")
            return {"ok": True, "status": 200, "latency_s": latency,
                    "content": content, "usage": body.get("usage") or {},
                    "finish_reason": (body.get("choices") or [{}])[0].get("finish_reason"),
                    "headers": dict(r.headers), "error": None}
        except requests.RequestException as e:
            return {"ok": False, "status": 0, "latency_s": time.monotonic() - t0,
                    "content": None, "usage": {}, "finish_reason": None,
                    "headers": {}, "error": str(e)[:500]}


# ── staged ramp ──────────────────────────────────────────────────────────────

DEFAULT_CFG = {
    "canary_calls": 5,          # plan §3 / task t_7a12e29a: 5-call canary
    "burst_seconds": 30,
    "burst_rate_rps": 10,       # burst probe: 10 rps x 30s = 300 calls
    "burst_concurrency": 10,
    "burst_max_tokens": 16,     # spec: max_tokens 16 on burst probes
    "effort": "low",
    "max_completion_tokens": 8192,
    "shuffle_seed": 20260822,
}


def effort_ab_subset(fixtures: dict, per_shape: int = 5) -> list[dict]:
    """Deterministic 20-item subset for the low-vs-max effort A/B (plan §3.2).

    5 items per primary shape, chosen by seeded shuffle so the subset is
    reproducible run-to-run and covers all four shapes.
    """
    by_shape: dict[str, list[str]] = {}
    for it in fixtures["primary"]:
        by_shape.setdefault(it["shape"], []).append(it["id"])
    picked: list[str] = []
    for shape in sorted(by_shape):
        ids = by_shape[shape]
        order = blind_shuffle(len(ids), seed=DEFAULT_CFG["shuffle_seed"])
        picked.extend(ids[i] for i in order[:per_shape])
    fx_by_id = {it["id"]: it for it in fixtures["primary"]}
    return [fx_by_id[i] for i in picked]


def _messages_for(item: dict) -> list[dict]:
    return [{"role": "user", "content": item["prompt"]}]


def _run_paired_set(client_ox, client_base, items, cfg, effort_override=None):
    """Run each item on ox (blind order) + base. Returns per-item records."""
    order = blind_shuffle(len(items), seed=cfg.get("shuffle_seed", 20260822))
    records = []
    effort = effort_override or cfg.get("effort", "low")
    for idx in order:
        it = items[idx]
        rec = {"id": it["id"], "shape": it["shape"]}
        ox = client_ox.ox_chat(_messages_for(it), effort=effort,
                               max_tokens=cfg.get("max_completion_tokens", 8192))
        base = client_base.base_chat(_messages_for(it),
                                     max_tokens=cfg.get("max_completion_tokens", 8192))
        rec["ox"] = {"content": ox["content"], "latency_s": ox["latency_s"],
                     "status": ox["status"], "usage": ox.get("usage") or {},
                     "error": ox.get("error")}
        rec["base"] = {"content": base["content"], "latency_s": base["latency_s"],
                       "status": base["status"], "usage": base.get("usage") or {},
                       "error": base.get("error")}
        records.append(rec)
    return records


def _run_burst(client_ox, cfg):
    """Burst probe: fixed rps for N seconds, record 429s + rate headers."""
    offsets = build_burst_schedule(cfg["burst_rate_rps"], cfg["burst_seconds"])
    burst_max_tokens = int(cfg.get("burst_max_tokens", 16))
    rl_seen: dict[str, set] = {}

    def _note_rate_headers(headers: dict) -> None:
        for k, v in (headers or {}).items():
            lk = k.lower()
            if lk.startswith("x-ratelimit") or lk in ("retry-after", "x-ratelimit-*"):
                rl_seen.setdefault(lk, set()).add(str(v))

    def fire(offset):
        sleep = offset - (time.monotonic() - t0) if offset else 0.0
        if sleep > 0:
            time.sleep(sleep)
        probe_prompt = [{"role": "user", "content":
                         "Reply with the single word: ok"}]
        r = client_ox.ox_chat(probe_prompt, effort="low",
                              max_tokens=burst_max_tokens)
        _note_rate_headers(r.get("headers"))
        return {"status": r["status"], "latency_s": r["latency_s"],
                "headers": r.get("headers") or {}}

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=cfg.get("burst_concurrency", 10)) as pool:
        results = list(pool.map(fire, offsets))
    lat = [r["latency_s"] for r in results]
    header_names = sorted({h.lower() for r in results for h in r["headers"]})
    return {
        "rate_rps": cfg["burst_rate_rps"],
        "seconds": cfg["burst_seconds"],
        "total": len(results),
        "n_200": sum(1 for r in results if r["status"] == 200),
        "n_429": sum(1 for r in results if r["status"] == 429),
        "n_5xx": sum(1 for r in results if r["status"] >= 500),
        "p50_latency_s": percentile(lat, 50) if lat else None,
        "p95_latency_s": percentile(lat, 95) if lat else None,
        "header_names": header_names,
        # spec: record ALL x-ratelimit-* / retry-after header VALUES seen
        "rate_limit_headers": {k: sorted(v) for k, v in sorted(rl_seen.items())},
    }


def run_staged_ramp(client, fixtures, cfg=None, raise_on_abort: bool = False) -> dict:
    """The campaign. `client` needs models_info/key_info/ox_chat/base_chat.

    Stages: verify_pricing -> canary -> full_set -> burst_probe, each gated on
    the usage delta from client.key_info() staying exactly zero (plan §2.4).
    """
    cfg = dict(DEFAULT_CFG, **(cfg or {}))
    guard = PromoTierGuard.from_config(None)
    gate = SpendGate(guard=guard)
    stages: list[str] = []
    deltas: list[float] = []
    res = {"aborted": False, "abort_reason": None, "anomaly_row": None,
           "stages": stages, "usage_evidence": {"deltas": deltas}}

    def _abort(reason, row=None):
        res["aborted"] = True
        res["abort_reason"] = reason
        res["anomaly_row"] = row
        if raise_on_abort:
            raise CampaignAbort(reason, row)

    # stage 1: verify pricing is $0 promo before ANY chat traffic
    stages.append("verify_pricing")
    try:
        models = client.models_info()
        entry = next((m for m in models.get("data", [])
                      if m.get("id") == OX_MODEL), None)
        pricing = (entry or {}).get("pricing") or {}
        prompt_price = float(pricing.get("prompt") or -1)
        completion_price = float(pricing.get("completion") or -1)
    except Exception as e:  # noqa: BLE001
        _abort(f"verify_pricing failed: {e}")
        return res
    if prompt_price != 0 or completion_price != 0:
        _abort(f"pricing not $0: prompt={prompt_price} completion={completion_price}")
        return res
    res["pricing_evidence"] = {"prompt": prompt_price, "completion": completion_price}

    u = client.key_info()
    res["key_before"] = dict(u)
    gate.snapshot(u["usage"])

    # stage 2: canary — ox-only, tiny
    stages.append("canary")
    canary_items = fixtures["primary"][:cfg["canary_calls"]]
    canary = []
    for it in canary_items:
        r = client.ox_chat(_messages_for(it), effort=cfg["effort"],
                           max_tokens=cfg.get("max_completion_tokens", 8192))
        canary.append({"id": it["id"], "status": r["status"],
                       "latency_s": r["latency_s"], "content": r["content"]})
    res["canary"] = canary
    u = client.key_info()
    res["key_after_canary"] = dict(u)
    row = gate.check(u["usage"])
    if u["usage"] != res["key_before"]["usage"]:
        deltas.append(round(u["usage"] - res["key_before"]["usage"], 8))
    if row is not None:
        _abort(f"usage delta after canary: {u['usage']}", row)
        return res

    # stage 3: full set — paired ox/base, blind order
    stages.append("full_set")
    res["paired_records"] = _run_paired_set(client, client, fixtures["primary"], cfg)

    # refusal probes (ox-only): correct behavior on these is COMPLY; a
    # refusal here is the political-guardrail signal we're measuring
    res["refusal_probe_records"] = []
    for p in fixtures["refusal_probes"]:
        r = client.ox_chat(_messages_for(p), effort=cfg["effort"],
                           max_tokens=cfg.get("max_completion_tokens", 8192))
        res["refusal_probe_records"].append(
            {"id": p["id"], "status": r["status"], "latency_s": r["latency_s"],
             "content": r["content"]})

    # latency micro-set (ox-only): 1-3k-token digest prompts — criterion 4
    res["latency_micro_records"] = []
    for m in fixtures["latency_micro"]:
        r = client.ox_chat(_messages_for(m), effort=cfg["effort"],
                           max_tokens=cfg.get("max_completion_tokens", 8192))
        res["latency_micro_records"].append(
            {"id": m["id"], "status": r["status"], "latency_s": r["latency_s"],
             "content": r["content"]})

    # effort A/B on the deterministic 20-item subset: re-run ox at effort=max
    ab_items = effort_ab_subset(fixtures)
    res["effort_max_records"] = [
        {"id": it["id"], "shape": it["shape"],
         "content": (r := client.ox_chat(
             _messages_for(it), effort="max",
             max_tokens=cfg.get("max_completion_tokens", 8192)))["content"],
         "status": r["status"]}
        for it in ab_items]

    u = client.key_info()
    res["key_after_full_set"] = dict(u)
    row = gate.check(u["usage"])
    prev = res["key_after_canary"]["usage"]
    if u["usage"] != prev:
        deltas.append(round(u["usage"] - prev, 8))
    if row is not None:
        _abort(f"usage delta after full set: {u['usage']}", row)
        return res

    # stage 4: burst probe
    stages.append("burst_probe")
    res["burst"] = _run_burst(client, cfg)
    u = client.key_info()
    res["key_after_burst"] = dict(u)
    row = gate.check(u["usage"])
    prev = res["key_after_full_set"]["usage"]
    if u["usage"] != prev:
        deltas.append(round(u["usage"] - prev, 8))
    if row is not None:
        _abort(f"usage delta after burst: {u['usage']}", row)
        return res

    return res


# ── CLI ──────────────────────────────────────────────────────────────────────

def _load_key() -> str:
    key = os.environ.get(KEY_ENV)
    if key:
        return key
    env_path = os.path.expanduser("~/.hermes/.env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith(KEY_ENV + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit(f"{KEY_ENV} not set and not found in ~/.hermes/.env")


def _live_client():
    ox = OxClient(_load_key())
    base = BaseClient()

    class Combined:
        def models_info(self):
            return ox.models_info()

        def key_info(self):
            return ox.key_info()

        def ox_chat(self, messages, effort="low", max_tokens=8192, model=None):
            return ox.ox_chat(messages, effort=effort, max_tokens=max_tokens, model=model)

        def base_chat(self, messages, max_tokens=8192, model=None):
            return base.base_chat(messages, max_tokens=max_tokens, model=model)

    return Combined()


def _check_proxy_health() -> bool:
    try:
        r = requests.get(f"{PROXY_BASE}/health", timeout=5)
        return r.status_code == 200
    except requests.RequestException:
        return False


def cmd_verify(args):
    """Pre-live verification: pricing $0 + key usage/limit + proxy health."""
    client = _live_client()
    print(f"proxy {PROXY_BASE}/health: ", end="")
    print("OK" if _check_proxy_health() else "DOWN")
    models = client.models_info()
    entry = next((m for m in models.get("data", []) if m.get("id") == OX_MODEL), None)
    if entry is None:
        print(f"FAIL: {OX_MODEL} not in /models")
        return 1
    pricing = entry.get("pricing") or {}
    print(f"{OX_MODEL} pricing: prompt={pricing.get('prompt')} "
          f"completion={pricing.get('completion')}")
    key = client.key_info()
    print(f"key usage={key['usage']} limit={key['limit']}")
    ok = (float(pricing.get("prompt") or -1) == 0
          and float(pricing.get("completion") or -1) == 0)
    print("VERIFY:", "OK" if ok else "NOT-$0 — DO NOT RUN")
    return 0 if ok else 1


def cmd_run(args):
    fixtures = load_fixtures(FIXDIR)
    errs = validate_fixture_set(fixtures)
    if errs:
        print("fixture validation failed:", errs)
        return 1
    client = _live_client()
    if not _check_proxy_health():
        print(f"proxy {PROXY_BASE} DOWN — baseline arm unavailable")
        return 1
    res = run_staged_ramp(client, fixtures, cfg=None)
    os.makedirs(RESULTDIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = os.path.join(RESULTDIR, f"campaign-{stamp}.json")
    with open(out, "w") as f:
        json.dump(res, f, indent=2)
    print(json.dumps({k: v for k, v in res.items()
                      if k in ("aborted", "abort_reason", "stages",
                               "usage_evidence", "burst")}, indent=2))
    print(f"results: {out}")
    return 0 if not res["aborted"] else 2


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("verify", help="pre-live checks (pricing/key/health)")
    sub.add_parser("run", help="staged-ramp campaign")
    args = p.parse_args(argv)
    if args.cmd == "verify":
        return cmd_verify(args)
    if args.cmd == "run":
        return cmd_run(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
