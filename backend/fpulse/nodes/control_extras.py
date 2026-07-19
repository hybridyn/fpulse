"""
Control & integration primitives.

  APPEND_VARIABLE     — push value into array variable
  FILTER_ARRAY        — filter an array variable
  VALIDATION          — wait until file/dataset exists
  FAIL                — fail pipeline with a custom message
  FILE_SYSTEM         — copy/move/rename/delete files on the local data dir
  EXECUTE_SQL_TASK    — run arbitrary SQL on a connection
"""

from __future__ import annotations

import ast
import logging
import operator
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

# Stage 2.5b: duckdb only used for type annotations on helpers and
# execute() returns.
if TYPE_CHECKING:
    import duckdb

from fpulse.ir.schema import StepType
from fpulse.nodes.base import BaseNode, ExecutionContext
from fpulse.nodes.registry import register

logger = logging.getLogger(__name__)


def _get_input(ctx: ExecutionContext, params: dict) -> duckdb.DuckDBPyRelation | None:
    inputs = ctx.get_inputs(params.get("_input_step_ids", []))
    return inputs[0] if inputs else None


def _empty(ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
    return ctx.conn.sql("SELECT NULL AS empty WHERE false")


# ─────────────────────────────────────────────────────────
#  APPEND VARIABLE
# ─────────────────────────────────────────────────────────

@register(StepType.APPEND_VARIABLE)
class AppendVariableNode(BaseNode):
    """Append a value to an array variable held on the execution context.

    Pushes a single item onto a predeclared array. We model the array on
    `ctx.vars[name]` so it survives across steps in the same run.
    """
    display_name = "Append Variable"
    category = "flow_control"
    description = "Add a value to a list-style pipeline variable"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        name = self.params.get("variable_name", "")
        value = self.params.get("value", "")
        if not name:
            raise ValueError("Append Variable: 'variable_name' is required")

        existing = ctx.vars.get(name)
        if existing is None:
            arr: list[Any] = []
        elif isinstance(existing, list):
            arr = list(existing)
        else:
            arr = [existing]

        arr.append(value)
        ctx.vars[name] = arr
        logger.info("AppendVariable: %s now has %d items", name, len(arr))

        src = _get_input(ctx, self.params)
        return src if src is not None else _empty(ctx)

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"variable_name": "my_array", "value": ""}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "variable_name", "type": "text", "label": "Variable Name",
             "required": True, "tab": "Settings",
             "description": "Name of the array variable to append to."},
            {"name": "value", "type": "text", "label": "Value to Append",
             "tab": "Settings",
             "description": "Literal value or @-expression to push."},
        ]


# ─────────────────────────────────────────────────────────
#  LOOKUP ACTIVITY
# ─────────────────────────────────────────────────────────

