"""OX-2 contract tests — proxy-side oxalpha tier (src/oxalpha_tier.py).

Covers the task-body TESTS list + the acceleration-plan §4 contract list:
  - absent key -> tier disabled (fail-closed, no error)
  - allowlist reject at request path (OX-1 helper)
  - 429 backoff sequence 60/120/300 + circuit breaker 5 fails / 300 s
  - expiry behaviour end-to-end at request path (guard flips, tier skipped)
  - cost>0 mid-stream kill (no re-enable)
  - zai routing unchanged (model_map regression vs git HEAD)
  - alias: kill-switch file respected; fall-through on 429/timeout/guard-kill;
    glm-5.2/glm-5.3 NEVER aliased; images NEVER aliased; gated task types
    never aliased at rung-1 config; chain byte-identical with tier disabled.

PURITY: no network, no production-file imports. The production wiring
(~/.hermes/bot/zai_proxy.py) is a thin shell over this module; its behaviour
is specified by these contracts and verified by the restart smoke test
documented in docs/REVERT-oxalpha-proxy-wiring-2026-08-22.md.
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import promo_tier  # noqa: E402
from src import oxalpha_tier as ox  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
PROMO_END = datetime(2026, 8, 28, 0, 0, tzinfo=timezone.utc)
NOW_OK = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)


class FakeClock:
    """Injectable monotonic clock for backoff/breaker state."""

    def __init__(self, start: float = 1_000_000.0):
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


def make_guard(**kw) -> promo_tier.PromoTierGuard:
    return promo_tier.PromoTierGuard(promo_end=kw.pop("promo_end", PROMO_END), **kw)


def make_tier(api_key: str = "sk-or-test-key", clock=None, **kw) -> ox.OxalphaTier:
    return ox.OxalphaTier(
        guard=kw.pop("guard", make_guard()),
        api_key=api_key,
        clock=clock or FakeClock(),
        killswitch_path=kw.pop("killswitch_path", REPO / ".oxalpha_alias_off.TEST"),
        **kw,
    )


def rung1_preferred(enabled: bool = True) -> dict:
    return {"enabled": enabled, "models": ["glm-4.5-flash"],
            "task_types": ["bulk_summarize"]}


# ── absent key -> disabled (fail-closed, no error) ──────────────────────────

def test_absent_key_disables_everything():
    tier = make_tier(api_key="")
    assert tier.configured is False
    assert tier.failover_eligible(now=NOW_OK) is False
    ok, reason = tier.alias_eligible("glm-4.5-flash", "bulk_summarize",
                                     now=NOW_OK)
    assert ok is False
    # fail-closed, never loud
    assert tier.status(now=NOW_OK)["failover_eligible"] is False


def test_placeholder_key_is_treated_as_absent():
    # a masked/placeholder value must not count as provisioned
    tier = make_tier(api_key="...")
    assert tier.configured is False


# ── descriptor (EMERGENCY config contract) ──────────────────────────────────

def test_descriptor_matches_emergency_config():
    d = make_tier().descriptor()
    assert d["name"] == "oxalpha"
    assert d["base_url"] == "https://openrouter.ai/api/v1"
    assert d["model"] == "stealth/ox-alpha"
    assert d["reasoning_effort"] == "low"
    assert d["max_completion_tokens"] == 8192
    assert d["upstream_timeout_s"] == 90.0
    assert d["single_attempt"] is True


# ── 429 backoff sequence 60/120/300 + breaker ───────────────────────────────

def test_429_backoff_sequence_60_120_300():
    clock = FakeClock()
    tier = make_tier(clock=clock)
    assert tier.failover_eligible(now=NOW_OK) is True
    assert tier.note_429() == 60.0
    assert tier.failover_eligible(now=NOW_OK) is False      # suppressed
    clock.advance(60.5)
    assert tier.failover_eligible(now=NOW_OK) is True
    assert tier.note_429() == 120.0                          # escalates
    clock.advance(120.5)
    assert tier.failover_eligible(now=NOW_OK) is True
    assert tier.note_429() == 300.0
    clock.advance(300.5)
    assert tier.note_429() == 300.0                          # capped
    clock.advance(300.5)
    assert tier.failover_eligible(now=NOW_OK) is True
    tier.note_success()                                      # reset
    assert tier.note_429() == 60.0                           # sequence restarts


def test_circuit_breaker_after_5_consecutive_failures():
    clock = FakeClock()
    tier = make_tier(clock=clock)
    for _ in range(4):
        tier.note_failure()
        assert tier.failover_eligible(now=NOW_OK) is True
    tier.note_failure()                                      # 5th
    assert tier.failover_eligible(now=NOW_OK) is False       # breaker open
    clock.advance(300.5)                                     # cooldown over
    assert tier.failover_eligible(now=NOW_OK) is True


def test_429s_also_count_toward_breaker():
    clock = FakeClock()
    tier = make_tier(clock=clock)
    for _ in range(4):
        tier.note_429()
        clock.advance(301.0)  # past each backoff window
    tier.note_429()           # 5th consecutive failure -> breaker opens NOW
    assert tier.failover_eligible(now=NOW_OK) is False
    clock.advance(300.5)      # cooldown over -> eligible again
    assert tier.failover_eligible(now=NOW_OK) is True


def test_success_resets_failure_streak():
    tier = make_tier(clock=FakeClock())
    for _ in range(4):
        tier.note_failure()
    tier.note_success()
    tier.note_failure()
    assert tier.failover_eligible(now=NOW_OK) is True  # streak was 1, not 5


# ── expiry end-to-end at request path ───────────────────────────────────────

def test_expiry_disables_failover_and_alias_at_request_path():
    tier = make_tier(guard=make_guard(promo_end=NOW_OK - timedelta(seconds=1)))
    assert tier.failover_eligible(now=NOW_OK) is False
    ok, _ = tier.alias_eligible("glm-4.5-flash", "bulk_summarize", now=NOW_OK)
    assert ok is False
    # and the guard has flipped pricing to the post-promo estimate (priced out)
    price = tier.guard.effective_price_per_m(NOW_OK)
    assert price["input"] == pytest.approx(10.0)
    assert price["output"] == pytest.approx(30.0)


# ── cost>0 mid-stream kill ──────────────────────────────────────────────────

def test_nonzero_charge_kills_with_no_reenable():
    tier = make_tier()
    ev = tier.observe_response_cost(0.04, now=NOW_OK)
    assert ev is not None and ev["severity"] == "critical"
    assert tier.failover_eligible(now=NOW_OK + timedelta(hours=1)) is False
    assert tier.alias_eligible("glm-4.5-flash", "bulk_summarize",
                               now=NOW_OK + timedelta(hours=1))[0] is False


def test_zero_charge_is_noop():
    tier = make_tier()
    assert tier.observe_response_cost(0.0, now=NOW_OK) is None
    assert tier.observe_response_cost(None, now=NOW_OK) is None
    assert tier.failover_eligible(now=NOW_OK) is True


def test_402_disables_for_promo_remainder():
    tier = make_tier()
    ev = tier.note_http_status(402, now=NOW_OK)
    assert ev is not None
    assert tier.failover_eligible(now=NOW_OK + timedelta(hours=1)) is False


# ── allowlist enforcement at request path (OX-1 helper) ─────────────────────

def test_allowlist_rejects_non_allowlisted_task_types():
    tier = make_tier(preferred=rung1_preferred())
    for tt in ("coding", "research", None, "VISION"):
        ok, reason = tier.alias_eligible("glm-4.5-flash", tt, now=NOW_OK)
        assert ok is False, tt
        assert reason  # reason string always present on reject


def test_gated_task_types_never_aliased_at_rung1():
    tier = make_tier(preferred=rung1_preferred())
    for tt in ("coding", "review", "research"):
        ok, _ = tier.alias_eligible("glm-4.5-flash", tt, now=NOW_OK)
        assert ok is False


def test_rung1_alias_accepts_bulk_summarize_flash_no_images():
    tier = make_tier(preferred=rung1_preferred())
    ok, _ = tier.alias_eligible("glm-4.5-flash", "bulk_summarize",
                                has_images=False, now=NOW_OK)
    assert ok is True


# ── alias contract (acceleration plan §4/§5) ────────────────────────────────

def test_hard_excluded_models_never_aliased_even_if_misconfigured():
    bad = {"enabled": True, "models": ["glm-4.5-flash", "glm-5.2", "glm-5.3"],
           "task_types": ["bulk_summarize"]}
    tier = make_tier(preferred=bad)
    ok, reason = tier.alias_eligible("glm-5.2", "bulk_summarize", now=NOW_OK)
    assert ok is False and "never" in reason.lower()
    ok, reason = tier.alias_eligible("glm-5.3", "bulk_summarize", now=NOW_OK)
    assert ok is False and "never" in reason.lower()
    # failover path is NOT aliased-model-gated (emergency catch-all), but the
    # model rewrite always forces stealth/ox-alpha — see body-mutation tests


def test_image_bearing_requests_never_aliased():
    tier = make_tier(preferred=rung1_preferred())
    ok, _ = tier.alias_eligible("glm-4.5-flash", "bulk_summarize",
                                has_images=True, now=NOW_OK)
    assert ok is False


def test_killswitch_file_respected(tmp_path):
    ks = tmp_path / ".oxalpha_alias_off"
    ks.write_text("off\n")
    tier = make_tier(preferred=rung1_preferred(), killswitch_path=ks)
    ok, _ = tier.alias_eligible("glm-4.5-flash", "bulk_summarize", now=NOW_OK)
    assert ok is False
    ks.unlink()
    ok, _ = tier.alias_eligible("glm-4.5-flash", "bulk_summarize", now=NOW_OK)
    assert ok is True


def test_killswitch_globs_alias_only_failover_stays():
    ks = tmp_path = None  # placeholder; real tmp below
    # (failover is the EMERGENCY bleed-stop path; kill-switch is alias-scoped
    # per acceleration plan §4.2 — tier usable for failover/opt-in after rm)
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        ks = Path(td) / ".oxalpha_alias_off"
        ks.write_text("off\n")
        tier = make_tier(preferred=rung1_preferred(), killswitch_path=ks)
        assert tier.alias_eligible("glm-4.5-flash", "bulk_summarize",
                                   now=NOW_OK)[0] is False
        assert tier.failover_eligible(now=NOW_OK) is True


def test_alias_disabled_by_default_config():
    tier = make_tier(preferred=None)  # absent block == not enabled
    ok, _ = tier.alias_eligible("glm-4.5-flash", "bulk_summarize", now=NOW_OK)
    assert ok is False


def test_alias_falls_through_on_429_timeout_guardkill():
    clock = FakeClock()
    tier = make_tier(preferred=rung1_preferred(), clock=clock)
    # 429 suppresses the alias attempt -> chain runs untouched
    tier.note_429()
    assert tier.alias_eligible("glm-4.5-flash", "bulk_summarize",
                               now=NOW_OK)[0] is False
    clock.advance(60.5)
    assert tier.alias_eligible("glm-4.5-flash", "bulk_summarize",
                               now=NOW_OK)[0] is True
    # breaker (timeout/failure streak) suppresses too
    for _ in range(5):
        tier.note_failure()
    assert tier.alias_eligible("glm-4.5-flash", "bulk_summarize",
                               now=NOW_OK)[0] is False
    # guard kill suppresses permanently
    tier2 = make_tier(preferred=rung1_preferred())
    tier2.observe_response_cost(0.01, now=NOW_OK)
    assert tier2.alias_eligible("glm-4.5-flash", "bulk_summarize",
                                now=NOW_OK)[0] is False


# ── request-body mutation (byte-identical chain guarantee) ──────────────────

def test_build_request_body_forces_promo_model_and_caps():
    body = {"model": "glm-4.5-flash", "max_tokens": 50000,
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.3}
    out = make_tier().build_request_body(body)
    assert out["model"] == "stealth/ox-alpha"
    assert out["reasoning_effort"] == "low"
    assert out["max_completion_tokens"] == 8192   # capped down from 50000
    assert "max_tokens" not in out
    assert out["messages"] == body["messages"]
    assert out["temperature"] == 0.3


def test_build_request_body_never_raises_low_caps():
    out = make_tier().build_request_body(
        {"model": "x", "max_completion_tokens": 400})
    assert out["max_completion_tokens"] == 400      # smaller asks respected
    out = make_tier().build_request_body({"model": "x"})
    assert out["max_completion_tokens"] == 8192     # default when absent


def test_build_request_body_returns_copy_original_untouched():
    body = {"model": "glm-4.5-flash", "messages": [{"role": "user",
                                                    "content": "hi"}]}
    snapshot = repr(body)
    make_tier().build_request_body(body)
    assert repr(body) == snapshot   # downstream chain sees original body


def test_ineligible_tier_never_mutates_anything():
    # with the tier disabled in ANY way, build_request_body is simply never
    # applied by the proxy — the eligibility gates above are the contract;
    # here: disabled tier reports not-eligible across every state we ship.
    for tier in (
        make_tier(api_key=""),
        make_tier(guard=make_guard(promo_end=NOW_OK - timedelta(seconds=1))),
    ):
        assert tier.failover_eligible(now=NOW_OK) is False


# ── 5-min usage-delta poller logic (kill on usage INCREASE) ─────────────────

def test_usage_delta_kill_on_increase_only():
    tier = make_tier()
    assert tier.decide_usage_kill(None, 0.0, now=NOW_OK) is None  # baseline
    assert tier.decide_usage_kill(0.0, 0.0, now=NOW_OK) is None    # flat
    ev = tier.decide_usage_kill(0.0, 0.018, now=NOW_OK)            # UP → kill
    assert ev is not None and ev["severity"] == "critical"
    assert tier.failover_eligible(now=NOW_OK) is False


def test_usage_poller_first_sample_is_baseline_not_kill():
    # fresh proxy restart with an already-nonzero cumulative usage must NOT
    # kill (baseline capture), only a later increase does
    tier = make_tier()
    assert tier.decide_usage_kill(None, 0.42, now=NOW_OK) is None
    assert tier.failover_eligible(now=NOW_OK) is True
    assert tier.decide_usage_kill(0.42, 0.43, now=NOW_OK) is not None


# ── anomaly-event drain (proxy inserts into anomaly_events) ─────────────────

def test_drain_anomaly_events_yields_guard_rows_once():
    tier = make_tier()
    tier.observe_response_cost(0.02, now=NOW_OK)
    tier.note_http_status(402, now=NOW_OK)  # independent detector: own row
    rows = tier.drain_anomaly_events()
    # each kill detector emits exactly ONE row (spend + 402)
    assert len(rows) == 2
    for row in rows:
        assert {"ts", "severity", "category", "title", "detail"} <= set(row)
    assert tier.drain_anomaly_events() == []   # drained exactly once
    tier.note_http_status(402, now=NOW_OK)     # repeat 402: no duplicate row
    assert tier.drain_anomaly_events() == []


# ── config integration ──────────────────────────────────────────────────────

def test_load_tier_from_config_builds_rung1_defaults():
    cfg = {
        "base_url": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_OXALPHA_KEY",
        "promo": {"expires_at": "2026-08-28T00:00:00Z"},
        "allowlist_task_types": ["vision", "bulk_summarize", "shadow_eval"],
        "preferred_for": {"enabled": False, "models": ["glm-4.5-flash"],
                          "task_types": ["bulk_summarize"]},
        "failover": {"enabled": True},
    }
    strategy = {"min_effective_price": 0.001}
    tier = ox.load_tier_from_config(cfg, strategy, api_key="sk-or-x")
    assert tier.configured and tier.failover_eligible(now=NOW_OK)
    assert tier.alias_eligible("glm-4.5-flash", "bulk_summarize",
                               now=NOW_OK)[0] is False  # preferred disabled
    assert tier.guard.allowed_task_types == frozenset(
        {"vision", "bulk_summarize", "shadow_eval"})


def test_load_tier_absent_failover_block_defaults_emergency_on():
    tier = ox.load_tier_from_config({}, {}, api_key="sk-or-x")
    assert tier.failover_enabled is True   # EMERGENCY default: bleed-stop on


def test_repo_fixture_config_parses_and_matches_descriptor():
    yaml = pytest.importorskip("yaml")
    cfg_all = yaml.safe_load((REPO / "config" / "providers.yaml").read_text())
    cfg = cfg_all.get("oxalpha") or {}
    tier = ox.load_tier_from_config(cfg, cfg_all.get("strategy") or {},
                                    api_key="sk-or-x")
    d = tier.descriptor()
    assert d["name"] == "oxalpha"
    assert d["base_url"] == cfg["base_url"]
    assert d["model"] == "stealth/ox-alpha"
    assert cfg["key_env"] == ox.KEY_ENV_NAME


# ── zai routing unchanged (regression vs git HEAD) ──────────────────────────

def test_model_map_additions_are_scoped_to_oxalpha_only():
    yaml = pytest.importorskip("yaml")
    head = yaml.safe_load(subprocess.run(
        ["git", "show", "HEAD:config/providers.yaml"],
        capture_output=True, text=True, check=True,
        cwd=REPO).stdout)
    work = yaml.safe_load((REPO / "config" / "providers.yaml").read_text())
    mm_head, mm_work = head["strategy"]["model_map"], work["strategy"]["model_map"]
    # zai cells byte-identical
    assert mm_work["zai"] == mm_head["zai"]
    # only additive oxalpha provider key
    assert set(mm_work) - set(mm_head) <= {"oxalpha"}
    # …and only the two sanctioned task-type cells
    assert set(mm_work.get("oxalpha", {})) <= {"vision", "bulk_summarize"}
    assert set(mm_work.get("oxalpha", {}).values()) == {"stealth/ox-alpha"}
