"""
Pre-Execution Validation Layer -- validates the ENTIRE pipeline before execution.

Goes beyond structural validation (ir/validator.py) to perform DATA-LEVEL checks:
- Source existence (files, SQL syntax)
- Inter-node schema compatibility (column references vs actual output)
- Parameter completeness
- Output path validation
- Connection completeness

Uses DuckDB in-memory connections for fast validation without running the full pipeline.
"""

from __future__ import annotations

import difflib
import os
import re
from typing import Any, TYPE_CHECKING

# Stage 2.5b: duckdb is RUNTIME-USED in this file (duckdb.connect,
# duckdb.ParserException). The runtime imports live inside the methods
# that use them so a CLI or test that imports this module without
# touching validation paths doesn't pay the duckdb load cost.
# Top-level import kept ONLY behind TYPE_CHECKING for any annotation
# that may reference duckdb types directly.
if TYPE_CHECKING:
    import duckdb  # noqa: F401  (annotations only)

from pydantic import BaseModel, Field

from fpulse.ir.schema import Workflow, Step, StepType, StepConnection


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ValidationCheck(BaseModel):
    """A single validation check result."""
    step_id: str
    step_label: str
    check_type: str  # source_exists, schema_compatible, column_exists, type_compatible, params_valid, connection_valid, output_valid
    passed: bool
    message: str
    severity: str  # error, warning, info
    suggestion: str | None = None


class PreValidationResult(BaseModel):
    """Full pre-validation result for a workflow."""
    valid: bool
    checks: list[ValidationCheck] = Field(default_factory=list)
    warnings: list[ValidationCheck] = Field(default_factory=list)
    errors: list[ValidationCheck] = Field(default_factory=list)
    can_execute: bool  # True if no blocking errors


# ---------------------------------------------------------------------------
# Column extraction helpers
# ---------------------------------------------------------------------------

# Matches identifiers in SQL: bare words, double-quoted, or backtick-quoted
_SQL_IDENT_RE = re.compile(r'"([^"]+)"|`([^`]+)`|([A-Za-z_]\w*)')

# SQL keywords / functions to exclude when extracting column references
_SQL_KEYWORDS = frozenset({
    "select", "from", "where", "and", "or", "not", "is", "null", "in",
    "between", "like", "ilike", "case", "when", "then", "else", "end",
    "as", "on", "join", "inner", "left", "right", "full", "outer",
    "cross", "group", "by", "order", "asc", "desc", "having", "limit",
    "offset", "union", "all", "distinct", "exists", "true", "false",
    "cast", "coalesce", "nullif", "count", "sum", "avg", "min", "max",
    "first", "last", "string_agg", "list", "any_value", "row_number",
    "rank", "dense_rank", "ntile", "lag", "lead", "over", "partition",
    "current_date", "current_timestamp", "now", "date", "timestamp",
    "int", "integer", "bigint", "float", "double", "varchar", "text",
    "boolean", "date_trunc", "date_part", "extract", "year", "month",
    "day", "hour", "minute", "second", "trim", "upper", "lower",
    "length", "substring", "replace", "concat", "abs", "ceil", "floor",
    "round", "power", "sqrt", "mod", "greatest", "least",
    "source_table", "input", "__filter_input", "__agg_input",
    "__join_left", "__join_right", "__dedup_input",
})


def _extract_column_refs(expression: str) -> list[str]:
    """Extract probable column name references from a SQL expression.

    Returns bare identifiers that are not SQL keywords or functions.
    """
    refs: list[str] = []
    for m in _SQL_IDENT_RE.finditer(expression):
        name = m.group(1) or m.group(2) or m.group(3)
        if name and name.lower() not in _SQL_KEYWORDS:
            refs.append(name)
    return list(dict.fromkeys(refs))  # dedupe preserving order


def _suggest_column(name: str, available: list[str], cutoff: float = 0.6) -> str | None:
    """Suggest a close column name match using fuzzy matching."""
    matches = difflib.get_close_matches(name, available, n=1, cutoff=cutoff)
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# Source node types and transform categories
# ---------------------------------------------------------------------------

