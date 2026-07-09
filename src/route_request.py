"""route_request.py — Reference interface for the Kalman-based router.

This is a thin wrapper that documents the interface the production
route_request() function (in burn_predictor.py) implements. The actual
Kalman filter logic lives in ~/.hermes/bot/burn_predictor.py.

When the merchant-routing-engine is fully migrated (Phase 3), this
module will contain the actual implementation. For now it's a reference.

Production location: ~/.hermes/bot/burn_predictor.py route_request()
(lines ~524-691)
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class RouteDecision:
    """Result of a routing decision."""
    tier: str          # "zai" | "ppq" | "openrouter" | "ollama"
    key: str | None    # "ours" | "friend" | None
    model: str         # model ID
    base_url: str      # API endpoint
    reason: str        # human-readable explanation
    cost_estimate_usd: float
    effective_cost_per_1m: float = 0.0
    quality_score: float = 0.0
    is_peak_hour: bool = False
    resource_healthy: bool = True


def route_request(
    estimated_tokens: int = 0,
    difficulty: str = "medium",
    prefer_free: bool = True,
) -> RouteDecision:
    """Decide which key and model to use for a request.

    Production implementation in burn_predictor.py:
    1. Load model_matrix.json (646 models with live pricing)
    2. Check multi-resource health (CPU, memory, workers)
    3. Get Kalman predict_all() for both z.ai keys
    4. Filter candidates by quality threshold
    5. Compute effective_cost = base_cost × overage_penalty × resource_penalty
    6. Sort by (effective_cost ASC, quality DESC)
    7. Return cheapest qualifying model

    Args:
        estimated_tokens: Rough token count (0 = unknown)
        difficulty: "simple" (quality≥60), "medium" (≥75), "complex" (≥85)
        prefer_free: Strongly prefer free z.ai over paid PPQ/OpenRouter

    Returns:
        RouteDecision with provider, model, cost, and quality info
    """
    raise NotImplementedError(
        "See ~/.hermes/bot/burn_predictor.py route_request() for production implementation. "
        "This module will be migrated in Phase 2/3."
    )
