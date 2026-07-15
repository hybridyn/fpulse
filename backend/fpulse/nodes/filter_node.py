"""Filter node — applies a WHERE condition to filter rows.

Supports two modes:
  - expression: Direct SQL WHERE clause (power users)
  - rules: Structured AND/OR rule groups (visual builder in UI)

Rules mode example:
  rules = [
    {"column": "amount", "op": ">", "value": "100"},
    {"column": "status", "op": "=", "value": "active"},
  ]
  combinator = "AND"  →  amount > 100 AND status = 'active'

Nested groups:
  rule_groups = [
    {"combinator": "AND", "rules": [...]},
    {"combinator": "OR", "rules": [...]},
  ]
  group_combinator = "AND"  →  (group1) AND (group2)
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

# Stage 2.5b: duckdb only used for the execute() return-type annotation.
if TYPE_CHECKING:
    import duckdb

from fpulse.ir.schema import StepType
from fpulse.nodes.base import BaseNode, ExecutionContext
from fpulse.nodes.registry import register


# ── Operators supported in rules mode ──
_OPERATORS = {
    "=": "=", "!=": "!=", "<>": "<>",
    ">": ">", ">=": ">=", "<": "<", "<=": "<=",
    "contains": "LIKE", "not_contains": "NOT LIKE",
    "starts_with": "LIKE", "ends_with": "LIKE",
    "is_null": "IS NULL", "is_not_null": "IS NOT NULL",
    "in": "IN", "not_in": "NOT IN",
    "between": "BETWEEN",
}


def _rule_to_sql(rule: dict) -> str:
    """Convert a single rule dict to a SQL condition fragment."""
    col = rule.get("column", "").strip()
    op = rule.get("op", "=").strip()
    val = rule.get("value", "").strip()

    if not col:
        return "TRUE"

    safe_col = f'"{col}"'

    if op in ("is_null", "is_not_null"):
        return f"{safe_col} {_OPERATORS[op]}"

    if op == "contains":
        return f"{safe_col} LIKE '%{val}%'"
    if op == "not_contains":
        return f"{safe_col} NOT LIKE '%{val}%'"
    if op == "starts_with":
        return f"{safe_col} LIKE '{val}%'"
    if op == "ends_with":
        return f"{safe_col} LIKE '%{val}'"

    if op in ("in", "not_in"):
        items = [v.strip() for v in val.split(",")]
        quoted = ", ".join(f"'{i}'" for i in items)
        keyword = "IN" if op == "in" else "NOT IN"
        return f"{safe_col} {keyword} ({quoted})"

    if op == "between":
        parts = [v.strip() for v in val.split(",")]
        if len(parts) == 2:
            return f"{safe_col} BETWEEN '{parts[0]}' AND '{parts[1]}'"
        return "TRUE"

    # Numeric-safe: try to use unquoted value for numbers
    sql_op = _OPERATORS.get(op, "=")
    try:
        float(val)
        return f"{safe_col} {sql_op} {val}"
    except (ValueError, TypeError):
        return f"{safe_col} {sql_op} '{val}'"


def _rules_to_condition(rules: list[dict], combinator: str = "AND") -> str:
    """Convert a list of rules to a SQL WHERE clause."""
    if not rules:
        return "TRUE"
    parts = [_rule_to_sql(r) for r in rules if r.get("column")]
    if not parts:
        return "TRUE"
    joiner = f" {combinator.upper()} "
    return f"({joiner.join(parts)})"


@register(StepType.FILTER)
class FilterNode(BaseNode):
    """Filter rows using SQL expression or visual rule builder.

    Modes:
      - expression (default): Write a raw SQL WHERE clause
      - rules: Structured rules with column/operator/value and AND/OR combinator

    The rules mode generates SQL automatically and is intended for
    visual rule builders in the UI.
    """
    display_name = "Filter"
    category = "transform"
    description = "Keep only rows that match your condition (build it visually or write SQL)"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        inputs = ctx.get_inputs(self.params.get("_input_step_ids", []))
        if not inputs:
            raise ValueError("Filter node has no input data")

        source = inputs[0]
        mode = self.params.get("mode", "expression")

        if mode == "rules":
            rules = self.params.get("rules", [])
            combinator = self.params.get("combinator", "AND")
            rule_groups = self.params.get("rule_groups", [])

            if rule_groups:
                # Multiple groups combined
                group_parts = []
                for grp in rule_groups:
                    grp_rules = grp.get("rules", [])
                    grp_comb = grp.get("combinator", "AND")
                    group_parts.append(_rules_to_condition(grp_rules, grp_comb))
                group_combinator = self.params.get("group_combinator", "AND")
                condition = f" {group_combinator} ".join(group_parts)
            else:
                condition = _rules_to_condition(rules, combinator)
        else:
            condition = self.params.get("condition", "TRUE")

        if not condition or condition.strip().upper() == "TRUE":
            return source

        filter_input = ctx.register_scoped("__filter_input", source)
        return ctx.conn.sql(f"SELECT * FROM {filter_input} WHERE {condition}")

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "mode": "expression",
            # Empty so the field shows its placeholder hint instead of a literal
            # "column_name" that references no real column (which looked valid
            # but failed at runtime with a binder error). Empty → pass-through.
            "condition": "",
            "rules": [],
            "combinator": "AND",
            "rule_groups": [],
            "group_combinator": "AND",
        }

    @staticmethod
    def expected_output_schema(input_schemas, params):
        # R5: Filter is a row filter — column shape is identical to
        # the single upstream input.
        if not input_schemas:
            return None
        return list(input_schemas[0])

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "mode", "type": "select", "label": "Filter Mode",
             "options": ["expression", "rules"], "default": "expression",
             "description": "expression = SQL WHERE clause, rules = visual builder."},
            # Expression mode
            {"name": "condition", "type": "expression", "label": "Filter Condition",
             "placeholder": "e.g. amount > 100 AND status = 'active'",
             "show_when": {"mode": ["expression"]}},
            # Rules mode
            {"name": "combinator", "type": "select", "label": "Combine Rules With",
             "options": ["AND", "OR"], "default": "AND",
             "show_when": {"mode": ["rules"]}},
            {"name": "rules", "type": "rule_list", "label": "Filter Rules",
             "show_when": {"mode": ["rules"]},
             "description": "Each rule: column, operator (=, !=, >, <, contains, in, is_null, between...), value."},
            {"name": "rule_groups", "type": "rule_groups", "label": "Rule Groups (advanced)",
             "show_when": {"mode": ["rules"]},
             "description": "Multiple groups of rules, each with own AND/OR, combined with group combinator."},
            {"name": "group_combinator", "type": "select", "label": "Combine Groups With",
             "options": ["AND", "OR"], "default": "AND",
             "show_when": {"mode": ["rules"]}},
        ]
