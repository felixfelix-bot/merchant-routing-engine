"""reasoning_handler.py — Inject reasoning content when model output is empty.

z.ai's glm-5.2 is a reasoning model that returns responses in two fields:
- content: the actual response (may be empty)
- reasoning_content: the model's internal thinking (usually has data)

When content is empty but reasoning_content has value, inject reasoning
into content so the tokens aren't wasted. No external failover needed.

Extracted from ~/.hermes/bot/zai_proxy.py (Phase 1 — standalone copy).
"""
from __future__ import annotations
import json


def check_and_inject_reasoning(response_body: bytes) -> bytes:
    """Check if response has empty content but valid reasoning.
    If so, inject reasoning as content and return modified body.

    Returns:
        Modified response body if reasoning was injected,
        original body otherwise.
    """
    try:
        resp_text = response_body.decode("utf-8", errors="ignore").strip()
        if not resp_text:
            return response_body

        resp_json = json.loads(resp_text)
        choices = resp_json.get("choices", [])
        if not choices:
            return response_body

        msg = choices[0].get("message", {})
        content = msg.get("content", "")
        reasoning = msg.get("reasoning_content", "")

        if (not content or not content.strip()) and reasoning and reasoning.strip():
            msg["content"] = reasoning
            return json.dumps(resp_json).encode()

        return response_body
    except Exception:
        return response_body


def is_content_empty(response_body: bytes) -> tuple[bool, bool]:
    """Check if response content is empty.

    Returns:
        (is_empty, has_reasoning) — is_empty=True means no usable content.
        has_reasoning=True means reasoning_content has data (can be injected).
    """
    try:
        resp_text = response_body.decode("utf-8", errors="ignore").strip()
        if not resp_text or resp_text == "data: [DONE]":
            return True, False

        resp_json = json.loads(resp_text)
        if "error" in resp_json and "choices" not in resp_json:
            return True, False  # Error response — no content at all

        choices = resp_json.get("choices", [])
        if not choices:
            return True, False

        msg = choices[0].get("message", {})
        content = msg.get("content", "")
        reasoning = msg.get("reasoning_content", "")

        if content and content.strip():
            return False, bool(reasoning)
        return True, bool(reasoning and reasoning.strip())
    except Exception:
        return False, False
