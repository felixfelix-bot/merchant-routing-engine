"""quality_floor.py — model quality registry + floor resolution.

Workstream B step 1-2: seed-then-replace quality registry.

The registry holds a *quality score* per model (normalized 0..1, higher is
better).  Scores come from two sources, kept strictly separate:

  * QUALITY_SEED          — real, independently-fetched scores (artificialanalysis.ai
                            Intelligence Index).  These are the ONLY numbers that may
                            ever be treated as "verified".  Never invent a number into
                            this dict.
  * PROVISIONAL_ESTIMATES — operator-supplied guesses for models that have no
                            independent score yet.  These are NOT VERIFIED and must be
                            reviewed before they can be promoted to seeds.

The registry is *seed-then-replace*: today every model resolves to a seed or a
provisional estimate (n_judged == 0).  A future probe writer will call
``read_quality_measurements(model)`` (currently a documented stub returning [])
and feed real per-request judgments into an EWMA that replaces the seed.

Integrity rule (carried from design spec §2): never invent scores into
QUALITY_SEED.  Unverified numbers go ONLY into PROVISIONAL_ESTIMATES, marked
"NOT VERIFIED — operator review required".
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import sys

log = logging.getLogger("quality_floor")

# ── Seed header + date ───────────────────────────────────────────────────────
# The seed date is parsed from this header so the 30-day TTL is self-describing.
SEED_HEADER = "# SEED(2026-09-04): artificialanalysis.ai Intelligence Index v4.1.1"
SEED_SOURCE = "artificialanalysis.ai Intelligence Index v4.1.1"

# 30-day TTL for seeds that have not yet been replaced by real measurements.
SEED_TTL_DAYS = 30


def _parse_seed_date(header: str) -> _dt.date:
    """Parse the YYYY-MM-DD out of the SEED header string."""
    import re

    m = re.search(r"SEED\((\d{4}-\d{2}-\d{2})\)", header)
    if not m:
        raise ValueError(f"SEED header missing date: {header!r}")
    return _dt.date.fromisoformat(m.group(1))


SEED_DATE = _parse_seed_date(SEED_HEADER)

# ── FAMILIES ─────────────────────────────────────────────────────────────────
# Explicit mapping of every model in the routing universe (flat_router.py
# PROVIDER_MODELS) to a family name.  Used for --family filtering.
FAMILIES: dict[str, str] = {
    # GLM family (z.ai catalog)
    "glm-5.2": "glm",
    "glm-5.3": "glm",
    "glm-5.3-flash": "glm",
    "glm-4.5-flash": "glm",
    "glm-4.5-air": "glm",
    "glm-4.5": "glm",
    "glm-4.6v": "glm",
    "glm-4.7": "glm",
    "glm-5": "glm",
    "glm-5-turbo": "glm",
    "glm-5.1": "glm",
    # DeepSeek family (slashed canonical form)
    "deepseek/deepseek-v4-flash": "deepseek",
    "deepseek/deepseek-v4-pro": "deepseek",
    "deepseek/gemma-4-31b": "deepseek",
    # Kimi family
    "kimi-k3": "kimi",
    "kimi-k2.7-code": "kimi",
    "kimi-k2.6": "kimi",
    "kimi-k2.5": "kimi",
    # MiniMax family
    "minimax-m3": "minimax",
    "minimax-m2.7": "minimax",
    # Qwen family
    "qwen3.5:397b": "qwen",
    # gpt-oss family
    "gpt-oss:120b": "gpt-oss",
    "gpt-oss:20b": "gpt-oss",
    # Gemma family
    "gemma4:31b": "gemma",
    # Mistral family
    "mistral-large-3:675b": "mistral",
    # Nemotron family
    "nemotron-3-nano:30b": "nemotron",
    "nemotron-3-super": "nemotron",
    "nemotron-3-ultra": "nemotron",
    # OpenAI / Anthropic (telnyx catalog)
    "gpt-5": "openai",
    "claude-haiku-4-5": "anthropic",
}

# ── QUALITY_SEED ─────────────────────────────────────────────────────────────
# REAL fetched scores only.  Source: artificialanalysis.ai Intelligence Index
# v4.1.1 (fetched 2026-09-04 from the /models page embedded ld+json Dataset).
# Raw index is 0..100; normalized to 0..1 by /100.
#
# Only 5 of our models appear in AA's published top-20 Intelligence Index list.
# Every other model in FAMILIES has NO independent score and lives in
# PROVISIONAL_ESTIMATES below.
QUALITY_SEED: dict[str, float] = {
    # Kimi K3 (max) — raw 59.70
    "kimi-k3": 0.5970,
    # GLM-5.3 (max) — raw 59.51
    "glm-5.3": 0.5951,
    # GLM-5.3-Flash — raw 57.46
    "glm-5.3-flash": 0.5746,
    # DeepSeek V4 Pro 0813 (max) — raw 53.20
    "deepseek/deepseek-v4-pro": 0.5320,
    # MiniMax-M3 — raw 45.40
    "minimax-m3": 0.4540,
}

# ── PROVISIONAL_ESTIMATES ────────────────────────────────────────────────────
# NOT VERIFIED — operator review required.
#
# Operator-supplied guesses for models with no independent Intelligence Index
# score.  These are anchored to the real seed scores where a family relationship
# exists (e.g. glm-5.2 sits just below the measured glm-5.3), but they are
# ESTIMATES, not measurements.  They must never be promoted to QUALITY_SEED
# without an independent source.
PROVISIONAL_ESTIMATES: dict[str, float] = {
    # GLM family — anchored to glm-5.3 (0.5951) / glm-5.3-flash (0.5746)
    "glm-5.2": 0.5600,
    "glm-5.1": 0.5500,
    "glm-5": 0.5400,
    "glm-5-turbo": 0.5300,
    "glm-4.7": 0.5200,
    "glm-4.6v": 0.5000,
    "glm-4.5": 0.4800,
    "glm-4.5-air": 0.4600,
    "glm-4.5-flash": 0.4400,
    # DeepSeek family — anchored to deepseek-v4-pro (0.5320)
    "deepseek/deepseek-v4-flash": 0.5000,
    "deepseek/gemma-4-31b": 0.4200,
    # Kimi family — anchored to kimi-k3 (0.5970)
    "kimi-k2.7-code": 0.5500,
    "kimi-k2.6": 0.5200,
    "kimi-k2.5": 0.5000,
    # MiniMax family — anchored to minimax-m3 (0.4540)
    "minimax-m2.7": 0.4200,
    # Qwen family
    "qwen3.5:397b": 0.5200,
    # gpt-oss family
    "gpt-oss:120b": 0.5000,
    "gpt-oss:20b": 0.4000,
    # Gemma family
    "gemma4:31b": 0.4500,
    # Mistral family
    "mistral-large-3:675b": 0.5000,
    # Nemotron family
    "nemotron-3-ultra": 0.5200,
    "nemotron-3-super": 0.4800,
    "nemotron-3-nano:30b": 0.4000,
    # OpenAI / Anthropic (telnyx catalog)
    "gpt-5": 0.6000,
    "claude-haiku-4-5": 0.5500,
}

# Every model the registry knows about (seed + provisional).
ALL_MODELS: set[str] = set(QUALITY_SEED) | set(PROVISIONAL_ESTIMATES)


# ── Measurement hook (future probe writer) ──────────────────────────────────
def read_quality_measurements(model: str) -> list[dict]:
    """Return per-request quality judgments for ``model``.

    DOCUMENTED INTERFACE for a future probe writer.  Today there is no
    per-model quality table in the DB (provider_telemetry is per-provider
    canary pass/fail with no correct_answer column), so this returns [].

    Expected future shape of each measurement dict::

        {
            "model": str,
            "correct": bool,          # was the answer judged correct?
            "ts": float,              # unix epoch seconds
            "judge": str,             # which judge produced the verdict
        }

    When this returns non-empty measurements, ``quality_ewma`` will fold them
    into an EWMA that replaces the seed/provisional score.
    """
    return []


# ── EWMA ─────────────────────────────────────────────────────────────────────
def quality_ewma(model: str) -> tuple[float | None, int, str]:
    """Return ``(score, n_judged, source)`` for ``model``.

    Seed-only today: returns the seed or provisional score with n_judged == 0.
    ``read_quality_measurements`` is consulted (currently always []) so the
    EWMA path is wired but inert until a probe writer lands.

    source is one of: ``"seed"``, ``"provisional"``, ``"no-evidence"``.
    """
    measurements = read_quality_measurements(model)

    if measurements:
        # Future path: fold real judgments into an EWMA seeded by the
        # registry score.  Not exercised today (stub returns []).
        base = QUALITY_SEED.get(model, PROVISIONAL_ESTIMATES.get(model))
        if base is None:
            return (None, 0, "no-evidence")
        # Placeholder EWMA — real implementation lands with the probe writer.
        score = base
        n = len(measurements)
        return (score, n, "ewma")

    if model in QUALITY_SEED:
        return (QUALITY_SEED[model], 0, "seed")
    if model in PROVISIONAL_ESTIMATES:
        return (PROVISIONAL_ESTIMATES[model], 0, "provisional")
    return (None, 0, "no-evidence")


# ── Floor resolution ─────────────────────────────────────────────────────────
def _seed_expired(source: str, n_judged: int, now: _dt.date) -> bool:
    """True if a seed with no real measurements has passed its 30-day TTL."""
    if source != "seed" or n_judged != 0:
        return False
    return (now - SEED_DATE).days > SEED_TTL_DAYS


def resolve_floor(
    floor: float,
    family: str | None = None,
    now: _dt.date | None = None,
) -> list[dict]:
    """Resolve which models clear ``floor``.

    Rules:
      * score >= floor passes (boundary inclusive).
      * optional ``family`` filter (matches FAMILIES values).
      * 30-day TTL on seeds with n_judged == 0 (parsed from SEED header date);
        expired seeds are excluded with a loud log.
      * no-evidence models (not in FAMILIES / no score) are excluded with a
        loud log.
      * result sorted by score descending.

    Returns a list of ``{"model", "score", "source", "n_judged"}`` dicts.
    """
    now = now or _dt.date.today()
    results: list[dict] = []

    # Iterate the full model universe (FAMILIES), not just ALL_MODELS, so that
    # models with no score in either dict surface as "no-evidence" and get a
    # loud exclusion log.
    for model in sorted(FAMILIES):
        if family is not None and FAMILIES.get(model) != family:
            continue

        score, n_judged, source = quality_ewma(model)

        if source == "no-evidence":
            log.warning(
                "quality_floor: model %r has no quality evidence — excluded from floor",
                model,
            )
            continue

        if _seed_expired(source, n_judged, now):
            log.warning(
                "quality_floor: seed for %r expired (seeded %s, %d days ago) — "
                "excluded from floor",
                model,
                SEED_DATE.isoformat(),
                (now - SEED_DATE).days,
            )
            continue

        if score is not None and score >= floor:
            results.append(
                {
                    "model": model,
                    "score": score,
                    "source": source,
                    "n_judged": n_judged,
                }
            )

    results.sort(key=lambda r: r["score"], reverse=True)
    return results


# ── CLI ──────────────────────────────────────────────────────────────────────
def _build_table(rows: list[dict]) -> str:
    lines = [
        f"{'MODEL':<28} {'SCORE':>6} {'SOURCE':<12} {'N_JUDGED':>8}",
        "-" * 60,
    ]
    for r in rows:
        lines.append(
            f"{r['model']:<28} {r['score']:>6.4f} {r['source']:<12} {r['n_judged']:>8}"
        )
    return "\n".join(lines)


def _list_all() -> str:
    """Full table including provisional models, flagged as NOT VERIFIED."""
    rows = []
    for model in sorted(ALL_MODELS):
        score, n_judged, source = quality_ewma(model)
        flag = ""
        if source == "provisional":
            flag = "  [NOT VERIFIED — operator review required]"
        elif source == "no-evidence":
            flag = "  [NO EVIDENCE]"
        rows.append(
            {
                "model": model,
                "score": score if score is not None else 0.0,
                "source": source,
                "n_judged": n_judged,
                "flag": flag,
            }
        )
    rows.sort(key=lambda r: r["score"], reverse=True)
    lines = [
        f"{'MODEL':<28} {'SCORE':>6} {'SOURCE':<12} {'N_JUDGED':>8}",
        "-" * 60,
    ]
    for r in rows:
        lines.append(
            f"{r['model']:<28} {r['score']:>6.4f} {r['source']:<12} "
            f"{r['n_judged']:>8}{r['flag']}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="quality_floor",
        description="Model quality registry + floor resolution.",
    )
    parser.add_argument("--floor", type=float, help="minimum quality score (0..1)")
    parser.add_argument("--family", type=str, help="restrict to a model family")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--list", action="store_true", help="full table incl. provisional")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    if args.list:
        print(_list_all())
        return 0

    if args.floor is None:
        parser.error("--floor is required unless --list is given")

    rows = resolve_floor(args.floor, family=args.family)
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print(_build_table(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
