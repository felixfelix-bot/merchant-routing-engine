#!/usr/bin/env python3
"""wire_production.py — Analyze and optionally apply wiring changes to zai_proxy.py.

This script analyzes the production proxy at ~/.hermes/bot/zai_proxy.py and
shows the changes needed to wire:

  a. CostObserver — import + initialize + call observe_success/observe_failure
  b. BurnRateAggregator — import + initialize + call record() per request +
     maybe_feed() every 5 min
  c. pace_windows — pass to _shadow_hook.compare() via quota_window_extractor
  d. failure_counts — pass to _shadow_hook.compare() from _zai_key_health
  e. update_burn_rate on PrimaryRouter (if/when deployed)

Modes:
  python scripts/wire_production.py             # Dry-run: print analysis + diff
  python scripts/wire_production.py --apply      # Apply changes (backup first)
  python scripts/wire_production.py --revert      # Restore from backup

Safety:
  --apply always creates a backup at zai_proxy.py.bak-pre-wire first.
  --revert restores from that backup.
  Without flags, the script is READ-ONLY and just prints analysis.
"""
from __future__ import annotations

import difflib
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

# ── Paths ──────────────────────────────────────────────────────────────────
PROXY_PATH = Path.home() / ".hermes" / "bot" / "zai_proxy.py"
BACKUP_PATH = Path.home() / ".hermes" / "bot" / "zai_proxy.py.bak-pre-wire"
REPO_PATH = Path.home() / "merchant-routing-engine"


# ═══════════════════════════════════════════════════════════════════════════
#  ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════

class AnalysisResult:
    """Structured result of analyzing zai_proxy.py for wiring readiness."""

    def __init__(self) -> None:
        self.proxy_path: Path = PROXY_PATH
        self.proxy_exists: bool = False
        self.proxy_lines: list[str] = []
        self.proxy_content: str = ""

        # Current state checks
        self.has_shadow_hook_import: bool = False
        self.has_shadow_hook_init: bool = False
        self.has_shadow_hook_compare_call: bool = False
        self.has_record_spend: bool = False
        self.has_cost_observer: bool = False
        self.has_burn_rate_aggregator: bool = False
        self.has_primary_router: bool = False
        self.has_update_burn_rate: bool = False
        self.has_pace_windows_in_compare: bool = False
        self.has_failure_counts_in_compare: bool = False
        self.has_quota_cache: bool = False
        self.has_snapshot_quota: bool = False
        self.has_snapshot_health: bool = False
        self.has_zai_key_health: bool = False

        # Hook point line numbers (0-indexed)
        self.shadow_hook_compare_line: int = -1
        self.record_spend_line: int = -1
        self.shadow_hook_init_block: tuple[int, int] = (-1, -1)  # start, end

        # Change sets
        self.changes_needed: list[dict[str, Any]] = []

    @property
    def is_wired(self) -> bool:
        """True if all wiring is already in place."""
        return (
            self.has_cost_observer
            and self.has_burn_rate_aggregator
            and self.has_pace_windows_in_compare
            and self.has_failure_counts_in_compare
        )

    @property
    def is_partially_wired(self) -> bool:
        """True if some but not all wiring is in place."""
        wired = sum([
            self.has_cost_observer,
            self.has_burn_rate_aggregator,
            self.has_pace_windows_in_compare,
            self.has_failure_counts_in_compare,
        ])
        return 0 < wired < 4


