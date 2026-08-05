"""RP-2: check deepinfra_balance deductions + look for any stored raw responses."""
from __future__ import annotations
import os
import sqlite3

DB = os.path.expanduser("~/.hermes/bot/zai_usage.db")


def main() -> None:
    db = sqlite3.connect(DB)

    print("=== deepinfra_balance ===")
    try:
        for r in db.execute("SELECT * FROM deepinfra_balance"):
            print(r)
    except Exception as e:
        print("err:", e)

    print("\n=== deepinfra api_calls (any) ===")
    try:
        for r in db.execute(
            "SELECT id, ts, key_name, model, total_tokens, cost_usd, cost_source "
            "FROM api_calls WHERE key_name='deepinfra' ORDER BY ts DESC LIMIT 10"
        ):
            print(r)
    except Exception as e:
        print("err:", e)

    print("\n=== openrouter api_calls (any) ===")
    try:
        for r in db.execute(
            "SELECT id, ts, key_name, model, total_tokens, cost_usd, cost_source "
            "FROM api_calls WHERE key_name='openrouter' ORDER BY ts DESC LIMIT 10"
        ):
            print(r)
    except Exception as e:
        print("err:", e)

    print("\n=== ppq api_calls (any) ===")
    try:
        for r in db.execute(
            "SELECT id, ts, key_name, model, total_tokens, cost_usd, cost_source "
            "FROM api_calls WHERE key_name='ppq' ORDER BY ts DESC LIMIT 10"
        ):
            print(r)
    except Exception as e:
        print("err:", e)

    print("\n=== ollama_cloud recent cost samples ===")
    try:
        for r in db.execute(
            "SELECT id, ts, model, total_tokens, cost_usd, cost_source "
            "FROM api_calls WHERE key_name='ollama_cloud' "
            "ORDER BY ts DESC LIMIT 5"
        ):
            print(r)
    except Exception as e:
        print("err:", e)

    db.close()


if __name__ == "__main__":
    main()
