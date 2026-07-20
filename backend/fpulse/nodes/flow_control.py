"""
Flow Control and Action nodes for orchestration.

Flow Control:
  IF_CONDITION, SWITCH_CASE, FOREACH_LOOP, UNTIL_LOOP,
  WAIT_DELAY, SET_VARIABLE, EXECUTE_PIPELINE

Action Nodes:
  HTTP_REQUEST, WEBHOOK_TRIGGER, CODE_SCRIPT, SEND_EMAIL,
  SLACK_NOTIFY, COPY_DATA, DELETE_DATA, GET_METADATA
"""

from __future__ import annotations

import json
import logging
import smtplib
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from email.mime.text import MIMEText
from typing import Any, TYPE_CHECKING

# Stage 2.5b: duckdb only used for type annotations on helpers and
# execute() returns. Runtime data flow is through ctx.conn.
if TYPE_CHECKING:
    import duckdb

from fpulse.ir.schema import StepType
from fpulse.nodes.base import BaseNode, ExecutionContext
from fpulse.nodes.registry import register

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────

def _get_single_input(ctx: ExecutionContext, params: dict) -> duckdb.DuckDBPyRelation:
    """Return the first input relation or raise."""
    # 2026-06-11: route the input by its branch port so a node wired to a
    # specific output handle (e.g. the True branch of a conditional_split)
    # receives only that branch's rows. Backward-compatible — legacy
    # `from_port="output"` edges pass through unchanged.
    inputs = ctx.get_routed_inputs(
        params.get("_input_step_ids", []),
        params.get("_input_step_ports"),
    )
    if not inputs:
        raise ValueError("Node has no input data")
    return inputs[0]


def _rows_to_relation(conn: duckdb.DuckDBPyConnection, rows: list[dict],
                      table_name: str = "__action_tmp") -> duckdb.DuckDBPyRelation:
    """Convert a list of dicts into a DuckDB relation via VALUES clause."""
    if not rows:
        return conn.sql("SELECT NULL AS empty WHERE false")

    all_keys: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for k in row:
            if k not in seen:
                all_keys.append(k)
                seen.add(k)

    def fmt(v: Any) -> str:
        if v is None:
            return "NULL"
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        return "'" + str(v).replace("'", "''") + "'"

    value_rows = []
    for row in rows:
        vals = ", ".join(fmt(row.get(k)) for k in all_keys)
        value_rows.append(f"({vals})")

    values_sql = ", ".join(value_rows)
    # Name the columns explicitly in the VALUES alias rather than relying
    # on DuckDB's positional auto-naming, which shifted between versions
    # (`column0` in old releases, `col0` in newer ones). The old
    # `column{i} AS "name"` rename raised `Binder Error: Referenced column
    # "column0" not found` on every install using the `col0` convention
    # (e.g. the HTTP Request node failed to materialize any response).
    quoted_cols = ", ".join(f'"{k}"' for k in all_keys)
    conn.execute(
        f"CREATE OR REPLACE TEMP TABLE {table_name} "
        f"AS SELECT * FROM (VALUES {values_sql}) AS __vals ({quoted_cols})"
    )
    return conn.sql(f"SELECT * FROM {table_name}")


# ═════════════════════════════════════════════════════════
#  FLOW CONTROL NODES
# ═════════════════════════════════════════════════════════

@register(StepType.IF_CONDITION)
class IfConditionNode(BaseNode):
    """
    If Condition — a true two-way branch.

    2026-06-15: tags every row with the branch it belongs to via
    a ``_split_output`` column ('true' when the condition holds, else 'false');
    the executor routes each row to the matching output port (True / False)
    and strips the tag. Wire the True and/or False handles downstream.

    Back-compat: legacy ``if_condition`` edges carry the schema-default
    ``from_port='output'``; ``migrate_legacy_node_types`` remaps those to the
    'true' branch, so pipelines that used If as a keep-matching-rows filter
    behave exactly as before (downstream still sees only the true rows, with
    no ``_split_output`` column).
    """
    display_name = "If Condition"
    category = "flow_control"
    description = "Route rows to True / False outputs by a condition"
    # Brancher: the branch a legacy 'output' edge maps to (see executor /
    # migrate_legacy_node_types). True is the historical filter semantics.
    default_branch_port = "true"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        source = _get_single_input(ctx, self.params)
        # 2026-05-30 (audit): legacy frontend wrote `expression`; canonical
        # field is `condition`. Read both so saved pipelines keep working
        # while new writes land under the canonical name.
        condition = (
            self.params.get("condition")
            or self.params.get("expression")
            or "1=1"
        )

        if_input = ctx.register_scoped("__if_input", source)
        return ctx.conn.sql(
            f"SELECT *, CASE WHEN ({condition}) THEN 'true' ELSE 'false' END "
            f"AS _split_output FROM {if_input}"
        )

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"condition": "1=1"}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "condition", "type": "expression", "label": "Condition",
             "required": True,
             "placeholder": "e.g. status = 'active' AND amount > 0",
             "description": "SQL boolean expression. Rows where this is TRUE pass through."},
        ]


@register(StepType.SWITCH_CASE)
class SwitchCaseNode(BaseNode):
    """
    Switch / Case — route rows based on a column value matching predefined
    case labels.

    Evaluates cases in order; rows matching the FIRST case are returned.
    A default_case SQL expression catches everything else.
    """
    display_name = "Switch Case"
    category = "flow_control"
    description = "Send rows down different paths based on a column value (like a switch / case statement)"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        source = _get_single_input(ctx, self.params)
        # 2026-05-30 (audit): legacy frontend wrote `on`/`default_label`
        # which the executor ignored. Canonical names are
        # `column`/`default_case`; read both so saved pipelines keep
        # routing correctly.
        column = self.params.get("column") or self.params.get("on") or ""
        cases = self.params.get("cases", [])
        default_case = (
            self.params.get("default_case")
            or self.params.get("default_label")
            or ""
        )
        active_case = self.params.get("active_case", "")

        if not column:
            raise ValueError("Switch Case: 'column' is required")

        switch_input = ctx.register_scoped("__switch_input", source)

        # 2026-06-15 (security): escape the identifier + value interpolations
        # (previously a single quote in a case value broke the query / was an
        # injection vector). This node is retired from the palette ("Switch" is
        # now conditional_split) but stays for back-compat with old pipelines.
        col_q = str(column).replace('"', '""')

        # If an active_case is specified, filter to that case value
        if active_case:
            active_q = str(active_case).replace("'", "''")
            return ctx.conn.sql(
                f'SELECT * FROM {switch_input} WHERE "{col_q}" = \'{active_q}\''
            )

        # Otherwise, use the first case value (or default condition)
        if cases:
            first_value = str(cases[0].get("value", "")).replace("'", "''")
            return ctx.conn.sql(
                f'SELECT * FROM {switch_input} '
                f'WHERE "{col_q}" = \'{first_value}\''
            )

        if default_case:
            return ctx.conn.sql(
                f"SELECT * FROM {switch_input} WHERE {default_case}"
            )

        # No cases defined — pass everything through
        return source

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "column": "",
            "cases": [{"value": "A", "label": "Case A"}],
            "default_case": "",
            "active_case": "",
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "column", "type": "text", "label": "Switch Column",
             "required": True,
             "description": "The column whose value determines the case."},
            {"name": "cases", "type": "rule_list", "label": "Cases",
             "description": "List of {value, label} case definitions."},
            {"name": "active_case", "type": "text", "label": "Active Case",
             "description": "Which case value to filter on (for this execution)."},
            {"name": "default_case", "type": "expression", "label": "Default Condition",
             "description": "Fallback SQL condition if no case matches."},
        ]


@register(StepType.FOREACH_LOOP)
class ForEachLoopNode(BaseNode):
    """ForEach Loop — split input into batches and process each batch.

    Supports sequential and parallel modes, batch counts, and per-batch
    error handling.

    How it works:
      1. Splits the upstream relation into batches of `batch_size` rows.
      2. Each batch is tagged with `_batch_index` (0-based) and `_batch_total`.
      3. All batches are UNIONed back into a single output relation.

    If `batch_size` is 0, the entire input passes through as one batch.

    Modes:
      - sequential (default) — batches processed in order
      - parallel — not yet parallelised in the engine but batches are
        still tagged so the executor can fan-out in future.

    Error handling:
      - on_error = "fail" (default) — abort on first batch error
      - on_error = "continue" — skip failed batches, collect the rest
    """

    display_name = "Batch Rows"
    category = "flow_control"
    description = "Split rows into fixed-size batches (tags each row with its batch index)"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        source = _get_single_input(ctx, self.params)
        batch_size = max(int(self.params.get("batch_size", 0)), 0)
        on_error = self.params.get("on_error", "fail")

        foreach_input = ctx.register_scoped("__foreach_input", source)

        # Total row count
        total_count = ctx.conn.sql(
            f"SELECT COUNT(*) FROM {foreach_input}"
        ).fetchone()[0]

        if total_count == 0:
            return ctx.conn.sql(
                f"SELECT *, 0 AS _batch_index, 0 AS _batch_total "
                f"FROM {foreach_input} WHERE false"
            )

        # No batching — single pass with metadata columns
        if batch_size <= 0 or batch_size >= total_count:
            return ctx.conn.sql(
                f"SELECT *, 0 AS _batch_index, 1 AS _batch_total "
                f"FROM {foreach_input}"
            )

        # ── Split into batches ──────────────────────────────────────
        num_batches = (total_count + batch_size - 1) // batch_size
        batch_parts: list[str] = []
        errors: list[str] = []

        for i in range(num_batches):
            offset = i * batch_size
            batch_tbl = ctx.scoped_name(f"__foreach_b{i}")
            try:
                ctx.conn.execute(
                    f"CREATE OR REPLACE TEMP TABLE {batch_tbl} AS "
                    f"SELECT *, {i} AS _batch_index, {num_batches} AS _batch_total "
                    f"FROM {foreach_input} LIMIT {batch_size} OFFSET {offset}"
                )
                batch_parts.append(f"SELECT * FROM {batch_tbl}")
            except Exception as exc:
                if on_error == "fail":
                    raise ValueError(
                        f"ForEach batch {i}/{num_batches} failed: {exc}"
                    ) from exc
                logger.warning(
                    "ForEach batch %d/%d failed (continuing): %s",
                    i, num_batches, exc,
                )
                errors.append(f"batch_{i}: {exc}")

        if not batch_parts:
            # All batches failed
            return ctx.conn.sql(
                f"SELECT *, 0 AS _batch_index, 0 AS _batch_total "
                f"FROM {foreach_input} WHERE false"
            )

        # UNION ALL the batches back together
        union_sql = " UNION ALL ".join(batch_parts)
        result = ctx.conn.sql(union_sql)

        if errors:
            logger.warning(
                "ForEach completed with %d/%d batch errors",
                len(errors), num_batches,
            )

        return result

    @staticmethod
    def default_params() -> dict[str, Any]:
        # 2026-06-15: dropped the fake `mode` (parallel) param — Batch Rows
        # always chunks sequentially; advertising a parallel mode that the
        # engine never honors was dishonest config.
        return {
            "batch_size": 0,
            "on_error": "fail",
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "batch_size", "type": "number", "label": "Batch Size",
             "default": 0,
             "description": "Split input into batches of N rows. 0 = process all at once."},
            {"name": "on_error", "type": "select", "label": "On Error",
             "options": ["fail", "continue"], "default": "fail",
             "description": "fail = abort on first error, continue = skip failed batches."},
        ]


