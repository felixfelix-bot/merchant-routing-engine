"""external_failover.py — Dynamic cheapest-funded external provider failover.

When z.ai keys are exhausted, forward to the cheapest funded external
provider (PPQ or OpenRouter). Providers are sorted by cost — no hardcoded
order. On 402, provider is marked unfunded and the next cheapest is tried.

Extracted from ~/.hermes/bot/zai_proxy.py (Phase 1 — standalone copy).
"""
from __future__ import annotations
import json
import urllib.request
import urllib.error

try:
    from .provider_funding_tracker import is_provider_funded, mark_unfunded, mark_funded
    from .reasoning_handler import is_content_empty
except ImportError:
    from provider_funding_tracker import is_provider_funded, mark_unfunded, mark_funded
    from reasoning_handler import is_content_empty


FALLBACK_MODEL = "deepseek/deepseek-v4-flash"


def get_cheapest_funded(providers: dict, model_cost_fn=None) -> list[tuple[float, str, dict]]:
    """Return funded providers sorted by cost (cheapest first).

    Args:
        providers: Dict of {name: {base_url, key}}
        model_cost_fn: Optional function(name, model) -> float for cost lookup

    Returns:
        List of (cost, name, provider_config) sorted by cost ascending
    """
    candidates = []
    for name, prov in providers.items():
        if not prov.get("key"):
            continue
        if not is_provider_funded(name):
            continue
        cost = model_cost_fn(name, FALLBACK_MODEL) if model_cost_fn else 0.28
        candidates.append((cost, name, prov))

    candidates.sort(key=lambda c: c[0])
    return candidates


def try_external_failover(
    body: bytes,
    providers: dict,
    model_cost_fn=None,
    timeout: int = 180,
) -> tuple[bool, bytes | None, str | None]:
    """Try forwarding to the cheapest funded external provider.

    Args:
        body: Original request body (JSON bytes)
        providers: Dict of {name: {base_url, key}}
        model_cost_fn: Optional cost lookup function
        timeout: Request timeout in seconds

    Returns:
        (success, response_body, provider_name)
        - success=True, response_body=bytes, provider_name=name on success
        - success=False, None, None if all providers fail
    """
    candidates = get_cheapest_funded(providers, model_cost_fn)
    if not candidates:
        return False, None, None

    for cost, name, prov in candidates:
        try:
            body_json = json.loads(body) if body else {}
            body_json["model"] = FALLBACK_MODEL
            fwd_body = json.dumps(body_json).encode()

            url = prov["base_url"] + "/chat/completions"
            hdrs = {
                "Authorization": f"Bearer {prov['key']}",
                "Content-Type": "application/json",
            }
            if name == "openrouter":
                hdrs["HTTP-Referer"] = "https://hermes.local"
                hdrs["X-Title"] = "Hermes Agent"

            req = urllib.request.Request(url, data=fwd_body, method="POST", headers=hdrs)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    response_body = resp.read()
                    # Verify non-empty content
                    is_empty, has_reasoning = is_content_empty(response_body)
                    if is_empty:
                        continue
                    mark_funded(name)
                    return True, response_body, name
            except urllib.error.HTTPError as he:
                if he.code == 402:
                    mark_unfunded(name)
                    continue
                raise
        except Exception:
            continue

    return False, None, None
