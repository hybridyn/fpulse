"""Shared-surface security fixes reconciled from the F-Pulse+ monorepo.

The monorepo and this OSS repo share no git history, so security fixes to
shared code don't flow automatically. This covers two found by the 2026-07-16
sweep and ported here:

  1. Sanitizer missed connection-string passwords and key=value secrets, so
     they leaked into logs / the Steward dismiss journal / AI context.
  2. Publishing a PUBLIC (no-API-key) gateway endpoint whose workflow
     interpolates request vars into raw SQL was allowed — unauthenticated SQL
     injection. Now refused unless explicitly overridden.
"""
from __future__ import annotations

import pytest


# ── 1. sanitizer redaction ──────────────────────────────────────────────────

def _redact(text: str) -> str:
    from fpulse.ai.sanitize import _redact_string
    return _redact_string(text, [])[0]


def test_sanitizer_redacts_connection_string_password():
    out = _redact("db=postgres://admin:s3cr3tP4ss@db.internal:5432/prod")
    assert "s3cr3tP4ss" not in out


def test_sanitizer_redacts_key_value_secrets():
    for probe, secret in [
        ("password=hunter2trustno1", "hunter2trustno1"),
        ("api_key: sk-abc123def456", "sk-abc123def456"),
        ("client_secret=zzzYYYxxx", "zzzYYYxxx"),
    ]:
        out = _redact(probe)
        assert secret not in out, f"{secret!r} leaked from {probe!r}: {out!r}"


# ── 2. public-endpoint templated-SQL guard ──────────────────────────────────

class _Step:
    def __init__(self, sid, params):
        self.id = sid
        self.params = params


class _WF:
    def __init__(self, steps):
        self.steps = steps


def test_templated_sql_detection():
    from fpulse.api.gateway import _templated_sql_step_ids
    wf = _WF([
        _Step("a", {"query": "SELECT * FROM t WHERE id = {{ body.id }}"}),  # templated raw SQL
        _Step("b", {"query": "SELECT 1"}),                                   # static — fine
        _Step("c", {"other": "{{ body.x }}"}),                               # not a SQL param
    ])
    assert _templated_sql_step_ids(wf) == ["a"]


def test_public_endpoint_guard_blocks_and_override_allows(monkeypatch):
    from fastapi import HTTPException
    import fpulse.api.gateway as gw

    wf = _WF([_Step("s1", {"sql": "DELETE FROM users WHERE name = {{ body.name }}"})])

    class _Ver:
        workflow = wf

    class _Store:
        def get(self, _id):
            return _Ver()

    monkeypatch.setitem(__import__("fpulse.main", fromlist=["app_state"]).app_state, "store", _Store())

    # Public + templated SQL, no override -> 400.
    with pytest.raises(HTTPException) as ei:
        gw._guard_public_templated_sql("wf1", allow_override=False)
    assert ei.value.status_code == 400

    # Explicit override -> allowed (returns None).
    gw._guard_public_templated_sql("wf1", allow_override=True)


# ── node-level "X4" fixes: SQLi in db_source, redirect-SSRF in http nodes ────

def test_db_source_rejects_identifier_injection():
    from fpulse.nodes.db_source import _validate_identifier, _reject_stacked_statements
    assert _validate_identifier("public.orders", kind="table") == "public.orders"
    for payload in ['orders"; DROP TABLE users; --', "a b", "x)", "1;2", "col--"]:
        with pytest.raises(ValueError):
            _validate_identifier(payload, kind="table")
    with pytest.raises(ValueError):
        _reject_stacked_statements("1=1; DROP TABLE x", kind="WHERE")


def test_code_script_disabled_by_default(monkeypatch):
    """Code Script runs unsandboxed in-process Python. It must be OFF unless an
    operator explicitly opts in — fail-closed, like seed-admin."""
    from fpulse.nodes.flow_control import CodeScriptNode

    monkeypatch.delenv("FPULSE_ENABLE_CODE_SCRIPT", raising=False)
    monkeypatch.delenv("FPULSE_DISABLE_CODE_SCRIPT", raising=False)
    assert CodeScriptNode._code_script_enabled() is False           # default: off

    monkeypatch.setenv("FPULSE_ENABLE_CODE_SCRIPT", "1")
    assert CodeScriptNode._code_script_enabled() is True            # explicit opt-in

    monkeypatch.setenv("FPULSE_DISABLE_CODE_SCRIPT", "1")
    assert CodeScriptNode._code_script_enabled() is False           # force-off wins


@pytest.mark.parametrize("module", ["fpulse.nodes.activities", "fpulse.nodes.sinks"])
def test_http_nodes_block_redirects(module):
    import importlib
    m = importlib.import_module(module)
    opener = m._no_redirect_opener()
    handler = next(h for h in opener.handlers if isinstance(h, m._NoRedirectHandler))
    # A 3xx must raise (blocked), never silently follow to an unvalidated host.
    with pytest.raises(Exception) as ei:
        handler.redirect_request(
            None, None, 302, "Found", {}, "http://169.254.169.254/latest/meta-data/"
        )
    assert "redirect" in str(ei.value).lower()