_SOURCE_TYPES = frozenset({
    StepType.CSV_SOURCE, StepType.DB_SOURCE, StepType.API_SOURCE,
    StepType.SOURCE,
})
_OUTPUT_TYPES = frozenset({StepType.OUTPUT, StepType.DB_SINK, StepType.DESTINATION})

# Required params by step type (beyond what structural validator checks)
_REQUIRED_PARAMS: dict[StepType, list[str]] = {
    StepType.SOURCE:            ["connector_type"],
    StepType.DESTINATION:       ["connector_type"],
    StepType.CSV_SOURCE:        ["file_path"],
    StepType.DB_SOURCE:         ["query"],
    StepType.FILTER:            ["condition"],
    StepType.TRANSFORM:         ["expression"],
    StepType.DEDUPLICATE:       ["key"],
    StepType.AGGREGATE:         ["group_by", "functions"],
    StepType.JOIN:              ["join_key"],
    StepType.SORT:              ["columns"],
    StepType.RENAME:            ["mapping"],
    StepType.TYPECAST:          ["casts"],
    StepType.DERIVED_COLUMN:    ["expression", "name"],
    StepType.LOOKUP:            ["lookup_key"],
    StepType.UNION:             [],
    StepType.PIVOT:             ["index", "columns", "values"],
    StepType.UNPIVOT:           ["columns"],
    StepType.WINDOW:            ["function", "partition_by"],
    StepType.SAMPLE:            [],
    StepType.VALIDATE:          ["rules"],
    StepType.CONDITIONAL_SPLIT: ["conditions"],
    StepType.OUTPUT:            ["format"],
    StepType.DB_SINK:           ["connection", "table"],
}


# ---------------------------------------------------------------------------
# PreValidator
# ---------------------------------------------------------------------------

