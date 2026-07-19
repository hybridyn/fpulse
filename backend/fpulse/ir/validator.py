"""IR validation — ensures workflows are structurally sound before execution."""

from __future__ import annotations

from typing import Callable

from .schema import Workflow, StepType


class ValidationError:
    def __init__(self, step_id: str, message: str, severity: str = "error"):
        self.step_id = step_id
        self.message = message
        self.severity = severity  # error | warning

    def dict(self):
        return {"step_id": self.step_id, "message": self.message, "severity": self.severity}


# Capability requirements per step type. Source-shaped nodes need a
# connection that can `read`; sink-shaped nodes need `write`. Anything
# not in either set (HTTP_REQUEST, EXECUTE_SQL_TASK, REST/SAAS_CONNECTOR,
# COPY_DATA, etc.) is direction-agnostic and skipped — these can legitimately
# point at a connection used in either role.
_READ_REQUIRED_TYPES: frozenset[StepType] = frozenset({
    StepType.SOURCE,
    StepType.CSV_SOURCE, StepType.DB_SOURCE, StepType.API_SOURCE,
    StepType.JSON_SOURCE, StepType.PARQUET_SOURCE, StepType.EXCEL_SOURCE,
    StepType.XML_SOURCE, StepType.S3_SOURCE, StepType.KAFKA_SOURCE,
    StepType.FTP_SOURCE, StepType.GSHEET_SOURCE, StepType.DELTA_SOURCE,
    StepType.ADLS_GEN2_SOURCE, StepType.AZURE_BLOB_SOURCE, StepType.GCS_SOURCE,
    StepType.FILE_SOURCE,
    StepType.SHAREPOINT_SOURCE, StepType.ONEDRIVE_SOURCE, StepType.GDRIVE_SOURCE,
    StepType.DROPBOX_SOURCE, StepType.BOX_SOURCE,
    StepType.JDBC_SOURCE, StepType.CDC_SOURCE, StepType.OPENAPI_SOURCE,
    StepType.VECTOR_SOURCE,
})

_WRITE_REQUIRED_TYPES: frozenset[StepType] = frozenset({
    StepType.DESTINATION, StepType.OUTPUT,
    StepType.DB_SINK, StepType.CSV_SINK, StepType.JSON_SINK, StepType.EXCEL_SINK,
    StepType.S3_SINK, StepType.KAFKA_SINK, StepType.API_SINK, StepType.WEBHOOK_SINK,
    StepType.EMAIL_SINK, StepType.DELTA_SINK, StepType.WAREHOUSE_SINK,
    StepType.ADLS_GEN2_SINK, StepType.AZURE_BLOB_SINK, StepType.GCS_SINK,
    StepType.FILE_SINK,
    StepType.SHAREPOINT_SINK, StepType.ONEDRIVE_SINK, StepType.GDRIVE_SINK,
    StepType.DROPBOX_SINK, StepType.BOX_SINK,
    StepType.JDBC_SINK, StepType.VECTOR_SINK,
    StepType.SEND_EMAIL, StepType.SLACK_NOTIFY,
})


def validate_capabilities(
    workflow: Workflow,
    get_connection: Callable[[str], object | None],
) -> list[ValidationError]:
    """Refuse a save when a source node points at a write-only connection
    (or vice versa). Lazy-fetches each unique connection_id at most once
    per call so a 200-step pipeline costs N unique-lookup queries, not
    200 scans. Empty / missing capabilities array on the connection is
    treated as both (legacy row, pre-Apr-22 schema) — keeps existing
    pipelines saveable until the user re-tags those rows.
    """
    errors: list[ValidationError] = []
    cache: dict[str, object | None] = {}

    for step in workflow.steps:
        required: str | None = None
        if step.type in _READ_REQUIRED_TYPES:
            required = "read"
        elif step.type in _WRITE_REQUIRED_TYPES:
            required = "write"
        if required is None:
            continue

        conn_id = (step.params or {}).get("connection_id")
        if not conn_id:
            continue

        if conn_id not in cache:
            cache[conn_id] = get_connection(conn_id)
        conn = cache[conn_id]
        if conn is None:
            continue  # connection-missing is a separate concern

        caps = getattr(conn, "capabilities", None) or []
        if not caps:
            continue  # legacy row — both directions allowed
        if required in caps:
            continue

        name = getattr(conn, "name", conn_id)
        ctype = getattr(conn, "type", "?")
        errors.append(ValidationError(
            step.id,
            f"Step uses connection '{name}' ({ctype}) which lacks "
            f"'{required}' capability — pick a connection that supports "
            f"{required}, or enable {required} on this connection.",
        ))

    return errors


