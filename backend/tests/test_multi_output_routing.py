"""Central multi-output branch routing (2026-06-11).

Branch nodes (conditional_split) tag rows with `_split_output`. Until now,
only the few nodes that opted into `get_routed_inputs` (flow_control,
transform) actually consumed a branch — every other node read the full
tagged relation via plain `get_inputs`, so wiring a Filter/Aggregate/sink to
a branch leaked all rows + the tag column.

The executor now routes centrally: before a step runs, if it consumes a
non-`output` port, its inputs are routed + `_split_output`-stripped, so EVERY
node sees only its branch's rows through the normal get_inputs path. These
tests pin that — using Filter (which uses plain get_inputs) as the downstream
— and that ordinary single-output pipelines are byte-for-byte unaffected.
"""
from __future__ import annotations

from fpulse.ir.schema import Workflow, Step, StepType, StepConnection
from fpulse.engine.executor import WorkflowExecutor


def test_conditional_split_branches_reach_plain_downstream(sample_csv_file, temp_data_dir):
    executor = WorkflowExecutor(data_dir=temp_data_dir)
    wf = Workflow(
        id="test-multi-output",
        steps=[
            Step(id="src", type=StepType.CSV_SOURCE, label="Load",
                 params={"file_path": "orders.csv"}),
            Step(id="split", type=StepType.CONDITIONAL_SPLIT, label="Split", params={
                "conditions": [{"name": "active", "condition": "status = 'active'"}],
                "default_output": "other",
                "mode": "first_match",
            }),
            # Filter uses plain ctx.get_inputs — NOT get_routed_inputs. If
            # central routing works, each sees only its branch.
            Step(id="act", type=StepType.FILTER, label="Active", params={"condition": "1=1"}),
            Step(id="oth", type=StepType.FILTER, label="Other", params={"condition": "1=1"}),
        ],
        connections=[
            StepConnection(from_step="src", to_step="split"),
            StepConnection(from_step="split", to_step="act", from_port="active"),
            StepConnection(from_step="split", to_step="oth", from_port="other"),
        ],
    )
    result = executor.execute_workflow(wf)
    assert result.status == "success", result.step_results
    assert result.step_results["act"].status == "success"
    assert result.step_results["oth"].status == "success"

    total = result.step_results["src"].row_count
    n_act = result.step_results["act"].row_count
    n_oth = result.step_results["oth"].row_count
    assert n_act == 3                       # active rows only (matches test_executor)
    assert n_oth >= 1                        # the remainder
    assert n_act + n_oth == total           # exact partition — nothing lost/duplicated


def test_split_output_tag_is_stripped_for_downstream(sample_csv_file, temp_data_dir):
    """A plain downstream node must NOT receive the engine-internal
    `_split_output` column from a branch input."""
    executor = WorkflowExecutor(data_dir=temp_data_dir)
    wf = Workflow(
        id="test-strip-tag",
        steps=[
            Step(id="src", type=StepType.CSV_SOURCE, params={"file_path": "orders.csv"}),
            Step(id="split", type=StepType.CONDITIONAL_SPLIT, params={
                "conditions": [{"name": "active", "condition": "status = 'active'"}],
                "default_output": "other",
            }),
            # Reference a real column; would error if schema were polluted,
            # and the output must not carry _split_output.
            Step(id="d", type=StepType.DERIVED_COLUMN, params={
                "columns": [{"name": "flag", "expression": "1"}],
            }),
        ],
        connections=[
            StepConnection(from_step="src", to_step="split"),
            StepConnection(from_step="split", to_step="d", from_port="active"),
        ],
    )
    result = executor.execute_workflow(wf)
    assert result.status == "success", result.step_results
    cols = result.step_results["d"].columns or []
    colnames = [c.get("name") if isinstance(c, dict) else c for c in cols]
    assert "_split_output" not in colnames
    assert result.step_results["d"].row_count == 3


