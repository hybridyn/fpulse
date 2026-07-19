"""
Intelligence API — schema detection, flattening, and pipeline suggestions.

Layer 1 of the F-Pulse Data Intelligence system.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Request
from pydantic import BaseModel, Field

from fpulse.auth.deps import current_workspace_id
from fpulse.intelligence.schema_detector import SchemaDetector, DetectedSchema
from fpulse.intelligence.flatten_engine import FlattenEngine, FlattenResult
from fpulse.intelligence.execution_intel import (
    ExecutionIntelligence, ExecutionConfig, RetryStrategy,
)
from fpulse.intelligence.pre_validator import PreValidator, PreValidationResult
from fpulse.intelligence.error_intel import ErrorIntelligence, ErrorAnalysis


router = APIRouter(prefix="/api/intelligence", tags=["intelligence"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_workspace_id(request: Request) -> str:
    """Wrap current_workspace_id so dep failures surface as readable
    HTTP errors — same pattern used by planner/templates/exports/logs."""
    try:
        return current_workspace_id(request)
    except HTTPException:
        raise
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception("workspace resolve failed")
        raise HTTPException(500, "workspace resolve failed") from exc


def get_data_dir() -> str:
    from fpulse.main import app_state
    return app_state["data_dir"]


def get_store():
    from fpulse.main import app_state
    return app_state["store"]


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class DetectSchemaRequest(BaseModel):
    """Request body for schema detection from raw data or path."""
    file_path: str | None = None
    raw_data: str | None = None
    source_type: str | None = None  # csv, json, xml — auto-detected if not given


class FlattenRequest(BaseModel):
    """Request body for flattening nested data."""
    file_path: str | None = None
    raw_data: str | None = None
    source_type: str | None = None
    sample_limit: int = 100


class SuggestPipelineRequest(BaseModel):
    """Request body for pipeline suggestion.

    Field is named `detected_schema` in Python (avoids shadowing the
    inherited BaseModel.schema() classmethod which Pydantic v2 warns
    about) but exposed as `"schema"` on the wire so the frontend
    payload format does not change.
    """
    # populate_by_name lets handlers do `req.detected_schema` AND lets
    # the JSON payload still send `{"schema": ...}` via the alias.
    model_config = {"populate_by_name": True}

    detected_schema: DetectedSchema = Field(alias="schema")


class PipelineSuggestion(BaseModel):
    """A suggested pipeline step."""
    step_type: str
    label: str
    reason: str
    params: dict[str, Any] = Field(default_factory=dict)
    order: int = 0


class PipelineSuggestionResponse(BaseModel):
    """Response with suggested pipeline steps."""
    suggestions: list[PipelineSuggestion]
    total_steps: int
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# POST /api/intelligence/detect-schema
# ---------------------------------------------------------------------------

@router.post("/detect-schema", response_model=DetectedSchema)
async def detect_schema(request: DetectSchemaRequest):
    """Detect schema from an uploaded file path or raw data.

    Accepts:
    - file_path: relative to data_dir or absolute path
    - raw_data: inline CSV/JSON/XML content
    - source_type: optional override (csv, json, xml)
    """
    detector = SchemaDetector(data_dir=get_data_dir())
    try:
        result = detector.detect(
            file_path=request.file_path,
            raw_data=request.raw_data,
            source_type=request.source_type,
        )
        return result
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (ValueError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("Schema detection failed")
        raise HTTPException(status_code=500, detail="Schema detection failed") from e


@router.post("/detect-schema/upload", response_model=DetectedSchema)
async def detect_schema_upload(
    file: UploadFile = File(...),
    source_type: str | None = Form(None),
):
    """Detect schema from a file upload (multipart form).

    Upload a CSV, JSON, or XML file and get the detected schema back.
    """
    if not file.filename:
        raise HTTPException(400, "No filename provided")

    content = await file.read()
    raw_data = content.decode("utf-8", errors="replace")

    # Infer source type from filename if not provided
    if not source_type:
        ext = os.path.splitext(file.filename)[1].lower()
        type_map = {".csv": "csv", ".tsv": "csv", ".json": "json", ".xml": "xml"}
        source_type = type_map.get(ext)

    detector = SchemaDetector(data_dir=get_data_dir())
    try:
        result = detector.detect(raw_data=raw_data, source_type=source_type)
        return result
    except (ValueError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("Schema detection failed")
        raise HTTPException(status_code=500, detail="Schema detection failed") from e


# ---------------------------------------------------------------------------
# POST /api/intelligence/flatten
# ---------------------------------------------------------------------------

@router.post("/flatten", response_model=FlattenResult)
async def flatten_data(request: FlattenRequest):
    """Flatten nested JSON/XML data into tabular form.

    Returns multiple tables if nested arrays are detected.
    """
    engine = FlattenEngine(
        data_dir=get_data_dir(),
        sample_limit=request.sample_limit,
    )
    try:
        result = engine.flatten(
            file_path=request.file_path,
            raw_data=request.raw_data,
            source_type=request.source_type,
        )
        return result
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (ValueError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("Flatten failed")
        raise HTTPException(status_code=500, detail="Flatten failed") from e


@router.post("/flatten/upload", response_model=FlattenResult)
async def flatten_upload(
    file: UploadFile = File(...),
    source_type: str | None = Form(None),
    sample_limit: int = Form(100),
):
    """Flatten a file upload (multipart form) into tabular form."""
    if not file.filename:
        raise HTTPException(400, "No filename provided")

    content = await file.read()
    raw_data = content.decode("utf-8", errors="replace")

    if not source_type:
        ext = os.path.splitext(file.filename)[1].lower()
        type_map = {".csv": "csv", ".tsv": "csv", ".json": "json", ".xml": "xml"}
        source_type = type_map.get(ext)

    engine = FlattenEngine(data_dir=get_data_dir(), sample_limit=sample_limit)
    try:
        result = engine.flatten(raw_data=raw_data, source_type=source_type)
        return result
    except (ValueError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("Flatten failed")
        raise HTTPException(status_code=500, detail="Flatten failed") from e


# ---------------------------------------------------------------------------
# POST /api/intelligence/suggest-pipeline
# ---------------------------------------------------------------------------

@router.post("/suggest-pipeline", response_model=PipelineSuggestionResponse)
async def suggest_pipeline(request: SuggestPipelineRequest):
    """Given a detected schema, suggest pipeline steps.

    Analyzes the schema and recommends appropriate source, transform,
    and output steps based on data characteristics.
    """
    schema = request.detected_schema  # renamed; wire format still 'schema'
    suggestions: list[PipelineSuggestion] = []
    notes: list[str] = []
    order = 0

    # --- Step 1: Source node ---
    order += 1
    if schema.source_type == "csv":
        suggestions.append(PipelineSuggestion(
            step_type="csv_source",
            label=f"Read {schema.source_type.upper()} Source",
            reason=f"Load the {schema.source_type.upper()} data ({schema.total_rows} rows, {schema.total_columns} columns)",
            params={"file_path": ""},
            order=order,
        ))
    elif schema.source_type in ("json", "xml"):
        suggestions.append(PipelineSuggestion(
            step_type="csv_source",
            label=f"Read {schema.source_type.upper()} Source",
            reason=f"Load the {schema.source_type.upper()} data. Consider flattening first if nested.",
            params={"file_path": ""},
            order=order,
        ))
        if schema.flatten_recommended:
            notes.append(
                f"Data has nesting depth {schema.nested_depth}. "
                f"Use the flatten endpoint first to convert to tabular form."
            )

    # --- Step 2: Deduplication if potential PKs exist ---
    if schema.suggested_primary_keys:
        order += 1
        pk = schema.suggested_primary_keys[0]
        suggestions.append(PipelineSuggestion(
            step_type="deduplicate",
            label=f"Deduplicate on '{pk}'",
            reason=f"Column '{pk}' has high uniqueness ({_find_col_ratio(schema, pk)}). Remove potential duplicates.",
            params={"columns": [pk]},
            order=order,
        ))

    # --- Step 3: Type casting for mismatched types ---
    date_cols = [c for c in schema.columns if c.detected_type == "date"]
    if date_cols:
        order += 1
        suggestions.append(PipelineSuggestion(
            step_type="typecast",
            label="Cast date columns",
            reason=f"Convert {len(date_cols)} column(s) with detected date patterns to proper DATE type",
            params={
                "casts": {
                    c.name: {"target_type": "DATE", "format": c.date_format}
                    for c in date_cols
                }
            },
            order=order,
        ))

    # --- Step 4: Handle nullable columns ---
    nullable_cols = [c for c in schema.columns if c.nullable and c.detected_type != "string"]
    if nullable_cols:
        order += 1
        suggestions.append(PipelineSuggestion(
            step_type="filter",
            label="Filter null rows",
            reason=f"{len(nullable_cols)} non-string column(s) have NULL values. Consider filtering or filling.",
            params={
                "condition": " AND ".join(f"{c.name} IS NOT NULL" for c in nullable_cols[:5])
            },
            order=order,
        ))

    # --- Step 5: Handle repeating groups ---
    if schema.repeating_groups:
        for rg in schema.repeating_groups:
            order += 1
            suggestions.append(PipelineSuggestion(
                step_type="transform",
                label=f"Normalize repeating group '{rg.pattern}'",
                reason=f"Detected {rg.count} repeating instances matching pattern '{rg.pattern}'. "
                       f"Consider unpivoting to normalize.",
                params={"pattern": rg.pattern, "fields": rg.fields},
                order=order,
            ))

    # --- Step 6: Nested data flattening ---
    if schema.flatten_recommended and schema.detected_tables and len(schema.detected_tables) > 1:
        for tbl in schema.detected_tables[1:]:  # Skip main table
            order += 1
            suggestions.append(PipelineSuggestion(
                step_type="transform",
                label=f"Extract nested table '{tbl.get('name', 'child')}'",
                reason=f"Nested structure detected with {tbl.get('row_count', '?')} rows. "
                       f"Extract as separate pipeline branch.",
                params={"table_name": tbl.get("name"), "columns": tbl.get("columns", [])},
                order=order,
            ))

    # --- Step 7: Output ---
    order += 1
    suggestions.append(PipelineSuggestion(
        step_type="output",
        label="Write Parquet Output",
        reason="Write cleaned data to Parquet format for efficient downstream consumption",
        params={"format": "parquet", "file_path": "output.parquet"},
        order=order,
    ))

    return PipelineSuggestionResponse(
        suggestions=suggestions,
        total_steps=len(suggestions),
        notes=notes,
    )


# ---------------------------------------------------------------------------
# GET /api/intelligence/analyze/{workflow_id}/step/{step_id}
# ---------------------------------------------------------------------------

@router.get("/analyze/{workflow_id}/step/{step_id}")
async def analyze_step_output(
    workflow_id: str,
    step_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Analyze a workflow step's output schema.

    Executes the step (with dependencies) and runs schema detection
    on the output data. Scoped to the caller's workspace — looking
    up another tenant's workflow returns 404 exactly like a genuinely
    missing record.
    """
    from fpulse.engine.executor import WorkflowExecutor

    store = get_store()
    v = store.get(workflow_id, workspace_id=workspace_id)
    if not v:
        raise HTTPException(404, "Workflow not found")

    wf = v.workflow
    step = next((s for s in wf.steps if s.id == step_id), None)
    if not step:
        raise HTTPException(404, f"Step '{step_id}' not found in workflow")

    executor = WorkflowExecutor(data_dir=get_data_dir())
    result = executor.execute_step(wf, step_id, preview_limit=200)

    if result.status == "error":
        raise HTTPException(
            400,
            f"Step execution failed: {result.error}",
        )

    # Build a DetectedSchema from the step result
    columns: list[dict] = []
    for col_info in result.schema_info:
        col_name = col_info.get("name", "")
        col_type = col_info.get("type", "VARCHAR")

        # Extract column values from sample data
        col_values = [row.get(col_name) for row in result.sample_data if col_name in row]
        non_null = [v for v in col_values if v is not None]
        unique_count = len(set(str(v) for v in non_null)) if non_null else 0
        unique_ratio = unique_count / max(len(non_null), 1)

        # Sample values
        samples = []
        seen = set()
        for v in non_null[:5]:
            sv = str(v)
            if sv not in seen:
                seen.add(sv)
                samples.append(v)

        columns.append({
            "name": col_name,
            "detected_type": _map_sql_type(col_type),
            "nullable": True,
            "unique_ratio": round(unique_ratio, 4),
            "sample_values": samples,
            "date_format": None,
        })

    # Check for suggested primary keys
    pk_candidates = [
        c["name"] for c in columns
        if c["unique_ratio"] > 0.95
        and c["detected_type"] in ("string", "integer")
        and result.row_count > 1
    ]

    return {
        "workflow_id": workflow_id,
        "step_id": step_id,
        "step_type": step.type.value if hasattr(step.type, "value") else str(step.type),
        "step_label": step.label,
        "schema": {
            "source_type": "step_output",
            "total_rows": result.row_count,
            "total_columns": len(columns),
            "columns": columns,
            "repeating_groups": [],
            "suggested_primary_keys": pk_candidates,
            "nested_depth": 0,
            "flatten_recommended": False,
            "detected_tables": [{
                "name": step.label or step_id,
                "columns": result.columns,
                "row_count": result.row_count,
            }],
        },
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_col_ratio(schema: DetectedSchema, col_name: str) -> str:
    """Find the unique ratio for a column and format it."""
    for c in schema.columns:
        if c.name == col_name:
            return f"{c.unique_ratio:.1%}"
    return "?"


# ---------------------------------------------------------------------------
# Execution Intelligence endpoints
# ---------------------------------------------------------------------------

class ExecutionConfigInput(BaseModel):
    """Optional execution config overrides."""
    retry_strategy: str = "exponential"
    max_retries: int = 3
    retry_delay_ms: int = 1000
    batch_size: int | None = None
    parallel_steps: bool = False
    timeout_ms: int = 300000


@router.post("/optimize/{workflow_id}")
async def optimize_execution(
    workflow_id: str,
    config: ExecutionConfigInput | None = None,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Analyze a workflow and create an optimized execution plan.

    Detects parallelizable phases, suggests batch sizes,
    and sets per-step timeouts based on step type. Scoped to the
    caller's workspace.
    """
    store = get_store()
    v = store.get(workflow_id, workspace_id=workspace_id)
    if not v:
        raise HTTPException(404, "Workflow not found")

    intel = ExecutionIntelligence()

    exec_config = None
    if config:
        exec_config = ExecutionConfig(
            retry_strategy=RetryStrategy(config.retry_strategy),
            max_retries=config.max_retries,
            retry_delay_ms=config.retry_delay_ms,
            batch_size=config.batch_size,
            parallel_steps=config.parallel_steps,
            timeout_ms=config.timeout_ms,
        )

    plan = intel.optimize_execution(v.workflow, config=exec_config)
    return plan.model_dump(mode="json")


@router.get("/estimate/{workflow_id}")
async def estimate_cost(
    workflow_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Estimate execution time and resources for a workflow.

    Returns per-step estimates, total duration, memory usage,
    and potential parallelism speedup. Scoped to the caller's
    workspace.
    """
    store = get_store()
    v = store.get(workflow_id, workspace_id=workspace_id)
    if not v:
        raise HTTPException(404, "Workflow not found")

    intel = ExecutionIntelligence()
    estimate = intel.estimate_cost(v.workflow)
    return estimate.model_dump(mode="json")


# ---------------------------------------------------------------------------
# POST /api/intelligence/pre-validate/{workflow_id}
# ---------------------------------------------------------------------------

@router.post("/pre-validate/{workflow_id}", response_model=PreValidationResult)
async def pre_validate_workflow(
    workflow_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Run pre-execution data-level validation on a workflow.

    Goes beyond structural validation to check:
    - Source file existence (CSV files, SQL syntax)
    - Inter-node schema compatibility (column references vs actual output)
    - Parameter completeness for every node
    - Output path validation
    - Connection completeness (every non-source has input, dead-end warnings)

    Scoped to the caller's workspace. Returns a detailed report with
    errors, warnings, suggestions, and a can_execute flag indicating
    whether the pipeline is safe to run.
    """
    store = get_store()
    v = store.get(workflow_id, workspace_id=workspace_id)
    if not v:
        raise HTTPException(404, "Workflow not found")

    data_dir = get_data_dir()
    validator = PreValidator(data_dir=data_dir)

    # Standard pre-validation
    result = validator.validate(v.workflow)

    # Inter-node schema validation (executes source nodes with preview_limit=1)
    connection_checks = validator.validate_node_connections(v.workflow, data_dir=data_dir)

    result.checks.extend(connection_checks)
    conn_errors = [c for c in connection_checks if not c.passed and c.severity == "error"]
    conn_warnings = [c for c in connection_checks if not c.passed and c.severity == "warning"]
    result.errors.extend(conn_errors)
    result.warnings.extend(conn_warnings)

    if conn_errors:
        result.can_execute = False
        result.valid = False
    elif conn_warnings:
        result.valid = False

    return result


# ---------------------------------------------------------------------------
# POST /api/intelligence/analyze-error
# ---------------------------------------------------------------------------

class AnalyzeErrorRequest(BaseModel):
    """Request body for error analysis."""
    error: str
    step_id: str | None = None
    step_type: str | None = None
    step_params: dict[str, Any] | None = None
    available_columns: list[str] | None = None


@router.post("/analyze-error", response_model=ErrorAnalysis)
async def analyze_error(request: AnalyzeErrorRequest):
    """Analyze a pipeline execution error and return smart suggestions.

    Categorizes the error, provides a human-friendly message,
    suggests fixes with confidence scores, and indicates whether
    an auto-fix is available.

    Categories: missing_object, schema_mismatch, permission, syntax,
    connection, data_type, timeout, resource, unknown
    """
    data_dir = get_data_dir()
    intel = ErrorIntelligence(data_dir=data_dir)

    analysis = intel.analyze(
        error=request.error,
        step_id=request.step_id,
        step_type=request.step_type,
        step_params=request.step_params,
        available_columns=request.available_columns,
    )
    return analysis


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _map_sql_type(sql_type: str) -> str:
    """Map a SQL type string to our canonical types."""
    t = sql_type.upper()
    if "INT" in t:
        return "integer"
    if "FLOAT" in t or "DOUBLE" in t or "DECIMAL" in t or "NUMERIC" in t:
        return "float"
    if "BOOL" in t:
        return "boolean"
    if "DATE" in t or "TIMESTAMP" in t or "TIME" in t:
        return "date"
    if "STRUCT" in t or "MAP" in t:
        return "nested_object"
    if "LIST" in t or t.endswith("[]"):
        return "nested_array"
    return "string"
