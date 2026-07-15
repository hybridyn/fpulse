"""Regression tests for the "two nodes of the same type in one pipeline" bug.

Several transform nodes used to stage their input under a HARDCODED internal
DuckDB view name (e.g. ``__derived_input``, ``__copy_passthrough``) and then
return a LAZY relation that referenced that view. When a pipeline chained TWO
nodes of the same type, the second node re-registered the shared view name with
a relation that transitively referenced itself, and DuckDB aborted the run with::

    Binder Error: infinite recursion detected: attempting to recursively
    bind view "__derived_input"

The fix scopes every such internal view/temp-table name by the active step id
(ExecutionContext.scoped_name / register_scoped). These tests build pipelines
that chain two (or more) nodes of the same type and assert the run succeeds —
they fail hard on the original bug.
"""

import pytest

from fpulse.ir.schema import Workflow, Step, StepType, StepConnection
from fpulse.engine.executor import WorkflowExecutor


def _linear(steps: list[Step]) -> list[StepConnection]:
    """Wire steps head-to-tail (s0 -> s1 -> s2 -> ...)."""
    return [
        StepConnection(from_step=steps[i].id, to_step=steps[i + 1].id)
        for i in range(len(steps) - 1)
    ]


def _run(executor: WorkflowExecutor, wf: Workflow):
    result = executor.execute_workflow(wf)
    if result.status != "success":
        errs = {
            sid: sr.error
            for sid, sr in result.step_results.items()
            if getattr(sr, "status", "") == "error"
        }
        pytest.fail(f"workflow failed: {errs}")
    return result