def test_single_output_pipeline_is_unaffected(sample_csv_file, temp_data_dir):
    """Strict no-op guard: ordinary 'output' edges never trigger routing."""
    executor = WorkflowExecutor(data_dir=temp_data_dir)
    wf = Workflow(
        id="test-single-output",
        steps=[
            Step(id="src", type=StepType.CSV_SOURCE, params={"file_path": "orders.csv"}),
            Step(id="f", type=StepType.FILTER, params={"condition": "status = 'active'"}),
        ],
        connections=[StepConnection(from_step="src", to_step="f")],
    )
    result = executor.execute_workflow(wf)
    assert result.status == "success"
    assert result.step_results["f"].row_count == 3


# ── C1 heterogeneous named outputs (2026-06-15) ──────────────────────────

def test_named_output_context_guards():
    """C1: set/get named output; the reserved 'output' port + empty names are
    never stored (the primary output is always the execute() return value)."""
    import duckdb
    from fpulse.nodes.base import ExecutionContext

    ctx = ExecutionContext(conn=duckdb.connect())
    rel = ctx.conn.sql("SELECT 1 AS a")
    ctx.set_named_output("s1", "report", rel)
    assert ctx.get_named_output("s1", "report") is rel
    assert ctx.get_named_output("s1", "missing") is None
    # 'output' is reserved for the primary return value → never stored/fetched.
    ctx.set_named_output("s1", "output", rel)
    assert ctx.get_named_output("s1", "output") is None
    ctx.set_named_output("s1", "", rel)
    assert ctx.get_named_output("s1", "") is None


def test_data_profile_dual_output_routes_report_and_data(sample_csv_file, temp_data_dir):
    """C1+C2 end-to-end: Data Profile with passthrough_data emits the column
    report on the primary 'output' port AND the original rows on a 'data' port
    (a DIFFERENT schema) — proving heterogeneous multi-output routing."""
    executor = WorkflowExecutor(data_dir=temp_data_dir)
    wf = Workflow(
        id="test-profile-dual",
        steps=[
            Step(id="src", type=StepType.CSV_SOURCE, params={"file_path": "orders.csv"}),
            Step(id="prof", type=StepType.DATA_PROFILE, params={"passthrough_data": True}),
            Step(id="rep", type=StepType.FILTER, params={"condition": "1=1"}),  # ← report
            Step(id="dat", type=StepType.FILTER, params={"condition": "1=1"}),  # ← data
        ],
        connections=[
            StepConnection(from_step="src", to_step="prof"),
            StepConnection(from_step="prof", to_step="rep", from_port="output"),
            StepConnection(from_step="prof", to_step="dat", from_port="data"),
        ],
    )
    result = executor.execute_workflow(wf)
    assert result.status == "success", result.step_results

    def _cols(sid):
        return [c.get("name") if isinstance(c, dict) else c
                for c in (result.step_results[sid].columns or [])]

    rep_cols, dat_cols = _cols("rep"), _cols("dat")
    # report port → profile schema (one row per source column)
    assert "column" in rep_cols and "null_pct" in rep_cols
    # data port → original rows + schema, NOT the report schema
    assert "status" in dat_cols and "column" not in dat_cols
    # row counts: data = source rows; report = at least one column row
    src_rows = result.step_results["src"].row_count
    assert result.step_results["dat"].row_count == src_rows
    assert result.step_results["rep"].row_count >= 1


def test_data_profile_single_output_back_compat(sample_csv_file, temp_data_dir):
    """passthrough_data off (default): no named output, the single downstream
    receives the report exactly as before."""
    executor = WorkflowExecutor(data_dir=temp_data_dir)
    wf = Workflow(
        id="test-profile-single",
        steps=[
            Step(id="src", type=StepType.CSV_SOURCE, params={"file_path": "orders.csv"}),
            Step(id="prof", type=StepType.DATA_PROFILE, params={}),
            Step(id="d", type=StepType.FILTER, params={"condition": "1=1"}),
        ],
        connections=[
            StepConnection(from_step="src", to_step="prof"),
            StepConnection(from_step="prof", to_step="d"),
        ],
    )
    result = executor.execute_workflow(wf)
    assert result.status == "success", result.step_results
    cols = [c.get("name") if isinstance(c, dict) else c
            for c in (result.step_results["d"].columns or [])]
    assert "column" in cols and "null_pct" in cols
