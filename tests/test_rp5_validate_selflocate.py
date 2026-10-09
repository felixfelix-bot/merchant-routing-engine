"""rp5_validate.py must not depend on a hardcoded repo path.

2026-10-09: the validator hardcoded
    REPO = "/home/c03rad0r/merchant-routing-engine"
a symlink onto the DQ05 sshfs mount. DQ05 went offline, so every access to that
path raised EIO/ENOENT and `os.chdir(REPO)` killed the process *before the first
check ran*. The rp5-shadow-health cron could therefore only ever report
"spawn:OSError"/"could not append evidence" — never a verdict.

The repo must be located from this file's own path (scripts/ -> repo root), with
an RP5_REPO override honoured so a caller can target a specific tree.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "rp5_validate.py"


def test_no_hardcoded_absolute_repo_path():
    """Guard the exact regression: no absolute /home/... REPO literal."""
    src = SCRIPT.read_text()
    m = re.search(r"""^REPO\s*=\s*["'](/home/[^"']+)["']""", src, re.M)
    assert m is None, (
        f"rp5_validate.py still hardcodes an absolute repo path: {m.group(1)!r}. "
        "Locate the repo from __file__ (or RP5_REPO) instead — a hardcoded path "
        "onto another node's mount kills the validator whenever that node is down."
    )


def test_honours_rp5_repo_override():
    """rp5_shadow_health.py targets a tree by setting RP5_REPO; it must win."""
    assert "RP5_REPO" in SCRIPT.read_text(), (
        "validator must honour the RP5_REPO override its caller sets")


def test_runs_from_unrelated_cwd():
    """Invoked from /tmp it must run to a verdict, not die on chdir."""
    env = {k: v for k, v in os.environ.items() if k != "RP5_REPO"}
    p = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd="/tmp", env=env, capture_output=True, text=True, timeout=600,
    )
    blob = p.stdout + p.stderr
    assert "Traceback" not in blob, blob[-1200:]
    assert "FileNotFoundError" not in blob, blob[-1200:]
    assert p.returncode in (0, 1), f"rc={p.returncode}: {blob[-1200:]}"