@register(StepType.UNTIL_LOOP)
class UntilLoopNode(BaseNode):
    """
    Until Loop — repeatedly apply a SQL condition until it is satisfied
    or max_iterations is reached.  Each iteration removes rows that
    already satisfy the stop condition.
    """
    display_name = "Until Loop"
    category = "flow_control"
    description = "Repeat the downstream steps until a condition is true (with a max-attempts safety limit)"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        source = _get_single_input(ctx, self.params)
        # 2026-05-30 (audit): legacy frontend wrote `expression` and
        # `limit`; canonical names are `condition` and `max_iterations`.
        # Read both so saved pipelines keep their iteration cap.
        condition = (
            self.params.get("condition")
            or self.params.get("expression")
            or "1=1"
        )
        raw_limit = (
            self.params.get("max_iterations")
            if self.params.get("max_iterations") is not None
            else self.params.get("limit", 10)
        )
        max_iterations = min(int(raw_limit or 10), 1000)

        until_input = ctx.register_scoped("__until_input", source)

        # Check if stop condition is already met (all rows satisfy it)
        check = ctx.conn.sql(
            f"SELECT COUNT(*) AS n FROM {until_input} WHERE NOT ({condition})"
        ).fetchone()

        if check and check[0] == 0:
            # Condition already met — all rows pass
            return source

        # Iterate: keep only rows NOT satisfying condition, up to max_iterations.
        # The per-iteration view name carries BOTH the step id (so two Until
        # nodes in one pipeline don't collide) AND the iteration index (so
        # re-registering doesn't create a view that references itself — which
        # would trip DuckDB's recursive-bind guard on the 2nd iteration).
        current = source
        for i in range(max_iterations):
            until_iter = ctx.register_scoped(f"__until_iter_{i}", current)
            remaining = ctx.conn.sql(
                f"SELECT COUNT(*) AS n FROM {until_iter} WHERE NOT ({condition})"
            ).fetchone()
            if remaining and remaining[0] == 0:
                break
            # In a real loop body you'd apply transforms here.
            # For the base node, we just pass through (the downstream
            # pipeline handles the actual iteration logic).
            current = ctx.conn.sql(f"SELECT * FROM {until_iter}")

        # Return all rows (both satisfied and remaining)
        return current

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"condition": "1=1", "max_iterations": 10}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "condition", "type": "expression", "label": "Stop Condition",
             "required": True,
             "placeholder": "e.g. retry_count >= 3",
             "description": "Loop stops when ALL rows satisfy this condition."},
            {"name": "max_iterations", "type": "number", "label": "Max Iterations",
             "default": 10,
             "description": "Safety limit to prevent infinite loops (max 1000)."},
        ]


@register(StepType.WAIT_DELAY)
class WaitDelayNode(BaseNode):
    """
    Wait / Delay — pause execution for a specified duration, then
    pass all data through unchanged.
    """
    display_name = "Wait"
    category = "flow_control"
    description = "Pause for a set time, then continue to the next step"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        source = _get_single_input(ctx, self.params)
        # 2026-05-30 (audit): legacy frontend wrote duration+unit instead
        # of seconds. Convert on read so old pipelines still pause for
        # the intended duration. Canonical write is `seconds`.
        if "seconds" in self.params and self.params["seconds"] is not None:
            seconds = int(self.params["seconds"] or 0)
        else:
            duration = int(self.params.get("duration", 0) or 0)
            unit = (self.params.get("unit") or "seconds").lower()
            multiplier = {"seconds": 1, "minutes": 60, "hours": 3600}.get(unit, 1)
            seconds = duration * multiplier

        # Cap at 300 seconds (5 min) to prevent abuse
        seconds = max(0, min(int(seconds), 300))

        if seconds > 0:
            logger.info("WaitDelay: sleeping %d seconds", seconds)
            time.sleep(seconds)

        return source

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"seconds": 1}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "seconds", "type": "number", "label": "Delay (seconds)",
             "default": 1,
             "description": "Pause execution for this many seconds (max 300)."},
        ]


@register(StepType.SET_VARIABLE)
class SetVariableNode(BaseNode):
    """
    Set Variable — capture runtime variables on the execution context.

    2026-06-15 (node-audit): repurposed. This node previously appended
    *columns* to the data (``SELECT *, expr AS name``) which made it a
    duplicate of Derived Column AND made its name a lie — it never wrote
    the ``{{ $vars }}`` it advertised. It now does what its name says:
    each entry evaluates a SQL/constant expression ONCE and stores the
    scalar result on ``ctx.vars[name]``, which downstream steps read
    through the expression engine as ``{{ $vars.<name> }}`` (resolved
    per-step in topological order, so any later step sees the value).

    Input rows pass through UNCHANGED — this is a control-flow node, not a
    transform. (To ADD a column to every row, use Derived Column.)
    """
    display_name = "Set Variable"
    category = "flow_control"
    description = (
        "Set runtime variables ({{ $vars.NAME }}) from a constant or SQL "
        "expression — read by any downstream step. Passes input through unchanged."
    )

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        inputs = ctx.get_routed_inputs(
            self.params.get("_input_step_ids", []),
            self.params.get("_input_step_ports"),
        )
        source = inputs[0] if inputs else None
        variables = self.params.get("variables", [])

        if source is not None:
            ctx.conn.register("__setvar_input", source)

        for v in variables:
            name = (v.get("name") or "").strip()
            expr = (v.get("expression") or "").strip()
            if not name or not expr:
                continue
            # Evaluate the expression once. With an input wired, the
            # expression may reference its columns (use an aggregate like
            # MAX(col) for a deterministic single value); the first result
            # row is captured. With no input, it's a standalone constant.
            sql = (
                f"SELECT ({expr}) AS __v FROM __setvar_input LIMIT 1"
                if source is not None
                else f"SELECT ({expr}) AS __v"
            )
            try:
                row = ctx.conn.sql(sql).fetchone()
            except Exception as e:  # noqa: BLE001 — surface a clear message
                raise ValueError(
                    f"Set Variable '{name}': could not evaluate expression "
                    f"{expr!r}: {e}"
                ) from e
            ctx.vars[name] = row[0] if row else None
            logger.info("SetVariable: $vars.%s = %r", name, ctx.vars[name])

        # Control-flow semantics: input passes straight through.
        if source is not None:
            return source
        return ctx.conn.sql("SELECT NULL AS empty WHERE false")

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"variables": [{"name": "my_var", "expression": "'default'"}]}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "variables", "type": "derived_list", "label": "Variables",
             "required": True,
             "description": "Each entry sets {{ $vars.NAME }} from a SQL/constant "
                            "expression (e.g. 'prod', 42, MAX(updated_at)). Evaluated "
                            "once; input rows pass through unchanged. To add a column "
                            "to the data instead, use Derived Column."},
        ]