def validate_workflow(workflow: Workflow) -> list[ValidationError]:
    """Validate a workflow IR. Returns list of errors (empty = valid)."""
    errors: list[ValidationError] = []
    step_ids = {s.id for s in workflow.steps}

    # Check: at least one step
    if not workflow.steps:
        errors.append(ValidationError("", "Workflow has no steps"))
        return errors

    # Check: connections reference valid steps
    for conn in workflow.connections:
        if conn.from_step not in step_ids:
            errors.append(ValidationError(conn.from_step, f"Connection references unknown step '{conn.from_step}'"))
        if conn.to_step not in step_ids:
            errors.append(ValidationError(conn.to_step, f"Connection references unknown step '{conn.to_step}'"))

    # 2026-05-22 (audit R1): source/destination contract.
    # File-shaped connectors (csv / json / parquet / excel / xml)
    # accept a saved connection OR an inline file_path. Network
    # connectors (rest_api / database / s3 / azure_blob / gcs /
    # kafka / sharepoint / ...) REQUIRE a saved connection_id.
    # The inline-url escape hatch was deliberately removed so the
    # Connections page stays the single source of truth for
    # network-side credentials, audit, and env scoping.
    _FILE_CONNECTORS: set[str] = {"csv", "json", "parquet", "excel", "xml"}
    _FILE_INPUT_KEYS: tuple[str, ...] = ("file_path", "dataset_id", "file_id")

    # Check: each step has required params
    for step in workflow.steps:
        if step.type == StepType.SOURCE:
            connector = (step.params.get("connector_type") or "").strip().lower()
            if not connector:
                errors.append(ValidationError(step.id, "Source requires 'connector_type' (pick a connector in the config panel)"))
            elif connector in _FILE_CONNECTORS:
                has_conn = bool(step.params.get("connection_id"))
                has_file = any(step.params.get(k) for k in _FILE_INPUT_KEYS)
                if not has_conn and not has_file:
                    errors.append(ValidationError(
                        step.id,
                        f"Source ({connector}) requires either a 'connection_id' or a file input "
                        f"(file_path / dataset_id / file_id).",
                    ))
            else:
                # Network connector — connection_id required.
                if not step.params.get("connection_id"):
                    errors.append(ValidationError(
                        step.id,
                        f"Source ({connector}) requires a 'connection_id'. "
                        f"Network sources must reference a saved Connection — "
                        f"inline URL/credentials are no longer accepted. "
                        f"Create a connection on the Connections page and select it here.",
                    ))

        elif step.type == StepType.DESTINATION:
            connector = (step.params.get("connector_type") or "").strip().lower()
            if not connector:
                errors.append(ValidationError(step.id, "Destination requires 'connector_type' (pick a connector in the config panel)"))
            elif connector in _FILE_CONNECTORS:
                has_conn = bool(step.params.get("connection_id"))
                has_file = any(step.params.get(k) for k in _FILE_INPUT_KEYS)
                if not has_conn and not has_file:
                    errors.append(ValidationError(
                        step.id,
                        f"Destination ({connector}) requires either a 'connection_id' or a file output path.",
                    ))
            else:
                if not step.params.get("connection_id"):
                    errors.append(ValidationError(
                        step.id,
                        f"Destination ({connector}) requires a 'connection_id'. "
                        f"Network destinations must reference a saved Connection.",
                    ))

        elif step.type == StepType.CSV_SOURCE:
            if "file_path" not in step.params:
                errors.append(ValidationError(step.id, "CSV Source requires 'file_path' parameter"))

        elif step.type == StepType.DB_SOURCE:
            if "query" not in step.params:
                errors.append(ValidationError(step.id, "Database Source requires 'query' parameter"))

        elif step.type == StepType.FILTER:
            if "condition" not in step.params:
                errors.append(ValidationError(step.id, "Filter requires 'condition' parameter"))

        elif step.type == StepType.TRANSFORM:
            if "expression" not in step.params:
                errors.append(ValidationError(step.id, "Transform requires 'expression' parameter"))

        elif step.type == StepType.DEDUPLICATE:
            # Accept 'columns' as an alias for 'key' — the node's label is
            # "which columns make a row unique", so API callers naturally
            # pass 'columns'. The node normalizes both (deduplicate.py).
            if "key" not in step.params and "columns" not in step.params:
                errors.append(ValidationError(step.id, "Deduplicate requires 'key' (or 'columns') parameter"))

        elif step.type == StepType.AGGREGATE:
            # 2026-05-22: dropped the `group_by` requirement — backend
            # treats an empty group_by as a global aggregate (one row
            # per pipeline), which is a legitimate use case
            # (count-all-rows, sum-everything). See aggregate.py:49 +
            # the module docstring "No GROUP BY = global aggregation."
            if "functions" not in step.params:
                errors.append(ValidationError(step.id, "Aggregate requires 'functions' parameter"))

        elif step.type == StepType.JOIN:
            if "join_key" not in step.params:
                errors.append(ValidationError(step.id, "Join requires 'join_key' parameter"))

        elif step.type == StepType.OUTPUT:
            if "format" not in step.params:
                errors.append(ValidationError(step.id, "Output requires 'format' parameter"))

        # 2026-05-22 — expanded per-node required-param coverage. The
        # frontend has been carrying these checks alone (validateWorkflow.ts)
        # but agent drafts + template imports + programmatic creates bypass
        # the editor — so the same rules need a backend mirror to catch
        # malformed saves at the chokepoint.
        elif step.type == StepType.DERIVED_COLUMN:
            if not step.params.get("columns"):
                errors.append(ValidationError(step.id, "Derived Column requires 'columns' parameter"))

        elif step.type == StepType.DATA_WRANGLER:
            # `steps` is the list of sub-operations (filter/select/rename/...).
            # Empty steps is allowed (pass-through) but missing key is a
            # malformed save.
            if "steps" not in step.params:
                errors.append(ValidationError(step.id, "Data Wrangler requires 'steps' parameter"))

        elif step.type == StepType.PIVOT:
            if not step.params.get("pivot_column"):
                errors.append(ValidationError(step.id, "Pivot requires 'pivot_column' parameter"))
            if not step.params.get("value_column"):
                errors.append(ValidationError(step.id, "Pivot requires 'value_column' parameter"))

        elif step.type == StepType.UNPIVOT:
            if not step.params.get("columns"):
                errors.append(ValidationError(step.id, "Unpivot requires 'columns' parameter"))

        elif step.type == StepType.SCD2:
            if not step.params.get("business_key"):
                errors.append(ValidationError(step.id, "SCD2 requires 'business_key' parameter"))

        elif step.type == StepType.SET_VARIABLE:
            if not step.params.get("variables"):
                errors.append(ValidationError(step.id, "Set Variable requires 'variables' parameter"))

        elif step.type == StepType.EXECUTE_PIPELINE:
            if not step.params.get("pipeline_id"):
                errors.append(ValidationError(step.id, "Execute Pipeline requires 'pipeline_id' parameter"))

        elif step.type == StepType.CODE_SCRIPT:
            if not step.params.get("code"):
                errors.append(ValidationError(step.id, "Code Script requires 'code' parameter"))

        elif step.type == StepType.SEND_EMAIL:
            if not step.params.get("to"):
                errors.append(ValidationError(step.id, "Send Email requires 'to' parameter"))

        elif step.type == StepType.SLACK_NOTIFY:
            # Slack accepts either a saved webhook_url or a channel +
            # bot token (looked up at execute time). Require at least
            # one of the two addressing keys.
            if not step.params.get("webhook_url") and not step.params.get("channel"):
                errors.append(ValidationError(
                    step.id, "Slack Notify requires 'webhook_url' or 'channel' parameter"
                ))

    # Check: join nodes must have exactly 2 inputs (not less, not more).
    # 2026-05-22: also catch >2 — extras were silently dropped at
    # runtime which made debugging "missing rows" downstream painful.
    for step in workflow.steps:
        if step.type == StepType.JOIN:
            inputs = [c for c in workflow.connections if c.to_step == step.id]
            if len(inputs) < 2:
                errors.append(ValidationError(step.id, "Join node requires exactly 2 input connections"))
            elif len(inputs) > 2:
                errors.append(ValidationError(
                    step.id,
                    f"Join node accepts exactly 2 input connections (has {len(inputs)}). "
                    "Insert a Union upstream to combine the extras, or remove them — "
                    "otherwise the extras are silently dropped at runtime.",
                ))

    # Check: no cycles (simple DFS)
    adjacency: dict[str, list[str]] = {s.id: [] for s in workflow.steps}
    for conn in workflow.connections:
        if conn.from_step in adjacency:
            adjacency[conn.from_step].append(conn.to_step)

    visited: set[str] = set()
    in_stack: set[str] = set()

    def has_cycle(node: str) -> bool:
        visited.add(node)
        in_stack.add(node)
        for neighbor in adjacency.get(node, []):
            if neighbor not in visited:
                if has_cycle(neighbor):
                    return True
            elif neighbor in in_stack:
                return True
        in_stack.discard(node)
        return False

    for sid in step_ids:
        if sid not in visited:
            if has_cycle(sid):
                errors.append(ValidationError("", "Workflow contains a cycle"))
                break

    return errors