def analyze_proxy(proxy_path: Path | None = None) -> AnalysisResult:
    """Analyze zai_proxy.py and return a structured result.

    Args:
        proxy_path: Override the default proxy path (for testing).

    Returns:
        AnalysisResult with all checks populated.
    """
    if proxy_path is None:
        proxy_path = PROXY_PATH

    result = AnalysisResult()
    result.proxy_path = proxy_path
    result.proxy_exists = proxy_path.exists()

    if not result.proxy_exists:
        return result

    result.proxy_content = proxy_path.read_text(errors="replace")
    result.proxy_lines = result.proxy_content.splitlines(keepends=True)

    content = result.proxy_content

    # ── Check for existing wiring ───────────────────────────────────────
    result.has_shadow_hook_import = "from src.shadow_hook import ShadowHook" in content
    result.has_shadow_hook_init = "_shadow_hook = ShadowHook(" in content
    result.has_shadow_hook_compare_call = "_shadow_hook.compare(" in content
    result.has_record_spend = "def _record_spend(" in content
    result.has_cost_observer = "CostObserver" in content
    result.has_burn_rate_aggregator = "BurnRateAggregator" in content
    result.has_primary_router = "PrimaryRouter" in content
    result.has_update_burn_rate = "update_burn_rate" in content
    result.has_pace_windows_in_compare = "pace_windows" in content and "compare" in content
    result.has_failure_counts_in_compare = "failure_counts" in content and "compare" in content
    result.has_quota_cache = "quota_cache" in content
    result.has_snapshot_quota = "def _snapshot_quota" in content
    result.has_snapshot_health = "def _snapshot_health" in content
    result.has_zai_key_health = "_zai_key_health" in content

    # ── Locate hook points ──────────────────────────────────────────────
    for i, line in enumerate(result.proxy_lines):
        if "_shadow_hook.compare(" in line and not line.strip().startswith("#"):
            result.shadow_hook_compare_line = i
        if "def _record_spend(" in line:
            result.record_spend_line = i
        if "_shadow_hook = ShadowHook(" in line:
            # Find the end of the init block (the except/pass block)
            result.shadow_hook_init_block = (i, i)
            for j in range(i, min(i + 20, len(result.proxy_lines))):
                if result.proxy_lines[j].strip().startswith("except") or \
                   result.proxy_lines[j].strip() == "_shadow_hook = None":
                    result.shadow_hook_init_block = (i, j + 1)
                    break

    # ── Determine changes needed ────────────────────────────────────────
    result.changes_needed = _compute_changes_needed(result)

    return result


