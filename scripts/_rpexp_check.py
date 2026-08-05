"""RP-EXP sanity check for the new quota_pressure_factor curve."""
import math
from src.pricing_engine import quota_pressure_factor as q, EXTRA_USAGE_MULTIPLIER as A

K = A - 1.0
print(f"EXTRA_USAGE_MULTIPLIER = {A:.4f} | K = {K:.4f}")
print("--- gate values ---")
print(f"usage=0.50 -> {q(0.50)} (want 1.0)")
print(f"usage=0.70 -> {q(0.70)} (want 1.0)")
print(f"usage=0.99 -> {q(0.99):.2f} (want >10x)")
print(f"usage=1.00 -> {q(1.00)} (want inf)")
print(f"usage=1.10 -> {q(1.10)} (want inf)")
print("--- shape checkpoints ---")
for u in (0.70, 0.80, 0.85, 0.90, 0.95, 0.99):
    print(f"  u={u:.2f} -> {q(u):.3f}x  (${0.024 * q(u):.3f}/M)")
print(f"midpoint(0.85) == asymptote? {round(q(0.85), 4) == round(A, 4)}")
print(f"custom asymptote=8 at midpoint 0.85: {round(q(0.85, asymptote=8.0), 4)} (want 8.0)")
print(f"monotonic 0.70->0.99: {all(q(i / 100) < q((i + 1) / 100) for i in range(70, 99))}")
