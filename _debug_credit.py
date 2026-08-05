import sqlite3, tempfile, os, time, math

# Create a temp DB with deepinfra spend
fd, db_path = tempfile.mkstemp(suffix=".db")
os.close(fd)
conn = sqlite3.connect(db_path)
conn.execute("""CREATE TABLE api_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, key_name TEXT, model TEXT,
    total_tokens INTEGER DEFAULT 0,
    cost_usd REAL, cost_source TEXT
)""")
conn.execute(
    "INSERT INTO api_calls (ts, key_name, model, cost_usd) VALUES (?, 'deepinfra', 'glm-5.2', 4.5)",
    (time.time(),),
)
conn.commit()
conn.close()

# Test _compute_credit_pressure directly
import src.live_router as lr
lr._credit_spend_cache.clear()

# Enable the kill switch
lr._DEEPINFRA_CREDIT_PRESSURE_ENABLED = True

p = lr._compute_credit_pressure(
    db_path, "deepinfra", 5.0,
    onset=0.80, asymptote=1.5,
)
print(f"Direct _compute_credit_pressure result: {p}")

# Check the spend query
spend = lr._query_cumulative_spend(db_path, "deepinfra")
print(f"Cumulative spend: {spend}")

# Now test via LiveRouter
rates = {
    "ours": 0.001, "friend": 0.029, "ollama_cloud": 0.024,
    "ppq": 0.14, "openrouter": 0.135, "deepinfra": 1.30,
}
lr._credit_spend_cache.clear()
router = lr.LiveRouter(db_path=db_path, converged_rates=rates)
quota_state = {
    "ours":         {"used_pct": 100.0, "remaining": 0, "total": 2_000_000},
    "friend":       {"used_pct": 100.0, "remaining": 0, "total": 2_000_000},
    "ollama_cloud": {"used_pct": 100.0, "remaining": 0, "total": 500_000_000},
    "ppq":          {"used_pct": 0.0, "remaining": float("inf")},
    "openrouter":   {"used_pct": 0.0, "remaining": float("inf")},
    "deepinfra":    {"used_pct": 0.0, "remaining": float("inf")},
}
health = {k: True for k in quota_state}
health["ours"] = False
health["friend"] = False
health["ollama_cloud"] = False
result = router.select_failover(
    quota_state=quota_state, health_state=health,
    peak=False, model="glm-5.2",
)
print(f"_last_credit_pressures: {router._last_credit_pressures}")
print(f"provider_names: {router._provider_names}")
print(f"_DEEPINFRA_CREDIT_PRESSURE_ENABLED: {lr._DEEPINFRA_CREDIT_PRESSURE_ENABLED}")

os.unlink(db_path)
