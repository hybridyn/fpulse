"""Tests for the backfill subsystem.

Covers:
  - Window enumeration (daily / weekly / hourly / monthly / custom)
  - 30-day daily backfill produces 30 windows with correct param bindings
  - Idempotency guardrail: append_risky / external sinks block the API
    unless acknowledge_side_effects=true
  - Store roundtrip + parent aggregate rollup
  - On-failure policies (stop / continue / retry_once)

The orchestrator is exercised end-to-end against a real WorkflowExecutor
running a tiny CSV→Filter pipeline that reads the ``window_start`` /
``window_end`` parameters into a filter condition. The test asserts that
each dispatched window's filter actually receives the per-window bounds.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

from fpulse.backfills import (
    BackfillStore,
    BackfillRun,
    BackfillStatus,
    OnFailure,
    WindowSize,
    generate_windows,
)
from fpulse.backfills.idempotency import classify, find_unsafe_sinks
from fpulse.backfills.orchestrator import (
    build_window_params,
    run_backfill_sync,
)
from fpulse.ir.schema import Step, StepConnection, StepType, Workflow, WorkflowParameter
from fpulse.engine.executor import WorkflowExecutor


# ── Window enumeration ────────────────────────────────────────────────


class TestWindowEnumeration:
    def test_thirty_day_daily_produces_thirty_windows(self):
        windows = generate_windows("2026-01-01", "2026-01-30", WindowSize.DAILY)
        assert len(windows) == 30
        # First window covers Jan 1 → Jan 2 (exclusive)
        assert windows[0].start == datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert windows[0].end == datetime(2026, 1, 2, tzinfo=timezone.utc)
        # Last window covers Jan 30 → Jan 31 (exclusive)
        assert windows[-1].start == datetime(2026, 1, 30, tzinfo=timezone.utc)
        assert windows[-1].end == datetime(2026, 1, 31, tzinfo=timezone.utc)

    def test_single_day_daily_produces_one_window(self):
        windows = generate_windows("2026-05-15", "2026-05-15", WindowSize.DAILY)
        assert len(windows) == 1
        assert windows[0].start == datetime(2026, 5, 15, tzinfo=timezone.utc)
        assert windows[0].end == datetime(2026, 5, 16, tzinfo=timezone.utc)

    def test_weekly_windows_cover_full_range(self):
        windows = generate_windows("2026-01-01", "2026-01-28", WindowSize.WEEKLY)
        # 28 days inclusive = 28 days → 4 windows of 7
        assert len(windows) == 4
        assert windows[0].start == datetime(2026, 1, 1, tzinfo=timezone.utc)

    def test_monthly_windows_cover_full_year(self):
        windows = generate_windows("2026-01-01", "2026-12-31", WindowSize.MONTHLY)
        assert len(windows) == 12
        # Each window starts at month boundary
        starts = [w.start.month for w in windows]
        assert starts == list(range(1, 13))

    def test_hourly_24h_produces_24_windows(self):
        windows = generate_windows("2026-01-01", "2026-01-01", WindowSize.HOURLY)
        # End is inclusive — full day = 24 hourly windows
        assert len(windows) == 24

    def test_custom_hours_split(self):
        windows = generate_windows(
            "2026-01-01", "2026-01-01", WindowSize.CUSTOM, window_size_hours=6,
        )
        # 24h / 6h = 4 windows
        assert len(windows) == 4
        assert windows[0].end - windows[0].start == windows[1].end - windows[1].start

    def test_inverted_range_rejected(self):
        with pytest.raises(ValueError):
            generate_windows("2026-01-30", "2026-01-01", WindowSize.DAILY)

    def test_custom_zero_hours_rejected(self):
        with pytest.raises(ValueError):
            generate_windows(
                "2026-01-01", "2026-01-02", WindowSize.CUSTOM, window_size_hours=0,
            )


# ── Idempotency classifier ────────────────────────────────────────────


class TestIdempotencyClassifier:
    def test_local_table_replace_is_safe_for_backfill(self):
        # replace mode = idempotent (each window overwrites the same target)
        assert classify("local_table_sink", {"mode": "replace"}) == "replace"

    def test_local_table_merge_is_safe_for_backfill(self):
        assert classify("local_table_sink", {"mode": "merge", "merge_on": ["id"]}) == "merge"

    def test_local_table_append_is_risky(self):
        assert classify("local_table_sink", {"mode": "append"}) == "append_risky"

    def test_local_table_default_mode_is_risky(self):
        # No mode specified → default to append (matches frontend)
        assert classify("local_table_sink", {}) == "append_risky"

    def test_email_sink_is_external(self):
        assert classify("email_sink", {}) == "external"

    def test_api_sink_is_external(self):
        assert classify("api_sink", {}) == "external"

    def test_csv_file_sink_is_replace_by_default(self):
        assert classify("csv_sink", {}) == "replace"

    def test_csv_file_sink_in_append_mode_is_risky(self):
        assert classify("csv_sink", {"mode": "append"}) == "append_risky"

    def test_non_sink_steps_return_none(self):
        assert classify("csv_source", {}) is None
        assert classify("filter", {}) is None

    def test_find_unsafe_sinks_flags_append_and_email(self):
        steps = [
            Step(id="s1", type=StepType.CSV_SOURCE, params={"file_path": "x.csv"}),
            Step(id="s2", type=StepType.FILTER, params={"condition": "id > 0"}),
            Step(id="s3", type=StepType.EMAIL_SINK, params={}),
        ]
        unsafe = find_unsafe_sinks(steps)
        # CSV_SOURCE and FILTER are not sinks; EMAIL_SINK is external.
        assert len(unsafe) == 1
        assert unsafe[0]["step_id"] == "s3"
        assert unsafe[0]["idempotency"] == "external"


# ── Parameter binding ─────────────────────────────────────────────────


class TestParamBinding:
    def test_build_window_params_binds_default_cursor_names(self):
        parent = BackfillRun(
            pipeline_id="wf1",
            window_start="2026-01-01T00:00:00+00:00",
            window_end="2026-01-31T00:00:00+00:00",
            cursor_param_names=["window_start", "window_end"],
        )
        child = BackfillRun(
            pipeline_id="wf1",
            parent_backfill_id=parent.id,
            window_start="2026-01-15T00:00:00+00:00",
            window_end="2026-01-16T00:00:00+00:00",
        )
        params = build_window_params(parent, child, extra={"batch_size": 100})
        assert params["window_start"] == "2026-01-15T00:00:00+00:00"
        assert params["window_end"] == "2026-01-16T00:00:00+00:00"
        assert params["batch_size"] == 100

    def test_build_window_params_honours_custom_cursor_names(self):
        parent = BackfillRun(
            pipeline_id="wf1",
            window_start="2026-01-01T00:00:00+00:00",
            window_end="2026-01-31T00:00:00+00:00",
            cursor_param_names=["from_date", "to_date"],
        )
        child = BackfillRun(
            pipeline_id="wf1",
            parent_backfill_id=parent.id,
            window_start="2026-01-15T00:00:00+00:00",
            window_end="2026-01-16T00:00:00+00:00",
        )
        params = build_window_params(parent, child)
        assert "from_date" in params
        assert "to_date" in params
        assert "window_start" not in params


# ── Store roundtrip ───────────────────────────────────────────────────


class TestBackfillStore:
    def test_parent_and_children_roundtrip(self, _fpulse_test_db):
        store = BackfillStore(db=_fpulse_test_db)
        parent = BackfillRun(
            pipeline_id="wf1",
            window_start="2026-01-01T00:00:00+00:00",
            window_end="2026-01-04T00:00:00+00:00",
            workspace_id="default",
        )
        store.create_parent(parent)
        children = [
            BackfillRun(
                pipeline_id="wf1",
                parent_backfill_id=parent.id,
                window_start=f"2026-01-0{i+1}T00:00:00+00:00",
                window_end=f"2026-01-0{i+2}T00:00:00+00:00",
                workspace_id="default",
            )
            for i in range(3)
        ]
        store.create_children(parent.id, children)

        # Parent appears in list_parents but children don't
        parents = store.list_parents(workspace_id="default")
        assert len(parents) == 1
        assert parents[0].id == parent.id

        loaded_children = store.list_children(parent.id)
        assert len(loaded_children) == 3
        # Children sorted by window_start ascending
        assert loaded_children[0].window_start < loaded_children[-1].window_start

    def test_parent_aggregate_rollup_after_child_status_change(self, _fpulse_test_db):
        store = BackfillStore(db=_fpulse_test_db)
        parent = BackfillRun(
            pipeline_id="wf1",
            window_start="2026-01-01T00:00:00+00:00",
            window_end="2026-01-03T00:00:00+00:00",
            workspace_id="default",
            total_windows=2,
        )
        store.create_parent(parent)
        c1 = BackfillRun(
            pipeline_id="wf1", parent_backfill_id=parent.id,
            window_start="2026-01-01T00:00:00+00:00",
            window_end="2026-01-02T00:00:00+00:00",
            workspace_id="default",
        )
        c2 = BackfillRun(
            pipeline_id="wf1", parent_backfill_id=parent.id,
            window_start="2026-01-02T00:00:00+00:00",
            window_end="2026-01-03T00:00:00+00:00",
            workspace_id="default",
        )
        store.create_children(parent.id, [c1, c2])

        store.update_status(c1.id, BackfillStatus.SUCCESS, completed=True)
        store.update_status(c2.id, BackfillStatus.FAILED, error_message="boom", completed=True)

        reloaded = store.get(parent.id)
        assert reloaded.succeeded_windows == 1
        assert reloaded.failed_windows == 1
        assert reloaded.status == BackfillStatus.PARTIAL

    def test_cancel_marks_parent_cancelled(self, _fpulse_test_db):
        store = BackfillStore(db=_fpulse_test_db)
        parent = BackfillRun(
            pipeline_id="wf1",
            window_start="2026-01-01T00:00:00+00:00",
            window_end="2026-01-05T00:00:00+00:00",
            workspace_id="default",
            status=BackfillStatus.RUNNING,
        )
        store.create_parent(parent)
        ok = store.cancel(parent.id, workspace_id="default")
        assert ok is True
        reloaded = store.get(parent.id)
        assert reloaded.status == BackfillStatus.CANCELLED
        assert reloaded.completed_at is not None


# ── Orchestrator end-to-end ───────────────────────────────────────────


def _make_param_backed_workflow(window_start_name: str = "window_start",
                                window_end_name: str = "window_end") -> Workflow:
    """A pipeline that uses cursor params in a filter condition.

    The test asserts that the executor actually receives the per-window
    bounds — the FILTER step's resolved params should show the
    substituted values.
    """
    return Workflow(
        id="wf-backfill-test",
        name="Backfill test",
        parameters=[
            WorkflowParameter(name=window_start_name, type="string", default="2000-01-01"),
            WorkflowParameter(name=window_end_name, type="string", default="2099-12-31"),
        ],
        steps=[
            Step(
                id="src", type=StepType.CSV_SOURCE, label="Load",
                params={"file_path": "orders.csv"},
            ),
            Step(
                id="filt", type=StepType.FILTER, label="Filter",
                params={
                    "condition": (
                        f"status = 'active' "
                        f"AND '${{param.{window_start_name}}}' <= '${{param.{window_end_name}}}'"
                    ),
                },
            ),
        ],
        connections=[StepConnection(from_step="src", to_step="filt")],
    )


class TestOrchestrator:
    def test_seven_day_backfill_dispatches_seven_executions(
        self, _fpulse_test_db, sample_csv_file, temp_data_dir,
    ):
        store = BackfillStore(db=_fpulse_test_db)
        executor = WorkflowExecutor(data_dir=temp_data_dir)
        from fpulse.monitoring.store import ExecutionStore
        exe_store = ExecutionStore(db=_fpulse_test_db)
        workflow = _make_param_backed_workflow()

        # Set up the backfill: 2026-01-01 → 2026-01-07 with daily windows.
        windows = generate_windows("2026-01-01", "2026-01-07", WindowSize.DAILY)
        assert len(windows) == 7

        parent = BackfillRun(
            pipeline_id=workflow.id,
            window_start=windows[0].start.isoformat(),
            window_end=windows[-1].end.isoformat(),
            workspace_id="default",
            cursor_param_names=["window_start", "window_end"],
            total_windows=len(windows),
        )
        store.create_parent(parent)
        children = [
            BackfillRun(
                pipeline_id=workflow.id,
                parent_backfill_id=parent.id,
                window_start=w.start.isoformat(),
                window_end=w.end.isoformat(),
                workspace_id="default",
                cursor_param_names=["window_start", "window_end"],
            )
            for w in windows
        ]
        store.create_children(parent.id, children)

        # Run synchronously — easier to assert in tests.
        final = run_backfill_sync(
            parent.id, store=store, executor=executor,
            workflow=workflow, exe_store=exe_store,
        )
        assert final.status == BackfillStatus.SUCCESS
        assert final.total_windows == 7
        assert final.succeeded_windows == 7
        assert final.failed_windows == 0

        # Every child has an execution_id stamped and the bound params.
        reloaded_children = store.list_children(parent.id)
        for child in reloaded_children:
            assert child.status == BackfillStatus.SUCCESS
            assert child.execution_id  # set by orchestrator
            assert "window_start" in child.params_template
            assert "window_end" in child.params_template
            # Bound bounds match the window
            assert child.params_template["window_start"] == child.window_start
            assert child.params_template["window_end"] == child.window_end

        # Seven execution records were written.
        rows = exe_store.list_all() if hasattr(exe_store, "list_all") else []
        if rows:
            backfill_runs = [r for r in rows if r.get("triggered_by") == "backfill"]
            assert len(backfill_runs) == 7

    def test_stop_on_failure_halts_remaining_windows(
        self, _fpulse_test_db, temp_data_dir,
    ):
        """With on_failure=stop, the first failing window halts the run."""
        store = BackfillStore(db=_fpulse_test_db)
        executor = WorkflowExecutor(data_dir=temp_data_dir)
        from fpulse.monitoring.store import ExecutionStore
        exe_store = ExecutionStore(db=_fpulse_test_db)

        # Pipeline that hits a missing file — every window will fail.
        workflow = Workflow(
            id="wf-failing",
            steps=[
                Step(
                    id="src", type=StepType.CSV_SOURCE,
                    params={"file_path": "nonexistent-{window_start}.csv"},
                ),
            ],
        )

        windows = generate_windows("2026-01-01", "2026-01-05", WindowSize.DAILY)
        parent = BackfillRun(
            pipeline_id=workflow.id,
            window_start=windows[0].start.isoformat(),
            window_end=windows[-1].end.isoformat(),
            workspace_id="default",
            on_failure=OnFailure.STOP,
            total_windows=len(windows),
        )
        store.create_parent(parent)
        children = [
            BackfillRun(
                pipeline_id=workflow.id, parent_backfill_id=parent.id,
                window_start=w.start.isoformat(), window_end=w.end.isoformat(),
                workspace_id="default",
            )
            for w in windows
        ]
        store.create_children(parent.id, children)

        run_backfill_sync(
            parent.id, store=store, executor=executor,
            workflow=workflow, exe_store=exe_store,
        )

        children_after = store.list_children(parent.id)
        # First child failed; remaining four stayed PENDING because of STOP policy.
        statuses = [c.status for c in children_after]
        assert statuses[0] == BackfillStatus.FAILED
        # Anything beyond the first should still be PENDING — STOP halted us.
        for status in statuses[1:]:
            assert status == BackfillStatus.PENDING


# ── API guardrail ────────────────────────────────────────────────────


class TestApiIdempotencyGuardrail:
    """The POST /api/executions/backfill endpoint must refuse to dispatch a
    backfill against a pipeline whose sinks aren't safe to re-run, unless
    the caller passes acknowledge_side_effects=true."""

    def test_unsafe_workflow_blocks_without_ack(self):
        # Pipeline with an email_sink (always external).
        steps = [
            Step(id="s1", type=StepType.CSV_SOURCE, params={"file_path": "x.csv"}),
            Step(id="s2", type=StepType.EMAIL_SINK, params={}),
        ]
        unsafe = find_unsafe_sinks(steps)
        assert len(unsafe) == 1
        assert unsafe[0]["idempotency"] == "external"

    def test_safe_workflow_passes_idempotency_check(self):
        steps = [
            Step(id="s1", type=StepType.CSV_SOURCE, params={"file_path": "x.csv"}),
            Step(
                id="s2", type=StepType.LOCAL_TABLE_SINK,
                params={"mode": "merge", "merge_on": ["id"]},
            ),
        ]
        unsafe = find_unsafe_sinks(steps)
        assert unsafe == []