def _compute_changes_needed(result: AnalysisResult) -> list[dict[str, Any]]:
    """Compute the list of changes needed to fully wire the proxy."""
    changes: list[dict[str, Any]] = []

    # ── Change 1: Import + initialize CostObserver ──────────────────────
    if not result.has_cost_observer:
        changes.append({
            "id": "cost_observer_import",
            "component": "CostObserver",
            "type": "import + init",
            "description": (
                "Import CostObserver from src.cost_observer and initialize it "
                "with _shadow_hook._price_kalmans (shared Kalman state)."
            ),
            "anchor": "shadow_hook init block (lines ~29-39)",
            "code": (
                "# ── CostObserver (ADR-008) — feeds real cost observations ───────────\n"
                "_cost_observer = None\n"
                "try:\n"
                "    from src.cost_observer import CostObserver as _CostObserver\n"
                "    if _shadow_hook is not None:\n"
                "        _cost_observer = _CostObserver(\n"
                "            price_kalmans=_shadow_hook._price_kalmans,\n"
                "            retry_penalty=0.01,\n"
                "        )\n"
                "    print(f\"[cost] CostObserver initialized\", flush=True)\n"
                "except Exception as _e:\n"
                "    print(f\"[cost] DISABLED — {_e}\", flush=True)\n"
                "    _cost_observer = None"
            ),
            "insert_after": result.shadow_hook_init_block[1],
        })

    # ── Change 2: Import + initialize BurnRateAggregator ───────────────
    if not result.has_burn_rate_aggregator:
        changes.append({
            "id": "burn_rate_aggregator_import",
            "component": "BurnRateAggregator",
            "type": "import + init",
            "description": (
                "Import BurnRateAggregator from src.burn_rate_aggregator and "
                "initialize it for 5-minute windowed token aggregation."
            ),
            "anchor": "after CostObserver init block",
            "code": (
                "# ── BurnRateAggregator (ADR-008) — 5-min windowed burn rates ─────\n"
                "_burn_aggregator = None\n"
                "try:\n"
                "    from src.burn_rate_aggregator import BurnRateAggregator as _BurnRateAggregator\n"
                "    _burn_aggregator = _BurnRateAggregator(window_minutes=5)\n"
                "    print(f\"[burn] BurnRateAggregator initialized (5-min windows)\", flush=True)\n"
                "except Exception as _e:\n"
                "    print(f\"[burn] DISABLED — {_e}\", flush=True)\n"
                "    _burn_aggregator = None"
            ),
            "insert_after": result.shadow_hook_init_block[1] + (10 if not result.has_cost_observer else 0),
        })

    # ── Change 3: Call CostObserver + BurnRateAggregator after requests ─
    if result.has_record_spend and not result.has_cost_observer:
        changes.append({
            "id": "cost_observer_calls",
            "component": "CostObserver + BurnRateAggregator",
            "type": "call-site insertion",
            "description": (
                "After _record_spend in the finally block, add calls to:\n"
                "  - _cost_observer.observe_success(provider, spend_usd, tokens) on success\n"
                "  - _burn_aggregator.record(provider, tokens) on every request\n"
                "  - _burn_aggregator.maybe_feed(kalmans) every 5 min"
            ),
            "anchor": f"_record_spend call at line ~{result.record_spend_line + 1}",
            "code": (
                "            # ── CostObserver + BurnRateAggregator (ADR-008) ────────\n"
                "            if _cost_observer is not None and key_used:\n"
                "                try:\n"
                "                    _cost_cost = _estimate_cost_usd(key_used, int(usage.get(\"total_tokens\") or 0))\n"
                "                    _cost_observer.observe_success(\n"
                "                        provider=key_used,\n"
                "                        spend_usd=_cost_cost,\n"
                "                        tokens=int(usage.get(\"total_tokens\") or 0),\n"
                "                    )\n"
                "                except Exception:\n"
                "                    pass\n"
                "            if _burn_aggregator is not None and key_used:\n"
                "                try:\n"
                "                    _burn_aggregator.record(key_used, int(usage.get(\"total_tokens\") or 0))\n"
                "                    if _shadow_hook is not None:\n"
                "                        _burn_aggregator.maybe_feed(_shadow_hook._consumption_kalmans)\n"
                "                except Exception:\n"
                "                    pass"
            ),
            "insert_after": result.record_spend_line,
        })

    # ── Change 4: Pass pace_windows to _shadow_hook.compare() ───────────
    if not result.has_pace_windows_in_compare:
        changes.append({
            "id": "pace_windows_in_compare",
            "component": "pace_factor",
            "type": "argument addition",
            "description": (
                "Extract pace_windows from quota_cache using "
                "quota_window_extractor.extract_quota_windows() and pass them "
                "to _shadow_hook.compare()."
            ),
            "anchor": f"_shadow_hook.compare() call at line ~{result.shadow_hook_compare_line + 1}",
            "code": (
                "                    # ── Build pace_windows from quota_cache (ADR-008) ──\n"
                "                    _pace_windows = {}\n"
                "                    try:\n"
                "                        from src.quota_window_extractor import extract_quota_windows\n"
                "                        for _kname in (\"ours\", \"friend\"):\n"
                "                            _cache_entry = quota_cache.get(_kname)\n"
                "                            if _cache_entry:\n"
                "                                _burn_rate = 0.0\n"
                "                                if _shadow_hook and _kname in _shadow_hook._consumption_kalmans:\n"
                "                                    _burn_rate = _shadow_hook._consumption_kalmans[_kname].burn_rate\n"
                "                                _tuples = extract_quota_windows(\n"
                "                                    quota_cache={_kname: _cache_entry},\n"
                "                                    burn_rate=_burn_rate,\n"
                "                                )\n"
                "                                if _tuples:\n"
                "                                    _pace_windows[_kname] = _tuples\n"
                "                    except Exception:\n"
                "                        _pace_windows = {}"
            ),
            "insert_before": result.shadow_hook_compare_line,
        })

    # ── Change 5: Pass failure_counts to _shadow_hook.compare() ─────────
    if not result.has_failure_counts_in_compare:
        changes.append({
            "id": "failure_counts_in_compare",
            "component": "failure_counts",
            "type": "argument addition",
            "description": (
                "Build a failure_counts dict from _zai_key_health and pass it "
                "to _shadow_hook.compare()."
            ),
            "anchor": f"_shadow_hook.compare() call at line ~{result.shadow_hook_compare_line + 1}",
            "code": (
                "                    # ── Build failure_counts from _zai_key_health ────\n"
                "                    _failure_counts = {}\n"
                "                    try:\n"
                "                        for _kname in (\"ours\", \"friend\"):\n"
                "                            _kh = _zai_key_health.get(_kname, {})\n"
                "                            _failure_counts[_kname] = int(_kh.get(\"consecutive_failures\", 0))\n"
                "                    except Exception:\n"
                "                        _failure_counts = {}"
            ),
            "insert_before": result.shadow_hook_compare_line,
        })

    # ── Change 6: Update compare() call to pass new args ────────────────
    if not result.has_pace_windows_in_compare or not result.has_failure_counts_in_compare:
        changes.append({
            "id": "compare_call_args",
            "component": "compare() signature",
            "type": "call-site modification",
            "description": (
                "Add failure_counts= and pace_windows= kwargs to the existing "
                "_shadow_hook.compare() call."
            ),
            "anchor": f"_shadow_hook.compare() call at line ~{result.shadow_hook_compare_line + 1}",
            "old_code": (
                "                    _shadow_hook.compare(\n"
                "                        live_provider=key_used,\n"
                "                        live_model=model,\n"
                "                        tokens=int(usage.get(\"total_tokens\") or 0),\n"
                "                        quota_state=_snapshot_quota(),\n"
                "                        health_state=_snapshot_health(),\n"
                "                        peak=peak if 'peak' in dir() else False,\n"
                "                    )"
            ),
            "new_code": (
                "                    _shadow_hook.compare(\n"
                "                        live_provider=key_used,\n"
                "                        live_model=model,\n"
                "                        tokens=int(usage.get(\"total_tokens\") or 0),\n"
                "                        quota_state=_snapshot_quota(),\n"
                "                        health_state=_snapshot_health(),\n"
                "                        peak=peak if 'peak' in dir() else False,\n"
                "                        failure_counts=_failure_counts,\n"
                "                        pace_windows=_pace_windows,\n"
                "                    )"
            ),
        })

    # ── Change 7: update_burn_rate on PrimaryRouter (if deployed) ───────
    if not result.has_update_burn_rate:
        changes.append({
            "id": "primary_router_burn_rate",
            "component": "PrimaryRouter",
            "type": "conditional (Phase 3 not yet deployed)",
            "description": (
                "When PrimaryRouter is deployed (Phase 3), call "
                "update_burn_rate() after each request to keep "
                "ConsumptionKalman burn-rate predictions current. "
                "Currently the proxy does NOT import PrimaryRouter — "
                "this is a no-op until Phase 3 deployment."
            ),
            "anchor": "after BurnRateAggregator.record() call",
            "code": (
                "            # ── Phase 3: feed burn rate to PrimaryRouter (if deployed) ──\n"
                "            # _primary_router = None  # set during Phase 3 deploy\n"
                "            # if _primary_router is not None and key_used:\n"
                "            #     try:\n"
                "            #         _primary_router.update_burn_rate(\n"
                "            #             provider=key_used,\n"
                "            #             tokens=int(usage.get(\"total_tokens\") or 0),\n"
                "            #         )\n"
                "            #     except Exception:\n"
                "            #         pass"
            ),
            "insert_after": result.record_spend_line,
            "commented_out": True,  # Phase 3 not deployed yet
        })

    return changes


