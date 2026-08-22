# Cost-Gate Overrides (CG-4)

Scoped, TTL-bound, audited escape hatch for the percentile cost gate
(plan v2 §3, `docs/PLAN-cost-gate-reform-v2-2026-08-21.md`). Implementation:
`src/override_store.py` (tests: `tests/test_override_store.py`).

An override NEVER weakens the two hard blocks — freeze marker and
dead-or-locked-key always DENY, override or not (enforced in
`src.cost_gate.evaluate_cost_gate`, decision rows 1–2).

## Who

| Principal | May issue/revoke |
|---|---|
| `felix` | yes |
| `merchant-routing-cw` | yes |
| `orchestrator-cw` (manager profile) | yes |
| workers | **no** — `UnauthorizedPrincipal` before any I/O |

Principals compare case/space-insensitively. `ALLOWED_PRINCIPALS` is the
single allowlist; anything else raises before a byte is written.

## Scopes

One scope per grant, from `src.cost_gate.OVERRIDE_SCOPES` (same frozenset
the CG-1 gate validates against — no duplication):

| scope | rescues |
|---|---|
| `budget` | budget-cap DENY row |
| `price_history` | insufficient/aged price-history DENY row |
| `infra_down` | infra-down strict DENY (Q10 escape) |
| `paid_ceiling` | paid-tier ceiling DENY row |

A list of scopes is rejected at issue AND treated as invalid (fail closed)
if forged into the marker.

## TTL

- mandatory; default **4 h**, hard max **24 h** (`DEFAULT_TTL_SECONDS`,
  `MAX_TTL_SECONDS`).
- expiry enforced on every read: `expires_ts <= now` ⇒ inactive.
- no unlimited overrides exist. `parse_ttl("4h"|"30m"|"90s"|"3600")` for the
  CLI (CG-7 wires `--ttl` to it); rejects zero/negative/>24h/unparsable.

## Marker file

`~/.hermes/bot/.cost_gate_override` — JSON, exactly four fields:

```json
{"scope": "infra_down", "expires_ts": 1770000000.0,
 "issued_by": "felix", "reason": "proxy price feed restart window"}
```

- written atomically (temp + `os.replace`), mode 0600, parent dirs created.
- single active grant: a second issue while one is active is rejected.
- re-issue allowed after expiry or revoke.
- no code path edits the marker by hand — grants flow through
  `OverrideStore.issue_override` / `.revoke` (CG-7 CLI calls these).

## Audit (append-only)

`cost_gate_overrides` table (in the shared `zai_usage.db`) — one row per
event, `kind ∈ {issued, consumed, revoked}`, fields
`(ts, issued_by, scope, ttl_seconds, expires_ts, reason, consumed_at_ts, task, detail)`:

- **issued** — at grant time.
- **consumed** — for EVERY gate invocation that consumed the grant; carries
  `task` and `would_have_been` (the verdict the gate would have returned).
  Marker is NOT removed on consumption; the TTL governs lifetime.
- **revoked** — at revoke; marker removed.

Every event also mirrors into `anomaly_events` as
`category='cost_gate_override'`, `severity='INFO'` (same table
`promo_tier`/`oxalpha` use).

**Fail-closed ordering:** the audit rows commit BEFORE the marker appears.
If the audit DB is unreachable, issuing fails and no override exists — an
unaudited override is ungrantable.

## Fail-closed reads

`load_override()` never raises. Absent, unreadable (perms), corrupt
(non-JSON), wrong-shape, unknown/multi-scope, non-numeric-expiry and
expired markers all return `{"active": False, ...}` — the gate then sees no
override and DENYs as usual. Validity is decided by the same
`src.cost_gate.is_override_active` the gate uses.

## Usage

```python
from src.override_store import OverrideStore, parse_ttl

store = OverrideStore()                      # production paths
grant = store.issue_override(                # raises on any §3 violation
    scope="infra_down", issued_by="felix",
    reason="proxy price feed restart window",
    ttl_seconds=parse_ttl("4h"), now_ts=time.time())

loaded = store.load_override(now_ts=time.time())   # fail-closed read
verdict = evaluate_cost_gate(..., override=loaded["grant"])
if verdict.get("override_consumed"):
    store.consume_override(verdict["override_consumed"],
                           now_ts=time.time(), task="cron-x",
                           would_have_been=verdict["override_consumed"]["would_have_been"])

store.revoke(now_ts=time.time(), revoked_by="felix", reason="window over")
```

All paths and timestamps are injectable (`marker_path=`, `db_path=`,
`now_ts=`) — tests point both at tmp paths; the module never reads the wall
clock and never touches the network.
