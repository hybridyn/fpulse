"""Execution API — run workflows and individual steps.

Execution is CPU-heavy (DuckDB) and must NOT run on the async event-loop.
All blocking work is dispatched to a worker thread via ``anyio.to_thread``.
A concurrency semaphore (sized from ``runtime_config.MAX_CONCURRENT_RUNS``)
limits how many pipelines can run at once so a burst of requests cannot OOM
the single-node process.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request

from fpulse import runtime_config
from fpulse.auth.deps import current_workspace_id, require_auth
from fpulse.engine.executor import WorkflowExecutor
from fpulse.engine.resource_monitor import ResourceMonitor
from fpulse.ir.schema import StepRunResult
from fpulse.monitoring.store import ExecutionRecord, StepLog
from fpulse.intelligence.pre_validator import PreValidator
from fpulse.intelligence.error_intel import ErrorIntelligence

logger = logging.getLogger(__name__)

# Every execution route requires an authenticated user. Running a pipeline
# resolves configured connections/credentials and writes to sinks, so it must
# never be reachable anonymously — not even on a loopback bind. The public
# trigger surface for *published* pipelines lives in api/gateway.py and is
# gated there by its own per-endpoint API key.
router = APIRouter(
    prefix="/api/execute",
    tags=["execution"],
    dependencies=[Depends(require_auth)],
)

# ── Concurrency gate ────────────────────────────────────────────────────
# 0 = unlimited (no semaphore). Any positive value caps parallel runs.
_MAX = runtime_config.MAX_CONCURRENT_RUNS
_run_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore | None:
    """Lazy-init the semaphore on the running event loop."""
    global _run_semaphore
    if _MAX <= 0:
        return None
    if _run_semaphore is None:
        _run_semaphore = asyncio.Semaphore(_MAX)
    return _run_semaphore


def _safe_workspace_id(request: Request) -> str:
    try:
        return current_workspace_id(request)
    except HTTPException:
        raise
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception("workspace resolve failed")
        raise HTTPException(500, "workspace resolve failed") from exc


def get_store():
    from fpulse.main import app_state
    return app_state["store"]


def get_data_dir():
    from fpulse.main import app_state
    return app_state["data_dir"]


def get_execution_store():
    from fpulse.main import app_state
    return app_state["execution_store"]


@router.post("/workflow/{workflow_id}/pre-validate")
async def pre_validate_workflow(
    workflow_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Run pre-execution validation on a workflow.

    Performs data-level checks beyond structural validation:
    - Source file existence
    - SQL syntax validation
    - Parameter completeness
    - Connection completeness
    - Output path validation
    - Inter-node schema compatibility (column references)
    """
    v = get_store().get(workflow_id, workspace_id=workspace_id)
    if not v:
        raise HTTPException(404, "Workflow not found")

    data_dir = get_data_dir()
    validator = PreValidator(data_dir=data_dir)

    # Run standard pre-validation
    result = validator.validate(v.workflow)

    # Run inter-node schema validation (executes source nodes with preview_limit=1)
    connection_checks = validator.validate_node_connections(v.workflow, data_dir=data_dir)

    # Merge connection checks into the result
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

    return result.model_dump(mode="json")


from pydantic import BaseModel, Field as _Field


class RunWorkflowBody(BaseModel):
    """Optional body for POST /workflow/{id}.

    Lets API callers pass per-run parameter values. Empty body or missing
    fields are tolerated — the legacy query-string-only invocation still
    works for backward compatibility.
    """
    parameter_values: dict[str, Any] = _Field(default_factory=dict)


class EphemeralRunBody(BaseModel):
    """Body for POST /workflow/ephemeral — execute an unsaved workflow IR.

    The full Workflow IR is sent in the body. The server validates it,
    looks up connections by ID against the caller's workspace, then runs
    the pipeline WITHOUT persisting the workflow to the store. Use this
    for canvas Run/Sample buttons before the user clicks Save — this
    honours the no-silent-create rule (2026-05-09) by ensuring Save
    remains the only path that creates a row in the Pipelines list.

    2026-05-26 — No ExecutionRecord is written for ephemeral runs. The
    Executions page is reserved for saved-pipeline runs only. The editor
    receives all step results in the response body for its canvas
    feedback panel; nothing is persisted to the execution log.
    """
    workflow: dict[str, Any] = _Field(...)
    preview_limit: int = 50
    full_run: bool = False
    safety_mode: str = "live"  # live | sample | dry_run | validate_only
    environment: str = "dev"
    parameter_values: dict[str, Any] = _Field(default_factory=dict)


