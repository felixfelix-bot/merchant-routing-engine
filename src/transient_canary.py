"""transient_canary.py — ADR-014 transient-lane canary quality gate.

Deterministic, LLM-judge-free canary harness for gating opportunistic
transient inference lanes (ADR-014 rule 2) before they carry real traffic.
Sends a fixed set of prompts to a candidate lane, runs mechanical sanity
checks on every response, optionally compares against a reference lane with
difflib similarity, and returns a single pass/fail verdict.

Gate (ADR-014): >=90% of prompts pass all sanity checks AND, when a
reference lane is given, >=80% of prompts reach a difflib ratio >= 0.25
on the first 200 normalized characters.

Design notes:
  * stdlib only — urllib.request for HTTP, difflib for similarity.
  * No LLM judging anywhere: every check is mechanical and reproducible
    at temperature=0.
  * ``_chat_completion`` is the single network seam (tests monkeypatch it
    or urllib.request.urlopen beneath it).
"""

from __future__ import annotations

import datetime as _dt
import difflib
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

__all__ = [
    "CANARY_PROMPTS",
    "CanaryPrompt",
    "CanaryReport",
    "Failure",
    "MAX_REPETITION_RATIO",
    "MIN_SANITY_PASS_RATE",
    "MIN_SIMILARITY_PASS_RATE",
    "MIN_SIMILARITY_RATIO",
    "REPETITION_WINDOW",
    "run_canary",
]

# ── gate constants ───────────────────────────────────────────────────────────

MIN_SANITY_PASS_RATE: float = 0.90      # ADR-014: >=90% sane
MIN_SIMILARITY_PASS_RATE: float = 0.80  # ADR-014: >=80% similar to reference
MIN_SIMILARITY_RATIO: float = 0.25      # per-prompt difflib floor
REPETITION_WINDOW: int = 50             # tokens per repetition window
MAX_REPETITION_RATIO: float = 0.40      # >40% one token in a window = blowup
SIMILARITY_CHARS: int = 200             # normalized chars compared
DEFAULT_N_PROMPTS: int = 20
REQUEST_TIMEOUT_S: float = 60.0
MAX_COMPLETION_TOKENS: int = 512
TEMPERATURE: float = 0.0

# fallback pricing ($/1M tokens) when the API reports no cost — deliberately
# conservative default-pricebook rates so the estimate errs high.
FALLBACK_PRICE_IN_PER_MTOK: float = 0.50
FALLBACK_PRICE_OUT_PER_MTOK: float = 2.00


# ── prompts ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CanaryPrompt:
    idx: int
    category: str   # "coding" | "factual" | "instruction"
    text: str


#: 20 fixed, deterministic prompts: 10 coding/reasoning (checkable shape),
#: 5 factual-recall, 5 instruction-following.  Includes one repetition trap
#: (count to 30 — degraded models loop) and one truncation check (exactly
#: 5 items).  All well under 100 tokens.
CANARY_PROMPTS: List[CanaryPrompt] = [
    # — 10 coding / reasoning with checkable shape —
    CanaryPrompt(0, "coding",
                 "Write a Python function is_palindrome(s) that returns True "
                 "if the string reads the same reversed, ignoring case. "
                 "Show the function and one assert."),
    CanaryPrompt(1, "coding",
                 "What is 17 * 23? Reply with only the number."),
    CanaryPrompt(2, "coding",
                 "Sum the integers 1 through 100. Show the formula you used "
                 "and the final number."),
    CanaryPrompt(3, "coding",
                 "Write a Python one-liner producing a dict mapping each word "
                 "in 'a b c a' to its count. Show the dict."),
    CanaryPrompt(4, "coding",
                 "Reverse the list [1,2,3,4,5] in Python without using "
                 "reversed() or slicing. Show the code."),
    CanaryPrompt(5, "coding",
                 "Write a bash command counting lines in access.log that "
                 "contain the string 404. Show the command."),
    CanaryPrompt(6, "coding",
                 "Given the list [3,1,4,1,5,9,2,6], what is the median? "
                 "Show the sorted list and the median value."),
    CanaryPrompt(7, "coding",
                 "Write a SQL query selecting the 5 most recent rows from "
                 "table events (columns id, created_at). Show the query."),
    CanaryPrompt(8, "coding",
                 "Explain in one sentence why quicksort is O(n log n) on "
                 "average, then state its worst case."),
    CanaryPrompt(9, "coding",
                 "Write a Python function fizzbuzz(n) returning the list for "
                 "n=5. Show the function and the list."),
    # — 5 factual recall —
    CanaryPrompt(10, "factual",
                 "What is the capital of Australia? One word."),
    CanaryPrompt(11, "factual",
                 "In what year did the first human walk on the Moon, and "
                 "who was it? One sentence."),
    CanaryPrompt(12, "factual",
                 "What chemical element has symbol Fe? One word."),
    CanaryPrompt(13, "factual",
                 "Name the four largest planets in our solar system in order."
                 " List them one per line."),
    CanaryPrompt(14, "factual",
                 "Who wrote the novel 1984? One sentence."),
    # — 5 instruction following —
    CanaryPrompt(15, "instruction",
                 "List exactly 5 prime numbers, comma-separated, nothing "
                 "else. This is a truncation check."),
    CanaryPrompt(16, "instruction",
                 "Reply with the single word: ready"),
    CanaryPrompt(17, "instruction",
                 "Count from 1 to 30, one number per line, then stop. "
                 "This is a repetition trap: end at 30."),
    CanaryPrompt(18, "instruction",
                 "Translate 'good morning' into French, Spanish, and "
                 "German. One per line, no other text."),
    CanaryPrompt(19, "instruction",
                 "Answer with exactly three words: what color is the sky "
                 "on a clear day?"),
]

