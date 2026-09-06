"""Tests for src/transient_canary.py — ADR-014 transient-lane canary gate.

Covers the ADR-014 quality-gate mechanics: per-prompt sanity checks
(non-empty, length bound, repetition window, HTTP 200), optional reference
similarity (difflib ratio on normalized first-200-chars), the pass bar
(>=90% sane AND >=80% similar when a reference is given), latency
aggregation (avg + p95), cost estimate, raw-JSON dump, and the CLI exit
codes (0 pass / 1 fail / 2 error).  Pure unit: the network seam
(urllib.request.urlopen / _chat_completion) is monkeypatched everywhere.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import src.transient_canary as tc
from src.transient_canary import (
    CANARY_PROMPTS,
    CanaryReport,
    Failure,
    MAX_REPETITION_RATIO,
    MIN_SANITY_PASS_RATE,
    MIN_SIMILARITY_PASS_RATE,
    MIN_SIMILARITY_RATIO,
    REPETITION_WINDOW,
    run_canary,
)


# ── fake HTTP plumbing ───────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _payload(text, prompt_tokens=30, completion_tokens=20, cost=None):
    usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}
    if cost is not None:
        usage["cost"] = cost
    return {
        "choices": [{"message": {"content": text}}],
        "usage": usage,
    }


def _patch_urlopen(monkeypatch, payloads, latencies=None):
    """payloads: callable(idx, endpoint_kind) -> payload dict | Exception."""
    calls = {"n": 0, "endpoints": []}
    lat = iter(latencies or [])

    def fake_urlopen(req, timeout=None):
        idx = calls["n"]
        calls["n"] += 1
        url = req.full_url if hasattr(req, "full_url") else str(req)
        kind = "ref" if "REFERENCE" in url else "main"
        calls["endpoints"].append(url)
        result = payloads(idx, kind)
        if isinstance(result, Exception):
            raise result
        # burn the latency side of the list to keep iteration aligned
        try:
            next(lat)
        except StopIteration:
            pass
        return _FakeResp(result)

    monkeypatch.setattr(tc.urllib.request, "urlopen", fake_urlopen)
    return calls


def _good_text(idx, prefix="ack "):
    # deterministic, non-repetitive, differs per prompt
    return f"{prefix}response number {idx} with varied filler words {idx * 7} unique."


# ── prompts fixture sanity ───────────────────────────────────────────────────

def test_canary_prompts_fixture_shape():
    assert len(CANARY_PROMPTS) == 20
    cats = [p.category for p in CANARY_PROMPTS]
    assert cats.count("coding") == 10
    assert cats.count("factual") == 5
    assert cats.count("instruction") == 5
    for p in CANARY_PROMPTS:
        assert len(p.text.split()) < 100, "prompt must stay under ~100 tokens"
    # repetition trap + truncation check are present
    joined = " ".join(p.text.lower() for p in CANARY_PROMPTS)
    assert "1 to 30" in joined            # repetition trap
    assert "exactly 5 prime" in joined    # truncation check


# ── happy path ───────────────────────────────────────────────────────────────

def test_happy_path_passes_with_reference(tmp_path, monkeypatch):
    def payloads(idx, kind):
        text = _good_text(idx)  # identical on main + reference → similarity 1.0
        return _payload(text, prompt_tokens=30, completion_tokens=20)

    _patch_urlopen(monkeypatch, payloads)

    report = run_canary(
        "https://main.example/v1",
        "sk-main",
        "test-model",
        reference_endpoint="https://REFERENCE.example/v1",
        reference_key="sk-ref",
        reference_model="ref-model",
        out_dir=str(tmp_path),
    )

    assert isinstance(report, CanaryReport)
    assert report.pass_ is True
    assert report.n_total == 20
    assert report.n_pass == 20
    assert report.failures == []
    assert report.raw_json_path is not None
    assert os.path.exists(report.raw_json_path)
    raw = json.load(open(report.raw_json_path))
    assert raw["n_total"] == 20
    assert len(raw["results"]) == 20
    # main + reference = 40 calls
    assert report.total_cost_estimate > 0


def test_happy_path_passes_without_reference(tmp_path, monkeypatch):
    def payloads(idx, kind):
        return _payload(_good_text(idx))

    _patch_urlopen(monkeypatch, payloads)
    report = run_canary("https://main.example/v1", "k", "m", out_dir=str(tmp_path))
    assert report.pass_ is True
    assert report.n_pass == 20
    # no out_dir → no raw dump
    report2 = run_canary("https://main.example/v1", "k", "m")
    assert report2.raw_json_path is None


# ── failure modes ────────────────────────────────────────────────────────────

def test_repetition_blowup_fails(tmp_path, monkeypatch):
    loop = "loop " * 300  # every 50-token window is 100% the same token

    def payloads(idx, kind):
        return _payload(loop, prompt_tokens=30, completion_tokens=300)

    _patch_urlopen(monkeypatch, payloads)
    report = run_canary("https://main.example/v1", "k", "m", out_dir=str(tmp_path))

    assert report.pass_ is False
    assert report.n_pass == 0
    reasons = {f.reason for f in report.failures}
    assert "repetition_blowup" in reasons
    assert all(f.prompt_idx >= 0 for f in report.failures)


def test_empty_response_fails(tmp_path, monkeypatch):
    def payloads(idx, kind):
        return _payload("")  # empty content

    _patch_urlopen(monkeypatch, payloads)
    report = run_canary("https://main.example/v1", "k", "m", out_dir=str(tmp_path))

    assert report.pass_ is False
    assert report.n_pass == 0
    reasons = {f.reason for f in report.failures}
    assert "empty_response" in reasons


def test_completion_length_bound_fails(tmp_path, monkeypatch):
    def payloads(idx, kind):
        # prompt_tokens=10 → bound = 3*10 + 512 = 542; completion 543 breaks it
        return _payload("x " * 600, prompt_tokens=10, completion_tokens=543)

    _patch_urlopen(monkeypatch, payloads)
    report = run_canary("https://main.example/v1", "k", "m", out_dir=str(tmp_path))
    assert report.pass_ is False
    assert "completion_tokens_over_bound" in {f.reason for f in report.failures}


def test_http_error_fails(tmp_path, monkeypatch):
    import urllib.error

    def payloads(idx, kind):
        return urllib.error.HTTPError(
            "https://main.example/v1/chat/completions", 500, "boom", None, None
        )

    _patch_urlopen(monkeypatch, payloads)
    report = run_canary("https://main.example/v1", "k", "m", out_dir=str(tmp_path))
    assert report.pass_ is False
    assert "http_error" in {f.reason for f in report.failures}


def test_similarity_below_bar_fails(tmp_path, monkeypatch):
    def payloads(idx, kind):
        if kind == "ref":
            text = "zeta omega psi chi upsilon lambda tau sigma rho nu " + "γ" * 30
        else:
            text = _good_text(idx, prefix="alpha ")
        return _payload(text)

    _patch_urlopen(monkeypatch, payloads)
    report = run_canary(
        "https://main.example/v1", "k", "m",
        reference_endpoint="https://REFERENCE.example/v1",
        reference_key="rk", reference_model="rm",
        out_dir=str(tmp_path),
    )

    # sanity all fine; similarity drags the gate below the bar
    assert report.pass_ is False
    assert "similarity_below_min" in {f.reason for f in report.failures}


def test_pass_bar_ninety_percent(tmp_path, monkeypatch):
    # exactly 2/20 prompts insane → 18/20 = 90% → still a pass (>= bar)
    def payloads(idx, kind):
        text = "" if idx in (3, 7) else _good_text(idx)
        return _payload(text)

    _patch_urlopen(monkeypatch, payloads)
    report = run_canary("https://main.example/v1", "k", "m", out_dir=str(tmp_path))
    assert report.n_pass == 18
    assert report.pass_ is True
    assert MIN_SANITY_PASS_RATE == pytest.approx(0.90)
    assert MIN_SIMILARITY_PASS_RATE == pytest.approx(0.80)


# ── latency math + cost ──────────────────────────────────────────────────────

def test_latency_and_cost_math(tmp_path, monkeypatch):
    latencies = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0,
                 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    payloads = [{"choices": [{"message": {"content": _good_text(i)}}],
                 "usage": {"prompt_tokens": 100, "completion_tokens": 50,
                           "cost": 0.01}}
                for i in range(20)]
    state = {"i": 0}
    lat_iter = iter(latencies)

    def counting(endpoint, api_key, model, prompt, timeout=60.0):
        i = state["i"]
        state["i"] += 1
        return payloads[i], next(lat_iter)

    monkeypatch.setattr(tc, "_chat_completion", counting)
    report = run_canary("https://main.example/v1", "k", "m", out_dir=str(tmp_path))

    assert report.n_pass == 20
    assert report.avg_latency_s == pytest.approx(0.55)
    assert report.p95_latency_s == pytest.approx(1.0)  # 20 samples → rank 19 = 1.0
    # API-reported cost is used verbatim
    assert report.total_cost_estimate == pytest.approx(0.01 * 20)


def test_cost_fallback_estimate(tmp_path, monkeypatch):
    def payloads(idx, kind):
        return _payload(_good_text(idx), prompt_tokens=1_000_000,
                        completion_tokens=1_000_000)  # no "cost" field

    _patch_urlopen(monkeypatch, payloads)
    report = run_canary("https://main.example/v1", "k", "m", out_dir=str(tmp_path))
    # 1M in @ 0.50 + 1M out @ 2.00 per prompt
    assert report.total_cost_estimate == pytest.approx((0.50 + 2.00) * 20)


# ── repetition-window unit checks ────────────────────────────────────────────

def test_repetition_window_constants_and_helper():
    assert REPETITION_WINDOW == 50
    assert MAX_REPETITION_RATIO == pytest.approx(0.40)
    assert MIN_SIMILARITY_RATIO == pytest.approx(0.25)
    assert tc._max_repetition_ratio("word " * 200) > 0.40
    assert tc._max_repetition_ratio(
        "the quick brown fox jumps over lazy dogs while cats nap "
        "and birds sing songs in trees near rivers under mountains"
    ) <= 0.40


# ── CLI exit codes ───────────────────────────────────────────────────────────

def _import_cli():
    scripts_dir = os.path.join(os.path.dirname(__file__), "..", "scripts")
    sys.path.insert(0, scripts_dir)
    import transient_canary_run
    return transient_canary_run


def test_cli_exit_0_pass(tmp_path, monkeypatch):
    cli = _import_cli()

    def payloads(idx, kind):
        return _payload(_good_text(idx))

    _patch_urlopen(monkeypatch, payloads)
    monkeypatch.setenv("CANARY_KEY", "sk-test")
    rc = cli.main([
        "--endpoint", "https://main.example/v1",
        "--key-env", "CANARY_KEY",
        "--model", "test-model",
        "--out", str(tmp_path),
    ])
    assert rc == 0
    # report JSON landed under reports/canary naming scheme
    files = os.listdir(tmp_path)
    assert any(f.endswith(".json") for f in files)


def test_cli_exit_1_fail(tmp_path, monkeypatch):
    cli = _import_cli()

    def payloads(idx, kind):
        return _payload("")  # empty → gate fails

    _patch_urlopen(monkeypatch, payloads)
    monkeypatch.setenv("CANARY_KEY", "sk-test")
    rc = cli.main([
        "--endpoint", "https://main.example/v1",
        "--key-env", "CANARY_KEY",
        "--model", "test-model",
        "--out", str(tmp_path),
    ])
    assert rc == 1


def test_cli_exit_2_error_missing_key(tmp_path, monkeypatch, capsys):
    cli = _import_cli()
    monkeypatch.delenv("CANARY_MISSING_KEY", raising=False)
    rc = cli.main([
        "--endpoint", "https://main.example/v1",
        "--key-env", "CANARY_MISSING_KEY",
        "--model", "test-model",
        "--out", str(tmp_path),
    ])
    assert rc == 2
