"""Quality / Shape primitives — Sprint B.

Three reusable nodes that any pipeline (REST, JDBC, CDC, file, vector) can chain
without needing connector-specific code:

- DataQualityNode: declarative row-level rules → pass rows downstream, optionally
  capture failed rows for a dead-letter sink.
- UpsertNode: idempotent merge into a relation by key columns. Rows with the
  same key are collapsed (latest wins per `order_by`), so re-running the
  pipeline never produces duplicates.
- SchemaMapperNode: source-to-target field mapping with type coercion. Lets a
  manifest-driven SaaS source feed any downstream sink without writing SQL.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

# Stage 2.5b: duckdb only used for return-type annotations on node
# execute() methods. `from __future__ import annotations` keeps those
# as strings at runtime, so the module import is unnecessary.
if TYPE_CHECKING:
    import duckdb

from fpulse.ir.schema import StepType
from fpulse.nodes.base import BaseNode, ExecutionContext
from fpulse.nodes.registry import register


# 2026-05-30: shared empty-profile sentinel so the three short-circuit
# paths (zero rows / no columns after filter / fallback when DuckDB
# rejects a min/max) all emit the same column shape. Adding a column
# to the profile means editing exactly one string.
_PROFILE_EMPTY_SQL = (
    "SELECT "
    "  CAST(NULL AS VARCHAR) AS column, "
    "  CAST(NULL AS VARCHAR) AS data_type, "
    "  CAST(0 AS BIGINT) AS row_count, "
    "  CAST(0 AS BIGINT) AS null_count, "
    "  CAST(0.0 AS DOUBLE) AS null_pct, "
    "  CAST(0 AS BIGINT) AS distinct_count, "
    "  CAST(0.0 AS DOUBLE) AS distinct_pct, "
    "  CAST(NULL AS VARCHAR) AS min_value, "
    "  CAST(NULL AS VARCHAR) AS max_value, "
    "  CAST(NULL AS VARCHAR) AS top_value, "
    "  CAST(0 AS BIGINT) AS top_value_count, "
    "  CAST(NULL AS DOUBLE) AS mean_value, "
    "  CAST(NULL AS DOUBLE) AS median_value, "
    "  CAST(NULL AS DOUBLE) AS stddev_value, "
    "  CAST(NULL AS DOUBLE) AS avg_length, "
    "  CAST(NULL AS BIGINT) AS max_length "
    "WHERE FALSE"
)


# ─────────────────────────────────────────────────────────────────────────────
# Data Quality
# ─────────────────────────────────────────────────────────────────────────────

_DQ_OPS = {
    "not_null":   lambda col, _v: f'"{col}" IS NOT NULL',
    "is_null":    lambda col, _v: f'"{col}" IS NULL',
    "eq":         lambda col, v: f'"{col}" = {_lit(v)}',
    "ne":         lambda col, v: f'"{col}" <> {_lit(v)}',
    "gt":         lambda col, v: f'"{col}" > {_lit(v)}',
    "lt":         lambda col, v: f'"{col}" < {_lit(v)}',
    "gte":        lambda col, v: f'"{col}" >= {_lit(v)}',
    "lte":        lambda col, v: f'"{col}" <= {_lit(v)}',
    "in":         lambda col, v: f'"{col}" IN ({_lit_list(v)})',
    "not_in":     lambda col, v: f'"{col}" NOT IN ({_lit_list(v)})',
    "regex":      lambda col, v: f'regexp_matches(CAST("{col}" AS VARCHAR), {_lit(v)})',
    "between":    lambda col, v: f'"{col}" BETWEEN {_lit(v[0])} AND {_lit(v[1])}',
    "min_length": lambda col, v: f'length(CAST("{col}" AS VARCHAR)) >= {int(v)}',
    "max_length": lambda col, v: f'length(CAST("{col}" AS VARCHAR)) <= {int(v)}',
}


def _lit(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v).replace("'", "''")
    return f"'{s}'"


def _lit_list(values: Any) -> str:
    if not isinstance(values, (list, tuple)):
        values = [values]
    return ", ".join(_lit(v) for v in values)


def _build_rule_sql(rule: dict) -> str:
    op = rule.get("op", "not_null")
    col = rule.get("column", "")
    val = rule.get("value")
    if not col:
        return "TRUE"
    builder = _DQ_OPS.get(op)
    if not builder:
        return "TRUE"
    try:
        return builder(col, val)
    except Exception:
        return "TRUE"


@register(StepType.DATA_QUALITY)
class DataQualityNode(BaseNode):
    """Validate rows against a list of declarative rules.

    Modes:
      - "drop"      → silently drop failed rows
      - "fail"      → raise if any row fails
      - "tag"       → keep all rows, add `__dq_passed` boolean column
      - "split"     → emit only passing rows; failures are stored in
                      ctx under `_dq_failures_<step_id>` for a downstream sink
    """

    display_name = "Data Quality"
    category = "transform"
    description = "Check rows against rules you define. Good rows pass through; bad rows go to a separate path."

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        import logging
        logger = logging.getLogger(__name__)

        inputs = ctx.get_inputs(self.params.get("_input_step_ids", []))
        if not inputs:
            raise ValueError("Data Quality node has no input data")

        source = inputs[0]
        rules = self.params.get("rules") or []
        mode = self.params.get("mode", "drop")
        threshold = float(self.params.get("quality_threshold", 0))
        include_score = self.params.get("include_score", False)
        include_profile = self.params.get("include_profile", False)

        dq_input = ctx.register_scoped("__dq_input", source)

        # ── Column profiling (logged, not in output) ──
        if include_profile:
            try:
                total = ctx.conn.sql(f"SELECT COUNT(*) FROM {dq_input}").fetchone()[0]
                for col in source.columns:
                    stats = ctx.conn.sql(
                        f'SELECT '
                        f'  ROUND(100.0 * SUM(CASE WHEN "{col}" IS NULL THEN 1 ELSE 0 END) / COUNT(*), 1) AS null_pct, '
                        f'  COUNT(DISTINCT "{col}") AS distinct_count '
                        f'FROM {dq_input}'
                    ).fetchone()
                    logger.info(
                        "DQ Profile: %s — null%%=%.1f%%, distinct=%d, total=%d",
                        col, stats[0] or 0, stats[1] or 0, total,
                    )
            except Exception:
                pass  # profiling is best-effort

        if not rules:
            return source

        clauses = [f"({_build_rule_sql(r)})" for r in rules]
        passed_expr = " AND ".join(clauses) if clauses else "TRUE"

        # ── Quality threshold check ──
        if threshold > 0:
            total = ctx.conn.sql(f"SELECT COUNT(*) FROM {dq_input}").fetchone()[0]
            if total > 0:
                passed_count = ctx.conn.sql(
                    f"SELECT COUNT(*) FROM {dq_input} WHERE {passed_expr}"
                ).fetchone()[0]
                pass_rate = (passed_count / total) * 100
                logger.info("DQ Score: %.1f%% pass rate (%d/%d)", pass_rate, passed_count, total)
                if pass_rate < threshold:
                    if mode == "fail":
                        raise ValueError(
                            f"Data Quality: pass rate {pass_rate:.1f}% below threshold {threshold}%"
                        )
                    logger.warning(
                        "DQ: pass rate %.1f%% below threshold %.1f%% — applying %s mode",
                        pass_rate, threshold, mode,
                    )

        # ── Per-row quality score ──
        if include_score and len(clauses) > 0:
            score_parts = " + ".join(
                f"CASE WHEN {c} THEN 1 ELSE 0 END" for c in clauses
            )
            score_expr = f"ROUND(100.0 * ({score_parts}) / {len(clauses)}, 1)"
        else:
            score_expr = None

        if mode == "fail":
            check_sql = f"SELECT COUNT(*) FROM {dq_input} WHERE NOT ({passed_expr})"
            failed = ctx.conn.sql(check_sql).fetchone()[0]
            if failed:
                raise ValueError(f"Data Quality: {failed} row(s) failed validation")
            if score_expr:
                return ctx.conn.sql(
                    f"SELECT *, {score_expr} AS __dq_score FROM {dq_input}"
                )
            return source

        if mode == "tag":
            extra = f", {score_expr} AS __dq_score" if score_expr else ""
            return ctx.conn.sql(
                f"SELECT *, ({passed_expr}) AS __dq_passed{extra} FROM {dq_input}"
            )

        if mode == "reject":
            # 2026-06-11 (multi-output): two real output streams. Every row is
            # tagged pass/reject via _split_output; downstream nodes wired to
            # the "Pass" or "Reject" handle receive only their subset (the
            # central executor routing strips the tag). Good rows continue;
            # bad rows can route to a reject sink / review queue.
            extra = f", {score_expr} AS __dq_score" if score_expr else ""
            return ctx.conn.sql(
                f"SELECT *{extra}, "
                f"CASE WHEN ({passed_expr}) THEN 'pass' ELSE 'reject' END AS _split_output "
                f"FROM {dq_input}"
            )

        if mode == "split":
            failures = ctx.conn.sql(
                f"SELECT * FROM {dq_input} WHERE NOT ({passed_expr})"
            )
            ctx.vars[f"_dq_failures_{self.params.get('_step_id', 'dq')}"] = failures

        # Default + split: keep only passing rows
        extra = f", {score_expr} AS __dq_score" if score_expr else ""
        return ctx.conn.sql(f"SELECT *{extra} FROM {dq_input} WHERE {passed_expr}")

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "rules": [], "mode": "drop",
            "quality_threshold": 0,
            "include_score": False,
            "include_profile": False,
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {
                "name": "rules",
                "type": "rule_list",
                "label": "Validation Rules",
                "required": True, "tab": "Rules",
                "ops": list(_DQ_OPS.keys()),
                "description": (
                    "Operators: not_null, is_null, eq, ne, gt, lt, gte, lte, "
                    "in, not_in, regex, between, min_length, max_length."
                ),
            },
            {
                "name": "mode",
                "type": "select",
                "label": "On Failure",
                "options": ["drop", "fail", "tag", "reject", "split"],
                "default": "drop", "tab": "Rules",
                "description": (
                    "drop = remove failed rows, fail = abort pipeline, "
                    "tag = add __dq_passed column, reject = two outputs (Pass + Reject) "
                    "you can wire separately, split = pass only good rows (failures in ctx vars)."
                ),
            },
            {
                "name": "quality_threshold",
                "type": "number",
                "label": "Quality Score Threshold (%)",
                "default": 0, "tab": "Quality",
                "description": (
                    "If > 0: calculate pass rate. If pass rate < threshold%, "
                    "treat as failure (respecting mode). 0 = disabled."
                ),
            },
            {
                "name": "include_score",
                "type": "boolean",
                "label": "Add Quality Score Column",
                "default": False, "tab": "Quality",
                "description": (
                    "Add __dq_score column (0-100%) showing per-row rule pass rate."
                ),
            },
            {
                "name": "include_profile",
                "type": "boolean",
                "label": "Include Column Profile",
                "default": False, "tab": "Quality",
                "description": (
                    "Log null %, distinct %, and min/max for each column. "
                    "Visible in execution logs."
                ),
            },
        ]


# ─────────────────────────────────────────────────────────────────────────────
# Upsert
# ─────────────────────────────────────────────────────────────────────────────

@register(StepType.UPSERT)
class UpsertNode(BaseNode):
    """Collapse duplicate keys, keeping the latest row per `order_by`.

    This makes any source idempotent: re-running the pipeline produces the same
    output regardless of duplicate or replayed events. The actual write to a
    target table is the responsibility of the downstream sink — this node only
    guarantees the relation is unique on the key columns.
    """

    # 2026-06-11 (node-audit): renamed from "Upsert". This node does NOT
    # update/insert against a target table — it collapses duplicate keys
    # within the stream, latest wins by Order By. ETL developers reading
    # "Upsert" expect MERGE / ON CONFLICT semantics against a target; the
    # honest name prevents that misread. (StepType stays `upsert` for
    # back-compat with saved pipelines; a true target-table upsert
    # belongs in the Destination/Managed Table Sink write modes.)
    display_name = "Keep Latest"
    category = "transform"
    description = "Collapse duplicate keys to one row — latest wins by Order By. (Stream-level: writing to a table is the sink's job.)"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        inputs = ctx.get_inputs(self.params.get("_input_step_ids", []))
        if not inputs:
            raise ValueError("Upsert node has no input data")

        source = inputs[0]
        keys = self.params.get("key") or []
        if isinstance(keys, str):
            keys = [k.strip() for k in keys.split(",") if k.strip()]
        if not keys:
            raise ValueError("Upsert node requires at least one key column")

        order_by = self.params.get("order_by", "").strip()
        order_clause = f"ORDER BY {order_by}" if order_by else "ORDER BY 1"
        key_cols = ", ".join(f'"{k}"' for k in keys)

        upsert_input = ctx.register_scoped("__upsert_input", source)
        sql = f"""
            SELECT * EXCLUDE (__upsert_rn) FROM (
                SELECT *,
                       ROW_NUMBER() OVER (PARTITION BY {key_cols} {order_clause}) AS __upsert_rn
                FROM {upsert_input}
            )
            WHERE __upsert_rn = 1
        """
        return ctx.conn.sql(sql)

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"key": [], "order_by": ""}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "key", "type": "column_list", "label": "Key Columns", "required": True},
            {"name": "order_by", "type": "text", "label": "Order By (latest wins)",
             "placeholder": "updated_at DESC"},
        ]


# ─────────────────────────────────────────────────────────────────────────────
# Schema Mapper
# ─────────────────────────────────────────────────────────────────────────────

_SQL_TYPES = {
    "string": "VARCHAR",
    "varchar": "VARCHAR",
    "text": "VARCHAR",
    "int": "BIGINT",
    "integer": "BIGINT",
    "bigint": "BIGINT",
    "long": "BIGINT",
    "float": "DOUBLE",
    "double": "DOUBLE",
    "decimal": "DECIMAL(38,9)",
    "bool": "BOOLEAN",
    "boolean": "BOOLEAN",
    "date": "DATE",
    "datetime": "TIMESTAMP",
    "timestamp": "TIMESTAMP",
    "json": "JSON",
}


@register(StepType.SCHEMA_MAPPER)
class SchemaMapperNode(BaseNode):
    """Map source columns to a target schema, with type coercion and defaults.

    `mappings` is a list of `{source, target, type, default}` dicts:
      - `source`: source column name (or empty if synthesizing from default)
      - `target`: target column name in the output relation
      - `type`:   one of the friendly names in _SQL_TYPES
      - `default`: literal value used when source is NULL or missing

    Unmapped source columns are dropped unless `keep_unmapped=True`.
    """

    display_name = "Schema Mapper"
    category = "transform"
    description = "Rename, reorder, and convert column types to match a target table or format"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        inputs = ctx.get_inputs(self.params.get("_input_step_ids", []))
        if not inputs:
            raise ValueError("Schema Mapper has no input data")

        source = inputs[0]
        mappings = self.params.get("mappings") or []
        keep_unmapped = bool(self.params.get("keep_unmapped", False))

        if not mappings:
            return source

        sm_input = ctx.register_scoped("__sm_input", source)
        src_cols = set(source.columns)

        select_parts: list[str] = []
        mapped_sources: set[str] = set()

        for m in mappings:
            tgt = (m.get("target") or "").strip()
            src = (m.get("source") or "").strip()
            sql_type = _SQL_TYPES.get((m.get("type") or "string").lower(), "VARCHAR")
            default = m.get("default")

            if not tgt:
                continue

            if src and src in src_cols:
                mapped_sources.add(src)
                expr = f'"{src}"'
                if default is not None:
                    expr = f"COALESCE({expr}, {_lit(default)})"
                expr = f"CAST({expr} AS {sql_type})"
            elif default is not None:
                expr = f"CAST({_lit(default)} AS {sql_type})"
            else:
                expr = f"CAST(NULL AS {sql_type})"

            select_parts.append(f'{expr} AS "{tgt}"')

        if keep_unmapped:
            for c in source.columns:
                if c not in mapped_sources:
                    select_parts.append(f'"{c}"')

        if not select_parts:
            return source

        sql = f"SELECT {', '.join(select_parts)} FROM {sm_input}"
        return ctx.conn.sql(sql)

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"mappings": [], "keep_unmapped": False}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {
                "name": "mappings",
                "type": "schema_map",
                "label": "Field Mappings",
                "required": True,
                "types": list(_SQL_TYPES.keys()),
            },
            {
                "name": "keep_unmapped",
                "type": "boolean",
                "label": "Keep unmapped source columns",
                "default": False,
            },
        ]


# ─────────────────────────────────────────────────────────────────────────────
# Data Profile (Sprint 1 — May 3 2026)
# ─────────────────────────────────────────────────────────────────────────────

@register(StepType.DATA_PROFILE)
class DataProfileNode(BaseNode):
    """Emit one row per source column with summary statistics.

    Output columns:
      - column            VARCHAR
      - data_type         VARCHAR        — DuckDB-reported type
      - row_count         BIGINT         — total rows in the source
      - null_count        BIGINT
      - null_pct          DOUBLE         — 0.0–100.0
      - distinct_count    BIGINT
      - distinct_pct      DOUBLE         — 100 * distinct / non-null
      - min_value         VARCHAR        — string-coerced; NULL for binary types
      - max_value         VARCHAR        — string-coerced
      - top_value         VARCHAR        — most-common value (first if tied)
      - top_value_count   BIGINT
      - mean_value        DOUBLE         — numeric mean, NULL for non-numeric (2026-05-30)
      - median_value      DOUBLE         — 50th percentile, NULL for non-numeric (2026-05-30)
      - stddev_value      DOUBLE         — population stddev, NULL for non-numeric (2026-05-30)
      - avg_length        DOUBLE         — average string length, NULL for non-string (2026-05-30)
      - max_length        BIGINT         — longest string length, NULL for non-string (2026-05-30)

    Cheap to compute: O(C × R) where C is column count. Uses DuckDB's
    aggregations directly so no Python row-by-row work happens. Non-applicable
    metrics (mean on strings, length on numerics) return NULL via TRY_CAST
    so a mixed-type relation profiles cleanly without per-column dispatch.

    Use this as a downstream sibling of any source to verify shape before
    spending time on transforms, or right before a Beta sink to flag
    nullability / cardinality surprises.
    """

    display_name = "Data Profile"
    category = "transform"
    description = "Summary statistics per column — null %, distinct count, min/max, top value. One row per column."

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "sample_rows": 0,
            "include_top_value": True,
            "include_columns": [],
            "exclude_columns": [],
            # C2 (2026-06-15) dual-output. False = single output (the report)
            # exactly as before. True = ALSO expose the original input rows on
            # a secondary "data" port (heterogeneous multi-output, C1) so the
            # pipeline can profile AND continue without a second source read.
            "passthrough_data": False,
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {
                "name": "sample_rows",
                "type": "int",
                "default": 0,
                "min": 0,
                "description": (
                    "Cap the profile to N rows (USING SAMPLE). 0 = scan the "
                    "full upstream relation. Use a sample for billion-row "
                    "tables when an approximate profile is good enough."
                ),
            },
            {
                "name": "passthrough_data",
                "type": "bool",
                "default": False,
                "description": (
                    "Also emit the original input rows on a second 'Data' "
                    "output port. Leave off to emit only the column report. "
                    "Turn on to profile a dataset AND keep building the "
                    "pipeline from the same rows — wire 'Report' to a sink "
                    "and 'Data' to the next transform."
                ),
            },
            {
                "name": "include_top_value",
                "type": "bool",
                "default": True,
                "description": (
                    "Compute the most common value per column. Adds one "
                    "GROUP BY per column — turn off on >1000-column relations."
                ),
            },
            {
                "name": "include_columns",
                "type": "list[str]",
                "default": [],
                "description": (
                    "Restrict the profile to these column names. Empty = profile "
                    "every column. Useful for narrowing onto customer-facing "
                    "fields when the source has hundreds of internal columns."
                ),
            },
            {
                "name": "exclude_columns",
                "type": "list[str]",
                "default": [],
                "description": (
                    "Skip these column names. Applied after include_columns. "
                    "Useful for excluding bulky free-text columns where "
                    "min/max/distinct adds little value."
                ),
            },
        ]

    def execute(self, ctx: ExecutionContext) -> "duckdb.DuckDBPyRelation":
        import logging
        logger = logging.getLogger(__name__)

        inputs = ctx.get_inputs(self.params.get("_input_step_ids", []))
        if not inputs:
            raise ValueError("Data Profile node has no input data")

        source = inputs[0]
        sample_rows = int(self.params.get("sample_rows", 0))  # 0 = full scan
        include_top_value = bool(self.params.get("include_top_value", True))
        include_columns = list(self.params.get("include_columns") or [])
        exclude_columns = list(self.params.get("exclude_columns") or [])

        # C2 (2026-06-15) dual-output: when passthrough_data is on, expose the
        # ORIGINAL input rows on a secondary 'data' port (heterogeneous
        # multi-output, C1). Registered up-front so every return path below
        # (empty-source, filtered-empty, full profile, degraded fallback)
        # still feeds the data port. The PRIMARY output stays the report.
        if bool(self.params.get("passthrough_data", False)):
            step_id = self.params.get("_step_id")
            if step_id:
                ctx.set_named_output(step_id, "data", source)

        # Materialize once so the multi-aggregate pass doesn't re-evaluate
        # the upstream relation per column.
        profile_input = ctx.register_scoped("__profile_input", source)
        profile_src = ctx.scoped_name("__profile_src")
        if sample_rows > 0:
            ctx.conn.execute(
                f"CREATE OR REPLACE TEMP TABLE {profile_src} AS "
                f"SELECT * FROM {profile_input} USING SAMPLE {sample_rows} ROWS"
            )
        else:
            ctx.conn.execute(f"CREATE OR REPLACE TEMP TABLE {profile_src} AS SELECT * FROM {profile_input}")

        total = ctx.conn.sql(f"SELECT COUNT(*) FROM {profile_src}").fetchone()[0]
        if total == 0:
            # Return an empty profile with the right schema so downstream
            # nodes don't break on a missing column set.
            return ctx.conn.sql(_PROFILE_EMPTY_SQL)

        # DuckDB columns + types
        try:
            schema_rows = ctx.conn.sql(f"DESCRIBE {profile_src}").fetchall()
            type_map = {r[0]: r[1] for r in schema_rows}
        except Exception:
            type_map = {c: "UNKNOWN" for c in source.columns}

        # Apply include / exclude filters. include_columns acts as an
        # allowlist (empty = all columns); exclude_columns then strips
        # any leftovers. This order matches what most data-profiling
        # tools do and is the least surprising for operators chaining
        # both filters in one node.
        all_cols = list(source.columns)
        if include_columns:
            allowed = set(include_columns)
            cols_to_profile = [c for c in all_cols if c in allowed]
        else:
            cols_to_profile = list(all_cols)
        if exclude_columns:
            denied = set(exclude_columns)
            cols_to_profile = [c for c in cols_to_profile if c not in denied]
        if not cols_to_profile:
            # Filters left nothing — return the empty-schema sentinel so
            # downstream nodes don't choke on a column-less relation.
            return ctx.conn.sql(_PROFILE_EMPTY_SQL)

        # Build the profile in one UNION ALL query — much faster than
        # iterating per-column in Python (one DuckDB plan, parallelized).
        per_column_selects: list[str] = []
        for col in cols_to_profile:
            col_sql = col.replace('"', '""')
            dt = type_map.get(col, "UNKNOWN")
            # min/max only meaningful for orderable types — for blobs and
            # nested types DuckDB will reject; fall back to NULL via TRY_CAST.
            min_expr = f'TRY_CAST(MIN("{col_sql}") AS VARCHAR)'
            max_expr = f'TRY_CAST(MAX("{col_sql}") AS VARCHAR)'

            if include_top_value:
                top_value_subquery = (
                    f'(SELECT TRY_CAST("{col_sql}" AS VARCHAR) FROM {profile_src} '
                    f'WHERE "{col_sql}" IS NOT NULL '
                    f'GROUP BY "{col_sql}" ORDER BY COUNT(*) DESC LIMIT 1)'
                )
                top_count_subquery = (
                    f'(SELECT COUNT(*) FROM {profile_src} '
                    f'WHERE "{col_sql}" IS NOT NULL '
                    f'GROUP BY "{col_sql}" ORDER BY COUNT(*) DESC LIMIT 1)'
                )
            else:
                top_value_subquery = "CAST(NULL AS VARCHAR)"
                top_count_subquery = "CAST(0 AS BIGINT)"

            # 2026-05-30: depth-of-profile additions. TRY_CAST inside
            # AVG/MEDIAN/STDDEV means non-numeric columns collapse to
            # NULL aggregates instead of raising — operators get one
            # uniform query rather than per-column dispatch on type.
            # Same logic for length stats on non-string columns.
            mean_expr = f'AVG(TRY_CAST("{col_sql}" AS DOUBLE))'
            median_expr = f'QUANTILE_CONT(TRY_CAST("{col_sql}" AS DOUBLE), 0.5)'
            stddev_expr = f'STDDEV_POP(TRY_CAST("{col_sql}" AS DOUBLE))'
            avg_len_expr = f'AVG(LENGTH(TRY_CAST("{col_sql}" AS VARCHAR)))'
            max_len_expr = f'MAX(LENGTH(TRY_CAST("{col_sql}" AS VARCHAR)))'

            per_column_selects.append(
                f"SELECT "
                f"  '{col_sql.replace(chr(39), chr(39) + chr(39))}' AS column, "
                f"  '{str(dt).replace(chr(39), chr(39) + chr(39))}' AS data_type, "
                f"  CAST({total} AS BIGINT) AS row_count, "
                f'  COUNT(*) FILTER (WHERE "{col_sql}" IS NULL) AS null_count, '
                f'  ROUND(100.0 * COUNT(*) FILTER (WHERE "{col_sql}" IS NULL) / NULLIF({total}, 0), 2) AS null_pct, '
                f'  COUNT(DISTINCT "{col_sql}") AS distinct_count, '
                f'  ROUND(100.0 * COUNT(DISTINCT "{col_sql}") / NULLIF(COUNT("{col_sql}"), 0), 2) AS distinct_pct, '
                f"  {min_expr} AS min_value, "
                f"  {max_expr} AS max_value, "
                f"  {top_value_subquery} AS top_value, "
                f"  {top_count_subquery} AS top_value_count, "
                f"  CAST({mean_expr} AS DOUBLE) AS mean_value, "
                f"  CAST({median_expr} AS DOUBLE) AS median_value, "
                f"  CAST({stddev_expr} AS DOUBLE) AS stddev_value, "
                f"  CAST({avg_len_expr} AS DOUBLE) AS avg_length, "
                f"  CAST({max_len_expr} AS BIGINT) AS max_length "
                f"FROM {profile_src}"
            )

        sql = " UNION ALL ".join(per_column_selects)
        try:
            return ctx.conn.sql(sql)
        except Exception as exc:
            # If a single problematic column blows up min/max (e.g. STRUCT
            # types), fall back to a degraded profile that omits min/max.
            logger.warning("Data Profile fallback (min/max disabled): %s", exc)
            fallback_selects: list[str] = []
            for col in source.columns:
                col_sql = col.replace('"', '""')
                dt = type_map.get(col, "UNKNOWN")
                fallback_selects.append(
                    f"SELECT "
                    f"  '{col_sql.replace(chr(39), chr(39) + chr(39))}' AS column, "
                    f"  '{str(dt).replace(chr(39), chr(39) + chr(39))}' AS data_type, "
                    f"  CAST({total} AS BIGINT) AS row_count, "
                    f'  COUNT(*) FILTER (WHERE "{col_sql}" IS NULL) AS null_count, '
                    f'  ROUND(100.0 * COUNT(*) FILTER (WHERE "{col_sql}" IS NULL) / NULLIF({total}, 0), 2) AS null_pct, '
                    f'  COUNT(DISTINCT "{col_sql}") AS distinct_count, '
                    f'  ROUND(100.0 * COUNT(DISTINCT "{col_sql}") / NULLIF(COUNT("{col_sql}"), 0), 2) AS distinct_pct, '
                    f"  CAST(NULL AS VARCHAR) AS min_value, "
                    f"  CAST(NULL AS VARCHAR) AS max_value, "
                    f"  CAST(NULL AS VARCHAR) AS top_value, "
                    f"  CAST(0 AS BIGINT) AS top_value_count, "
                    # 2026-05-30: keep the schema consistent with the
                    # full-profile path so downstream SchemaMapper /
                    # sinks don't blow up when the fallback fires.
                    f"  CAST(NULL AS DOUBLE) AS mean_value, "
                    f"  CAST(NULL AS DOUBLE) AS median_value, "
                    f"  CAST(NULL AS DOUBLE) AS stddev_value, "
                    f"  CAST(NULL AS DOUBLE) AS avg_length, "
                    f"  CAST(NULL AS BIGINT) AS max_length "
                    f"FROM {profile_src}"
                )
            return ctx.conn.sql(" UNION ALL ".join(fallback_selects))