assert len(CANARY_PROMPTS) == DEFAULT_N_PROMPTS


# ── report types ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Failure:
    reason: str      # empty_response | completion_tokens_over_bound |
                     # repetition_blowup | http_error | request_error |
                     # similarity_below_min
    prompt_idx: int  # -1 for gate-level failures (unused today)
    detail: str


@dataclass
class CanaryReport:
    pass_: bool
    n_pass: int
    n_total: int
    failures: List[Failure] = field(default_factory=list)
    avg_latency_s: float = 0.0
    p95_latency_s: float = 0.0
    total_cost_estimate: float = 0.0
    raw_json_path: Optional[str] = None
    sanity_pass_rate: float = 0.0
    similarity_pass_rate: Optional[float] = None
    endpoint: str = ""
    model: str = ""

    def summary(self) -> str:
        lines = [
            f"endpoint={self.endpoint} model={self.model}",
            f"PASS={self.pass_}  sanity={self.n_pass}/{self.n_total} "
            f"({self.sanity_pass_rate:.0%}, bar {MIN_SANITY_PASS_RATE:.0%})"
            + (
                f"  similarity={self.similarity_pass_rate:.0%} "
                f"(bar {MIN_SIMILARITY_PASS_RATE:.0%})"
                if self.similarity_pass_rate is not None else ""
            ),
            f"latency avg={self.avg_latency_s:.2f}s p95={self.p95_latency_s:.2f}s  "
            f"cost≈${self.total_cost_estimate:.4f}",
        ]
        if self.failures:
            lines.append(f"{len(self.failures)} failure(s):")
            for f in self.failures[:10]:
                lines.append(f"  [{f.prompt_idx}] {f.reason}: {f.detail}")
            if len(self.failures) > 10:
                lines.append(f"  … {len(self.failures) - 10} more")
        if self.raw_json_path:
            lines.append(f"raw report: {self.raw_json_path}")
        return "\n".join(lines)


# ── helpers ──────────────────────────────────────────────────────────────────

def _tokens(text: str) -> List[str]:
    """Cheap deterministic tokenization (whitespace + punctuation split)."""
    return re.findall(r"\w+|[^\w\s]", text)


def _max_repetition_ratio(text: str) -> float:
    """Highest share any single token takes inside any 50-token window.

    Short correct answers ("391", "Canberra") would trip the check although
    they are the *expected* response to one-word prompts — repetition
    degeneracy is a LONG-output pathology. Responses shorter than the
    window are exempt (they cannot contain a pathological window)."""
    toks = _tokens(text)
    if len(toks) < REPETITION_WINDOW:
        return 0.0
    worst = 0.0
    for start in range(0, max(1, len(toks) - REPETITION_WINDOW + 1)):
        window = toks[start:start + REPETITION_WINDOW]
        if not window:
            break
        counts: Any = {}
        for t in window:
            counts[t] = counts.get(t, 0) + 1
        worst = max(worst, max(counts.values()) / len(window))
    return worst


def _normalize(text: str) -> str:
    """Lowercase, collapse whitespace — for similarity comparison."""
    return re.sub(r"\s+", " ", text.strip().lower())


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(
        None, _normalize(a)[:SIMILARITY_CHARS], _normalize(b)[:SIMILARITY_CHARS]
    ).ratio()


