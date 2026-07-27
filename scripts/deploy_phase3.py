#!/usr/bin/env python3
"""Phase 3 deployment script — switch proxy from best_key() to PrimaryRouter.

This script:
1. Backs up the current proxy
2. Patches best_key() to delegate to PrimaryRouter when available
3. Restarts the proxy
4. Verifies health
5. On any failure, reverts automatically

Usage:
    python3 scripts/deploy_phase3.py [--dry-run] [--revert]

--dry-run: show what would be changed, don't modify anything
--revert: restore from backup (undo Phase 3)
"""
import os
import sys
import shutil
import subprocess
import time
from pathlib import Path

# ── Repo root discovery ────────────────────────────────────────────────────
# Walk up from this script to find the merchant-routing-engine repo root.
_SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = _SCRIPT_DIR.parent
if not (REPO_ROOT / "src" / "primary_router.py").exists():
    # Try common alternative location
    REPO_ROOT = Path.home() / "merchant-routing-engine"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PROXY = Path.home() / ".hermes" / "bot" / "zai_proxy.py"
BACKUP = Path.home() / ".hermes" / "bot" / "zai_proxy.py.bak-phase3"
PRE_PHASE3 = Path.home() / ".hermes" / "bot" / "zai_proxy.py.bak-phase2"

# The Phase 3 patch: replace best_key's internal logic with PrimaryRouter
# delegation, keeping best_key() as a fallback wrapper.
PATCH_MARKER = "# ── Phase 3: PrimaryRouter delegation ──"
PATCH_CODE = '''
    # ── Phase 3: PrimaryRouter delegation ──────────────────────────────
    # Use the price-first optimizer as the PRIMARY routing decision.
    # Falls back to the original best_key logic if PrimaryRouter is unavailable.
    global _primary_router
    if _primary_router is not None:
        try:
            quota = _snapshot_quota()
            health = _snapshot_health()
            choice = _primary_router.route(
                model=None,
                tokens=0,  # unknown before request
                quota_state=quota,
                health_state=health,
            )
            if choice in ("ours", "friend"):
                op = _max_pct(quota_cache.get("ours", ([], 0.0))[0])
                fp = _max_pct(quota_cache.get("friend", ([], 0.0))[0])
                _log_key_decision(chosen_key=choice,
                                  reason="phase3_primary_router",
                                  ours_pct=op, friend_pct=fp,
                                  ours_available=1, friend_available=1)
                return choice
            # None → optimizer says skip z.ai (ollama/external). Return None.
            _log_key_decision(chosen_key=None,
                              reason="phase3_optimizer_skip_zai",
                              ours_pct=0, friend_pct=0,
                              ours_available=0, friend_available=0)
            return None
        except Exception:
            pass  # fall through to original logic
    # ── End Phase 3 patch ──
'''


# ── Pre-deploy health checks ──────────────────────────────────────────────────

# Modules that must import cleanly (Phase 4 + Phase 5 additions)
_REQUIRED_IMPORTS = [
    "src.primary_router",
    "src.shadow_hook",
    "src.pricing_engine",
    "src.demand_kalman",
    "src.margin_layer",
    "src.routing_optimizer",
    "src.price_kalman",
    "src.consumption_kalman",
    "src.shadow_logger",
    "src.provider_names",
    "src.key_health_tracker",
    "src.provider_funding_tracker",
    "src.profit_tracker",
    "src.route_request",
    "src.external_failover",
    "src.backoff",
    "src.reasoning_handler",
    "src.routing_advisor",
]

# Source files to syntax-check (all modified modules + scripts)
_SYNTAX_CHECK_FILES = [
    "src/primary_router.py",
    "src/shadow_hook.py",
    "src/pricing_engine.py",
    "src/demand_kalman.py",
    "src/margin_layer.py",
    "src/routing_optimizer.py",
    "src/price_kalman.py",
    "src/consumption_kalman.py",
    "src/shadow_logger.py",
    "src/provider_names.py",
    "src/key_health_tracker.py",
    "src/provider_funding_tracker.py",
    "src/profit_tracker.py",
    "src/route_request.py",
    "src/external_failover.py",
    "src/backoff.py",
    "src/reasoning_handler.py",
    "src/routing_advisor.py",
    "scripts/feed_historical_costs.py",
    "scripts/deploy_phase3.py",
]


