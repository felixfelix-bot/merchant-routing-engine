#!/usr/bin/env python3
"""transient_canary_run.py — CLI runner for the ADR-014 transient-lane canary gate.

Exit codes: 0 = gate PASS, 1 = gate FAIL, 2 = error (bad args, missing key,
network exception before any prompt could run).

The API key is NEVER passed on the command line or stored in the report —
only the *name* of an environment variable is accepted (--key-env).

Usage examples:

  # OpenInference base-Flash candidate vs the incumbent reference lane
  export OPENINFERENCE_KEY=sk-...
  export REFERENCE_KEY=sk-...
  python3 scripts/transient_canary_run.py \\
      --endpoint https://api.openinference.ai/v1 \\
      --key-env OPENINFERENCE_KEY \\
      --model deepseek-v4-flash \\
      --reference-endpoint https://openrouter.ai/api/v1 \\
      --reference-key-env REFERENCE_KEY \\
      --reference-model deepseek/deepseek-v4-flash

  # OpenRouter lane (Baidu-0731) without a reference — sanity-only gate
  export OPENROUTER_KEY=sk-or-...
  python3 scripts/transient_canary_run.py \\
      --endpoint https://openrouter.ai/api/v1 \\
      --key-env OPENROUTER_KEY \\
      --model baidu/ernie-4.5-0731

Report lands in reports/canary/<slug>-<date>.json (relative to --out,
default: ./reports/canary relative to the repo root).
"""

from __future__ import annotations

import argparse
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

from src.transient_canary import run_canary  # noqa: E402

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_ERROR = 2

DEFAULT_OUT_DIR = os.path.join(_REPO_ROOT, "reports", "canary")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="ADR-014 transient-lane canary quality gate "
                    "(deterministic, no LLM judging).")
    p.add_argument("--endpoint", required=True,
                   help="candidate lane base URL, e.g. https://host/v1")
    p.add_argument("--key-env", required=True,
                   help="NAME of the env var holding the candidate API key "
                        "(the key itself is never accepted here)")
    p.add_argument("--model", required=True,
                   help="model name on the candidate lane")
    p.add_argument("--reference-endpoint", default=None,
                   help="optional reference lane base URL (incumbent)")
    p.add_argument("--reference-key-env", default=None,
                   help="NAME of the env var holding the reference API key")
    p.add_argument("--reference-model", default=None,
                   help="model name on the reference lane "
                        "(default: same as --model)")
    p.add_argument("--out", default=DEFAULT_OUT_DIR,
                   help=f"report output directory (default: {DEFAULT_OUT_DIR})")
    p.add_argument("--n-prompts", type=int, default=20,
                   help="number of canary prompts (default: 20)")
    return p


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)

    api_key = os.environ.get(args.key_env)
    if not api_key:
        print(f"error: env var {args.key_env!r} is not set or empty "
              f"(it must hold the candidate API key)", file=sys.stderr)
        return EXIT_ERROR

    reference_key = None
    if args.reference_endpoint:
        if args.reference_key_env:
            reference_key = os.environ.get(args.reference_key_env)
            if not reference_key:
                print(f"error: env var {args.reference_key_env!r} is not set "
                      f"or empty", file=sys.stderr)
                return EXIT_ERROR
        else:
            reference_key = api_key  # fall back to candidate key

    print(f"canary: {args.endpoint} model={args.model} "
          f"n_prompts={args.n_prompts}"
          + (f" vs reference {args.reference_endpoint}"
             f" model={args.reference_model or args.model}"
             if args.reference_endpoint else " (no reference)"))

    try:
        report = run_canary(
            args.endpoint,
            api_key,
            args.model,
            reference_endpoint=args.reference_endpoint,
            reference_key=reference_key,
            reference_model=args.reference_model,
            n_prompts=args.n_prompts,
            out_dir=args.out,
        )
    except Exception as e:  # unexpected — gate never ran to completion
        print(f"error: canary run crashed: {e!r}", file=sys.stderr)
        return EXIT_ERROR

    print(report.summary())
    return EXIT_PASS if report.pass_ else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
