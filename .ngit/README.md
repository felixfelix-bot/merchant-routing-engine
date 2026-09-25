# Nostr CI (ngit-ci) for merchant-routing-engine

`.ngit/act/workflows/python-test.yml` is read by the **ngit-ci** coordinator.
It is separate from `.github/workflows/` (this repo has none), so GitHub
Actions is untouched; this is the repo's first automated gate.
`felixfelix-bot/merchant-routing-engine` is **not a fork** (`gh api … fork:false`,
default `main`, public). The file was authored on the branch `ci/ngit-workflows`
(cut from `main`), merged to `main` as `2ccbbb91`, and the lane tip `4e55c8f`
was additionally pushed to the ngit mirror as the plain ref
`refs/heads/ci/ngit-workflow-lane` so a maintainer-directed manual replay of the
same workflow could be run at it (see "Triggering and reading results").

## What the workflow runs (job `tests`)

| Step | Command | Source |
|---|---|---|
| deps | `pip install pytest numpy pyyaml requests` | REPRODUCE.md; `requests` added because `tests/test_oxalpha_eval.py` imports `scripts/oxalpha_eval.py`, which imports it at module level — otherwise collection aborts (observed locally) |
| import smoke | `python -c "import src.key_health_tracker; import src.provider_funding_tracker"` | AGENTS.md "Build/Test Commands" |
| byte-compile | `python -m py_compile src/*.py` | AGENTS.md "Lint" |
| router suite | `python -m pytest test_flat_router.py -q` | REPRODUCE.md "Verification" |
| root suites | `python -m pytest test_garbage_circuit_breaker.py test_quality_floor.py -q` | **not in the docs** — two root-level suites outside `tests/`; cheap (33 passed) and additive |
| module suite | `python -m pytest tests/ -q` + the 24 ignores below and one `--deselect` | AGENTS.md "Run tests" (`tests/ -v`) |

Triggers: `push` on `main` only (branch-filtered, so no other branch can
start an unintended coordinator run), `pull_request` on any branch, and
`workflow_dispatch` (manual replay). `runs-on: ubuntu-latest`, 30-min timeout,
no `container:`/`services:`, no job-level `uses:`, no secrets, no `GITHUB_TOKEN`
(the coordinator supplies none).

Pins: `actions/checkout@v5` / `actions/setup-python@v5` are explicit literal
version tags (the ngit-ci recipe's "literal pin" means a concrete version, not
`-file`/`latest`/an expression); the interpreter is pinned to the literal
`3.14`. The pip line is deliberately unpinned: this repo ships no lockfile, so
there is no repo pin to mirror — the four packages are the test-time
dependencies named in `REPRODUCE.md` plus `requests` (see the table above).

**Verified locally with the workflow driver
(`…/skills/devops/nostr-ci-ngit/scripts/run-workflow-locally.py`) on the commit
that carries this file, run CLEAN-ROOM (empty `HOME`, so the optional host paths
the tests probe do not exist — which is what `act` gives you): `ALL RUN STEPS
PASSED`, every `run:` step exit 0 — 91 passed (flat router), 33 passed (two root
suites), 1912 passed / 1 deselected (module suite) — matching the module-suite
count the CI runs report for this suite.**

Two steps are host-dependent, so a naive "reproduce it on my box" can go red for
reasons that have nothing to do with the commit:

- *Root-level suites* read LIVE router state: `flat_router.py` consults
  `~/.hermes/bot/zai_usage.db`, the live flag files and the balance bridges.
  Measured on the operator's host 2026-09-25: `3 failed, 30 passed`
  (`neuralwatt` demoted by live state). In the clean CI workspace the step is
  green — treat the kind-9842 result, not a host run, as this step's verdict.
- `~/.hermes/bot/zai_proxy.py` (the 420 KB live proxy) is not in this repo, so
  the modules listed below that load it cannot run here at all.

Both driver logs (clean-room and host) are attached to the tracking kanban card
(`merchant-routing:t_433b9ad7`).

### Repo docs vs reality
- REPRODUCE.md says `test_flat_router.py` is "77 tests → 77 passed"; it is **91
  passed** at this commit. The documented command runs, so it picks up the 91.
- AGENTS.md's `pytest tests/ -v` is **red** here (22 of the 80 `tests/` modules
  fail), so the step keeps the documented command and excludes them explicitly.

## ⚠️ 22 of the 80 `tests/` modules are RED — the ignore list is debt

