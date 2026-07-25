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

PROXY = Path.home() / ".hermes" / "bot" / "zai_proxy.py"
BACKUP = Path.home() / ".hermes" / "bot" / "zai_proxy.py.bak-phase3"
PRE_PHASE3 = Path.home() / ".hermes" / "bot" / "zai_proxy.py.bak-phase2"

# The Phase 3 patch: replace best_key's internal logic with PrimaryRouter
# delegation, keeping best_key() as a fallback wrapper.
PATCH_MARKER = "# ── Phase 3: PrimaryRouter delegation ──"
PATCH_CODE = '''
    # ── Phase 3: PrimaryRouter delegation ──
    # Use the price-first optimizer as the PRIMARY routing decision.
    # Falls back to the original best_key logic if PrimaryRouter is unavailable.
    global _primary_router
    if _primary_router is not None:
        try:
            quota = _snapshot_quota()
            health = _snapshot_health()
            choice = _primary_router.route(
                model=getattr(self, '_current_model', None),
                tokens=0,  # unknown before request
                quota_state=quota,
                health_state=health,
            )
            if choice in ("ours", "friend"):
                _log_key_decision(chosen_key=choice,
                                  reason="phase3_primary_router",
                                  ours_pct=_max_pct(quota_cache.get("ours", ([], 0.0))[0]),
                                  friend_pct=_max_pct(quota_cache.get("friend", ([], 0.0))[0]),
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


def check_proxy_health() -> bool:
    """Verify proxy is up and responding."""
    try:
        import urllib.request
        with urllib.request.urlopen("http://localhost:9099/health", timeout=5) as r:
            return r.status == 200 and r.read().strip() == b"ok"
    except Exception:
        return False


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
    """Apply Phase 3 patch to the proxy."""
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
    print(f"Proxy: {PROXY}")
    print(f"Backup: {BACKUP}")
    print(f"Insertion: before best_key() proactive section")
    print(f"Lines to add: ~35 (import + delegation code)")
    print()
    print("The patch:")
    print(f"1. Imports PrimaryRouter at module level")
    print(f"2. Inside best_key(), tries PrimaryRouter.route() FIRST")
    print(f"3. Falls back to existing proactive + reactive logic on any error")
    print()
    print("Safety:")
    print("- PrimaryRouter failure → falls through to best_key's existing logic")
    print("- Syntax error → auto-revert from backup")
    print("- Health check fail → auto-revert + restart")


if __name__ == "__main__":
    if "--dry-run" in sys.argv:
        dry_run()
    elif "--revert" in sys.argv:
        revert()
    else:
        success = deploy()
        sys.exit(0 if success else 1)
