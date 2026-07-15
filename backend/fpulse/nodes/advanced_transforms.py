"""Advanced transform nodes — Flatten/Explode + Materialize.

These fill two critical gaps identified by architecture review:

1. **Flatten / Explode** — JSON blobs are everywhere in modern data.
   API sources return nested objects, NoSQL exports have arrays-of-structs.
   This node unnests them into rows DuckDB can query with normal SQL.

2. **Materialize** — Saves intermediate results to a temp DuckDB table.
   Downstream nodes read from the snapshot instead of re-executing the
   entire upstream chain.  Huge perf win for diamond-shaped DAGs and
   iterative development (change one downstream node without re-running
   the expensive source).
"""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

# Stage 2.5b: duckdb only used for the execute() return-type annotation.
if TYPE_CHECKING:
    import duckdb

from fpulse.ir.schema import StepType
from fpulse.nodes.base import BaseNode, ExecutionContext
from fpulse.nodes.registry import register

logger = logging.getLogger(__name__)


# ── Flatten / Explode ──────────────────────────────────────────────────

@register(StepType.FLATTEN_EXPLODE)
class FlattenExplodeNode(BaseNode):
    """Flatten nested JSON structs or explode array columns into rows.

    Modes:
      - **flatten**: Expand a STRUCT/JSON column into top-level columns.
        ``{"user": {"name": "A", "age": 30}}`` → ``user_name, user_age``
      - **explode**: Unnest a LIST/ARRAY column so each element becomes a row.
        ``[1, 2, 3]`` in column ``tags`` → 3 rows, one per tag.

    Under the hood this uses DuckDB's ``UNNEST()`` for arrays and
    ``struct.*`` expansion for structs — both are zero-copy in DuckDB's
    columnar engine, so even large nested datasets stay fast.
    """

    display_name = "Flatten"
    category = "transform"
    description = "Expand nested data — turn arrays into separate rows, or unpack nested fields into columns"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        inputs = ctx.get_inputs(self._get_input_ids(ctx))
        if not inputs:
            raise ValueError("Flatten / Explode: no input connected")
        rel = inputs[0]

        mode = self.params.get("mode", "flatten")
        column = self.params.get("column", "")
        prefix = self.params.get("prefix", "")
        keep_original = self.params.get("keep_original", False)

        if not column:
            raise ValueError(
                "Flatten / Explode: 'column' parameter is required. "
                "Select which column contains nested data."
            )

        # Register input as a temp view so we can query it with SQL
        view_name = "__flatten_input"
        ctx.conn.register(view_name, rel)

        columns = rel.columns
        # 2026-06-11 (flatten/explode parity): dot-notation lets `column`
        # address a nested array inside a struct — e.g. "data.items". The
        # first segment is the real top-level column; the rest is struct
        # field access compiled to "data"."items".
        path_parts = column.split(".")
        base_col = path_parts[0]
        is_nested = len(path_parts) > 1
        # Bracket access for nested struct fields is unambiguous in DuckDB
        # ("data"['items']); dotted quotes ("data"."items") can be misread
        # as schema.table.column.
        col_ref = f'"{base_col}"' + "".join(f"['{p}']" for p in path_parts[1:])
        leaf_name = path_parts[-1]
        if is_nested and mode != "explode":
            raise ValueError(
                "Flatten / Explode: dotted column paths (e.g. data.items) are "
                "supported in explode mode only."
            )
        if base_col not in columns:
            raise ValueError(
                f"Flatten / Explode: column '{base_col}' not found. "
                f"Available: {', '.join(columns)}"
            )

        if mode == "explode":
            # UNNEST the array column — each element becomes a row
            other_cols = [f'"{c}"' for c in columns if c != base_col]
            other_select = ", ".join(other_cols) + ", " if other_cols else ""
            exploded_alias = f"{prefix}{leaf_name}" if prefix else leaf_name

            # 2026-06-11 (node-audit): explicit type guard + outer-explode
            # and index options. Explode is for LIST/ARRAY columns; a
            # scalar column produces a confusing engine error, and a
            # STRUCT column means the user wanted Flatten — say so. We can
            # only introspect a TOP-LEVEL column's type; for a nested path
            # (data.items) we trust UNNEST + the fallback to surface errors.
            col_type = ""
            is_list = True
            if not is_nested:
                col_type = str(dict(zip(rel.columns, rel.types)).get(base_col, "")).upper()
                is_list = col_type.endswith("[]") or col_type.startswith("LIST")
                is_jsonish = col_type in ("JSON", "VARCHAR")
                if not is_list and not is_jsonish:
                    hint = (
                        " That column is a STRUCT — use mode='flatten' to expand it into columns."
                        if col_type.startswith("STRUCT")
                        else f" That column is {col_type or 'unknown'} — explode needs an ARRAY/LIST column."
                    )
                    raise ValueError(
                        f"Flatten / Explode: cannot explode column '{column}'.{hint}"
                    )

            # keep_empty (outer explode): a NULL/empty array would drop
            # the row entirely under plain UNNEST. Wrapping in [NULL]
            # keeps the row with a NULL element — the user chooses.
            keep_empty = bool(self.params.get("keep_empty", False))
            add_index = bool(self.params.get("add_index", False))
            list_expr = col_ref
            if keep_empty and is_list:
                list_expr = (
                    f'(CASE WHEN {col_ref} IS NULL OR len({col_ref}) = 0 '
                    f'THEN [NULL] ELSE {col_ref} END)'
                )
            # Parallel UNNESTs zip by position in DuckDB — the second one
            # emits a 1-based element index alongside each element.
            index_select = (
                f', UNNEST(range(1, len({list_expr}) + 1)) AS "{exploded_alias}_index"'
                if add_index and is_list
                else ""
            )

            sql = (
                f'SELECT {other_select}'
                f'UNNEST({list_expr}) AS "{exploded_alias}"{index_select} '
                f'FROM {view_name}'
            )
            try:
                result = ctx.conn.sql(sql)
            except Exception as exc:
                if keep_empty or add_index:
                    # The options only make sense for true LIST columns —
                    # don't silently fall back to a path that ignores them.
                    raise ValueError(
                        f"Flatten / Explode: could not explode column '{column}' with "
                        f"keep-empty/index options — those need a LIST-typed column "
                        f"(got {col_type or 'unknown'})."
                    ) from exc
                # Fallback: try with LATERAL FLATTEN for deeply nested
                logger.warning("flatten_explode: UNNEST failed (%s), trying lateral", exc)
                sql_fallback = (
                    f'SELECT {other_select}f.value AS "{exploded_alias}" '
                    f'FROM {view_name}, '
                    f'LATERAL FLATTEN(input => {col_ref}) f'
                )
                try:
                    result = ctx.conn.sql(sql_fallback)
                except Exception:
                    raise ValueError(
                        f"Flatten / Explode: could not explode column '{column}'. "
                        f"Ensure it contains an array/list type."
                    ) from exc
            return result

        else:  # flatten (struct expansion)
            # Use struct.* to expand struct fields into columns
            pfx = f"{prefix}_" if prefix else f"{column}_"
            other_cols = [f'"{c}"' for c in columns if c != column]
            other_select = ", ".join(other_cols) + ", " if other_cols else ""

            # Try struct expansion: SELECT other_cols, col.* FROM ...
            try:
                # First, get struct field names
                probe_sql = f'SELECT "{column}" FROM {view_name} LIMIT 1'
                probe = ctx.conn.sql(probe_sql)
                # DuckDB struct columns show child columns via describe
                struct_cols_sql = (
                    f'SELECT * FROM ('
                    f'SELECT "{column}".* FROM {view_name} LIMIT 0'
                    f')'
                )
                struct_probe = ctx.conn.sql(struct_cols_sql)
                struct_fields = struct_probe.columns

                # Build renamed struct expansion
                renamed = ", ".join(
                    f'"{column}"."{f}" AS "{pfx}{f}"'
                    for f in struct_fields
                )
                keep_clause = f', "{column}"' if keep_original else ""
                sql = f'SELECT {other_select}{renamed}{keep_clause} FROM {view_name}'
                return ctx.conn.sql(sql)

            except Exception as exc:
                # Fallback: try json_extract for JSON string columns
                logger.warning("flatten_explode: struct expansion failed (%s), trying JSON", exc)
                try:
                    # Attempt to auto-extract JSON keys
                    key_sql = (
                        f"SELECT DISTINCT unnest(json_keys(\"{column}\")) AS k "
                        f"FROM {view_name} "
                        f"WHERE \"{column}\" IS NOT NULL LIMIT 50"
                    )
                    keys = [row[0] for row in ctx.conn.sql(key_sql).fetchall()]
                    if not keys:
                        raise ValueError("No JSON keys found")

                    extracted = ", ".join(
                        f"json_extract_string(\"{column}\", '$.{k}') AS \"{pfx}{k}\""
                        for k in keys
                    )
                    keep_clause = f', "{column}"' if keep_original else ""
                    sql = f'SELECT {other_select}{extracted}{keep_clause} FROM {view_name}'
                    return ctx.conn.sql(sql)
                except Exception:
                    raise ValueError(
                        f"Flatten / Explode: could not flatten column '{column}'. "
                        f"Ensure it contains a STRUCT or JSON type."
                    ) from exc

    def _get_input_ids(self, ctx: ExecutionContext) -> list[str]:
        """Get input step IDs — the wired inputs, not every executed ancestor.

        2026-06-11: prefer `_input_step_ids` (the edges the user drew).
        The old all-results fallback grabbed the FIRST executed node in
        the whole run, which in a multi-branch DAG isn't necessarily this
        node's upstream — same hidden-dependency class the Transform node
        was cured of on 2026-06-10. The fallback remains for legacy
        contexts that never injected the param.
        """
        ids = self.params.get("_input_step_ids")
        if ids:
            return list(ids)
        return list(ctx._results.keys())

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "mode": "flatten", "column": "", "prefix": "", "keep_original": False,
            "keep_empty": False, "add_index": False,
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "mode", "type": "select", "label": "Mode",
             "options": ["flatten", "explode"], "default": "flatten",
             "description": "flatten = expand struct/JSON fields into columns. explode = unnest array into rows."},
            {"name": "column", "type": "text", "label": "Column",
             "required": True, "placeholder": "data",
             "description": "Column containing nested data (struct, JSON, or array)"},
            {"name": "prefix", "type": "text", "label": "Column Prefix",
             "placeholder": "user_",
             "description": "Prefix for expanded column names (default: original column name)"},
            {"name": "keep_original", "type": "boolean", "label": "Keep Original Column",
             "default": False,
             "description": "Keep the original nested column alongside expanded columns"},
            {"name": "keep_empty", "type": "boolean", "label": "Keep Empty Arrays",
             "default": False, "show_when": {"mode": ["explode"]},
             "description": "Keep rows whose array is NULL/empty (element becomes NULL) instead of dropping them"},
            {"name": "add_index", "type": "boolean", "label": "Add Index Column",
             "default": False, "show_when": {"mode": ["explode"]},
             "description": "Add <column>_index with each element's 1-based position"},
        ]


