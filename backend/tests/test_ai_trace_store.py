"""
Tests for fpulse.ai.trace_store.TraceStore.

Uses the per-test SQLite Database fixture from conftest.py — same isolation
pattern as every other store test.
"""

from __future__ import annotations

import time

import pytest

from fpulse.ai.agent import TraceStep
from fpulse.ai.trace_store import TraceStore


def _step(name: str = "summarize_pipeline", outcome: str = "success") -> TraceStep:
    return TraceStep(
        step_id=f"step-{name}-{int(time.time() * 1000)}",
        tool_name=name,
        tool_tier="read",
        input_hash="a" * 64,
        output_hash="b" * 64,
        timestamp="2026-04-29T12:00:00+00:00",
        latency_ms=42,
        tokens_in=100,
        tokens_out=20,
        decision_reason="ok",
        redactions_applied={},
        outcome=outcome,
        policy_rules_fired=[],
    )


@pytest.fixture
def store(_fpulse_test_db):
    return TraceStore(db=_fpulse_test_db)


def test_store_creates_schema_idempotently(_fpulse_test_db):
    s1 = TraceStore(db=_fpulse_test_db)
    s2 = TraceStore(db=_fpulse_test_db)  # second construction shouldn't error
    assert s1.count() == 0
    assert s2.count() == 0


def test_store_and_get_roundtrip(store):
    store.store(
        run_id="r-1",
        user_id="u-1",
        workspace_id="ws-1",
        page="pipelines.list",
        user_intent="Summarize pipeline p-1",
        outcome="success",
        iterations=1,
        total_tokens_in=240,
        total_tokens_out=60,
        elapsed_ms=482,
        steps=[_step()],
        final_text="12 nodes, 2 sources, 1 destination.",
    )
    out = store.get("r-1")
    assert out is not None
    assert out["run_id"] == "r-1"
    assert out["user_id"] == "u-1"
    assert out["workspace_id"] == "ws-1"
    assert out["page"] == "pipelines.list"
    assert out["outcome"] == "success"
    assert out["iterations"] == 1
    assert out["final_text"] == "12 nodes, 2 sources, 1 destination."
    assert len(out["steps"]) == 1
    assert out["steps"][0]["tool_name"] == "summarize_pipeline"


def test_get_returns_none_on_miss(store):
    assert store.get("does-not-exist") is None
    assert store.get("") is None


def test_list_recent_filters_by_user(store):
    for i in range(3):
        store.store(
            run_id=f"r-u1-{i}",
            user_id="u-1",
            workspace_id="ws-1",
            page="x",
            user_intent=f"intent {i}",
            outcome="success",
            iterations=1,
            total_tokens_in=10,
            total_tokens_out=5,
            elapsed_ms=100,
            steps=[],
            final_text="",
        )
    store.store(
        run_id="r-u2-0",
        user_id="u-2",
        workspace_id="ws-1",
        page="x",
        user_intent="other user",
        outcome="success",
        iterations=1,
        total_tokens_in=10,
        total_tokens_out=5,
        elapsed_ms=100,
        steps=[],
        final_text="",
    )
    rows = store.list_recent(user_id="u-1")
    assert len(rows) == 3
    assert all(r["user_id"] == "u-1" for r in rows)


def test_list_recent_orders_newest_first(store):
    # Insert with monotonically increasing created_at (created_at is set in
    # store() to now() so multiple inserts yield ascending timestamps).
    for i in range(3):
        store.store(
            run_id=f"r-{i}",
            user_id="u-1",
            workspace_id="ws-1",
            page="x",
            user_intent=f"intent {i}",
            outcome="success",
            iterations=1,
            total_tokens_in=0,
            total_tokens_out=0,
            elapsed_ms=0,
            steps=[],
            final_text="",
        )
        time.sleep(0.001)  # ensure ISO timestamps differ
    rows = store.list_recent(user_id="u-1")
    # Newest first → r-2 before r-0
    assert rows[0]["run_id"] == "r-2"
    assert rows[-1]["run_id"] == "r-0"


def test_list_recent_workspace_scope_when_no_user(store):
    store.store(
        run_id="r-anon",
        user_id=None,
        workspace_id="ws-A",
        page="x",
        user_intent="anon",
        outcome="success",
        iterations=0,
        total_tokens_in=0,
        total_tokens_out=0,
        elapsed_ms=0,
        steps=[],
        final_text="",
    )
    store.store(
        run_id="r-other-ws",
        user_id=None,
        workspace_id="ws-B",
        page="x",
        user_intent="other",
        outcome="success",
        iterations=0,
        total_tokens_in=0,
        total_tokens_out=0,
        elapsed_ms=0,
        steps=[],
        final_text="",
    )
    rows = store.list_recent(workspace_id="ws-A")
    assert len(rows) == 1
    assert rows[0]["run_id"] == "r-anon"


def test_list_recent_limit_clamping(store):
    for i in range(5):
        store.store(
            run_id=f"r-{i}",
            user_id="u-1",
            workspace_id="ws-1",
            page="x",
            user_intent=f"i{i}",
            outcome="success",
            iterations=0,
            total_tokens_in=0,
            total_tokens_out=0,
            elapsed_ms=0,
            steps=[],
            final_text="",
        )
    assert len(store.list_recent(user_id="u-1", limit=2)) == 2
    # Limit must be clamped to >= 1
    assert len(store.list_recent(user_id="u-1", limit=0)) == 1


def test_replace_on_same_run_id(store):
    store.store(
        run_id="r-1",
        user_id="u-1",
        workspace_id="ws-1",
        page="x",
        user_intent="first",
        outcome="success",
        iterations=1,
        total_tokens_in=0,
        total_tokens_out=0,
        elapsed_ms=0,
        steps=[],
        final_text="",
    )
    store.store(
        run_id="r-1",
        user_id="u-1",
        workspace_id="ws-1",
        page="x",
        user_intent="updated",
        outcome="tool_failure",
        iterations=2,
        total_tokens_in=0,
        total_tokens_out=0,
        elapsed_ms=0,
        steps=[],
        final_text="",
    )
    out = store.get("r-1")
    assert out["user_intent"] == "updated"
    assert out["outcome"] == "tool_failure"
    assert store.count() == 1


def test_store_truncates_long_user_intent(store):
    long_intent = "x" * 1000
    store.store(
        run_id="r-long",
        user_id="u-1",
        workspace_id="ws-1",
        page="x",
        user_intent=long_intent,
        outcome="success",
        iterations=0,
        total_tokens_in=0,
        total_tokens_out=0,
        elapsed_ms=0,
        steps=[],
        final_text="",
    )
    rows = store.list_recent(user_id="u-1")
    # Indexed column truncated to 256
    assert len(rows[0]["user_intent"]) == 256


def test_purge_older_than_rejects_nonpositive(store):
    with pytest.raises(ValueError):
        store.purge_older_than(0)


def test_count_zero_on_fresh_store(store):
    assert store.count() == 0


def test_store_handles_missing_db_gracefully():
    """Store with no db should be a no-op (defensive)."""
    s = TraceStore(db=None)
    s.store(
        run_id="r-1", user_id="u-1", workspace_id="ws-1", page="x",
        user_intent="x", outcome="success", iterations=0,
        total_tokens_in=0, total_tokens_out=0, elapsed_ms=0,
        steps=[], final_text="",
    )
    assert s.get("r-1") is None
    assert s.list_recent(user_id="u-1") == []
    assert s.count() == 0
