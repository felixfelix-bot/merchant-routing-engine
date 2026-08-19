#!/usr/bin/env python3
"""Test the Kalman pricing export module."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from export_kalman_pricing import compute_kalman_pricing, _sat_per_usd, _dollars_per_m_to_sat_per_token

# Test conversion functions
assert _sat_per_usd(95000) == 100_000_000 / 95000
assert _dollars_per_m_to_sat_per_token(0.0, 1052.63) == 0.0
assert _dollars_per_m_to_sat_per_token(float('inf'), 1052.63) == 0.0
assert _dollars_per_m_to_sat_per_token(float('nan'), 1052.63) == 0.0
sat = _dollars_per_m_to_sat_per_token(1.0, 1052.63)
assert abs(sat - 0.00105263) < 1e-8, f"Expected ~0.00105, got {sat}"
print("Conversion functions: PASS")

# Test full pricing computation
data = compute_kalman_pricing(btc_price_usd=95000.0)
assert "generated_at" in data
assert "providers" in data
assert "sat_per_usd" in data
assert len(data["providers"]) == 6
for name, prov in data["providers"].items():
    assert "effective_rate_per_m" in prov
    assert "sat_per_token" in prov
    assert "source" in prov
    assert prov["effective_rate_per_m"] > 0
    assert prov["sat_per_token"] >= 0
print(f"Full computation: PASS ({len(data['providers'])} providers)")
print(f"  ours:          ${data['providers']['ours']['effective_rate_per_m']}/M, {data['providers']['ours']['sat_per_token']} sat/token")
print(f"  ollama_cloud:  ${data['providers']['ollama_cloud']['effective_rate_per_m']}/M, {data['providers']['ollama_cloud']['sat_per_token']} sat/token")
measured = sum(1 for p in data["providers"].values() if p["is_measured"])
print(f"  measured:      {measured} providers")
print("All Python tests: PASS")