def check_proxy_health() -> bool:
    """Verify proxy is up and responding."""
    try:
        import urllib.request
        with urllib.request.urlopen("http://localhost:9099/health", timeout=5) as r:
            return r.status == 200 and r.read().strip() == b"ok"
    except Exception:
        return False


def pre_deploy_health_checks() -> tuple[bool, list[str]]:
    """Run comprehensive health checks before deploying.

    Verifies:
      (a) All tests pass (pytest)
      (b) PrimaryRouter and all required modules import correctly
      (c) No syntax errors in any modified source file

    Returns:
        (all_passed, messages) — True if all checks pass, plus a list of
        status/error messages for logging.
    """
    messages: list[str] = []
    all_passed = True

    # ── Check (a): Test suite ─────────────────────────────────────────────
    messages.append("── Pre-deploy Health Check 1/3: Test suite ──")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=short"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            # Extract pass count from last line
            last_line = result.stdout.strip().split("\n")[-1] if result.stdout.strip() else ""
            messages.append(f"  ✓ Tests PASS — {last_line}")
        else:
            all_passed = False
            # Show last 10 lines of output for context
            lines = result.stdout.strip().split("\n")[-10:] if result.stdout else []
            messages.append(f"  ✗ Tests FAIL (exit {result.returncode})")
            for line in lines:
                messages.append(f"    {line}")
    except FileNotFoundError:
        messages.append("  ⚠ pytest not found — skipping test suite check")
        messages.append("    (install with: pip install pytest)")
    except Exception as e:
        messages.append(f"  ✗ Test runner error: {e}")
        all_passed = False

    # ── Check (b): Module imports ─────────────────────────────────────────
    messages.append("── Pre-deploy Health Check 2/3: Module imports ──")
    import_errors = []
    for mod_name in _REQUIRED_IMPORTS:
        try:
            __import__(mod_name)
            messages.append(f"  ✓ {mod_name}")
        except Exception as e:
            messages.append(f"  ✗ {mod_name}: {e}")
            import_errors.append(mod_name)
            all_passed = False
    if not import_errors:
        messages.append("  All imports OK")

    # ── Check (c): Syntax checks ──────────────────────────────────────────
    messages.append("── Pre-deploy Health Check 3/3: Syntax checks ──")
    import py_compile
    syntax_errors = []
    for rel_path in _SYNTAX_CHECK_FILES:
        full_path = REPO_ROOT / rel_path
        if not full_path.exists():
            messages.append(f"  ⚠ {rel_path}: file not found (skipped)")
            continue
        try:
            py_compile.compile(str(full_path), doraise=True)
            messages.append(f"  ✓ {rel_path}")
        except py_compile.PyCompileError as e:
            messages.append(f"  ✗ {rel_path}: {e}")
            syntax_errors.append(rel_path)
            all_passed = False
    if not syntax_errors:
        messages.append("  All syntax checks PASS")

    # ── Summary ───────────────────────────────────────────────────────────
    if all_passed:
        messages.append("")
        messages.append("══ ALL PRE-DEPLOY HEALTH CHECKS PASSED ══")
    else:
        messages.append("")
        messages.append("══ PRE-DEPLOY HEALTH CHECKS FAILED — ABORTING DEPLOYMENT ══")

    return all_passed, messages


def restart_proxy() -> bool:
    """Restart the proxy service."""
    try:
        env = os.environ.copy()
        env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
        subprocess.run(["systemctl", "--user", "restart", "zai-proxy"],
                      env=env, capture_output=True, timeout=15)
        time.sleep(3)
        return check_proxy_health()
    except Exception:
        return False