@register(StepType.EXECUTE_PIPELINE)
class ExecutePipelineNode(BaseNode):
    """
    Execute Pipeline — reference a sub-workflow to run.

    In the current version this is a pass-through placeholder that
    records which pipeline_id should be invoked.  The orchestrator
    is responsible for actually running the sub-pipeline.
    """
    display_name = "Execute Pipeline"
    category = "flow_control"
    description = "Run another saved pipeline from inside this one. Pass values to it as parameters."

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        source = _get_single_input(ctx, self.params)
        pipeline_id = self.params.get("pipeline_id", "")
        wait = self.params.get("wait_for_completion", True)

        if not pipeline_id:
            logger.warning("ExecutePipeline: no pipeline_id — passing data through")
            return source

        # Access the workflow store and executor from app_state
        store = ctx.app_state.get("store")
        if not store:
            logger.warning("ExecutePipeline: no workflow store in context — passing data through")
            return source

        # Load the sub-pipeline definition
        sub_workflow = store.get(pipeline_id)
        if not sub_workflow:
            raise ValueError(f"ExecutePipeline: pipeline '{pipeline_id}' not found")

        logger.info("ExecutePipeline: running sub-pipeline '%s' (%s)", pipeline_id, sub_workflow.get("name", ""))

        # Build Workflow IR from stored definition
        from fpulse.ir.schema import Workflow
        try:
            wf = Workflow.from_dict(sub_workflow)
        except Exception as exc:
            raise ValueError(f"ExecutePipeline: failed to parse pipeline '{pipeline_id}': {exc}") from exc

        # ── Param injection (PR13 follow-up) ──
        # The child workflow's variable resolver (set_variable / expression
        # editor / templated SQL) reads from `wf.metadata['parameters']`.
        # Caller passes a dict via params.parameters; we merge over any
        # existing metadata.parameters so downstream nodes see them as
        # workflow variables.
        injected = self.params.get("parameters") or {}
        if injected:
            if not isinstance(injected, dict):
                logger.warning(
                    "ExecutePipeline: 'parameters' param must be a dict; got %s — ignoring",
                    type(injected).__name__,
                )
            else:
                merged = dict(wf.metadata.get("parameters") or {})
                merged.update(injected)
                wf.metadata["parameters"] = merged
                logger.info(
                    "ExecutePipeline: injected %d parameter(s) into sub-pipeline '%s': %s",
                    len(injected), pipeline_id, sorted(injected.keys()),
                )

        # Execute via a new WorkflowExecutor (isolated DuckDB connection)
        from fpulse.engine.executor import WorkflowExecutor
        sub_executor = WorkflowExecutor(data_dir=ctx.data_dir, app_state=ctx.app_state)
        from fpulse.security.execution_codes import mint_for_run
        result = sub_executor.execute_workflow(
            wf, preview_limit=0, full_run=ctx.full_run,
            execution_code=mint_for_run(wf),
        )

        if result.status == "error":
            on_failure = self.params.get("on_failure", "fail")
            if on_failure == "fail":
                raise ValueError(f"ExecutePipeline: sub-pipeline '{pipeline_id}' failed: {result.error}")
            logger.warning("ExecutePipeline: sub-pipeline '%s' failed (on_failure=%s), continuing", pipeline_id, on_failure)
            return source

        # Return last step's result if available, otherwise pass through
        if result.step_results:
            last_step = list(result.step_results.values())[-1]
            if last_step.preview_data:
                # Reconstruct from preview data
                cols = last_step.preview_columns or [f"col_{i}" for i in range(len(last_step.preview_data[0]))]
                rows = last_step.preview_data
                if rows:
                    val_strs = ", ".join(
                        "(" + ", ".join(
                            "NULL" if v is None else f"'{str(v).replace(chr(39), chr(39)+chr(39))}'"
                            for v in row
                        ) + ")"
                        for row in rows
                    )
                    col_defs = ", ".join(f'"{c}"' for c in cols)
                    return ctx.conn.sql(f"SELECT * FROM (VALUES {val_strs}) AS t({col_defs})")

        logger.info("ExecutePipeline: sub-pipeline '%s' completed successfully", pipeline_id)
        return source

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "pipeline_id": "",
            "on_failure": "fail",
            "parameters": {},
        }

    @staticmethod
    def param_schema() -> list[dict]:
        # 2026-06-15 (honest config): dropped the `wait_for_completion` toggle —
        # Execute Pipeline always runs the child synchronously, so the switch
        # was a no-op. (Re-add when true fire-and-forget async is supported.)
        return [
            {"name": "pipeline_id", "type": "workflow_picker", "label": "Sub-Pipeline",
             "required": True,
             "description": "Pipeline to execute as a sub-workflow. Pick from the list of saved workflows."},
            {"name": "on_failure", "type": "select", "label": "On Failure",
             "options": ["fail", "skip", "continue"],
             "default": "fail",
             "description": "fail = stop parent, skip = continue with parent input, continue = same as skip"},
            {"name": "parameters", "type": "key_value_map", "label": "Parameters",
             "default": {},
             "description": "Key/value pairs passed to the sub-pipeline as workflow variables."},
        ]


def _run_subpipeline(ctx: ExecutionContext, pipeline_id: str, overrides: dict):
    """Run a saved sub-pipeline once with `overrides` as parameter values.

    Mirrors ExecutePipelineNode's invocation but uses the VALIDATED
    parameter path (`execute_workflow(parameter_values=...)`) and filters
    overrides down to the child's DECLARED parameters — so passing a whole
    input row never trips "unknown parameter" on columns the child doesn't
    declare. Returns the WorkflowRunResult. Raises if the store/pipeline is
    unavailable. Factored out so ForEachPipelineNode is unit-testable via
    monkeypatch.
    """
    store = ctx.app_state.get("store") if ctx.app_state else None
    if not store:
        raise ValueError(
            "For Each (Run Pipeline): no workflow store available to run the sub-pipeline."
        )
    sub = store.get(pipeline_id)
    if not sub:
        raise ValueError(f"For Each (Run Pipeline): sub-pipeline '{pipeline_id}' not found.")

    from fpulse.ir.schema import Workflow
    wf = Workflow.from_dict(sub) if isinstance(sub, dict) else sub
    declared = {p.name for p in (getattr(wf, "parameters", None) or [])}
    filtered = {k: v for k, v in (overrides or {}).items() if k in declared}

    from fpulse.engine.executor import WorkflowExecutor
    sub_exec = WorkflowExecutor(data_dir=ctx.data_dir, app_state=ctx.app_state)
    from fpulse.security.execution_codes import mint_for_run
    return sub_exec.execute_workflow(
        wf, preview_limit=0, full_run=ctx.full_run, parameter_values=filtered,
        execution_code=mint_for_run(wf),
    )


@register(StepType.FOREACH_PIPELINE)
class ForEachPipelineNode(BaseNode):
    """For Each (Run Pipeline) — a true per-item loop.

    Runs a saved sub-pipeline ONCE PER input row, injecting that row's
    columns as the sub-pipeline's parameters (the child references them as
    ``${param.<column>}``). The whole row is also passed as a single JSON
    parameter (default name ``item``) for children that prefer ``@item``.

    This is the per-row loop pattern — the
    sub-pipeline IS the loop body / child scope. Distinct from the
    foreach_loop node, which only chunks rows into batches.

    Iteration is sequential and capped (``max_iterations``) — running a
    full sub-pipeline per row is expensive, so the node refuses oversized
    inputs rather than silently spawning thousands of child runs. The input
    relation passes through unchanged (ForEach is control flow / side
    effects); collecting child outputs is a future option.
    """
    display_name = "ForEach"
    category = "flow_control"
    description = (
        "Run a sub-pipeline once per input row — the row is passed as the "
        "sub-pipeline's parameters"
    )

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        source = _get_single_input(ctx, self.params)
        if source is None:
            raise ValueError(
                "For Each (Run Pipeline): needs one input — the rows to iterate over."
            )
        pipeline_id = (self.params.get("pipeline_id") or "").strip()
        if not pipeline_id:
            raise ValueError(
                "For Each (Run Pipeline): 'pipeline_id' is required — pick the sub-pipeline to run per item."
            )
        on_item_error = (self.params.get("on_item_error") or "fail").lower()
        if on_item_error not in ("fail", "continue"):
            raise ValueError(
                f"For Each (Run Pipeline): invalid on_item_error '{on_item_error}'. Allowed: fail, continue."
            )
        try:
            max_iter = int(self.params.get("max_iterations", 100) or 100)
        except (TypeError, ValueError):
            raise ValueError("For Each (Run Pipeline): 'max_iterations' must be a number.")
        if max_iter <= 0:
            raise ValueError("For Each (Run Pipeline): 'max_iterations' must be greater than 0.")
        item_param = (self.params.get("item_param") or "item").strip() or "item"
        static = self.params.get("parameters") or {}
        if not isinstance(static, dict):
            static = {}

        cols = source.columns
        rows = [dict(zip(cols, r)) for r in source.fetchall()]
        if len(rows) > max_iter:
            raise ValueError(
                f"For Each (Run Pipeline): input has {len(rows)} rows but max_iterations is "
                f"{max_iter}. Running a sub-pipeline per row is expensive — raise the cap "
                f"deliberately or reduce the input first."
            )

        failures = 0
        for i, row in enumerate(rows):
            overrides = {**static, **row, item_param: row}
            try:
                res = _run_subpipeline(ctx, pipeline_id, overrides)
            except Exception as exc:
                if on_item_error == "fail":
                    raise ValueError(
                        f"For Each (Run Pipeline): item {i + 1}/{len(rows)} failed: {exc}"
                    ) from exc
                failures += 1
                logger.warning("ForEachPipeline: item %d failed (continuing): %s", i + 1, exc)
                continue
            if res is not None and getattr(res, "status", None) == "error":
                if on_item_error == "fail":
                    raise ValueError(
                        f"For Each (Run Pipeline): item {i + 1}/{len(rows)} sub-pipeline failed: "
                        f"{getattr(res, 'error', '') or 'unknown error'}"
                    )
                failures += 1

        if failures:
            logger.info(
                "ForEachPipeline: %d of %d items failed (on_item_error=continue)",
                failures, len(rows),
            )
        return source

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "pipeline_id": "",
            "item_param": "item",
            "max_iterations": 100,
            "on_item_error": "fail",
            "parameters": {},
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "pipeline_id", "type": "workflow_picker", "label": "Sub-Pipeline",
             "required": True,
             "description": "The pipeline to run once per input row. Its declared parameters receive the row's matching columns."},
            {"name": "item_param", "type": "text", "label": "Item Parameter Name", "default": "item",
             "description": "The whole row is also passed as one JSON parameter under this name (for children that take a single item param)."},
            {"name": "max_iterations", "type": "number", "label": "Max Iterations", "default": 100,
             "description": "Safety cap — the node refuses inputs larger than this (a sub-pipeline per row is expensive)."},
            {"name": "on_item_error", "type": "select", "label": "On Item Error",
             "options": ["fail", "continue"], "default": "fail",
             "description": "fail = stop on the first failed item; continue = skip failures and finish the rest."},
            {"name": "parameters", "type": "key_value_map", "label": "Static Parameters", "default": {},
             "description": "Fixed key/value parameters passed to every iteration (the row's columns override these per item)."},
        ]


# ═════════════════════════════════════════════════════════
#  ACTION NODES
# ═════════════════════════════════════════════════════════

