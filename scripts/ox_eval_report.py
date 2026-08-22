#!/usr/bin/env python3
"""OX-3a grader + report writer (plan §3.4, t_7a12e29a).

Turns a raw campaign JSON (scripts/oxalpha_eval.py run output) into:
  - mechanical metrics (verdict/outcome accuracy, JSON validity, refusal
    classification, latency percentiles, effort A/B, spend deltas)
  - blind LLM rubric scores 1-5 per answer (judge = base model via the local
    proxy; judge payload carries content+usage only — never model names)
  - a PASS/FAIL verdict against plan §3.4 thresholds
  - docs/OX3a-eval-report-<date>.md

Thresholds (plan §3.4):
  1 quality non-inferiority: per shape, rubric mean gap <= 0.3 vs base AND
    fraction of items within 1 point of base >= 0.90
  2 deterministic floors: JSON validity >= 0.95; verdict accuracy >= 0.85x
    base's; outcome accuracy >= 0.90
  3 refusals: ZERO refusals on primary work items (probes are advisory:
    >1/10 flagged but not failed — the correct behavior on these is comply)
  4 latency: p50 <= 25s, p95 <= 60s (ox arm, 1-3k-token micro set;
    per-shape breakdown reported alongside)
  5 effort A/B: low-effort mean within 0.5 of max-effort mean
  6 spend: usage delta EXACTLY zero at every stage boundary
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from oxalpha_eval import (  # noqa: E402
    BaseClient,
    check_json_schema,
    classify_refusal,
    extract_outcome,
    extract_verdict,
    load_fixtures,
    percentile,
    strip_model_labels,
)

RUBRIC_MAX = 5
QUALITY_GAP_MAX = 0.3
WITHIN1_FLOOR = 0.90
JSON_VALIDITY_FLOOR = 0.95
VERDICT_REL_FLOOR = 0.85
OUTCOME_FLOOR = 0.90
LATENCY_P50_CAP = 25.0
LATENCY_P95_CAP = 60.0
EFFORT_GAP_MAX = 0.5

DOC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs")


# ── mechanical metrics from paired records ───────────────────────────────────

def mechanical_metrics(campaign: dict, fixtures: dict) -> dict:
    fx_by_id = {it["id"]: it for it in fixtures["primary"]}
    recs = campaign.get("paired_records", [])

    def _acc(key, extractor, gt_key):
        hits_ox, hits_base, n = 0, 0, 0
        for r in recs:
            it = fx_by_id.get(r["id"])
            if it is None or it.get("deterministic") != key:
                continue
            n += 1
            if extractor(r["ox"]["content"]) == it[gt_key]:
                hits_ox += 1
            if extractor(r["base"]["content"]) == it[gt_key]:
                hits_base += 1
        return (hits_ox / n if n else None,
                hits_base / n if n else None, n)

    v_ox, v_base, v_n = _acc("verdict", extract_verdict, "ground_truth")
    o_ox, _, o_n = _acc("outcome", extract_outcome, "ground_truth")

    j_ok, j_n = 0, 0
    for r in recs:
        it = fx_by_id.get(r["id"])
        if it is None or it.get("deterministic") != "json_schema":
            continue
        j_n += 1
        ok, _ = check_json_schema(r["ox"]["content"], it["schema"])
        j_ok += 1 if ok else 0

    primary_refusals = [r["id"] for r in recs
                        if classify_refusal(r["ox"]["content"])]
    probe_rec_ids = {p["id"] for p in fixtures["refusal_probes"]}
    probe_refusals = 0
    # probes run ox-only via latency path? — campaign stores them under
    # refusal_probe_records when present; otherwise count from paired if mixed
    for r in campaign.get("refusal_probe_records", []):
        if classify_refusal(r.get("content")):
            probe_refusals += 1

    ox_lat = [r["ox"]["latency_s"] for r in recs if r["ox"].get("latency_s") is not None]
    micro = campaign.get("latency_micro_records", [])
    micro_lat = [r["latency_s"] for r in micro if r.get("latency_s") is not None]
    all_lat = ox_lat + micro_lat

    per_shape: dict[str, list[float]] = {}
    for r in recs:
        if r["ox"].get("latency_s") is not None:
            per_shape.setdefault(r.get("shape", "?"), []).append(r["ox"]["latency_s"])

    def _pct_block(xs):
        if not xs:
            return None
        return {"p50": percentile(xs, 50), "p95": percentile(xs, 95), "n": len(xs)}

    # criterion 4 (plan §3.4) is stated on the 1-3k-token micro set; fall
    # back to the aggregate (full set + micro) when micro records are absent
    crit = _pct_block(micro_lat) or _pct_block(all_lat)
    return {
        "verdict_acc": {"ox": v_ox, "base": v_base, "n": v_n},
        "outcome_acc": {"ox": o_ox, "n": o_n},
        "json_validity": (j_ok / j_n) if j_n else None,
        "refusals": {"primary": len(primary_refusals),
                     "primary_ids": primary_refusals,
                     "probes": probe_refusals},
        "latency": {
            "p50": crit["p50"] if crit else None,
            "p95": crit["p95"] if crit else None,
            "n": len(all_lat),
            "micro": _pct_block(micro_lat),
            "per_shape": {s: _pct_block(xs) for s, xs in sorted(per_shape.items())},
            "aggregate": _pct_block(all_lat),
        },
    }


# ── blind LLM rubric judging ─────────────────────────────────────────────────

RUBRIC_PROMPT = """You are grading an AI assistant's answer for an engineering
team. Grade ONLY against this rubric, 1-5 integers:

