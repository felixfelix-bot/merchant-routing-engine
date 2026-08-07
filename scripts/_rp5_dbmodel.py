import sqlite3, os
c = sqlite3.connect(os.path.expanduser('~/.hermes/bot/zai_usage.db'))
print('api_calls cols:', [r[1] for r in c.execute('PRAGMA table_info(api_calls)')])
rows = c.execute("SELECT model, COUNT(*), SUM(total_tokens) FROM api_calls WHERE key_name='ollama_cloud' GROUP BY model ORDER BY 3 DESC LIMIT 12").fetchall()
print('ollama_cloud models (model, calls, tokens):')
for r in rows: print('  ', r)
# 4-week total tokens for ollama_cloud (matches activity.period last_4_weeks)
import time
cutoff = time.time() - 28*86400
row = c.execute("SELECT SUM(total_tokens), COUNT(*) FROM api_calls WHERE key_name='ollama_cloud' AND ts >= ?", (cutoff,)).fetchone()
print(f'\nollama_cloud last 4 weeks: {row[0]} tokens, {row[1]} calls')
if row[0]:
    print(f'  activity.cost $60 / tokens -> ${60/(row[0]/1e6):.5f}/M')
c.close()