# ═══════════════════════════════════════════════════════════════════════════
#  DIFF GENERATION
# ═══════════════════════════════════════════════════════════════════════════

def generate_diff(result: AnalysisResult) -> str:
    """Generate a unified diff of the proposed changes.

    Returns:
        Unified diff string. Empty if no changes needed.
    """
    if not result.changes_needed:
        return ""

    original_lines = result.proxy_lines.copy()
    modified_lines = result.proxy_lines.copy()

    # Apply changes in reverse order (bottom-up) so line numbers stay valid
    insertions: list[tuple[int, list[str]]] = []
    replacements: list[tuple[int, int, list[str]]] = []

    for change in result.changes_needed:
        if change.get("commented_out"):
            continue  # Skip commented-out Phase 3 changes for diff

        if change["type"] == "call-site modification":
            old_code = change["old_code"]
            new_code = change["new_code"]
            old_lines = old_code.splitlines(keepends=True)
            new_lines = new_code.splitlines(keepends=True)

            # Find the old code block in the proxy
            old_text = "".join(old_lines)
            proxy_text = "".join(modified_lines)
            start = proxy_text.find(old_text)
            if start >= 0:
                # Convert char offset to line offset
                line_start = proxy_text[:start].count("\n")
                line_end = line_start + len(old_lines)
                replacements.append((line_start, line_end, new_lines))

        elif "insert_after" in change:
            insert_at = change["insert_after"]
            code_lines = change["code"].splitlines(keepends=True)
            # Add a blank line separator before
            code_lines = ["\n"] + code_lines + ["\n"]
            insertions.append((insert_at, code_lines))

        elif "insert_before" in change:
            insert_at = change["insert_before"]
            code_lines = change["code"].splitlines(keepends=True)
            code_lines = code_lines + ["\n"]
            insertions.append((insert_at, code_lines))

    # Sort insertions by position descending (apply bottom-up)
    insertions.sort(key=lambda x: x[0], reverse=True)
    replacements.sort(key=lambda x: x[0], reverse=True)

    # Apply replacements first (bottom-up)
    for start, end, new_lines in replacements:
        modified_lines = modified_lines[:start] + new_lines + modified_lines[end:]

    # Then apply insertions (bottom-up)
    for pos, code_lines in insertions:
        # Adjust pos if replacements above shifted things
        modified_lines = modified_lines[:pos + 1] + code_lines + modified_lines[pos + 1:]

    # Generate unified diff
    diff = difflib.unified_diff(
        original_lines,
        modified_lines,
        fromfile=str(result.proxy_path),
        tofile=str(result.proxy_path) + ".wired",
        lineterm="",
    )
    return "".join(diff)


