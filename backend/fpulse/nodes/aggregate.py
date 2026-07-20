"""Aggregate node — GROUP BY with aggregate functions.

Enterprise features:
  - All standard functions: SUM, AVG, MIN, MAX, COUNT, COUNT DISTINCT
  - Statistical: MEDIAN, STDDEV, VARIANCE, PERCENTILE_CONT
  - List: STRING_AGG, ARRAY_AGG, FIRST, LAST
  - Custom SQL expressions as aggregates
  - HAVING clause for post-aggregation filtering
  - ORDER BY on the aggregated result
  - No GROUP BY = global aggregation across all rows
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

# Stage 2.5b: duckdb only used for the execute() return-type annotation.
if TYPE_CHECKING:
    import duckdb

from fpulse.ir.schema import StepType
from fpulse.nodes.base import BaseNode, ExecutionContext
from fpulse.nodes.registry import register


# Functions supported in the visual aggregate builder
_SUPPORTED_FUNCTIONS = {
    "COUNT", "COUNT_DISTINCT", "SUM", "AVG", "MIN", "MAX",
    "MEDIAN", "STDDEV", "STDDEV_POP", "VARIANCE", "VAR_POP",
    "FIRST", "LAST", "STRING_AGG", "ARRAY_AGG",
    "PERCENTILE_CONT", "PERCENTILE_DISC",
    "CUSTOM",
}


@register(StepType.AGGREGATE)
class AggregateNode(BaseNode):
    """Aggregate with GROUP BY, HAVING, and advanced functions."""
    display_name = "Aggregate"
    category = "transform"
    description = "Group rows and calculate sums, averages, counts, and other totals"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        inputs = ctx.get_inputs(self.params.get("_input_step_ids", []))
        if not inputs:
            raise ValueError("Aggregate node has no input data")

        source = inputs[0]
        group_by = self.params.get("group_by", [])
        if isinstance(group_by, str):
            group_by = [g.strip() for g in group_by.split(",") if g.strip()]

        functions = self.params.get("functions", [])
        # Normalize input shapes. The canonical form is a list of dicts
        # ({"column","function","alias",...}), which the UI always emits.
        # But API callers + older saved pipelines commonly use the
        # natural shorthands, and a reasonable input must never crash the
        # node with `'str' object has no attribute 'get'`:
        #   * dict shorthand   {"price": "sum"}  -> [{"column":"price","function":"sum"}]
        #   * bare string      "price"           -> [{"column":"price","function":"COUNT"}]
        # (group_by already accepts its string shorthand above, line ~50.)
        if isinstance(functions, dict):
            functions = [
                {"column": c, "function": fn}
                for c, fn in functions.items()
            ]
        normalized: list[dict] = []
        for f in (functions or []):
            if isinstance(f, str):
                normalized.append({"column": f, "function": "COUNT"})
            elif isinstance(f, dict):
                normalized.append(f)
            # silently skip anything else (None, numbers) rather than crash
        functions = normalized

        having = self.params.get("having", "").strip()
        order_by = self.params.get("order_by", "").strip()

        agg_input = ctx.register_scoped("__agg_input", source)

        group_cols = ", ".join(f'"{g}"' for g in group_by) if group_by else ""

        # Build aggregate expressions
        agg_exprs = []
        for f in functions:
            col = f.get("column", "*")
            func = f.get("function", "COUNT").upper()
            alias = f.get("alias", "")

            if func == "CUSTOM":
                # Raw SQL expression
                expr = f.get("expression", "")
                if expr:
                    alias = alias or "custom_agg"
                    agg_exprs.append(f'{expr} AS "{alias}"')
                continue

            if not alias:
                alias = f"{func.lower()}_{col}" if col != "*" else f"{func.lower()}_all"

            if func == "COUNT" and col == "*":
                agg_exprs.append(f'COUNT(*) AS "{alias}"')
            elif func == "COUNT_DISTINCT":
                agg_exprs.append(f'COUNT(DISTINCT "{col}") AS "{alias}"')
            elif func == "MEDIAN":
                agg_exprs.append(f'MEDIAN("{col}") AS "{alias}"')
            elif func == "PERCENTILE_CONT":
                pct = float(f.get("percentile", 0.5))
                agg_exprs.append(
                    f'PERCENTILE_CONT({pct}) WITHIN GROUP (ORDER BY "{col}") AS "{alias}"'
                )
            elif func == "PERCENTILE_DISC":
                pct = float(f.get("percentile", 0.5))
                agg_exprs.append(
                    f'PERCENTILE_DISC({pct}) WITHIN GROUP (ORDER BY "{col}") AS "{alias}"'
                )
            elif func == "STRING_AGG":
                sep = f.get("separator", ", ")
                agg_exprs.append(f"STRING_AGG(\"{col}\", '{sep}') AS \"{alias}\"")
            elif func == "FIRST":
                agg_exprs.append(f'FIRST("{col}") AS "{alias}"')
            elif func == "LAST":
                agg_exprs.append(f'LAST("{col}") AS "{alias}"')
            else:
                # Standard: SUM, AVG, MIN, MAX, STDDEV, VARIANCE, etc.
                agg_exprs.append(f'{func}("{col}") AS "{alias}"')

        if not agg_exprs:
            agg_exprs = ['COUNT(*) AS "count"']

        # Build SQL
        select_parts = ([group_cols] if group_cols else []) + agg_exprs
        sql = f"SELECT {', '.join(select_parts)} FROM {agg_input}"
        if group_cols:
            sql += f" GROUP BY {group_cols}"
        if having:
            sql += f" HAVING {having}"
        if order_by:
            sql += f" ORDER BY {order_by}"

        return ctx.conn.sql(sql)

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "group_by": [],
            "functions": [{"column": "*", "function": "COUNT", "alias": "count"}],
            "having": "",
            "order_by": "",
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "group_by", "type": "column_list", "label": "Group By Columns",
             "tab": "Aggregation",
             "description": "Columns to group by. Leave empty for global aggregation."},
            {"name": "functions", "type": "aggregate_list", "label": "Aggregate Functions",
             "required": True, "tab": "Aggregation",
             "description": (
                 "Functions: COUNT, COUNT_DISTINCT, SUM, AVG, MIN, MAX, "
                 "MEDIAN, STDDEV, VARIANCE, PERCENTILE_CONT, STRING_AGG, "
                 "FIRST, LAST, ARRAY_AGG, or CUSTOM (raw SQL)."
             )},
            {"name": "having", "type": "expression", "label": "HAVING Clause",
             "tab": "Filter",
             "placeholder": 'e.g. COUNT(*) > 10 OR SUM("amount") > 1000',
             "description": "Filter groups after aggregation (like WHERE but for groups)."},
            {"name": "order_by", "type": "text", "label": "Order By",
             "tab": "Filter",
             "placeholder": 'e.g. count DESC, "total_amount" ASC',
             "description": "Sort the aggregated results."},
        ]
