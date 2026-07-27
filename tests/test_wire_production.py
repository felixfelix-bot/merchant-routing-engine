"""Tests for wire_production.py — analysis logic (no real proxy modification).

Tests verify:
  1. Analysis of a mock proxy that matches the real proxy's structure
  2. Detection of missing vs present components
  3. Change computation accuracy
  4. Diff generation produces valid unified diff
  5. Apply creates backup + modifies content correctly
  6. Revert restores from backup
  7. Idempotency (analyzing an already-wired proxy reports no changes)
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from wire_production import (
    AnalysisResult,
    analyze_proxy,
    apply_changes,
    generate_diff,
    revert_changes,
)


# ═══════════════════════════════════════════════════════════════════════════
#  MOCK PROXY FIXTURE
# ═══════════════════════════════════════════════════════════════════════════

# A minimal mock of zai_proxy.py that mirrors the real proxy's structure:
# - ShadowHook import + init
# - _record_spend function
# - _shadow_hook.compare() call WITHOUT pace_windows/failure_counts
# - quota_cache
# - _snapshot_quota / _snapshot_health
# - _zai_key_health
MOCK_PROXY = '''#!/usr/bin/env python3
"""Mock proxy for testing wire_production.py."""
from __future__ import annotations
import os, sys, time
from pathlib import Path

# ── Shadow mode (Phase 2) ──────────────────────────────────────────────
_shadow_hook = None
try:
    _MRE_PATH = os.path.expanduser("~/merchant-routing-engine")
    if _MRE_PATH not in sys.path:
        sys.path.insert(0, _MRE_PATH)
    from src.shadow_hook import ShadowHook
    _shadow_hook = ShadowHook(db_path=os.path.expanduser("~/.hermes/bot/zai_usage.db"))
    print(f"[shadow] ShadowHook initialized", flush=True)
except Exception as _e:
    print(f"[shadow] DISABLED — {_e}", flush=True)
    _shadow_hook = None

# ── config ──────────────────────────────────────────────────────────────
KEYS = {"ours": "sk-test", "friend": "sk-test2"}
quota_cache: dict[str, tuple[list[dict], float]] = {}
_zai_key_health: dict[str, dict] = {}

def _snapshot_quota() -> dict:
    return {"ours": {"used_pct": 0.0, "remaining": 2000000, "total": 2000000}}

def _snapshot_health() -> dict:
    return {"ours": True, "friend": True}

def _record_spend(key_name: str | None, model: str | None, total_tokens: int,
                  actual_cost: float | None = None) -> None:
    pass

class Handler:
    def _proxy(self):
        try:
            key_used = "ours"
            model = "glm-5.2"
            peak = False
        finally:
            usage = {"total_tokens": 1000}
            if not getattr(self, '_spend_recorded', False):
                _record_spend(key_used, model, int(usage.get("total_tokens") or 0))
            if _shadow_hook is not None:
                try:
                    _shadow_hook.compare(
                        live_provider=key_used,
                        live_model=model,
                        tokens=int(usage.get("total_tokens") or 0),
                        quota_state=_snapshot_quota(),
                        health_state=_snapshot_health(),
                        peak=peak if 'peak' in dir() else False,
                    )
                except Exception:
                    pass
'''

# A mock proxy that already has all wiring in place
MOCK_PROXY_WIRED = '''#!/usr/bin/env python3
"""Mock proxy with full wiring."""
from __future__ import annotations
import os, sys, time
from pathlib import Path

_shadow_hook = None
try:
    _MRE_PATH = os.path.expanduser("~/merchant-routing-engine")
    if _MRE_PATH not in sys.path:
        sys.path.insert(0, _MRE_PATH)
    from src.shadow_hook import ShadowHook
    _shadow_hook = ShadowHook(db_path=os.path.expanduser("~/.hermes/bot/zai_usage.db"))
except Exception as _e:
    _shadow_hook = None

_cost_observer = None
try:
    from src.cost_observer import CostObserver as _CostObserver
    if _shadow_hook is not None:
        _cost_observer = _CostObserver(price_kalmans=_shadow_hook._price_kalmans, retry_penalty=0.01)
except Exception:
    _cost_observer = None

_burn_aggregator = None
try:
    from src.burn_rate_aggregator import BurnRateAggregator as _BurnRateAggregator
    _burn_aggregator = _BurnRateAggregator(window_minutes=5)
except Exception:
    _burn_aggregator = None

quota_cache: dict[str, tuple[list[dict], float]] = {}
_zai_key_health: dict[str, dict] = {}

def _snapshot_quota() -> dict:
    return {}

def _snapshot_health() -> dict:
    return {}

def _record_spend(key_name, model, total_tokens, actual_cost=None):
    pass

class Handler:
    def _proxy(self):
        try:
            key_used = "ours"
            model = "glm-5.2"
            peak = False
        finally:
            usage = {"total_tokens": 1000}
            if not getattr(self, '_spend_recorded', False):
                _record_spend(key_used, model, int(usage.get("total_tokens") or 0))
            if _cost_observer is not None and key_used:
                try:
                    _cost_observer.observe_success(provider=key_used, spend_usd=0.01, tokens=1000)
                except Exception:
                    pass
            if _burn_aggregator is not None and key_used:
                try:
                    _burn_aggregator.record(key_used, int(usage.get("total_tokens") or 0))
                    if _shadow_hook is not None:
                        _burn_aggregator.maybe_feed(_shadow_hook._consumption_kalmans)
                except Exception:
                    pass
            if _shadow_hook is not None:
                try:
                    _failure_counts = {}
                    for _kname in ("ours", "friend"):
                        _kh = _zai_key_health.get(_kname, {})
                        _failure_counts[_kname] = int(_kh.get("consecutive_failures", 0))
                    _pace_windows = {}
                    from src.quota_window_extractor import extract_quota_windows
                    _shadow_hook.compare(
                        live_provider=key_used,
                        live_model=model,
                        tokens=int(usage.get("total_tokens") or 0),
                        quota_state=_snapshot_quota(),
                        health_state=_snapshot_health(),
                        peak=peak if 'peak' in dir() else False,
                        failure_counts=_failure_counts,
                        pace_windows=_pace_windows,
                    )
                except Exception:
                    pass
'''


@pytest.fixture
def mock_proxy(tmp_path: Path) -> Path:
    """Create a mock proxy file matching the real proxy structure."""
    p = tmp_path / "zai_proxy.py"
    p.write_text(MOCK_PROXY)
    return p


@pytest.fixture
def mock_proxy_wired(tmp_path: Path) -> Path:
    """Create a mock proxy that's already fully wired."""
    p = tmp_path / "zai_proxy.py"
    p.write_text(MOCK_PROXY_WIRED)
    return p