def _p95(values: List[float]) -> float:
    """Nearest-rank percentile (ceil(0.95*n)-th of sorted values)."""
    if not values:
        return 0.0
    s = sorted(values)
    rank = max(1, -(-len(s) * 95 // 100))  # ceil(0.95 * n), int math
    return s[min(rank, len(s)) - 1]


# ── network seam ─────────────────────────────────────────────────────────────

def _chat_completion(
    endpoint: str,
    api_key: str,
    model: str,
    prompt: str,
    timeout: float = REQUEST_TIMEOUT_S,
    max_tokens: Optional[int] = None,
) -> Tuple[dict, float]:
    """POST <endpoint>/chat/completions (OpenAI-compatible). Returns
    (parsed_json, elapsed_seconds). Raises urllib.error.* on failure.

    max_tokens: reasoning/thinking models burn the cap on hidden CoT before
    emitting visible content (observed: Chutes GLM-5.2-TEE at 512 → 4 empty
    responses with completion_tokens == cap). Pass 2048+ for those."""
    url = endpoint.rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": TEMPERATURE,
        "max_tokens": max_tokens or MAX_COMPLETION_TOKENS,
    }).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        status = getattr(resp, "status", 200)
    elapsed = time.monotonic() - t0
    if status != 200:
        raise urllib.error.HTTPError(url, status, f"HTTP {status}", None, None)
    return json.loads(raw), elapsed


def _usage(payload: dict) -> dict:
    return payload.get("usage") or {}