@register(StepType.HTTP_REQUEST)
class HttpRequestNode(BaseNode):
    """
    HTTP Request — make an HTTP call per row or as a single batch request.

    In batch mode (default), sends one request and returns the parsed JSON.
    In per-row mode, sends one request per row, embedding column values
    into the URL/body via {column_name} placeholders.
    """

    @staticmethod
    def preview_message(params, row_count):
        # X4 — per-row mode would fire N requests; batch mode would
        # fire 1. Tell the operator which + the URL.
        method = params.get("method", "GET").upper()
        url = params.get("url") or "(no URL set)"
        per_row = bool(params.get("per_row", False))
        if per_row:
            return f"would send {row_count} {method} request{'s' if row_count != 1 else ''} to {url}"
        return f"would send 1 {method} request to {url}"
    display_name = "HTTP Request"
    category = "action"
    description = "Call a web API and capture the response (one call total or one per row)"

    # Default timeout when the user hasn't set one. Overridable by env
    # var (FPULSE_HTTP_DEFAULT_TIMEOUT) so a deployer can tune for their
    # network conditions; per-step value still wins when set.
    _DEFAULT_TIMEOUT_S = 30

    @classmethod
    def _resolve_timeout(cls, requested: Any) -> int:
        try:
            n = int(requested or 0)
        except (TypeError, ValueError):
            n = 0
        if n > 0:
            return n
        import os as _os
        try:
            env = int(_os.environ.get("FPULSE_HTTP_DEFAULT_TIMEOUT", "") or 0)
            if env > 0:
                return env
        except (TypeError, ValueError):
            pass
        return cls._DEFAULT_TIMEOUT_S

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        url = self.params.get("url", "")
        method = self.params.get("method", "GET").upper()
        headers = self.params.get("headers", {})
        # The editor's HTTP Request form stores headers as a [{key,value}] list
        # (a repeater); coerce to the dict the request layer expects so headers
        # actually apply instead of raising. (2026-06-15 shape fix.)
        if isinstance(headers, list):
            headers = {
                h.get("key"): h.get("value")
                for h in headers
                if isinstance(h, dict) and h.get("key")
            }
        body = self.params.get("body", "")
        per_row = self.params.get("per_row", False)
        timeout_s = self._resolve_timeout(self.params.get("timeout"))

        if not url:
            raise ValueError("HTTP Request: 'url' is required")

        # 2026-05-22: only per-row mode actually needs an upstream
        # relation (one request per row, with {col} placeholders).
        # Batch mode makes a single request regardless of input, so
        # demanding an input made HTTP Request unusable as a pipeline
        # entry point — e.g. "kick off a daily run by hitting an API"
        # was forced to put a dummy source upstream. The nodeArity
        # contract for http_request already has required=0 (it sits
        # in NO_INPUT_NODES); this brings the backend in line.
        if per_row:
            source = _get_single_input(ctx, self.params)
            return self._per_row_request(ctx, source, url, method, headers, body, timeout_s)
        else:
            return self._batch_request(ctx, url, method, headers, body, timeout_s)

    def _batch_request(self, ctx: ExecutionContext, url: str, method: str,
                       headers: dict, body: str, timeout_s: int) -> duckdb.DuckDBPyRelation:
        """Single request, return response as relation."""
        rows = self._do_request(url, method, headers, body, timeout_s)
        return _rows_to_relation(ctx.conn, rows, "__http_batch")

    def _per_row_request(self, ctx: ExecutionContext,
                         source: duckdb.DuckDBPyRelation,
                         url_template: str, method: str,
                         headers: dict, body_template: str,
                         timeout_s: int,
                         ) -> duckdb.DuckDBPyRelation:
        """One request per input row, merge responses."""
        ctx.conn.register("__http_per_row_src", source)
        input_rows = ctx.conn.sql("SELECT * FROM __http_per_row_src").fetchdf()

        # Guard against a self-inflicted outage: per-row mode fires one
        # synchronous request per input row. A large relation would block
        # the worker for a long time and hammer the target API. Cap it
        # (override with FPULSE_HTTP_PER_ROW_MAX) and fail fast with a
        # clear, actionable message rather than melting down silently.
        import os as _os
        try:
            _max_rows = int(_os.environ.get("FPULSE_HTTP_PER_ROW_MAX", "1000") or "1000")
        except ValueError:
            _max_rows = 1000
        if len(input_rows) > _max_rows:
            raise ValueError(
                f"HTTP Request (per-row) refused: {len(input_rows)} input rows "
                f"exceeds the safety cap of {_max_rows}. That would issue "
                f"{len(input_rows)} serial HTTP calls. Filter/sample the input, "
                f"or raise FPULSE_HTTP_PER_ROW_MAX if this is intentional."
            )

        result_rows: list[dict] = []

        for _, row in input_rows.iterrows():
            row_dict = row.to_dict()
            rendered_url = url_template
            rendered_body = body_template
            for col, val in row_dict.items():
                rendered_url = rendered_url.replace(f"{{{col}}}", str(val))
                rendered_body = rendered_body.replace(f"{{{col}}}", str(val))

            try:
                resp = self._do_request(rendered_url, method, headers, rendered_body, timeout_s)
                if resp:
                    # Merge input row with first response row
                    merged = {**row_dict, **resp[0]}
                    result_rows.append(merged)
                else:
                    result_rows.append({**row_dict, "_http_status": "empty"})
            except Exception as exc:
                result_rows.append({**row_dict, "_http_error": str(exc)})

        if not result_rows:
            return source

        return _rows_to_relation(ctx.conn, result_rows, "__http_per_row")

    @staticmethod
    def _do_request(url: str, method: str, headers: dict,
                    body: str, timeout_s: int) -> list[dict]:
        """Execute a single HTTP request, return parsed JSON rows."""
        req_headers = {"Accept": "application/json", "User-Agent": "F-Pulse/0.6.0"}
        req_headers.update(headers)

        data = body.encode("utf-8") if body else None
        if data:
            req_headers.setdefault("Content-Type", "application/json")

        # SSRF guard — validate the user-supplied URL against the shared
        # policy (blocks loopback / private / link-local / multicast /
        # cloud-metadata hosts) BEFORE opening any socket. Operators can
        # allow internal targets in trusted deployments via
        # FPULSE_HTTP_ALLOW_PRIVATE=1.
        from fpulse.security.ssrf import check_url, SsrfBlockedError
        try:
            check_url(url, allow_private_env="FPULSE_HTTP_ALLOW_PRIVATE")
        except SsrfBlockedError as exc:
            raise ValueError(f"HTTP Request blocked: {exc}") from exc

        req = urllib.request.Request(url, method=method, headers=req_headers, data=data)
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise ValueError(
                f"HTTP Request: {exc.code} from {url}: {exc.reason}"
            ) from exc
        except urllib.error.URLError as exc:
            raise ValueError(
                f"HTTP Request: cannot reach {url}: {exc.reason}"
            ) from exc

        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            for key in ("data", "results", "items", "records", "rows", "values"):
                if key in raw and isinstance(raw[key], list):
                    return raw[key]
            return [raw]
        return [{"response": str(raw)}]

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "url": "", "method": "GET", "headers": {}, "body": "",
            "per_row": False, "timeout": 0,  # 0 = use _DEFAULT_TIMEOUT_S / env override
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "url", "type": "text", "label": "URL", "required": True,
             "placeholder": "https://api.example.com/data",
             "description": "Use {column} placeholders for per-row mode."},
            {"name": "method", "type": "select", "label": "Method",
             "options": ["GET", "POST", "PUT", "PATCH", "DELETE"], "default": "GET"},
            {"name": "headers", "type": "key_value", "label": "Headers"},
            {"name": "body", "type": "code", "label": "Request Body (JSON)",
             "description": "Supports {column} placeholders in per-row mode."},
            {"name": "per_row", "type": "boolean", "label": "Send Per Row",
             "default": False,
             "description": "Send a separate request for each input row."},
            {"name": "timeout", "type": "number", "label": "Timeout (seconds)",
             "default": 0,
             "description": "Per-request timeout. 0 = use the default (30s, or FPULSE_HTTP_DEFAULT_TIMEOUT env)."},
        ]


# WebhookTriggerNode dropped from palette — webhook ingestion belongs in
# the Source category and is covered by api_source / openapi_source. Class
# kept for legacy pipelines.
class WebhookTriggerNode(BaseNode):
    """
    Webhook Trigger — acts as a source node that represents incoming
    webhook data.  Generates sample data for testing; in production the
    orchestrator injects the actual webhook payload.
    """
    display_name = "Webhook Trigger"
    category = "action"
    description = "Receive data sent to a URL by another system (sample data is shown while you build)"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        sample_rows = self.params.get("sample_data", None)

        if sample_rows and isinstance(sample_rows, list):
            return _rows_to_relation(ctx.conn, sample_rows, "__webhook_data")

        # Generate default sample webhook payload
        now = datetime.now(timezone.utc).isoformat()
        default_rows = [
            {
                "webhook_id": uuid.uuid4().hex[:8],
                "event": "trigger",
                "timestamp": now,
                "payload": "{}",
            },
        ]
        return _rows_to_relation(ctx.conn, default_rows, "__webhook_data")

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "path": "/webhook",
            "method": "POST",
            "sample_data": None,
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "path", "type": "text", "label": "Webhook Path",
             "default": "/webhook",
             "description": "URL path this webhook listens on."},
            {"name": "method", "type": "select", "label": "HTTP Method",
             "options": ["POST", "GET", "PUT"], "default": "POST"},
            {"name": "sample_data", "type": "code", "label": "Sample Data (JSON)",
             "description": "JSON array of objects for test/preview runs."},
        ]