@register(StepType.LOOKUP_ACTIVITY)
class LookupActivityNode(BaseNode):
    """Lookup *activity* — fetch reference row(s) into a named variable.

    This is the orchestration-layer Lookup ("Lookup activity"),
    distinct from the LOOKUP *transformation* (which enriches a stream by
    joining a second input). It reads its single upstream relation — wire
    any source / db_source / local_table_source / query into it, so the
    activity composes with the whole connector ecosystem instead of
    re-implementing database drivers — and captures:

        $vars.<output_var> = {
            "firstRow": {col: value, ...},   # {} when no rows
            "rows":     [ {...}, ... ],       # up to max_rows (off when first-row-only)
            "count":    <int>,
            "isEmpty":  <bool>,
        }

    Downstream steps consume it through the expression engine — the
    executor resolves these per-step, in topological order, so any step
    placed after the lookup sees the captured value:

        {{ $vars.watermark.firstRow.max_ts }}   →  inject into a filter / query
        {{ $vars.watermark.count }}             →  gate an If / Fail on row count
        {{ $vars.watermark.isEmpty }}           →  boolean branch

    The looked-up rows are ALSO returned as the node's relation, so the
    expression-style ``{{ $('Lookup (Activity)').first().col }}`` reference and a
    normal downstream dataflow both work.

    Use ``order_by`` + first-row-only for the classic watermark pattern
    (``ORDER BY updated_at DESC`` → newest row wins).
    """
    display_name = "Lookup"
    category = "flow_control"
    description = (
        "Lookup activity — fetch a value or reference row into a "
        "variable for control flow (watermarks, config lookups, row-count gates)"
    )

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        # 2026-06-15 (#15): self-contained mode. With a connection +
        # query the activity fetches its OWN reference data (no upstream wiring
        # needed), like a self-contained Lookup activity. Without a connection it
        # reads the wired upstream relation (back-compat). The driver matrix is
        # shared with Execute SQL via _run_connection_sql.
        source_mode = (self.params.get("source_mode") or "").strip().lower()
        conn_id = (self.params.get("connection_id") or "").strip()
        use_connection = source_mode == "connection" or (not source_mode and conn_id)
        if use_connection:
            if not conn_id:
                raise ValueError("Lookup (Activity): connection mode needs a 'connection_id'.")
            query = (self.params.get("query") or "").strip()
            if not query:
                raise ValueError("Lookup (Activity): connection mode needs a 'query'.")
            try:
                q_timeout = int(self.params.get("timeout", 60) or 60)
            except (TypeError, ValueError):
                q_timeout = 60
            try:
                conn_rows, _ = _run_connection_sql(conn_id, query, q_timeout)
            except ValueError as exc:
                raise ValueError(f"Lookup (Activity): {exc}") from exc
            except Exception as exc:  # noqa: BLE001 — driver/query error
                raise RuntimeError(f"Lookup (Activity): query failed: {exc}") from exc
            src = _rows(conn_rows, ctx) if conn_rows else _empty(ctx)
        else:
            src = _get_input(ctx, self.params)
            if src is None:
                raise ValueError(
                    "Lookup (Activity): needs one input — wire a reference dataset in, "
                    "or set a connection + query (self-contained mode)."
                )

        output_var = (self.params.get("output_var") or "").strip()
        first_row_only = bool(self.params.get("first_row_only", True))
        on_empty = (self.params.get("on_empty") or "fail").lower()
        where = (self.params.get("filter") or "").strip()
        order_by = (self.params.get("order_by") or "").strip()
        try:
            max_rows = int(self.params.get("max_rows", 5000) or 5000)
        except (TypeError, ValueError):
            raise ValueError("Lookup (Activity): 'max_rows' must be a number.")
        if max_rows <= 0:
            raise ValueError("Lookup (Activity): 'max_rows' must be greater than 0.")
        if on_empty not in ("fail", "empty"):
            raise ValueError(
                f"Lookup (Activity): invalid 'on_empty' value '{on_empty}'. Allowed: fail, empty."
            )

        # Per-step view + materialized output table so the returned relation
        # is stable (independent of later view re-registration) and keeps
        # its real column types (VALUES-based rebuilds would coerce them).
        safe = re.sub(r"\W", "_", str(self.params.get("_step_id", "lookup")))
        view = f"__lookup_act_{safe}"
        out_tbl = f"__lookup_act_out_{safe}"
        ctx.conn.register(view, src)

        sql = f"SELECT * FROM {view}"
        if where:
            sql += f" WHERE {where}"
        if order_by:
            sql += f" ORDER BY {order_by}"
        sql += f" LIMIT {1 if first_row_only else max_rows}"

        ctx.conn.execute(f"CREATE OR REPLACE TEMP TABLE {out_tbl} AS {sql}")
        rel = ctx.conn.sql(f"SELECT * FROM {out_tbl}")

        cols = rel.columns
        rows = [dict(zip(cols, r)) for r in rel.fetchall()]
        count = len(rows)

        if count == 0 and on_empty == "fail":
            raise ValueError(
                "Lookup (Activity): returned 0 rows. Set 'If No Rows' to 'empty' "
                "to continue with an empty result, or adjust the filter."
            )

        if output_var:
            ctx.vars[output_var] = {
                "firstRow": rows[0] if rows else {},
                "rows": rows,
                "value": rows,   # alias for `rows`
                "count": count,
                "isEmpty": count == 0,
            }
            logger.info(
                "LookupActivity: captured %d row(s) into $vars.%s", count, output_var
            )

        return rel

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "output_var": "lookup_result",
            "source_mode": "upstream",
            "connection_id": "",
            "query": "",
            "timeout": 60,
            "first_row_only": True,
            "filter": "",
            "order_by": "",
            "max_rows": 5000,
            "on_empty": "fail",
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "output_var", "type": "text", "label": "Output Variable Name",
             "required": True, "placeholder": "watermark",
             "description": (
                 "Captured as $vars.<name>. Reference downstream with "
                 "{{ $vars.<name>.firstRow.<column> }}, {{ $vars.<name>.count }}, "
                 "{{ $vars.<name>.value }}, or {{ $vars.<name>.isEmpty }}."
             )},
            # 2026-06-15 (#15): self-contained source.
            {"name": "source_mode", "type": "select", "label": "Source",
             "options": ["upstream", "connection"], "default": "upstream",
             "description": "upstream = read the wired input. connection = fetch with this node's own connection + query (self-contained mode)."},
            {"name": "connection_id", "type": "connection", "label": "Connection",
             "show_when": {"source_mode": ["connection"]},
             "description": "Database connection to run the query against."},
            {"name": "query", "type": "code", "label": "Query (SQL)",
             "show_when": {"source_mode": ["connection"]},
             "placeholder": "SELECT MAX(updated_at) AS watermark FROM orders",
             "description": "SELECT run against the connection; its rows are the lookup result."},
            {"name": "timeout", "type": "number", "label": "Query Timeout (s)", "default": 60,
             "show_when": {"source_mode": ["connection"]}},
            {"name": "first_row_only", "type": "boolean", "label": "First Row Only",
             "default": True,
             "description": "On = capture a single row (firstRow). Off = capture up to Max Rows into .rows."},
            {"name": "order_by", "type": "text", "label": "Order By",
             "placeholder": "updated_at DESC",
             "description": "Which row wins when First Row Only is on (e.g. updated_at DESC for a watermark)."},
            {"name": "filter", "type": "expression", "label": "Filter (WHERE)",
             "placeholder": "status = 'active'",
             "description": "Optional SQL predicate over the incoming reference data."},
            {"name": "max_rows", "type": "number", "label": "Max Rows", "default": 5000,
             "show_when": {"first_row_only": [False]},
             "description": "Cap on rows captured into .rows — lookups are meant for small reference reads."},
            {"name": "on_empty", "type": "select", "label": "If No Rows",
             "options": ["fail", "empty"], "default": "fail",
             "description": "fail = stop the run (default); empty = continue with firstRow={} and count=0."},
        ]