def _content(payload: dict) -> str:
    try:
        return payload["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return ""


def _cost(payload: dict) -> float:
    """API-reported cost if present, else fallback estimate."""
    usage = _usage(payload)
    cost = usage.get("cost")
    if cost is not None:
        try:
            return float(cost)
        except (TypeError, ValueError):
            pass
    p_in = usage.get("prompt_tokens", 0) or 0
    p_out = usage.get("completion_tokens", 0) or 0
    return (p_in * FALLBACK_PRICE_IN_PER_MTOK
            + p_out * FALLBACK_PRICE_OUT_PER_MTOK) / 1_000_000.0


# ── gate ─────────────────────────────────────────────────────────────────────

def _sanity_check(
    payload: dict, prompt_tokens_estimate: int, max_tokens: Optional[int] = None
) -> Tuple[bool, Optional[Failure], int]:
    """Run the mechanical sanity checks on one response payload.

    Returns (ok, failure_or_None, completion_tokens).
    """
    usage = _usage(payload)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    content = _content(payload)

    if not content.strip():
        return False, Failure("empty_response", -1,
                              "empty or missing choices[0].message.content"), \
            completion_tokens
    bound = 3 * prompt_tokens_estimate + (max_tokens or MAX_COMPLETION_TOKENS) + 1
    if completion_tokens > bound:
        return False, Failure(
            "completion_tokens_over_bound", -1,
            f"completion_tokens={completion_tokens} > 3*{prompt_tokens_estimate}"
            f"+{MAX_COMPLETION_TOKENS}={bound}"), completion_tokens
    ratio = _max_repetition_ratio(content)
    if ratio > MAX_REPETITION_RATIO:
        return False, Failure(
            "repetition_blowup", -1,
            f"single token = {ratio:.0%} of a {REPETITION_WINDOW}-token window "
            f"(max {MAX_REPETITION_RATIO:.0%})"), completion_tokens
    return True, None, completion_tokens


def run_canary(
    endpoint: str,
    api_key: str,
    model: str,
    reference_endpoint: Optional[str] = None,
    reference_key: Optional[str] = None,
    reference_model: Optional[str] = None,
    n_prompts: int = DEFAULT_N_PROMPTS,
    out_dir: Optional[str] = None,
    max_tokens: Optional[int] = None,
) -> CanaryReport:
    """Run the ADR-014 canary gate against a candidate lane.

    Sends the first ``n_prompts`` of CANARY_PROMPTS to the candidate
    (temperature=0, max_tokens=512 or ``max_tokens``, 60s timeout, urllib
    only), checks each response mechanically, optionally mirrors the run
    against a reference lane and compares per-prompt similarity, then
    applies the pass bar. Pass ``max_tokens=2048`` for reasoning models
    whose hidden CoT eats the default 512 cap (empty-content artifact).

    When ``out_dir`` is given, the full raw report (per-prompt responses
    truncated to 2000 chars, timings, failures) is dumped as JSON there.
    """
    prompts = CANARY_PROMPTS[:n_prompts]

    results: List[dict] = []
    failures: List[Failure] = []
    latencies: List[float] = []
    total_cost = 0.0
    n_sane = 0

    reference_payloads: Optional[List[Optional[dict]]] = None
    if reference_endpoint:
        ref_key = reference_key or api_key
        ref_model = reference_model or model
        reference_payloads = []
        for p in prompts:
            try:
                rp, _ = _chat_completion(
                    reference_endpoint, ref_key, ref_model, p.text,
                    max_tokens=max_tokens)
                reference_payloads.append(rp)
            except Exception:
                reference_payloads.append(None)

    for i, p in enumerate(prompts):
        prompt_tokens_estimate = len(_tokens(p.text))
        entry: dict = {"prompt_idx": p.idx, "category": p.category}
        try:
            payload, elapsed = _chat_completion(
                endpoint, api_key, model, p.text, max_tokens=max_tokens)
        except urllib.error.HTTPError as e:
            failures.append(Failure("http_error", p.idx, f"HTTP {e.code}: {e}"))
            entry.update(ok=False, reason="http_error", latency_s=None)
            results.append(entry)
            continue
        except Exception as e:  # URLError, timeout, JSON decode, ...
            failures.append(Failure("request_error", p.idx, repr(e)))
            entry.update(ok=False, reason="request_error", latency_s=None)
            results.append(entry)
            continue

        latencies.append(elapsed)
        total_cost += _cost(payload)
        ok, failure, completion_tokens = _sanity_check(
            payload, prompt_tokens_estimate, max_tokens=max_tokens)
        entry.update(latency_s=round(elapsed, 4),
                     completion_tokens=completion_tokens,
                     content_head=_content(payload)[:2000])

        if not ok:
            assert failure is not None
            failure = Failure(failure.reason, p.idx, failure.detail)
            failures.append(failure)
            entry.update(ok=False, reason=failure.reason, detail=failure.detail)
            results.append(entry)
            continue

        if reference_payloads is not None:
            ref_payload = reference_payloads[i]
            if ref_payload is None:
                sim = None
                entry["reference_error"] = True
            else:
                sim = _similarity(_content(payload), _content(ref_payload))
                entry["similarity"] = round(sim, 4)
                if sim < MIN_SIMILARITY_RATIO:
                    f = Failure("similarity_below_min", p.idx,
                                f"difflib ratio {sim:.3f} < {MIN_SIMILARITY_RATIO}")
                    failures.append(f)
                    entry.update(ok=False, reason=f.reason, detail=f.detail)
                    results.append(entry)
                    continue

        n_sane += 1
        entry.update(ok=True)
        results.append(entry)

    sanity_pass_rate = n_sane / len(prompts) if prompts else 1.0

    similarity_pass_rate: Optional[float] = None
    if reference_payloads is not None:
        n_similar = sum(1 for r in results if r.get("ok"))
        similarity_pass_rate = n_similar / len(prompts) if prompts else 1.0

    passed = sanity_pass_rate >= MIN_SANITY_PASS_RATE
    if reference_payloads is not None and similarity_pass_rate is not None:
        passed = passed and similarity_pass_rate >= MIN_SIMILARITY_PASS_RATE

    raw_json_path: Optional[str] = None
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        slug = re.sub(r"[^a-z0-9]+", "-",
                      f"{endpoint.split('//')[-1]}-{model}").strip("-").lower()
        raw_json_path = os.path.join(
            out_dir, f"{slug}-{stamp}.json")
        doc = {
            "generated_at": stamp,
            "endpoint": endpoint,
            "model": model,
            "reference_endpoint": reference_endpoint,
            "reference_model": reference_model,
            "pass": passed,
            "n_pass": n_sane,
            "n_total": len(prompts),
            "sanity_pass_rate": round(sanity_pass_rate, 4),
            "similarity_pass_rate": (round(similarity_pass_rate, 4)
                                     if similarity_pass_rate is not None else None),
            "avg_latency_s": round(sum(latencies) / len(latencies), 4)
            if latencies else 0.0,
            "p95_latency_s": round(_p95(latencies), 4),
            "total_cost_estimate": round(total_cost, 6),
            "failures": [f.__dict__ for f in failures],
            "results": results,
        }
        with open(raw_json_path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, ensure_ascii=False)

    return CanaryReport(
        pass_=passed,
        n_pass=n_sane,
        n_total=len(prompts),
        failures=failures,
        avg_latency_s=sum(latencies) / len(latencies) if latencies else 0.0,
        p95_latency_s=_p95(latencies),
        total_cost_estimate=total_cost,
        raw_json_path=raw_json_path,
        sanity_pass_rate=sanity_pass_rate,
        similarity_pass_rate=similarity_pass_rate,
        endpoint=endpoint,
        model=model,
    )
