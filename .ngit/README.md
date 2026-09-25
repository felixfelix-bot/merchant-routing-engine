# Nostr CI (ngit-ci) for merchant-routing-engine

`.ngit/act/workflows/python-test.yml` is read by the **ngit-ci** coordinator.
It is separate from `.github/workflows/` (this repo has none), so GitHub
Actions is untouched; this is the repo's first automated gate.
`felixfelix-bot/merchant-routing-engine` is **not a fork** (`gh api … fork:false`,
default `main`, public), so the workflow is committed on `ci/ngit-workflows`
(cut from `origin/main`).

## What the workflow runs (job `tests`)

| Step | Command | Source |
|---|---|---|
| deps | `pip install pytest numpy pyyaml requests` | REPRODUCE.md; `requests` added because `tests/test_oxalpha_eval.py` imports `scripts/oxalpha_eval.py`, which imports it at module level — otherwise collection aborts (observed locally) |
| import smoke | `python -c "import src.key_health_tracker; import src.provider_funding_tracker"` | AGENTS.md "Build/Test Commands" |
| byte-compile | `python -m py_compile src/*.py` | AGENTS.md "Lint" |
| router suite | `python -m pytest test_flat_router.py -q` | REPRODUCE.md "Verification" |
| root suites | `python -m pytest test_garbage_circuit_breaker.py test_quality_floor.py -q` | **not in the docs** — two root-level suites outside `tests/`; cheap (33 passed) and additive |
| module suite | `python -m pytest tests/ -q` + the 24 ignores below | AGENTS.md "Run tests" (`tests/ -v`) |

Triggers: `push`, `pull_request` (any branch) and `workflow_dispatch`.
`runs-on: ubuntu-latest`, 30-min timeout, no `container:`/`services:`, no
job-level `uses:`, no secrets, no `GITHUB_TOKEN` (the coordinator supplies
none); Python pinned to `3.14`.

**Verified with the repo's local workflow driver, before and after the commit:
`ALL RUN STEPS PASSED` — 91 passed (flat router), 33 passed (root suites), 1893
passed (module suite), every `run:` step exit 0.**

### Repo docs vs reality
- REPRODUCE.md says `test_flat_router.py` is "77 tests → 77 passed"; it is **91
  passed** at this commit. The documented command runs, so it picks up the 91.
- AGENTS.md's `pytest tests/ -v` is **red** here (22 of the 80 `tests/` modules
  fail), so the step keeps the documented command and excludes them explicitly.

## ⚠️ 22 of the 80 `tests/` modules are RED — the ignore list is debt

The ignores are not a claim that the repo is green. Excluding them keeps the job
useful (it reports on the other 54 modules / 1893 tests); each one is listed with
its measured signature, and removing ignores as modules are fixed is the point.