# ═══════════════════════════════════════════════════════════════════════════
#  APPLY / REVERT
# ═══════════════════════════════════════════════════════════════════════════

def apply_changes(result: AnalysisResult, proxy_path: Path | None = None) -> bool:
    """Apply wiring changes to the proxy file.

    Creates a backup first, then applies the changes.

    Args:
        result: Analysis result from analyze_proxy().
        proxy_path: Override path (for testing).

    Returns:
        True if changes were applied, False if already wired or error.
    """
    if proxy_path is None:
        proxy_path = PROXY_PATH

    if result.is_wired:
        print("✓ Proxy is already fully wired — no changes needed.")
        return False

    if not result.changes_needed:
        print("✓ No changes needed.")
        return False

    # ── Backup ──────────────────────────────────────────────────────────
    backup = proxy_path.with_suffix(".py.bak-pre-wire")
    print(f"Backing up to {backup}...")
    shutil.copy2(proxy_path, backup)
    print("  ✓ Backup created.")

    # ── Apply changes ───────────────────────────────────────────────────
    content = proxy_path.read_text()
    lines = content.splitlines(keepends=True)

    # Collect all modifications
    insertions: list[tuple[int, list[str]]] = []
    replacements: list[tuple[int, int, list[str]]] = []

    for change in result.changes_needed:
        if change.get("commented_out"):
            continue

        if change["type"] == "call-site modification":
            old_text = "".join(change["old_code"].splitlines(keepends=True))
            start = content.find(old_text)
            if start >= 0:
                line_start = content[:start].count("\n")
                line_end = line_start + len(change["old_code"].splitlines(keepends=True))
                new_lines = change["new_code"].splitlines(keepends=True)
                replacements.append((line_start, line_end, new_lines))

        elif "insert_after" in change:
            insert_at = change["insert_after"]
            code_lines = change["code"].splitlines(keepends=True)
            insertions.append((insert_at, ["\n"] + code_lines + ["\n"]))

        elif "insert_before" in change:
            insert_at = change["insert_before"]
            code_lines = change["code"].splitlines(keepends=True)
            insertions.append((insert_at, code_lines + ["\n"]))

    # Sort bottom-up
    insertions.sort(key=lambda x: x[0], reverse=True)
    replacements.sort(key=lambda x: x[0], reverse=True)

    # Apply replacements
    for start, end, new_lines in replacements:
        lines = lines[:start] + new_lines + lines[end:]

    # Apply insertions
    for pos, code_lines in insertions:
        lines = lines[:pos + 1] + code_lines + lines[pos + 1:]

    proxy_path.write_text("".join(lines))
    print(f"✓ Changes applied to {proxy_path}")
    print(f"  Backup at {backup}")
    print(f"  Revert with: python scripts/wire_production.py --revert")
    return True


def revert_changes(proxy_path: Path | None = None) -> bool:
    """Restore the proxy from backup.

    Args:
        proxy_path: Override path (for testing).

    Returns:
        True if reverted, False if no backup found.
    """
    if proxy_path is None:
        proxy_path = PROXY_PATH

    backup = proxy_path.with_suffix(".py.bak-pre-wire")
    if not backup.exists():
        print(f"✗ No backup found at {backup}")
        return False

    shutil.copy2(backup, proxy_path)
    print(f"✓ Restored {proxy_path} from {backup}")
    return True