@pytest.fixture
def mock_proxy_no_file(tmp_path: Path) -> Path:
    """Return a path to a non-existent proxy file."""
    return tmp_path / "nonexistent.py"


# ═══════════════════════════════════════════════════════════════════════════
#  ANALYSIS TESTS
# ═══════════════════════════════════════════════════════════════════════════

class TestAnalyzeProxy:
    """Tests for analyze_proxy()."""

    def test_analyze_finds_shadow_hook_import(self, mock_proxy: Path):
        """ShadowHook import should be detected."""
        result = analyze_proxy(mock_proxy)
        assert result.proxy_exists is True
        assert result.has_shadow_hook_import is True

    def test_analyze_finds_shadow_hook_init(self, mock_proxy: Path):
        """ShadowHook initialization should be detected."""
        result = analyze_proxy(mock_proxy)
        assert result.has_shadow_hook_init is True

    def test_analyze_finds_shadow_hook_compare_call(self, mock_proxy: Path):
        """_shadow_hook.compare() call should be detected."""
        result = analyze_proxy(mock_proxy)
        assert result.has_shadow_hook_compare_call is True
        assert result.shadow_hook_compare_line >= 0

    def test_analyze_finds_record_spend(self, mock_proxy: Path):
        """_record_spend function should be detected."""
        result = analyze_proxy(mock_proxy)
        assert result.has_record_spend is True
        assert result.record_spend_line >= 0

    def test_analyze_detects_missing_cost_observer(self, mock_proxy: Path):
        """CostObserver should be MISSING in the unwired proxy."""
        result = analyze_proxy(mock_proxy)
        assert result.has_cost_observer is False

    def test_analyze_detects_missing_burn_rate_aggregator(self, mock_proxy: Path):
        """BurnRateAggregator should be MISSING in the unwired proxy."""
        result = analyze_proxy(mock_proxy)
        assert result.has_burn_rate_aggregator is False

    def test_analyze_detects_missing_pace_windows(self, mock_proxy: Path):
        """pace_windows should be MISSING from compare() call."""
        result = analyze_proxy(mock_proxy)
        assert result.has_pace_windows_in_compare is False

    def test_analyze_detects_missing_failure_counts(self, mock_proxy: Path):
        """failure_counts should be MISSING from compare() call."""
        result = analyze_proxy(mock_proxy)
        assert result.has_failure_counts_in_compare is False

    def test_analyze_finds_quota_cache(self, mock_proxy: Path):
        """quota_cache should be detected."""
        result = analyze_proxy(mock_proxy)
        assert result.has_quota_cache is True

    def test_analyze_finds_snapshot_functions(self, mock_proxy: Path):
        """_snapshot_quota and _snapshot_health should be detected."""
        result = analyze_proxy(mock_proxy)
        assert result.has_snapshot_quota is True
        assert result.has_snapshot_health is True

    def test_analyze_finds_zai_key_health(self, mock_proxy: Path):
        """_zai_key_health should be detected."""
        result = analyze_proxy(mock_proxy)
        assert result.has_zai_key_health is True

    def test_analyze_detects_missing_primary_router(self, mock_proxy: Path):
        """PrimaryRouter should be MISSING (Phase 3 not deployed)."""
        result = analyze_proxy(mock_proxy)
        assert result.has_primary_router is False

    def test_analyze_nonexistent_file(self, mock_proxy_no_file: Path):
        """Analysis of a non-existent file should return exists=False."""
        result = analyze_proxy(mock_proxy_no_file)
        assert result.proxy_exists is False
        assert len(result.proxy_lines) == 0

    def test_analyze_not_wired_status(self, mock_proxy: Path):
        """Unwired proxy should report NOT WIRED."""
        result = analyze_proxy(mock_proxy)
        assert result.is_wired is False
        assert result.is_partially_wired is False

    def test_analyze_wired_status(self, mock_proxy_wired: Path):
        """Fully wired proxy should report FULLY WIRED."""
        result = analyze_proxy(mock_proxy_wired)
        assert result.is_wired is True
        assert result.has_cost_observer is True
        assert result.has_burn_rate_aggregator is True
        assert result.has_pace_windows_in_compare is True
        assert result.has_failure_counts_in_compare is True


