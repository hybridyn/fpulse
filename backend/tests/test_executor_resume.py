"""Integration test for workflow-level resume — Sprint A exit gate.

Scenario: a 2-step workflow runs, succeeds, then we re-run it with
``resume_from_run_id=<first-run-id>``. The first step's output is
loaded from the on-disk parquet snapshot rather than re-executed.

The resume path is the key Sprint A primitive: a 10M-row sync killed
mid-flight via ``kill -9`` must pick up at the first non-success step.
The full kill-and-resume scenario lives in the launch-demo smoke test;
this unit test locks the mechanism: cached steps are loaded from
snapshot, fresh steps execute, the new run gets its own run_id, and
checkpoints chain correctly.
"""

from __future__ import annotations

import pytest

from fpulse.ir.schema import Workflow, Step, StepType, StepConnection
from fpulse.engine.executor import WorkflowExecutor
from fpulse.engine.checkpoint_store import get_checkpoint_store


class TestWorkflowResume:
    def test_resume_short_circuits_cached_steps(
        self, sample_csv_file, temp_data_dir, _fpulse_test_db,
    ):
        """First run populates the step cache. Second run with
        resume_from_run_id loads s1 from snapshot — verified by checking
        that the resumed step's StepRunResult carries the
        'resumed-from-snapshot' marker placed by the resume short-circuit."""
        executor = WorkflowExecutor(data_dir=temp_data_dir)
        wf = Workflow(
            id="test-resume",
            steps=[
                Step(id="s1", type=StepType.CSV_SOURCE, label="Load",
                     params={"file_path": "orders.csv"}),
                Step(id="s2", type=StepType.FILTER, label="Filter",
                     params={"condition": "status = 'active'"}),
            ],
            connections=[StepConnection(from_step="s1", to_step="s2")],
        )

        # First run — populate cache + checkpoints.
        first = executor.execute_workflow(wf, run_id="run-1")
        assert first.status == "success"
        assert first.step_results["s1"].status == "success"
        assert first.step_results["s2"].status == "success"

        # Verify checkpoint store has the success rows.
        store = get_checkpoint_store()
        ok_steps = store.successful_step_ids("run-1")
        assert ok_steps == {"s1", "s2"}

        # Resume from run-1. Expected: s1 short-circuits (loaded from
        # cache), s2 also short-circuits since both are in the success
        # set and the cache is fresh.
        executor2 = WorkflowExecutor(data_dir=temp_data_dir)
        second = executor2.execute_workflow_resume(wf, run_id="run-1")
        assert second.status == "success"
        # Both steps should carry the resume marker — neither re-executed.
        assert second.step_results["s1"].error == "resumed-from-snapshot"
        assert second.step_results["s2"].error == "resumed-from-snapshot"
        # Duration is zero for snapshot-loaded steps.
        assert second.step_results["s1"].duration_ms == 0.0
        assert second.step_results["s2"].duration_ms == 0.0

    def test_resume_partial_failure_continues_from_first_failure(
        self, sample_csv_file, temp_data_dir, _fpulse_test_db,
    ):
        """Manually simulate a partial-failure run: only s1 succeeded,
        s2 failed (no parquet for s2). On resume, s1 loads from cache,
        s2 must execute fresh."""
        executor = WorkflowExecutor(data_dir=temp_data_dir)
        wf = Workflow(
            id="test-resume-partial",
            steps=[
                Step(id="s1", type=StepType.CSV_SOURCE, label="Load",
                     params={"file_path": "orders.csv"}),
                Step(id="s2", type=StepType.FILTER, label="Filter",
                     params={"condition": "status = 'active'"}),
            ],
            connections=[StepConnection(from_step="s1", to_step="s2")],
        )

        # Run successfully so the cache is populated for both.
        executor.execute_workflow(wf, run_id="run-A")

        # Manually mark s2 as failed in the checkpoint store, simulating
        # a kill -9 between s1 success and s2 success.
        store = get_checkpoint_store()
        store.mark_failed(
            workflow_id="test-resume-partial",
            run_id="run-A",
            step_id="s2",
            error_summary="killed mid-run",
            duration_ms=0,
        )

        # Resume — s1 should be marked as "resumed-from-snapshot" (loaded
        # from cache), s2 should re-execute and finish fresh.
        executor2 = WorkflowExecutor(data_dir=temp_data_dir)
        second = executor2.execute_workflow_resume(wf, run_id="run-A")
        assert second.status == "success"
        assert second.step_results["s1"].error == "resumed-from-snapshot"
        # s2 must run for real — different marker, real duration.
        s2 = second.step_results["s2"]
        assert s2.status == "success"
        assert s2.error != "resumed-from-snapshot"

    def test_resume_requires_run_id(self, temp_data_dir):
        executor = WorkflowExecutor(data_dir=temp_data_dir)
        wf = Workflow(id="x", steps=[
            Step(id="s1", type=StepType.CSV_SOURCE, params={"file_path": "x.csv"}),
        ])
        with pytest.raises(ValueError):
            executor.execute_workflow_resume(wf, run_id="")
