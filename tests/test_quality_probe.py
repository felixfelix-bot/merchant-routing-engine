"""Tests for scripts/quality_probe.py — canary prompts to detect silent model downgrades.

Phase 2.5.3 (Gate 1, TDD): written BEFORE the implementation.
Every DB-touching test uses a throwaway SQLite file — the production
usage DB (``~/.hermes/bot/zai_usage.db``) is never touched.

The module under test is ``scripts.quality_probe``.  It is a standalone
cron job that:
  - Sends 3 known probe prompts to each provider
  - Scores: response_received, correct_answer, latency_ms
  - Logs results to the ``provider_telemetry`` table (same as P3.3a)
  - Prints JSON output + human-readable summary
  - Exits 1 if any provider's quality drops below threshold
  - NEVER crashes (every provider wrapped in try/except)
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import sys
import tempfile

import pytest

# ── Import path setup ──────────────────────────────────────────────────────
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts import quality_probe as qp


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_db_path():
    """A fresh temp file path for an isolated SQLite DB. Cleaned up after."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="qprobe_test_")
    os.close(fd)
    os.unlink(path)  # let the probe create it fresh
    yield path
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


@pytest.fixture
def provider_configs():
    """A small provider map for tests (injected, no file parsing needed)."""
    return {
        "alpha": {"endpoint": "https://alpha.example/v1/chat/completions",
                  "key_env": "ALPHA_KEY", "model": "m1"},
        "beta": {"endpoint": "https://beta.example/v1/chat/completions",
                 "key_env": "BETA_KEY", "model": "m2"},
    }