# ═══════════════════════════════════════════════════════════════════════════
#  CHANGES NEEDED TESTS
# ═══════════════════════════════════════════════════════════════════════════

class TestChangesNeeded:
    """Tests for the change computation logic."""

    def test_unwired_proxy_needs_changes(self, mock_proxy: Path):
        """An unwired proxy should need multiple changes."""
        result = analyze_proxy(mock_proxy)
        assert len(result.changes_needed) >= 5

    def test_wired_proxy_needs_no_changes(self, mock_proxy_wired: Path):
        """A fully wired proxy should need zero changes (except commented Phase 3)."""
        result = analyze_proxy(mock_proxy_wired)
        # The only change should be the commented-out PrimaryRouter (Phase 3 not deployed)
        non_commented = [c for c in result.changes_needed if not c.get("commented_out")]
        assert len(non_commented) == 0

    def test_changes_include_cost_observer_import(self, mock_proxy: Path):
        """Changes should include CostObserver import+init."""
        result = analyze_proxy(mock_proxy)
        ids = [c["id"] for c in result.changes_needed]
        assert "cost_observer_import" in ids

    def test_changes_include_burn_rate_aggregator_import(self, mock_proxy: Path):
        """Changes should include BurnRateAggregator import+init."""
        result = analyze_proxy(mock_proxy)
        ids = [c["id"] for c in result.changes_needed]
        assert "burn_rate_aggregator_import" in ids

    def test_changes_include_cost_observer_calls(self, mock_proxy: Path):
        """Changes should include call-site insertion for CostObserver."""
        result = analyze_proxy(mock_proxy)
        ids = [c["id"] for c in result.changes_needed]
        assert "cost_observer_calls" in ids

    def test_changes_include_pace_windows(self, mock_proxy: Path):
        """Changes should include pace_windows argument addition."""
        result = analyze_proxy(mock_proxy)
        ids = [c["id"] for c in result.changes_needed]
        assert "pace_windows_in_compare" in ids

    def test_changes_include_failure_counts(self, mock_proxy: Path):
        """Changes should include failure_counts argument addition."""
        result = analyze_proxy(mock_proxy)
        ids = [c["id"] for c in result.changes_needed]
        assert "failure_counts_in_compare" in ids

    def test_changes_include_compare_call_args(self, mock_proxy: Path):
        """Changes should include compare() call modification."""
        result = analyze_proxy(mock_proxy)
        ids = [c["id"] for c in result.changes_needed]
        assert "compare_call_args" in ids

    def test_changes_include_primary_router_burn_rate(self, mock_proxy: Path):
        """Changes should include PrimaryRouter burn rate (commented out)."""
        result = analyze_proxy(mock_proxy)
        ids = [c["id"] for c in result.changes_needed]
        assert "primary_router_burn_rate" in ids
        # Should be flagged as commented out
        pr_change = [c for c in result.changes_needed if c["id"] == "primary_router_burn_rate"][0]
        assert pr_change.get("commented_out") is True

    def test_each_change_has_required_fields(self, mock_proxy: Path):
        """Every change should have id, component, type, and description."""
        result = analyze_proxy(mock_proxy)
        for change in result.changes_needed:
            assert "id" in change
            assert "component" in change
            assert "type" in change
            assert "description" in change
            assert isinstance(change["description"], str)
            assert len(change["description"]) > 10