class PreValidator:
    """Validates an entire workflow at the data level before execution."""

    def __init__(self, data_dir: str = "."):
        self.data_dir = data_dir

    def validate(self, workflow: Workflow) -> PreValidationResult:
        """Run all pre-execution validation checks on a workflow."""
        checks: list[ValidationCheck] = []

        step_map = {s.id: s for s in workflow.steps}
        input_map = self._build_input_map(workflow)
        output_map = self._build_output_map(workflow)

        # 1. Parameter completeness
        checks.extend(self._check_param_completeness(workflow))

        # 2. Source existence
        checks.extend(self._check_source_existence(workflow))

        # 3. Connection completeness
        checks.extend(self._check_connection_completeness(workflow, input_map, output_map))

        # 4. Output path validation
        checks.extend(self._check_output_paths(workflow))

        # 5. Inter-node schema compatibility (the big one)
        checks.extend(self._check_schema_compatibility(workflow, step_map, input_map))

        # Separate errors, warnings, and info
        errors = [c for c in checks if not c.passed and c.severity == "error"]
        warnings = [c for c in checks if not c.passed and c.severity == "warning"]
        passed = [c for c in checks if c.passed]
        can_execute = len(errors) == 0

        return PreValidationResult(
            valid=can_execute and len(warnings) == 0,
            checks=checks,
            warnings=warnings,
            errors=errors,
            can_execute=can_execute,
        )

    def validate_node_connections(
        self, workflow: Workflow, data_dir: str | None = None,
    ) -> list[ValidationCheck]:
        """Validate inter-node column references by executing source nodes with preview_limit=1.

        1. Executes source nodes to get their output schemas
        2. Propagates schemas through the DAG
        3. Checks each downstream node's params for valid column references
        """
        effective_data_dir = data_dir or self.data_dir
        step_map = {s.id: s for s in workflow.steps}
        input_map = self._build_input_map(workflow)
        checks: list[ValidationCheck] = []

        # Execute source nodes and collect output schemas
        schemas: dict[str, list[str]] = {}  # step_id -> list of column names
        import duckdb  # method-scoped — first call pays import, rest hit sys.modules cache
        conn = duckdb.connect(":memory:")

        try:
            # Topological order
            order = self._topological_sort(workflow)

            for step in order:
                step_inputs = input_map.get(step.id, [])

                if step.type in _SOURCE_TYPES:
                    # Execute source to get real schema
                    cols = self._execute_source_for_schema(step, conn, effective_data_dir)
                    if cols is not None:
                        schemas[step.id] = cols
                        checks.append(ValidationCheck(
                            step_id=step.id,
                            step_label=step.label or step.id,
                            check_type="schema_compatible",
                            passed=True,
                            message=f"Source outputs {len(cols)} columns: {', '.join(cols[:10])}{'...' if len(cols) > 10 else ''}",
                            severity="info",
                        ))
                    else:
                        checks.append(ValidationCheck(
                            step_id=step.id,
                            step_label=step.label or step.id,
                            check_type="schema_compatible",
                            passed=False,
                            message=f"Could not determine output schema for source node",
                            severity="warning",
                        ))
                else:
                    # Gather input columns from upstream nodes
                    input_cols: list[str] = []
                    for inp_id in step_inputs:
                        if inp_id in schemas:
                            input_cols.extend(schemas[inp_id])
                    input_cols = list(dict.fromkeys(input_cols))  # dedupe

                    if not input_cols and step_inputs:
                        # Can't validate without knowing input schema
                        checks.append(ValidationCheck(
                            step_id=step.id,
                            step_label=step.label or step.id,
                            check_type="schema_compatible",
                            passed=True,
                            message="Input schema unknown; skipping column validation",
                            severity="info",
                        ))
                        # Propagate unknown
                        schemas[step.id] = []
                        continue

                    # Validate column references in this node's params
                    col_checks = self._validate_column_refs(step, input_cols)
                    checks.extend(col_checks)

                    # Propagate schema: estimate output columns
                    output_cols = self._estimate_output_schema(step, input_cols)
                    schemas[step.id] = output_cols

        finally:
            conn.close()

        return checks

    # ------------------------------------------------------------------
    # Check: Parameter completeness
    # ------------------------------------------------------------------

    def _check_param_completeness(self, workflow: Workflow) -> list[ValidationCheck]:
        checks: list[ValidationCheck] = []
        for step in workflow.steps:
            required = _REQUIRED_PARAMS.get(step.type, [])
            for param_name in required:
                val = step.params.get(param_name)
                if val is None or val == "" or val == []:
                    checks.append(ValidationCheck(
                        step_id=step.id,
                        step_label=step.label or step.id,
                        check_type="params_valid",
                        passed=False,
                        message=f"Required parameter '{param_name}' is empty or missing",
                        severity="error",
                        suggestion=f"Set the '{param_name}' parameter for this {step.type.value} node",
                    ))
                else:
                    checks.append(ValidationCheck(
                        step_id=step.id,
                        step_label=step.label or step.id,
                        check_type="params_valid",
                        passed=True,
                        message=f"Parameter '{param_name}' is set",
                        severity="info",
                    ))
        return checks

    # ------------------------------------------------------------------
    # Check: Source existence
    # ------------------------------------------------------------------

    def _check_source_existence(self, workflow: Workflow) -> list[ValidationCheck]:
        checks: list[ValidationCheck] = []
        for step in workflow.steps:
            if step.type == StepType.CSV_SOURCE:
                checks.extend(self._check_csv_source(step))
            elif step.type == StepType.DB_SOURCE:
                checks.extend(self._check_db_source(step))
        return checks

    def _check_csv_source(self, step: Step) -> list[ValidationCheck]:
        checks: list[ValidationCheck] = []
        file_path = step.params.get("file_path", "")
        if not file_path:
            return checks  # Already caught by param completeness

        # Resolve path
        if not os.path.isabs(file_path):
            full_path = os.path.join(self.data_dir, file_path)
        else:
            full_path = file_path

        if os.path.exists(full_path):
            checks.append(ValidationCheck(
                step_id=step.id,
                step_label=step.label or step.id,
                check_type="source_exists",
                passed=True,
                message=f"File exists: {file_path}",
                severity="info",
            ))
        else:
            # Fuzzy match against files in data_dir
            suggestion = self._suggest_file(file_path)
            checks.append(ValidationCheck(
                step_id=step.id,
                step_label=step.label or step.id,
                check_type="source_exists",
                passed=False,
                message=f"File not found: {file_path}",
                severity="error",
                suggestion=suggestion,
            ))
        return checks

    def _check_db_source(self, step: Step) -> list[ValidationCheck]:
        checks: list[ValidationCheck] = []
        query = step.params.get("query", "")
        if not query:
            return checks

        # Validate SQL syntax using DuckDB parser
        import duckdb  # method-scoped (Stage 2.5b)
        conn = duckdb.connect(":memory:")
        try:
            # Create a dummy table so basic queries don't fail on missing tables
            conn.sql("CREATE TABLE __syntax_check (id INTEGER)")
            conn.sql(f"EXPLAIN {query}")
            checks.append(ValidationCheck(
                step_id=step.id,
                step_label=step.label or step.id,
                check_type="source_exists",
                passed=True,
                message="SQL query syntax is valid",
                severity="info",
            ))
        except duckdb.ParserException as e:
            checks.append(ValidationCheck(
                step_id=step.id,
                step_label=step.label or step.id,
                check_type="source_exists",
                passed=False,
                message=f"SQL syntax error: {str(e)[:200]}",
                severity="error",
                suggestion="Check your SQL query for syntax errors",
            ))
        except Exception:
            # Other errors (missing table etc.) are OK for syntax check
            checks.append(ValidationCheck(
                step_id=step.id,
                step_label=step.label or step.id,
                check_type="source_exists",
                passed=True,
                message="SQL query syntax appears valid (table existence not verified)",
                severity="info",
            ))
        finally:
            conn.close()

        return checks

    # ------------------------------------------------------------------
    # Check: Connection completeness
    # ------------------------------------------------------------------

    def _check_connection_completeness(
        self, workflow: Workflow,
        input_map: dict[str, list[str]],
        output_map: dict[str, list[str]],
    ) -> list[ValidationCheck]:
        checks: list[ValidationCheck] = []
        for step in workflow.steps:
            has_inputs = len(input_map.get(step.id, [])) > 0
            has_outputs = len(output_map.get(step.id, [])) > 0

            # Non-source nodes must have at least one input
            if step.type not in _SOURCE_TYPES and not has_inputs:
                checks.append(ValidationCheck(
                    step_id=step.id,
                    step_label=step.label or step.id,
                    check_type="connection_valid",
                    passed=False,
                    message=f"Node has no input connections (type: {step.type.value})",
                    severity="error",
                    suggestion="Connect an upstream node to provide input data",
                ))

            # Non-output nodes should have at least one output (warning, not error)
            if step.type not in _OUTPUT_TYPES and not has_outputs:
                checks.append(ValidationCheck(
                    step_id=step.id,
                    step_label=step.label or step.id,
                    check_type="connection_valid",
                    passed=False,
                    message=f"Node has no output connections (dead end)",
                    severity="warning",
                    suggestion="Connect this node to a downstream node or add an Output node",
                ))

            # Join requires exactly 2 inputs
            if step.type == StepType.JOIN:
                n_inputs = len(input_map.get(step.id, []))
                if n_inputs < 2:
                    checks.append(ValidationCheck(
                        step_id=step.id,
                        step_label=step.label or step.id,
                        check_type="connection_valid",
                        passed=False,
                        message=f"Join node needs 2 inputs, has {n_inputs}",
                        severity="error",
                        suggestion="Connect a second data source to the Join node",
                    ))
                elif n_inputs == 2:
                    checks.append(ValidationCheck(
                        step_id=step.id,
                        step_label=step.label or step.id,
                        check_type="connection_valid",
                        passed=True,
                        message="Join node has 2 inputs",
                        severity="info",
                    ))

        return checks

    # ------------------------------------------------------------------
    # Check: Output path validation
    # ------------------------------------------------------------------

    def _check_output_paths(self, workflow: Workflow) -> list[ValidationCheck]:
        checks: list[ValidationCheck] = []
        for step in workflow.steps:
            if step.type != StepType.OUTPUT:
                continue

            file_path = step.params.get("file_path", "")
            if not file_path:
                # Will default to data_dir/output.<fmt>
                checks.append(ValidationCheck(
                    step_id=step.id,
                    step_label=step.label or step.id,
                    check_type="output_valid",
                    passed=True,
                    message="Output will use default path in data directory",
                    severity="info",
                ))
                continue

            if not os.path.isabs(file_path):
                full_path = os.path.join(self.data_dir, file_path)
            else:
                full_path = file_path

            out_dir = os.path.dirname(full_path)
            if not out_dir:
                out_dir = self.data_dir

            if os.path.isdir(out_dir):
                checks.append(ValidationCheck(
                    step_id=step.id,
                    step_label=step.label or step.id,
                    check_type="output_valid",
                    passed=True,
                    message=f"Output directory exists: {out_dir}",
                    severity="info",
                ))
            else:
                checks.append(ValidationCheck(
                    step_id=step.id,
                    step_label=step.label or step.id,
                    check_type="output_valid",
                    passed=False,
                    message=f"Output directory does not exist: {out_dir}",
                    severity="warning",
                    suggestion="The directory will be created automatically during execution",
                ))

        return checks

    # ------------------------------------------------------------------
    # Check: Inter-node schema compatibility
    # ------------------------------------------------------------------

    def _check_schema_compatibility(
        self, workflow: Workflow,
        step_map: dict[str, Step],
        input_map: dict[str, list[str]],
    ) -> list[ValidationCheck]:
        """Validate SQL expressions and column references using DuckDB."""
        checks: list[ValidationCheck] = []

        for step in workflow.steps:
            if step.type == StepType.FILTER:
                condition = step.params.get("condition", "")
                if condition:
                    checks.extend(self._validate_sql_expression(
                        step, f"SELECT * FROM __t WHERE {condition}", "filter condition",
                    ))

            elif step.type == StepType.TRANSFORM:
                expression = step.params.get("expression", "")
                if expression:
                    # Replace source_table/input references with __t
                    test_sql = expression.replace("source_table", "__t").replace("input", "__t")
                    checks.extend(self._validate_sql_expression(
                        step, test_sql, "transform expression",
                    ))

        return checks

    def _validate_sql_expression(
        self, step: Step, sql: str, context: str,
    ) -> list[ValidationCheck]:
        """Validate a SQL expression against a dummy table to catch syntax errors."""
        checks: list[ValidationCheck] = []
        import duckdb  # method-scoped (Stage 2.5b)
        conn = duckdb.connect(":memory:")
        try:
            # Create a minimal dummy table
            conn.sql("CREATE TABLE __t (dummy INTEGER)")
            conn.sql(f"EXPLAIN {sql}")
            checks.append(ValidationCheck(
                step_id=step.id,
                step_label=step.label or step.id,
                check_type="schema_compatible",
                passed=True,
                message=f"SQL syntax valid for {context}",
                severity="info",
            ))
        except duckdb.ParserException as e:
            error_msg = str(e)
            checks.append(ValidationCheck(
                step_id=step.id,
                step_label=step.label or step.id,
                check_type="schema_compatible",
                passed=False,
                message=f"SQL syntax error in {context}: {error_msg[:200]}",
                severity="error",
                suggestion="Check your SQL syntax",
            ))
        except duckdb.BinderException:
            # Column/table reference errors are expected with dummy table -- not a syntax error
            checks.append(ValidationCheck(
                step_id=step.id,
                step_label=step.label or step.id,
                check_type="schema_compatible",
                passed=True,
                message=f"SQL syntax valid for {context} (column binding deferred to execution)",
                severity="info",
            ))
        except Exception:
            # Other errors we can't categorize -- skip
            pass
        finally:
            conn.close()

        return checks

    # ------------------------------------------------------------------
    # Column reference validation (used by validate_node_connections)
    # ------------------------------------------------------------------

    def _validate_column_refs(
        self, step: Step, available_columns: list[str],
    ) -> list[ValidationCheck]:
        """Check that column references in a node's params exist in its input schema."""
        checks: list[ValidationCheck] = []
        if not available_columns:
            return checks

        available_lower = {c.lower(): c for c in available_columns}

        # Extract column references based on node type
        referenced_cols: list[str] = []

        if step.type == StepType.FILTER:
            condition = step.params.get("condition", "")
            referenced_cols = _extract_column_refs(condition)

        elif step.type == StepType.AGGREGATE:
            group_by = step.params.get("group_by", [])
            if isinstance(group_by, str):
                group_by = [group_by]
            referenced_cols.extend(group_by)
            functions = step.params.get("functions", [])
            for f in functions:
                col = f.get("column", "")
                if col and col != "*":
                    referenced_cols.append(col)

        elif step.type == StepType.JOIN:
            join_key = step.params.get("join_key", [])
            if isinstance(join_key, str):
                join_key = [join_key]
            referenced_cols.extend(join_key)

        elif step.type == StepType.DEDUPLICATE:
            keys = step.params.get("key", [])
            if isinstance(keys, str):
                keys = [keys]
            referenced_cols.extend(keys)

        elif step.type == StepType.SORT:
            columns = step.params.get("columns", [])
            if isinstance(columns, str):
                columns = [columns]
            # Sort columns may have ASC/DESC suffix
            for col_spec in columns:
                col_name = col_spec.split()[0] if isinstance(col_spec, str) else str(col_spec)
                referenced_cols.append(col_name)

        elif step.type == StepType.RENAME:
            mapping = step.params.get("mapping", {})
            if isinstance(mapping, dict):
                referenced_cols.extend(mapping.keys())

        elif step.type == StepType.TYPECAST:
            casts = step.params.get("casts", {})
            if isinstance(casts, dict):
                referenced_cols.extend(casts.keys())

        elif step.type == StepType.TRANSFORM:
            expression = step.params.get("expression", "")
            referenced_cols = _extract_column_refs(expression)

        # Validate each referenced column
        for col in referenced_cols:
            if col.lower() in available_lower:
                checks.append(ValidationCheck(
                    step_id=step.id,
                    step_label=step.label or step.id,
                    check_type="column_exists",
                    passed=True,
                    message=f"Column '{col}' exists in input",
                    severity="info",
                ))
            else:
                suggestion_col = _suggest_column(col, available_columns)
                suggestion_msg = f'Did you mean "{suggestion_col}"?' if suggestion_col else None
                checks.append(ValidationCheck(
                    step_id=step.id,
                    step_label=step.label or step.id,
                    check_type="column_exists",
                    passed=False,
                    message=f"Column '{col}' not found in input columns [{', '.join(available_columns[:15])}{'...' if len(available_columns) > 15 else ''}]",
                    severity="warning",
                    suggestion=suggestion_msg,
                ))

        return checks

    # ------------------------------------------------------------------
    # Execute source nodes to get schema
    # ------------------------------------------------------------------

    def _execute_source_for_schema(
        self, step: Step, conn: duckdb.DuckDBPyConnection, data_dir: str,
    ) -> list[str] | None:
        """Execute a source step with preview_limit=1 to get output column names."""
        try:
            if step.type == StepType.CSV_SOURCE:
                file_path = step.params.get("file_path", "")
                if not file_path:
                    return None
                if not os.path.isabs(file_path):
                    file_path = os.path.join(data_dir, file_path)
                if not os.path.exists(file_path):
                    return None
                delimiter = step.params.get("delimiter", ",")
                header = step.params.get("header", True)
                rel = conn.read_csv(file_path, delimiter=delimiter, header=header)
                return rel.columns

            elif step.type == StepType.DB_SOURCE:
                # Can't execute real DB queries in validation -- return None
                return None

            elif step.type == StepType.API_SOURCE:
                return None

        except Exception:
            return None

        return None

    # ------------------------------------------------------------------
    # Estimate output schema for downstream propagation
    # ------------------------------------------------------------------

    def _estimate_output_schema(self, step: Step, input_cols: list[str]) -> list[str]:
        """Estimate what columns a node will output based on its type and params."""
        if step.type == StepType.FILTER:
            return input_cols  # Filter doesn't change columns

        elif step.type == StepType.AGGREGATE:
            group_by = step.params.get("group_by", [])
            if isinstance(group_by, str):
                group_by = [group_by]
            output = list(group_by)
            for f in step.params.get("functions", []):
                alias = f.get("alias", f"{f.get('function', 'agg')}_{f.get('column', 'col')}")
                output.append(alias)
            return output

        elif step.type == StepType.RENAME:
            mapping = step.params.get("mapping", {})
            if isinstance(mapping, dict):
                renamed = []
                for c in input_cols:
                    renamed.append(mapping.get(c, c))
                return renamed
            return input_cols

        elif step.type == StepType.DEDUPLICATE:
            return input_cols  # Same columns, fewer rows

        elif step.type == StepType.SORT:
            return input_cols

        elif step.type == StepType.DERIVED_COLUMN:
            new_col = step.params.get("name", "new_column")
            return input_cols + [new_col]

        elif step.type == StepType.JOIN:
            # Join merges columns from both inputs -- we already gathered all upstream cols
            return input_cols

        elif step.type == StepType.SAMPLE:
            return input_cols

        elif step.type == StepType.VALIDATE:
            return input_cols

        # For Transform, Output, and others: can't reliably predict
        return input_cols

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _suggest_file(self, missing_path: str) -> str | None:
        """Suggest similar files when one isn't found."""
        if not os.path.isdir(self.data_dir):
            return None

        files = []
        for f in os.listdir(self.data_dir):
            ext = os.path.splitext(f)[1].lower()
            if ext in {".csv", ".json", ".parquet", ".tsv", ".txt"}:
                files.append(f)

        if not files:
            return f"No data files found in {self.data_dir}"

        basename = os.path.basename(missing_path)
        matches = difflib.get_close_matches(basename, files, n=3, cutoff=0.4)
        if matches:
            return f"Did you mean: {', '.join(matches)}?"

        # Check for wrong extension
        name_no_ext = os.path.splitext(basename)[0]
        for f in files:
            if os.path.splitext(f)[0].lower() == name_no_ext.lower():
                return f'Did you mean "{f}"? (different file extension)'

        return f"Available files: {', '.join(files[:10])}"

    def _build_input_map(self, workflow: Workflow) -> dict[str, list[str]]:
        """Map each step to its input step IDs."""
        result: dict[str, list[str]] = {s.id: [] for s in workflow.steps}
        for conn in workflow.connections:
            if conn.to_step in result:
                result[conn.to_step].append(conn.from_step)
        return result

    def _build_output_map(self, workflow: Workflow) -> dict[str, list[str]]:
        """Map each step to its output step IDs."""
        result: dict[str, list[str]] = {s.id: [] for s in workflow.steps}
        for conn in workflow.connections:
            if conn.from_step in result:
                result[conn.from_step].append(conn.to_step)
        return result

    def _topological_sort(self, workflow: Workflow) -> list[Step]:
        """Sort steps in dependency order."""
        step_map = {s.id: s for s in workflow.steps}
        in_degree: dict[str, int] = {s.id: 0 for s in workflow.steps}
        adjacency: dict[str, list[str]] = {s.id: [] for s in workflow.steps}

        for conn in workflow.connections:
            if conn.from_step in adjacency and conn.to_step in in_degree:
                adjacency[conn.from_step].append(conn.to_step)
                in_degree[conn.to_step] += 1

        queue = [sid for sid, deg in in_degree.items() if deg == 0]
        order: list[Step] = []

        while queue:
            sid = queue.pop(0)
            order.append(step_map[sid])
            for neighbor in adjacency[sid]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        return order
