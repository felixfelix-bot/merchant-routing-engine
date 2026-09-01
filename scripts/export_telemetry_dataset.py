#!/usr/bin/env python3
"""
Export the scrubbed routing-telemetry dataset for GitHub Release publication.

Implements ADR-010 (docs/adr/ADR-010-binary-artifacts-via-releases-not-git.md):
the scrubbed SQLite DB + per-table CSVs are built OUTSIDE the repo and
published as GitHub Release assets; only text (this script, README.md,
SCHEMA.sql) is committed to git.

The source databases are LIVE production stores and are opened EXCLUSIVELY
read-only via SQLite URI "mode=ro". This script never writes to them and
never opens any other file under their directory.

Outputs (to --out-dir, default ~/tmp-telemetry-export-2026-09-01):
  scrubbed.db       new SQLite DB built from scratch with the scrubbed schema
  scrubbed.db.gz    gzip of the above (primary Release asset)
  <table>.csv       one CSV per scrubbed table (Release assets)

Fail-loud guarantees:
  * source schema drift (added/renamed/missing columns) aborts the export
  * any secret-pattern hit in scrubbed text data aborts the export (exit 1)
  * source-vs-scrubbed rowcount mismatch aborts the export

Deliberately EXCLUDED tables (see README "Excluded tables"):
  zai_usage.db:  model_decisions (empty), circuit_breaker_events
                 (kanban-board ops, not router), resource_metrics (host
                 monitor), task_duration_samples (worker durations),
                 ppq_daily_used (legacy daily rollup; superseded by
                 ppq_queries in api_burn.db)

Scrub applied during the copy (column drops / transforms):
  api_calls:        drop key_suffix, session_id, task_type; free-text
                    error -> error_type enum (broken_pipe|timeout|dns_error|
                    exhausted|auth|rate_limit|parse_error|other|none)
  api_calls_cost_inflated_pre_rewrite: drop session_id (consistency with
                    the api_calls scrub; table is an audit snapshot)
  anomaly_events:   drop detail (keep title + category)
  key_health:       drop last_failure_ts, backoff_until, backoff_seconds,
                    last_error_type
  provider_balances: drop raw_json
  balance_snapshots: drop raw, error
  ppq_queries:      drop api_key_id
"""

import argparse
import csv
import gzip
import os
import re
import shutil
import sqlite3
import sys

DEFAULT_ZAI_DB = os.path.expanduser("~/.hermes/bot/zai_usage.db")
DEFAULT_BURN_DB = os.path.expanduser("~/.hermes/bot/api_burn.db")
DEFAULT_OUT_DIR = os.path.expanduser("~/tmp-telemetry-export-2026-09-01")

BATCH = 50000


class ScrubError(RuntimeError):
    """Fatal, fail-loud export error."""


# --------------------------------------------------------------------------
# Table specs
# --------------------------------------------------------------------------
# Each spec: dest CREATE TABLE for scrubbed.db, dest_cols (insert order),
# exprs (SQL SELECT expressions aligned 1:1 with dest_cols) and src_cols
# (the EXACT expected column list of the source table — drift guard: any
# mismatch aborts the export rather than silently shipping a new column).


def _err_type_expr():
    """SQL expression: free-text api_calls.error -> error_type enum."""
    return (
        "CASE "
        "WHEN error IS NULL OR error = '' THEN 'none' "
        "WHEN lower(error) LIKE '%brokenpipe%' OR lower(error) LIKE '%broken pipe%' "
        "  THEN 'broken_pipe' "
        "WHEN lower(error) LIKE '%timed out%' OR lower(error) LIKE '%timeout%' "
        "  THEN 'timeout' "
        "WHEN lower(error) LIKE '%name not known%' "
        "     OR lower(error) LIKE '%name or service not known%' "
        "     OR lower(error) LIKE '%name resolution%' "
        "     OR lower(error) LIKE '%getaddrinfo%' THEN 'dns_error' "
        "WHEN lower(error) LIKE '%429%' OR lower(error) LIKE '%rate limit%' "
        "  THEN 'rate_limit' "
        "WHEN lower(error) LIKE '%exhaust%' THEN 'exhausted' "
        "WHEN lower(error) LIKE '%401%' OR lower(error) LIKE '%unauthorized%' "
        "     OR lower(error) LIKE '%auth%' THEN 'auth' "
        "WHEN lower(error) LIKE '%parse%' OR lower(error) LIKE '%json%' "
        "  THEN 'parse_error' "
        "ELSE 'other' END"
    )