# ─────────────────────────────────────────────────────────
#  Safe expression evaluator (replaces eval())
# ─────────────────────────────────────────────────────────

_SAFE_OPS = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne,
    ast.Lt: operator.lt, ast.LtE: operator.le,
    ast.Gt: operator.gt, ast.GtE: operator.ge,
    ast.Is: operator.is_, ast.IsNot: operator.is_not,
    ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b,
    ast.Add: operator.add, ast.Sub: operator.sub,
    ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv,
    ast.And: None, ast.Or: None, ast.Not: None,
    ast.USub: operator.neg,
}


def _safe_eval_node(node: ast.AST, ctx_item: Any) -> Any:
    """Recursively evaluate an AST node with only safe operations."""
    if isinstance(node, ast.Expression):
        return _safe_eval_node(node.body, ctx_item)

    # Literals: numbers, strings, booleans, None
    if isinstance(node, ast.Constant):
        return node.value

    # Name references: 'item', 'True', 'False', 'None'
    if isinstance(node, ast.Name):
        if node.id == "item":
            return ctx_item
        if node.id == "True":
            return True
        if node.id == "False":
            return False
        if node.id == "None":
            return None
        raise ValueError(f"Unknown variable: {node.id}")

    # Attribute access: item.name → item["name"] for dicts
    if isinstance(node, ast.Attribute):
        obj = _safe_eval_node(node.value, ctx_item)
        if isinstance(obj, dict):
            return obj.get(node.attr)
        return getattr(obj, node.attr, None)

    # Subscript: item["key"] or item[0]
    if isinstance(node, ast.Subscript):
        obj = _safe_eval_node(node.value, ctx_item)
        key = _safe_eval_node(node.slice, ctx_item)
        return obj[key]

    # Comparisons: item > 100, item.status == "active"
    if isinstance(node, ast.Compare):
        left = _safe_eval_node(node.left, ctx_item)
        for op_node, comparator in zip(node.ops, node.comparators):
            right = _safe_eval_node(comparator, ctx_item)
            op_fn = _SAFE_OPS.get(type(op_node))
            if op_fn is None:
                raise ValueError(f"Unsupported comparison: {type(op_node).__name__}")
            if not op_fn(left, right):
                return False
            left = right
        return True

    # Boolean operators: and, or
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return all(_safe_eval_node(v, ctx_item) for v in node.values)
        if isinstance(node.op, ast.Or):
            return any(_safe_eval_node(v, ctx_item) for v in node.values)

    # Unary: not, -
    if isinstance(node, ast.UnaryOp):
        operand = _safe_eval_node(node.operand, ctx_item)
        if isinstance(node.op, ast.Not):
            return not operand
        if isinstance(node.op, ast.USub):
            return -operand

    # Binary math: item.price * item.qty
    if isinstance(node, ast.BinOp):
        left = _safe_eval_node(node.left, ctx_item)
        right = _safe_eval_node(node.right, ctx_item)
        op_fn = _SAFE_OPS.get(type(node.op))
        if op_fn is None:
            raise ValueError(f"Unsupported operator: {type(node.op).__name__}")
        return op_fn(left, right)

    # List/Tuple literals: [1, 2, 3]
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_safe_eval_node(el, ctx_item) for el in node.elts]

    # Function calls: len(item), str(item.name)
    if isinstance(node, ast.Call):
        _SAFE_FUNCS = {"len": len, "str": str, "int": int, "float": float,
                       "bool": bool, "abs": abs, "min": min, "max": max,
                       "round": round, "type": type}
        if isinstance(node.func, ast.Name) and node.func.id in _SAFE_FUNCS:
            args = [_safe_eval_node(a, ctx_item) for a in node.args]
            return _SAFE_FUNCS[node.func.id](*args)
        raise ValueError(f"Disallowed function call: {ast.dump(node.func)}")

    raise ValueError(f"Unsupported expression: {type(node).__name__}")


