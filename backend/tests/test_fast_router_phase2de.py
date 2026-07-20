"""Tests for the Phase 2D + 2E fast-router handlers (May 18 2026).

Covers:
  * slowest_runs — aggregates list_executions by duration_ms
  * compare_runs — diffs the latest two runs of a pipeline
  * summarize_failure — fetches the latest failed run + suggests
    a fix via pattern matching against the error string

All three handlers are async; tests use asyncio.run() + a fake
_call_tool stub that returns canned execution rows.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from fpulse.ai.fast_router import (
    _render_compare_runs,
    _render_slowest_runs,
    _render_summarize_failure,
    _suggest_from_error,
)
from fpulse.ai.tools.base import ToolContext


def _ctx(**overrides) -> ToolContext:
    defaults = dict(
        tenant_id="t1",
        user_id="u1",
        workspace_id="ws1",
        environment="dev",
        dry_run=False,
        selected_ids=(),
        visible_ids=(),
    )
    defaults.update(overrides)
    return ToolContext(**defaults)


def _stub_executions(rows: list[dict]):
    """Patch _call_tool to return canned executions instead of hitting
    the real list_executions handler."""
    async def fake_call_tool(tool_name, args, ctx):
        if tool_name == "list_executions":
            return {"executions": rows, "total": len(rows)}
        return {"_error": f"unexpected tool {tool_name}"}
    return patch("fpulse.ai.fast_router._call_tool", side_effect=fake_call_tool)


# ── _suggest_from_error (10 patterns) ─────────────────────────────────────


def test_suggest_from_error_empty():
    assert _suggest_from_error("") is None
    assert _suggest_from_error("   ") is None


def test_suggest_from_error_login_timeout():
    s = _suggest_from_error("ODBC Driver 17 for SQL Server - Login timeout expired")
    assert s is not None
    assert "timeout" in s.lower()


def test_suggest_from_error_auth():
    for msg in (
        "401 Unauthorized",
        "authentication failed",
        "invalid credentials",
        "403 Forbidden",
    ):
        s = _suggest_from_error(msg)
        assert s is not None, f"missed: {msg!r}"
        assert "credential" in s.lower() or "auth" in s.lower()


def test_suggest_from_error_missing_table():
    s = _suggest_from_error('relation "orders" does not exist')
    assert s is not None
    assert "table" in s.lower()


def test_suggest_from_error_duplicate_key():
    s = _suggest_from_error("UNIQUE constraint failed: customers.email")
    assert s is not None
    assert "upsert" in s.lower() or "deduplicate" in s.lower()


def test_suggest_from_error_oom():
    s = _suggest_from_error("MemoryError: out of memory")
    assert s is not None
    assert "memory" in s.lower() or "batch" in s.lower()


def test_suggest_from_error_rate_limit():
    s = _suggest_from_error("HTTP 429 Too Many Requests")
    assert s is not None
    assert "rate" in s.lower() or "retry" in s.lower()


def test_suggest_from_error_unknown_pattern():
    """Errors that don't match any pattern return None."""
    s = _suggest_from_error("some bizarre proprietary error code XQ-7734")
    assert s is None


# ── slowest_runs ──────────────────────────────────────────────────────────


def test_slowest_runs_empty():
    with _stub_executions([]):
        out = asyncio.run(_render_slowest_runs("show slowest runs", _ctx()))
    assert "No finished runs" in out


def test_slowest_runs_filters_unfinished_runs():
    """Running / queued runs have no meaningful duration — exclude them."""
    rows = [
        {"id": "e1", "workflow_name": "p1", "status": "running", "duration_ms": 0},
        {"id": "e2", "workflow_name": "p2", "status": "queued", "duration_ms": 0},
        {"id": "e3", "workflow_name": "p3", "status": "success", "duration_ms": 5000},
    ]
    with _stub_executions(rows):
        out = asyncio.run(_render_slowest_runs("show slowest runs", _ctx()))
    assert "p3" in out
    assert "p1" not in out  # running, no duration
    assert "p2" not in out  # queued


