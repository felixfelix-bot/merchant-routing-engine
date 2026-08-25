# CG-5 — task_type Logging in the Proxy

**Plan ref:** `docs/PLAN-cost-gate-reform-v2-2026-08-21.md` §CG-5
**Task:** `t_852bbe0d` · **Date:** 2026-08-21 · **Status:** REVIEW

The proxy now records a caller-declared **task_type** on every `api_calls`
row, enabling per-task cost attribution (CG-6 slice of the cost-gate-reform-v2
plan) without any behavior change to request forwarding.

## What changed

| Piece | Location | Change |
|---|---|---|
| Schema | `zai_proxy.py` `CREATE TABLE api_calls` + idempotent `ALTER` | new nullable `task_type TEXT DEFAULT NULL` column; no index, no backfill |
| Extraction | `zai_proxy.py` `_extract_task_type` / `_resolve_task_type` | `X-Task-Type` header (wins) over body `task_type` field; never guessed |
| Threading | `_proxy()` sets `self._task_type` next to `self._session_id` | available to every logging path in the request lifetime |
| Logging | all 4 `_log_api_call` call sites pass `task_type=` | z.ai primary (`do_POST` finally), ollama_cloud, telnyx, external failover |
| Fallback chain | `_log_api_call` | pre-CG-5 DB conns retry without `task_type`, then without `session_id`, then base columns — logging never breaks |

## Contract for callers (CW / agents)

- **Header (preferred):** `X-Task-Type: <string>` on the chat-completions
  request. Case-insensitive; value is stripped; empty/whitespace = unset.
- **Body fallback:** `{"task_type": "<string>", ...}`. Only plain strings
  count — numbers, booleans, null, lists, objects are treated as unset.
- **Precedence:** header wins over body when both are present.
- **Unknown/unset:** logged as SQL `NULL`. The proxy NEVER guesses, derives,
  or backfills a task type.
- **Forwarding:** the header is consumed for logging only; the request body
  is forwarded byte-identical (a body `task_type` field passes through to
  the upstream provider untouched).
- **Trust boundary:** loopback-only proxy, same trust model as
  `X-Hermes-Session` / `X-Model-Tier`.

Example:

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Task-Type: code-review" \
  -d '{"model": "glm-4.5-flash", "task_type": "ignored-when-header-set", ...}'
# api_calls row: task_type = 'code-review'
```

## Semantics consumers can rely on

- `task_type IS NULL` for all rows with `ts < deploy time` (no backfill).
- `task_type IS NULL` for any request whose caller set nothing (pre-upgrade
  callers, curl probes, health checks).
- One row per request attempt — a failover hop logs `tier = <provider>` with
  the ORIGINATING request's task_type/session_id.
- `SELECT` patterns: `WHERE task_type IS NOT NULL` gives exactly
  attributed spend; group by `task_type` for per-task cost.

## Tests

`tests/test_task_type_logging.py` — 30 tests, all loading the PRODUCTION
`~/.hermes/bot/zai_proxy.py` via importlib (same pattern as
`tests/test_provider_telemetry.py`; throwaway SQLite files only):

1. **Schema migration** (5) — fresh + legacy + pre-session-id DBs get the
   column; history stays NULL; idempotent re-connect.
2. **`_resolve_task_type`** (13) — precedence, case-insensitivity, stripping,
   whitespace/empty handling, non-string rejection, garbage never raises.
3. **`_log_api_call` round-trip** (5) — value lands in the row; NULL when
   absent; legacy conns without the column still log (fallback chain).
4. **Failover hop** (2) — behavioral: a request that fails over to an
   external provider carries the task_type through, and the forwarded body
   is untouched; absent task_type → NULL row.
5. **Call-site contract** (2) — AST guard: every current or future
   `_log_api_call` site must pass `task_type=`.
6. **No behavior change** (2) — plain requests resolve to None; row shape
   of legacy columns is identical.

## Production deployment

```bash
# Backup (done pre-edit)
cp ~/.hermes/bot/zai_proxy.py ~/.hermes/bot/zai_proxy.py.bak-cg5-task-type

# Compile gate (must print nothing / exit 0)
python3 -m py_compile ~/.hermes/bot/zai_proxy.py

# Deploy
systemctl --user restart zai-proxy
systemctl --user is-active zai-proxy   # expect: active
```

Migration is automatic and lazy: on first connect after restart, the proxy
`ALTER TABLE api_calls ADD COLUMN task_type TEXT` (idempotent try/except,
same pattern as the earlier `session_id` migration). Existing rows keep
`NULL` by design.

## Revert

```bash
cp ~/.hermes/bot/zai_proxy.py.bak-cg5-task-type ~/.hermes/bot/zai_proxy.py
systemctl --user restart zai-proxy
```

No DB revert needed — the extra `task_type` column is ignored by the old
code path's INSERT (which doesn't name it), and the old `_log_api_call`
never reads it. Leaving the column in place is safe.

## Follow-ups (out of scope here)

- CG-6 consumer: per-task spend dashboards / cost-gate decisions keyed on
  `task_type`.
- Agents/callers (e.g. Claude-Worker) adoption: send `X-Task-Type` per
  plan §CW; adoption tracked separately.