# IMPORTANT: this route MUST be declared before /workflow/{workflow_id}
# so FastAPI matches the literal "ephemeral" path before treating it as a
# workflow_id path-parameter value.
@router.post("/workflow/ephemeral")
async def run_workflow_ephemeral(
    body: EphemeralRunBody,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Execute a workflow IR straight from the request body — no persistence.

    Mirrors the response shape of POST /workflow/{workflow_id} so the
    frontend Run/Sample handlers can swap endpoints without changing
    downstream rendering logic.

    Validation is strict: if ``validate_workflow`` returns any ``severity=
    "error"`` items, this endpoint returns 400 with the full error list
    before attempting execution.
    """
    # ── 1. Parse the IR ─────────────────────────────────────────────────
    from fpulse.ir.schema import StepRunResult, Workflow
    try:
        wf = Workflow(**body.workflow)
    except Exception as exc:
        raise HTTPException(400, {
            "code": "invalid_workflow_ir",
            "message": f"Could not parse workflow IR: {exc}",
        })

    # Always stamp the caller's workspace — never trust body.workflow.workspace_id.
    wf.workspace_id = workspace_id

    # ── 2. Structural validation (the "X steps valid" gate) ─────────────
    from fpulse.ir.validator import validate_workflow as _validate_workflow
    val_items = _validate_workflow(wf)
    fatal = [v for v in val_items if getattr(v, "severity", "error") == "error"]
    warns = [v for v in val_items if getattr(v, "severity", "error") == "warning"]

    if fatal:
        raise HTTPException(400, {
            "code": "validation_failed",
            "message": f"{len(fatal)} validation error(s) — fix the canvas before running.",
            "errors": [e.dict() for e in fatal],
            "warnings": [w.dict() for w in warns],
        })

    # ── 3. Safety mode normalisation ────────────────────────────────────
    safety_mode = (body.safety_mode or "live").lower()
    if safety_mode not in {"live", "sample", "dry_run", "validate_only"}:
        safety_mode = "live"

    full_run = bool(body.full_run)
    if safety_mode == "sample":
        full_run = False

    # ── 4. dry_run / validate_only short-circuit (no execution) ────────
    if safety_mode in ("dry_run", "validate_only"):
        connection_issues: list[dict] = []
        if safety_mode == "dry_run":
            try:
                from fpulse.connections.store import get_store as _conn_store
                conn_store = _conn_store()
                seen: set[str] = set()
                for s in (getattr(wf, "steps", []) or []):
                    cref = (getattr(s, "params", {}) or {}).get("connection_id")
                    if not cref or cref in seen:
                        continue
                    seen.add(cref)
                    found = conn_store.get(cref, workspace_id=workspace_id)
                    if not found:
                        connection_issues.append({
                            "step_id": getattr(s, "id", ""),
                            "connection_id": cref,
                            "message": f"Connection {cref!r} not found in workspace",
                        })
            except Exception:
                pass
        return {
            "status": "validated" if not connection_issues else "invalid",
            "safety_mode": safety_mode,
            "ephemeral": True,
            "validator": {
                "errors": [],
                "warnings": [w.dict() for w in warns],
            },
            "connection_issues": connection_issues,
            "step_results": {},
            "message": (
                f"{safety_mode}: no issues found" if not connection_issues
                else f"{safety_mode}: {len(connection_issues)} connection issue(s)"
            ),
        }

    # ── 5. Real execution ───────────────────────────────────────────────
    # 2026-05-26 — user feedback: ephemeral runs (unsaved drafts /
    # imported pipelines being tested before Save) must NOT be persisted
    # to the execution log. The Executions page is for saved-pipeline
    # runs only. We still execute the workflow and return the full
    # step_results inline (the editor reads from the response body for
    # its run feedback panel) — we just don't create or record an
    # ExecutionRecord. As a side-effect this removes the "API / Webhook"
    # row that previously appeared in the Executions list after every
    # canvas Run on a draft.
    import uuid as _uuid
    eff_id = wf.id or f"ephemeral_{_uuid.uuid4().hex[:8]}"
    # run_id is needed by the executor for step-IO persistence + realtime
    # event broadcasting. Generate a fresh UUID per run.
    run_id = _uuid.uuid4().hex
    data_dir = get_data_dir()

    start = time.time()
    from fpulse.main import app_state as _app_state
    executor = WorkflowExecutor(data_dir=data_dir, app_state=_app_state)
    error_intel = ErrorIntelligence(data_dir=data_dir)

    sem = _get_semaphore()
    if sem is not None:
        if sem._value == 0:
            logger.info(
                "execute (ephemeral): all %d slots busy — queueing draft '%s'",
                _MAX, wf.name or eff_id,
            )
        await sem.acquire()

    try:
        SAMPLE_ROWS = 100
        sample_kwargs: dict = {}
        if safety_mode == "sample":
            sample_kwargs = {
                "sandbox_namespace": f"sample_{eff_id}",
                "sandbox_row_limit": SAMPLE_ROWS,
            }

        from fpulse.security.execution_codes import mint_for_run
        _eph_code = mint_for_run(wf, job_run_id=run_id)
        with ResourceMonitor() as _resmon:
            result = await anyio.to_thread.run_sync(
                lambda: executor.execute_workflow(
                    wf, preview_limit=body.preview_limit, full_run=full_run,
                    parameter_values=body.parameter_values or None,
                    run_id=run_id,
                    execution_code=_eph_code,
                    **sample_kwargs,
                )
            )

        # Error-intelligence enrichment — runs per failed step so the
        # editor can show actionable hints even though the run isn't
        # persisted.
        error_analyses: dict[str, Any] = {}
        for step in wf.steps:
            step_result = result.step_results.get(step.id)
            if step_result and step_result.status == "error" and step_result.error:
                analysis = error_intel.analyze(
                    error=step_result.error,
                    step_id=step.id,
                    step_type=step.type.value if hasattr(step.type, "value") else str(step.type),
                    step_params=step.params,
                    available_columns=step_result.columns or None,
                )
                error_analyses[step.id] = analysis.model_dump(mode="json")

        response = result.model_dump(mode="json")
        if error_analyses:
            response["error_intelligence"] = error_analyses
        response["ephemeral"] = True
        response["execution_id"] = run_id
        response["validator"] = {
            "errors": [],
            "warnings": [w.dict() for w in warns],
        }
        return response
    finally:
        if sem is not None:
            sem.release()


@router.post("/workflow/{workflow_id}")
async def run_workflow(
    workflow_id: str,
    request: Request,
    preview_limit: int = 50,
    full_run: bool = False,
    environment: str = "dev",
    safety_mode: str = "live",
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Execute all steps of a workflow — workspace-scoped.

    ``full_run=True`` skips the dev sample-mode row limit and processes
    the entire dataset.  The default (False) limits source nodes to
    ``DEV_SAMPLE_ROWS`` rows in dev mode for fast iteration.

    ``safety_mode`` (Copilot pre-run banner integration):
      - ``live``           — normal run (default)
      - ``sample``         — forces ``full_run=False`` regardless of the param
      - ``dry_run``        — validate IR + connections, skip step execution
      - ``validate_only``  — IR validator only, no connection touching

    Optional JSON body (pipeline-parameter passing):
      {
        "parameter_values": {
          "dataset": "orders_2026_04",
          "batch_size": 5000,
          "run_date": "2026-04-30"
        }
      }
    Values must match the names declared in ``workflow.parameters``;
    unknown keys raise 400, type-coercion failures raise 400, missing
    required parameters raise 400.
    """
    # Best-effort body parse — empty body / missing Content-Type both fine.
    parameter_values: dict[str, Any] = {}
    try:
        if request.headers.get("content-length") and request.headers.get("content-type", "").startswith("application/json"):
            raw = await request.json()
            if isinstance(raw, dict):
                pv = raw.get("parameter_values") or raw.get("parameters")
                if isinstance(pv, dict):
                    parameter_values = pv
    except Exception:
        # Malformed body shouldn't kill the endpoint — fall through with no
        # overrides. Validation will catch missing-required cases.
        parameter_values = {}

    v = get_store().get(workflow_id, workspace_id=workspace_id)
    if not v:
        raise HTTPException(404, "Workflow not found")

    wf = v.workflow

    # --- Safety mode short-circuits -----------------------------------------
    safety_mode = (safety_mode or "live").lower()
    if safety_mode not in {"live", "sample", "dry_run", "validate_only"}:
        safety_mode = "live"

    if safety_mode == "sample":
        # User asked for sample mode — force the dev row cap.
        full_run = False

    if safety_mode in ("dry_run", "validate_only"):
        # Both modes return without executing any steps. validate_only is
        # the lightest — IR structure check only. dry_run also probes that
        # connections referenced by the IR resolve in this workspace.
        from fpulse.ir.validator import validate_workflow as _validate_workflow
        try:
            errors, warnings = _validate_workflow(wf)
        except Exception as e:
            errors = [{"step_id": "", "message": f"Validator crashed: {e}", "severity": "error"}]
            warnings = []

        connection_issues: list[dict] = []
        if safety_mode == "dry_run":
            try:
                from fpulse.connections.store import get_store as _conn_store
                conn_store = _conn_store()
                seen_conn_ids: set[str] = set()
                for s in (getattr(wf, "steps", []) or []):
                    cref = (getattr(s, "params", {}) or {}).get("connection_id")
                    if not cref or cref in seen_conn_ids:
                        continue
                    seen_conn_ids.add(cref)
                    found = conn_store.get(cref, workspace_id=workspace_id)
                    if not found:
                        connection_issues.append({
                            "step_id": getattr(s, "id", ""),
                            "connection_id": cref,
                            "message": f"Referenced connection {cref!r} not found in this workspace",
                        })
            except Exception:
                pass

        return {
            "status": "validated" if not errors else "invalid",
            "safety_mode": safety_mode,
            "validator": {"errors": errors, "warnings": warnings},
            "connection_issues": connection_issues,
            "step_results": {},
            "message": (
                f"{safety_mode}: {'no issues found' if not errors and not connection_issues else 'issues found'}"
            ),
        }
    exe_store = get_execution_store()
    data_dir = get_data_dir()

    # --- Overlap detection ---
    # Check execution settings from workflow metadata
    metadata = getattr(wf, "metadata", {}) or {}
    exec_settings = metadata.get("execution_settings", {})
    overlap_policy = exec_settings.get("overlap_policy", "parallel")
    enable_overlap = exec_settings.get("enable_overlap_detection", False)
    max_runtime_min = exec_settings.get("max_runtime_minutes", 0)

    if enable_overlap and overlap_policy != "parallel":
        running = [
            e for e in exe_store.list_by_workflow(workflow_id, workspace_id=workspace_id)
            if (e.get("status") if isinstance(e, dict) else getattr(e, "status", None)) == "running"
        ]
        if running:
            first = running[0]
            first_started = first.get("started_at") if isinstance(first, dict) else getattr(first, "started_at", "")
            first_id = first.get("id") if isinstance(first, dict) else getattr(first, "id", "")
            if overlap_policy == "skip":
                return {
                    "status": "skipped",
                    "message": f"Skipped: previous execution still running (started {first_started})",
                    "overlap_detected": True,
                    "running_execution_id": first_id,
                    "step_results": {},
                }
            elif overlap_policy == "queue":
                return {
                    "status": "queued",
                    "message": "Queued: waiting for running execution to complete",
                    "overlap_detected": True,
                    "running_execution_id": first_id,
                    "step_results": {},
                }
            # cancel_previous: mark previous as cancelled, continue
            elif overlap_policy == "cancel_previous":
                for r in running:
                    rid = r.get("id") if isinstance(r, dict) else getattr(r, "id", "")
                    exe_store.update(
                        rid,
                        {"status": "cancelled", "completed_at": datetime.now(timezone.utc)},
                        workspace_id=workspace_id,
                    )

    # Create execution record — stamped with caller's workspace_id so
    # the history is correctly attributed. The workflow's own
    # workspace_id would also work, but the caller's is the correct
    # audit identity (same as every other write in the request).
    exe = ExecutionRecord(
        workflow_id=workflow_id,
        workflow_name=wf.name,
        project_id=getattr(wf, "project_id", "default"),
        workspace_id=workspace_id,
        steps_total=len(wf.steps),
        workflow_snapshot=wf.model_dump(mode="json"),
    )

    start = time.time()
    from fpulse.main import app_state as _app_state
    executor = WorkflowExecutor(data_dir=data_dir, app_state=_app_state)
    error_intel = ErrorIntelligence(data_dir=data_dir)

    # ── Record in worker pool for live tracking ──
    # The pool tracks the job for the admin Execution Pool page.
    # Actual execution still uses the semaphore + anyio dispatch below.
    _pool = None
    _pool_job = None
    try:
        from fpulse.main import app_state as _as
        _pool = _as.get("worker_pool")
        if _pool:
            metadata = getattr(wf, "metadata", {}) or {}
            priority = metadata.get("priority", 3)
            from fpulse.engine.worker_pool import QueuedJob
            _pool_job = QueuedJob(
                id=exe.id,
                workflow_id=workflow_id,
                workflow_name=wf.name,
                project_id=getattr(wf, "project_id", "default"),
                workspace_id=workspace_id,
                environment=environment,
                priority=min(max(priority, 1), 5),
                triggered_by="manual",
            )
            _pool._lock.acquire()
            _pool._total_submitted += 1
            worker = _pool._find_free_worker()
            if worker:
                worker.status = "busy"
                worker.current_job_id = _pool_job.id
                worker.current_workflow_id = _pool_job.workflow_id
                worker.current_workflow_name = _pool_job.workflow_name
                worker.current_priority = _pool_job.priority
                worker.current_environment = _pool_job.environment
                from datetime import datetime as _dt, timezone as _tz
                worker.started_at = _dt.now(_tz.utc)
                _pool._active[_pool_job.id] = _pool_job
                _pool_job._kwargs["_worker"] = worker
            _pool._lock.release()
    except Exception:
        pass

    # Acquire the concurrency gate — if all slots are taken, callers
    # wait here instead of launching a 3rd / 4th heavy DuckDB run.
    sem = _get_semaphore()
    if sem is not None:
        if sem._value == 0:
            logger.info(
                "execute: all %d slots busy — queueing workflow %s",
                _MAX, workflow_id,
            )
        await sem.acquire()

    try:
        # DuckDB is CPU-bound → run off the async event loop so the API
        # thread can still serve /health, /api/auth, and WebSocket frames
        # while the pipeline is crunching data.
        # Wrap the run with a 1Hz resource sampler. Captures peak RSS and
        # cumulative CPU seconds for the process during the pipeline. Cheap;
        # falls through to zeros if psutil isn't installed. Stored on the
        # execution log so the UI can show usage and threshold alerts can
        # fire on the post-run check.
        # Sample-mode isolation (2026-05-11): the PreRunBanner promises
        # "Sample = first N rows, no effect on destinations". The
        # executor honours that by routing the run through the existing
        # PR10 sandbox-namespace path — every destination is rewritten
        # to a per-pipeline scratch namespace so the real sink stays
        # untouched. Row count is capped at SAMPLE_ROWS regardless of
        # `full_run`. Without this, sample mode would just shrink the
        # row count but still write to the real destination.
        SAMPLE_ROWS = 100
        sample_kwargs: dict = {}
        if safety_mode == "sample":
            sample_kwargs = {
                "sandbox_namespace": f"sample_{workflow_id}",
                "sandbox_row_limit": SAMPLE_ROWS,
            }
        # Phase 7: mint a one-time, run-bound execution code for this
        # authorized run and hand it to the executor. No-op unless
        # FPULSE_REQUIRE_EXECUTION_CODE is on; the executor enforces it.
        from fpulse.security.execution_codes import get_execution_code_store
        from fpulse.auth.deps import current_user_optional as _cuo
        _run_user = _cuo(request)
        _exec_code = get_execution_code_store().mint(
            user_id=getattr(_run_user, "id", None) or "system",
            workspace_id=getattr(wf, "workspace_id", None) or "default",
            pipeline_id=workflow_id,
            job_run_id=exe.id,
            action="run",
        )
        with ResourceMonitor() as _resmon:
            result = await anyio.to_thread.run_sync(
                lambda: executor.execute_workflow(
                    wf, preview_limit=preview_limit, full_run=full_run,
                    parameter_values=parameter_values or None,
                    run_id=exe.id,
                    execution_code=_exec_code,
                    **sample_kwargs,
                )
            )

        # Build step logs from result
        step_logs = []
        error_analyses = {}
        for step in wf.steps:
            step_result = result.step_results.get(step.id)
            if step_result:
                step_logs.append(StepLog(
                    step_id=step.id,
                    step_name=step.label or step.id,
                    step_type=step.type.value if hasattr(step.type, "value") else str(step.type),
                    status=step_result.status,
                    rows_processed=step_result.row_count,
                    duration_ms=step_result.duration_ms,
                    error_message=step_result.error,
                ))

                # Run error intelligence on failed steps
                if step_result.status == "error" and step_result.error:
                    analysis = error_intel.analyze(
                        error=step_result.error,
                        step_id=step.id,
                        step_type=step.type.value,
                        step_params=step.params,
                        available_columns=step_result.columns or None,
                    )
                    error_analyses[step.id] = analysis.model_dump(mode="json")

        duration = (time.time() - start) * 1000
        exe.status = result.status
        exe.completed_at = datetime.now(timezone.utc)
        exe.duration_ms = round(duration, 1)
        exe.steps_completed = len([s for s in step_logs if s.status == "success"])
        exe.steps_failed = len([s for s in step_logs if s.status == "error"])
        exe.step_logs = step_logs
        # Stash compute/memory usage on metadata so the execution log carries
        # it forward without a schema migration. The Executions UI can read
        # `metadata.peak_memory_mb` / `metadata.cpu_seconds`.
        try:
            md = dict(getattr(exe, "metadata", {}) or {})
            md["peak_memory_mb"] = round(_resmon.peak_memory_mb, 2)
            md["cpu_seconds"] = round(_resmon.cpu_seconds, 2)
            md["sample_count"] = _resmon.sample.sample_count
            if parameter_values:
                md["parameter_values"] = parameter_values
            exe.metadata = md
        except Exception:
            pass
        if result.status == "error":
            failed_steps = [s for s in step_logs if s.error_message]
            exe.error_message = failed_steps[0].error_message if failed_steps else "Unknown error"

        exe_store.record(exe)

        # Return enhanced result with error intelligence
        response = result.model_dump(mode="json")
        if error_analyses:
            response["error_intelligence"] = error_analyses

        # Timeout detection
        if max_runtime_min > 0 and duration > max_runtime_min * 60 * 1000:
            response["timeout_exceeded"] = True
            response["max_runtime_minutes"] = max_runtime_min
            response["actual_runtime_minutes"] = round(duration / 60000, 1)
            response["timeout_message"] = (
                f"Pipeline exceeded maximum runtime of {max_runtime_min} min "
                f"(actual: {round(duration / 60000, 1)} min)"
            )

        # Capture per-step status so the alert email can render a
        # lineage diagram with the failed step highlighted (mirrors the
        # in-app Execution Summary view). Best-effort — empty list on
        # any extraction error and the email skips the lineage block.
        # Step IR fields: `id`, `type` (StepType enum), `label`. There
        # is no `name` / `step_type` attribute — using those wrong names
        # used to throw AttributeError silently and the email always
        # skipped the lineage block (the bug reported on 2026-05-09).
        _alert_steps: list[dict] = []
        _alert_failed = ""
        try:
            _step_results = getattr(result, "step_results", {}) or {}
            for s in wf.steps:
                sr = _step_results.get(s.id) if isinstance(_step_results, dict) else None
                s_status = (getattr(sr, "status", "") if sr else "") or ""
                _step_type = getattr(s, "type", "")
                _step_type_str = getattr(_step_type, "value", str(_step_type) if _step_type else "")
                _step_label = getattr(s, "label", "") or s.id
                if s_status == "error" and not _alert_failed:
                    _alert_failed = _step_label
                # 2026-05-21: include the same metrics the in-app Executions
                # Lineage view shows on each node card — rows + duration —
                # so the email lineage reads at parity with the UI.
                _step_rows = 0
                _step_duration_ms = 0
                _sr = _step_results.get(s.id)
                if _sr is not None:
                    _step_rows = int(getattr(_sr, "row_count", 0) or 0)
                    _step_duration_ms = float(getattr(_sr, "duration_ms", 0) or 0)
                _alert_steps.append({
                    "id": s.id,
                    "name": _step_label,
                    "type": _step_type_str,
                    "status": s_status,
                    "rows_processed": _step_rows,
                    "duration_ms": _step_duration_ms,
                })
        except Exception:
            _alert_steps, _alert_failed = [], ""

        # Capture pipeline edges so the alert email can render the
        # real DAG (parallel branches + joins) rather than a flat
        # linear chain. Same {from,to} shape the notifier reads;
        # empty list → email falls back to the linear layout.
        # Best-effort: a malformed connections list never blocks
        # alert dispatch.
        _alert_connections: list[dict] = []
        try:
            for c in (getattr(wf, "connections", None) or []):
                f = getattr(c, "from_step", "") or ""
                t = getattr(c, "to_step", "") or ""
                if f and t:
                    _alert_connections.append({"from": f, "to": t})
        except Exception:
            _alert_connections = []

        # In-app notification (bell + Notifications page). Always fires
        # for terminal outcomes regardless of alert rules — the bell is
        # the ambient signal, alert rules drive external delivery only.
        try:
            from fpulse.notifications.run_events import emit_run_notification
            from fpulse.main import app_state as _ns
            emit_run_notification(
                notification_store=_ns.get("notification_store"),
                user_store=_ns.get("user_store"),
                workflow_id=workflow_id,
                workflow_name=wf.name,
                execution_id=exe.id,
                status=result.status,
                workspace_id=workspace_id,
                triggered_by="manual",
                error_message=exe.error_message or "",
                failed_step=_alert_failed,
                duration_ms=round(duration),
            )
        except Exception:
            pass

        # Trigger alert notifications — pass workspace so we only fire
        # rules that live in the caller's workspace. Resource numbers
        # let ON_RESOURCE_THRESHOLD rules evaluate the actual usage.
        # Parameter values flow through so the notification body shows
        # exactly what was passed at trigger time (audit + replay).
        # 2026-05-21: also forward project/folder/owner/trigger/started/
        # rows/steps so the redesigned alert email's Run Details block
        # has the operational metadata it needs (was empty before).
        _alert_owner_email = ""
        try:
            from fpulse.auth.deps import current_user_optional
            _u = current_user_optional(request)
            _alert_owner_email = (getattr(_u, "email", "") or "") if _u else ""
        except Exception:
            pass
        _alert_rows_total = sum(int(s.rows_processed or 0) for s in step_logs)
        _alert_started_iso = exe.started_at.isoformat() if exe.started_at else ""
        _trigger_pipeline_alerts(
            workflow_id=workflow_id,
            workflow_name=wf.name,
            status=result.status,
            duration_ms=round(duration),
            error_message=exe.error_message or "",
            workspace_id=workspace_id,
            peak_memory_mb=_resmon.peak_memory_mb,
            cpu_seconds=_resmon.cpu_seconds,
            parameter_values=parameter_values or None,
            workflow_steps=_alert_steps,
            workflow_connections=_alert_connections,
            first_failed_step=_alert_failed,
            project_id=getattr(wf, "project_id", "") or "",
            folder_id=getattr(wf, "folder_id", "") or "",
            triggered_by="manual",
            started_at=_alert_started_iso,
            rows_processed=_alert_rows_total,
            steps_completed=exe.steps_completed,
            steps_total=exe.steps_total,
            owner_email=_alert_owner_email,
        )

        return response

    except Exception as e:
        duration = (time.time() - start) * 1000
        exe.status = "error"
        exe.completed_at = datetime.now(timezone.utc)
        exe.duration_ms = round(duration, 1)
        exe.error_message = str(e)
        exe_store.record(exe)

        # In-app notification for the exception path too.
        try:
            from fpulse.notifications.run_events import emit_run_notification
            from fpulse.main import app_state as _ns
            emit_run_notification(
                notification_store=_ns.get("notification_store"),
                user_store=_ns.get("user_store"),
                workflow_id=workflow_id,
                workflow_name=wf.name,
                execution_id=exe.id,
                status="error",
                workspace_id=workspace_id,
                triggered_by="manual",
                error_message=str(e),
                duration_ms=round(duration),
            )
        except Exception:
            pass

        # Trigger alert notifications for failures
        _trigger_pipeline_alerts(
            workflow_id=workflow_id,
            workflow_name=wf.name,
            status="error",
            duration_ms=round(duration),
            error_message=str(e),
            workspace_id=workspace_id,
        )

        # Run error intelligence on the exception
        analysis = error_intel.analyze(error=str(e))
        raise HTTPException(
            status_code=500,
            detail={
                "message": str(e),
                "error_intelligence": analysis.model_dump(mode="json"),
            },
        )
    finally:
        if sem is not None:
            sem.release()
        # Release worker pool slot
        if _pool and _pool_job:
            try:
                duration_ms = (time.time() - start) * 1000
                _pool._on_job_complete(
                    _pool_job,
                    _pool_job._kwargs.get("_worker") or _pool._workers[0],
                    exe.status or "error",
                    duration_ms,
                    exe.error_message,
                )
            except Exception:
                pass


def _trigger_pipeline_alerts(
    workflow_id: str,
    workflow_name: str,
    status: str,
    duration_ms: int,
    error_message: str,
    workspace_id: str | None = None,
    peak_memory_mb: float = 0.0,
    cpu_seconds: float = 0.0,
    parameter_values: dict | None = None,
    workflow_steps: list[dict] | None = None,
    workflow_connections: list[dict] | None = None,
    first_failed_step: str = "",
    # 2026-05-21: extra context that flows into the alert email's compact
    # header + Run Details section. All optional so older callers (the
    # error-path call site below) keep working unchanged.
    project_id: str = "",
    folder_id: str = "",
    triggered_by: str = "",
    started_at: str = "",
    rows_processed: int | None = None,
    steps_completed: int | None = None,
    steps_total: int | None = None,
    owner_email: str = "",
) -> None:
    """Check alert rules and send notifications after pipeline execution.

    When ``workspace_id`` is provided, only fire rules that live in
    that workspace — this is the common case for user-triggered
    executions. A system-level execution (e.g. scheduler loop) may
    omit it, in which case all matching rules fire (the scheduler
    loops per-workspace separately).
    """
    from fpulse.main import app_state
    from fpulse.alerts.models import AlertCondition

    try:
        alert_store = app_state["alert_store"]
        rules = alert_store.list_rules_by_workflow(workflow_id, workspace_id=workspace_id)
        if not rules:
            return

        for rule_dict in rules:
            rule = alert_store.get_rule(rule_dict["id"], workspace_id=workspace_id)
            if not rule or not rule.enabled:
                continue

            conditions = rule.conditions or [rule.condition]
            should_fire = False
            triggered_condition = ""

            for cond in conditions:
                if cond == AlertCondition.ON_FAILURE and status == "error":
                    should_fire = True
                    triggered_condition = "on_failure"
                elif cond == AlertCondition.ON_SUCCESS and status == "success":
                    should_fire = True
                    triggered_condition = "on_success"
                elif cond == AlertCondition.ON_ANY:
                    should_fire = True
                    triggered_condition = "on_any"
                elif cond == AlertCondition.ON_LONG_RUNNING:
                    threshold_ms = rule.long_running_threshold_minutes * 60 * 1000
                    if duration_ms > threshold_ms:
                        should_fire = True
                        triggered_condition = "on_long_running"
                elif cond == AlertCondition.ON_RESOURCE_THRESHOLD:
                    # Memory threshold is interpreted as MB on this machine
                    # (the rule field is `memory_threshold` — the legacy
                    # 'percent' label is misleading for OSS where we measure
                    # the actual process RSS in MB). cpu_threshold is treated
                    # as CPU-seconds for the same reason. Either breach fires.
                    breaches: list[str] = []
                    if peak_memory_mb and rule.memory_threshold and peak_memory_mb > rule.memory_threshold:
                        breaches.append(f"memory={peak_memory_mb:.0f}MB > {rule.memory_threshold}MB")
                    if cpu_seconds and rule.cpu_threshold and cpu_seconds > rule.cpu_threshold:
                        breaches.append(f"cpu={cpu_seconds:.1f}s > {rule.cpu_threshold}s")
                    if breaches:
                        should_fire = True
                        triggered_condition = f"on_resource_threshold ({'; '.join(breaches)})"
                if should_fire:
                    break

            if should_fire:
                from fpulse.alerts.notifier import NotificationService
                notifier = NotificationService()
                # Build a richer payload so the email/Slack body includes
                # actual failure context (step name, excerpt) + a deep-link
                # to the Executions detail page. The LLM summarize step in
                # notifications/notification_summary.py polishes the prose
                # on top of this when an AI provider is configured.
                # first_failed_step now flows in from the caller (which
                # has access to step_results); fall back to "" so older
                # callers that don't pass it still work.
                _failed_step = first_failed_step or ""
                error_excerpt = (error_message or "")[:300]
                # Format parameter values as a single-line "k=v · k=v" string
                # so it slots into both plain-text and HTML email bodies.
                params_inline = ""
                if parameter_values:
                    params_inline = " · ".join(
                        f"{k}={v}" for k, v in parameter_values.items()
                    )[:300]
                # Run the rule-based diagnoser on the error so the
                # alert email/Teams message carries both the original
                # failure AND a plain-English diagnosis + fix suggestion.
                # Sync + fast — no LLM round-trip blocks the alert path.
                ai_diagnosis = ""
                ai_suggestion = ""
                ai_severity = ""
                ai_powered = False
                if status == "error" and error_excerpt:
                    try:
                        # Real LLM analysis when configured; deterministic
                        # rule-based diagnosis when not. Capped at 12s so
                        # a slow LLM never blocks the alert dispatch.
                        from fpulse.ai.embedded import analyze_error as _diag
                        d = _diag(
                            error_message=error_excerpt,
                            node_type="",
                            workflow_steps=workflow_steps or [],
                            failed_step=first_failed_step or "",
                            workspace_id=workspace_id,
                        ) or {}
                        ai_diagnosis = (d.get("diagnosis") or "")[:500]
                        ai_suggestion = (d.get("suggestion") or "")[:500]
                        ai_severity = d.get("severity") or ""
                        ai_powered = bool(d.get("ai_powered"))
                    except Exception:
                        # Diagnoser shouldn't ever block an alert. If
                        # it crashes for any reason, send the alert
                        # without the AI section.
                        pass

                payload = {
                    "workflow_name": workflow_name,
                    "workflow_id": workflow_id,
                    "status": status,
                    "duration_ms": duration_ms,
                    "duration_s": round(duration_ms / 1000.0, 2),
                    "error_message": error_excerpt,
                    "first_failed_step": _failed_step,
                    "workflow_steps": workflow_steps or [],
                    # Edges between steps — drives the layered DAG
                    # lineage in the alert email so parallel branches
                    # and joins render correctly instead of being
                    # flattened into a single misleading chain. Empty
                    # list = the renderer falls back to the legacy
                    # linear layout.
                    "workflow_connections": workflow_connections or [],
                    "peak_memory_mb": round(peak_memory_mb or 0, 1),
                    "cpu_seconds": round(cpu_seconds or 0, 2),
                    "triggered_condition": triggered_condition,
                    "parameter_values": parameter_values or {},
                    "parameter_values_inline": params_inline,
                    # AI-side enrichment for the failure body. Empty
                    # strings on success runs (or when the diagnoser
                    # didn't match a known pattern).
                    "ai_diagnosis": ai_diagnosis,
                    "ai_suggestion": ai_suggestion,
                    "ai_severity": ai_severity,
                    "ai_powered": ai_powered,
                    # 2026-05-21: Run Details fields. Empty strings/None on
                    # call sites that didn't yet forward them — the email
                    # template skips empty rows so older paths stay quiet.
                    "project_id": project_id,
                    "folder_id": folder_id,
                    "triggered_by": triggered_by,
                    "started_at": started_at,
                    "rows_processed": rows_processed,
                    "steps_completed": steps_completed,
                    "steps_total": steps_total,
                    "owner_email": owner_email,
                    "workspace_id": workspace_id or "",
                    # Deep-link the recipient straight into the Executions
                    # detail panel for this workflow. App URL falls back to
                    # localhost in dev; admin can override via env.
                    "deep_link": f"{os.environ.get('FPULSE_APP_URL', 'http://localhost:5174')}/#executions?workflow={workflow_id}",
                }
                log = notifier.send(rule, payload)
                alert_store.add_log(log)
    except Exception as e:
        import logging
        logging.getLogger("fpulse.execution").error(f"Alert trigger failed: {e}")


@router.post("/replay/{execution_id}")
async def replay_execution(
    execution_id: str,
    request: Request,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Re-run a finished execution with the SAME parameter_values + IR snapshot.

    Power-user feature: closes the audit loop by letting an operator
    re-trigger an exact prior run for debugging or scheduled-event
    replay. The captured `workflow_snapshot` becomes the IR (so the
    rerun sees the SAME steps the original run saw, even if the
    pipeline has been edited since), and `metadata.parameter_values`
    becomes the override dict.

    Optionally accepts a JSON body `{ "parameter_values": {...} }` to
    override individual values for the replay (the rest still inherit
    from the original run). Empty body = exact replay.
    """
    from fpulse.monitoring.store import get_execution_store
    exe_store = get_execution_store()

    original = exe_store.get(execution_id, workspace_id=workspace_id)
    if not original:
        raise HTTPException(404, "Execution not found")

    # Original IR snapshot — falls back to current saved workflow if the
    # snapshot wasn't captured (pre-feature runs).
    snapshot = getattr(original, "workflow_snapshot", None)
    if not snapshot and isinstance(original, dict):
        snapshot = original.get("workflow_snapshot")
    if not snapshot:
        raise HTTPException(
            400,
            "Original execution did not capture a workflow snapshot; cannot replay. "
            "Trigger a fresh run from the Pipelines page instead.",
        )

    # Pull captured parameter_values; merge with any caller-supplied overrides.
    original_params: dict[str, Any] = {}
    md = getattr(original, "metadata", None)
    if md is None and isinstance(original, dict):
        md = original.get("metadata")
    if isinstance(md, dict):
        captured = md.get("parameter_values") or {}
        if isinstance(captured, dict):
            original_params = captured

    overrides: dict[str, Any] = {}
    try:
        if request.headers.get("content-length"):
            raw = await request.json()
            if isinstance(raw, dict):
                pv = raw.get("parameter_values") or raw.get("parameters")
                if isinstance(pv, dict):
                    overrides = pv
    except Exception:
        overrides = {}

    merged_param_values = {**original_params, **overrides}

    # Hydrate Workflow from the snapshot. The snapshot may be a dict
    # straight from `model_dump`; pass it back through the pydantic
    # constructor for validation.
    from fpulse.ir.schema import Workflow
    try:
        wf = Workflow(**snapshot) if isinstance(snapshot, dict) else snapshot
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Snapshot did not validate as a Workflow: %s", e)
        raise HTTPException(400, "Snapshot did not validate as a Workflow") from e

    workflow_id = wf.id

    exe_store_obj = exe_store
    data_dir = get_data_dir()
    from fpulse.main import app_state as _app_state
    executor = WorkflowExecutor(data_dir=data_dir, app_state=_app_state)

    exe = ExecutionRecord(
        workflow_id=workflow_id,
        workflow_name=wf.name,
        project_id=getattr(wf, "project_id", "default"),
        workspace_id=workspace_id,
        steps_total=len(wf.steps),
        triggered_by=f"replay:{execution_id[:8]}",
        workflow_snapshot=wf.model_dump(mode="json"),
    )

    start = time.time()
    try:
        from fpulse.security.execution_codes import mint_for_run
        _replay_code = mint_for_run(wf, job_run_id=exe.id)
        with ResourceMonitor() as _resmon:
            result = await anyio.to_thread.run_sync(
                lambda: executor.execute_workflow(
                    wf,
                    parameter_values=merged_param_values or None,
                    run_id=exe.id,
                    execution_code=_replay_code,
                )
            )

        duration = (time.time() - start) * 1000
        exe.status = result.status
        exe.completed_at = datetime.now(timezone.utc)
        exe.duration_ms = round(duration, 1)
        try:
            exe.metadata = {
                **(getattr(exe, "metadata", {}) or {}),
                "peak_memory_mb": round(_resmon.peak_memory_mb, 2),
                "cpu_seconds": round(_resmon.cpu_seconds, 2),
                "parameter_values": merged_param_values,
                "replay_of": execution_id,
            }
        except Exception:
            pass
        exe_store_obj.record(exe)

        return {
            "execution_id": exe.id,
            "replay_of": execution_id,
            "workflow_id": workflow_id,
            "status": result.status,
            "duration_ms": round(duration, 1),
            "parameter_values": merged_param_values,
            "step_results": result.model_dump(mode="json").get("step_results", {}),
        }
    except Exception as e:
        duration = (time.time() - start) * 1000
        exe.status = "error"
        exe.duration_ms = round(duration, 1)
        exe.error_message = f"{type(e).__name__}: {str(e)[:300]}"
        try:
            exe_store_obj.record(exe)
        except Exception:
            pass
        import logging
        logging.getLogger(__name__).exception("Replay failed")
        raise HTTPException(500, "Replay failed") from e


class EphemeralStepRunBody(BaseModel):
    """Body for POST /workflow/ephemeral/step/{step_id}.

    Sibling of EphemeralRunBody (whole-workflow) for the Test Node case:
    the user wants to test a single step's config against the current
    canvas state WITHOUT being forced to name + save the pipeline first.

    Honors the same no-silent-create rule (2026-05-09) — no row in the
    Pipelines list is created, no execution_history entry is written.
    Pure ephemeral preview against the inline workflow IR.
    """
    workflow: dict[str, Any] = _Field(...)
    preview_limit: int = 50


# IMPORTANT — this route MUST be registered BEFORE
# /workflow/{workflow_id}/step/{step_id} below. FastAPI matches routes
# in registration order, and a literal segment like "ephemeral" will
# otherwise be eaten by the parameterised {workflow_id} pattern,
# resulting in a 404 "Workflow not found" lookup against an id of
# "ephemeral". Mirrors the ordering of /workflow/ephemeral (whole-run)
# vs /workflow/{workflow_id} earlier in this file.
@router.post("/workflow/ephemeral/step/{step_id}")
async def run_step_ephemeral(
    step_id: str,
    body: EphemeralStepRunBody,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Run a single step against an inline workflow IR — no persistence.

    Frontend Test Node uses this path when the pipeline hasn't been
    saved yet, so users can iterate on a step's config without the
    name-prompt friction.
    """
    from fpulse.ir.schema import Workflow
    try:
        wf = Workflow(**body.workflow)
    except Exception as exc:
        raise HTTPException(400, {
            "code": "invalid_workflow_ir",
            "message": f"Could not parse workflow IR: {exc}",
        })
    # Stamp the caller's workspace — never trust body content.
    wf.workspace_id = workspace_id

    # Validate (same gate as the ephemeral whole-run endpoint) so a
    # broken canvas surfaces structured errors instead of a 500.
    from fpulse.ir.validator import validate_workflow as _validate_workflow
    val_items = _validate_workflow(wf)
    fatal = [v for v in val_items if getattr(v, "severity", "error") == "error"]
    if fatal:
        raise HTTPException(400, {
            "code": "validation_failed",
            "message": f"{len(fatal)} validation error(s) — fix the canvas before testing.",
            "errors": [e.dict() for e in fatal],
        })

    from fpulse.main import app_state as _app_state
    executor = WorkflowExecutor(data_dir=get_data_dir(), app_state=_app_state)
    trace = await anyio.to_thread.run_sync(
        lambda: executor.execute_step_trace(wf, step_id, preview_limit=body.preview_limit)
    )
    selected = trace.step_results.get(step_id)
    if selected is None:
        selected = StepRunResult(step_id=step_id, status="error", error="Step not found")
    payload = selected.model_dump(mode="json")
    payload["step_results"] = {
        sid: res.model_dump(mode="json")
        for sid, res in trace.step_results.items()
    }
    payload["workflow_status"] = trace.status
    payload["workflow_duration_ms"] = trace.duration_ms
    return payload


@router.post("/workflow/{workflow_id}/step/{step_id}")
async def run_step(
    workflow_id: str,
    step_id: str,
    preview_limit: int = 50,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Execute a single step (with dependencies) for preview — workspace-scoped."""
    v = get_store().get(workflow_id, workspace_id=workspace_id)
    if not v:
        raise HTTPException(404, "Workflow not found")

    from fpulse.main import app_state as _app_state
    executor = WorkflowExecutor(data_dir=get_data_dir(), app_state=_app_state)
    trace = await anyio.to_thread.run_sync(
        lambda: executor.execute_step_trace(v.workflow, step_id, preview_limit=preview_limit)
    )
    selected = trace.step_results.get(step_id)
    if selected is None:
        selected = StepRunResult(step_id=step_id, status="error", error="Step not found")
    payload = selected.model_dump(mode="json")
    payload["step_results"] = {
        sid: res.model_dump(mode="json")
        for sid, res in trace.step_results.items()
    }
    payload["workflow_status"] = trace.status
    payload["workflow_duration_ms"] = trace.duration_ms
    return payload


@router.post("/workflow/{workflow_id}/resume")
async def resume_workflow(
    workflow_id: str,
    run_id: str,
    preview_limit: int = 50,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Resume a previously-failed workflow run from the first non-success step.

    Sprint A: takes the prior `run_id` (failed mid-run), reads
    `pipeline_checkpoints` for that run, loads parquet snapshots for every
    step that succeeded, and re-executes only the rest. The new run has
    its own fresh `run_id`, so a future resume can chain off this one.
    """
    v = get_store().get(workflow_id, workspace_id=workspace_id)
    if not v:
        raise HTTPException(404, "Workflow not found")

    from fpulse.main import app_state as _app_state
    executor = WorkflowExecutor(data_dir=get_data_dir(), app_state=_app_state)
    result = await anyio.to_thread.run_sync(
        lambda: executor.execute_workflow_resume(
            v.workflow, run_id=run_id, preview_limit=preview_limit,
        )
    )
    return result.model_dump(mode="json")


@router.post("/workflow/{workflow_id}/step/{step_id}/resume")
async def resume_from_step(
    workflow_id: str,
    step_id: str,
    preview_limit: int = 50,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Rerun from here — reuses cached upstream outputs when fresh.

    Dependencies whose params and ancestors are unchanged since the last
    successful run are loaded from the on-disk Parquet cache; the rest are
    re-executed. The target step always runs.
    """
    v = get_store().get(workflow_id, workspace_id=workspace_id)
    if not v:
        raise HTTPException(404, "Workflow not found")

    from fpulse.main import app_state as _app_state
    executor = WorkflowExecutor(data_dir=get_data_dir(), app_state=_app_state)
    result = await anyio.to_thread.run_sync(
        lambda: executor.execute_step_resume(v.workflow, step_id, preview_limit=preview_limit)
    )
    return result.model_dump(mode="json")


@router.get("/workflow/{workflow_id}/cache")
async def cache_summary(
    workflow_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Per-step cache state — which nodes have a fresh cached output."""
    from fpulse.engine.step_cache import StepCache
    v = get_store().get(workflow_id, workspace_id=workspace_id)
    if not v:
        raise HTTPException(404, "Workflow not found")
    from fpulse.main import app_state as _app_state
    executor = WorkflowExecutor(data_dir=get_data_dir(), app_state=_app_state)
    input_map = executor._build_input_map(v.workflow)
    cache = StepCache(get_data_dir(), workflow_id)
    return cache.summary(v.workflow, input_map)


@router.delete("/workflow/{workflow_id}/cache")
async def clear_cache(
    workflow_id: str,
    step_id: str | None = None,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Clear the step cache — all steps, or a single step if step_id is given."""
    from fpulse.engine.step_cache import StepCache
    v = get_store().get(workflow_id, workspace_id=workspace_id)
    if not v:
        raise HTTPException(404, "Workflow not found")
    cache = StepCache(get_data_dir(), workflow_id)
    if step_id:
        cache.clear_step(step_id)
    else:
        cache.clear()
    return {"status": "ok", "step_id": step_id or "all"}


@router.post("/preview")
async def preview_file(file_path: str, limit: int = 50):
    """Preview a data file (CSV/Parquet/JSON)."""
    import duckdb

    data_dir = get_data_dir()
    if not os.path.isabs(file_path):
        file_path = os.path.join(data_dir, file_path)

    if not os.path.exists(file_path):
        raise HTTPException(404, f"File not found: {file_path}")

    conn = duckdb.connect(":memory:")
    try:
        ext = os.path.splitext(file_path)[1].lower()
        if ext == ".csv":
            rel = conn.read_csv(file_path)
        elif ext == ".parquet":
            rel = conn.read_parquet(file_path)
        elif ext == ".json":
            rel = conn.read_json(file_path)
        else:
            raise HTTPException(400, f"Unsupported file type: {ext}")

        from fpulse.engine.preview import preview_relation
        return preview_relation(rel, limit=limit)
    finally:
        conn.close()


# ── Per-source preview — F11 gap (the "Test / Preview" button on every
# source config panel). Build a synthetic 1-step workflow with the given
# step_type + params, run it through the executor with a row cap, and
# return sample rows + columns + schema for the UI to render.
#
# Pure read path: no writes happen because we only execute SOURCE-type
# nodes (sinks would be rejected). Safe even for connection-less previews
# (e.g. CSV from local path) and for connection-backed previews (DB / API
# / S3) — the connection's own permissions still apply.

@router.post("/preview-source")
async def preview_source(payload: dict, request: Request):
    """Preview the rows a configured source node would produce.

    Request body: `{step_type: str, params: dict, limit: int = 20}`.
    Returns the same shape as `preview_relation()` —
    `{total_rows, columns, sample_data, schema_info}`. Errors surface
    as `{error: "..."}` with a 200 so the UI can render them inline
    rather than triggering a fetch failure.
    """
    from fpulse.engine.preview import preview_relation
    from fpulse.ir.schema import StepType, Step, Workflow

    step_type_raw = (payload.get("step_type") or "").strip().lower()
    params = payload.get("params") or {}
    limit = int(payload.get("limit") or 20)
    if limit < 1 or limit > 500:
        limit = 20

    if not step_type_raw:
        raise HTTPException(400, "step_type is required")

    # Resolve into the StepType enum. Unknown / non-source types are
    # refused — preview is intentionally read-only.
    try:
        step_type = StepType(step_type_raw)
    except ValueError:
        raise HTTPException(400, f"Unknown step_type: {step_type_raw!r}")

    # Whitelist source-y types. The matrix's F11 gap is about file +
    # data sources; sinks / writes / control nodes are NOT preview-eligible.
    SOURCE_PREVIEW_ALLOWED = {
        "csv_source", "json_source", "excel_source", "parquet_source",
        "db_source", "api_source", "s3_source",
    }
    if step_type.value not in SOURCE_PREVIEW_ALLOWED:
        raise HTTPException(
            400,
            f"Preview not supported for step_type={step_type.value!r}. "
            f"Only source nodes can be previewed.",
        )

    # Build a synthetic workflow with one step. Use the existing executor
    # so any per-dialect quirks (CSV header detection, DB connection
    # reuse, parameter substitution) match what a real run would do.
    workflow = Workflow(
        id="__preview__",
        name="__preview__",
        steps=[Step(id="src", type=step_type, label="Preview source", params=params)],
        connections=[],
    )

    data_dir = get_data_dir()
    workspace_id = current_workspace_id(request)
    app_state = getattr(request.app.state, "fpulse_state", None) or {}
    if not app_state:
        from fpulse.main import app_state as _app_state
        app_state = _app_state
    executor = WorkflowExecutor(data_dir=data_dir, app_state=app_state)
    try:
        result = await anyio.to_thread.run_sync(
            lambda: executor.execute_step(workflow, "src", preview_limit=limit)
        )
    except Exception as e:  # noqa: BLE001 — surface to UI as inline error
        return {"error": f"Preview failed: {e}", "step_type": step_type.value}

    if result.status != "success":
        return {
            "error": result.error or "Preview failed",
            "step_type": step_type.value,
            "status": result.status,
        }

    return {
        "step_type": step_type.value,
        "total_rows": result.row_count,
        "columns": result.columns,
        "sample_data": result.sample_data,
        "schema_info": result.schema_info,
        "duration_ms": result.duration_ms,
    }


# ── Step IO replay endpoints ────────────────────────────────────────────
# Surfaces the captures written by StepOutputStore at run-time. The UI
# uses these to build the per-node IO drawer (Schema / Table / JSON tabs
# + search + export) and the per-edge row-count labels on the lineage
# graph for a historical execution.

def get_step_output_store():
    from fpulse.main import app_state
    return app_state["step_output_store"]


def _require_execution(execution_id: str, workspace_id: str):
    exe = get_execution_store().get(execution_id, workspace_id=workspace_id)
    if exe is None:
        raise HTTPException(404, "Execution not found")
    return exe


@router.get("/execution/{execution_id}/step/{step_id}/output")
async def get_step_output(
    execution_id: str,
    step_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Captured output sample + schema + counts for one step in one execution."""
    _require_execution(execution_id, workspace_id)
    record = get_step_output_store().get_step(execution_id, step_id)
    if record is None:
        raise HTTPException(404, f"No output captured for step {step_id!r}")
    return record


@router.get("/execution/{execution_id}/step/{step_id}/input")
async def get_step_input(
    execution_id: str,
    step_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """This step's input(s), derived from upstream output(s) via the run's IR snapshot.

    Multi-parent steps (joins, unions) return multiple inputs.
    Root steps return an empty inputs list. Upstream captures that were
    TTL-pruned still appear with `sample_pruned=true`; row_count + schema
    remain so the UI can render the structural context.
    """
    exe = _require_execution(execution_id, workspace_id)

    snapshot = exe.workflow_snapshot or {}
    connections = snapshot.get("connections", []) or []
    upstream_ids = [c.get("from_step") for c in connections if c.get("to_step") == step_id]

    store = get_step_output_store()
    inputs = []
    for upstream_id in upstream_ids:
        if not upstream_id:
            continue
        upstream = store.get_step(execution_id, upstream_id)
        if upstream is None:
            inputs.append({
                "source_step_id": upstream_id,
                "label": upstream_id,
                "row_count": 0,
                "sample_rows": [],
                "sample_truncated": False,
                "sample_pruned": False,
                "schema": [],
                "missing": True,
            })
            continue
        inputs.append({
            "source_step_id": upstream_id,
            "label": upstream.get("label") or upstream_id,
            "row_count": upstream.get("row_count", 0),
            "sample_rows": upstream.get("sample_rows", []),
            "sample_truncated": upstream.get("sample_truncated", False),
            "sample_pruned": upstream.get("sample_pruned", False),
            "schema": upstream.get("schema", []),
            "missing": False,
        })

    return {
        "execution_id": execution_id,
        "step_id": step_id,
        "inputs": inputs,
    }


@router.get("/execution/{execution_id}/edges")
async def get_execution_edges(
    execution_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Per-edge metadata for the lineage view's row-count labels."""
    exe = _require_execution(execution_id, workspace_id)

    snapshot = exe.workflow_snapshot or {}
    connections = snapshot.get("connections", []) or []

    outputs = {
        o["step_id"]: o
        for o in get_step_output_store().list_for_execution(execution_id)
    }

    edges = []
    for conn in connections:
        from_step = conn.get("from_step")
        to_step = conn.get("to_step")
        upstream = outputs.get(from_step) or {}
        edges.append({
            "from_step": from_step,
            "to_step": to_step,
            "row_count": upstream.get("row_count", 0),
            "from_status": upstream.get("status", "unknown"),
        })

    return {
        "execution_id": execution_id,
        "edges": edges,
    }


@router.get("/execution/{execution_id}/step/{step_id}/output/export")
async def export_step_output(
    execution_id: str,
    step_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
    fmt: str = "csv",
):
    """Download the captured sample as CSV or JSON.

    Query: ?fmt=csv (default) or ?fmt=json. The UI surfaces a truncation
    banner when sample_truncated=true so users know they're exporting a
    sample, not the full dataset (full export = Plus feature).
    """
    from fastapi.responses import Response

    _require_execution(execution_id, workspace_id)
    record = get_step_output_store().get_step(execution_id, step_id)
    if record is None:
        raise HTTPException(404, f"No output captured for step {step_id!r}")

    rows = record.get("sample_rows", []) or []
    schema = record.get("schema", []) or []
    safe_label = (record.get("label") or step_id).replace("/", "_").replace("\\", "_").replace(" ", "_")
    short_exec = execution_id[:8]

    fmt = (fmt or "csv").lower()
    if fmt == "json":
        import json as _json
        body = _json.dumps(rows, indent=2, default=str)
        return Response(
            content=body,
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{safe_label}_{short_exec}.json"',
            },
        )

    if fmt == "csv":
        import csv as _csv
        import io as _io
        buf = _io.StringIO()
        cols = [c.get("name") for c in schema if c.get("name")]
        if not cols and rows:
            cols = list(rows[0].keys())
        if cols:
            writer = _csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row.get(k) for k in cols})
        return Response(
            content=buf.getvalue(),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{safe_label}_{short_exec}.csv"',
            },
        )

    raise HTTPException(400, f"Unsupported fmt={fmt!r}; use 'csv' or 'json'")