def test_slowest_runs_sorts_descending_top_5():
    rows = [
        {"id": f"e{i}", "workflow_name": f"p{i}", "status": "success",
         "duration_ms": i * 1000, "rows_processed": 100, "peak_memory_mb": 50,
         "started_at": "2026-05-18T10:00:00Z"}
        for i in range(1, 11)
    ]
    with _stub_executions(rows):
        out = asyncio.run(_render_slowest_runs("longest runs", _ctx()))
    # p10 should appear first (10000ms), p1 last (1000ms); only 5 should
    # actually appear in the table.
    assert "p10" in out
    assert "p9" in out
    assert "p6" in out
    assert "p5" not in out  # below top-5 threshold


def test_slowest_runs_formats_duration_units():
    """ms / s / m / h formatting depending on magnitude.

    Note: 80_000ms (not 75_000) for the minute test — 75_000/60_000=1.25
    exactly, and Python's `.1f` uses round-half-to-even, which gives
    '1.2m' not '1.3m'. 80_000/60_000=1.333... rounds unambiguously
    to '1.3m' regardless of tie-break rule. Lesson: avoid x.5 ties in
    formatting-roundtrip tests."""
    rows = [
        {"id": "e1", "workflow_name": "p", "status": "success",
         "duration_ms": 500, "rows_processed": 0, "peak_memory_mb": 0,
         "started_at": "2026-05-18T10:00:00Z"},
        {"id": "e2", "workflow_name": "p", "status": "success",
         "duration_ms": 5000, "rows_processed": 0, "peak_memory_mb": 0,
         "started_at": "2026-05-18T10:00:00Z"},
        {"id": "e3", "workflow_name": "p", "status": "success",
         "duration_ms": 80_000, "rows_processed": 0, "peak_memory_mb": 0,
         "started_at": "2026-05-18T10:00:00Z"},
    ]
    with _stub_executions(rows):
        out = asyncio.run(_render_slowest_runs("longest runs", _ctx()))
    assert "500ms" in out
    assert "5.0s" in out
    assert "1.3m" in out


# ── compare_runs ──────────────────────────────────────────────────────────


def test_compare_runs_no_pipeline_context_asks():
    """When no pipeline is selected/visible, the handler asks for one."""
    with _stub_executions([]):
        out = asyncio.run(_render_compare_runs("compare runs", _ctx()))
    assert "which pipeline" in out.lower()


def test_compare_runs_fewer_than_two_runs():
    with _stub_executions([
        {"id": "e1", "workflow_name": "p", "status": "success", "duration_ms": 1000},
    ]):
        out = asyncio.run(_render_compare_runs(
            "compare runs", _ctx(selected_ids=("p1",)),
        ))
    assert "only has 1 run" in out


def test_compare_runs_delta_computed():
    rows = [
        {"id": "e1", "workflow_name": "p", "status": "failed",
         "duration_ms": 8000, "rows_processed": 90, "peak_memory_mb": 200,
         "started_at": "2026-05-18T12:00:00Z", "error": "Login timeout expired"},
        {"id": "e2", "workflow_name": "p", "status": "success",
         "duration_ms": 4000, "rows_processed": 100, "peak_memory_mb": 150,
         "started_at": "2026-05-18T06:00:00Z"},
    ]
    with _stub_executions(rows):
        out = asyncio.run(_render_compare_runs(
            "compare runs", _ctx(selected_ids=("p1",)),
        ))
    # Delta column should show duration grew by 4000ms (+100%)
    assert "+100%" in out or "+4000" in out
    # Error should be surfaced because latest failed + baseline succeeded
    assert "Login timeout expired" in out


def test_compare_runs_last_successful_target():
    """When user says 'compare with last successful', the handler uses
    the most recent successful run as the baseline, skipping any failed
    runs in between."""
    rows = [
        {"id": "e3", "workflow_name": "p", "status": "failed",
         "duration_ms": 8000, "rows_processed": 50, "peak_memory_mb": 200,
         "started_at": "2026-05-18T12:00:00Z", "error": "X"},
        {"id": "e2", "workflow_name": "p", "status": "failed",
         "duration_ms": 7000, "rows_processed": 60, "peak_memory_mb": 180,
         "started_at": "2026-05-18T11:00:00Z", "error": "Y"},
        {"id": "e1", "workflow_name": "p", "status": "success",
         "duration_ms": 4000, "rows_processed": 100, "peak_memory_mb": 150,
         "started_at": "2026-05-18T06:00:00Z"},
    ]
    with _stub_executions(rows):
        out = asyncio.run(_render_compare_runs(
            "compare with last successful run",
            _ctx(selected_ids=("p1",)),
        ))
    # Baseline = e1 (success), not e2 (failed) — so duration delta is +4000 not +1000
    assert "4000ms" in out or "4.0s" in out


