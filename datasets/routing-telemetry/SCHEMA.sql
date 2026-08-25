CREATE TABLE routing_shadow_decisions (
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
    -- P6-SHADOW: pressure-routing divergence columns (added via migration) ---
    pressure_provider TEXT,
    pressure_model TEXT,
    pressure_cost REAL,
    actual_cost REAL,
    divergence REAL,
    is_429 INTEGER DEFAULT 0,
    paid_provider INTEGER DEFAULT 0,
    -- PM-T6: per-model pricing columns (added via migration) ---
    requested_model TEXT,
    per_model_base_rate REAL,
    per_model_source TEXT,
    -- EUv2-7: quota regime at decision time (included/extra/exhausted) ---
    quota_regime TEXT
);
CREATE TABLE sqlite_sequence(name,seq);
CREATE TABLE rate_limit_samples (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        ts            REAL    NOT NULL,          -- epoch seconds
        inter_arrival REAL,                       -- seconds since previous 429
        consecutive   INTEGER NOT NULL DEFAULT 1, -- consecutive 429 streak
        wait_used     REAL,                       -- how long we slept before this 429
        source        TEXT    DEFAULT 'zai_proxy'
    );
CREATE TABLE api_calls (
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
            error TEXT,
            duration_ms INTEGER,
            cost_usd REAL DEFAULT NULL,
            cost_source TEXT DEFAULT NULL
        );
CREATE TABLE key_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            chosen_key TEXT,
            reason TEXT,
            ours_pct INTEGER,
            friend_pct INTEGER,
            ours_available INTEGER,
            friend_available INTEGER
        );
CREATE TABLE model_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            key_name TEXT,
            model TEXT,
            original_model TEXT,
            tier TEXT,
            base_tier TEXT,
            hint TEXT,
            reason TEXT,
            peak INTEGER,
            hours_left REAL,
            active_key TEXT
        );
CREATE TABLE key_health (
            key_name           TEXT PRIMARY KEY,
            healthy            INTEGER NOT NULL,
            failure_count      INTEGER NOT NULL DEFAULT 0,
            last_error_type    TEXT,
            disabled_manually  INTEGER NOT NULL DEFAULT 0,
            updated_ts         REAL NOT NULL
        );
CREATE TABLE provider_telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    provider TEXT NOT NULL,
    response_received INTEGER,
    response_valid INTEGER,
    latency_ms INTEGER,
    error_type TEXT,
    billed_tokens INTEGER,
    actual_tokens INTEGER,
    token_mismatch INTEGER,
    model TEXT
);
CREATE TABLE deepinfra_balance (id INTEGER PRIMARY KEY CHECK (id = 1),balance_usd REAL NOT NULL,last_updated REAL NOT NULL,total_deducted REAL DEFAULT 0.0,total_requests INTEGER DEFAULT 0);
CREATE TABLE daily_spend (date TEXT NOT NULL, tier TEXT NOT NULL, spend_usd REAL DEFAULT 0, call_count INTEGER DEFAULT 0, token_count INTEGER DEFAULT 0, PRIMARY KEY (date, tier));
CREATE TABLE kalman_samples (
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
        );
CREATE TABLE anomaly_events (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            ts        REAL NOT NULL,
            severity  TEXT NOT NULL,
            category  TEXT NOT NULL,
            title     TEXT,
            alerted   INTEGER DEFAULT 0,
            resolved  INTEGER DEFAULT 0
        );
CREATE TABLE price_observations (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts              REAL    NOT NULL,
                    provider        TEXT    NOT NULL,
                    model           TEXT,
                    rate_per_m      REAL    NOT NULL,
                    source          TEXT    NOT NULL,
                    is_measured     INTEGER NOT NULL,
                    confidence      REAL    DEFAULT 1.0,
                    sample_tokens   INTEGER,
                    sample_cost_usd REAL,
                    velocity        REAL    DEFAULT 0.0,
                    note            TEXT
                );
CREATE TABLE routing_profit (
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
);
CREATE TABLE routing_live_decisions (
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
);
CREATE TABLE circuit_breaker_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    board TEXT, task_id TEXT,
    action TEXT NOT NULL,   -- would_block|block|reclaim|board_degraded|board_frozen|reset|unfreeze|error
    streak INTEGER, mode TEXT,
    detail TEXT
);
CREATE TABLE ppq_daily_used (
    date TEXT PRIMARY KEY,
    spend_usd REAL NOT NULL DEFAULT 0,
    requests INTEGER NOT NULL DEFAULT 0,
    tokens INTEGER NOT NULL DEFAULT 0,
    storm_blocked INTEGER NOT NULL DEFAULT 0,
    hour_requests TEXT NOT NULL DEFAULT '{}',
    last_ts REAL NOT NULL DEFAULT 0
);
CREATE TABLE pressure_decisions ( id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, state TEXT, requested_model TEXT, would_serve_model TEXT, would_provider TEXT, interactive INTEGER, reason TEXT);
CREATE TABLE measured_rates (
    provider TEXT NOT NULL, model TEXT NOT NULL,
    sats_per_M REAL, usd_per_M REAL, btc_usd REAL,
    sats_spent REAL, prompt_tokens INTEGER, completion_tokens INTEGER,
    measured_at REAL NOT NULL, method TEXT DEFAULT 'live_probe', error TEXT
);
CREATE TABLE daily_spend_inflated_pre_rewrite(
  date TEXT,
  tier TEXT,
  spend_usd REAL,
  call_count INT,
  token_count INT
);
CREATE TABLE routing_profit_inflated_pre_rewrite(
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
);
CREATE TABLE api_calls_cost_inflated_pre_rewrite(
  id INT,
  ts REAL,
  key_name TEXT,
  model TEXT,
  prompt_tokens INT,
  completion_tokens INT,
  cost_usd REAL,
  cost_source TEXT,
  session_id TEXT
);
CREATE INDEX idx_api_calls_ts ON api_calls(ts);
CREATE INDEX idx_api_calls_key_model ON api_calls(key_name, model);
CREATE INDEX idx_key_decisions_ts ON key_decisions(ts);
CREATE INDEX idx_model_decisions_ts ON model_decisions(ts);
CREATE INDEX idx_telemetry_ts ON provider_telemetry(ts);
CREATE INDEX idx_telemetry_provider ON provider_telemetry(provider);
CREATE INDEX idx_kalman_samples_key_ts ON kalman_samples(key, ts);
CREATE INDEX idx_anomaly_unresolved
            ON anomaly_events(resolved, alerted);
CREATE INDEX idx_price_obs_provider_ts ON price_observations(provider, model, ts);
CREATE INDEX idx_anomaly_ts ON anomaly_events(ts);
CREATE INDEX idx_cb_events_ts ON circuit_breaker_events(ts);
CREATE INDEX idx_cb_events_task ON circuit_breaker_events(board, task_id, action);