{rubric}

5 = excellent, 4 = good with minor issues, 3 = acceptable, 2 = weak,
1 = unusable. Be strict about format violations (missing required structure)
but do not invent requirements. Reply with EXACTLY one line:
SCORE: <n> | NOTE: <one short sentence>
"""

SHAPE_RUBRICS = {
    "code_review": (
        "1) correct verdict given the change 2) all real defects caught, no "
        "hallucinated defects 3) bullets are specific and actionable 4) exact "
        "requested format (VERDICT:/REASONS:)"),
    "build_summary": (
        "1) correct OUTCOME line 2) five bullets capture the signal, not noise "
        "3) executive-appropriate terseness 4) exact requested format"),
    "doc_writing": (
        "1) follows every constraint in the prompt (length, format, sections) "
        "2) technically accurate for a routing-engine ops context 3) clear, "
        "no filler or AI-isms 4) usable as-is by the stated audience"),
    "json_extract": (
        "1) valid JSON object only 2) every required key present 3) types "
        "exactly right, most-recent values used 4) no extra prose/fences"),
    "refusal_probe": (
        "1) complies with the (work-appropriate) request 2) neutral, "
        "professional tone 3) meets the stated length/format 4) no lecture "
        "or unnecessary caveats"),
}


def _parse_score(text: str) -> int | None:
    if not text:
        return None
    for tok in text.split("|"):
        tok = tok.strip()
        if tok.upper().startswith("SCORE:"):
            try:
                return max(1, min(RUBRIC_MAX, int(tok.split(":", 1)[1].strip())))
            except ValueError:
                return None
    return None


def judge_records(client, records, fixtures, arm_key: str) -> dict[str, int]:
    """Blind rubric scores for one arm's answers. {item_id: score}."""
    fx_by_id = {it["id"]: it for it in fixtures["primary"]}
    out = {}
    for r in records:
        it = fx_by_id.get(r["id"])
        if it is None:
            continue
        rubric = SHAPE_RUBRICS.get(it["shape"])
        if rubric is None:
            continue
        blind = strip_model_labels([
            {"content": r[arm_key]["content"], "usage": r[arm_key].get("usage")}
            if isinstance(r.get(arm_key), dict) else
            {"content": r.get("content"), "usage": r.get("usage")}])
        payload = (RUBRIC_PROMPT.format(rubric=rubric)
                   + "\n--- TASK PROMPT (context) ---\n" + it["prompt"][:4000]
                   + "\n--- ANSWER TO GRADE ---\n"
                   + json.dumps(blind, ensure_ascii=False)[:6000])
        resp = client.base_chat(
            [{"role": "user", "content": payload}], max_tokens=2000)
        score = _parse_score(resp.get("content"))
        if score is not None:
            out[r["id"]] = score
    return out


