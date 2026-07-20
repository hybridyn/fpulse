"""
Tests for the list_steward_findings agent tool.

The tool runs the same deterministic Steward scan the Insights page uses
(``record=False``) and shapes it into a compact, honest payload for the
Copilot. These tests pin:
  - the tool is registered as a READ tool (no idempotency key),
  - status / level / min_severity filtering + severity-desc sorting,
  - the honesty `coverage` note is always present (so an empty list is never
    read as "verified clean" for the un-fed detectors),
  - a scan failure degrades to an honest empty payload, never an exception.

No LLM, no network, no DB — `_run_scan` is monkeypatched with synthetic
StewardFinding records.
"""

from __future__ import annotations

import asyncio

import pytest

from fpulse.ai.tools import LIST_STEWARD_FINDINGS, ToolContext, ToolTier
from fpulse.steward.models import (
    FindingKind,
    FindingLevel,
    FindingSeverity,
    FindingStatus,
    StewardFinding,
)
from fpulse.steward.settings import StewardSettings


def _ctx() -> ToolContext:
    return ToolContext(
        tenant_id="t-1",
        user_id="u-1",
        workspace_id="ws-1",
        environment="dev",
    )


def _finding(
    *,
    fid: str,
    kind: FindingKind,
    level: FindingLevel,
    severity: FindingSeverity,
    status: FindingStatus = FindingStatus.OPEN,
    occurrences: int = 1,
    actions: list[dict] | None = None,
) -> StewardFinding:
    return StewardFinding(
        id=fid,
        kind=kind,
        level=level,
        severity=severity,
        status=status,
        title=f"{kind.value} title",
        body=f"{kind.value} body — what/why/action",
        first_seen="2026-06-16T00:00:00Z",
        last_seen="2026-06-16T00:00:00Z",
        occurrences=occurrences,
        proposed_actions=actions or [],
    )


def _sample() -> list[StewardFinding]:
    return [
        _finding(fid="f-p3", kind=FindingKind.CONNECTOR_AUTH_FAILURE,
                 level=FindingLevel.CONNECTOR, severity=FindingSeverity.P3),
        _finding(fid="f-p1", kind=FindingKind.ENV_CROSSING,
                 level=FindingLevel.GOVERNANCE, severity=FindingSeverity.P1),
        _finding(fid="f-p2", kind=FindingKind.DUPLICATE_SOURCE,
                 level=FindingLevel.ARCHITECTURE, severity=FindingSeverity.P2,
                 occurrences=4, actions=[{"label": "Merge sources", "action": "merge"}]),
        _finding(fid="f-dismissed", kind=FindingKind.DUPLICATE_PIPELINE,
                 level=FindingLevel.ARCHITECTURE, severity=FindingSeverity.P2,
                 status=FindingStatus.DISMISSED),
    ]


def _patch_scan(monkeypatch, findings):
    def fake(workspace_id, *, record=True):
        return list(findings), StewardSettings()
    monkeypatch.setattr("fpulse.api.steward._run_scan", fake)


def test_registered_as_read_tool():
    assert LIST_STEWARD_FINDINGS.name == "list_steward_findings"
    assert LIST_STEWARD_FINDINGS.tier == ToolTier.READ
    assert not LIST_STEWARD_FINDINGS.requires_idempotency_key
    schema = LIST_STEWARD_FINDINGS.to_anthropic_schema()
    assert schema["name"] == "list_steward_findings"


def test_default_lists_open_sorted_by_severity(monkeypatch):
    _patch_scan(monkeypatch, _sample())
    out = asyncio.run(LIST_STEWARD_FINDINGS.handler({}, _ctx()))
    # default status=open → 3 of 4 (dismissed excluded)
    assert out["count"] == 3
    assert out["returned"] == 3
    ids = [f["id"] for f in out["findings"]]
    # P1 first, then P2, then P3
    assert ids == ["f-p1", "f-p2", "f-p3"]
    assert out["by_level"] == {"governance": 1, "architecture": 1, "connector": 1}
    # honesty note always present
    assert "coverage" in out and "nothing recorded" in out["coverage"]


def test_min_severity_filters(monkeypatch):
    _patch_scan(monkeypatch, _sample())
    out = asyncio.run(LIST_STEWARD_FINDINGS.handler({"min_severity": "p2"}, _ctx()))
    ids = {f["id"] for f in out["findings"]}
    assert ids == {"f-p1", "f-p2"}  # p3 dropped


def test_level_filter(monkeypatch):
    _patch_scan(monkeypatch, _sample())
    out = asyncio.run(LIST_STEWARD_FINDINGS.handler({"level": "governance"}, _ctx()))
    assert [f["id"] for f in out["findings"]] == ["f-p1"]


def test_status_all_includes_dismissed(monkeypatch):
    _patch_scan(monkeypatch, _sample())
    out = asyncio.run(LIST_STEWARD_FINDINGS.handler({"status": "all"}, _ctx()))
    assert out["count"] == 4


def test_limit_caps_returned_but_count_is_total(monkeypatch):
    _patch_scan(monkeypatch, _sample())
    out = asyncio.run(LIST_STEWARD_FINDINGS.handler({"limit": 1}, _ctx()))
    assert out["count"] == 3        # total open
    assert out["returned"] == 1     # capped
    assert out["findings"][0]["id"] == "f-p1"


def test_suggested_actions_surface(monkeypatch):
    _patch_scan(monkeypatch, _sample())
    out = asyncio.run(LIST_STEWARD_FINDINGS.handler({"level": "architecture"}, _ctx()))
    dup = next(f for f in out["findings"] if f["id"] == "f-p2")
    assert dup["suggested_actions"] == ["Merge sources"]


def test_scan_failure_degrades_to_honest_empty(monkeypatch):
    def boom(workspace_id, *, record=True):
        raise RuntimeError("store offline")
    monkeypatch.setattr("fpulse.api.steward._run_scan", boom)
    out = asyncio.run(LIST_STEWARD_FINDINGS.handler({}, _ctx()))
    assert out["count"] == 0
    assert out["findings"] == []
    assert "Could not run the Steward scan" in out["coverage"]
