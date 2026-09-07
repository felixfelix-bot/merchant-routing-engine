# Test-run topology fix: make the flat_router / zai_proxy imported by the
# suite resolve to THIS repo tree, not a sibling checkout that can sit earlier
# on sys.path (e.g. ~/merchant-routing-engine, ~/.hermes/bot). Without this
# the caller-class T-A tests in test_flat_router.py can run against a stale,
# unmodified flat_router.py and fail spuriously. Importing this conftest pins
# the flat_router module before the test files' own path bootstrap runs.
import os
import sys

import flat_router  # noqa: F401  (import side effect: resolve+compile this tree's copy)

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)