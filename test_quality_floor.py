"""Tests for src/quality_floor.py (Workstream B step 1-2)."""

import datetime as _dt
import importlib.util as _ilu
import os as _os

import pytest

# ── Pin the repo's quality_floor module ──────────────────────────────────────
# When this file runs in the same pytest process as test_flat_router.py,
# flat_router's path bootstrap has already imported `src` from ~/.hermes/bot
# (a namespace package) and cached it in sys.modules, so a bare
# `from src import quality_floor` resolves to the wrong package. Load the repo's
# quality_floor.py by explicit path instead — the source of truth for this
# module.
_QF_PATH = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                         "src", "quality_floor.py")
_qf_spec = _ilu.spec_from_file_location("quality_floor", _QF_PATH)
qf = _ilu.module_from_spec(_qf_spec)
_qf_spec.loader.exec_module(qf)


# ── ewma stub behavior ───────────────────────────────────────────────────────
def test_ewma_seed_returns_seed_score_n0():
    score, n, source = qf.quality_ewma("kimi-k3")
    assert score == pytest.approx(0.5970)
    assert n == 0
    assert source == "seed"


def test_ewma_provisional_returns_provisional_score_n0():
    score, n, source = qf.quality_ewma("glm-5.2")
    assert score == pytest.approx(0.5600)
    assert n == 0
    assert source == "provisional"


def test_ewma_unknown_model_no_evidence():
    score, n, source = qf.quality_ewma("does-not-exist")
    assert score is None
    assert n == 0
    assert source == "no-evidence"


def test_read_quality_measurements_stub_returns_empty():
    assert qf.read_quality_measurements("kimi-k3") == []


# ── TTL expiry ───────────────────────────────────────────────────────────────
def test_seed_not_expired_within_ttl():
    now = qf.SEED_DATE + _dt.timedelta(days=10)
    assert not qf._seed_expired("seed", 0, now)


def test_seed_expired_after_ttl():
    now = qf.SEED_DATE + _dt.timedelta(days=31)
    assert qf._seed_expired("seed", 0, now)


def test_seed_not_expired_when_judged():
    # n_judged > 0 means real measurements replaced the seed — no TTL.
    now = qf.SEED_DATE + _dt.timedelta(days=1000)
    assert not qf._seed_expired("seed", 5, now)


def test_provisional_never_expires():
    now = qf.SEED_DATE + _dt.timedelta(days=1000)
    assert not qf._seed_expired("provisional", 0, now)


def test_resolve_floor_excludes_expired_seed():
    now = qf.SEED_DATE + _dt.timedelta(days=31)
    rows = qf.resolve_floor(0.0, now=now)
    models = {r["model"] for r in rows}
    # kimi-k3 is a seed; after TTL it must be gone.
    assert "kimi-k3" not in models
    # provisional models still present.
    assert "glm-5.2" in models


# ── no-evidence exclusion ────────────────────────────────────────────────────
def test_resolve_floor_excludes_no_evidence(caplog):
    # A model in FAMILIES but with no score in either dict.
    qf.FAMILIES["phantom-model"] = "phantom"
    try:
        with caplog.at_level("WARNING"):
            rows = qf.resolve_floor(0.0)
        models = {r["model"] for r in rows}
        assert "phantom-model" not in models
        assert any("no quality evidence" in r.message for r in caplog.records)
    finally:
        del qf.FAMILIES["phantom-model"]


# ── family filter ────────────────────────────────────────────────────────────
def test_resolve_floor_family_filter():
    rows = qf.resolve_floor(0.0, family="glm")
    assert rows, "expected glm models"
    assert all(qf.FAMILIES[r["model"]] == "glm" for r in rows)
    assert "kimi-k3" not in {r["model"] for r in rows}


def test_resolve_floor_family_no_match_empty():
    rows = qf.resolve_floor(0.0, family="nonexistent-family")
    assert rows == []


# ── floor boundary inclusivity ───────────────────────────────────────────────
def test_floor_boundary_inclusive():
    # kimi-k3 seed = 0.5970.  Floor exactly equal must pass.
    rows = qf.resolve_floor(0.5970)
    models = {r["model"]: r["score"] for r in rows}
    assert "kimi-k3" in models
    assert models["kimi-k3"] == pytest.approx(0.5970)


def test_floor_just_above_excludes():
    rows = qf.resolve_floor(0.5971)
    assert "kimi-k3" not in {r["model"] for r in rows}


# ── provisional/seed separation ──────────────────────────────────────────────
def test_seed_and_provisional_disjoint():
    assert not (set(qf.QUALITY_SEED) & set(qf.PROVISIONAL_ESTIMATES))


def test_all_models_union():
    assert qf.ALL_MODELS == set(qf.QUALITY_SEED) | set(qf.PROVISIONAL_ESTIMATES)


def test_every_model_has_family():
    for model in qf.ALL_MODELS:
        assert model in qf.FAMILIES, f"{model} missing from FAMILIES"


def test_seed_scores_normalized_0_1():
    for model, score in qf.QUALITY_SEED.items():
        assert 0.0 <= score <= 1.0, f"{model} seed out of range: {score}"


# ── resolve ordering ─────────────────────────────────────────────────────────
def test_resolve_floor_sorted_desc():
    rows = qf.resolve_floor(0.0)
    scores = [r["score"] for r in rows]
    assert scores == sorted(scores, reverse=True)


def test_resolve_floor_entry_shape():
    rows = qf.resolve_floor(0.0)
    assert rows
    for r in rows:
        assert set(r.keys()) == {"model", "score", "source", "n_judged"}
