"""OX-3a harness unit tests (plan §3, task t_7a12e29a).

Pure-logic tests only — NO live calls in CI (fixtures, sanitization, blind
shuffle, schema/verdict/outcome checkers, refusal classifier, percentiles,
staged-ramp abort semantics with mocked clients and mocked usage deltas).
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from oxalpha_eval import (  # noqa: E402
    CampaignAbort,
    SpendGate,
    blind_shuffle,
    build_burst_schedule,
    check_json_schema,
    classify_refusal,
    extract_outcome,
    extract_verdict,
    load_fixtures,
    percentile,
    run_staged_ramp,
    sanitize_scan,
    strip_model_labels,
    validate_fixture_set,
)
from promo_tier import PromoTierGuard  # noqa: E402

FIXDIR = os.path.join(os.path.dirname(__file__), "..", "eval", "ox3a", "fixtures")

# ── fixture loading ──────────────────────────────────────────────────────────


class TestFixtureLoading:
    def test_loads_expected_counts_and_shapes(self):
        fx = load_fixtures(FIXDIR)
        assert len(fx["primary"]) == 60
        assert len(fx["refusal_probes"]) == 10
        assert len(fx["latency_micro"]) == 10
        shapes = {}
        for it in fx["primary"]:
            shapes[it["shape"]] = shapes.get(it["shape"], 0) + 1
        assert shapes == {"code_review": 15, "build_summary": 15,
                          "doc_writing": 15, "json_extract": 15}

    def test_fixture_set_validates(self):
        fx = load_fixtures(FIXDIR)
        assert validate_fixture_set(fx) == []

    def test_validation_catches_bad_counts(self):
        fx = load_fixtures(FIXDIR)
        bad = {"primary": fx["primary"][:-1], "refusal_probes": fx["refusal_probes"],
               "latency_micro": fx["latency_micro"]}
        errs = validate_fixture_set(bad)
        assert errs and any("59" in e or "primary" in e for e in errs)

    def test_validation_catches_duplicate_ids(self):
        fx = load_fixtures(FIXDIR)
        dup = json.loads(json.dumps(fx["primary"]))
        dup[1]["id"] = dup[0]["id"]
        errs = validate_fixture_set({"primary": dup, "refusal_probes": fx["refusal_probes"],
                                     "latency_micro": fx["latency_micro"]})
        assert any("duplicate" in e for e in errs)

    def test_deterministic_shapes_have_ground_truth(self):
        fx = load_fixtures(FIXDIR)
        for it in fx["primary"]:
            if it["deterministic"] in ("verdict", "outcome"):
                assert it["ground_truth"] in (
                    {"approve", "request-changes", "block"} if it["deterministic"] == "verdict"
                    else {"failure", "no-failure"})
            if it["deterministic"] == "json_schema":
                assert isinstance(it["schema"], dict) and it["schema"]

    def test_latency_micro_prompts_sized(self):
        fx = load_fixtures(FIXDIR)
        for it in fx["latency_micro"]:
            toks = len(it["prompt"]) // 4  # rough token estimate
            assert 700 <= toks <= 3400, f"{it['id']} ~{toks} tokens"

    def test_json_extract_prompts_embed_source_text(self):
        # regression (found while grading the 2026-08-22 campaign): the
        # builder dropped je["text"] at prompt composition, shipping 15
        # json_extract prompts whose --- TEXT --- block was empty.
        # json_validity then measured behavior on missing input, not
        # extraction quality. Campaign artifacts from that build remain
        # valid evidence of the abort; fixtures are fixed for future runs.
        fx = load_fixtures(FIXDIR)
        marker = "--- TEXT ---\n"
        n = 0
        for it in fx["primary"]:
            if it["shape"] != "json_extract":
                continue
            n += 1
            assert marker in it["prompt"], f"{it['id']}: marker missing"
            tail = it["prompt"].split(marker, 1)[1]
            assert len(tail.strip()) >= 20, (
                f"{it['id']}: source text after marker is empty "
                f"({len(tail.strip())} chars)")
        assert n == 15


# ── sanitization (v1 §2.5) ───────────────────────────────────────────────────


class TestSanitize:
    def test_committed_fixtures_are_clean(self):
        fx = load_fixtures(FIXDIR)
        everything = fx["primary"] + fx["refusal_probes"] + fx["latency_micro"]
        assert sanitize_scan(everything) == []

    def test_flags_openrouter_style_key(self):
        items = [{"id": "x", "prompt": "config: " + "sk-or-v1-" + "a1b2c3d4e5f6g7h8i9j0"}]
        v = sanitize_scan(items)
        assert v and "sk-" in v[0]

    def test_flags_generic_secret_assignment(self):
        items = [{"id": "x", "prompt": "password: hunter2supersecret"}]
        assert sanitize_scan(items) != []

    def test_flags_long_hex_blob(self):
        items = [{"id": "x", "prompt": "digest " + "ab" * 24}]
        assert sanitize_scan(items) != []

    def test_flags_email(self):
        items = [{"id": "x", "prompt": "contact ops.person@corp.example"}]
        assert sanitize_scan(items) != []

    def test_clean_text_passes(self):
        items = [{"id": "x", "prompt": "pytest: 73 passed in 12.41s — gate green"}]
        assert sanitize_scan(items) == []


# ── blind shuffle ────────────────────────────────────────────────────────────


class TestBlindShuffle:
    def test_deterministic_for_seed(self):
        assert blind_shuffle(20, seed=20260822) == blind_shuffle(20, seed=20260822)

    def test_is_permutation(self):
        assert sorted(blind_shuffle(30, seed=1)) == list(range(30))

    def test_label_free_payload_has_no_model_names(self):
        arms = [
            {"arm": "ox_low", "model": "stealth/ox-alpha", "content": "answer A",
             "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
            {"arm": "base", "model": "glm-5.3", "content": "answer B",
             "usage": {"prompt_tokens": 11, "completion_tokens": 6}},
        ]
        payload = json.dumps(strip_model_labels(arms))
        assert "ox-alpha" not in payload and "glm" not in payload and "arm" not in payload
        assert "answer A" in payload  # content preserved


# ── deterministic checkers ───────────────────────────────────────────────────


class TestJsonSchemaChecker:
    SCHEMA = {"invoice_id": "str", "total": "number", "lines": "array", "ok": "bool"}

    def test_valid(self):
        ok, why = check_json_schema(
            '{"invoice_id": "INV-1", "total": 12.5, "lines": ["a"], "ok": true}', self.SCHEMA)
        assert ok, why

    def test_fenced_json_accepted(self):
        ok, why = check_json_schema(
            "```json\n{\"invoice_id\": \"I\", \"total\": 1, \"lines\": [], \"ok\": false}\n```",
            self.SCHEMA)
        assert ok, why

    def test_missing_required_key(self):
        ok, _ = check_json_schema('{"total": 1, "lines": [], "ok": true}', self.SCHEMA)
        assert not ok

    def test_wrong_type(self):
        ok, _ = check_json_schema(
            '{"invoice_id": "I", "total": "12.5", "lines": [], "ok": true}', self.SCHEMA)
        assert not ok

    def test_invalid_json(self):
        ok, why = check_json_schema("{not json", self.SCHEMA)
        assert not ok and "parse" in why.lower()

    def test_non_object(self):
        ok, _ = check_json_schema("[1,2,3]", self.SCHEMA)
        assert not ok

    def test_extra_keys_tolerated(self):
        ok, _ = check_json_schema(
            '{"invoice_id": "I", "total": 1, "lines": [], "ok": true, "extra": 9}', self.SCHEMA)
        assert ok


class TestExtractVerdict:
    def test_canonical_format(self):
        assert extract_verdict("VERDICT: approve\nREASONS:\n- a\n- b\n- c") == "approve"

    def test_request_changes_variant(self):
        assert extract_verdict("VERDICT: request-changes\n...") == "request-changes"

    def test_inline_noise_tolerated(self):
        assert extract_verdict(
            "Looking at this, I say:\nVERDICT: block\nREASONS:\n- x") == "block"

    def test_missing_returns_none(self):
        assert extract_verdict("LGTM, merging now.") is None

    def test_invalid_value_returns_none(self):
        assert extract_verdict("VERDICT: ship-it") is None


class TestExtractOutcome:
    def test_both_values(self):
        assert extract_outcome("...\nOUTCOME: failure") == "failure"
        assert extract_outcome("OUTCOME: no-failure") == "no-failure"

    def test_none_when_absent(self):
        assert extract_outcome("5 bullets, all good") is None


class TestRefusalClassifier:
    def test_explicit_refusal(self):
        assert classify_refusal("I can't help with that request.") is True

    def test_policy_refusal(self):
        assert classify_refusal(
            "I'm sorry, but I can't assist with summarizing this content.") is True

    def test_empty_content(self):
        assert classify_refusal("") is True
        assert classify_refusal(None) is True

    def test_normal_content(self):
        assert classify_refusal("VERDICT: approve\nREASONS:\n- a\n- b\n- c") is False
        assert classify_refusal("The build failed at step 4.") is False

    def test_word_containing_cant_is_not_refusal(self):
        # "can't" inside a bullet about the code, not addressed to the user
        assert classify_refusal(
            "- The retry loop can't be bounded as written; needs a cap.") is False


class TestPercentile:
    def test_p50_p95_known(self):
        xs = list(range(1, 101))  # 1..100
        assert percentile(xs, 50) == pytest.approx(50.5)
        assert percentile(xs, 95) == pytest.approx(95.05)

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            percentile([], 50)


# ── burst schedule ───────────────────────────────────────────────────────────


class TestBurstSchedule:
    def test_shape(self):
        offs = build_burst_schedule(rate_rps=10, seconds=30)
        assert len(offs) == 300
        assert offs == sorted(offs)
        assert offs[0] == pytest.approx(0.0)
        assert offs[-1] < 30.0

    def test_spacing(self):
        offs = build_burst_schedule(rate_rps=10, seconds=30)
        gaps = [b - a for a, b in zip(offs, offs[1:])]
        assert min(gaps) == pytest.approx(0.1, abs=1e-6)


# ── staged ramp + spend gate ─────────────────────────────────────────────────


def _tiny_fixtures():
    """3 primary (one per deterministic shape), 1 probe, 1 latency item."""
    def item(i, shape, det, gt):
        return {"id": f"t-{i}", "shape": shape, "prompt": f"prompt {i}",
                "deterministic": det, "ground_truth": gt,
                "schema": {"a": "str"} if det == "json_schema" else None,
                "provenance": "test"}
    return {
        "primary": [
            item(1, "code_review", "verdict", "approve"),
            item(2, "build_summary", "outcome", "failure"),
            item(3, "json_extract", "json_schema", None),
        ],
        "refusal_probes": [item(9, "refusal_probe", None, None)],
        "latency_micro": [item(10, "latency_digest", None, None)],
    }


class FakeClient:
    """Records calls; usage stays flat unless a delta schedule is set."""

    def __init__(self, usage_deltas=None, pricing_zero=True):
        self.calls = {"models": 0, "key": 0, "ox_chat": 0, "base_chat": 0}
        self.usage_deltas = list(usage_deltas or [])  # popped per key_info call
        self.pricing_zero = pricing_zero
        self._usage = 0.0

    def models_info(self):
        self.calls["models"] += 1
        price = {"prompt": "0", "completion": "0"} if self.pricing_zero else \
            {"prompt": "10", "completion": "30"}
        return {"data": [{"id": "stealth/ox-alpha", "pricing": price}]}

    def key_info(self):
        self.calls["key"] += 1
        if self.usage_deltas:
            self._usage += self.usage_deltas.pop(0)
        return {"usage": self._usage, "limit": 0}

    def ox_chat(self, messages, effort="low", max_tokens=8192, model=None):
        self.calls["ox_chat"] += 1
        return {"ok": True, "status": 200, "latency_s": 0.5, "content": "OK",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "finish_reason": "stop", "headers": {}, "error": None}

    def base_chat(self, messages, max_tokens=8192, model="glm-5.3"):
        self.calls["base_chat"] += 1
        return {"ok": True, "status": 200, "latency_s": 0.4, "content": "OK",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "finish_reason": "stop", "headers": {}, "error": None}


class TestSpendGate:
    def test_flat_usage_passes(self):
        gate = SpendGate()
        gate.snapshot(0.0)
        assert gate.check(0.0) is None

    def test_positive_delta_aborts_with_anomaly_row(self):
        guard = PromoTierGuard.from_config({"expires_at": "2026-08-28T00:00:00Z"})
        gate = SpendGate(guard=guard)
        gate.snapshot(0.0)
        abort = gate.check(0.0001)
        assert abort is not None
        assert abort["category"] == "promo_spend"
        assert abort["severity"] == "critical"
        d = json.loads(abort["detail"])
        assert d["cost_usd"] == pytest.approx(0.0001)
        assert guard.disabled_reason is not None  # tier killed

    def test_fires_once(self):
        gate = SpendGate(guard=PromoTierGuard.from_config({}))
        gate.snapshot(0.0)
        first = gate.check(0.01)
        second = gate.check(0.01)
        assert first is not None and second is None

    def test_negative_usage_is_not_spend(self):
        gate = SpendGate()
        gate.snapshot(1.0)
        assert gate.check(0.5) is None  # usage went DOWN — not our abort case


class TestStagedRamp:
    CFG = {
        "canary_calls": 2, "burst_seconds": 2, "burst_rate_rps": 5,
        "burst_concurrency": 4, "effort": "low", "max_completion_tokens": 8192,
    }

    def test_zero_delta_completes_all_stages(self):
        client = FakeClient()
        res = run_staged_ramp(client, _tiny_fixtures(), self.CFG)
        assert res["aborted"] is False
        assert res["stages"] == ["verify_pricing", "canary", "full_set", "burst_probe"]
        # canary + full set paired runs + A/B reruns for deterministic items
        assert client.calls["ox_chat"] > 0 and client.calls["base_chat"] > 0
        assert res["usage_evidence"]["deltas"] == []

    def test_delta_after_canary_aborts_before_full_set(self):
        client = FakeClient(usage_deltas=[0.0, 0.42])  # 2nd key check > 0
        res = run_staged_ramp(client, _tiny_fixtures(), self.CFG)
        assert res["aborted"] is True
        assert res["stages"] == ["verify_pricing", "canary"]
        assert client.calls["base_chat"] == 0  # full set never started
        assert res["anomaly_row"]["category"] == "promo_spend"

    def test_nonzero_pricing_aborts_before_any_chat(self):
        client = FakeClient(pricing_zero=False)
        res = run_staged_ramp(client, _tiny_fixtures(), self.CFG)
        assert res["aborted"] is True
        assert client.calls["ox_chat"] == 0
        assert "pricing" in res["abort_reason"]

    def test_delta_after_full_set_aborts_before_burst(self):
        client = FakeClient(usage_deltas=[0.0, 0.0, 0.05])
        res = run_staged_ramp(client, _tiny_fixtures(), self.CFG)
        assert res["aborted"] is True
        assert res["stages"] == ["verify_pricing", "canary", "full_set"]
        assert "burst" not in res["stages"]

    def test_campaign_abort_exception_carries_row(self):
        client = FakeClient(usage_deltas=[0.0, 1.0])
        try:
            run_staged_ramp(client, _tiny_fixtures(), self.CFG, raise_on_abort=True)
            assert False, "should raise"
        except CampaignAbort as e:
            assert e.anomaly_row["category"] == "promo_spend"

    def test_burst_records_429s_and_headers(self):
        class BurstClient(FakeClient):
            def ox_chat(self, messages, effort="low", max_tokens=8192, model=None):
                r = super().ox_chat(messages, effort, max_tokens, model)
                n = self.calls["ox_chat"]
                if n > 6:  # later calls get throttled
                    r.update(ok=False, status=429, content=None,
                             headers={"x-ratelimit-remaining-requests": "0",
                                      "retry-after": "12"})
                return r

        client = BurstClient()
        res = run_staged_ramp(client, _tiny_fixtures(), self.CFG)
        assert res["aborted"] is False
        burst = res["burst"]
        assert burst["total"] == self.CFG["burst_rate_rps"] * self.CFG["burst_seconds"]
        assert burst["n_429"] > 0
        assert any("x-ratelimit" in h or "retry-after" in h
                   for h in burst["header_names"])

    def test_p95_under_load_recorded(self):
        client = FakeClient()
        res = run_staged_ramp(client, _tiny_fixtures(), self.CFG)
        assert res["burst"]["p95_latency_s"] >= 0


# ── pass-criteria evaluation (report side, pure) ─────────────────────────────

from ox_eval_report import evaluate_pass_criteria  # noqa: E402


def _rubric_item(mean):
    return {"rubric_mean": mean, "within_1_of_base": True}


class TestPassCriteria:
    BASE_CFG = {}  # thresholds are module defaults from plan §3.4

    def _mk(self, **over):
        # Minimal well-formed results blob covering all 6 criteria.
        base = {
            "noninferiority": {  # per shape: ox mean, base mean, within% 
                "code_review": {"ox_mean": 4.2, "base_mean": 4.3, "within_1_pct": 1.0},
                "build_summary": {"ox_mean": 4.4, "base_mean": 4.2, "within_1_pct": 1.0},
                "doc_writing": {"ox_mean": 4.0, "base_mean": 4.1, "within_1_pct": 0.95},
                "json_extract": {"ox_mean": 4.3, "base_mean": 4.35, "within_1_pct": 1.0},
            },
            "json_validity": 1.0,
            "verdict_acc": {"ox": 0.9, "base": 0.9},
            "outcome_acc": {"ox": 1.0},
            "refusals": {"primary": 0, "probes": 1},
            "latency": {"p50": 8.0, "p95": 30.0},
            "effort_ab": {"low_mean": 4.1, "max_mean": 4.35},
            "usage_deltas": [0.0, 0.0, 0.0, 0.0],
        }
        base.update(over)
        return base

    def test_all_pass(self):
        verdict = evaluate_pass_criteria(self._mk())
        assert all(c["pass"] for k, c in verdict.items() if k != "_overall")
        assert verdict["_overall"] == "PASS"

    def test_quality_miss_fails(self):
        bad = self._mk()
        bad["noninferiority"]["code_review"]["ox_mean"] = 3.5  # gap 0.8 > 0.3
        v = evaluate_pass_criteria(bad)
        assert v["1_quality_noninferiority"]["pass"] is False
        assert v["_overall"] == "FAIL"

    def test_within1_floor(self):
        bad = self._mk()
        bad["noninferiority"]["doc_writing"]["within_1_pct"] = 0.89  # < 0.90
        assert evaluate_pass_criteria(bad)["1_quality_noninferiority"]["pass"] is False

    def test_json_floor_95(self):
        assert evaluate_pass_criteria(self._mk(json_validity=0.95))[
            "2_deterministic_floor"]["pass"] is True
        assert evaluate_pass_criteria(self._mk(json_validity=0.94))[
            "2_deterministic_floor"]["pass"] is False

    def test_verdict_relative_floor(self):
        # ox verdict accuracy must be >= 0.85 * base accuracy
        bad = self._mk(verdict_acc={"ox": 0.7, "base": 0.9})
        assert evaluate_pass_criteria(bad)["2_deterministic_floor"]["pass"] is False
        ok = self._mk(verdict_acc={"ox": 0.765, "base": 0.9})
        assert evaluate_pass_criteria(ok)["2_deterministic_floor"]["pass"] is True

    def test_outcome_floor_90(self):
        assert evaluate_pass_criteria(self._mk(outcome_acc={"ox": 0.9}))[
            "2_deterministic_floor"]["pass"] is True
        assert evaluate_pass_criteria(self._mk(outcome_acc={"ox": 0.89}))[
            "2_deterministic_floor"]["pass"] is False

    def test_refusal_rules(self):
        # 0 primary refusals required; probes ≤ 1/10 is a flag not a fail
        assert evaluate_pass_criteria(self._mk(refusals={"primary": 0, "probes": 1}))[
            "3_refusals"]["pass"] is True
        v = evaluate_pass_criteria(self._mk(refusals={"primary": 1, "probes": 0}))
        assert v["3_refusals"]["pass"] is False
        v = evaluate_pass_criteria(self._mk(refusals={"primary": 0, "probes": 2}))
        assert v["3_refusals"]["pass"] is True  # flagged, not failed
        assert v["3_refusals"]["flagged"]

    def test_latency_caps(self):
        ok = evaluate_pass_criteria(self._mk(latency={"p50": 25.0, "p95": 60.0}))
        assert ok["4_latency"]["pass"] is True
        bad = evaluate_pass_criteria(self._mk(latency={"p50": 25.1, "p95": 59.0}))
        assert bad["4_latency"]["pass"] is False
        bad = evaluate_pass_criteria(self._mk(latency={"p50": 20.0, "p95": 60.1}))
        assert bad["4_latency"]["pass"] is False

    def test_effort_ab_gap(self):
        # low must not lose > 0.5 to max
        ok = evaluate_pass_criteria(self._mk(effort_ab={"low_mean": 4.0, "max_mean": 4.5}))
        assert ok["5_effort_ab"]["pass"] is True
        bad = evaluate_pass_criteria(self._mk(effort_ab={"low_mean": 4.0, "max_mean": 4.51}))
        assert bad["5_effort_ab"]["pass"] is False

    def test_spend_must_be_exactly_zero(self):
        assert evaluate_pass_criteria(self._mk(usage_deltas=[0.0, 0.0]))[
            "6_spend"]["pass"] is True
        assert evaluate_pass_criteria(self._mk(usage_deltas=[0.0, 0.001]))[
            "6_spend"]["pass"] is False


# ── OX-3a spec conformance: probes, micro set, effort subset, burst spec ──────

from oxalpha_eval import DEFAULT_CFG, effort_ab_subset  # noqa: E402


class TestSpecConformance:
    CFG = {
        "canary_calls": 2, "burst_seconds": 2, "burst_rate_rps": 5,
        "burst_concurrency": 4, "effort": "low", "max_completion_tokens": 8192,
    }

    def test_ramp_runs_refusal_probes_and_latency_micro(self):
        client = FakeClient()
        res = run_staged_ramp(client, _tiny_fixtures(), self.CFG)
        assert "refusal_probe_records" in res and "latency_micro_records" in res
        # tiny fixtures carry 1 probe + 1 micro item
        assert len(res["refusal_probe_records"]) == 1
        assert len(res["latency_micro_records"]) == 1
        probe = res["refusal_probe_records"][0]
        assert probe["id"] == "t-9" and probe["content"]
        micro = res["latency_micro_records"][0]
        assert micro["id"] == "t-10" and micro["latency_s"] >= 0

    def test_probes_run_ox_only(self):
        client = FakeClient()
        run_staged_ramp(client, _tiny_fixtures(), self.CFG)
        # 1 probe + 1 micro ran on ox; base_chat only from paired primary items
        assert client.calls["ox_chat"] >= 2

    def test_default_cfg_matches_plan(self):
        # task t_7a12e29a: 5-call canary; burst 10 rps x 30s, concurrency 10
        assert DEFAULT_CFG["canary_calls"] == 5
        assert DEFAULT_CFG["burst_rate_rps"] == 10
        assert DEFAULT_CFG["burst_seconds"] == 30
        assert DEFAULT_CFG["burst_concurrency"] == 10
        assert DEFAULT_CFG["burst_max_tokens"] == 16

    def test_effort_ab_subset_is_20_with_shape_coverage(self):
        fx = load_fixtures(FIXDIR)
        subset = effort_ab_subset(fx)
        assert len(subset) == 20
        shapes = {}
        for it in subset:
            shapes[it["shape"]] = shapes.get(it["shape"], 0) + 1
        assert shapes == {"code_review": 5, "build_summary": 5,
                          "doc_writing": 5, "json_extract": 5}
        # deterministic: same fixtures -> same subset
        assert [i["id"] for i in subset] == [i["id"] for i in effort_ab_subset(fx)]

    def test_ramp_effort_max_runs_only_subset(self):
        client = FakeClient()
        res = run_staged_ramp(client, _tiny_fixtures(), self.CFG)
        # tiny fixtures: 3 primary, subset selection caps at min(20, primary)
        assert len(res["effort_max_records"]) <= 3
        assert all(r["shape"] for r in res["effort_max_records"])

    def test_burst_uses_spec_max_tokens_and_records_header_values(self):
        seen_tokens = []

        class TokClient(FakeClient):
            def ox_chat(self, messages, effort="low", max_tokens=8192, model=None):
                seen_tokens.append(max_tokens)
                r = super().ox_chat(messages, effort, max_tokens, model)
                if len(seen_tokens) > 3:
                    r.update(ok=False, status=429, content=None,
                             headers={"x-ratelimit-remaining-requests": "0",
                                      "x-ratelimit-reset-timestamp": "1771900000",
                                      "retry-after": "12"})
                return r

        client = TokClient()
        res = run_staged_ramp(client, _tiny_fixtures(), self.CFG)
        # burst probes use the spec max_tokens (16), not 512/8192
        assert seen_tokens[-1] == 16
        burst = res["burst"]
        assert burst["n_429"] > 0
        rl = burst["rate_limit_headers"]
        assert any("x-ratelimit" in h for h in rl)
        assert rl.get("x-ratelimit-remaining-requests") or \
            any(v for k, v in rl.items() if "remaining" in k)


# ── per-shape latency + micro-set criterion (report side) ─────────────────────

from ox_eval_report import mechanical_metrics  # noqa: E402


class TestMechanicalLatencyBreakdown:
    def _campaign(self):
        return {
            "paired_records": [
                {"id": "cr-001", "shape": "code_review",
                 "ox": {"content": "x", "latency_s": 10.0},
                 "base": {"content": "y", "latency_s": 9.0}},
                {"id": "cr-002", "shape": "code_review",
                 "ox": {"content": "x", "latency_s": 20.0},
                 "base": {"content": "y", "latency_s": 9.0}},
                {"id": "dw-001", "shape": "doc_writing",
                 "ox": {"content": "x", "latency_s": 40.0},
                 "base": {"content": "y", "latency_s": 9.0}},
            ],
            "latency_micro_records": [
                {"id": "lm-001", "latency_s": 5.0},
                {"id": "lm-002", "latency_s": 7.0},
            ],
            "refusal_probe_records": [],
        }

    def test_latency_has_micro_and_per_shape(self):
        m = mechanical_metrics(self._campaign(), load_fixtures(FIXDIR))
        lat = m["latency"]
        assert lat["micro"]["n"] == 2
        assert lat["micro"]["p50"] == pytest.approx(6.0)
        assert lat["per_shape"]["code_review"]["n"] == 2
        assert lat["per_shape"]["doc_writing"]["n"] == 1
        assert lat["per_shape"]["doc_writing"]["p50"] == pytest.approx(40.0)
        # aggregate still exists (full set + micro)
        assert lat["n"] == 5

    def test_criterion4_uses_micro_when_present(self):
        m = mechanical_metrics(self._campaign(), load_fixtures(FIXDIR))
        # micro p95=6.8 <= 60 -> criterion satisfied via micro set
        assert m["latency"]["micro"]["p95"] <= 60.0
