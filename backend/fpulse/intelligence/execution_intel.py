"""
Execution Intelligence — smart execution planning, retry logic, and cost estimation.

Analyzes workflow DAGs to determine parallelism, batch sizing,
per-step timeouts, and wraps step execution with configurable retry strategies.
"""

from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from fpulse.ir.schema import Workflow, Step, StepType, StepRunResult, WorkflowRunResult


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class RetryStrategy(str, Enum):
    NONE = "none"
    FIXED = "fixed"
    EXPONENTIAL = "exponential"
    LINEAR = "linear"


class ExecutionConfig(BaseModel):
    """Per-workflow execution configuration."""
    retry_strategy: RetryStrategy = RetryStrategy.EXPONENTIAL
    max_retries: int = 3
    retry_delay_ms: int = 1000
    batch_size: int | None = None  # None = process all at once
    parallel_steps: bool = False  # Allow parallel execution of independent steps
    timeout_ms: int = 300000  # 5 min default


class ExecutionPhase(BaseModel):
    """A group of steps that can execute in parallel."""
    phase_number: int
    steps: list[str]  # step IDs
    can_parallel: bool
    estimated_duration_ms: float


class ExecutionPlan(BaseModel):
    """Optimized execution plan for a workflow."""
    workflow_id: str
    phases: list[ExecutionPhase]
    estimated_duration_ms: float
    parallel_possible: bool
    optimization_notes: list[str]
    config: ExecutionConfig


class StepEstimate(BaseModel):
    """Estimated cost/time for a single step."""
    step_id: str
    step_type: str
    estimated_duration_ms: float
    estimated_memory_mb: float
    suggested_timeout_ms: int
    suggested_batch_size: int | None = None


class ExecutionEstimate(BaseModel):
    """Full workflow execution estimate."""
    workflow_id: str
    total_estimated_duration_ms: float
    step_estimates: list[StepEstimate]
    total_estimated_memory_mb: float
    parallelism_speedup: float  # ratio: sequential / parallel
    notes: list[str]


class StepResult(BaseModel):
    """Result from a retry-wrapped step execution."""
    step_id: str
    status: str  # "success" | "error"
    attempts: int
    total_duration_ms: float
    last_error: str | None = None
    result: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Step type characteristics for estimation
# ---------------------------------------------------------------------------

# Base estimates: (duration_ms_per_1k_rows, memory_mb, timeout_ms)
_STEP_PROFILES: dict[str, tuple[float, float, int]] = {
    # Sources — I/O bound
    StepType.CSV_SOURCE.value:   (50, 10, 60000),
    StepType.DB_SOURCE.value:    (100, 20, 120000),
    StepType.API_SOURCE.value:   (200, 15, 180000),
    # Row transforms — CPU light
    StepType.FILTER.value:       (5, 5, 30000),
    StepType.TRANSFORM.value:    (20, 10, 60000),
    StepType.DEDUPLICATE.value:  (30, 20, 60000),
    StepType.SORT.value:         (40, 30, 60000),
    StepType.RENAME.value:       (2, 5, 15000),
    StepType.TYPECAST.value:     (5, 5, 15000),
    StepType.DERIVED_COLUMN.value: (15, 10, 30000),
    # Set transforms — CPU/memory heavy
    StepType.AGGREGATE.value:    (60, 40, 120000),
    StepType.JOIN.value:         (80, 60, 180000),
    StepType.LOOKUP.value:       (70, 40, 120000),
    StepType.UNION.value:        (10, 20, 60000),
    StepType.PIVOT.value:        (50, 30, 60000),
    StepType.UNPIVOT.value:      (40, 20, 60000),
    StepType.WINDOW.value:       (80, 50, 120000),
    # Quality
    StepType.SAMPLE.value:       (5, 5, 15000),
    StepType.VALIDATE.value:     (20, 10, 60000),
    StepType.CONDITIONAL_SPLIT.value: (10, 10, 30000),
    # Outputs — I/O bound
    StepType.OUTPUT.value:       (60, 10, 120000),
    StepType.DB_SINK.value:      (100, 15, 180000),
}