def _plain(cols):
    return list(cols)


API_CALLS_DDL = """CREATE TABLE api_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    key_name TEXT,
    model TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    tier TEXT,
    cache_hit INTEGER DEFAULT 0,
    ollama_hit INTEGER DEFAULT 0,
    ppq_hit INTEGER DEFAULT 0,
    status_code INTEGER,
    -- enum: broken_pipe|timeout|dns_error|exhausted|auth|rate_limit|
    --      parse_error|other|none  (free-text error scrubbed)
    error_type TEXT,
    duration_ms INTEGER,
    cost_usd REAL DEFAULT NULL,
    cost_source TEXT DEFAULT NULL
)"""

KEY_DECISIONS_DDL = """CREATE TABLE key_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    chosen_key TEXT,
    reason TEXT,
    ours_pct INTEGER,
    friend_pct INTEGER,
    ours_available INTEGER,
    friend_available INTEGER
)"""

ROUTING_SHADOW_DDL = """CREATE TABLE routing_shadow_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    live_provider TEXT,
    live_model TEXT,
    shadow_provider TEXT,
    shadow_model TEXT,
    shadow_cost REAL,
    live_cost REAL,
    tokens INTEGER,
    agree INTEGER,
    reason TEXT,
    -- P6-SHADOW: pressure-routing divergence columns ---
    pressure_provider TEXT,
    pressure_model TEXT,
    pressure_cost REAL,
    actual_cost REAL,
    divergence REAL,
    is_429 INTEGER DEFAULT 0,
    paid_provider INTEGER DEFAULT 0,
    -- PM-T6: per-model pricing columns ---
    requested_model TEXT,
    per_model_base_rate REAL,
    per_model_source TEXT,
    -- EUv2-7: quota regime at decision time (included/extra/exhausted) ---
    quota_regime TEXT
)"""

ROUTING_LIVE_DDL = """CREATE TABLE routing_live_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    live_provider TEXT,
    live_model TEXT,
    shadow_provider TEXT,
    shadow_model TEXT,
    shadow_cost REAL,
    live_cost REAL,
    tokens INTEGER,
    agree INTEGER,
    reason TEXT,
    pace_mults TEXT
)"""

FLAT_ROUTER_SHADOW_DDL = """CREATE TABLE flat_router_shadow_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    best_key_choice TEXT,
    flat_router_top TEXT,
    flat_router_top_cost REAL,
    agreement INTEGER,
    model TEXT,
    candidate_list TEXT
)"""

PRESSURE_DDL = """CREATE TABLE pressure_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    state TEXT,              -- GREEN|AMBER|RED (enum-like)
    requested_model TEXT,
    would_serve_model TEXT,
    would_provider TEXT,
    interactive INTEGER,
    reason TEXT              -- enum-like (bg_kept, bg_downgraded_ollama, ...)
)"""

ROUTING_PROFIT_DDL = """CREATE TABLE routing_profit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    provider_used TEXT NOT NULL,
    effective_price REAL NOT NULL,
    next_best_price REAL,
    savings_per_1m REAL,
    estimated_tokens INTEGER,
    estimated_savings_usd REAL,
    is_peak_hour INTEGER,
    mode TEXT DEFAULT 'consumer'
)"""

ROUTING_PROFIT_INFLATED_DDL = """CREATE TABLE routing_profit_inflated_pre_rewrite (
    id INT,
    ts REAL,
    provider_used TEXT,
    effective_price REAL,
    next_best_price REAL,
    savings_per_1m REAL,
    estimated_tokens INT,
    estimated_savings_usd REAL,
    is_peak_hour INT,
    mode TEXT
)"""

PROVIDER_TELEMETRY_DDL = """CREATE TABLE provider_telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,          -- ISO-8601 text timestamps
    provider TEXT NOT NULL,
    response_received INTEGER,
    response_valid INTEGER,
    latency_ms INTEGER,
    error_type TEXT,
    billed_tokens INTEGER,
    actual_tokens INTEGER,
    token_mismatch INTEGER,
    model TEXT
)"""