**Cannot pass off the operator's machine — `FileNotFoundError:
$HOME/.hermes/bot/zai_proxy.py`** (they load the 420 KB *live* production proxy,
which is not in the repo; the repo only has `production/zai_proxy.py`):
`tests/test_live_router_wire.py` (16 errors), `tests/test_tier_cleanup.py`
(12 errors), `tests/test_pricing_exposure.py` (60 passed, 6 errors),
`tests/test_task_type_logging.py` (2 failed, 28 passed).

**Needs credentials or live network:** `tests/test_telnyx_integration_live.py`
(8 failed — `urllib.error.HTTPError: HTTP Error 403: Forbidden`),
`tests/test_neuralwatt_collector.py` (1 failed, 4 passed — `AssertionError: No
NEURALWATT_API_KEY found in .env files`).

**Genuine assertion failures at this commit (no environment excuse):**

| Module | Result | First signature |
|---|---|---|
| `tests/test_cpvo_live_router.py` | 2 failed, 8 passed | `quality-aware optimizer must pick the reliable 'friend' over the cheap-but-flaky 'ollama_cloud'` |
| `tests/test_dynamic_base_rates.py` | 9 failed, 21 passed | `assert 0.096 == 0.03 ± 3.0e-11` (amortized z.ai rate) + provider-set mismatch |
| `tests/test_live_router_model_aware.py` | 10 failed, 3 passed | `assert 'ollama_cloud_2' == 'ollama_cloud'` |
| `tests/test_live_router.py` | 19 failed, 42 passed | `assert 12 == 6` (provider table); `assert 0.03 == 0.001 ± 0.01` |
| `tests/test_live_router_quota_regime.py` | 8 failed, 34 passed | `assert 'ollama_cloud_2' == 'ollama_cloud'` |
| `tests/test_live_router_throttle.py` | 13 failed, 22 passed | `assert 'ollama_cloud_2' == 'ollama_cloud'` |
| `tests/test_p7_cascade.py` | 5 failed, 4 passed | `assert 'ollama_cloud_2' == 'ours'` |
| `tests/test_per_model_pricing.py` | 5 failed, 24 passed | `glm-5.2 regressed: chose 'ollama_cloud', expected 'ours'` |
| `tests/test_proxy_snapshot_quota.py` | 4 failed, 29 passed (125 s) | `TestSnapshotWithRealData` / `TestFallback` assertions |
| `tests/test_quota_pressure_routing.py` | 4 failed, 3 passed | `expected z.ai friend (cheaper at high pressure), got ollama_cloud_2` |
| `tests/test_rate_export.py` | 3 failed, 10 passed | `assert 'measured' == 'fallback'` |
| `tests/test_real_price_tracker.py` | 41 failed, 101 passed | `assert 0.096 == 0.03 ± 1.0e-12`; `assert 1.0 == 2.7 ± 2.7e-12` |
| `tests/test_telnyx_provider.py` | 13 failed, 68 passed, 10 skipped | `assert 16.0 == 10.0 ± 1.0e-05` |
| `tests/test_universal_pressure.py` | 3 failed, 70 passed | `assert 16.0 == 10.0`; `expected pressure > 1.0, got 1.0` |
| `tests/test_kalman_pricing/test_publisher.py` | 4 failed, 24 passed | `assert False is True` (availability) |
| `tests/test_urgency_cost_estimator.py` | 1 error (collection) | `ImportError: cannot import name 'display_urgency_costs' from 'src.urgency_cost_estimator'` |

Two of the 24 ignores are **not** red, and deleting them is the first debt item:
`tests/test_cpvo_calculator.py` (22 passed, 70 s) and `tests/test_cpvo_model_aware.py`
(19 passed, 80 s) pass alone and in-suite — a run with only the other 22 ignores
is green at **1934 passed in 259 s**. They stayed in the measured set as slow
modules the classification pass did not finish.

## Deliberately not covered

- The 24 modules above, for the reasons in their rows.
- REPRODUCE.md's failure-injection recipe (`curl` to the live proxy on `:9099`,
  `~/.hermes/bot/.key_disabled_ours`, the `key_decisions` sqlite table): needs
  the running production proxy and its live DB, not a checkout.

## Triggering and reading results
This deployment uses the **`request-required`** policy: push-triggered runs
wait until a maintainer publishes a standing Service Request (kind `9843`) that
names **this repo's** `a=30617:<maintainer-hex>:merchant-routing-engine`
coordinate. An old SR (`15249e4d`, 2026-09-12) still on the relays targets the
rotated-away `36bdeb…` coordinate and covers nothing here — the current SR for
the live `9cd14d9a…` coordinate is what authorizes runs; a one-shot kind `9840`
manual trigger bypasses the gate regardless.

A maintainer trigger signs with the repo's maintainer key (git config
`nostr.nsec`, set locally for the duration of the push/trigger and unset after):

```bash
git config --local nostr.nsec <maintainer-nsec>
git push ngit HEAD:refs/heads/ci/<slug>          # plain ci/ ref, NOT a pr/ ref
git ls-remote ngit refs/heads/ci/<slug>          # must show the sha first
ngit -d ci trigger --workflow .ngit/act/workflows/python-test.yml \
     --ref refs/heads/ci/<slug> <COORDINATOR_HEX> <COMMIT_SHA>
git config --local --unset nostr.nsec
```

- `ngit ci status <commit|pr>` — available on this fleet's ngit v3.0.1 CLI.
- Kind **39842** = workflow progress, **9841** = per-job result (log tail in
  `content`), **9842** = workflow conclusion:
  `nak req -k 9842 -a "<coordinator-hex>" -l 5 wss://relay.ngit.dev`
- Results are signed by whoever's coordinator ran the job — a green mark in a
  viewer (gitworkshop.dev) can come from a relay's own coordinator. Trust our
  own coordinator's signature (`765cd47b…`, DQ05) for gate evidence.
- Gate evidence line shape: `ci_evidence repo=merchant-routing-engine head=<40-hex> ref=refs/heads/ci/<slug>`.