# ── Materialize / Cache ───────────────────────────────────────────────

@register(StepType.MATERIALIZE)
class MaterializeNode(BaseNode):
    """Save intermediate results to a named temp table.

    This is a **checkpoint node** — it evaluates all upstream, writes the
    result into a DuckDB temp table, and returns a reference to that table.
    Downstream nodes query the snapshot instead of re-executing the entire
    upstream DAG.

    Why this matters:
      - Diamond DAGs: if two branches read the same upstream, without
        materialize both execute it independently.  With materialize,
        the upstream runs once and both branches read the cached table.
      - Iterative dev: change a downstream filter and re-run — the
        materialized checkpoint skips the expensive source/transform.
      - Debugging: the temp table persists for the connection lifetime,
        so you can inspect it in the execution log.

    The table name is auto-generated from the node ID to avoid collisions
    between multiple materialize nodes in the same pipeline.
    """

    display_name = "Materialize"
    category = "transform"
    description = "Save intermediate results so the next steps run faster (and re-runs use the cache)"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        inputs = ctx.get_inputs(self._get_input_ids(ctx))
        if not inputs:
            raise ValueError("Materialize: no input connected")
        rel = inputs[0]

        # Generate a safe table name from the node label or a default
        table_name = self.params.get("table_name", "").strip()
        auto_name = not table_name
        if not table_name:
            # Auto-generate from node context
            table_name = "__materialized_cache"

        # Sanitize: only allow alphanumeric + underscore
        safe_name = "".join(c if c.isalnum() or c == "_" else "_" for c in table_name)
        if not safe_name:
            safe_name = "__materialized_cache"
            auto_name = True

        # Auto-generated names are scoped per step so two Materialize nodes
        # using the default don't clobber each other's cache table (the
        # returned relation reads this table lazily). An explicit user-supplied
        # name is respected as-is — an intentional, shared cache handle.
        if auto_name:
            safe_name = ctx.scoped_name(safe_name)

        # Write to temp table
        mat_input = ctx.register_scoped("__mat_input", rel)
        ctx.conn.execute(
            f"CREATE OR REPLACE TEMP TABLE {safe_name} AS "
            f"SELECT * FROM {mat_input}"
        )

        row_count = ctx.conn.sql(f"SELECT count(*) FROM {safe_name}").fetchone()[0]
        logger.info(
            "materialize: cached %s rows into temp table '%s'",
            f"{row_count:,}", safe_name,
        )

        return ctx.conn.sql(f"SELECT * FROM {safe_name}")

    def _get_input_ids(self, ctx: ExecutionContext) -> list[str]:
        return list(ctx._results.keys())

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"table_name": ""}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "table_name", "type": "text", "label": "Cache Table Name",
             "placeholder": "orders_cleaned",
             "description": "Name for the temp table. Leave blank for auto-generated name."},
        ]