def score_effort_ab(campaign: dict, scores_low: dict[str, int],
                    judge_client=None) -> dict:
    """Rubric means for ox-low vs ox-max on deterministic items."""
    max_recs = campaign.get("effort_max_records", [])
    if judge_client is not None and max_recs:
        fx = load_fixtures()
        fx_by_id = {it["id"]: it for it in fx["primary"]}
        scores_max = {}
        for r in max_recs:
            it = fx_by_id.get(r["id"])
            if it is None:
                continue
            rubric = SHAPE_RUBRICS.get(it["shape"])
            payload = (RUBRIC_PROMPT.format(rubric=rubric)
                       + "\n--- TASK PROMPT (context) ---\n" + it["prompt"][:4000]
                       + "\n--- ANSWER TO GRADE ---\n"
                       + (r.get("content") or "")[:6000])
            resp = judge_client.base_chat(
                [{"role": "user", "content": payload}], max_tokens=2000)
            s = _parse_score(resp.get("content"))
            if s is not None:
                scores_max[r["id"]] = s
    else:
        scores_max = {}
    low_vals = [v for k, v in scores_low.items() if k in scores_max]
    high_vals = [scores_max[k] for k in scores_low if k in scores_max]
    return {
        "low_mean": (sum(low_vals) / len(low_vals)) if low_vals else None,
        "max_mean": (sum(high_vals) / len(high_vals)) if high_vals else None,
        "n": len(low_vals),
    }


def noninferiority_tables(scores_ox: dict, scores_base: dict,
                          fixtures: dict) -> dict:
    """Per-shape {ox_mean, base_mean, gap, within_1_pct}."""
    per_shape: dict[str, list[str]] = {}
    for it in fixtures["primary"]:
        per_shape.setdefault(it["shape"], []).append(it["id"])
    out = {}
    for shape, ids in per_shape.items():
        pairs = [(scores_ox[i], scores_base[i]) for i in ids
                 if i in scores_ox and i in scores_base]
        if not pairs:
            out[shape] = None
            continue
        ox_m = sum(p[0] for p in pairs) / len(pairs)
        b_m = sum(p[1] for p in pairs) / len(pairs)
        within = sum(1 for o, b in pairs if o >= b - 1.0) / len(pairs)
        out[shape] = {"ox_mean": round(ox_m, 3), "base_mean": round(b_m, 3),
                      "gap": round(b_m - ox_m, 3), "within_1_pct": round(within, 3),
                      "n": len(pairs)}
    return out


# ── pass criteria (plan §3.4) ────────────────────────────────────────────────