KALMAN_DDL = """CREATE TABLE kalman_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    key TEXT NOT NULL,
    window TEXT,
    used_pct_observed REAL,
    projected_additional_pct REAL,
    projected_total_pct REAL,
    burn_rate_tph REAL,
    velocity_tph2 REAL,
    uncertainty REAL,
    exhausts_in_hours REAL,
    will_exhaust INTEGER,
    note TEXT
)"""

PRICE_OBS_DDL = """CREATE TABLE price_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    provider TEXT NOT NULL,
    model TEXT,
    rate_per_m REAL NOT NULL,
    source TEXT NOT NULL,
    is_measured INTEGER NOT NULL,
    confidence REAL DEFAULT 1.0,
    sample_tokens INTEGER,
    sample_cost_usd REAL,
    velocity REAL DEFAULT 0.0,
    note TEXT
)"""

MEASURED_RATES_DDL = """CREATE TABLE measured_rates (
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    sats_per_M REAL,
    usd_per_M REAL,
    btc_usd REAL,
    sats_spent REAL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    measured_at REAL NOT NULL,
    method TEXT DEFAULT 'live_probe',
    error TEXT
)"""

DAILY_SPEND_DDL = """CREATE TABLE daily_spend (
    date TEXT NOT NULL,
    tier TEXT NOT NULL,
    spend_usd REAL DEFAULT 0,
    call_count INTEGER DEFAULT 0,
    token_count INTEGER DEFAULT 0,
    PRIMARY KEY (date, tier)
)"""

DAILY_SPEND_INFLATED_DDL = """CREATE TABLE daily_spend_inflated_pre_rewrite (
    date TEXT,
    tier TEXT,
    spend_usd REAL,
    call_count INT,
    token_count INT
)"""

ANOMALY_DDL = """CREATE TABLE anomaly_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    severity TEXT NOT NULL,
    category TEXT NOT NULL,
    title TEXT,
    alerted INTEGER DEFAULT 0,
    resolved INTEGER DEFAULT 0
)"""

RATE_LIMIT_DDL = """CREATE TABLE rate_limit_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,              -- epoch seconds
    inter_arrival REAL,            -- seconds since previous 429
    consecutive INTEGER NOT NULL DEFAULT 1,  -- consecutive 429 streak
    wait_used REAL,                -- how long we slept before this 429
    source TEXT DEFAULT 'zai_proxy'
)"""

KEY_HEALTH_DDL = """CREATE TABLE key_health (
    key_name TEXT PRIMARY KEY,
    healthy INTEGER NOT NULL,
    failure_count INTEGER NOT NULL DEFAULT 0,
    disabled_manually INTEGER NOT NULL DEFAULT 0,
    updated_ts REAL NOT NULL
)"""

DEEPINFRA_DDL = """CREATE TABLE deepinfra_balance (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    balance_usd REAL NOT NULL,
    last_updated REAL NOT NULL,
    total_deducted REAL DEFAULT 0.0,
    total_requests INTEGER DEFAULT 0
)"""

API_CALLS_INFLATED_DDL = """CREATE TABLE api_calls_cost_inflated_pre_rewrite (
    id INT,
    ts REAL,
    key_name TEXT,
    model TEXT,
    prompt_tokens INT,
    completion_tokens INT,
    cost_usd REAL,
    cost_source TEXT
)"""

PROVIDER_BALANCES_DDL = """CREATE TABLE provider_balances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    collected_at REAL NOT NULL,
    usage REAL,
    limit_credits REAL,
    limit_remaining REAL,
    usage_fraction REAL NOT NULL,
    is_unlimited INTEGER NOT NULL,
    is_free_tier INTEGER
)"""

BALANCE_SNAPSHOTS_DDL = """CREATE TABLE balance_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    provider TEXT NOT NULL,
    balance_usd REAL,
    total_credits REAL,
    total_usage REAL,
    currency TEXT
)"""

PPQ_QUERIES_DDL = """CREATE TABLE ppq_queries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    total_tokens INTEGER,
    cost_usd REAL,
    query_type TEXT
)"""

