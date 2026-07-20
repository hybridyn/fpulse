"""Tests for fpulse.planner.ai_client.policy_route — local-first routing."""

from __future__ import annotations

import pytest

from fpulse.planner.ai_client import policy_route, invalidate_ollama_autoprobe


def _clear_provider_env(monkeypatch):
    for key in (
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY",
        "OLLAMA_URL", "AI_MODEL", "FPULSE_ENABLE_POLICY_ROUTE",
    ):
        monkeypatch.delenv(key, raising=False)
    invalidate_ollama_autoprobe()
    monkeypatch.setenv("FPULSE_DISABLE_OLLAMA_AUTOPROBE", "1")


def test_policy_route_passthrough_when_flag_off(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("AI_MODEL", "claude-test")

    provider, _, _, _ = policy_route("code")
    # Without the flag, policy_route is a pass-through — uses the cloud provider
    assert provider == "claude"


def test_policy_route_passthrough_for_non_sensitive_kind(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("FPULSE_ENABLE_POLICY_ROUTE", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("OLLAMA_URL", "http://localhost:11434")

    provider, _, _, _ = policy_route("greeting")
    # Non-sensitive kind: uses cloud provider even with flag on
    assert provider == "claude"


def test_policy_route_prefers_local_for_sensitive_kind(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("FPULSE_ENABLE_POLICY_ROUTE", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("OLLAMA_URL", "http://localhost:11434")
    monkeypatch.setenv("AI_MODEL", "llama3.1")

    for kind in ("code", "tool_result", "sensitive"):
        provider, _, model, base_url = policy_route(kind)
        assert provider == "ollama", f"kind={kind} should route to local"
        assert base_url == "http://localhost:11434"
        assert model == "llama3.1"


def test_policy_route_falls_back_to_cloud_when_no_local(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("FPULSE_ENABLE_POLICY_ROUTE", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    # No OLLAMA_URL, autoprobe disabled — falls back to cloud

    provider, _, _, _ = policy_route("code")
    assert provider == "claude"