@register(StepType.CODE_SCRIPT)
class CodeScriptNode(BaseNode):
    """
    Code / Script — execute user-provided Python code on the data.

    The code receives a pandas DataFrame as `df` and must assign the
    result back to `df`. A restricted builtin set is provided and a
    wall-clock timeout applies.

    SECURITY: this runs IN-PROCESS via ``exec`` — it is NOT a sandbox.
    The restricted builtins (and the removed ``open``) raise the bar but
    do not contain a determined attacker: ``pd`` / ``np`` still expose
    filesystem and other host I/O, and a timed-out script runs on a daemon
    thread that is not force-killed. Treat Code Script as *trusted* code.

    DISABLED BY DEFAULT and not shown in the node palette. It runs only when an
    operator opts in on a trusted, single-tenant install with
    ``FPULSE_ENABLE_CODE_SCRIPT=1``; ``FPULSE_DISABLE_CODE_SCRIPT=1`` force-disables
    even if the enable flag is set.
    """
    display_name = "Code Script"
    category = "action"
    description = "Run your own Python code on the data. Executes in-process with restricted builtins (trusted code — not a security sandbox)."

    # Allowed builtins — deliberately restrictive
    _SAFE_BUILTINS = {
        "abs", "all", "any", "bool", "dict", "enumerate", "filter",
        "float", "format", "frozenset", "getattr", "hasattr", "hash",
        "int", "isinstance", "issubclass", "iter", "len", "list",
        "map", "max", "min", "next", "print", "range", "repr",
        "reversed", "round", "set", "slice", "sorted", "str", "sum",
        "tuple", "type", "zip",
    }

    # R9 (2026-05-30) — import allowlist. Anything outside this set
    # raises at parse time, BEFORE the user code executes. This catches
    # creative obfuscation that the string-based _BLOCKED list misses
    # (e.g. `__import__('o' 's')`, `from socket import *`, etc.). The
    # set is intentionally tight: stdlib data utilities + pandas/numpy
    # + the duckdb relation. No network, no filesystem, no subprocess.
    _ALLOWED_IMPORTS = {
        "re", "json", "math", "statistics", "datetime", "decimal",
        "itertools", "functools", "collections", "csv", "io",
        "pandas", "numpy", "duckdb",
    }

    @staticmethod
    def _code_script_enabled() -> bool:
        """DISABLED BY DEFAULT (fail-closed). Code Script runs user-provided
        Python in-process (restricted builtins + an import allowlist, but NOT a
        sandbox), and it is not offered in the node palette, so a workflow can
        only reach it if hand-crafted. It runs only when an operator explicitly
        opts in on a trusted, single-tenant install with
        FPULSE_ENABLE_CODE_SCRIPT=1. FPULSE_DISABLE_CODE_SCRIPT=1 is an explicit
        force-off that wins even if the enable flag is set."""
        import os as _os

        def _flag(name: str) -> bool:
            return _os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}

        return _flag("FPULSE_ENABLE_CODE_SCRIPT") and not _flag("FPULSE_DISABLE_CODE_SCRIPT")

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        code = self.params.get("code", "")
        timeout = min(int(self.params.get("timeout", 30)), 300)  # max 5 min

        # Gate BEFORE any work: a disabled node never reads input or runs code.
        # An empty-code node is a harmless passthrough, so it isn't gated.
        if code.strip() and not self._code_script_enabled():
            raise ValueError(
                "Code Script is disabled on this instance. It executes "
                "user-provided Python in-process and is NOT sandboxed, so it is "
                "off by default and not offered in the node palette. To run it on "
                "a trusted, single-tenant install, set FPULSE_ENABLE_CODE_SCRIPT=1. "
                "For custom logic, use the SQL Transform (DuckDB) node instead."
            )

        source = _get_single_input(ctx, self.params)
        if not code.strip():
            return source

        # ── Static analysis: block dangerous patterns ──
        _BLOCKED = [
            "import os", "import sys", "import subprocess", "import socket",
            "import shutil", "import signal", "import ctypes", "import pickle",
            "__import__", "eval(", "exec(", "compile(", "globals(", "locals(",
            "getattr(", "setattr(", "delattr(", "open(", "file(",
            "breakpoint(", "__class__", "__subclasses__", "__bases__",
            "__mro__", "__code__", "__globals__", "os.system", "os.popen",
            "os.exec", "os.spawn", "os.remove", "os.unlink", "os.rmdir",
            "shutil.rmtree",
        ]
        code_lower = code.lower().replace(" ", "")
        for pattern in _BLOCKED:
            if pattern.lower().replace(" ", "") in code_lower:
                raise ValueError(
                    f"Code Script: blocked dangerous pattern: '{pattern}'. "
                    "For security, file I/O, system calls, and dynamic code execution are not allowed."
                )

        # R9 (2026-05-30) — AST-based import allowlist. Parses the user
        # code and rejects ANY import statement whose root module isn't
        # in _ALLOWED_IMPORTS. More robust than the string-match _BLOCKED
        # list because it catches creative obfuscation ("import socket  "
        # with trailing spaces, `from socket import *`, conditional
        # imports, etc.). Also surfaces SyntaxError up front rather than
        # at exec time inside the worker thread.
        try:
            import ast as _ast
            tree = _ast.parse(code)
        except SyntaxError as exc:
            raise ValueError(
                f"Code Script: syntax error at line {exc.lineno}: {exc.msg}"
            ) from exc
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                for alias in node.names:
                    root = alias.name.split(".", 1)[0]
                    if root not in self._ALLOWED_IMPORTS:
                        raise ValueError(
                            f"Code Script: import '{alias.name}' is not in the "
                            "allowlist. Allowed: "
                            + ", ".join(sorted(self._ALLOWED_IMPORTS))
                        )
            elif isinstance(node, _ast.ImportFrom):
                root = (node.module or "").split(".", 1)[0]
                if root and root not in self._ALLOWED_IMPORTS:
                    raise ValueError(
                        f"Code Script: import 'from {node.module} import …' is "
                        "not in the allowlist. Allowed: "
                        + ", ".join(sorted(self._ALLOWED_IMPORTS))
                    )

        # Convert to pandas for user code
        code_input = ctx.register_scoped("__code_input", source)
        df = ctx.conn.sql(f"SELECT * FROM {code_input}").fetchdf()

        # Build restricted globals — NO access to __import__, open, etc.
        import builtins as _builtins
        safe = {k: getattr(_builtins, k) for k in self._SAFE_BUILTINS
                if hasattr(_builtins, k)}
        safe["__builtins__"] = safe  # block __import__, open, etc.

        namespace: dict[str, Any] = {"df": df}

        # Safe imports only
        try:
            import pandas as pd
            namespace["pd"] = pd
        except ImportError:
            pass
        try:
            import numpy as np
            namespace["np"] = np
        except ImportError:
            pass

        import math as _math
        namespace["json"] = json
        namespace["math"] = _math

        # ── Execute with timeout via threading ──
        import threading
        exec_error: list[Exception] = []
        exec_done = threading.Event()

        def _run():
            try:
                exec(code, safe, namespace)  # noqa: S102
            except Exception as exc:
                exec_error.append(exc)
            finally:
                exec_done.set()

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        if not exec_done.wait(timeout=timeout):
            raise ValueError(
                f"Code Script: execution timed out after {timeout}s. "
                "Reduce data size or simplify your code."
            )
        if exec_error:
            raise ValueError(f"Code Script error: {exec_error[0]}") from exec_error[0]

        result_df = namespace.get("df", df)

        # Convert back to DuckDB relation
        code_output = ctx.register_scoped("__code_output", result_df)
        return ctx.conn.sql(f"SELECT * FROM {code_output}")

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "code": (
                "# 'df' is a pandas DataFrame with your input data.\n"
                "# Modify df in place or reassign it.\n"
                "# Example:\n"
                "# df['new_col'] = df['amount'] * 2\n"
            ),
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "code", "type": "code", "label": "Python Code",
             "required": True, "language": "python",
             "description": (
                 "Receives 'df' (pandas DataFrame). "
                 "Assign result back to 'df'. "
                 "Available: pd, np, json, math."
             )},
        ]


