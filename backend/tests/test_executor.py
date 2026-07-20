"""Unit tests for WorkflowExecutor — execution, topological sort, preview."""

import os
import pytest
from fpulse.ir.schema import Workflow, Step, StepType, StepConnection
from fpulse.engine.executor import WorkflowExecutor


class TestExecutorBasic:
    def test_execute_csv_workflow(self, sample_csv_file, temp_data_dir):
        """Execute a simple CSV→Filter→Output pipeline."""
        executor = WorkflowExecutor(data_dir=temp_data_dir)
        wf = Workflow(
            id="test-exec",
            steps=[
                Step(id="s1", type=StepType.CSV_SOURCE, label="Load",
                     params={"file_path": "orders.csv"}),
                Step(id="s2", type=StepType.FILTER, label="Filter",
                     params={"condition": "status = 'active'"}),
            ],
            connections=[StepConnection(from_step="s1", to_step="s2")],
        )
        result = executor.execute_workflow(wf)
        assert result.status == "success"
        assert "s1" in result.step_results
        assert "s2" in result.step_results
        assert result.step_results["s1"].status == "success"
        assert result.step_results["s2"].status == "success"
        assert result.step_results["s2"].row_count == 3  # 3 active rows

    def test_execute_single_source(self, sample_csv_file, temp_data_dir):
        executor = WorkflowExecutor(data_dir=temp_data_dir)
        wf = Workflow(
            id="test-src",
            steps=[
                Step(id="s1", type=StepType.CSV_SOURCE, params={"file_path": "orders.csv"}),
            ],
        )
        result = executor.execute_workflow(wf)
        assert result.status == "success"
        assert result.step_results["s1"].row_count == 5

    def test_execute_returns_columns(self, sample_csv_file, temp_data_dir):
        executor = WorkflowExecutor(data_dir=temp_data_dir)
        wf = Workflow(
            id="test-cols",
            steps=[
                Step(id="s1", type=StepType.CSV_SOURCE, params={"file_path": "orders.csv"}),
            ],
        )
        result = executor.execute_workflow(wf)
        assert "id" in result.step_results["s1"].columns
        assert "name" in result.step_results["s1"].columns

    def test_execute_returns_sample_data(self, sample_csv_file, temp_data_dir):
        executor = WorkflowExecutor(data_dir=temp_data_dir)
        wf = Workflow(
            id="test-data",
            steps=[
                Step(id="s1", type=StepType.CSV_SOURCE, params={"file_path": "orders.csv"}),
            ],
        )
        result = executor.execute_workflow(wf)
        assert len(result.step_results["s1"].sample_data) > 0

    def test_execute_has_duration(self, sample_csv_file, temp_data_dir):
        executor = WorkflowExecutor(data_dir=temp_data_dir)
        wf = Workflow(
            id="test-dur",
            steps=[
                Step(id="s1", type=StepType.CSV_SOURCE, params={"file_path": "orders.csv"}),
            ],
        )
        result = executor.execute_workflow(wf)
        assert result.duration_ms > 0
        assert result.step_results["s1"].duration_ms > 0


class TestExecutorValidation:
    def test_invalid_workflow_returns_error(self, temp_data_dir):
        executor = WorkflowExecutor(data_dir=temp_data_dir)
        wf = Workflow(id="test-invalid", steps=[])
        result = executor.execute_workflow(wf)
        assert result.status == "error"
        assert "validation" in result.step_results

    def test_missing_file_returns_error(self, temp_data_dir):
        executor = WorkflowExecutor(data_dir=temp_data_dir)
        wf = Workflow(
            id="test-missing",
            steps=[
                Step(id="s1", type=StepType.CSV_SOURCE,
                     params={"file_path": "nonexistent.csv"}),
            ],
        )
        result = executor.execute_workflow(wf)
        assert result.status == "error"
        assert result.step_results["s1"].status == "error"


class TestExecutorStepExecution:
    def test_execute_step(self, sample_csv_file, temp_data_dir):
        executor = WorkflowExecutor(data_dir=temp_data_dir)
        wf = Workflow(
            id="test-step",
            steps=[
                Step(id="s1", type=StepType.CSV_SOURCE, params={"file_path": "orders.csv"}),
                Step(id="s2", type=StepType.FILTER, params={"condition": "status = 'active'"}),
            ],
            connections=[StepConnection(from_step="s1", to_step="s2")],
        )
        result = executor.execute_step(wf, "s2")
        assert result.status == "success"
        assert result.row_count == 3

    def test_execute_step_trace_resolves_parameters(self, sample_csv_file, temp_data_dir):
        """Test Node (execute_step_trace) must resolve ${param.x} against the
        DECLARED defaults — like the editor Run — so a parameterized step
        previews instead of sending a literal ${...} into the SQL (which
        failed with a DuckDB parser error before 2026-06-16)."""
        from fpulse.ir.schema import WorkflowParameter
        executor = WorkflowExecutor(data_dir=temp_data_dir)
        wf = Workflow(
            id="test-step-param",
            steps=[
                Step(id="s1", type=StepType.CSV_SOURCE, params={"file_path": "orders.csv"}),
                Step(id="s2", type=StepType.FILTER,
                     params={"condition": "amount > ${param.min_amount}"}),
            ],
            connections=[StepConnection(from_step="s1", to_step="s2")],
            parameters=[WorkflowParameter(name="min_amount", type="int", default=0)],
        )
        trace = executor.execute_step_trace(wf, "s2")
        res = trace.step_results["s2"]
        assert res.status == "success", f"param not resolved into SQL: {res.error}"

    def test_execute_step_trace_resolves_runtime_expressions(self, sample_csv_file, temp_data_dir):
        """The {{ }} expression engine resolves per-step in execute_step_trace
        (the Test Node path), proven via $now: if it resolves, the condition
        '<2026-...>' LIKE '20%' keeps every row; if it stayed the literal
        '{{ $now }}' the filter would drop them all."""
        executor = WorkflowExecutor(data_dir=temp_data_dir)
        wf = Workflow(
            id="test-expr",
            steps=[
                Step(id="s1", type=StepType.CSV_SOURCE, params={"file_path": "orders.csv"}),
                Step(id="s2", type=StepType.FILTER,
                     params={"condition": "'{{ $now }}' LIKE '20%'"}),
            ],
            connections=[StepConnection(from_step="s1", to_step="s2")],
        )
        trace = executor.execute_step_trace(wf, "s2")
        res = trace.step_results["s2"]
        src = trace.step_results["s1"]
        assert res.status == "success", "expression step errored: " + str(res.error)
        assert res.row_count == src.row_count and res.row_count > 0, (
            "$now did not resolve — rows %s of %s" % (res.row_count, src.row_count)
        )

    def test_execute_step_not_found(self, temp_data_dir):
        executor = WorkflowExecutor(data_dir=temp_data_dir)
        wf = Workflow(id="test", steps=[])
        result = executor.execute_step(wf, "nonexistent")
        assert result.status == "error"
        assert "not found" in result.error.lower()


