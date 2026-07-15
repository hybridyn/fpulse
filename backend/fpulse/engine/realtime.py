"""
Real-time Workflow Executor — runs workflows with live progress events.

Extends the base WorkflowExecutor pattern with event callbacks for
WebSocket streaming, detailed timing, memory tracking, and structured logs.
"""

from __future__ import annotations

import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, TYPE_CHECKING

# Stage 2.5b: duckdb is RUNTIME-USED here (RealtimeExecutor.execute
# calls duckdb.connect to open the workflow's in-memory DB). The
# runtime import lives inside that method.
if TYPE_CHECKING:
    import duckdb

from fpulse.ir.schema import Workflow, StepRunResult, WorkflowRunResult, Step
from fpulse.ir.validator import validate_workflow
from fpulse.nodes.base import ExecutionContext
from fpulse.nodes.registry import get_registry
from fpulse.engine.preview import preview_relation
from fpulse.engine.step_output_store import StepOutputStore, schema_from_sample
# 2026-05-26 — typed event publish, in addition to legacy on_event
# callback. EventBus is an optional dependency on the executor; we
# import only the interface + event types (no transport import here),
# preserving Rule 11 (engine MUST NOT import fpulse.ai or other heavy
# packages). The bus itself is constructed in main.lifespan.
from fpulse.events.bus import EventBus
from fpulse.events.types import (
    PipelineRunCancelled,
    PipelineRunCompleted,
    PipelineRunFailed,
    PipelineRunStarted,
    StepCompleted,
    StepFailed,
    StepStarted,
)


EventCallback = Callable[[dict[str, Any]], None]


