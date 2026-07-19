"""Join node — join two datasets with enterprise join types.

Supports:
  - Standard: INNER, LEFT, RIGHT, FULL OUTER
  - Advanced: SEMI, ANTI, CROSS
  - Key modes:
    - same_key: both sides share the same column name(s)
    - mapped_keys: left/right columns have different names
    - custom: raw SQL ON clause for inequality or complex joins
  - Column selection: choose which columns to include from each side
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

# Stage 2.5b: duckdb only used for the execute() return-type annotation.
if TYPE_CHECKING:
    import duckdb

from fpulse.ir.schema import StepType
from fpulse.nodes.base import BaseNode, ExecutionContext
from fpulse.nodes.registry import register


@register(StepType.JOIN)
class JoinNode(BaseNode):
    """Join two datasets with full enterprise join support."""
    display_name = "Join"
    category = "transform"
    description = "Combine two datasets by matching a key column (inner / left / right / full / etc.)"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        input_ids = self.params.get("_input_step_ids", [])
        if len(input_ids) < 2:
            raise ValueError("Join node requires exactly 2 inputs")

        # 2026-06-11 (node-audit): explicit side assignment. "Left = the
        # first edge you drew" made edge order carry semantics — deleting
        # and re-drawing a connection silently swapped the join sides.
        # `left_input_id` pins the left side by step id; the swap button
        # in the UI just flips it. Legacy pipelines without the param
        # keep the old first-edge behavior.
        left_id = self.params.get("left_input_id") or ""
        left_idx = input_ids.index(left_id) if left_id in input_ids else 0
        right_idx = 1 if left_idx == 0 else 0

        left = ctx.get_input(input_ids[left_idx])
        right = ctx.get_input(input_ids[right_idx])
        if left is None or right is None:
            raise ValueError("Join node: one or both inputs missing")

        join_type = self.params.get("join_type", "INNER").upper()
        key_mode = self.params.get("key_mode", "same_key")

        # Scope the internal view names per step so a pipeline with TWO join
        # nodes doesn't hit DuckDB's "infinite recursion detected" binder error
        # (the second join would re-register a shared view name whose relation
        # references itself). __join_left / __join_right are ALSO documented,
        # user-facing table names for custom ON clauses and column selects
        # (see param_schema placeholders), so translate any user-supplied
        # reference to the canonical names onto the actual scoped names.
        jl = ctx.register_scoped("__join_left", left)
        jr = ctx.register_scoped("__join_right", right)

        def _sub(sql_fragment: str) -> str:
            return sql_fragment.replace("__join_left", jl).replace("__join_right", jr)

        # Build ON clause based on key mode
        if join_type == "CROSS":
            on_clause = ""
        elif key_mode == "custom":
            on_clause = _sub(self.params.get("custom_on", "TRUE"))
        elif key_mode == "mapped_keys":
            key_pairs = self.params.get("key_pairs", [])
            if not key_pairs:
                raise ValueError("Join: mapped_keys mode requires at least one key pair")
            parts = []
            for pair in key_pairs:
                left_col = pair.get("left", "")
                right_col = pair.get("right", "")
                op = pair.get("operator", "=")
                if left_col and right_col:
                    parts.append(f'{jl}."{left_col}" {op} {jr}."{right_col}"')
            on_clause = " AND ".join(parts) if parts else "TRUE"
        else:
            # same_key mode
            join_key = self.params.get("join_key", [])
            if isinstance(join_key, str):
                join_key = [k.strip() for k in join_key.split(",") if k.strip()]
            if not join_key:
                raise ValueError("Join: at least one join key is required")
            on_clause = " AND ".join(
                f'{jl}."{k}" = {jr}."{k}"' for k in join_key
            )

        # Build SQL based on join type
        if join_type == "SEMI":
            sql = (
                f"SELECT {jl}.* FROM {jl} "
                f"WHERE EXISTS (SELECT 1 FROM {jr} WHERE {on_clause})"
            )
        elif join_type == "ANTI":
            sql = (
                f"SELECT {jl}.* FROM {jl} "
                f"WHERE NOT EXISTS (SELECT 1 FROM {jr} WHERE {on_clause})"
            )
        elif join_type == "CROSS":
            select_clause = self._default_projection(left, right, key_mode, join_type, jl, jr)
            sql = f"SELECT {select_clause} FROM {jl} CROSS JOIN {jr}"
        else:
            join_keyword = join_type
            if join_type == "FULL":
                join_keyword = "FULL OUTER"

            # Column selection
            select_left = self.params.get("select_left", "").strip()
            select_right = self.params.get("select_right", "").strip()

            if select_left or select_right:
                left_cols = _sub(select_left) if select_left else f"{jl}.*"
                right_cols = _sub(select_right) if select_right else f"{jr}.*"
                select_clause = f"{left_cols}, {right_cols}"
            else:
                # 2026-06-15 (node-audit): default projection used to be
                # `SELECT *`, which emits TWO columns of the same name
                # whenever both inputs share one (and ALWAYS for same_key
                # join keys). DuckDB keeps both, so downstream references to
                # that name were ambiguous. Build an explicit, collision-safe
                # projection instead — see _default_projection.
                select_clause = self._default_projection(left, right, key_mode, join_type, jl, jr)

            sql = (
                f"SELECT {select_clause} FROM {jl} "
                f"{join_keyword} JOIN {jr} ON {on_clause}"
            )

        return ctx.conn.sql(sql)

    def _default_projection(self, left, right, key_mode: str, join_type: str, jl: str, jr: str) -> str:
        """Build a collision-safe `SELECT` list for the default projection.

        Rules:
          * Keep every LEFT column as-is.
          * For ``same_key`` joins, the join keys exist on both sides with the
            same name — emit a single ``COALESCE(left.k, right.k) AS k`` so the
            key survives RIGHT/FULL rows where the left side is NULL, and drop
            the right copy.
          * Any remaining RIGHT column whose name clashes with a kept column is
            aliased with ``dup_column_suffix`` (default ``_right``).
        Non-colliding joins produce the same columns, in the same order
        (left then right), as the old ``SELECT *`` did.
        """
        suffix = (self.params.get("dup_column_suffix") or "_right").strip() or "_right"
        left_names = list(left.columns)
        right_names = list(right.columns)

        drop_right: set[str] = set()
        if key_mode == "same_key" and join_type != "CROSS":
            jk = self.params.get("join_key", [])
            if isinstance(jk, str):
                jk = [k.strip() for k in jk.split(",") if k.strip()]
            drop_right = {k for k in jk if k in left_names and k in right_names}

        parts: list[str] = []
        taken: set[str] = set()
        for c in left_names:
            if c in drop_right:
                parts.append(f'COALESCE({jl}."{c}", {jr}."{c}") AS "{c}"')
            else:
                parts.append(f'{jl}."{c}"')
            taken.add(c)
        for c in right_names:
            if c in drop_right:
                continue
            if c in taken:
                alias = f"{c}{suffix}"
                n = 2
                while alias in taken:
                    alias = f"{c}{suffix}{n}"
                    n += 1
                parts.append(f'{jr}."{c}" AS "{alias}"')
                taken.add(alias)
            else:
                parts.append(f'{jr}."{c}"')
                taken.add(c)
        return ", ".join(parts)

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "join_key": [],
            "join_type": "INNER",
            "key_mode": "same_key",
            "key_pairs": [],
            "custom_on": "",
            "select_left": "",
            "select_right": "",
            "left_input_id": "",
            "dup_column_suffix": "_right",
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "join_type", "type": "select", "label": "Join Type",
             "options": ["INNER", "LEFT", "RIGHT", "FULL", "SEMI", "ANTI", "CROSS"],
             "default": "INNER", "tab": "Join",
             "description": "SEMI = rows from left that match right, ANTI = rows from left that don't match right."},
            {"name": "key_mode", "type": "select", "label": "Key Mode",
             "options": ["same_key", "mapped_keys", "custom"], "default": "same_key",
             "tab": "Join",
             "description": "same_key = same column names, mapped_keys = different names, custom = SQL ON clause."},
            # same_key mode
            {"name": "join_key", "type": "column_list", "label": "Join Key Columns",
             "tab": "Join",
             "show_when": {"key_mode": ["same_key"]},
             "description": "Columns that exist in both inputs with the same name."},
            # mapped_keys mode
            {"name": "key_pairs", "type": "key_pair_list", "label": "Key Pairs (left → right)",
             "tab": "Join",
             "show_when": {"key_mode": ["mapped_keys"]},
             "description": "Each pair: {left: 'col_a', right: 'col_b', operator: '='}. Supports =, >, <, >=, <=, !=."},
            # custom mode
            {"name": "custom_on", "type": "expression", "label": "Custom ON Clause",
             "tab": "Join",
             "show_when": {"key_mode": ["custom"]},
             "placeholder": '__join_left."date" BETWEEN __join_right."start" AND __join_right."end"',
             "description": "Full SQL ON clause. Tables are __join_left and __join_right."},
            # Column selection
            {"name": "select_left", "type": "text", "label": "Left Columns",
             "tab": "Columns",
             "placeholder": '__join_left."id", __join_left."name"',
             "description": "Specific columns from left side. Empty = all columns."},
            {"name": "select_right", "type": "text", "label": "Right Columns",
             "tab": "Columns",
             "placeholder": '__join_right."amount", __join_right."date"',
             "description": "Specific columns from right side. Empty = all columns."},
            {"name": "dup_column_suffix", "type": "text", "label": "Duplicate Column Suffix",
             "tab": "Columns", "default": "_right",
             "description": "When both sides share a non-key column name, the right "
                            "copy is renamed with this suffix (e.g. name → name_right). "
                            "Only applies to the default (all-columns) projection."},
        ]