class TestExecutorTopologicalSort:
    def test_linear_pipeline_order(self, sample_csv_file, temp_data_dir):
        """Steps should execute in dependency order."""
        executor = WorkflowExecutor(data_dir=temp_data_dir)
        wf = Workflow(
            id="test-topo",
            steps=[
                Step(id="s3", type=StepType.OUTPUT, params={"format": "csv", "path": "out.csv"}),
                Step(id="s1", type=StepType.CSV_SOURCE, params={"file_path": "orders.csv"}),
                Step(id="s2", type=StepType.FILTER, params={"condition": "amount > 0"}),
            ],
            connections=[
                StepConnection(from_step="s1", to_step="s2"),
                StepConnection(from_step="s2", to_step="s3"),
            ],
        )
        order = executor._topological_sort(wf)
        ids = [s.id for s in order]
        assert ids.index("s1") < ids.index("s2")
        assert ids.index("s2") < ids.index("s3")

    def test_diamond_dependency(self, temp_data_dir):
        """Test diamond: s1 → s2, s1 → s3, s2+s3 → s4."""
        executor = WorkflowExecutor(data_dir=temp_data_dir)
        wf = Workflow(
            id="test-diamond",
            steps=[
                Step(id="s1", type=StepType.CSV_SOURCE, params={"file_path": "x.csv"}),
                Step(id="s2", type=StepType.FILTER, params={"condition": "x > 0"}),
                Step(id="s3", type=StepType.FILTER, params={"condition": "y > 0"}),
                Step(id="s4", type=StepType.JOIN, params={"join_key": "id"}),
            ],
            connections=[
                StepConnection(from_step="s1", to_step="s2"),
                StepConnection(from_step="s1", to_step="s3"),
                StepConnection(from_step="s2", to_step="s4"),
                StepConnection(from_step="s3", to_step="s4"),
            ],
        )
        order = executor._topological_sort(wf)
        ids = [s.id for s in order]
        assert ids.index("s1") < ids.index("s2")
        assert ids.index("s1") < ids.index("s3")
        assert ids.index("s2") < ids.index("s4")
        assert ids.index("s3") < ids.index("s4")


class TestExecutorChainedPipeline:
    def test_csv_filter_deduplicate(self, sample_csv_file, temp_data_dir):
        executor = WorkflowExecutor(data_dir=temp_data_dir)
        wf = Workflow(
            id="test-chain",
            steps=[
                Step(id="s1", type=StepType.CSV_SOURCE, params={"file_path": "orders.csv"}),
                Step(id="s2", type=StepType.FILTER, params={"condition": "status = 'active'"}),
                Step(id="s3", type=StepType.DEDUPLICATE, params={"key": "name"}),
            ],
            connections=[
                StepConnection(from_step="s1", to_step="s2"),
                StepConnection(from_step="s2", to_step="s3"),
            ],
        )
        result = executor.execute_workflow(wf)
        assert result.status == "success"
        assert result.step_results["s1"].row_count == 5
        assert result.step_results["s2"].row_count == 3
        assert result.step_results["s3"].row_count == 3  # all unique names

    def test_csv_aggregate(self, sample_csv_file, temp_data_dir):
        executor = WorkflowExecutor(data_dir=temp_data_dir)
        wf = Workflow(
            id="test-agg",
            steps=[
                Step(id="s1", type=StepType.CSV_SOURCE, params={"file_path": "orders.csv"}),
                # Aggregate node moved from `functions: dict` to
                # `functions: list[dict]` with per-function {column, function, alias}.
                Step(id="s2", type=StepType.AGGREGATE, params={
                    "group_by": ["status"],
                    "functions": [
                        {"column": "amount", "function": "SUM", "alias": "total"},
                    ],
                }),
            ],
            connections=[StepConnection(from_step="s1", to_step="s2")],
        )
        result = executor.execute_workflow(wf)
        assert result.status == "success"
        assert result.step_results["s2"].row_count == 2  # active + inactive