# ═══════════════════════════════════════════════════════════════════════════
#  DIFF GENERATION TESTS
# ═══════════════════════════════════════════════════════════════════════════

class TestDiffGeneration:
    """Tests for generate_diff()."""

    def test_diff_is_non_empty_for_unwired(self, mock_proxy: Path):
        """Diff should be non-empty for an unwired proxy."""
        result = analyze_proxy(mock_proxy)
        diff = generate_diff(result)
        assert diff != ""
        assert len(diff) > 0

    def test_diff_is_empty_for_wired(self, mock_proxy_wired: Path):
        """Diff should be empty for a fully wired proxy."""
        result = analyze_proxy(mock_proxy_wired)
        diff = generate_diff(result)
        assert diff == ""

    def test_diff_contains_unified_format(self, mock_proxy: Path):
        """Diff should contain unified diff markers."""
        result = analyze_proxy(mock_proxy)
        diff = generate_diff(result)
        assert "---" in diff
        assert "+++" in diff
        assert "@@" in diff

    def test_diff_mentions_cost_observer(self, mock_proxy: Path):
        """Diff should mention CostObserver in added lines."""
        result = analyze_proxy(mock_proxy)
        diff = generate_diff(result)
        assert "CostObserver" in diff or "cost_observer" in diff

    def test_diff_mentions_burn_aggregator(self, mock_proxy: Path):
        """Diff should mention BurnRateAggregator in added lines."""
        result = analyze_proxy(mock_proxy)
        diff = generate_diff(result)
        assert "BurnRateAggregator" in diff or "burn_aggregator" in diff

    def test_diff_mentions_pace_windows(self, mock_proxy: Path):
        """Diff should mention pace_windows in added lines."""
        result = analyze_proxy(mock_proxy)
        diff = generate_diff(result)
        assert "pace_windows" in diff

    def test_diff_mentions_failure_counts(self, mock_proxy: Path):
        """Diff should mention failure_counts in added lines."""
        result = analyze_proxy(mock_proxy)
        diff = generate_diff(result)
        assert "failure_counts" in diff

    def test_diff_mentions_quota_window_extractor(self, mock_proxy: Path):
        """Diff should mention quota_window_extractor."""
        result = analyze_proxy(mock_proxy)
        diff = generate_diff(result)
        assert "quota_window_extractor" in diff or "extract_quota_windows" in diff

    def test_diff_does_not_include_commented_phase3(self, mock_proxy: Path):
        """Diff should NOT include commented-out Phase 3 changes."""
        result = analyze_proxy(mock_proxy)
        diff = generate_diff(result)
        # The commented-out primary_router section should not appear in the diff
        # (it's skipped by generate_diff)
        assert "_primary_router" not in diff


# ═══════════════════════════════════════════════════════════════════════════
#  APPLY / REVERT TESTS
# ═══════════════════════════════════════════════════════════════════════════

