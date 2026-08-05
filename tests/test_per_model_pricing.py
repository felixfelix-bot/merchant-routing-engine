"""PM-T3 gate: per-model base rate wired into the failover path.

The kimi-k3 485x cost blindspot: kimi-k3 was priced at ollama_cloud's
$0.024/M flat blend instead of its real ~$7.53/M cost, so the optimizer
flooded traffic to an expensive model. T3 wires ``_resolve_model_rate()``
into ``_do_select_failover`` so each provider is priced by the requested
model's OWN measured rate when ``PER_MODEL_PRICING_ENABLED`` is on, and marks
a provider unreachable when it cannot serve the requested model. See
``docs/plan-per-model-pricing.md`` section 6 T3.

These tests spy on ``RoutingOptimizer.add_provider`` to capture the
``(base_rate, breaker_tripped)`` actually fed to the optimizer for each
provider — i.e. they verify what the optimizer *sees*, which is the gate.
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

# Ensure we can import src modules.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.live_router as lr
from src.live_router import LiveRouter
from src.routing_optimizer import RoutingOptimizer


# Per-model rate seeds shared across tests.
#   ours / friend / ollama_cloud  -> serve kimi-k3 explicitly at $7.53/M
#   ppq                           -> has a _default only (served via default)
#   openrouter                    -> NO kimi-k3 AND NO _default (cannot serve)
#   deepinfra                     -> has a _default (served via default)
PER_MODEL_RATES: dict[str, dict[str, float]] = {
    "ours":         {"kimi-k3": 7.53, "glm-5.2": 0.014, "_default": 0.014},
    "friend":       {"kimi-k3": 7.53, "_default": 0.029},
    "ollama_cloud": {"kimi-k3": 7.53, "_default": 0.024},
    "ppq":          {"_default": 0.14},
    "deepinfra":    {"deepseek-v4-flash": 1.30, "_default": 1.30},
    "openrouter":   {"some-other-model": 0.135},  # no _default, no kimi-k3
}

# Flat per-provider blend (the legacy path / the buggy pricing).
FLAT_RATES: dict[str, float] = {
    "ours": 0.001, "friend": 0.029, "ollama_cloud": 0.024,
    "ppq": 0.14, "openrouter": 0.135, "deepinfra": 1.30,
}


@pytest.fixture(autouse=True)
def _reset_singleton():
    """No leaked LiveRouter singleton between tests."""
    LiveRouter.reset_instance()
    yield
    LiveRouter.reset_instance()


@pytest.fixture
def tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


def _spy_optimizer(monkeypatch) -> dict[str, tuple[float, bool]]:
    """Wrap ``RoutingOptimizer.add_provider`` to capture, per provider, the
    ``(base_rate, breaker_tripped)`` the optimizer is fed. Still calls through
    so routing proceeds normally."""
    captured: dict[str, tuple[float, bool]] = {}
    orig = RoutingOptimizer.add_provider

    def _spy(self_opt, name, price_kalman, **kw):
        captured[name] = (
            float(price_kalman.base_rate),
            bool(kw.get("breaker_tripped", False)),
        )
        return orig(self_opt, name, price_kalman, **kw)

    monkeypatch.setattr(RoutingOptimizer, "add_provider", _spy)
    return captured


def _all_healthy() -> dict[str, bool]:
    return {n: True for n in FLAT_RATES}


def _quota_available() -> dict[str, object]:
    """Everyone has ample quota — price/health is the only differentiator."""
    return {
        "ours": {"remaining": 2_000_000, "total": 2_000_000},
        "friend": {"remaining": 2_000_000, "total": 2_000_000},
        "ollama_cloud": {"remaining": 400_000_000, "total": 500_000_000},
        "ppq": {"remaining": float("inf")},
        "openrouter": {"remaining": float("inf")},
        "deepinfra": {"remaining": float("inf")},
    }


# ── THE GATE ─────────────────────────────────────────────────────────────────


class TestPerModelFailoverGate:
    def test_optimizer_sees_real_kimi_k3_rate(self, tmp_db, monkeypatch):
        """THE GATE (PM-T3): with per-model pricing ON and model='kimi-k3',
        the optimizer is fed kimi-k3's real $7.53/M for providers that serve
        it — NOT the flat $0.024/M ollama blend that caused the blindspot."""
        monkeypatch.setattr(lr, "_PER_MODEL_PRICING_ENABLED", True)
        # Keep __init__ hermetic: don't hit the real resolver; seed directly.
        monkeypatch.setattr(
            lr, "_resolve_dynamic_base_rates_per_model", lambda dbp=None: {}
        )
        captured = _spy_optimizer(monkeypatch)

        router = LiveRouter(db_path=tmp_db, converged_rates=FLAT_RATES)
        router._base_rates_per_model = PER_MODEL_RATES

        router.select_failover(
            quota_state=_quota_available(),
            health_state=_all_healthy(),
            peak=False,
            model="kimi-k3",
        )
        # ours serves kimi-k3 at $7.53/M -> that is what the optimizer sees.
        assert "ours" in captured
        ours_rate = captured["ours"][0]
        assert ours_rate == pytest.approx(7.53, rel=1e-3), (
            f"optimizer saw ${ours_rate:.4f}/M for ours/kimi-k3, "
            f"expected $7.53/M — the per-model wiring (PM-T3) is broken"
        )
        # The whole point of the fix: NOT the flat $0.024/M blend.
        assert ours_rate != pytest.approx(0.024, abs=0.01)
        assert ours_rate > 1.0  # clearly expensive, not the cheap blend

    def test_provider_not_serving_model_is_unreachable(self, tmp_db, monkeypatch):
        """A provider that cannot serve the requested model (no model entry AND
        no ``_default``) is marked unreachable (breaker_tripped=True) so the
        optimizer filters it. A provider with a ``_default`` stays reachable
        (served via the provider default)."""
        monkeypatch.setattr(lr, "_PER_MODEL_PRICING_ENABLED", True)
        monkeypatch.setattr(
            lr, "_resolve_dynamic_base_rates_per_model", lambda dbp=None: {}
        )
        captured = _spy_optimizer(monkeypatch)

        router = LiveRouter(db_path=tmp_db, converged_rates=FLAT_RATES)
        router._base_rates_per_model = PER_MODEL_RATES

        router.select_failover(
            quota_state=_quota_available(),
            health_state=_all_healthy(),
            peak=False,
            model="kimi-k3",
        )
        # openrouter has NO kimi-k3 and NO _default -> must be tripped.
        assert captured["openrouter"][1] is True, (
            "openrouter cannot serve kimi-k3 (no entry, no _default) but was "
            "not marked unreachable — the not-served gate is broken"
        )
        # ppq has a _default -> served via default, stays reachable.
        assert captured["ppq"][1] is False
        # ours explicitly serves kimi-k3 -> reachable, priced at its rate.
        assert captured["ours"][1] is False
        assert captured["ours"][0] == pytest.approx(7.53, rel=1e-3)

    def test_kill_switch_off_uses_flat_blend(self, tmp_db, monkeypatch):
        """Backward compatibility: with the kill switch OFF, a seeded per-model
        dict is IGNORED — providers keep the flat per-provider effective rate.
        kimi-k3 is priced at the cheap blend (the old, buggy behavior) until an
        operator flips the switch. This is by design (plan section 2)."""
        monkeypatch.setattr(lr, "_PER_MODEL_PRICING_ENABLED", False)
        captured = _spy_optimizer(monkeypatch)

        router = LiveRouter(db_path=tmp_db, converged_rates=FLAT_RATES)
        # Seed per-model rates — they MUST be ignored with the switch off.
        router._base_rates_per_model = PER_MODEL_RATES

        router.select_failover(
            quota_state=_quota_available(),
            health_state=_all_healthy(),
            peak=False,
            model="kimi-k3",
        )
        # ours priced at the flat $0.001/M blend, NOT $7.53/M.
        assert captured["ours"][0] != pytest.approx(7.53, rel=1e-3)
        assert captured["ours"][0] < 1.0
        # openrouter reachable again — the not-served gate is per-model only.
        assert captured["openrouter"][1] is False

    def test_model_none_uses_flat_blend(self, tmp_db, monkeypatch):
        """Backward compatibility: even with the switch ON, model=None keeps the
        flat per-provider effective rate (the legacy path). Per-model pricing
        only activates when a concrete model is requested."""
        monkeypatch.setattr(lr, "_PER_MODEL_PRICING_ENABLED", True)
        monkeypatch.setattr(
            lr, "_resolve_dynamic_base_rates_per_model", lambda dbp=None: {}
        )
        captured = _spy_optimizer(monkeypatch)

        router = LiveRouter(db_path=tmp_db, converged_rates=FLAT_RATES)
        router._base_rates_per_model = PER_MODEL_RATES

        router.select_failover(
            quota_state=_quota_available(),
            health_state=_all_healthy(),
            peak=False,
            model=None,
        )
        assert captured["ours"][0] != pytest.approx(7.53, rel=1e-3)
        assert captured["ours"][0] < 1.0
