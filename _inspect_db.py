import sqlite3, os, glob
for p in [os.path.expanduser('~/.hermes/bot/zai_usage.db')] + glob.glob('/tmp/*.db'):
    if os.path.exists(p):
        try:
            c = sqlite3.connect(p)
            cols = c.execute('PRAGMA table_info(api_calls)').fetchall()
            print("DB:", p)
            for col in cols:
                print('  ', col[1], col[2])
            for colname in ['key_name','provider','cost_source','model']:
                try:
                    vals = c.execute(f'SELECT DISTINCT {colname} FROM api_calls LIMIT 30').fetchall()
                    print(f'  DISTINCT {colname}:', sorted(set(v[0] for v in vals if v[0])))
                except Exception:
                    pass
            try:
                for prov in ('deepinfra','openrouter','ppq','ollama_cloud','ours','friend'):
                    for matchcol in ('key_name','provider'):
                        try:
                            row = c.execute(f"SELECT COUNT(*), COALESCE(SUM(cost_usd),0) FROM api_calls WHERE {matchcol}=?", (prov,)).fetchone()
                            if row[0]>0:
                                print(f"  SPEND {prov} (via {matchcol}): {row[0]} calls, ${row[1]:.4f}")
                        except Exception:
                            pass
            except Exception:
                pass
            c.close()
        except Exception as e:
            print(f'{p}: {e}')