def _safe_eval_condition(condition: str, item: Any) -> bool:
    """Safely evaluate a condition string against an item. No exec/eval."""
    if condition.strip().upper() in ("TRUE", "1"):
        return True
    if condition.strip().upper() in ("FALSE", "0"):
        return False
    tree = ast.parse(condition, mode="eval")
    return bool(_safe_eval_node(tree, item))


# ─────────────────────────────────────────────────────────
#  FILTER ARRAY
# ─────────────────────────────────────────────────────────

@register(StepType.FILTER_ARRAY)
class FilterArrayNode(BaseNode):
    """Filter an array variable using a SQL boolean expression.

    The expression sees each array element as `item`. Items where the
    expression is TRUE are kept; others are dropped. The filtered array
    is written back to `ctx.vars[output_variable]`.
    """
    display_name = "Filter Array"
    category = "flow_control"
    description = "Keep only items in a list-style variable that match your condition"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        in_name = self.params.get("input_variable", "")
        out_name = self.params.get("output_variable", in_name)
        condition = self.params.get("condition", "TRUE")
        if not in_name:
            raise ValueError("Filter Array: 'input_variable' is required")

        items = ctx.vars.get(in_name) or []
        if not isinstance(items, list):
            items = [items]

        kept: list[Any] = []
        for item in items:
            try:
                if _safe_eval_condition(condition, item):
                    kept.append(item)
            except Exception:
                continue

        ctx.vars[out_name] = kept
        logger.info("FilterArray: %s -> %s (%d/%d kept)", in_name, out_name, len(kept), len(items))

        src = _get_input(ctx, self.params)
        return src if src is not None else _empty(ctx)

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"input_variable": "my_array", "output_variable": "my_array_filtered", "condition": "True"}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "input_variable", "type": "text", "label": "Input Variable",
             "required": True, "tab": "Settings"},
            {"name": "output_variable", "type": "text", "label": "Output Variable",
             "tab": "Settings",
             "description": "Where to store the filtered array (default: same as input)."},
            {"name": "condition", "type": "expression", "label": "Condition",
             "required": True, "tab": "Settings",
             "placeholder": "item > 100",
             "description": "Python boolean expression. Use 'item' to reference each element."},
        ]


