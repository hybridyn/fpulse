"""Phase 5 — the assistant's execution actions are gated server-side.

Read/draft actions never reach the gate; run/cancel/test do. Default is
allow-on-local / read-draft-when-exposed, and when execution is enabled on
an exposed server a write role is still required (never chip/prompt trust).
"""
from fpulse import runtime_config
from fpulse.api import agent_action as aa


class _User:
    def __init__(self, role):
        self.role = role


def test_local_default_allows_execute(monkeypatch):
    monkeypatch.setattr(runtime_config, "AI_ALLOW_EXECUTE", True)
    monkeypatch.setattr(runtime_config, "IS_SERVER_MODE", False)
    assert aa._ai_execution_denied(_User("viewer")) is None


def test_read_draft_mode_denies_execute(monkeypatch):
    monkeypatch.setattr(runtime_config, "AI_ALLOW_EXECUTE", False)
    monkeypatch.setattr(runtime_config, "IS_SERVER_MODE", False)
    msg = aa._ai_execution_denied(_User("admin"))
    assert msg and "read/draft" in msg


def test_server_mode_requires_write_role(monkeypatch):
    monkeypatch.setattr(runtime_config, "AI_ALLOW_EXECUTE", True)
    monkeypatch.setattr(runtime_config, "IS_SERVER_MODE", True)
    assert aa._ai_execution_denied(_User("viewer")) is not None      # denied
    assert aa._ai_execution_denied(None) is not None                 # anon denied
    assert aa._ai_execution_denied(_User("developer")) is None       # allowed
    assert aa._ai_execution_denied(_User("super_admin")) is None     # allowed


def test_only_execution_handlers_are_gated():
    # The gate keys off handler name — read/draft handlers are not in the set.
    assert "direct_run_pipeline" in aa._AI_EXECUTION_HANDLER_NAMES
    assert "direct_cancel_execution" in aa._AI_EXECUTION_HANDLER_NAMES
    assert "direct_test_connection" in aa._AI_EXECUTION_HANDLER_NAMES
    assert "describe_entity" not in aa._AI_EXECUTION_HANDLER_NAMES
    assert "diagnose_failure" not in aa._AI_EXECUTION_HANDLER_NAMES