class RealtimeExecutor:
    """Execute workflows with real-time progress reporting.

    Drop-in replacement for WorkflowExecutor that emits structured events
    via an on_event callback. When no callback is provided, behaves
    identically to the base executor.
    """

    def __init__(
        self,
        data_dir: str = ".",
        on_event: EventCallback | None = None,
        step_output_store: StepOutputStore | None = None,
        event_bus: EventBus | None = None,
    ):
        self.data_dir = data_dir
        self.on_event = on_event
        self.step_output_store = step_output_store
        # 2026-05-26 — Optional EventBus. When supplied, the executor
        # publishes typed events (PipelineRunStarted, StepCompleted, …)
        # alongside the legacy ``on_event`` callback. Existing callers
        # that only pass ``on_event`` keep working unchanged; new
        # consumers can subscribe to ``bus`` without the executor having
        # to know about them. This is the load-bearing edge of the
        # "add an observability path = add one file" claim.
        self.event_bus = event_bus
        self.registry = get_registry()
        self._cancelled = False
        self._events: list[dict[str, Any]] = []

    # ── Public API ──

    def cancel(self):
        """Request cancellation of the current execution."""
        self._cancelled = True

    @property
    def collected_events(self) -> list[dict[str, Any]]:
        """Return all events emitted during the last execution."""
        return list(self._events)

    def execute_workflow(
        self, workflow: Workflow, preview_limit: int = 50
    ) -> WorkflowRunResult:
        """Execute all steps in topological order with progress events."""
        self._workflow_ref = workflow
        self._cancelled = False
        self._events = []

        execution_id = uuid.uuid4().hex[:12]

        # Validate first
        errors = validate_workflow(workflow)
        if any(e.severity == "error" for e in errors):
            error_msg = "; ".join(e.message for e in errors)
            self._emit("workflow_completed", {
                "execution_id": execution_id,
                "workflow_id": workflow.id,
                "status": "error",
                "total_duration_ms": 0,
                "total_rows": 0,
                "error": f"Validation failed: {error_msg}",
                "step_results": {},
            })
            return WorkflowRunResult(
                workflow_id=workflow.id,
                status="error",
                step_results={
                    "validation": StepRunResult(
                        step_id="validation",
                        status="error",
                        error=error_msg,
                    )
                },
            )

        # Build execution order
        order = self._topological_sort(workflow)
        input_map = self._build_input_map(workflow)
        total_steps = len(order)

        # Emit workflow_started
        self._emit("workflow_started", {
            "execution_id": execution_id,
            "workflow_id": workflow.id,
            "workflow_name": workflow.name,
            "total_steps": total_steps,
            "step_ids": [s.id for s in order],
            "started_at": datetime.now(timezone.utc).isoformat(),
        })

        run_result = WorkflowRunResult(
            workflow_id=workflow.id,
            status="running",
        )

        import duckdb  # method-scoped (Stage 2.5b)
        conn = duckdb.connect(":memory:")
        ctx = ExecutionContext(conn=conn, data_dir=self.data_dir)
        t0 = time.perf_counter()

        for idx, step in enumerate(order):
            # Check cancellation
            if self._cancelled:
                self._emit("workflow_completed", {
                    "execution_id": execution_id,
                    "workflow_id": workflow.id,
                    "status": "cancelled",
                    "total_duration_ms": round((time.perf_counter() - t0) * 1000, 2),
                    "total_rows": self._total_rows(run_result),
                    "completed_steps": idx,
                    "total_steps": total_steps,
                    "step_results": self._summarize_results(run_result),
                })
                run_result.status = "cancelled"
                run_result.completed_at = datetime.now(timezone.utc)
                run_result.duration_ms = round((time.perf_counter() - t0) * 1000, 2)
                conn.close()
                return run_result

            step_result = self._execute_step_with_events(
                step, ctx, input_map, preview_limit,
                step_index=idx, total_steps=total_steps,
                execution_id=execution_id,
            )
            run_result.step_results[step.id] = step_result

            if step_result.status == "error":
                run_result.status = "error"
                break

        elapsed = (time.perf_counter() - t0) * 1000

        if run_result.status not in ("error", "cancelled"):
            run_result.status = "success"

        run_result.completed_at = datetime.now(timezone.utc)
        run_result.duration_ms = round(elapsed, 2)

        # Emit workflow_completed
        self._emit("workflow_completed", {
            "execution_id": execution_id,
            "workflow_id": workflow.id,
            "status": run_result.status,
            "total_duration_ms": round(elapsed, 2),
            "total_rows": self._total_rows(run_result),
            "completed_steps": len([
                r for r in run_result.step_results.values()
                if r.status == "success"
            ]),
            "failed_steps": len([
                r for r in run_result.step_results.values()
                if r.status == "error"
            ]),
            "total_steps": total_steps,
            "step_results": self._summarize_results(run_result),
        })

        conn.close()
        return run_result

    # ── Internal execution ──

    def _execute_step_with_events(
        self,
        step: Step,
        ctx: ExecutionContext,
        input_map: dict[str, list[str]],
        preview_limit: int,
        step_index: int,
        total_steps: int,
        execution_id: str,
    ) -> StepRunResult:
        """Execute a single step with event emission."""

        step_type = step.type.value if hasattr(step.type, "value") else str(step.type)

        # Emit step_started
        self._emit("step_started", {
            "execution_id": execution_id,
            "step_id": step.id,
            "step_type": step_type,
            "label": step.label or step.id,
            "step_index": step_index,
            "total_steps": total_steps,
        })

        t0 = time.perf_counter()
        mem_before = self._get_memory_usage()

        try:
            node_cls = self.registry.get(step.type)
            params = {**step.params, "_input_step_ids": input_map.get(step.id, [])}

            # Inject label map for transform nodes
            if step_type == "transform":
                label_map = {}
                for s in self._workflow_ref.steps if hasattr(self, '_workflow_ref') else []:
                    label_map[s.id] = s.label or s.type.value
                params["_node_labels"] = label_map

            node = node_cls(params)
            # Scope internal DuckDB view/temp names by step id (see
            # ExecutionContext.scoped_name) so same-type nodes don't collide.
            ctx.current_step_id = step.id
            relation = node.execute(ctx)
            ctx.set_result(step.id, relation)

            elapsed = (time.perf_counter() - t0) * 1000
            mem_after = self._get_memory_usage()

            # Build preview
            preview = preview_relation(relation, limit=preview_limit) if preview_limit > 0 else {}
            row_count = preview.get("total_rows", 0)
            columns = preview.get("columns", [])
            progress_pct = round(((step_index + 1) / total_steps) * 100, 1) if total_steps > 0 else 100

            # Emit step_completed
            self._emit("step_completed", {
                "execution_id": execution_id,
                "step_id": step.id,
                "step_type": step_type,
                "label": step.label or step.id,
                "status": "success",
                "row_count": row_count,
                "columns": columns,
                "duration_ms": round(elapsed, 2),
                "progress_pct": progress_pct,
                "step_index": step_index,
                "total_steps": total_steps,
                "memory_delta_mb": round((mem_after - mem_before) / (1024 * 1024), 2),
            })

            # Capture sample — sized to the OSS cap (MAX_SAMPLE_ROWS) so
            # the drawer can scroll the full 100 rows. The live preview
            # above stays at `preview_limit` (smaller) to keep WS payloads
            # small. A second preview pass against the same DuckDB
            # relation is cheap (count is cached, limit fetch is bounded).
            if preview and preview_limit > 0:
                from fpulse.engine.step_output_store import MAX_SAMPLE_ROWS as _MAX_CAP
                if preview_limit >= _MAX_CAP:
                    capture_preview = preview
                else:
                    capture_preview = preview_relation(relation, limit=_MAX_CAP)
                sample_data = capture_preview.get("sample_data", [])
                schema_info = capture_preview.get("schema_info", [])
            else:
                sample_data = []
                schema_info = []

            self._capture_step_output(
                execution_id=execution_id,
                step=step,
                step_index=step_index,
                step_type=step_type,
                status="success",
                row_count=row_count,
                sample_data=sample_data,
                schema_info=schema_info,
            )

            return StepRunResult(
                step_id=step.id,
                status="success",
                row_count=row_count,
                columns=columns,
                sample_data=sample_data,
                schema_info=schema_info,
                duration_ms=round(elapsed, 2),
            )

        except Exception as e:
            elapsed = (time.perf_counter() - t0) * 1000
            error_str = str(e)
            tb = traceback.format_exc()

            # Generate diagnosis and suggestion
            diagnosis, suggestion = self._diagnose_error(step, error_str)

            # Emit step_error
            self._emit("step_error", {
                "execution_id": execution_id,
                "step_id": step.id,
                "step_type": step_type,
                "label": step.label or step.id,
                "error": error_str,
                "traceback": tb,
                "diagnosis": diagnosis,
                "suggestion": suggestion,
                "duration_ms": round(elapsed, 2),
                "step_index": step_index,
                "total_steps": total_steps,
            })

            # Record the failed step too so the replay view shows the
            # node with its error state, not as a hole in the lineage.
            self._capture_step_output(
                execution_id=execution_id,
                step=step,
                step_index=step_index,
                step_type=step_type,
                status="error",
                row_count=0,
                sample_data=[],
                schema_info=[],
            )

            return StepRunResult(
                step_id=step.id,
                status="error",
                error=error_str,
                duration_ms=round(elapsed, 2),
            )

    # ── Step output capture (for historical replay viewer) ──

    def _capture_step_output(
        self,
        *,
        execution_id: str,
        step: Step,
        step_index: int,
        step_type: str,
        status: str,
        row_count: int,
        sample_data: list[dict[str, Any]],
        schema_info: list[dict[str, Any]],
    ) -> None:
        """Persist this step's output snapshot (best-effort, never fails the run).

        The store is optional — when not wired (older callers, tests), capture
        is a no-op. When wired, this writes a row to step_outputs the UI can
        later pull to render the per-step IO drawer on the Executions page.
        """
        if self.step_output_store is None:
            return
        try:
            schema = schema_from_sample(sample_data, schema_info)
            self.step_output_store.record(
                execution_id=execution_id,
                step_id=step.id,
                step_index=step_index,
                step_type=step_type,
                label=step.label or step.id,
                status=status,
                row_count=row_count,
                sample_rows=sample_data,
                schema=schema,
            )
        except Exception:
            # Capture is best-effort — a hiccup writing to step_outputs
            # must NEVER abort the pipeline. Worst case: missing replay
            # row for this step; the run itself still completes.
            pass

    _schema_from_sample = staticmethod(schema_from_sample)

    # ── Event emission ──

    def _emit(self, event_type: str, data: dict[str, Any]):
        """Emit a structured event via the callback and record it.

        Three sinks now (each isolated — a failure in one never affects
        the others, never breaks the run):
          1. ``self._events`` — in-memory log for ``collected_events``.
          2. ``self.on_event`` — legacy point-to-point callback. Still
             how WebSocket and AI agent live consumers read execution
             progress. Unchanged contract.
          3. ``self.event_bus`` — typed pub/sub. Whoever cares
             subscribes; the executor doesn't know who's listening.
        """
        event = {
            "type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **data,
        }
        self._events.append(event)
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:
                pass  # Never let callback errors break execution
        if self.event_bus is not None:
            try:
                self._publish_to_bus(event_type, data)
            except Exception:
                # Bus publish errors are observability bugs, never
                # business-logic bugs. Swallow — the run is the source
                # of truth, the bus is a side channel.
                pass

    def _publish_to_bus(self, event_type: str, payload: dict[str, Any]) -> None:
        """Map the legacy ``(name, dict)`` event to a typed Event class
        and publish to the bus.

        Mapping rules (kept dumb on purpose — one branch per legacy
        event name, fields copied straight across):

          workflow_started   → PipelineRunStarted
          workflow_completed → PipelineRunCompleted | RunFailed | RunCancelled
                               (branch on payload["status"])
          step_started       → StepStarted
          step_completed     → StepCompleted
          step_error         → StepFailed

        Legacy events without a typed equivalent (e.g. heartbeats from
        future executor work) silently no-op; subscribers receive what
        the bus has defined, not raw dicts.
        """
        bus = self.event_bus
        if bus is None:
            return
        if event_type == "workflow_started":
            bus.publish(PipelineRunStarted(
                run_id=payload.get("execution_id", ""),
                pipeline_id=payload.get("workflow_id", ""),
                pipeline_version="",  # Not tracked by realtime executor today
                triggered_by="executor",
                project_id="",
                environment="dev",
            ))
        elif event_type == "workflow_completed":
            status = payload.get("status")
            duration_ms = int(payload.get("total_duration_ms") or 0)
            if status == "success":
                bus.publish(PipelineRunCompleted(
                    run_id=payload.get("execution_id", ""),
                    pipeline_id=payload.get("workflow_id", ""),
                    duration_ms=duration_ms,
                    rows_processed=int(payload.get("total_rows") or 0),
                    step_count=int(payload.get("total_steps") or 0),
                ))
            elif status == "cancelled":
                bus.publish(PipelineRunCancelled(
                    run_id=payload.get("execution_id", ""),
                    pipeline_id=payload.get("workflow_id", ""),
                    cancelled_by="user",
                    reason="user_cancel",
                ))
            else:
                # 'error' or anything non-success ⇒ failure event.
                bus.publish(PipelineRunFailed(
                    run_id=payload.get("execution_id", ""),
                    pipeline_id=payload.get("workflow_id", ""),
                    duration_ms=duration_ms,
                    failed_step_id="",  # Not surfaced in this payload
                    error_class="",
                    error_message=str(payload.get("error") or ""),
                ))
        elif event_type == "step_started":
            bus.publish(StepStarted(
                run_id=payload.get("execution_id", ""),
                step_id=payload.get("step_id", ""),
                step_type=payload.get("step_type", ""),
            ))
        elif event_type == "step_completed":
            bus.publish(StepCompleted(
                run_id=payload.get("execution_id", ""),
                step_id=payload.get("step_id", ""),
                step_type=payload.get("step_type", ""),
                duration_ms=int(payload.get("duration_ms") or 0),
                row_count=int(payload.get("row_count") or 0),
                output_columns=list(payload.get("columns") or []),
            ))
        elif event_type == "step_error":
            bus.publish(StepFailed(
                run_id=payload.get("execution_id", ""),
                step_id=payload.get("step_id", ""),
                step_type=payload.get("step_type", ""),
                duration_ms=int(payload.get("duration_ms") or 0),
                error_class="",  # Not separated from message today
                error_message=str(payload.get("error") or ""),
            ))

    # ── Helpers ──

    def _get_memory_usage(self) -> int:
        """Get current process memory usage in bytes."""
        try:
            import psutil
            return psutil.Process().memory_info().rss
        except ImportError:
            return sys.getsizeof(0)  # Fallback: no psutil

    def _total_rows(self, run_result: WorkflowRunResult) -> int:
        """Sum all row counts from step results."""
        return sum(
            r.row_count for r in run_result.step_results.values()
            if r.row_count and r.status == "success"
        )

    def _summarize_results(self, run_result: WorkflowRunResult) -> dict[str, dict]:
        """Build a compact summary of step results for the final event."""
        summary = {}
        for step_id, r in run_result.step_results.items():
            summary[step_id] = {
                "status": r.status,
                "row_count": r.row_count,
                "duration_ms": r.duration_ms,
                "error": r.error,
            }
        return summary

    def _diagnose_error(self, step: Step, error: str) -> tuple[str, str]:
        """Generate a diagnosis and suggestion for a step error."""
        step_type = step.type.value if hasattr(step.type, "value") else str(step.type)
        error_lower = error.lower()

        # Common error patterns
        if "file not found" in error_lower or "no such file" in error_lower:
            return (
                f"The {step_type} node could not find the specified file.",
                "Check that the file path is correct and the file exists in the data directory.",
            )
        if "permission denied" in error_lower:
            return (
                f"The {step_type} node does not have permission to access the resource.",
                "Check file permissions or run F-Pulse with appropriate access.",
            )
        if "column" in error_lower and "not found" in error_lower:
            return (
                f"A referenced column does not exist in the input data.",
                "Check column names in the node configuration against the upstream schema.",
            )
        if "syntax error" in error_lower or "parse error" in error_lower:
            return (
                f"SQL or expression syntax error in the {step_type} node.",
                "Review the SQL/expression for typos, missing commas, or unmatched parentheses.",
            )
        if "connection" in error_lower or "timeout" in error_lower:
            return (
                f"Connection or timeout error in the {step_type} node.",
                "Check network connectivity and increase timeout if needed.",
            )
        if "out of memory" in error_lower or "memory" in error_lower:
            return (
                f"Memory limit exceeded during {step_type} execution.",
                "Reduce data volume with filters or sampling before this step.",
            )

        # Generic fallback
        return (
            f"Unexpected error in {step_type} node: {error[:200]}",
            "Check the node configuration and input data. Review the error details for specifics.",
        )

    def _build_input_map(self, workflow: Workflow) -> dict[str, list[str]]:
        """Map each step to its input step IDs."""
        result: dict[str, list[str]] = {s.id: [] for s in workflow.steps}
        for conn in workflow.connections:
            if conn.to_step in result:
                result[conn.to_step].append(conn.from_step)
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
        order = []

        while queue:
            sid = queue.pop(0)
            order.append(step_map[sid])
            for neighbor in adjacency[sid]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        return order