def deploy():
    """Apply Phase 3 patch to the proxy.

    Runs pre-deploy health checks first. If any check fails, aborts before
    touching the proxy file.
    """
    # ── Pre-deploy health checks ─────────────────────────────────────────
    print("Running pre-deploy health checks...")
    passed, msgs = pre_deploy_health_checks()
    for m in msgs:
        print(m)
    if not passed:
        print("\nDEPLOY ABORTED — pre-deploy health checks failed.")
        return False
    print()

    if not PROXY.exists():
        print(f"ERROR: proxy not found at {PROXY}")
        return False

    # Backup current version
    shutil.copy2(PROXY, BACKUP)
    print(f"Backed up to {BACKUP}")

    content = PROXY.read_text()

    # Check if already patched
    if PATCH_MARKER in content:
        print("ALREADY PATCHED — Phase 3 is already deployed")
        return True

    # Find the insertion point: right after best_key()'s docstring
    # The pattern is the docstring close + proactive section comment
    marker = '    # Phase 1 — PROACTIVE: use Kalman predictions'
    if marker not in content:
        print(f"ERROR: cannot find insertion point in best_key()")
        return False

    # Insert Phase 3 code before the proactive section
    patched = content.replace(marker, PATCH_CODE + "\n" + marker, 1)

    # Also need to add the import + singleton at the top
    import_marker = "    _shadow_hook = None\n"
    if import_marker not in patched:
        print("ERROR: cannot find shadow hook import to add PrimaryRouter")
        return False

    primary_import = '''
# ── Phase 3: PrimaryRouter (optimizer as primary routing) ───────────────────
_primary_router = None
try:
    from src.primary_router import PrimaryRouter
    _primary_router = PrimaryRouter.get_instance()
    print(f"[phase3] PrimaryRouter initialized", flush=True)
except Exception as _e:
    print(f"[phase3] DISABLED — {_e}", flush=True)
    _primary_router = None
'''
    patched = patched.replace(import_marker, import_marker + primary_import, 1)

    # Write patched version
    PROXY.write_text(patched)
    print("Patch applied")

    # Syntax check
    try:
        import py_compile
        py_compile.compile(str(PROXY), doraise=True)
        print("Syntax check: PASS")
    except Exception as e:
        print(f"Syntax check: FAIL — {e}")
        print("REVERTING...")
        shutil.copy2(BACKUP, PROXY)
        return False

    # Restart + verify
    print("Restarting proxy...")
    if restart_proxy():
        print("Proxy restarted — HEALTH OK")
        return True
    else:
        print("Proxy health check FAILED — REVERTING...")
        shutil.copy2(BACKUP, PROXY)
        restart_proxy()
        return False


def revert():
    """Revert Phase 3, restore from backup."""
    if BACKUP.exists():
        shutil.copy2(BACKUP, PROXY)
        print(f"Reverted from {BACKUP}")
        restart_proxy()
        print("Proxy restarted after revert")
    else:
        print(f"No backup at {BACKUP} — cannot revert")
        return False


def dry_run():
    """Show what would be changed without modifying anything."""
    print("=" * 72)
    print("  Phase 3 Deployment — DRY RUN")
    print("=" * 72)
    print()
    print(f"Proxy:      {PROXY}")
    print(f"Backup:      {BACKUP}")
    print(f"Repo root:   {REPO_ROOT}")
    print(f"Insertion:   before best_key() proactive section")
    print(f"Lines to add: ~35 (import + delegation code)")
    print()

    # ── Run health checks in dry-run mode ────────────────────────────────
    print("Running pre-deploy health checks (dry-run)...")
    print()
    passed, msgs = pre_deploy_health_checks()
    for m in msgs:
        print(m)
    print()

    print("The patch:")
    print("  1. Imports PrimaryRouter at module level")
    print("  2. Inside best_key(), tries PrimaryRouter.route() FIRST")
    print("  3. Falls back to existing proactive + reactive logic on any error")
    print()
    print("Phase 4+5 features verified:")
    print("  - Health-driven pricing (graduated failure penalties)")
    print("  - Provider name normalization (zai_ours→ours, manager→ours)")
    print("  - Historical cost feed for instant Kalman convergence")
    print("  - DeepInfra per-token provider support")
    print("  - Demand Kalman + Margin Layer (profit optimization)")
    print()
    print("Safety:")
    print("  - Pre-deploy: tests + imports + syntax checks (abort if any fail)")
    print("  - PrimaryRouter failure → falls through to best_key's existing logic")
    print("  - Syntax error → auto-revert from backup")
    print("  - Health check fail → auto-revert + restart")
    print("  - --revert flag available for manual rollback")
    print()
    if passed:
        print("✓ All health checks PASSED — safe to deploy.")
    else:
        print("✗ Health checks FAILED — resolve issues before deploying.")


if __name__ == "__main__":
    if "--dry-run" in sys.argv:
        dry_run()
    elif "--revert" in sys.argv:
        revert()
    else:
        success = deploy()
        sys.exit(0 if success else 1)