def _fetch_probe_rows(conn: sqlite3.Connection) -> list[dict]:
    """Fetch all provider_telemetry rows that have a probe_id set."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, ts, provider, response_received, response_valid, "
        "latency_ms, error_type, probe_id, response_text, correct_answer "
        "FROM provider_telemetry WHERE probe_id IS NOT NULL ORDER BY id"
    ).fetchall()
    conn.row_factory = None
    return [dict(r) for r in rows]


# ── Tests: import + structure ──────────────────────────────────────────────────


class TestImportAndStructure:
    def test_module_importable(self):
        assert qp is not None

    def test_three_probes_defined(self):
        assert len(qp.PROBES) == 3
        for p in qp.PROBES:
            assert "probe_id" in p
            assert "prompt" in p
            assert 1 <= p["probe_id"] <= 3

    def test_probe_ids_unique_and_ordered(self):
        ids = [p["probe_id"] for p in qp.PROBES]
        assert ids == [1, 2, 3]

    def test_has_main(self):
        assert callable(qp.main)


# ── Tests: correctness evaluation (Gate 1 core) ─────────────────────────────────


class TestCorrectnessEvaluation:
    def test_correct_answer_detection_probe1(self):
        """Probe 1 ('2+2') with '4' → correct."""
        assert qp.evaluate_correctness(1, "4") is True

    def test_wrong_answer_detection_probe1(self):
        """Probe 1 ('2+2') with '5' → incorrect."""
        assert qp.evaluate_correctness(1, "5") is False

    def test_correct_probe1_embedded(self):
        """Probe 1: 'The answer is 4.' → correct (contains '4')."""
        assert qp.evaluate_correctness(1, "The answer is 4.") is True

    def test_wrong_probe1_empty(self):
        """Probe 1: empty/garbage → incorrect."""
        assert qp.evaluate_correctness(1, "") is False
        assert qp.evaluate_correctness(1, "banana") is False

    def test_correct_probe2(self):
        """Probe 2: response with 'def add' AND 'return' → correct."""
        text = "def add(a, b):\n    return a + b"
        assert qp.evaluate_correctness(2, text) is True

    def test_wrong_probe2_missing_return(self):
        """Probe 2: has 'def add' but no 'return' → incorrect."""
        text = "def add(a, b):\n    a + b"
        assert qp.evaluate_correctness(2, text) is False

    def test_wrong_probe2_missing_def(self):
        """Probe 2: has 'return' but no 'def add' → incorrect."""
        text = "function add(a, b) return a + b"
        assert qp.evaluate_correctness(2, text) is False

    def test_correct_probe3(self):
        """Probe 3: response containing 'Paris' → correct (case-insensitive)."""
        assert qp.evaluate_correctness(3, "Paris") is True
        assert qp.evaluate_correctness(3, "paris") is True
        assert qp.evaluate_correctness(3, "The capital is Paris.") is True

    def test_wrong_probe3(self):
        """Probe 3: wrong city → incorrect."""
        assert qp.evaluate_correctness(3, "London") is False
        assert qp.evaluate_correctness(3, "paris texas") is True  # contains paris

    def test_unknown_probe_id_is_false(self):
        """An unknown probe_id never claims correctness."""
        assert qp.evaluate_correctness(99, "anything") is False


# ── Tests: never crashes ──────────────────────────────────────────────────────────


class TestNeverCrashes:
    def test_never_crashes_on_bad_endpoint(self, provider_configs, tmp_db_path, monkeypatch):
        """A provider whose call_llm raises must not crash the run.

        The probe wraps every provider in try/except and records a failed
        result instead of propagating the exception.
        """
        def boom(provider_name, config, prompt, timeout):
            raise ConnectionError("simulated network down")

        monkeypatch.setattr(qp, "call_llm", boom)
        # must not raise
        results = qp.run_all(provider_configs, db_path=tmp_db_path,
                             caller=qp.call_llm)
        assert len(results) == 6  # 2 providers x 3 probes
        for r in results:
            assert r["response_received"] is False
            assert r["correct_answer"] is False
            assert r["error_type"]  # some error recorded

    def test_never_crashes_on_timeout(self, provider_configs, tmp_db_path, monkeypatch):
        """Timeouts are caught, not raised."""
        import socket

        def slow(provider_name, config, prompt, timeout):
            raise socket.timeout("timed out")

        monkeypatch.setattr(qp, "call_llm", slow)
        results = qp.run_all(provider_configs, db_path=tmp_db_path,
                             caller=qp.call_llm)
        assert len(results) == 6
        assert all(r["response_received"] is False for r in results)
        assert all("timeout" in (r["error_type"] or "").lower() or
                   r["error_type"] for r in results)


# ── Tests: no response (timeout) ─────────────────────────────────────────────────


class TestNoResponse:
    def test_no_response_sets_response_received_false(self, provider_configs, tmp_db_path, monkeypatch):
        """A timeout/no-response records response_received=False."""
        def no_response(provider_name, config, prompt, timeout):
            # returns (response_received, text, latency_ms, error_type)
            return (False, "", 30000, "timeout")

        monkeypatch.setattr(qp, "call_llm", no_response)
        results = qp.run_all(provider_configs, db_path=tmp_db_path,
                             caller=qp.call_llm)
        for r in results:
            assert r["response_received"] is False
            assert r["correct_answer"] is False
            assert r["response_text"] == ""


# ── Tests: latency recorded ────────────────────────────────────────────────────────


class TestLatencyRecorded:
    def test_latency_recorded(self, provider_configs, tmp_db_path, monkeypatch):
        """latency_ms is captured from the caller and recorded as a positive int."""
        def fast(provider_name, config, prompt, timeout):
            return (True, "4", 42, "none")

        monkeypatch.setattr(qp, "call_llm", fast)
        results = qp.run_all(provider_configs, db_path=tmp_db_path,
                             caller=qp.call_llm)
        for r in results:
            assert isinstance(r["latency_ms"], int)
            assert r["latency_ms"] >= 0
            assert r["latency_ms"] == 42


# ── Tests: DB logging ────────────────────────────────────────────────────────────────


class TestDbLogging:
    def test_results_logged_to_telemetry_table(self, provider_configs, tmp_db_path, monkeypatch):
        """Each probe result is INSERTed into provider_telemetry with probe_id."""
        def ok(provider_name, config, prompt, timeout):
            return (True, "4", 100, "none")

        monkeypatch.setattr(qp, "call_llm", ok)
        qp.run_all(provider_configs, db_path=tmp_db_path, caller=qp.call_llm)

        conn = sqlite3.connect(tmp_db_path)
        rows = _fetch_probe_rows(conn)
        conn.close()

        assert len(rows) == 6  # 2 providers x 3 probes
        for r in rows:
            assert r["provider"] in ("alpha", "beta")
            assert r["probe_id"] in (1, 2, 3)
            assert r["response_received"] == 1
            assert r["response_text"] is not None

    def test_correct_answer_flag_persisted(self, provider_configs, tmp_db_path, monkeypatch):
        """correct_answer is stored as 1 when the response is correct."""
        def ok(provider_name, config, prompt, timeout):
            return (True, "4", 100, "none")

        monkeypatch.setattr(qp, "call_llm", ok)
        qp.run_all(provider_configs, db_path=tmp_db_path, caller=qp.call_llm)

        conn = sqlite3.connect(tmp_db_path)
        rows = _fetch_probe_rows(conn)
        conn.close()

        # Probe 1 expects "4" → correct
        p1 = [r for r in rows if r["probe_id"] == 1]
        assert all(r["correct_answer"] == 1 for r in p1)


# ── Tests: JSON output ────────────────────────────────────────────────────────────────


class TestJsonOutput:
    def test_json_output_is_valid(self, provider_configs, tmp_db_path, monkeypatch, capsys):
        """The main() JSON output parses as valid JSON and contains results."""
        def ok(provider_name, config, prompt, timeout):
            return (True, "4", 50, "none")

        monkeypatch.setattr(qp, "call_llm", ok)
        monkeypatch.setenv("ALPHA_KEY", "test")
        monkeypatch.setenv("BETA_KEY", "test")

        rc = qp.main(["--db", tmp_db_path,
                      "--providers", _inline_providers_yaml(),
                      "--json"])
        out = capsys.readouterr().out
        # find the JSON blob (it's the line(s) starting with '{' or '[')
        assert rc in (0, 1)
        # parse the JSON portion
        parsed = qp.parse_json_output(out)
        assert parsed is not None, f"no valid JSON in output:\n{out}"
        assert "results" in parsed
        assert isinstance(parsed["results"], list)
        assert len(parsed["results"]) == 6

    def test_format_json_produces_valid_json(self):
        """The format_json helper produces parseable JSON with required fields."""
        sample = [
            {"provider": "alpha", "probe_id": 1, "response_received": True,
             "response_text": "4", "correct_answer": True, "latency_ms": 10,
             "error_type": "none", "timestamp": "2026-01-01T00:00:00Z"},
        ]
        blob = qp.format_json(sample)
        parsed = json.loads(blob)
        assert parsed["results"][0]["provider"] == "alpha"
        assert parsed["results"][0]["correct_answer"] is True


# ── Tests: alerting ────────────────────────────────────────────────────────────────────


class TestAlerting:
    def test_alert_on_two_plus_failures(self, provider_configs, tmp_db_path, monkeypatch):
        """A provider failing 2+ probes → alert (exit code 1)."""
        # alpha fails everything, beta passes everything
        def selective(provider_name, config, prompt, timeout):
            if provider_name == "alpha":
                return (False, "", 100, "error")
            return (True, "4", 100, "none")

        monkeypatch.setattr(qp, "call_llm", selective)
        results = qp.run_all(provider_configs, db_path=tmp_db_path,
                             caller=qp.call_llm)
        should_alert, warnings = qp.check_alerts(results)
        assert should_alert is True
        assert any("alpha" in w for w in warnings)

    def test_no_alert_when_all_pass(self, provider_configs, tmp_db_path, monkeypatch):
        """All providers passing → no alert (exit 0)."""
        def ok(provider_name, config, prompt, timeout):
            # return the correct answer for whichever probe we're on
            if "2+2" in prompt:
                return (True, "4", 100, "none")
            if "add(a,b)" in prompt:
                return (True, "def add(a, b):\n    return a + b", 100, "none")
            if "capital of France" in prompt:
                return (True, "Paris", 100, "none")
            return (True, "ok", 100, "none")

        monkeypatch.setattr(qp, "call_llm", ok)
        results = qp.run_all(provider_configs, db_path=tmp_db_path,
                             caller=qp.call_llm)
        should_alert, _ = qp.check_alerts(results)
        assert should_alert is False

    def test_alert_on_high_latency(self, provider_configs, tmp_db_path, monkeypatch):
        """Latency > 10s triggers a latency warning."""
        def slow(provider_name, config, prompt, timeout):
            return (True, "4", 11000, "none")  # 11s > 10s threshold

        monkeypatch.setattr(qp, "call_llm", slow)
        results = qp.run_all(provider_configs, db_path=tmp_db_path,
                             caller=qp.call_llm)
        _, warnings = qp.check_alerts(results)
        assert any("latency" in w.lower() for w in warnings)

    def test_main_exits_1_on_quality_drop(self, provider_configs, tmp_db_path, monkeypatch, capsys):
        """main() returns 1 when a provider fails 2+ probes."""
        def failing(provider_name, config, prompt, timeout):
            return (False, "", 100, "error")

        monkeypatch.setattr(qp, "call_llm", failing)
        monkeypatch.setenv("ALPHA_KEY", "test")
        monkeypatch.setenv("BETA_KEY", "test")

        rc = qp.main(["--db", tmp_db_path,
                      "--providers", _inline_providers_yaml(),
                      "--json"])
        assert rc == 1
        err = capsys.readouterr().err
        # warning printed to stderr
        assert err.strip() != ""


# ── Tests: dry-run mode ────────────────────────────────────────────────────────────────


class TestDryRun:
    def test_dry_run_no_network_calls(self, provider_configs, tmp_db_path, monkeypatch):
        """--dry-run uses canned responses and never calls the network."""
        called = {"n": 0}

        def spy(provider_name, config, prompt, timeout):
            called["n"] += 1
            return (True, "4", 10, "none")

        # In dry-run, run_all should NOT invoke the real caller.
        results = qp.run_all(provider_configs, db_path=tmp_db_path,
                             caller=spy, dry_run=True)
        assert called["n"] == 0
        assert len(results) == 6
        # canned responses are sane
        assert all(isinstance(r, dict) for r in results)

    def test_dry_run_main(self, tmp_db_path, monkeypatch, capsys):
        """main --dry-run produces output without needing keys/network."""
        monkeypatch.delenv("ALPHA_KEY", raising=False)
        rc = qp.main(["--db", tmp_db_path,
                      "--providers", _inline_providers_yaml(),
                      "--dry-run", "--json"])
        assert rc in (0, 1)
        out = capsys.readouterr().out
        parsed = qp.parse_json_output(out)
        assert parsed is not None
        assert len(parsed["results"]) == 6


# ── Tests: config loading ──────────────────────────────────────────────────────────────


class TestLoadProviders:
    def test_load_providers_from_yaml(self, tmp_path):
        """load_providers parses a providers.yaml file into a provider map."""
        cfg = tmp_path / "providers.yaml"
        cfg.write_text(
            "zai:\n"
            "  upstream: \"https://api.z.ai/api/coding/paas/v4\"\n"
            "  keys:\n"
            "    ours:\n"
            "      key_env: \"ZAI_OUR_KEY\"\n"
            "ollama_cloud:\n"
            "  base_url: \"https://api.ollama.cloud/v1\"\n"
            "  key_env: \"OLLAMA_CLOUD_KEY\"\n"
            "external:\n"
            "  ppq:\n"
            "    base_url: \"https://api.ppq.ai/v1\"\n"
            "    key_env: \"PPQ_API_KEY\"\n"
        )
        providers = qp.load_providers(str(cfg))
        assert "zai_ours" in providers
        assert providers["zai_ours"]["endpoint"] == "https://api.z.ai/api/coding/paas/v4"
        assert "ollama_cloud" in providers
        assert "ppq" in providers

    def test_load_providers_missing_file_falls_back(self):
        """A missing config file falls back to built-in defaults (never crashes)."""
        providers = qp.load_providers("/nonexistent/path/providers.yaml")
        assert isinstance(providers, dict)
        assert len(providers) >= 1  # defaults present

    def test_load_providers_has_required_keys(self):
        """Built-in defaults each have endpoint + key_env."""
        providers = qp.load_providers(None)
        for name, cfg in providers.items():
            assert "endpoint" in cfg, f"{name} missing endpoint"
            assert "key_env" in cfg, f"{name} missing key_env"


# ── Helpers ────────────────────────────────────────────────────────────────────────────


def _inline_providers_yaml() -> str:
    """Write the test provider_configs to a temp yaml file, return its path."""
    fd, path = tempfile.mkstemp(suffix=".yaml", prefix="qprobe_cfg_")
    os.close(fd)
    with open(path, "w") as f:
        f.write(
            "zai:\n"
            "  upstream: \"https://alpha.example/v1\"\n"
            "  keys:\n"
            "    ours:\n"
            "      key_env: \"ALPHA_KEY\"\n"
            "ollama_cloud:\n"
            "  base_url: \"https://beta.example/v1\"\n"
            "  key_env: \"BETA_KEY\"\n"
        )
    # NOTE: tempfile is cleaned by the OS on reboot; fine for a test.
    return path


# ── Tests: call_llm (mocked network) ──────────────────────────────────────────────


class TestCallLlm:
    """Exercise call_llm's branches without real network (mock urllib)."""

    def test_success(self, monkeypatch):
        """A 200 response with valid JSON → (True, text, latency, 'none')."""
        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({
                    "choices": [{"message": {"content": "4"}}]
                }).encode()

        monkeypatch.setattr(qp.urllib.request, "urlopen", lambda *a, **k: FakeResp())
        monkeypatch.setenv("TESTKEY", "secret")
        cfg = {"endpoint": "https://x.example/v1/chat/completions",
               "key_env": "TESTKEY", "model": "m"}
        received, text, latency, err = qp.call_llm("prov", cfg, "hi", 5)
        assert received is True
        assert text == "4"
        assert err == "none"
        assert latency >= 0

    def test_no_api_key(self, monkeypatch):
        """Missing API key → (False, '', 0, 'no_api_key')."""
        monkeypatch.delenv("NOPE_KEY", raising=False)
        cfg = {"endpoint": "https://x.example/v1/chat/completions",
               "key_env": "NOPE_KEY", "model": "m"}
        received, text, latency, err = qp.call_llm("prov", cfg, "hi", 5)
        assert received is False
        assert err == "no_api_key"
        assert latency == 0

    def test_no_endpoint(self, monkeypatch):
        """Missing endpoint → (False, '', 0, 'no_endpoint')."""
        monkeypatch.setenv("TESTKEY", "secret")
        cfg = {"endpoint": "", "key_env": "TESTKEY", "model": "m"}
        received, text, latency, err = qp.call_llm("prov", cfg, "hi", 5)
        assert received is False
        assert err == "no_endpoint"

    def test_http_error(self, monkeypatch):
        """HTTP 500 → (False, '', latency, 'http_500')."""
        def raise_500(*a, **k):
            raise qp.urllib.error.HTTPError(
                "url", 500, "Server Error", {}, None)

        monkeypatch.setattr(qp.urllib.request, "urlopen", raise_500)
        monkeypatch.setenv("TESTKEY", "secret")
        cfg = {"endpoint": "https://x.example/v1/chat/completions",
               "key_env": "TESTKEY", "model": "m"}
        received, text, latency, err = qp.call_llm("prov", cfg, "hi", 5)
        assert received is False
        assert err == "http_500"

    def test_url_error_timeout(self, monkeypatch):
        """URLError with timeout reason → 'timeout'."""
        def raise_timeout(*a, **k):
            raise qp.urllib.error.URLError(socket.timeout("timed out"))

        monkeypatch.setattr(qp.urllib.request, "urlopen", raise_timeout)
        monkeypatch.setenv("TESTKEY", "secret")
        cfg = {"endpoint": "https://x.example/v1/chat/completions",
               "key_env": "TESTKEY", "model": "m"}
        received, text, latency, err = qp.call_llm("prov", cfg, "hi", 5)
        assert received is False
        assert err == "timeout"

    def test_url_error_generic(self, monkeypatch):
        """URLError with a non-timeout reason → 'url_error'."""
        def raise_url(*a, **k):
            raise qp.urllib.error.URLError("connection refused")

        monkeypatch.setattr(qp.urllib.request, "urlopen", raise_url)
        monkeypatch.setenv("TESTKEY", "secret")
        cfg = {"endpoint": "https://x.example/v1/chat/completions",
               "key_env": "TESTKEY", "model": "m"}
        received, text, latency, err = qp.call_llm("prov", cfg, "hi", 5)
        assert received is False
        assert err == "url_error"

    def test_socket_timeout_direct(self, monkeypatch):
        """A bare socket.timeout → 'timeout'."""
        def raise_sock(*a, **k):
            raise socket.timeout("timed out")

        monkeypatch.setattr(qp.urllib.request, "urlopen", raise_sock)
        monkeypatch.setenv("TESTKEY", "secret")
        cfg = {"endpoint": "https://x.example/v1/chat/completions",
               "key_env": "TESTKEY", "model": "m"}
        received, text, latency, err = qp.call_llm("prov", cfg, "hi", 5)
        assert received is False
        assert err == "timeout"

    def test_unexpected_exception(self, monkeypatch):
        """Any other exception → caught, error:type recorded."""
        def raise_weird(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(qp.urllib.request, "urlopen", raise_weird)
        monkeypatch.setenv("TESTKEY", "secret")
        cfg = {"endpoint": "https://x.example/v1/chat/completions",
               "key_env": "TESTKEY", "model": "m"}
        received, text, latency, err = qp.call_llm("prov", cfg, "hi", 5)
        assert received is False
        assert err.startswith("error:")


# ── Tests: _extract_text variants ─────────────────────────────────────────────────


class TestExtractText:
    def test_openai_format(self):
        raw = json.dumps({"choices": [{"message": {"content": "hello"}}]})
        assert qp._extract_text(raw) == "hello"

    def test_zai_format(self):
        raw = json.dumps({"data": {"content": "world"}})
        assert qp._extract_text(raw) == "world"

    def test_invalid_json_falls_back_to_raw(self):
        assert qp._extract_text("not json at all").startswith("not json")

    def test_empty_choices_falls_back(self):
        raw = json.dumps({"choices": []})
        assert qp._extract_text(raw) == raw.strip()


# ── Tests: output helpers ─────────────────────────────────────────────────────────


class TestOutputHelpers:
    def test_print_human(self, capsys):
        results = [
            {"provider": "alpha", "probe_id": 1, "response_received": True,
             "response_text": "4", "correct_answer": True, "latency_ms": 10,
             "error_type": "none", "timestamp": "2026-01-01T00:00:00+00:00"},
            {"provider": "alpha", "probe_id": 2, "response_received": False,
             "response_text": "", "correct_answer": False, "latency_ms": 0,
             "error_type": "timeout", "timestamp": "2026-01-01T00:00:00+00:00"},
        ]
        qp._print_human(results)
        out = capsys.readouterr().out
        assert "Quality Probe Results" in out
        assert "alpha" in out
        assert "probe 1" in out
        assert "probe 2" in out

    def test_parse_json_output_plain(self):
        blob = qp.format_json([{"provider": "x", "probe_id": 1,
                                "response_received": True, "response_text": "4",
                                "correct_answer": True, "latency_ms": 1,
                                "error_type": "none", "timestamp": "t"}])
        parsed = qp.parse_json_output(blob)
        assert parsed["results"][0]["provider"] == "x"

    def test_parse_json_output_with_noise(self):
        blob = "cron started\n" + qp.format_json([]) + "\ncron done\n"
        parsed = qp.parse_json_output(blob)
        assert parsed is not None
        assert parsed["results"] == []

    def test_parse_json_output_no_json(self):
        assert qp.parse_json_output("just plain text") is None

    def test_parse_json_output_garbage(self):
        assert qp.parse_json_output("{not valid") is None


# ── Tests: main() CLI paths ───────────────────────────────────────────────────────


class TestMainCli:
    def test_main_human_readable(self, tmp_db_path, monkeypatch, capsys):
        """main() without --json prints the human table."""
        def ok(provider_name, config, prompt, timeout):
            if "2+2" in prompt:
                return (True, "4", 5, "none")
            if "add(a,b)" in prompt:
                return (True, "def add(a, b):\n    return a + b", 5, "none")
            if "capital of France" in prompt:
                return (True, "Paris", 5, "none")
            return (True, "ok", 5, "none")

        monkeypatch.setattr(qp, "call_llm", ok)
        rc = qp.main(["--db", tmp_db_path, "--providers",
                      _inline_providers_yaml(), "--dry-run"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "Quality Probe Results" in out

    def test_main_only_filter(self, tmp_db_path, monkeypatch, capsys):
        """--only restricts the probe set."""
        rc = qp.main(["--db", tmp_db_path, "--providers",
                      _inline_providers_yaml(), "--dry-run", "--json",
                      "--only", "zai_ours"])
        out = capsys.readouterr().out
        parsed = qp.parse_json_output(out)
        assert rc == 0
        providers = {r["provider"] for r in parsed["results"]}
        assert providers == {"zai_ours"}

    def test_main_no_providers(self, tmp_db_path, monkeypatch, capsys):
        """--only with an unknown name → 'No providers' + exit 1."""
        rc = qp.main(["--db", tmp_db_path, "--providers",
                      _inline_providers_yaml(), "--dry-run", "--json",
                      "--only", "does_not_exist"])
        err = capsys.readouterr().err
        assert rc == 1
        assert "No providers" in err


# ── Tests: DB schema robustness ───────────────────────────────────────────────────


class TestDbRobustness:
    def test_ensure_quality_table_idempotent(self, tmp_db_path):
        """Calling _ensure_quality_table twice is a no-op."""
        conn = sqlite3.connect(tmp_db_path)
        qp._ensure_quality_table(conn)
        qp._ensure_quality_table(conn)  # must not raise
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(provider_telemetry)").fetchall()}
        for expected in ("probe_id", "response_text", "correct_answer"):
            assert expected in cols
        conn.close()

    def test_log_probe_never_raises_on_bad_conn(self):
        """_log_probe must not raise even if the connection is broken."""

        class BadConn:
            def execute(self, *a, **k):
                raise sqlite3.OperationalError("dead db")

        result = {"timestamp": "t", "provider": "x", "probe_id": 1,
                  "response_received": True, "response_text": "4",
                  "correct_answer": True, "latency_ms": 1, "error_type": "none"}
        # must not raise
        qp._log_probe(BadConn(), result)

    def test_open_db_returns_none_on_bad_path(self):
        """A path that can't be opened returns None (never raises)."""
        # /dev/null is not a valid sqlite file path for writing tables
        assert qp._open_db("/dev/null/cannot/append") is None

    def test_preexisting_table_gets_probe_columns(self, tmp_db_path):
        """If zai_proxy created the table first, probe columns get added."""
        conn = sqlite3.connect(tmp_db_path)
        # simulate the production schema (no probe columns)
        conn.execute("""CREATE TABLE provider_telemetry (
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
        )""")
        conn.commit()
        conn.close()

        # now run through quality_probe — should ALTER ADD COLUMN
        providers = {"p": {"endpoint": "https://x/v1/chat/completions",
                           "key_env": "X", "model": "m"}}
        qp.run_all(providers, db_path=tmp_db_path,
                   caller=lambda *a: (False, "", 1, "test"))

        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(provider_telemetry)").fetchall()}
        conn.close()
        assert "probe_id" in cols
        assert "response_text" in cols
        assert "correct_answer" in cols


# ── Tests: canned caller fallback ────────────────────────────────────────────────


class TestCannedCaller:
    def test_canned_unknown_prompt(self):
        """The canned caller returns a sane default for unknown prompts."""
        received, text, latency, err = qp._canned_caller(
            "p", {}, "some unknown prompt", 5)
        assert received is True
        assert err == "none"
        assert latency >= 0


# ── Tests: config parsing edge cases ──────────────────────────────────────────────


class TestConfigParsing:
    def test_parse_yaml_malformed_returns_none(self, tmp_path):
        """Malformed YAML returns None (falls back to defaults)."""
        cfg = tmp_path / "bad.yaml"
        cfg.write_text("this: is: not: valid: yaml: [")
        # _parse_yaml swallows the error; load_providers falls back
        providers = qp.load_providers(str(cfg))
        assert len(providers) >= 1  # defaults

    def test_join_chat_idempotent(self):
        assert qp._join_chat("https://x/v1") == "https://x/v1/chat/completions"
        assert qp._join_chat("https://x/v1/chat/completions") == \
            "https://x/v1/chat/completions"
        assert qp._join_chat(None) == ""

    def test_parse_yaml_ignores_non_dict_sections(self, tmp_path):
        """Non-dict entries in external/keys are skipped gracefully."""
        cfg = tmp_path / "providers.yaml"
        cfg.write_text(
            "external:\n"
            "  ppq:\n"
            "    base_url: \"https://api.ppq.ai/v1\"\n"
            "    key_env: \"PPQ_API_KEY\"\n"
            "  garbage: \"not a dict\"\n"
        )
        providers = qp.load_providers(str(cfg))
        assert "ppq" in providers
        # 'garbage' is skipped, not crashed on
