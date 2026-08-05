"""RP-2 verification: syntax check the patched proxy + smoke-test cost extraction."""
from __future__ import annotations

import json
import os
import py_compile
import subprocess
import sys

PROXY = os.path.expanduser("~/.hermes/bot/zai_proxy.py")
MRE = os.path.expanduser("~/merchant-routing-engine")


def main() -> None:
    # 1. Syntax check the patched proxy.
    py_compile.compile(PROXY, doraise=True)
    print("[1] proxy syntax: OK")

    # 2. Module import + smoke tests from the proxy's perspective.
    sys.path.insert(0, MRE)
    from src.cost_extraction import extract_cost  # noqa: E402

    body = json.dumps({"usage": {"cost": 0.001}}).encode()
    assert extract_cost("openrouter", body) == (0.001, "measured")
    assert extract_cost("ppq", body) == (0.001, "measured")
    assert extract_cost("ours", body) == (None, None)
    assert extract_cost("ollama_cloud", body) == (None, None)
    print("[2] cost_extraction smoke: OK")

    # 3. Verify _log_api_call signature has the new params by importing
    #    the patched proxy module (runs the guarded import block).
    #    We exec it in a throwaway namespace so it doesn't start the server.
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0,'" + MRE + "'); "
         "import importlib.util as u; s=u.spec_from_file_location('zp','" + PROXY + "'); "
         "m=u.module_from_spec(s); s.loader.exec_module(m); "
         "import inspect; sig=inspect.signature(m._log_api_call); "
         "assert 'cost_usd' in sig.parameters, sig; "
         "assert 'cost_source' in sig.parameters, sig; "
         "assert hasattr(m, '_extract_cost'); "
         "print('  _log_api_call params:', list(sig.parameters)); "
         "print('  _extract_cost exists:', callable(m._extract_cost)); "
         "print('  cost import ok:', m._extract_cost_module is not None)"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        print("[3] proxy import FAILED:")
        print(result.stderr[-2000:])
        sys.exit(1)
    print(result.stdout.rstrip())
    print("[3] proxy import + signature: OK")


if __name__ == "__main__":
    main()