@register(StepType.SEND_EMAIL)
class SendEmailNode(BaseNode):
    """Send Email — enterprise email with HTML, CC/BCC, per-row, connections.

    Features:
      - HTML or plain text body with {column} placeholders
      - CC / BCC recipients
      - Connection-based SMTP (pull credentials from connection store)
      - Per-row mode: send one email per input row
      - TLS / SSL selection
      - On failure: fail or continue (log and pass data through)

    If SMTP is not configured (no host, no connection), the email is logged.
    """

    display_name = "Send Email"
    category = "action"
    description = "Send an email — supports HTML, CC/BCC, and one email per row"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        source = _get_single_input(ctx, self.params)

        # R8 (2026-05-30) — preview-mode short-circuit. Don't actually
        # send the email; instead surface a "would send N emails"
        # summary so the dry-run path is observable. Returns the input
        # relation unchanged so downstream nodes still see the data.
        if self.is_preview(ctx):
            try:
                ctx.conn.register("__email_preview", source)
                n = ctx.conn.sql("SELECT COUNT(*) FROM __email_preview").fetchone()[0]
            except Exception:
                n = 0
            logger.info(
                "send_email preview_mode: would send %d email(s) to %r",
                n, self.params.get("to", ""),
            )
            return source

        # ── Resolve SMTP config (connection or inline) ──
        smtp_cfg = self._resolve_smtp(ctx)
        to_addr = self.params.get("to", "")
        cc_addr = self.params.get("cc", "")
        bcc_addr = self.params.get("bcc", "")
        subject_tpl = self.params.get("subject", "F-Pulse Notification")
        body_tpl = self.params.get("body", "")
        body_type = self.params.get("body_type", "plain")  # plain | html
        per_row = self.params.get("per_row", False)
        on_error = self.params.get("on_error", "fail")

        ctx.conn.register("__email_input", source)

        if per_row:
            rows_df = ctx.conn.sql("SELECT * FROM __email_input").fetchdf()
            sent = 0
            errors = []
            for _, row in rows_df.iterrows():
                row_dict = row.to_dict()
                subj = self._render(subject_tpl, row_dict)
                body = self._render(body_tpl, row_dict)
                to_rendered = self._render(to_addr, row_dict)
                try:
                    self._send_one(smtp_cfg, to_rendered, cc_addr, bcc_addr,
                                   subj, body, body_type)
                    sent += 1
                except Exception as exc:
                    if on_error == "fail":
                        raise ValueError(f"SendEmail per-row failed: {exc}") from exc
                    errors.append(str(exc))
                    logger.warning("SendEmail per-row error (continuing): %s", exc)
            logger.info("SendEmail: sent %d/%d emails", sent, len(rows_df))
        else:
            # Single email — render from first row
            first = ctx.conn.sql("SELECT * FROM __email_input LIMIT 1").fetchdf()
            row_dict = first.iloc[0].to_dict() if not first.empty else {}
            subj = self._render(subject_tpl, row_dict)
            body = self._render(body_tpl, row_dict)
            try:
                self._send_one(smtp_cfg, to_addr, cc_addr, bcc_addr,
                               subj, body, body_type)
            except Exception as exc:
                if on_error == "fail":
                    raise ValueError(f"SendEmail failed: {exc}") from exc
                logger.warning("SendEmail error (continuing): %s", exc)

        return source

    # ── SMTP resolution ─────────────────────────────────────────────
    def _resolve_smtp(self, ctx: ExecutionContext) -> dict:
        """Return {host, port, user, password, from, security} from connection or inline."""
        conn_id = self.params.get("connection_id", "")
        if conn_id:
            store = ctx.app_state.get("connection_store")
            if store:
                conn_cfg = store.get(conn_id)
                if conn_cfg:
                    config = conn_cfg.get("config", conn_cfg)
                    return {
                        "host": config.get("host", ""),
                        "port": int(config.get("port", 587)),
                        "user": config.get("user") or config.get("username", ""),
                        "password": config.get("password", ""),
                        "from": config.get("from") or config.get("sender", ""),
                        "security": config.get("security", "tls"),
                    }
        return {
            "host": self.params.get("smtp_host", ""),
            "port": int(self.params.get("smtp_port", 587)),
            "user": self.params.get("smtp_user", ""),
            "password": self.params.get("smtp_pass", ""),
            "from": self.params.get("from", ""),
            "security": self.params.get("security", "tls"),
        }

    # ── Send a single email ─────────────────────────────────────────
    def _send_one(self, smtp_cfg: dict, to: str, cc: str, bcc: str,
                  subject: str, body: str, body_type: str) -> None:
        host = smtp_cfg.get("host", "")
        if not host:
            self._log_email(to, subject, body)
            return

        from email.mime.multipart import MIMEMultipart

        port = smtp_cfg.get("port", 587)
        user = smtp_cfg.get("user", "")
        password = smtp_cfg.get("password", "")
        from_addr = smtp_cfg.get("from") or user or "fpulse@localhost"
        security = smtp_cfg.get("security", "tls")

        # Build MIME message
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to
        if cc:
            msg["Cc"] = cc
        # BCC not added to headers (by design), but included in recipients

        subtype = "html" if body_type == "html" else "plain"
        msg.attach(MIMEText(body, subtype, "utf-8"))

        # All recipients
        all_recipients = [
            a.strip() for a in (to + "," + cc + "," + bcc).split(",")
            if a.strip()
        ]

        try:
            if security == "ssl":
                with smtplib.SMTP_SSL(host, port, timeout=15) as server:
                    if user:
                        server.login(user, password)
                    server.sendmail(from_addr, all_recipients, msg.as_string())
            else:
                with smtplib.SMTP(host, port, timeout=15) as server:
                    if security == "tls":
                        server.starttls()
                    if user:
                        server.login(user, password)
                    server.sendmail(from_addr, all_recipients, msg.as_string())
            logger.info("SendEmail: sent to %s (cc=%s)", to, cc or "none")
        except Exception as exc:
            logger.warning("SendEmail: SMTP failed (%s), logging instead", exc)
            self._log_email(to, subject, body)
            raise

    @staticmethod
    def _render(template: str, row: dict) -> str:
        """Replace {column} placeholders with values from row."""
        result = template
        for col, val in row.items():
            result = result.replace(f"{{{col}}}", str(val))
        return result

    @staticmethod
    def _log_email(to: str, subject: str, body: str) -> None:
        logger.info(
            "SendEmail [logged]: to=%s subject=%s body_len=%d",
            to, subject, len(body),
        )

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "connection_id": "",
            "to": "", "cc": "", "bcc": "",
            "subject": "F-Pulse Notification",
            "body": "Pipeline completed successfully.",
            "body_type": "plain",
            "per_row": False,
            "on_error": "fail",
            "smtp_host": "", "smtp_port": 587,
            "smtp_user": "", "smtp_pass": "",
            "from": "",
            "security": "tls",
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "connection_id", "type": "connection_picker", "label": "SMTP Connection",
             "tab": "Connection",
             "description": "Select an SMTP connection or configure inline below."},
            {"name": "to", "type": "text", "label": "To", "required": True,
             "tab": "Message",
             "placeholder": "user@example.com",
             "description": "Comma-separated. Supports {column} in per-row mode."},
            {"name": "cc", "type": "text", "label": "CC", "tab": "Message",
             "placeholder": "cc@example.com"},
            {"name": "bcc", "type": "text", "label": "BCC", "tab": "Message",
             "placeholder": "bcc@example.com"},
            {"name": "subject", "type": "text", "label": "Subject", "tab": "Message",
             "default": "F-Pulse Notification",
             "description": "Supports {column} placeholders."},
            {"name": "body_type", "type": "select", "label": "Body Format",
             "options": ["plain", "html"], "default": "plain", "tab": "Message"},
            {"name": "body", "type": "code", "label": "Email Body", "tab": "Message",
             "description": "Supports {column} placeholders. HTML if body_type=html."},
            {"name": "per_row", "type": "boolean", "label": "Send Per Row",
             "default": False, "tab": "Behavior",
             "description": "Send one email per input row instead of a single email."},
            {"name": "on_error", "type": "select", "label": "On Error",
             "options": ["fail", "continue"], "default": "fail", "tab": "Behavior",
             "description": "fail = abort pipeline, continue = log and keep going."},
            {"name": "security", "type": "select", "label": "Security",
             "options": ["tls", "ssl", "none"], "default": "tls", "tab": "SMTP (inline)"},
            {"name": "smtp_host", "type": "text", "label": "SMTP Host", "tab": "SMTP (inline)",
             "description": "Used only when no connection is selected."},
            {"name": "smtp_port", "type": "number", "label": "SMTP Port",
             "default": 587, "tab": "SMTP (inline)"},
            {"name": "smtp_user", "type": "text", "label": "SMTP User", "tab": "SMTP (inline)"},
            {"name": "smtp_pass", "type": "password", "label": "SMTP Password", "tab": "SMTP (inline)"},
            {"name": "from", "type": "text", "label": "From Address", "tab": "SMTP (inline)"},
        ]


@register(StepType.SLACK_NOTIFY)
class SlackNotifyNode(BaseNode):
    """
    Slack Notification — send a message to a Slack channel via
    Incoming Webhook URL.

    Supports {column} placeholders in the message text.
    """

    @staticmethod
    def preview_message(params, row_count):
        # X4 — surface the channel + the fact that {col} interp would
        # fire once (not per-row, by Slack's webhook contract).
        channel = params.get("channel") or "(default Slack channel)"
        return (
            f"would post 1 Slack message to {channel} "
            f"(interpolated from {row_count} upstream row{'s' if row_count != 1 else ''})"
        )

    display_name = "Slack Notify"
    category = "action"
    description = "Send a message to a Slack channel"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        source = _get_single_input(ctx, self.params)

        webhook_url = self.params.get("webhook_url", "")
        message = self.params.get("message", "F-Pulse pipeline completed.")
        channel = self.params.get("channel", "")

        # Render placeholders from first row
        ctx.conn.register("__slack_input", source)
        first = ctx.conn.sql("SELECT * FROM __slack_input LIMIT 1").fetchdf()
        if not first.empty:
            row = first.iloc[0].to_dict()
            for col, val in row.items():
                message = message.replace(f"{{{col}}}", str(val))

        if webhook_url:
            # 2026-06-15 (security): SSRF guard, same as HTTP Request. A
            # user-supplied webhook URL can point at loopback / private /
            # cloud-metadata endpoints; block those (override with
            # FPULSE_HTTP_ALLOW_PRIVATE=1 for self-hosted internal webhooks).
            from fpulse.security.ssrf import check_url, SsrfBlockedError
            try:
                check_url(webhook_url, allow_private_env="FPULSE_HTTP_ALLOW_PRIVATE")
            except SsrfBlockedError as exc:
                raise ValueError(f"Slack/Teams: webhook URL blocked: {exc}") from exc

            payload: dict[str, Any] = {"text": message}
            if channel:
                payload["channel"] = channel

            try:
                data = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(
                    webhook_url, method="POST", data=data,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    resp.read()
                logger.info("SlackNotify: message sent to %s", channel or "default")
            except Exception as exc:
                logger.warning("SlackNotify: failed (%s), logging message instead", exc)
                logger.info("SlackNotify [logged]: %s", message)
        else:
            logger.info("SlackNotify [no webhook]: %s", message)

        # Pass data through unchanged
        return source

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "webhook_url": "", "message": "F-Pulse pipeline completed.",
            "channel": "",
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "webhook_url", "type": "text", "label": "Webhook URL",
             "required": True,
             "placeholder": "https://hooks.slack.com/services/...",
             "description": "Slack Incoming Webhook URL."},
            {"name": "message", "type": "code", "label": "Message",
             "description": "Supports {column} placeholders from input data."},
            {"name": "channel", "type": "text", "label": "Channel Override",
             "placeholder": "#general",
             "description": "Override the webhook's default channel."},
        ]