# ═══════════════════════════════════════════════════════════════════════════
#  REPORT
# ═══════════════════════════════════════════════════════════════════════════

def print_report(result: AnalysisResult) -> None:
    """Print a human-readable analysis report."""
    print("=" * 78)
    print("  PRODUCTION WIRING ANALYSIS — zai_proxy.py")
    print("=" * 78)
    print()
    print(f"Proxy path:  {result.proxy_path}")
    print(f"Exists:      {result.proxy_exists}")
    print(f"Lines:       {len(result.proxy_lines)}")
    print()

    if not result.proxy_exists:
        print("✗ Proxy file not found — cannot analyze.")
        return

    # ── Current state ───────────────────────────────────────────────────
    print("─" * 78)
    print("  CURRENT STATE")
    print("─" * 78)

    checks = [
        ("ShadowHook import", result.has_shadow_hook_import),
        ("ShadowHook init", result.has_shadow_hook_init),
        ("ShadowHook .compare() call", result.has_shadow_hook_compare_call),
        ("_record_spend function", result.has_record_spend),
        ("CostObserver", result.has_cost_observer),
        ("BurnRateAggregator", result.has_burn_rate_aggregator),
        ("PrimaryRouter", result.has_primary_router),
        ("update_burn_rate", result.has_update_burn_rate),
        ("pace_windows in compare", result.has_pace_windows_in_compare),
        ("failure_counts in compare", result.has_failure_counts_in_compare),
        ("quota_cache", result.has_quota_cache),
        ("_snapshot_quota()", result.has_snapshot_quota),
        ("_snapshot_health()", result.has_snapshot_health),
        ("_zai_key_health", result.has_zai_key_health),
    ]

    for name, present in checks:
        marker = "✓" if present else "✗"
        state = "PRESENT" if present else "MISSING"
        print(f"  {marker} {name:.<40} {state}")

    print()

    # ── Hook points ─────────────────────────────────────────────────────
    print("─" * 78)
    print("  HOOK POINTS")
    print("─" * 78)
    print(f"  _shadow_hook.compare() at line:  {result.shadow_hook_compare_line + 1 if result.shadow_hook_compare_line >= 0 else 'NOT FOUND'}")
    print(f"  _record_spend() at line:          {result.record_spend_line + 1 if result.record_spend_line >= 0 else 'NOT FOUND'}")
    print(f"  ShadowHook init block:            lines {result.shadow_hook_init_block[0]+1}-{result.shadow_hook_init_block[1]+1}" if result.shadow_hook_init_block[0] >= 0 else "  ShadowHook init block: NOT FOUND")
    print()

    # ── Wiring status ───────────────────────────────────────────────────
    print("─" * 78)
    print("  WIRING STATUS")
    print("─" * 78)
    if result.is_wired:
        print("  ✓ FULLY WIRED — all components are in place.")
    elif result.is_partially_wired:
        print("  ⚠ PARTIALLY WIRED — some components present, some missing.")
    else:
        print("  ✗ NOT WIRED — no wiring components detected.")
    print()

    # ── Changes needed ──────────────────────────────────────────────────
    if result.changes_needed:
        print("─" * 78)
        print(f"  CHANGES NEEDED ({len(result.changes_needed)})")
        print("─" * 78)
        for i, change in enumerate(result.changes_needed, 1):
            print(f"  {i}. [{change['component']}] {change['type']}")
            print(f"     {change['description']}")
            if "anchor" in change:
                print(f"     Anchor: {change['anchor']}")
            print()

    # ── Diff ────────────────────────────────────────────────────────────
    diff = generate_diff(result)
    if diff:
        print("─" * 78)
        print("  DIFF (dry-run — not applied)")
        print("─" * 78)
        print(diff)

    print("=" * 78)


# ═══════════════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════════════

def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    if argv is None:
        argv = sys.argv[1:]

    apply = "--apply" in argv
    revert = "--revert" in argv

    if revert:
        return 0 if revert_changes() else 1

    result = analyze_proxy()
    print_report(result)

    if apply:
        print()
        print("─" * 78)
        print("  APPLYING CHANGES")
        print("─" * 78)
        apply_changes(result)

    return 0


if __name__ == "__main__":
    sys.exit(main())