"""Tests for the NeuralWatt balance collector script.

Gate 1 (TDD): verifies the collector script:
1. Loads the API key from .env (not os.environ)
2. Runs without error when the API is reachable
3. Produces valid output (JSON with ok=True and real kWh fields)
4. Handles 401 errors gracefully (no crash)
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "collect_neuralwatt_balance.py"
DB_PATH = Path.home() / ".hermes" / "bot" / "api_burn.db"


def test_script_exists_and_is_executable():
    """The collector script must exist and be executable."""
    assert SCRIPT_PATH.is_file(), f"Script not found at {SCRIPT_PATH}"
    assert os.access(SCRIPT_PATH, os.X_OK), f"Script is not executable: {SCRIPT_PATH}"


def test_load_key_from_env_file():
    """_load_key_from_env_file should return a non-empty key from the .env file."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import collect_neuralwatt_balance
    key = collect_neuralwatt_balance._load_key_from_env_file()
    assert key is not None, "No NEURALWATT_API_KEY found in .env files"
    assert key.startswith("sk-"), f"Key should start with 'sk-', got: {key[:5]}..."
    assert len(key) > 10, f"Key too short: len={len(key)}"


def test_mask_key_does_not_leak_full_key():
    """_mask_key must never expose the full key."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import collect_neuralwatt_balance
    masked = collect_neuralwatt_balance._mask_key("sk-1234567890abcdefghijklmnopqrstuvwxyz")
    # The masked version should be significantly shorter than the original
    assert len(masked) < len("sk-1234567890abcdefghijklmnopqrstuvwxyz")
    # Should contain "..." indicating masking
    assert "..." in masked or "***" in masked


def test_script_runs_and_produces_valid_output():
    """Run the script end-to-end and verify it produces a valid JSON row.

    This is the real integration test — it hits the live NeuralWatt API
    with the correct key from .env and stores the result in api_burn.db.
    """
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode in (0, 1), f"Unexpected exit code: {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"

    # Parse the JSON output
    assert result.stdout.strip(), f"No stdout output. stderr: {result.stderr}"
    data = json.loads(result.stdout.strip())
    assert data["provider"] == "neuralwatt"

    if data.get("ok"):
        # Success — verify the fields
        assert "kwh_remaining" in data, "Missing kwh_remaining field"
        assert "kwh_included" in data, "Missing kwh_included field"
        assert "used_pct" in data, "Missing used_pct field"
        assert data["method"] == "real-api"
        # kwh_remaining should be a positive number (we have ~1.93 remaining)
        assert data["kwh_remaining"] is not None
        assert isinstance(data["kwh_remaining"], (int, float))
        assert data["kwh_remaining"] >= 0, f"kwh_remaining should be >= 0, got {data['kwh_remaining']}"

        # Verify a fresh row was written to the DB
        import sqlite3
        conn = sqlite3.connect(str(DB_PATH))
        try:
            row = conn.execute(
                "SELECT * FROM provider_balances WHERE provider='neuralwatt' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            assert row is not None, "No neuralwatt row found in provider_balances"
            # Row: (id, provider, collected_at, usage, limit_credits,
            #       limit_remaining, usage_fraction, is_unlimited, is_free_tier, raw_json)
            collected_at = row[2]
            age = time.time() - collected_at
            assert age < 60, f"Row is too old: {age:.1f}s (should be < 60s)"
            # Check raw_json has method='real-api'
            raw = json.loads(row[9])
            assert raw.get("method") == "real-api", f"Expected method='real-api', got: {raw.get('method')}"
        finally:
            conn.close()
    else:
        # If the API call failed (e.g., 401), the script should not crash
        # and should print a clear error message
        assert "error" in data, f"Failed but no error field: {data}"


def test_no_hardcoded_secrets():
    """Gate 2.5 (Cold review): verify no full API keys are hardcoded in the script.

    The docstring references the key mismatch (sk-76de... vs sk-d843...) for
    context, but the actual full keys (67 chars) must never appear in the code.
    """
    import re
    content = SCRIPT_PATH.read_text()
    # Check for full-length API keys (sk- + 60+ alphanumeric chars = 67 total)
    sk_matches = re.findall(r'sk-[a-zA-Z0-9]{60,}', content)
    assert not sk_matches, f"Found potential hardcoded full keys: {sk_matches}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])