api_calls_dest = [
    "id", "ts", "key_name", "model", "prompt_tokens", "completion_tokens",
    "total_tokens", "tier", "cache_hit", "ollama_hit", "ppq_hit",
    "status_code", "error_type", "duration_ms", "cost_usd", "cost_source",
]
anomaly_dest = ["id", "ts", "severity", "category", "title", "alerted", "resolved"]
key_health_dest = ["key_name", "healthy", "failure_count", "disabled_manually", "updated_ts"]
api_calls_inflated_dest = [
    "id", "ts", "key_name", "model", "prompt_tokens", "completion_tokens",
    "cost_usd", "cost_source",
]
provider_balances_dest = [
    "id", "provider", "collected_at", "usage", "limit_credits",
    "limit_remaining", "usage_fraction", "is_unlimited", "is_free_tier",
]
balance_snapshots_dest = [
    "id", "ts", "provider", "balance_usd", "total_credits", "total_usage",
    "currency",
]
ppq_queries_dest = [
    "id", "ts", "model", "input_tokens", "output_tokens", "total_tokens",
    "cost_usd", "query_type",
]


def spec(name, db, ddl, dest_cols, src_cols, exprs=None):
    return {
        "table": name,
        "db": db,
        "ddl": ddl,
        "dest_cols": dest_cols,
        "exprs": exprs if exprs is not None else list(dest_cols),
        "src_cols": list(src_cols),
    }