@register(StepType.COPY_DATA)
class CopyDataNode(BaseNode):
    """Copy Data — self-contained Copy Activity.

    Four configuration tabs:

      Source   — connection + (table | query) + optional WHERE filter
      Sink     — connection + table + table_action + write_behavior + keys
      Mapping  — auto | explicit list of {source, target, type}"""

    @staticmethod
    def preview_message(params, row_count):
        # X4 — Copy Data takes its own source so the upstream row_count
        # is usually 0 (it doesn't read from a canvas upstream). Tell
        # the operator about the source+sink pair the dry-run targets.
        src_table = params.get("source_table") or "(via source_query)"
        sink_table = params.get("sink_table") or "(no sink_table set)"
        write_behavior = params.get("write_behavior", "append")
        return (
            f"would copy from {src_table} to {sink_table} "
            f"(write_behavior={write_behavior})"
        )

    # Extended class docstring (X4 split the original docstring above
    # to land the preview_message hook between sections). The narrative
    # continues here as a comment block so the class body stays valid:
    #
    #   Four configuration tabs (unchanged):
    #     Source   — connection + (table | query) + optional WHERE filter
    #     Sink     — connection + table + table_action + write_behavior + keys
    #     Mapping  — auto | explicit list of {source, target, type}
    #     Settings — parallel_copies, skip_on_error, max_rows, batch_size,
    #                pre_copy_script, log_path, enable_staging
    #
    #   Three execution shapes are supported:
    #     1. source_connection set → read from source DB, write to sink DB
    #     2. only sink_connection set → take upstream input, write to sink DB
    #     3. neither set → identity pass-through (legacy behavior)
    #
    # The node always returns the relation it just wrote, so downstream
    # nodes can chain off the copied data (e.g. validate row count
    # after copy).

    display_name = "Copy Data"
    category = "action"
    description = "Copy data from a source to a destination with column mapping and retries"

    # ── execute ──────────────────────────────────────────────────────────
    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        source_conn_id = self.params.get("source_connection_id", "")
        sink_conn_id = self.params.get("sink_connection_id", "")

        # 1. Acquire the source relation
        if source_conn_id:
            source_rel = self._read_from_source(ctx, source_conn_id)
        else:
            inputs = ctx.get_inputs(self.params.get("_input_step_ids", []))
            if not inputs:
                raise ValueError(
                    "Copy Data: provide a source connection OR connect an upstream node"
                )
            source_rel = inputs[0]

        # 2. Apply mapping
        source_rel = self._apply_mapping(ctx, source_rel)

        # 3. Apply max_rows cap (Settings tab)
        max_rows = int(self.params.get("max_rows", 0) or 0)
        if max_rows > 0:
            capped_in = ctx.register_scoped("__copy_capped_in", source_rel)
            source_rel = ctx.conn.sql(f"SELECT * FROM {capped_in} LIMIT {max_rows}")

        # 4. Write to sink
        if sink_conn_id:
            self._write_to_sink(ctx, source_rel, sink_conn_id)
        else:
            passthrough = ctx.register_scoped("__copy_passthrough", source_rel)
            source_rel = ctx.conn.sql(f"SELECT * FROM {passthrough}")

        return source_rel

    # ── Source side ──────────────────────────────────────────────────────
    def _read_from_source(self, ctx: ExecutionContext, connection_id: str) -> duckdb.DuckDBPyRelation:
        from fpulse.nodes.db_source import DbSourceNode, _get_connection_config

        result = _get_connection_config(connection_id)
        if not result:
            raise ValueError(f"Copy Data: source connection '{connection_id}' not found")
        config, conn_type = result

        source_kind = (self.params.get("source_kind") or "table").lower()
        if source_kind == "query":
            query = (self.params.get("source_query") or "").strip()
            if not query:
                raise ValueError("Copy Data: source_kind=query but source_query is empty")
        else:
            table = (self.params.get("source_table") or "").strip()
            if not table:
                raise ValueError("Copy Data: source_kind=table but source_table is empty")
            where = (self.params.get("source_filter") or "").strip()
            query = f"SELECT * FROM {table}"
            if where:
                query += f" WHERE {where}"

        helper = DbSourceNode({"query": query, "connection_id": connection_id})
        rows, columns = helper._execute_real(conn_type, config, query)

        if not rows:
            col_defs = ", ".join(f'NULL AS "{c}"' for c in columns) if columns else "NULL AS empty"
            return ctx.conn.sql(f"SELECT {col_defs} WHERE false")

        _qcols = ", ".join(f'"{c}"' for c in columns)
        copy_src = ctx.scoped_name("__copy_src")
        ctx.conn.execute(
            f"CREATE OR REPLACE TEMP TABLE {copy_src} AS SELECT * FROM (VALUES "
            + helper._rows_to_values(rows, columns) + f") AS __vals ({_qcols})"
        )
        return ctx.conn.sql(f"SELECT * FROM {copy_src}")

    # ── Mapping ──────────────────────────────────────────────────────────
    def _apply_mapping(self, ctx: ExecutionContext, rel: duckdb.DuckDBPyRelation) -> duckdb.DuckDBPyRelation:
        mode = (self.params.get("mapping_mode") or "auto").lower()
        if mode == "auto":
            return rel

        mappings = self.params.get("mappings") or []
        if not mappings:
            return rel

        copy_map_in = ctx.register_scoped("__copy_map_in", rel)
        src_cols = set(rel.columns)
        parts: list[str] = []
        for m in mappings:
            tgt = (m.get("target") or "").strip()
            src = (m.get("source") or "").strip()
            sql_type = (m.get("type") or "").strip().upper() or "VARCHAR"
            if not tgt:
                continue
            if src and src in src_cols:
                expr = f'CAST("{src}" AS {sql_type})'
            else:
                expr = f'CAST(NULL AS {sql_type})'
            parts.append(f'{expr} AS "{tgt}"')

        if not parts:
            return rel
        return ctx.conn.sql(f"SELECT {', '.join(parts)} FROM {copy_map_in}")

    # ── Sink side ────────────────────────────────────────────────────────
    def _write_to_sink(self, ctx: ExecutionContext, rel: duckdb.DuckDBPyRelation, connection_id: str):
        from fpulse.nodes.activities import DbSinkNode
        from fpulse.nodes.db_source import _get_connection_config

        result = _get_connection_config(connection_id)
        if not result:
            raise ValueError(f"Copy Data: sink connection '{connection_id}' not found")
        config, conn_type = result

        table = (self.params.get("sink_table") or "").strip()
        if not table:
            raise ValueError("Copy Data: sink_table is required")

        table_action = (self.params.get("table_action") or "none").lower()
        write_behavior = (self.params.get("write_behavior") or "append").lower()
        pre_copy_sql = (self.params.get("pre_copy_script") or "").strip()
        post_copy_sql = (self.params.get("post_copy_script") or "").strip()
        skip_on_error = bool(self.params.get("skip_on_error", False))
        enable_staging = bool(self.params.get("enable_staging", False))
        key_columns = self.params.get("key_columns") or []

        # Map (table_action × write_behavior) → unified mode used by both
        # the bulk-load runner and the legacy row-by-row writer.
        if write_behavior == "upsert" or write_behavior == "merge":
            mode = "merge"
        elif table_action == "recreate":
            mode = "create"
        elif table_action == "truncate":
            mode = "truncate"
        elif write_behavior == "overwrite":
            mode = "create"
        else:
            mode = "append"

        try:
            # 1. Pre-copy script (runs first per documented order:
            #    table_action → pre_copy → write → post_copy).
            if pre_copy_sql:
                self._run_script(conn_type, config, pre_copy_sql)

            # 2. Write — try the bulk loader first when staging is on AND
            #    the user picked merge/append/create/truncate. Fall back
            #    to row-by-row INSERT when no plugin is registered for
            #    this dialect (BulkLoaderNotAvailable). Any other failure
            #    propagates — bulk-load failures are NOT silently swapped
            #    for slow-path writes.
            used_bulk = False
            if enable_staging:
                effective_batch = self._resolve_batch_size(
                    conn_type, self.params.get("batch_size"),
                )
                try:
                    self._run_bulk_load(
                        ctx=ctx, rel=rel, conn_type=conn_type, config=config,
                        table=table, mode=mode, primary_key=list(key_columns),
                        batch_size=effective_batch,
                    )
                    used_bulk = True
                except Exception as exc:  # noqa: BLE001 — narrow check below
                    from fpulse.engine.bulk_load.types import BulkLoaderNotAvailable
                    if not isinstance(exc, BulkLoaderNotAvailable):
                        raise

            if not used_bulk:
                copy_sink_export = ctx.register_scoped("__copy_sink_export", rel)
                columns = rel.columns
                rows = ctx.conn.sql(f"SELECT * FROM {copy_sink_export}").fetchall()
                sink_helper = DbSinkNode({"connection_id": connection_id, "table": table, "mode": mode})
                sink_helper._write_real(conn_type, config, table, columns, rows, mode)

            # 3. Post-copy script (ANALYZE, REFRESH MATERIALIZED VIEW, etc.).
            if post_copy_sql:
                self._run_script(conn_type, config, post_copy_sql)

        except Exception as e:
            if not skip_on_error:
                raise
            print(f"[copy_data] write failed but skip_on_error=true: {e}")

    # Per-dialect batch-size defaults used when the user hasn't set a
    # specific value (or set 0 = "auto"). Picked from each dialect's
    # native sweet spot — a Postgres COPY chunk wants ~10k rows; a
    # Snowflake stage wants ~50k+; a SQLite executemany wants ~1k.
    # Override per-step from the UI by entering any non-zero value.
    _BATCH_SIZE_DEFAULTS: dict[str, int] = {
        "postgresql": 10_000,
        "mysql": 5_000,
        "mssql": 5_000,
        "snowflake": 50_000,
        "bigquery": 50_000,
        "redshift": 50_000,
        "sqlite": 1_000,
    }

    @classmethod
    def _resolve_batch_size(cls, conn_type: str, requested: Any) -> int:
        """Pick the effective batch size: user value if set (>0), else
        the per-dialect default, else 10_000 as a generic fallback."""
        try:
            n = int(requested or 0)
        except (TypeError, ValueError):
            n = 0
        if n > 0:
            return n
        return cls._BATCH_SIZE_DEFAULTS.get(conn_type, 10_000)

    @classmethod
    def _run_bulk_load(
        cls, *, ctx: ExecutionContext, rel: duckdb.DuckDBPyRelation,
        conn_type: str, config: dict, table: str, mode: str,
        primary_key: list[str], batch_size: int,
    ) -> None:
        """Dispatch to the per-dialect bulk loader (Postgres COPY,
        Snowflake stage, BigQuery load, Redshift COPY, MSSQL bcp).

        Raises BulkLoaderNotAvailable when no plugin is registered —
        the caller falls back to the row-INSERT path.
        """
        from fpulse.engine.bulk_load.runner import bulk_load
        from fpulse.engine.bulk_load.types import BulkLoadRequest

        # Split schema.table if present so the loader can route correctly.
        if "." in table:
            schema_name, bare = table.split(".", 1)
        else:
            schema_name, bare = "public", table

        # Bulk loader's mode enum is {create, append, truncate, merge}.
        # Our `merge` covers both upsert + merge — primary_key must be set.
        if mode == "merge" and not primary_key:
            raise ValueError(
                "Copy Data: write_behavior=upsert/merge requires Key Columns"
            )

        req = BulkLoadRequest(
            conn_type=conn_type, config=config,
            table=bare, schema_name=schema_name,
            mode=mode,  # type: ignore[arg-type]
            primary_key=primary_key,
            relation=rel, duckdb_conn=ctx.conn,
            columns=list(rel.columns),
            batch_size=batch_size,
        )
        bulk_load(req)

    @staticmethod
    def _run_script(conn_type: str, config: dict, sql: str):
        if conn_type == "sqlite":
            import sqlite3
            db_path = config.get("database") or config.get("file")
            conn = sqlite3.connect(db_path)
            try:
                conn.executescript(sql)
                conn.commit()
            finally:
                conn.close()
        elif conn_type == "postgresql":
            import psycopg2  # type: ignore
            conn = psycopg2.connect(
                host=config.get("host"), port=config.get("port") or 5432,
                dbname=config.get("database"),
                user=config.get("user") or config.get("username"),
                password=config.get("password"), connect_timeout=10,
            )
            try:
                conn.cursor().execute(sql)
                conn.commit()
            finally:
                conn.close()
        elif conn_type == "mysql":
            import pymysql  # type: ignore
            conn = pymysql.connect(
                host=config.get("host"), port=int(config.get("port") or 3306),
                database=config.get("database"),
                user=config.get("user") or config.get("username"),
                password=config.get("password"), connect_timeout=10,
            )
            try:
                conn.cursor().execute(sql)
                conn.commit()
            finally:
                conn.close()
        else:
            print(f"[copy_data] pre-copy script unsupported for {conn_type}, skipping")

    # ── Param schema ─────────────────────────────────────────────────────
    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            # Source
            "source_connection_id": "",
            "source_kind": "table",
            "source_table": "",
            "source_query": "",
            "source_filter": "",
            # Sink
            "sink_connection_id": "",
            "sink_table": "",
            "table_action": "none",
            "write_behavior": "append",
            "key_columns": [],
            "pre_copy_script": "",
            "post_copy_script": "",
            "batch_size": 0,  # 0 = auto (per-dialect default)
            # Mapping
            "mapping_mode": "auto",
            "mappings": [],
            # Settings
            "parallel_copies": 1,
            "skip_on_error": False,
            "max_rows": 0,
            "log_path": "",
            "enable_staging": False,
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            # Source tab
            {"name": "source_connection_id", "type": "connection_picker", "label": "Source Connection",
             "description": "Leave empty to copy from the upstream node instead.", "tab": "Source"},
            {"name": "source_kind", "type": "select", "label": "Source Type",
             "options": ["table", "query"], "default": "table", "tab": "Source"},
            {"name": "source_table", "type": "text", "label": "Source Table",
             "placeholder": "schema.table_name", "tab": "Source"},
            {"name": "source_query", "type": "sql", "label": "Source Query",
             "placeholder": "SELECT * FROM ...", "tab": "Source"},
            {"name": "source_filter", "type": "text", "label": "Filter (WHERE clause)",
             "placeholder": "updated_at > '2025-01-01'", "tab": "Source"},
            # Sink tab
            {"name": "sink_connection_id", "type": "connection_picker", "label": "Sink Connection",
             "tab": "Sink"},
            {"name": "sink_table", "type": "text", "label": "Sink Table",
             "placeholder": "schema.target_table", "required": True, "tab": "Sink"},
            {"name": "table_action", "type": "select", "label": "Table Action",
             "options": ["none", "autocreate", "recreate", "truncate"], "default": "none", "tab": "Sink"},
            {"name": "write_behavior", "type": "select", "label": "Write Behavior",
             "options": ["append", "overwrite", "upsert", "merge"], "default": "append", "tab": "Sink"},
            {"name": "key_columns", "type": "column_list", "label": "Key Columns (for upsert/merge)",
             "tab": "Sink"},
            {"name": "pre_copy_script", "type": "sql", "label": "Pre-Copy Script",
             "placeholder": "-- runs once before the write (after Table Action).",
             "description": "Runs on the SINK connection BEFORE rows are written. Use to prepare/cleanup. Order: Table Action → Pre-Copy → Write → Post-Copy.",
             "tab": "Sink"},
            {"name": "post_copy_script", "type": "sql", "label": "Post-Copy Script",
             "placeholder": "ANALYZE schema.target_table",
             "description": "Runs on the SINK connection AFTER rows are written. Common: ANALYZE, REFRESH MATERIALIZED VIEW, audit insert.",
             "tab": "Sink"},
            {"name": "batch_size", "type": "number", "label": "Batch Size", "default": 0,
             "tab": "Sink",
             "description": (
                 "Rows per chunk on the bulk-load path. 0 = auto (per-dialect default: "
                 "Postgres 10k, MySQL/MSSQL 5k, Snowflake/BigQuery/Redshift 50k, SQLite 1k). "
                 "Override only if you know your row size or memory budget needs a different value."
             )},
            # Mapping tab
            {"name": "mapping_mode", "type": "select", "label": "Schema Mapping",
             "options": ["auto", "explicit"], "default": "auto", "tab": "Mapping"},
            {"name": "mappings", "type": "schema_map", "label": "Field Mappings", "tab": "Mapping"},
            # Settings tab
            {"name": "parallel_copies", "type": "number", "label": "Parallel Copies", "default": 1, "tab": "Settings"},
            {"name": "skip_on_error", "type": "boolean", "label": "Skip Incompatible Rows", "tab": "Settings"},
            {"name": "max_rows", "type": "number", "label": "Max Rows (0 = unlimited)", "default": 0, "tab": "Settings"},
            {"name": "log_path", "type": "text", "label": "Log Path (for skipped rows)",
             "placeholder": "logs/copy_errors.csv", "tab": "Settings"},
            {"name": "enable_staging", "type": "boolean", "label": "Enable Staging (interim copy)", "tab": "Settings"},
        ]