# ── summarize_failure ─────────────────────────────────────────────────────


def test_summarize_failure_no_failures():
    rows = [
        {"id": "e1", "workflow_name": "p", "status": "success", "duration_ms": 1000},
    ]
    with _stub_executions(rows):
        out = asyncio.run(_render_summarize_failure(
            "summarize last failure", _ctx(),
        ))
    assert "No recent failures" in out


def test_summarize_failure_with_credential_pattern():
    """The handler should match the credential pattern and suggest reset."""
    rows = [
        {"id": "e1", "workflow_name": "nightly_etl", "status": "failed",
         "duration_ms": 2400, "rows_processed": 0, "trigger": "schedule",
         "started_at": "2026-05-18T02:14:00Z",
         "error": "401 Unauthorized: invalid token"},
    ]
    with _stub_executions(rows):
        out = asyncio.run(_render_summarize_failure(
            "summarize last failure", _ctx(),
        ))
    assert "nightly_etl" in out
    assert "401" in out or "Unauthorized" in out
    assert "credential" in out.lower() or "auth" in out.lower()
    # Recommends opening Credentials page or similar
    assert "credentials" in out.lower() or "reset" in out.lower()


def test_summarize_failure_with_unknown_error_pattern():
    """Unknown errors still get summarised; suggestion is just omitted."""
    rows = [
        {"id": "e1", "workflow_name": "p", "status": "failed",
         "duration_ms": 500, "rows_processed": 0, "trigger": "manual",
         "started_at": "2026-05-18T10:00:00Z",
         "error": "weird vendor-specific error XQ-9999"},
    ]
    with _stub_executions(rows):
        out = asyncio.run(_render_summarize_failure(
            "explain last failure", _ctx(),
        ))
    assert "Failure summary" in out
    assert "XQ-9999" in out
    # No suggestion line when error doesn't match any pattern.
    assert "Likely cause" not in out


# ── Phase 3.2 LLM augmentation ────────────────────────────────────────────


def test_summarize_failure_llm_silent_when_no_provider():
    """No provider configured → no AI explanation line appended.
    Deterministic summary always renders."""
    rows = [
        {"id": "e1", "workflow_name": "p", "status": "failed",
         "duration_ms": 1000, "rows_processed": 0, "trigger": "manual",
         "started_at": "2026-05-18T10:00:00Z",
         "error": "401 Unauthorized"},
    ]
    # Patch resolve_provider to return 'none' — simulates the
    # default OSS Free install with no AI configured.
    with _stub_executions(rows), patch(
        "fpulse.planner.ai_client.resolve_provider",
        return_value=("none", None, "", None),
    ):
        out = asyncio.run(_render_summarize_failure(
            "summarize last failure", _ctx(),
        ))
    # Pattern-matched suggestion is still there
    assert "credential" in out.lower() or "auth" in out.lower()
    # But no AI explanation line
    assert "AI explanation" not in out


def test_summarize_failure_llm_silent_on_provider_error():
    """LLM call raising should never break the deterministic output."""
    rows = [
        {"id": "e1", "workflow_name": "p", "status": "failed",
         "duration_ms": 1000, "rows_processed": 0, "trigger": "manual",
         "started_at": "2026-05-18T10:00:00Z",
         "error": "401 Unauthorized"},
    ]
    # Patch resolve_provider to raise — simulates a misconfigured
    # secrets store or stale credential.
    with _stub_executions(rows), patch(
        "fpulse.planner.ai_client.resolve_provider",
        side_effect=RuntimeError("simulated provider lookup failure"),
    ):
        out = asyncio.run(_render_summarize_failure(
            "summarize last failure", _ctx(),
        ))
    # The deterministic summary still renders
    assert "Failure summary" in out
    assert "401" in out
    # AI line is not present
    assert "AI explanation" not in out