TABLES = [
    # ---- zai_usage.db ----
    spec(
        "api_calls", "zai", API_CALLS_DDL, api_calls_dest,
        src_cols=[
            "id", "ts", "key_name", "key_suffix", "model", "prompt_tokens",
            "completion_tokens", "total_tokens", "tier", "cache_hit",
            "ollama_hit", "ppq_hit", "status_code", "error", "duration_ms",
            "cost_usd", "cost_source", "session_id", "task_type",
        ],
        exprs=[
            "id", "ts", "key_name", "model", "prompt_tokens",
            "completion_tokens", "total_tokens", "tier", "cache_hit",
            "ollama_hit", "ppq_hit", "status_code",
            _err_type_expr() + " AS error_type",
            "duration_ms", "cost_usd", "cost_source",
        ],
    ),
    spec(
        "key_decisions", "zai", KEY_DECISIONS_DDL, _plain(
            ["id", "ts", "chosen_key", "reason", "ours_pct", "friend_pct",
             "ours_available", "friend_available"]),
        src_cols=["id", "ts", "chosen_key", "reason", "ours_pct",
                  "friend_pct", "ours_available", "friend_available"],
    ),
    spec(
        "routing_shadow_decisions", "zai", ROUTING_SHADOW_DDL, _plain(
            ["id", "ts", "live_provider", "live_model", "shadow_provider",
             "shadow_model", "shadow_cost", "live_cost", "tokens", "agree",
             "reason", "pressure_provider", "pressure_model",
             "pressure_cost", "actual_cost", "divergence", "is_429",
             "paid_provider", "requested_model", "per_model_base_rate",
             "per_model_source", "quota_regime"]),
        src_cols=[
            "id", "ts", "live_provider", "live_model", "shadow_provider",
            "shadow_model", "shadow_cost", "live_cost", "tokens", "agree",
            "reason", "pressure_provider", "pressure_model",
            "pressure_cost", "actual_cost", "divergence", "is_429",
            "paid_provider", "requested_model", "per_model_base_rate",
            "per_model_source", "quota_regime",
        ],
    ),
    spec(
        "routing_live_decisions", "zai", ROUTING_LIVE_DDL, _plain(
            ["id", "ts", "live_provider", "live_model", "shadow_provider",
             "shadow_model", "shadow_cost", "live_cost", "tokens", "agree",
             "reason", "pace_mults"]),
        src_cols=[
            "id", "ts", "live_provider", "live_model", "shadow_provider",
            "shadow_model", "shadow_cost", "live_cost", "tokens", "agree",
            "reason", "pace_mults",
        ],
    ),
    spec(
        "flat_router_shadow_decisions", "zai", FLAT_ROUTER_SHADOW_DDL, _plain(
            ["id", "ts", "best_key_choice", "flat_router_top",
             "flat_router_top_cost", "agreement", "model", "candidate_list"]),
        src_cols=[
            "id", "ts", "best_key_choice", "flat_router_top",
            "flat_router_top_cost", "agreement", "model", "candidate_list",
        ],
    ),
    spec(
        "pressure_decisions", "zai", PRESSURE_DDL, _plain(
            ["id", "ts", "state", "requested_model", "would_serve_model",
             "would_provider", "interactive", "reason"]),
        src_cols=[
            "id", "ts", "state", "requested_model", "would_serve_model",
            "would_provider", "interactive", "reason",
        ],
    ),
    spec(
        "routing_profit", "zai", ROUTING_PROFIT_DDL, _plain(
            ["id", "ts", "provider_used", "effective_price",
             "next_best_price", "savings_per_1m", "estimated_tokens",
             "estimated_savings_usd", "is_peak_hour", "mode"]),
        src_cols=[
            "id", "ts", "provider_used", "effective_price",
            "next_best_price", "savings_per_1m", "estimated_tokens",
            "estimated_savings_usd", "is_peak_hour", "mode",
        ],
    ),
    spec(
        "routing_profit_inflated_pre_rewrite", "zai",
        ROUTING_PROFIT_INFLATED_DDL, _plain(
            ["id", "ts", "provider_used", "effective_price",
             "next_best_price", "savings_per_1m", "estimated_tokens",
             "estimated_savings_usd", "is_peak_hour", "mode"]),
        src_cols=[
            "id", "ts", "provider_used", "effective_price",
            "next_best_price", "savings_per_1m", "estimated_tokens",
            "estimated_savings_usd", "is_peak_hour", "mode",
        ],
    ),
    spec(
        "provider_telemetry", "zai", PROVIDER_TELEMETRY_DDL, _plain(
            ["id", "ts", "provider", "response_received", "response_valid",
             "latency_ms", "error_type", "billed_tokens", "actual_tokens",
             "token_mismatch", "model"]),
        src_cols=[
            "id", "ts", "provider", "response_received", "response_valid",
            "latency_ms", "error_type", "billed_tokens", "actual_tokens",
            "token_mismatch", "model",
        ],
    ),
    spec(
        "kalman_samples", "zai", KALMAN_DDL, _plain(
            ["id", "ts", "key", "window", "used_pct_observed",
             "projected_additional_pct", "projected_total_pct",
             "burn_rate_tph", "velocity_tph2", "uncertainty",
             "exhausts_in_hours", "will_exhaust", "note"]),
        src_cols=[
            "id", "ts", "key", "window", "used_pct_observed",
            "projected_additional_pct", "projected_total_pct",
            "burn_rate_tph", "velocity_tph2", "uncertainty",
            "exhausts_in_hours", "will_exhaust", "note",
        ],
    ),
    spec(
        "price_observations", "zai", PRICE_OBS_DDL, _plain(
            ["id", "ts", "provider", "model", "rate_per_m", "source",
             "is_measured", "confidence", "sample_tokens",
             "sample_cost_usd", "velocity", "note"]),
        src_cols=[
            "id", "ts", "provider", "model", "rate_per_m", "source",
            "is_measured", "confidence", "sample_tokens",
            "sample_cost_usd", "velocity", "note",
        ],
    ),
    spec(
        "measured_rates", "zai", MEASURED_RATES_DDL, _plain(
            ["provider", "model", "sats_per_M", "usd_per_M", "btc_usd",
             "sats_spent", "prompt_tokens", "completion_tokens",
             "measured_at", "method", "error"]),
        src_cols=[
            "provider", "model", "sats_per_M", "usd_per_M", "btc_usd",
            "sats_spent", "prompt_tokens", "completion_tokens",
            "measured_at", "method", "error",
        ],
    ),
    spec(
        "daily_spend", "zai", DAILY_SPEND_DDL, _plain(
            ["date", "tier", "spend_usd", "call_count", "token_count"]),
        src_cols=["date", "tier", "spend_usd", "call_count", "token_count"],
    ),
    spec(
        "daily_spend_inflated_pre_rewrite", "zai",
        DAILY_SPEND_INFLATED_DDL, _plain(
            ["date", "tier", "spend_usd", "call_count", "token_count"]),
        src_cols=["date", "tier", "spend_usd", "call_count", "token_count"],
    ),
    spec(
        "anomaly_events", "zai", ANOMALY_DDL, anomaly_dest,
        src_cols=[
            "id", "ts", "severity", "category", "title", "detail",
            "alerted", "resolved",
        ],
        exprs=["id", "ts", "severity", "category", "title", "alerted",
               "resolved"],  # 'detail' dropped
    ),
    spec(
        "rate_limit_samples", "zai", RATE_LIMIT_DDL, _plain(
            ["id", "ts", "inter_arrival", "consecutive", "wait_used",
             "source"]),
        src_cols=[
            "id", "ts", "inter_arrival", "consecutive", "wait_used",
            "source",
        ],
    ),
    spec(
        "key_health", "zai", KEY_HEALTH_DDL, key_health_dest,
        src_cols=[
            "key_name", "healthy", "failure_count", "last_failure_ts",
            "last_error_type", "backoff_until", "disabled_manually",
            "backoff_seconds", "updated_ts",
        ],
        exprs=["key_name", "healthy", "failure_count", "disabled_manually",
               "updated_ts"],  # last_failure_ts/backoff_until/backoff_seconds/
                               # last_error_type dropped
    ),
    spec(
        "deepinfra_balance", "zai", DEEPINFRA_DDL, _plain(
            ["id", "balance_usd", "last_updated", "total_deducted",
             "total_requests"]),
        src_cols=[
            "id", "balance_usd", "last_updated", "total_deducted",
            "total_requests",
        ],
    ),
    spec(
        "api_calls_cost_inflated_pre_rewrite", "zai",
        API_CALLS_INFLATED_DDL, api_calls_inflated_dest,
        src_cols=[
            "id", "ts", "key_name", "model", "prompt_tokens",
            "completion_tokens", "cost_usd", "cost_source", "session_id",
        ],
        exprs=[
            "id", "ts", "key_name", "model", "prompt_tokens",
            "completion_tokens", "cost_usd", "cost_source",
        ],  # session_id dropped (same scrub convention as api_calls)
    ),
    # ---- api_burn.db ----
    spec(
        "provider_balances", "burn", PROVIDER_BALANCES_DDL,
        provider_balances_dest,
        src_cols=[
            "id", "provider", "collected_at", "usage", "limit_credits",
            "limit_remaining", "usage_fraction", "is_unlimited",
            "is_free_tier", "raw_json",
        ],
        exprs=provider_balances_dest,  # raw_json dropped
    ),
    spec(
        "balance_snapshots", "burn", BALANCE_SNAPSHOTS_DDL,
        balance_snapshots_dest,
        src_cols=[
            "id", "ts", "provider", "balance_usd", "total_credits",
            "total_usage", "currency", "raw", "error",
        ],
        exprs=balance_snapshots_dest,  # raw + error dropped
    ),
    spec(
        "ppq_queries", "burn", PPQ_QUERIES_DDL, ppq_queries_dest,
        src_cols=[
            "id", "ts", "model", "input_tokens", "output_tokens",
            "total_tokens", "cost_usd", "query_type", "api_key_id",
        ],
        exprs=ppq_queries_dest,  # api_key_id dropped
    ),
]