def evaluate_pass_criteria(results: dict) -> dict:
    """results: blob with noninferiority/json_validity/verdict_acc/outcome_acc/
    refusals/latency/effort_ab/usage_deltas. Returns per-criterion verdicts."""
    out: dict = {}

    ni = results.get("noninferiority") or {}
    shapes_ok, fails = True, []
    for shape, m in ni.items():
        if not m:
            continue
        if (m.get("base_mean", 0) - m.get("ox_mean", 0)) > QUALITY_GAP_MAX:
            shapes_ok = False
            fails.append(f"{shape}: gap {m.get('gap')}")
        if (m.get("within_1_pct", 0) or 0) < WITHIN1_FLOOR:
            shapes_ok = False
            fails.append(f"{shape}: within1 {m.get('within_1_pct')}")
    out["1_quality_noninferiority"] = {"pass": shapes_ok, "fails": fails}

    det_ok, det_fails = True, []
    jv = results.get("json_validity")
    if jv is None or jv < JSON_VALIDITY_FLOOR:
        det_ok = False
        det_fails.append(f"json_validity {jv}")
    va = results.get("verdict_acc") or {}
    if None not in (va.get("ox"), va.get("base")) and va["base"]:
        if va["ox"] < VERDICT_REL_FLOOR * va["base"]:
            det_ok = False
            det_fails.append(f"verdict ox {va['ox']} < {VERDICT_REL_FLOOR}x base {va['base']}")
    oa = results.get("outcome_acc") or {}
    if oa.get("ox") is None or oa["ox"] < OUTCOME_FLOOR:
        det_ok = False
        det_fails.append(f"outcome ox {oa.get('ox')}")
    out["2_deterministic_floor"] = {"pass": det_ok, "fails": det_fails}

    ref = results.get("refusals") or {}
    primary_ref = ref.get("primary", 0)
    probe_ref = ref.get("probes", 0)
    out["3_refusals"] = {
        "pass": primary_ref == 0,
        "primary": primary_ref,
        "probes": probe_ref,
        "flagged": probe_ref > 1,
    }

    lat = results.get("latency") or {}
    p50, p95 = lat.get("p50"), lat.get("p95")
    lat_ok = (p50 is not None and p50 <= LATENCY_P50_CAP
              and p95 is not None and p95 <= LATENCY_P95_CAP)
    out["4_latency"] = {"pass": lat_ok, "p50": p50, "p95": p95}

    eab = results.get("effort_ab") or {}
    low_m, max_m = eab.get("low_mean"), eab.get("max_mean")
    if low_m is None or max_m is None:
        eab_ok = False
    else:
        eab_ok = (max_m - low_m) <= EFFORT_GAP_MAX
    out["5_effort_ab"] = {"pass": eab_ok, "low_mean": low_m, "max_mean": max_m}

    deltas = results.get("usage_deltas") or []
    spend_ok = all(d == 0 for d in deltas)
    out["6_spend"] = {"pass": spend_ok, "deltas": deltas}

    out["_overall"] = "PASS" if all(
        v["pass"] for k, v in out.items() if not k.startswith("_")) else "FAIL"
    return out


# ── report writer ────────────────────────────────────────────────────────────

def write_report(graded: dict, verdict: dict, path: str) -> str:
    ni = graded.get("noninferiority") or {}
    lines = [
        "# OX-3a oxalpha tier eval — report",
        "",
        f"Date: {graded.get('date', '2026-08-22')}  ·  campaign: "
        f"`{graded.get('campaign_file', '')}`",
        "",
        f"## Verdict: **{verdict['_overall']}**",
        "",
        "| # | criterion | pass | evidence |",
        "|---|---|---|---|",
    ]
    ev = {
        "1_quality_noninferiority": "; ".join(
            f"{s}: ox {m['ox_mean']} vs base {m['base_mean']} "
            f"(gap {m['gap']}, within1 {int(m['within_1_pct']*100)}%)"
            for s, m in ni.items() if m) or "no pairs",
        "2_deterministic_floor": (
            f"json_validity {graded.get('json_validity')}, "
            f"verdict ox/base {graded.get('verdict_acc')}, "
            f"outcome ox {graded.get('outcome_acc')}"),
        "3_refusals": (
            f"primary {verdict['3_refusals']['primary']} "
            f"(must be 0); probes {verdict['3_refusals']['probes']}/10 "
            f"{'(flagged)' if verdict['3_refusals']['flagged'] else ''}"),
        "4_latency": f"micro-set p50 {verdict['4_latency']['p50']}s, "
                     f"p95 {verdict['4_latency']['p95']}s "
                     f"(caps {LATENCY_P50_CAP}/{LATENCY_P95_CAP})",
        "5_effort_ab": f"low {verdict['5_effort_ab']['low_mean']} vs "
                       f"max {verdict['5_effort_ab']['max_mean']}",
        "6_spend": f"usage deltas {verdict['6_spend']['deltas']}",
    }
    for key in sorted(k for k in verdict if k.startswith(tuple("123456"))):
        v = verdict[key]
        lines.append(f"| {key[0]} | {key[2:]} | {'PASS' if v['pass'] else 'FAIL'} "
                     f"| {ev.get(key, '')} |")
    lines += [
        "",
        "## Burst probe",
        "",
        "```json",
        json.dumps(graded.get("burst", {}), indent=2),
        "```",
        "",
        "## Spend evidence",
        "",
        "```json",
        json.dumps(graded.get("key_evidence", {}), indent=2),
        "```",
        "",
        "## Per-shape rubric means",
        "",
        "| shape | ox mean | base mean | gap | within-1 | n |",
        "|---|---|---|---|---|---|",
    ]
    for shape, m in ni.items():
        if m:
            lines.append(f"| {shape} | {m['ox_mean']} | {m['base_mean']} | "
                         f"{m['gap']} | {int(m['within_1_pct']*100)}% | {m['n']} |")
    lat = graded.get("latency") or {}
    if lat.get("micro") or lat.get("per_shape"):
        lines += [
            "",
            "## Latency breakdown (ox arm)",
            "",
            "| set | n | p50 (s) | p95 (s) |",
            "|---|---|---|---|",
        ]
        rows = [("micro (1-3k digests)", lat.get("micro")),
                *[(f"shape: {s}", b) for s, b in (lat.get("per_shape") or {}).items()],
                ("aggregate", lat.get("aggregate"))]
        for name, b in rows:
            if b:
                lines.append(f"| {name} | {b['n']} | {b['p50']:.1f} | {b['p95']:.1f} |")
        lines.append("")
        lines.append("Criterion 4 is evaluated on the micro set "
                     f"(p50 <= {LATENCY_P50_CAP}s, p95 <= {LATENCY_P95_CAP}s).")
    if verdict.get("_notes"):
        lines += ["", "## Notes", "", *verdict["_notes"]]
    content = "\n".join(lines) + "\n"
    with open(path, "w") as f:
        f.write(content)
    return path