The ignores are not a claim that the repo is green. Excluding them keeps the job
useful (it reports on the other 56 modules); each one is listed with
its measured signature, and removing ignores as modules are fixed is the point.

**One test deselected for the CI environment (2026-09-25, run at `de4c73d4`):**
`tests/test_oxalpha_tier.py::test_model_map_additions_are_scoped_to_oxalpha_only`
runs `git show HEAD:config/providers.yaml` via `subprocess.run(check=True,
cwd=REPO)`. In the ngit-ci act workspace `git show` exits **128** (the workspace
is not a git checkout the way a clone is), so the test errored with
`CalledProcessError` — the only failure of that run (1 failed, 1912 passed,
43 s). It passes in a normal clone, so it is deselected here rather than
ignored-whole-module; the deselect is the first thing to revisit if the
coordinator's workspace ever carries a real `.git`.

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

## Scope: what a green run does and does NOT certify

Read this before quoting the lane as evidence for a card.

- **It certifies the public tree of THIS repo at the exact commit of the run** —
  the steps in the table above, at that SHA, run by a coordinator and signed as
  a kind-9842 result (see the next section for how to bind the result to a
  signer).
- **It does NOT certify the 24 excluded `tests/` modules.** 22 of them are red
  at this commit for reasons recorded row by row above; a green lane is not a
  green `pytest tests/`, and must not be reported as one.
- **It does NOT certify the LIVE engine code.** The live engine
  (`~/.hermes/bot/`) is AHEAD of this public tree, and the delta is not in this
  repo:

  | File | Live (private) | This repo | Note |
  |---|---|---|---|
  | `zai_proxy.py` | `~/.hermes/bot/zai_proxy.py`, 9 011 lines | `production/zai_proxy.py`, 7 852 lines | live-only work: caller-class routing, tier alias/shadow resolution, ollama paywall disarm, quota-bench persistence, garbage-check integration, egress hints, upstream timeout |
  | `flat_router.py` | `~/.hermes/bot/flat_router.py`, 2 169 lines | `flat_router.py`, 1 338 lines | live-only provider/lane selection work |
  | `garbage_detector.py` | `~/.hermes/bot/garbage_detector.py`, 653 lines | **absent** | no counterpart in this repo |

  A card whose deliverable is a change to the LIVE engine therefore cannot be
  certified by this lane as it stands: the green run's commit does not contain
  the code under review. For honest evidence such a card must first publish the
  sanitized delta of the files above to this repo (public tree — no `.env`, no
  keys, no `nsec`) and re-run the lane at that commit. Line counts are measured
  on the operator's machine (2026-09-25); re-measure rather than trust them.

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
- Gate evidence line shape: `ci_evidence repo=merchant-routing-engine head=<40-hex> ref=<ref>`.

### Observed runs (both green, both signed by the same coordinator)

| Commit | Ref | Trigger | Conclusion | kind-9842 event |
|---|---|---|---|---|
| `2ccbbb91` (merged `main`) | `refs/heads/main` | `push` | `success` | `126f2258ce745050858e432193bcce4938e47da81f4e349dd4f9cfeaa694d144` |
| `4e55c8f` (lane tip) | `refs/heads/ci/ngit-workflow-lane` | `manual` (maintainer-directed) | `success` | `5706173c272916c402f38b25a4a89b87e59a9eedf4304fd8855bc804082808c4` |
| `4e55c8f` (lane tip) | `refs/heads/ci/ngit-workflow-lane` | `push` | `success` | `a6c2d1ac5ff3e6c2741fe1e201931ef0d9ea24b6ff01fb0b1ce01bce937e2bc8` |

`4e55c8f` is the branch tip of the change that merged as `2ccbbb91`, so the
manual replay proves the maintainer-directed path on a plain `ci/<slug>` ref as
well as the push path on `main`. An earlier run at `de4c73d4` (before the
deselect) concluded `failure`; it is the source of the classification data in
the ignore list above.

Bind a result to a signer before quoting it:

```bash
# the kind-9842 event's own pubkey (must be the coordinator you trust)
nak req -k 9842 -l 200 wss://relay.ngit.dev wss://gitnostr.com \
  | python3 -c 'import sys,json
for l in sys.stdin:
    if l.startswith("{"):
        e=json.loads(l); t={x[0]:x[1:] for x in e["tags"] if x}
        if "merchant-routing-engine" in str(t.get("a")): print(e["pubkey"], t.get("conclusion"), t.get("c"))'

ngit ci status <commit>       # trust context + integrity ("commit present, workflow hash matches")
```