# Indexes recreated in scrubbed.db (dropped-column indexes and indexes on
# excluded tables are intentionally NOT recreated).
INDEXES = [
    "CREATE INDEX idx_api_calls_ts ON api_calls(ts)",
    "CREATE INDEX idx_api_calls_key_model ON api_calls(key_name, model)",
    "CREATE INDEX idx_key_decisions_ts ON key_decisions(ts)",
    "CREATE INDEX idx_telemetry_ts ON provider_telemetry(ts)",
    "CREATE INDEX idx_telemetry_provider ON provider_telemetry(provider)",
    "CREATE INDEX idx_kalman_samples_key_ts ON kalman_samples(key, ts)",
    "CREATE INDEX idx_anomaly_unresolved ON anomaly_events(resolved, alerted)",
    "CREATE INDEX idx_anomaly_ts ON anomaly_events(ts)",
    "CREATE INDEX idx_price_obs_provider_ts ON price_observations(provider, model, ts)",
    "CREATE INDEX idx_snap_ts ON balance_snapshots(ts)",
    "CREATE INDEX idx_snap_provider ON balance_snapshots(provider, ts)",
    "CREATE INDEX idx_ppq_queries_ts ON ppq_queries(ts)",
    "CREATE UNIQUE INDEX idx_ppq_queries_dedup ON ppq_queries(ts, model, total_tokens)",
    "CREATE INDEX idx_provider_balances_provider_time ON provider_balances(provider, collected_at DESC)",
]

