"""Tests for fpulse.ai.tool_only_mode.

Operator-level kill switch: when FPULSE_TOOL_ONLY_MODE=1 is set, every
LLM-using lane is blocked at the agent endpoint and the user gets a
deterministic "tool-only mode is on" reply instead of falling through
to an LLM call.
"""

from __future__ import annotations

import os

import pytest

from fpulse.ai.tool_only_mode import is_enabled, unavailable_response_text


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    """Each test starts with a clean env so order doesn't matter."""
    monkeypatch.delenv("FPULSE_TOOL_ONLY_MODE", raising=False)
    yield


def test_default_is_off():
    assert is_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "True", "TRUE", "yes", "on", "ON"])
def test_truthy_values_enable(monkeypatch, value):
    monkeypatch.setenv("FPULSE_TOOL_ONLY_MODE", value)
    assert is_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "anything-else"])
def test_falsy_values_stay_disabled(monkeypatch, value):
    monkeypatch.setenv("FPULSE_TOOL_ONLY_MODE", value)
    assert is_enabled() is False


def test_whitespace_is_stripped(monkeypatch):
    monkeypatch.setenv("FPULSE_TOOL_ONLY_MODE", "  1  ")
    assert is_enabled() is True


def test_unavailable_text_mentions_tool_only():
    text = unavailable_response_text()
    assert "tool-only" in text.lower()
    # Must enumerate at least one working phrasing so the user knows
    # what they CAN ask.
    assert "list pipelines" in text.lower() or "show my failures" in text.lower()
    # Must tell the operator how to undo the setting.
    assert "FPULSE_TOOL_ONLY_MODE" in text