_DEFAULT_PROFILE = (30, 15, 60000)


# ---------------------------------------------------------------------------
# Intelligence engine
# ---------------------------------------------------------------------------

class ExecutionIntelligence:
    """Analyzes workflows and creates optimized execution plans."""

    def optimize_execution(
        self,
        workflow: Workflow,
        config: ExecutionConfig | None = None,
    ) -> ExecutionPlan:
        """Analyze workflow and create an optimized execution plan.

        Detects which steps can run in parallel (no dependencies between them),
        suggests batch sizes based on estimated data volume, and sets appropriate
        timeouts per step type.
        """
        if config is None:
            config = ExecutionConfig()

        step_map = {s.id: s for s in workflow.steps}
        input_map = self._build_input_map(workflow)
        notes: list[str] = []

        # Compute topological layers (phases)
        phases = self._compute_phases(workflow, step_map, input_map)

        # Check if any phase has multiple steps (parallelism possible)
        parallel_possible = any(len(p) > 1 for p in phases)
        if parallel_possible:
            notes.append(
                f"Workflow has {sum(1 for p in phases if len(p) > 1)} phases "
                "with independent steps that can run in parallel."
            )

        # Build ExecutionPhase objects with estimates
        execution_phases: list[ExecutionPhase] = []
        total_duration = 0.0

        for i, phase_step_ids in enumerate(phases, start=1):
            step_estimates = [
                self._estimate_step(step_map[sid]) for sid in phase_step_ids
            ]

            if config.parallel_steps and len(phase_step_ids) > 1:
                # Parallel: phase duration = max of step durations
                phase_duration = max(e.estimated_duration_ms for e in step_estimates)
                can_parallel = True
            else:
                # Sequential: phase duration = sum of step durations
                phase_duration = sum(e.estimated_duration_ms for e in step_estimates)
                can_parallel = len(phase_step_ids) > 1

            execution_phases.append(ExecutionPhase(
                phase_number=i,
                steps=phase_step_ids,
                can_parallel=can_parallel,
                estimated_duration_ms=round(phase_duration, 2),
            ))
            total_duration += phase_duration

        # Detect heavy steps
        for step in workflow.steps:
            stype = step.type.value
            profile = _STEP_PROFILES.get(stype, _DEFAULT_PROFILE)
            if profile[1] >= 40:  # memory_mb >= 40
                notes.append(
                    f"Step '{step.label or step.id}' ({stype}) is memory-intensive. "
                    "Consider batch processing for large datasets."
                )

        # Suggest batch sizes for I/O-heavy patterns
        source_count = sum(
            1 for s in workflow.steps
            if s.type in (StepType.CSV_SOURCE, StepType.DB_SOURCE, StepType.API_SOURCE)
        )
        if source_count > 1:
            notes.append(
                f"{source_count} source nodes detected. "
                "Sources in different phases will execute in parallel if enabled."
            )

        # Warn about join/lookup without indexes
        for step in workflow.steps:
            if step.type in (StepType.JOIN, StepType.LOOKUP):
                inputs = input_map.get(step.id, [])
                if len(inputs) < 2 and step.type == StepType.JOIN:
                    notes.append(
                        f"Join step '{step.label or step.id}' has fewer than 2 inputs. "
                        "Ensure both sides of the join are connected."
                    )

        return ExecutionPlan(
            workflow_id=workflow.id,
            phases=execution_phases,
            estimated_duration_ms=round(total_duration, 2),
            parallel_possible=parallel_possible,
            optimization_notes=notes,
            config=config,
        )

    def create_retry_wrapper(
        self,
        step_fn,
        step: Step,
        config: ExecutionConfig,
    ) -> StepResult:
        """Wrap a synchronous step execution function with retry logic.

        Args:
            step_fn: Callable that takes no args and returns a StepRunResult.
            step: The Step being executed (for metadata).
            config: Execution configuration with retry settings.

        Returns:
            StepResult with attempt count and final status.
        """
        attempts = 0
        last_error: str | None = None
        total_start = time.perf_counter()

        while attempts <= config.max_retries:
            attempts += 1
            try:
                result: StepRunResult = step_fn()
                if result.status == "success":
                    elapsed = (time.perf_counter() - total_start) * 1000
                    return StepResult(
                        step_id=step.id,
                        status="success",
                        attempts=attempts,
                        total_duration_ms=round(elapsed, 2),
                        result=result.model_dump(mode="json"),
                    )
                last_error = result.error or "Step returned non-success status"
            except Exception as e:
                last_error = str(e)

            # Don't retry if strategy is NONE or we've exhausted retries
            if config.retry_strategy == RetryStrategy.NONE or attempts > config.max_retries:
                break

            # Calculate delay
            delay_s = self._compute_delay(attempts, config) / 1000.0
            time.sleep(delay_s)

        elapsed = (time.perf_counter() - total_start) * 1000
        return StepResult(
            step_id=step.id,
            status="error",
            attempts=attempts,
            total_duration_ms=round(elapsed, 2),
            last_error=last_error,
        )

    async def create_retry_wrapper_async(
        self,
        step_fn,
        step: Step,
        config: ExecutionConfig,
    ) -> StepResult:
        """Async version of create_retry_wrapper for use in async executors."""
        attempts = 0
        last_error: str | None = None
        total_start = time.perf_counter()

        while attempts <= config.max_retries:
            attempts += 1
            try:
                if asyncio.iscoroutinefunction(step_fn):
                    result = await step_fn()
                else:
                    result = step_fn()

                if isinstance(result, StepRunResult) and result.status == "success":
                    elapsed = (time.perf_counter() - total_start) * 1000
                    return StepResult(
                        step_id=step.id,
                        status="success",
                        attempts=attempts,
                        total_duration_ms=round(elapsed, 2),
                        result=result.model_dump(mode="json"),
                    )
                last_error = (result.error if isinstance(result, StepRunResult) else None) or "Non-success"
            except Exception as e:
                last_error = str(e)

            if config.retry_strategy == RetryStrategy.NONE or attempts > config.max_retries:
                break

            delay_s = self._compute_delay(attempts, config) / 1000.0
            await asyncio.sleep(delay_s)

        elapsed = (time.perf_counter() - total_start) * 1000
        return StepResult(
            step_id=step.id,
            status="error",
            attempts=attempts,
            total_duration_ms=round(elapsed, 2),
            last_error=last_error,
        )

    def estimate_cost(self, workflow: Workflow) -> ExecutionEstimate:
        """Estimate execution time and resources for a workflow."""
        step_estimates: list[StepEstimate] = []
        total_memory = 0.0
        notes: list[str] = []

        for step in workflow.steps:
            est = self._estimate_step(step)
            step_estimates.append(est)
            total_memory = max(total_memory, est.estimated_memory_mb)

        # Sequential total
        sequential_ms = sum(e.estimated_duration_ms for e in step_estimates)

        # Parallel total (using phases)
        input_map = self._build_input_map(workflow)
        step_map = {s.id: s for s in workflow.steps}
        phases = self._compute_phases(workflow, step_map, input_map)
        parallel_ms = 0.0
        for phase_ids in phases:
            phase_durations = [
                next(e.estimated_duration_ms for e in step_estimates if e.step_id == sid)
                for sid in phase_ids
            ]
            parallel_ms += max(phase_durations) if phase_durations else 0

        speedup = sequential_ms / parallel_ms if parallel_ms > 0 else 1.0

        if speedup > 1.1:
            notes.append(
                f"Parallel execution could provide ~{speedup:.1f}x speedup "
                f"({round(sequential_ms)}ms -> {round(parallel_ms)}ms)."
            )

        heavy_steps = [e for e in step_estimates if e.estimated_memory_mb >= 40]
        if heavy_steps:
            names = ", ".join(e.step_id for e in heavy_steps)
            notes.append(f"Memory-intensive steps: {names}. Consider batch processing.")

        return ExecutionEstimate(
            workflow_id=workflow.id,
            total_estimated_duration_ms=round(sequential_ms, 2),
            step_estimates=step_estimates,
            total_estimated_memory_mb=round(total_memory, 2),
            parallelism_speedup=round(speedup, 2),
            notes=notes,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _estimate_step(self, step: Step) -> StepEstimate:
        """Estimate cost for a single step based on its type and params."""
        stype = step.type.value
        duration_per_1k, memory_mb, timeout_ms = _STEP_PROFILES.get(stype, _DEFAULT_PROFILE)

        # Estimate row count from params if available
        estimated_rows = step.params.get("estimated_rows", 10000)
        if step.type == StepType.SAMPLE:
            sample_n = step.params.get("count", step.params.get("n", 1000))
            estimated_rows = min(estimated_rows, sample_n)

        duration_ms = (estimated_rows / 1000) * duration_per_1k
        # Minimum duration of 10ms
        duration_ms = max(duration_ms, 10.0)

        # Suggest batch size for large datasets
        suggested_batch: int | None = None
        if estimated_rows > 100000 and memory_mb >= 30:
            suggested_batch = 50000

        return StepEstimate(
            step_id=step.id,
            step_type=stype,
            estimated_duration_ms=round(duration_ms, 2),
            estimated_memory_mb=memory_mb,
            suggested_timeout_ms=timeout_ms,
            suggested_batch_size=suggested_batch,
        )

    def _build_input_map(self, workflow: Workflow) -> dict[str, list[str]]:
        """Map each step to its upstream step IDs."""
        result: dict[str, list[str]] = {s.id: [] for s in workflow.steps}
        for conn in workflow.connections:
            if conn.to_step in result:
                result[conn.to_step].append(conn.from_step)
        return result

    def _compute_phases(
        self,
        workflow: Workflow,
        step_map: dict[str, Step],
        input_map: dict[str, list[str]],
    ) -> list[list[str]]:
        """Compute execution phases via topological layering.

        Each phase contains steps that have all their dependencies satisfied
        by previous phases — meaning they can execute in parallel within
        the phase.
        """
        in_degree: dict[str, int] = {}
        adjacency: dict[str, list[str]] = {}

        for s in workflow.steps:
            in_degree[s.id] = 0
            adjacency[s.id] = []

        for conn in workflow.connections:
            if conn.from_step in adjacency and conn.to_step in in_degree:
                adjacency[conn.from_step].append(conn.to_step)
                in_degree[conn.to_step] += 1

        # BFS layering — each layer is one phase
        phases: list[list[str]] = []
        current_layer = [sid for sid, deg in in_degree.items() if deg == 0]

        while current_layer:
            phases.append(list(current_layer))
            next_layer = []
            for sid in current_layer:
                for neighbor in adjacency.get(sid, []):
                    in_degree[neighbor] -= 1
                    if in_degree[neighbor] == 0:
                        next_layer.append(neighbor)
            current_layer = next_layer

        return phases

    def _compute_delay(self, attempt: int, config: ExecutionConfig) -> float:
        """Compute retry delay in milliseconds."""
        base = config.retry_delay_ms

        if config.retry_strategy == RetryStrategy.FIXED:
            return base
        elif config.retry_strategy == RetryStrategy.LINEAR:
            return base * attempt
        elif config.retry_strategy == RetryStrategy.EXPONENTIAL:
            return base * (2 ** (attempt - 1))
        return 0