# --------------------------------------------------------------------------
# Secret scan patterns (fail-loud). hex64 is also hard-fail; the
# KNOWN_HASH_COLUMNS set lists columns that legitimately store hex64 hashes
# (warning-only there). Current dataset has no such columns.
# --------------------------------------------------------------------------
SECRET_PATTERNS = {
    "api-key (sk-...)": re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    "nostr secret (nsec1)": re.compile(r"nsec1"),
    "bearer token": re.compile(r"Bearer ", re.IGNORECASE),
    "email address": re.compile(
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "64-char hex": re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])"),
}
KNOWN_HASH_COLUMNS = set()  # (table, column) pairs that may hold hex64 hashes


def ro_connect(path):
    """Open a database STRICTLY read-only (SQLite URI mode=ro)."""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True,
                           isolation_level=None)


def verify_source_schema(conn, spec):
    actual = [r[1] for r in conn.execute(f'PRAGMA table_info("{spec["table"]}")')]
    if actual != spec["src_cols"]:
        raise ScrubError(
            f"source schema drift for {spec['db']}:{spec['table']} — "
            f"expected columns {spec['src_cols']}, found {actual}. "
            "Update the TABLES spec (and scrub rules) before exporting.")


def copy_table(src, dest, spec):
    """Copy one table with scrub transforms. Returns (source_rowcount)."""
    t = spec["table"]
    verify_source_schema(src, spec)

    src.execute("BEGIN")  # consistent snapshot for COUNT + SELECT
    try:
        src_count = src.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        col_exprs = ", ".join(spec["exprs"])
        cur = src.execute(f'SELECT {col_exprs} FROM "{t}"')
        dest_cols = ", ".join(f'"{c}"' for c in spec["dest_cols"])
        placeholders = ", ".join("?" for _ in spec["dest_cols"])
        insert_sql = (f'INSERT INTO "{t}" ({dest_cols}) '
                      f"VALUES ({placeholders})")
        inserted = 0
        while True:
            rows = cur.fetchmany(BATCH)
            if not rows:
                break
            dest.executemany(insert_sql, rows)
            inserted += len(rows)
        src.execute("COMMIT")
    except Exception:
        try:
            src.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise

    if inserted != src_count:
        raise ScrubError(
            f"{t}: copied {inserted} rows but source had {src_count} "
            "(live DB moved mid-copy?)")
    return src_count


def secret_scan(conn):
    """Scan every text-typed cell of every scrubbed table. Fail loud."""
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    hits, warnings, scanned = [], [], 0
    for t in tables:
        cols = [(r[1], r[2] or "") for r in conn.execute(f'PRAGMA table_info("{t}")')]
        for col, decltype in cols:
            scanned += 1
            for (val,) in conn.execute(
                    f'SELECT "{col}" FROM "{t}" WHERE typeof("{col}") = \'text\''):
                if not val:
                    continue
                for label, pat in SECRET_PATTERNS.items():
                    if pat.search(val):
                        if label == "64-char hex" and (t, col) in KNOWN_HASH_COLUMNS:
                            warnings.append((t, col, label, val[:40]))
                        else:
                            hits.append((t, col, label, val[:40]))
    if warnings:
        for t, c, lbl, v in warnings:
            print(f"  [secret-scan WARNING] {t}.{c}: {lbl} in known hash "
                  f"column: {v!r}")
    if hits:
        print("SECRET SCAN FAILED — do NOT publish:")
        for t, c, lbl, v in hits:
            print(f"  HIT {t}.{c} [{lbl}]: {v!r}")
        raise ScrubError(f"secret scan: {len(hits)} hard pattern hit(s)")
    print(f"secret scan: clean (0 hits across {len(tables)} tables, "
          f"{scanned} table-columns, patterns: {', '.join(SECRET_PATTERNS)})")


def export_csvs(conn, out_dir):
    csvs = []
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    for t in tables:
        path = os.path.join(out_dir, f"{t}.csv")
        cur = conn.execute(f'SELECT * FROM "{t}"')
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow([d[0] for d in cur.description])
            while True:
                rows = cur.fetchmany(BATCH)
                if not rows:
                    break
                w.writerows(rows)
        csvs.append(path)
    return csvs


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n} B"