# ─────────────────────────────────────────────────────────
#  VALIDATION
# ─────────────────────────────────────────────────────────

@register(StepType.VALIDATION)
class ValidationNode(BaseNode):
    """Wait for a file (or upstream input) to exist before proceeding.

    Polls every `sleep` seconds until the dataset is present, the
    timeout is reached, or the minimum size
    threshold is met. If `child_items=True`, waits for at least one entry
    inside a directory.
    """
    display_name = "Wait for File"
    category = "flow_control"
    description = "Wait until a file or dataset exists before continuing"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        path = self.params.get("path", "")
        timeout = int(self.params.get("timeout", 60) or 60)
        sleep_s = max(1, int(self.params.get("sleep", 5) or 5))
        min_size = int(self.params.get("minimum_size", 0) or 0)
        child_items = bool(self.params.get("child_items", False))

        if not path:
            # No file to validate — fall back to checking upstream produced data
            src = _get_input(ctx, self.params)
            if src is None:
                raise ValueError("Validation: no path and no upstream input")
            return src

        deadline = time.time() + timeout
        while time.time() < deadline:
            p = Path(path)
            if p.exists():
                if child_items and p.is_dir():
                    if any(p.iterdir()):
                        break
                elif min_size > 0 and p.is_file():
                    if p.stat().st_size >= min_size:
                        break
                else:
                    break
            time.sleep(sleep_s)
        else:
            raise TimeoutError(
                f"Validation: '{path}' did not satisfy criteria within {timeout}s"
            )

        logger.info("Validation: '%s' is ready", path)
        src = _get_input(ctx, self.params)
        return src if src is not None else _empty(ctx)

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "path": "", "timeout": 60, "sleep": 5,
            "minimum_size": 0, "child_items": False,
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "path", "type": "text", "label": "File / Directory Path",
             "required": True, "tab": "Settings",
             "placeholder": "/data/incoming/orders.csv"},
            {"name": "timeout", "type": "number", "label": "Timeout (seconds)",
             "default": 60, "tab": "Settings"},
            {"name": "sleep", "type": "number", "label": "Poll Interval (seconds)",
             "default": 5, "tab": "Settings"},
            {"name": "minimum_size", "type": "number", "label": "Minimum Size (bytes)",
             "default": 0, "tab": "Settings",
             "description": "0 to skip the size check."},
            {"name": "child_items", "type": "boolean", "label": "Require Child Items",
             "default": False, "tab": "Settings",
             "description": "When path is a directory, wait until it is non-empty."},
        ]


# ─────────────────────────────────────────────────────────
#  FAIL
# ─────────────────────────────────────────────────────────

@register(StepType.FAIL)
class FailNode(BaseNode):
    """Explicitly fail the pipeline with a message and error code.

    Use this on the failure branch of an If/Switch to abort the run with
    a clear, actionable error message instead of letting the pipeline
    silently complete.
    """
    display_name = "Fail"
    category = "flow_control"
    description = "Stop the pipeline with an error message"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        message = self.params.get("message", "Pipeline failed by Fail activity")
        code = self.params.get("error_code", "USER_FAIL")
        condition = self.params.get("condition", "")

        # Optional gating: only fail when the condition (over upstream input) is true
        if condition:
            src = _get_input(ctx, self.params)
            if src is not None:
                ctx.conn.register("__fail_input", src)
                row = ctx.conn.sql(
                    f"SELECT COUNT(*) FROM __fail_input WHERE {condition}"
                ).fetchone()
                if not row or row[0] == 0:
                    return src

        # Render {col} placeholders from the first input row, if any
        src = _get_input(ctx, self.params)
        if src is not None:
            ctx.conn.register("__fail_render", src)
            head = ctx.conn.sql("SELECT * FROM __fail_render LIMIT 1").fetchdf()
            if not head.empty:
                row = head.iloc[0].to_dict()
                for col, val in row.items():
                    message = message.replace(f"{{{col}}}", str(val))

        raise RuntimeError(f"[{code}] {message}")

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "message": "Pipeline failed by Fail activity",
            "error_code": "USER_FAIL",
            "condition": "",
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "message", "type": "text", "label": "Failure Message",
             "required": True, "tab": "Settings",
             "placeholder": "Row count below threshold ({row_count})",
             "description": "Supports {column} placeholders from the first upstream row."},
            {"name": "error_code", "type": "text", "label": "Error Code",
             "default": "USER_FAIL", "tab": "Settings"},
            {"name": "condition", "type": "expression", "label": "Fail When (optional)",
             "tab": "Settings",
             "placeholder": "row_count < 100",
             "description": "Only fail if this SQL condition matches some upstream rows."},
        ]