@register(StepType.DELETE_DATA)
class DeleteDataNode(BaseNode):
    """
    Delete Data — remove rows matching a condition (inverse of Filter).

    Rows where the condition is TRUE are removed; all other rows pass through.
    """
    display_name = "Delete Data"
    category = "action"
    description = "Remove rows that match your condition (the opposite of Filter)"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        source = _get_single_input(ctx, self.params)

        # 2026-06-11 (node-audit): the param schema has always advertised
        # a 'files' target (target_path / wildcard / recursive) that this
        # execute NEVER implemented — a retention pipeline configured to
        # delete files silently deleted nothing and reported success.
        # For a delete node, claiming-but-not-deleting is as dangerous as
        # over-deleting. Fail loudly until files mode actually exists.
        target_kind = self.params.get("target_kind", "rows")
        if target_kind == "files":
            raise ValueError(
                "Delete Data: 'files' mode is not implemented in this edition — "
                "NO files would be deleted. Use a File System node for file "
                "cleanup, or switch Delete Target to 'rows'."
            )

        condition = self.params.get("condition", "")
        if not condition:
            # No condition = nothing to remove. Pass through unchanged;
            # the frontend validator flags this as a likely mistake.
            return source

        delete_input = ctx.register_scoped("__delete_input", source)
        return ctx.conn.sql(
            f"SELECT * FROM {delete_input} WHERE NOT ({condition})"
        )

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "target_kind": "rows",
            "condition": "id IS NULL",
            "target_path": "",
            "recursive": False,
            "wildcard": "",
            "max_concurrent": 1,
            "log_path": "",
            "enable_logging": False,
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            # Source tab — what to delete
            {"name": "target_kind", "type": "select", "label": "Delete Target",
             "options": ["rows", "files"], "required": True, "tab": "Source",
             "description": "rows: filter upstream relation. files: remove files from disk."},
            {"name": "condition", "type": "expression", "label": "Delete Condition",
             "tab": "Source",
             "placeholder": "status = 'deleted' OR expired = true",
             "description": "Rows matching this condition are REMOVED. (rows mode)"},
            {"name": "target_path", "type": "text", "label": "File / Folder Path",
             "tab": "Source",
             "placeholder": "/data/incoming/",
             "description": "Path to delete. (files mode)"},
            {"name": "wildcard", "type": "text", "label": "Wildcard Filter",
             "tab": "Source",
             "placeholder": "*.csv",
             "description": "Optional glob pattern to limit deleted files."},
            {"name": "recursive", "type": "boolean", "label": "Recursive",
             "tab": "Source",
             "description": "Recurse into subfolders. (files mode)"},
            # Settings tab
            {"name": "max_concurrent", "type": "number", "label": "Max Concurrent Connections",
             "default": 1, "tab": "Settings"},
            # Logging tab
            {"name": "enable_logging", "type": "boolean", "label": "Enable Logging",
             "tab": "Logging",
             "description": "Write a CSV record of every deleted file/row count."},
            {"name": "log_path", "type": "text", "label": "Log Folder",
             "tab": "Logging",
             "placeholder": "logs/delete/"},
        ]


@register(StepType.GET_METADATA)
class GetMetadataNode(BaseNode):
    """
    Get Metadata — return schema and statistics about the input data
    instead of the data itself.

    Returns one row per column: name, type, nullable, null_count, distinct_count,
    min_value, max_value, plus a total row_count.
    """
    display_name = "Get Metadata"
    category = "action"
    description = "Inspect your data — column names, types, row counts, and basic statistics"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        source = _get_single_input(ctx, self.params)

        ctx.conn.register("__meta_input", source)

        # Get total row count
        total = ctx.conn.sql("SELECT COUNT(*) AS n FROM __meta_input").fetchone()
        row_count = total[0] if total else 0

        # Get column info from the relation's description
        columns = source.columns
        types = source.types

        meta_rows: list[dict] = []
        for col_name, col_type in zip(columns, types):
            safe_col = f'"{col_name}"'

            null_count = 0
            distinct_count = 0
            min_val = None
            max_val = None

            try:
                stats = ctx.conn.sql(
                    f"SELECT "
                    f"  SUM(CASE WHEN {safe_col} IS NULL THEN 1 ELSE 0 END) AS nulls, "
                    f"  COUNT(DISTINCT {safe_col}) AS distincts, "
                    f"  MIN({safe_col})::VARCHAR AS min_val, "
                    f"  MAX({safe_col})::VARCHAR AS max_val "
                    f"FROM __meta_input"
                ).fetchone()
                if stats:
                    null_count = int(stats[0] or 0)
                    distinct_count = int(stats[1] or 0)
                    min_val = stats[2]
                    max_val = stats[3]
            except Exception:
                # Some types may not support MIN/MAX — that's okay
                pass

            meta_rows.append({
                "column_name": col_name,
                "column_type": str(col_type),
                "nullable": null_count > 0,
                "null_count": null_count,
                "distinct_count": distinct_count,
                "min_value": str(min_val) if min_val is not None else "",
                "max_value": str(max_val) if max_val is not None else "",
                "total_rows": row_count,
            })

        if not meta_rows:
            return ctx.conn.sql(
                "SELECT 0 AS total_rows, '' AS column_name, "
                "'' AS column_type WHERE false"
            )

        return _rows_to_relation(ctx.conn, meta_rows, "__metadata_out")

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {}

    @staticmethod
    def param_schema() -> list[dict]:
        # 2026-06-15 (honest config): Get Metadata profiles the UPSTREAM
        # relation and emits a fixed column-stats report — it takes no
        # configuration. The previous schema advertised file/directory stat,
        # field selection, and row-count/size toggles that execute() never
        # read (dishonest config). File/directory introspection is a separate
        # future feature, not a flipped switch here.
        return []
