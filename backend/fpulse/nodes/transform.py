"""Transform node — apply a SQL expression to transform data."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

# Stage 2.5b: duckdb is referenced ONLY in the return-type annotation
# below. With `from __future__ import annotations` the annotation is a
# string at runtime, so the runtime import is unnecessary. TYPE_CHECKING
# keeps mypy / pyright happy without forcing an import on `import
# fpulse.nodes.transform`. The actual duckdb work happens via ctx.conn,
# whose type is owned by ExecutionContext.
if TYPE_CHECKING:
    import duckdb

from fpulse.ir.schema import StepType
from fpulse.nodes.base import BaseNode, ExecutionContext
from fpulse.nodes.registry import register


@register(StepType.TRANSFORM)
class TransformNode(BaseNode):
    display_name = "SQL Transform"
    category = "transform"
    description = "Reshape your data by writing a SQL query (use source_table to refer to upstream data)"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        input_ids = list(self.params.get("_input_step_ids", []))
        input_ports = self.params.get("_input_step_ports")
        inputs = ctx.get_routed_inputs(input_ids, input_ports)
        if not inputs:
            raise ValueError("Transform node has no input data")

        source = inputs[0]
        # 2026-05-30 audit: was `self.params["expression"]` — a missing
        # key bubbled up as `KeyError: 'expression'`, which the run-card
        # surfaces as an opaque "KeyError" instead of a meaningful
        # "configure your SQL" prompt. Use .get() + an explicit check.
        expression = (self.params.get("expression") or "").strip()
        if not expression:
            raise ValueError(
                "Transform node has no SQL expression. Open the node and "
                "paste a SELECT statement that reads from `source_table`."
            )

        # The first directly-connected input is the primary table, exposed
        # as both `source_table` and `input`.
        ctx.conn.register("source_table", source)
        ctx.conn.register("input", source)

        # 2026-06-10 node-contract hardening: register ONLY the directly
        # connected inputs as named tables — by sanitized node label and by
        # step-id. This previously iterated `ctx._results` (EVERY executed
        # ancestor), which let SQL silently reference grandparent nodes that
        # were not wired into this Transform — hidden cross-node dependencies
        # that surprised users and broke when an unrelated upstream changed.
        # Multi-input now means exactly the edges drawn on the canvas.
        import re
        labels = self.params.get("_node_labels", {})
        # Per-edge user aliases ({from_step_id: alias}) stamped by the executor's
        # _build_input_map. When set, each incoming relation is also registered
        # under the (sanitized) alias so SQL can read a stable, user-named table
        # instead of the upstream label.
        aliases = self.params.get("_input_step_aliases", {}) or {}
        _port_by_step: dict = {}
        for _e in (input_ports or []):
            try:
                _port_by_step.setdefault(_e[0], _e[1])
            except Exception:
                pass

        def _register_as(name: str, relation) -> None:
            safe = re.sub(r"[^a-z0-9_]", "_", str(name).lower()).strip("_")
            if safe and safe not in ("source_table", "input"):
                try:
                    ctx.conn.register(safe, relation)
                except Exception:
                    pass

        for step_id in input_ids:
            relation = ctx._results.get(step_id)
            if relation is None:
                continue
            relation = ctx.route_relation(relation, _port_by_step.get(step_id, "output"))
            safe_id = str(step_id).replace("-", "_")
            try:
                ctx.conn.register(safe_id, relation)
            except Exception:
                pass
            alias = str(aliases.get(step_id) or "").strip()
            if alias:
                _register_as(alias, relation)
            label = labels.get(step_id)
            if label:
                _register_as(label, relation)

        # Materialize the result into a per-step temp table before returning.
        # The SQL Transform node deliberately exposes `source_table` / `input`
        # (and upstream labels) as FIXED, user-facing table names — users author
        # arbitrary SQL against them, so we can't rename them per step like the
        # internal-view nodes do. If we returned the lazy `ctx.conn.sql(expression)`
        # (whose definition references `source_table`), a SECOND SQL Transform
        # downstream would re-register `source_table` with a relation that
        # transitively references itself → DuckDB "infinite recursion detected".
        # Materializing into a step-scoped temp table breaks that chain: the
        # returned relation reads the temp table, not `source_table`.
        out_tbl = ctx.scoped_name("__transform_out")
        ctx.conn.execute(f"CREATE OR REPLACE TEMP TABLE {out_tbl} AS {expression}")
        return ctx.conn.sql(f"SELECT * FROM {out_tbl}")

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"expression": "SELECT *, CURRENT_DATE AS processed_at FROM source_table"}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "expression", "type": "sql", "label": "SQL Expression", "required": True,
             "placeholder": "SELECT *, col1 + col2 AS total FROM source_table"},
        ]