# ─────────────────────────────────────────────────────────
#  FILE SYSTEM
# ─────────────────────────────────────────────────────────

@register(StepType.FILE_SYSTEM)
class FileSystemNode(BaseNode):
    """Local file-system task: copy / move / rename / delete files & dirs.

    Operates on the local F-Pulse runtime filesystem. For S3/blob, use
    the dedicated sink/source nodes.
    """
    display_name = "File System"
    category = "action"
    description = "Copy, move, rename or delete files & folders"

    _OPS = {
        "copy_file", "move_file", "rename_file", "delete_file",
        "copy_directory", "move_directory", "delete_directory",
        "create_directory",
    }

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        op = self.params.get("operation", "copy_file")
        source = self.params.get("source", "")
        destination = self.params.get("destination", "")
        overwrite = bool(self.params.get("overwrite", False))
        recursive = bool(self.params.get("recursive", True))

        if op not in self._OPS:
            raise ValueError(f"File System: unknown operation '{op}'")

        result_rows: list[dict] = []
        try:
            if op == "copy_file":
                if not overwrite and os.path.exists(destination):
                    raise FileExistsError(destination)
                shutil.copy2(source, destination)
            elif op == "move_file":
                shutil.move(source, destination)
            elif op == "rename_file":
                os.replace(source, destination)
            elif op == "delete_file":
                Path(source).unlink(missing_ok=True)
            elif op == "copy_directory":
                shutil.copytree(source, destination, dirs_exist_ok=overwrite)
            elif op == "move_directory":
                shutil.move(source, destination)
            elif op == "delete_directory":
                shutil.rmtree(source, ignore_errors=not recursive)
            elif op == "create_directory":
                Path(source).mkdir(parents=True, exist_ok=True)

            result_rows.append({
                "operation": op,
                "source": source,
                "destination": destination,
                "status": "success",
                "error": "",
            })
            logger.info("FileSystem %s: %s -> %s OK", op, source, destination)
        except Exception as exc:
            result_rows.append({
                "operation": op,
                "source": source,
                "destination": destination,
                "status": "error",
                "error": str(exc),
            })
            if not bool(self.params.get("continue_on_error", False)):
                raise

        # Build a relation from the result row. Per-step table name so two
        # File System nodes don't clobber each other's lazily-returned output.
        fs_out = ctx.scoped_name("__fs_out")
        ctx.conn.execute(
            f"CREATE OR REPLACE TEMP TABLE {fs_out} (operation VARCHAR, source VARCHAR, "
            f"destination VARCHAR, status VARCHAR, error VARCHAR)"
        )
        for r in result_rows:
            ctx.conn.execute(
                f"INSERT INTO {fs_out} VALUES (?, ?, ?, ?, ?)",
                [r["operation"], r["source"], r["destination"], r["status"], r["error"]],
            )
        return ctx.conn.sql(f"SELECT * FROM {fs_out}")

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "operation": "copy_file",
            "source": "",
            "destination": "",
            "overwrite": False,
            "recursive": True,
            "continue_on_error": False,
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "operation", "type": "select", "label": "Operation",
             "required": True, "tab": "Settings",
             "options": [
                 "copy_file", "move_file", "rename_file", "delete_file",
                 "copy_directory", "move_directory", "delete_directory",
                 "create_directory",
             ]},
            {"name": "source", "type": "text", "label": "Source Path",
             "required": True, "tab": "Settings",
             "placeholder": "/data/incoming/orders.csv"},
            {"name": "destination", "type": "text", "label": "Destination Path",
             "tab": "Settings",
             "placeholder": "/data/processed/orders.csv",
             "description": "Required for copy/move/rename operations."},
            {"name": "overwrite", "type": "boolean", "label": "Overwrite if Exists",
             "default": False, "tab": "Settings"},
            {"name": "recursive", "type": "boolean", "label": "Recursive (directories)",
             "default": True, "tab": "Settings"},
            {"name": "continue_on_error", "type": "boolean", "label": "Continue on Error",
             "default": False, "tab": "Settings"},
        ]