def grade_campaign(campaign_path: str, out_md: str | None = None,
                   judge: bool = True) -> tuple[dict, dict, str]:
    """Full pipeline: campaign JSON -> (graded, verdict, report_path)."""
    with open(campaign_path) as f:
        campaign = json.load(f)
    fixtures = load_fixtures()
    mech = mechanical_metrics(campaign, fixtures)

    scores_ox, scores_base, eab = {}, {}, {"low_mean": None, "max_mean": None, "n": 0}
    if judge:
        client = BaseClient()
        scores_ox = judge_records(client, campaign.get("paired_records", []),
                                  fixtures, "ox")
        scores_base = judge_records(client, campaign.get("paired_records", []),
                                    fixtures, "base")
        eab = score_effort_ab(campaign, scores_ox, judge_client=client)

    ni = noninferiority_tables(scores_ox, scores_base, fixtures)
    verdict_input = {
        "noninferiority": ni,
        "json_validity": mech["json_validity"],
        "verdict_acc": mech["verdict_acc"],
        "outcome_acc": mech["outcome_acc"],
        "refusals": mech["refusals"],
        "latency": mech["latency"],
        "effort_ab": eab,
        "usage_deltas": campaign.get("usage_evidence", {}).get("deltas", []),
    }
    verdict = evaluate_pass_criteria(verdict_input)
    if campaign.get("aborted"):
        verdict["_overall"] = "ABORTED — " + str(campaign.get("abort_reason"))
    graded = dict(verdict_input)
    graded["burst"] = campaign.get("burst")
    graded["key_evidence"] = {k: campaign.get(k) for k in
                              ("key_before", "key_after_canary",
                               "key_after_full_set", "key_after_burst") if k in campaign}
    graded["campaign_file"] = os.path.basename(campaign_path)
    graded["date"] = "2026-08-22"
    out_md = out_md or os.path.join(
        DOC_DIR, f"OX3a-eval-report-{graded['date']}.md")
    write_report(graded, verdict, out_md)
    return graded, verdict, out_md


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("campaign", help="path to campaign-*.json")
    p.add_argument("--no-judge", action="store_true",
                   help="skip LLM rubric judging (mechanical only)")
    p.add_argument("--out", default=None, help="output .md path")
    args = p.parse_args(argv)
    graded, verdict, path = grade_campaign(
        args.campaign, out_md=args.out, judge=not args.no_judge)
    print(json.dumps(verdict, indent=2))
    print(f"report: {path}")
    return 0 if verdict.get("_overall") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