class TestSameTypeNodeChains:
    def test_two_derived_columns_chained(self, sample_csv_file, temp_data_dir):
        """The exact bug-report repro: source -> derived -> derived."""
        executor = WorkflowExecutor(data_dir=temp_data_dir)
        steps = [
            Step(id="s1", type=StepType.CSV_SOURCE, params={"file_path": "orders.csv"}),
            Step(id="s2", type=StepType.DERIVED_COLUMN,
                 params={"columns": [{"name": "a", "expression": "amount * 0.2"}]}),
            Step(id="s3", type=StepType.DERIVED_COLUMN,
                 params={"columns": [{"name": "b", "expression": "amount - 10"}]}),
        ]
        wf = Workflow(id="two-derived", steps=steps, connections=_linear(steps))
        result = _run(executor, wf)
        last = result.step_results["s3"]
        assert last.row_count == 5
        assert "a" in last.columns and "b" in last.columns

    def test_two_copy_data_passthrough_chained(self, sample_csv_file, temp_data_dir):
        """source -> copy_data -> copy_data (both passthrough)."""
        executor = WorkflowExecutor(data_dir=temp_data_dir)
        steps = [
            Step(id="s1", type=StepType.CSV_SOURCE, params={"file_path": "orders.csv"}),
            Step(id="s2", type=StepType.COPY_DATA, params={}),
            Step(id="s3", type=StepType.COPY_DATA, params={}),
        ]
        wf = Workflow(id="two-copy", steps=steps, connections=_linear(steps))
        result = _run(executor, wf)
        assert result.step_results["s3"].row_count == 5

    def test_mixed_repro_copy_derived_sort_copy(self, sample_csv_file, temp_data_dir):
        """copy_data -> derived -> sort -> copy_data."""
        executor = WorkflowExecutor(data_dir=temp_data_dir)
        steps = [
            Step(id="s1", type=StepType.CSV_SOURCE, params={"file_path": "orders.csv"}),
            Step(id="s2", type=StepType.COPY_DATA, params={}),
            Step(id="s3", type=StepType.DERIVED_COLUMN,
                 params={"columns": [{"name": "bonus", "expression": "amount * 0.1"}]}),
            Step(id="s4", type=StepType.SORT, params={"sort_by": ["amount DESC"]}),
            Step(id="s5", type=StepType.COPY_DATA, params={}),
        ]
        wf = Workflow(id="mixed-repro", steps=steps, connections=_linear(steps))
        result = _run(executor, wf)
        assert result.step_results["s5"].row_count == 5
        assert "bonus" in result.step_results["s5"].columns

    # One entry per single-input transform node that stages its input through a
    # scoped internal view. Only nodes whose params compose when applied TWICE
    # with the same config live here (identical-params chain).
    _CHAIN_CASES = {
        "filter": (StepType.FILTER, {"condition": "amount > 0"}),
        "sort": (StepType.SORT, {"sort_by": ["amount"]}),
        "typecast": (StepType.TYPECAST, {"casts": {"amount": "VARCHAR"}}),
        "sample": (StepType.SAMPLE, {"mode": "rows", "count": 100, "method": "first"}),
        "validate": (StepType.VALIDATE,
                     {"rules": [{"name": "positive", "condition": "amount > 0"}]}),
    }

    @pytest.mark.parametrize("name", sorted(_CHAIN_CASES))
    def test_two_of_same_transform_type(self, name, sample_csv_file, temp_data_dir):
        step_type, params = self._CHAIN_CASES[name]
        executor = WorkflowExecutor(data_dir=temp_data_dir)
        steps = [
            Step(id="s1", type=StepType.CSV_SOURCE, params={"file_path": "orders.csv"}),
            Step(id="s2", type=step_type, params=dict(params)),
            Step(id="s3", type=step_type, params=dict(params)),
        ]
        wf = Workflow(id=f"two-{name}", steps=steps, connections=_linear(steps))
        # Just needs to complete without the recursion binder error.
        _run(executor, wf)

    def test_two_renames_chained(self, sample_csv_file, temp_data_dir):
        """Two Rename nodes with composable mappings (name->customer->client)."""
        executor = WorkflowExecutor(data_dir=temp_data_dir)
        steps = [
            Step(id="s1", type=StepType.CSV_SOURCE, params={"file_path": "orders.csv"}),
            Step(id="s2", type=StepType.RENAME, params={"mappings": {"name": "customer"}}),
            Step(id="s3", type=StepType.RENAME, params={"mappings": {"customer": "client"}}),
        ]
        wf = Workflow(id="two-rename", steps=steps, connections=_linear(steps))
        result = _run(executor, wf)
        assert "client" in result.step_results["s3"].columns

    def test_two_aggregates_chained(self, sample_csv_file, temp_data_dir):
        """Two Aggregate nodes; the second aggregates the first's output column."""
        executor = WorkflowExecutor(data_dir=temp_data_dir)
        steps = [
            Step(id="s1", type=StepType.CSV_SOURCE, params={"file_path": "orders.csv"}),
            Step(id="s2", type=StepType.AGGREGATE,
                 params={"group_by": ["status"],
                         "functions": [{"column": "amount", "function": "SUM", "alias": "total"}]}),
            Step(id="s3", type=StepType.AGGREGATE,
                 params={"group_by": [],
                         "functions": [{"column": "total", "function": "SUM", "alias": "grand_total"}]}),
        ]
        wf = Workflow(id="two-agg", steps=steps, connections=_linear(steps))
        result = _run(executor, wf)
        assert "grand_total" in result.step_results["s3"].columns

    def test_three_derived_columns_chained(self, sample_csv_file, temp_data_dir):
        """More than two, to be sure scoping holds along a longer chain."""
        executor = WorkflowExecutor(data_dir=temp_data_dir)
        steps = [
            Step(id="s1", type=StepType.CSV_SOURCE, params={"file_path": "orders.csv"}),
            Step(id="s2", type=StepType.DERIVED_COLUMN,
                 params={"columns": [{"name": "a", "expression": "amount + 1"}]}),
            Step(id="s3", type=StepType.DERIVED_COLUMN,
                 params={"columns": [{"name": "b", "expression": "amount + 2"}]}),
            Step(id="s4", type=StepType.DERIVED_COLUMN,
                 params={"columns": [{"name": "c", "expression": "amount + 3"}]}),
        ]
        wf = Workflow(id="three-derived", steps=steps, connections=_linear(steps))
        result = _run(executor, wf)
        cols = result.step_results["s4"].columns
        assert {"a", "b", "c"}.issubset(set(cols))