class TestApplyChanges:
    """Tests for apply_changes() with mock files."""

    def test_apply_creates_backup(self, mock_proxy: Path):
        """apply_changes() should create a .bak-pre-wire backup."""
        result = analyze_proxy(mock_proxy)
        original = mock_proxy.read_text()
        apply_changes(result, proxy_path=mock_proxy)
        backup = mock_proxy.with_suffix(".py.bak-pre-wire")
        assert backup.exists()
        assert backup.read_text() == original

    def test_apply_modifies_proxy(self, mock_proxy: Path):
        """apply_changes() should modify the proxy file."""
        result = analyze_proxy(mock_proxy)
        original = mock_proxy.read_text()
        apply_changes(result, proxy_path=mock_proxy)
        modified = mock_proxy.read_text()
        assert modified != original
        assert "CostObserver" in modified
        assert "BurnRateAggregator" in modified

    def test_apply_adds_pace_windows_to_compare(self, mock_proxy: Path):
        """Applied changes should include pace_windows in compare() call."""
        result = analyze_proxy(mock_proxy)
        apply_changes(result, proxy_path=mock_proxy)
        modified = mock_proxy.read_text()
        assert "pace_windows=" in modified
        assert "pace_windows" in modified

    def test_apply_adds_failure_counts_to_compare(self, mock_proxy: Path):
        """Applied changes should include failure_counts in compare() call."""
        result = analyze_proxy(mock_proxy)
        apply_changes(result, proxy_path=mock_proxy)
        modified = mock_proxy.read_text()
        assert "failure_counts=" in modified

    def test_apply_on_wired_proxy_is_noop(self, mock_proxy_wired: Path):
        """Applying to an already-wired proxy should be a no-op."""
        result = analyze_proxy(mock_proxy_wired)
        original = mock_proxy_wired.read_text()
        applied = apply_changes(result, proxy_path=mock_proxy_wired)
        assert applied is False
        assert mock_proxy_wired.read_text() == original

    def test_apply_adds_quota_window_extractor_import(self, mock_proxy: Path):
        """Applied changes should import quota_window_extractor."""
        result = analyze_proxy(mock_proxy)
        apply_changes(result, proxy_path=mock_proxy)
        modified = mock_proxy.read_text()
        assert "extract_quota_windows" in modified


class TestRevertChanges:
    """Tests for revert_changes()."""

    def test_revert_restores_original(self, mock_proxy: Path):
        """revert_changes() should restore the original file."""
        original = mock_proxy.read_text()
        result = analyze_proxy(mock_proxy)
        apply_changes(result, proxy_path=mock_proxy)
        # Verify file was modified
        assert mock_proxy.read_text() != original
        # Revert
        reverted = revert_changes(proxy_path=mock_proxy)
        assert reverted is True
        assert mock_proxy.read_text() == original

    def test_revert_without_backup_returns_false(self, mock_proxy: Path):
        """revert_changes() without a backup should return False."""
        # No backup exists yet
        reverted = revert_changes(proxy_path=mock_proxy)
        assert reverted is False

    def test_revert_after_apply_works(self, mock_proxy: Path):
        """Full apply → revert cycle should restore exactly."""
        original = mock_proxy.read_text()
        result = analyze_proxy(mock_proxy)
        apply_changes(result, proxy_path=mock_proxy)
        revert_changes(proxy_path=mock_proxy)
        assert mock_proxy.read_text() == original


# ═══════════════════════════════════════════════════════════════════════════
#  INTEGRATION / EDGE CASES
# ═══════════════════════════════════════════════════════════════════════════