def main():
    ap = argparse.ArgumentParser(
        description="Export scrubbed routing-telemetry dataset (ADR-010).")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    ap.add_argument("--zai-db", default=DEFAULT_ZAI_DB)
    ap.add_argument("--burn-db", default=DEFAULT_BURN_DB)
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing scrubbed.db in --out-dir")
    args = ap.parse_args()

    for p in (args.zai_db, args.burn_db):
        if not os.path.exists(p):
            raise ScrubError(f"source database not found: {p}")
    os.makedirs(args.out_dir, exist_ok=True)
    db_path = os.path.join(args.out_dir, "scrubbed.db")
    gz_path = os.path.join(args.out_dir, "scrubbed.db.gz")
    if os.path.exists(db_path) and not args.force:
        raise ScrubError(f"{db_path} already exists (use --force to replace)")
    for stale in (db_path, gz_path):
        if os.path.exists(stale):
            os.remove(stale)

    src = {
        "zai": ro_connect(args.zai_db),
        "burn": ro_connect(args.burn_db),
    }
    print(f"sources (read-only, mode=ro):")
    for k, c in src.items():
        print(f"  {k}: {args.zai_db if k == 'zai' else args.burn_db} "
              f"(sqlite {c.execute('select sqlite_version()').fetchone()[0]})")

    dest = sqlite3.connect(db_path)
    dest.execute("PRAGMA foreign_keys=ON")
    counts = {}
    try:
        for s in TABLES:
            dest.execute(s["ddl"])
        for s in TABLES:
            counts[s["table"]] = copy_table(src[s["db"]], dest, s)
            dest.commit()
        for idx in INDEXES:
            dest.execute(idx)
        dest.commit()

        # reclaim space (drops must actually shrink the file)
        print("vacuuming scrubbed.db ...")
        dest.execute("VACUUM")
        dest.close()

        # ---- verification pass on the scrubbed DB (opened read-only) ----
        chk = ro_connect(db_path)
        integrity = chk.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ScrubError(f"integrity_check failed: {integrity}")
        print(f"integrity_check: ok")

        print("\nSECRET SCAN (scrubbed.db):")
        secret_scan(chk)

        print("\nROWCOUNTS (source@copy-time vs scrubbed):")
        print(f"  {'table':<42} {'src':>9} {'scrubbed':>9}  match")
        total = 0
        for s in TABLES:
            t = s["table"]
            n = chk.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            total += n
            match = "OK" if n == counts[t] else "MISMATCH"
            if n != counts[t]:
                raise ScrubError(
                    f"{t}: source {counts[t]} rows, scrubbed {n} rows")
            print(f"  {t:<42} {counts[t]:>9} {n:>9}  {match}")
        print(f"  {'TOTAL':<42} {'':>9} {total:>9}")

        mn, mx = chk.execute(
            "SELECT datetime(MIN(ts),'unixepoch'), datetime(MAX(ts),'unixepoch') "
            "FROM api_calls").fetchone()
        print(f"\napi_calls window: {mn} .. {mx} (UTC)")

        print("\napi_calls error_type distribution:")
        for et, n in chk.execute(
                "SELECT error_type, COUNT(*) FROM api_calls "
                "GROUP BY error_type ORDER BY 2 DESC"):
            print(f"  {et or '<NULL>':<14} {n:>8}")

        # ---- artifacts ----
        csvs = export_csvs(chk, args.out_dir)
        chk.close()

        with open(db_path, "rb") as f_in, gzip.GzipFile(
                gz_path, "wb", compresslevel=9, mtime=0) as f_out:
            shutil.copyfileobj(f_in, f_out)

        print("\nARTIFACTS:")
        for p in [gz_path] + sorted(csvs):
            print(f"  {os.path.basename(p):<48} {human(os.path.getsize(p)):>10}")
        print(f"  scrubbed.db (uncompressed, not published)       "
              f"{human(os.path.getsize(db_path)):>10}")
        print(f"\nDONE — {len(csvs)} CSVs + scrubbed.db.gz in {args.out_dir}")
    finally:
        for c in src.values():
            c.close()
        try:
            dest.close()
        except sqlite3.Error:
            pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ScrubError as e:
        print(f"\nEXPORT ABORTED: {e}", file=sys.stderr)
        sys.exit(1)