# ─────────────────────────────────────────────────────────
#  EXECUTE SQL TASK
# ─────────────────────────────────────────────────────────

def _run_connection_sql(conn_id: str, sql: str, timeout: int = 60) -> tuple[list[dict], int]:
    """Run a SQL statement against a saved connection.

    Returns ``(rows, affected)`` — ``rows`` is the result set as a list of
    dicts (empty for DML), ``affected`` is the cursor rowcount for DML (or -1
    when a result set was returned). Shared by Execute SQL and the Lookup
    activity so the per-dialect driver matrix lives in ONE place (2026-06-15).

    Raises ``ValueError`` for an unknown connection id / unsupported dialect;
    propagates the driver's own exception on a query error.
    """
    from fpulse.nodes.db_source import _get_connection_config

    cfg = _get_connection_config(conn_id)
    if not cfg:
        raise ValueError(f"connection '{conn_id}' not found")
    kind = (cfg.get("type") or cfg.get("kind") or "").lower()

    def _capture(cur) -> tuple[list[dict], int]:
        if cur.description:
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()], -1
        return [], cur.rowcount

    if kind in ("postgres", "postgresql"):
        import psycopg2
        with psycopg2.connect(
            host=cfg.get("host"), port=int(cfg.get("port", 5432) or 5432),
            dbname=cfg.get("database"), user=cfg.get("username"),
            password=cfg.get("password"), connect_timeout=timeout,
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                return _capture(cur)
    elif kind in ("mysql", "mariadb"):
        import pymysql
        conn = pymysql.connect(
            host=cfg.get("host"), port=int(cfg.get("port", 3306) or 3306),
            db=cfg.get("database"), user=cfg.get("username"),
            password=cfg.get("password"), connect_timeout=timeout,
        )
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                return _capture(cur)
        finally:
            conn.close()
    elif kind == "sqlite":
        import sqlite3
        conn = sqlite3.connect(cfg.get("database") or ":memory:", timeout=timeout)
        try:
            cur = conn.cursor()
            cur.execute(sql)
            rows, affected = _capture(cur)
            conn.commit()
            return rows, affected
        finally:
            conn.close()
    raise ValueError(f"unsupported connection kind '{kind}'")


@register(StepType.EXECUTE_SQL_TASK)
class ExecuteSqlTaskNode(BaseNode):
    """Run arbitrary SQL on a connection — DDL, DML, anonymous blocks.

    Unlike Transform (which is a SELECT against the in-memory data),
    this fires a statement at a real database connection. Returns either
    the rowcount or the result set, depending on `return_mode`.
    """
    display_name = "Execute SQL"
    category = "action"
    description = "Run any SQL statement on a database — CREATE, INSERT, UPDATE, DELETE, etc."

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        conn_id = self.params.get("connection_id", "")
        sql = (self.params.get("sql") or "").strip()
        return_mode = self.params.get("return_mode", "rowcount")  # rowcount | full
        timeout = int(self.params.get("timeout", 60) or 60)

        if not sql:
            raise ValueError("Execute SQL: 'sql' is required")

        # Render {col} placeholders from upstream first row, if any.
        #
        # 2026-05-30 audit (security): we used to do a raw `str.replace`
        # of the upstream value into the SQL string. That's classic SQL
        # injection — a value containing `'; DROP TABLE x; --` would be
        # spliced verbatim into the executed statement.
        #
        # We can't switch to true parameter binding because operators
        # use {col} placeholders for table/column names too, not just
        # values. The mitigation here is the standard string-escape:
        # double single quotes inside the substituted value so the
        # surrounding `'...'` literal stays a literal. Operators who
        # explicitly want an identifier substitution should still review
        # the SQL — this is documented in the UI hint text.
        src = _get_input(ctx, self.params)
        if src is not None:
            ctx.conn.register("__exec_render", src)
            head = ctx.conn.sql("SELECT * FROM __exec_render LIMIT 1").fetchdf()
            if not head.empty:
                row = head.iloc[0].to_dict()
                for col, val in row.items():
                    escaped = str(val).replace("'", "''")
                    sql = sql.replace(f"{{{col}}}", escaped)

        if not conn_id:
            # Run on the in-memory DuckDB instead
            try:
                if return_mode == "full":
                    return ctx.conn.sql(sql)
                ctx.conn.execute(sql)
                return _rows([{"affected": -1, "status": "ok"}], ctx)
            except Exception as exc:
                raise RuntimeError(f"Execute SQL (DuckDB): {exc}") from exc

        # 2026-06-15: the per-dialect driver matrix now lives in the shared
        # _run_connection_sql helper (also used by the Lookup activity).
        try:
            rows, affected = _run_connection_sql(conn_id, sql, timeout)
        except ValueError as exc:
            raise ValueError(f"Execute SQL: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 — driver/query error
            raise RuntimeError(f"Execute SQL: {exc}") from exc
        if return_mode == "full":
            return _rows(rows, ctx) if rows else _empty(ctx)
        return _rows([{"affected": affected, "status": "ok"}], ctx)

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "connection_id": "",
            "sql": "",
            "return_mode": "rowcount",
            "timeout": 60,
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "connection_id", "type": "connection", "label": "Connection",
             "tab": "Settings",
             "description": "Leave empty to run against the in-memory DuckDB."},
            {"name": "sql", "type": "code", "label": "SQL Statement",
             "required": True, "tab": "Settings", "language": "sql",
             "placeholder": "TRUNCATE TABLE staging.events;\nCALL refresh_summary();",
             "description": "Supports {column} placeholders from upstream first row."},
            {"name": "return_mode", "type": "select", "label": "Return Mode",
             "options": ["rowcount", "full"], "default": "rowcount",
             "tab": "Settings",
             "description": "rowcount: number of affected rows. full: result set."},
            {"name": "timeout", "type": "number", "label": "Timeout (seconds)",
             "default": 60, "tab": "Settings"},
        ]


