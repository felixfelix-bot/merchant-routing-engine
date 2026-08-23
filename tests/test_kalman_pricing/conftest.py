"""Shared fixtures + module loading for the Kalman pricing pipeline tests.

Loads two production modules:

* ``zai_proxy`` — the local proxy script (from ``production/zai_proxy.py``).
  Contains the Nostr kind-30315 publisher: ``_build_kalman_pricing_json()``
  and ``_nostr_publish_kalman()``.

* ``kalman_pricing_hook`` — the VPS2 hook script
  (``production/kalman-pricing-hook.py``).  Contains ``query_nostr_kalman_events``,
  ``pick_freshest_event``, ``update_provider_db``, ``disable_provider_safe``, ``main``.

Both modules are loaded once at conftest import time and exposed via
session-scoped fixtures.  All external dependencies are mocked in individual
test modules.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys

import pytest

# ── Path setup ──────────────────────────────────────────────────────────────

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROD_DIR = os.path.abspath(os.path.join(_HERE, "..", "..", "production"))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))

if _PROD_DIR not in sys.path:
    sys.path.insert(0, _PROD_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# ── Load publisher (zai_proxy) ──────────────────────────────────────────────
# The import is noisy (prints init messages from ShadowHook, LiveRouter, etc.)
# but all side effects are wrapped in try/except and never crash.  The
# ``if __name__ == "__main__"`` block that starts threads is NOT executed on
# import, so no background threads are started.
import zai_proxy  # noqa: E402

# ── Load hook (kalman-pricing-hook.py — hyphenated name → importlib) ────────
_HOOK_PATH = os.path.join(_PROD_DIR, "kalman-pricing-hook.py")
_hook_spec = importlib.util.spec_from_file_location("kalman_pricing_hook", _HOOK_PATH)
hook = importlib.util.module_from_spec(_hook_spec)
_hook_spec.loader.exec_module(hook)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def publisher():
    """The zai_proxy module (publisher code)."""
    return zai_proxy


@pytest.fixture(scope="session")
def hook_mod():
    """The kalman-pricing-hook module."""
    return hook


@pytest.fixture(autouse=True)
def _silence_hook_log(monkeypatch):
    """Redirect hook.log() to a no-op so tests don't try to write to /var/log."""
    monkeypatch.setattr(hook, "log", lambda *a, **kw: None)