class TestEdgeCases:
    """Edge case tests."""

    def test_empty_file(self, tmp_path: Path):
        """An empty proxy file should be handled gracefully."""
        p = tmp_path / "empty.py"
        p.write_text("")
        result = analyze_proxy(p)
        assert result.proxy_exists is True
        # splitlines on empty string produces []
        assert len(result.proxy_lines) == 0
        # Should not crash
        diff = generate_diff(result)
        # An empty file has no anchor points so diff may be minimal
        assert isinstance(diff, str)

    def test_proxy_with_only_shadow_hook(self, tmp_path: Path):
        """Proxy with only ShadowHook should need most wiring."""
        content = '''#!/usr/bin/env python3
import os, sys
_shadow_hook = None
try:
    from src.shadow_hook import ShadowHook
    _shadow_hook = ShadowHook()
except Exception:
    _shadow_hook = None

quota_cache = {}
_zai_key_health = {}

def _snapshot_quota():
    return {}
def _snapshot_health():
    return {}

def _record_spend(key_name, model, tokens, actual_cost=None):
    pass

class H:
    def _proxy(self):
        try:
            key_used = "ours"
            model = "glm-5.2"
            peak = False
        finally:
            usage = {"total_tokens": 1000}
            if _shadow_hook is not None:
                try:
                    _shadow_hook.compare(
                        live_provider=key_used,
                        live_model=model,
                        tokens=1000,
                        quota_state=_snapshot_quota(),
                        health_state=_snapshot_health(),
                        peak=peak if 'peak' in dir() else False,
                    )
                except Exception:
                    pass
'''
        p = tmp_path / "proxy.py"
        p.write_text(content)
        result = analyze_proxy(p)
        assert result.has_shadow_hook_import is True
        assert result.has_cost_observer is False
        assert result.has_burn_rate_aggregator is False
        assert len(result.changes_needed) >= 5

    def test_partially_wired_proxy(self, tmp_path: Path):
        """Proxy with CostObserver but not BurnRateAggregator should be partial."""
        content = '''#!/usr/bin/env python3
import os, sys
_shadow_hook = None
try:
    from src.shadow_hook import ShadowHook
    _shadow_hook = ShadowHook()
except Exception:
    _shadow_hook = None

_cost_observer = None
try:
    from src.cost_observer import CostObserver as _CostObserver
    if _shadow_hook is not None:
        _cost_observer = _CostObserver(price_kalmans=_shadow_hook._price_kalmans)
except Exception:
    _cost_observer = None

quota_cache = {}
_zai_key_health = {}

def _snapshot_quota():
    return {}
def _snapshot_health():
    return {}
def _record_spend(k, m, t, a=None):
    pass

class H:
    def _proxy(self):
        try:
            key_used = "ours"
            model = "glm-5.2"
            peak = False
        finally:
            usage = {"total_tokens": 1000}
            if _shadow_hook is not None:
                try:
                    _shadow_hook.compare(
                        live_provider=key_used,
                        live_model=model,
                        tokens=1000,
                        quota_state=_snapshot_quota(),
                        health_state=_snapshot_health(),
                        peak=peak if 'peak' in dir() else False,
                    )
                except Exception:
                    pass
'''
        p = tmp_path / "partial.py"
        p.write_text(content)
        result = analyze_proxy(p)
        assert result.has_cost_observer is True
        assert result.has_burn_rate_aggregator is False
        assert result.is_wired is False
        assert result.is_partially_wired is True


# ═══════════════════════════════════════════════════════════════════════════
#  ANALYSIS RESULT PROPERTIES
# ═══════════════════════════════════════════════════════════════════════════

class TestAnalysisResult:
    """Tests for AnalysisResult properties."""

    def test_is_wired_all_true(self):
        """is_wired should be True when all 4 components are present."""
        r = AnalysisResult()
        r.has_cost_observer = True
        r.has_burn_rate_aggregator = True
        r.has_pace_windows_in_compare = True
        r.has_failure_counts_in_compare = True
        assert r.is_wired is True

    def test_is_wired_any_false(self):
        """is_wired should be False when any component is missing."""
        for missing in ["has_cost_observer", "has_burn_rate_aggregator",
                        "has_pace_windows_in_compare", "has_failure_counts_in_compare"]:
            r = AnalysisResult()
            r.has_cost_observer = True
            r.has_burn_rate_aggregator = True
            r.has_pace_windows_in_compare = True
            r.has_failure_counts_in_compare = True
            setattr(r, missing, False)
            assert r.is_wired is False

    def test_is_partially_wired(self):
        """is_partially_wired should be True for 1-3 components present."""
        r = AnalysisResult()
        r.has_cost_observer = True
        r.has_burn_rate_aggregator = False
        r.has_pace_windows_in_compare = False
        r.has_failure_counts_in_compare = False
        assert r.is_partially_wired is True
        assert r.is_wired is False

    def test_not_wired_and_not_partial(self):
        """Zero components should be neither wired nor partial."""
        r = AnalysisResult()
        r.has_cost_observer = False
        r.has_burn_rate_aggregator = False
        r.has_pace_windows_in_compare = False
        r.has_failure_counts_in_compare = False
        assert r.is_wired is False
        assert r.is_partially_wired is False