def _rows(rows: list[dict], ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
    """Helper: build a DuckDB relation from a list of dicts."""
    if not rows:
        return _empty(ctx)
    keys = list(rows[0].keys())

    def fmt(v: Any) -> str:
        if v is None:
            return "NULL"
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        return "'" + str(v).replace("'", "''") + "'"

    values_sql = ", ".join("(" + ", ".join(fmt(r.get(k)) for k in keys) + ")" for r in rows)
    # 2026-06-15: name the columns directly in the table alias. The old code
    # renamed DuckDB's positional VALUES columns assuming they're `column0,
    # column1, …`, but this DuckDB version names them `col0, …` — so the rename
    # raised a Binder error. Naming via `AS _t("k1", "k2", …)` is version-proof.
    col_list = ", ".join(f'"{k}"' for k in keys)
    # Per-step temp-table name: the returned relation is lazy over it, so two
    # nodes that both funnel results through _rows() must not share one name
    # (else the second CREATE OR REPLACE rebinds the first node's output).
    out_tbl = ctx.scoped_name("__ce_out")
    ctx.conn.execute(
        f"CREATE OR REPLACE TEMP TABLE {out_tbl} AS "
        f"SELECT * FROM (VALUES {values_sql}) AS _t({col_list})"
    )
    return ctx.conn.sql(f"SELECT * FROM {out_tbl}")
