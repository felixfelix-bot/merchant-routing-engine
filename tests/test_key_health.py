"""Tests for key_health_tracker module."""
import time
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.key_health_tracker import (
    is_key_healthy,
    mark_key_exhausted,
    mark_key_healthy,
    select_healthy_key,
    _zai_key_health,
)


@pytest.fixture(autouse=True)
def reset_health():
    """Reset health state before each test."""
    _zai_key_health.clear()
    yield
    _zai_key_health.clear()


def test_key_starts_healthy():
    assert is_key_healthy("ours") is True
    assert is_key_healthy("friend") is True


def test_mark_exhausted_makes_unhealthy():
    mark_key_exhausted("ours")
    assert is_key_healthy("ours") is False
    assert is_key_healthy("friend") is True


def test_mark_healthy_restores():
    mark_key_exhausted("ours")
    assert is_key_healthy("ours") is False
    mark_key_healthy("ours")
    assert is_key_healthy("ours") is True


def test_exhausted_recovers_after_timeout():
    mark_key_exhausted("ours")
    assert is_key_healthy("ours") is False
    # Simulate time passing by modifying retry_after
    _zai_key_health["ours"]["retry_after"] = time.time() - 1
    assert is_key_healthy("ours") is True


def test_select_healthy_key_prefers_chosen():
    assert select_healthy_key("ours") == "ours"
    assert select_healthy_key("friend") == "friend"


def test_select_healthy_key_falls_back():
    mark_key_exhausted("ours")
    result = select_healthy_key("ours")
    assert result == "friend"


def test_select_healthy_key_returns_none_when_both_exhausted():
    mark_key_exhausted("ours")
    mark_key_exhausted("friend")
    assert select_healthy_key("ours") is None
    assert select_healthy_key("friend") is None
    assert select_healthy_key(None) is None


def test_mark_exhausted_sets_retry_after():
    before = time.time()
    mark_key_exhausted("ours")
    retry_after = _zai_key_health["ours"]["retry_after"]
    assert retry_after > before
    assert retry_after <= before + 301  # 5 min + small buffer
