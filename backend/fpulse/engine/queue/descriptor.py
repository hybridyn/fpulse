"""
JobDescriptor — serializable job shape for Stage 5 Phase 2.

Why this exists
---------------
Phase 1 ``QueuedJob`` carries a live Python callable reference
(``_fn``) and kwargs dict (``_kwargs``). That's fine when the queue
lives inside the same Python process, but crosses a serialization
boundary the moment the queue moves to Redis and the consumer is a
different container.

JobDescriptor is the over-the-wire shape. It drops the live callable
and instead encodes enough context (workflow_id, workspace, project,
environment, etc.) for the worker daemon to resolve the right
function from its own registry — today that's always
``engine.executor.execute_workflow``, but the contract allows for
more job types later via an optional ``fn_name`` field.

Round-trip contract:
  to_queued_job(from_queued_job(job)) produces an equivalent
  QueuedJob for scheduling/admission purposes. The live callable is
  NOT round-tripped — callers on the consumer side resolve it
  themselves. This is deliberate: we never want a serialized queue
  payload to cause arbitrary imports in the worker.

JSON is the transport. Redis lists hold JSON strings. We use the
stdlib json module with default=str for datetime; the worker parses
timestamps back via datetime.fromisoformat(). Same pattern as the
Stage 3b PG writers.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fpulse.engine.worker_pool import QueuedJob


# Canonical function name for the only job type Phase 2 supports.
# If the worker daemon ever needs to run something other than a
# pipeline, add a new constant and a registry lookup in worker.py.
FN_EXECUTE_WORKFLOW = "engine.executor.execute_workflow"


@dataclass
class JobDescriptor:
    """Serializable description of a job in the queue.

    Every field must be JSON-safe. No callables, no open file
    handles, no pydantic models that don't round-trip cleanly.

    Retry semantics:
      ``attempt`` counts from 0. When a worker fails a job and
      re-enqueues it, ``attempt`` is incremented. When
      ``attempt >= max_attempts``, the job is not re-enqueued and
      instead written to the DLQ (``status = failed_permanent``).

    Timeout semantics (enforced by the worker, not the queue):
      Soft cancel at ``timeout_s``. Hard kill at ``2 * timeout_s``.
      Timeouts ARE retried unless ``attempt >= max_attempts``.
    """
    id: str
    workflow_id: str
    workflow_name: str = ""
    project_id: str = "default"
    workspace_id: str = "default"
    environment: str = "dev"  # dev | prod
    priority: int = 3
    queued_at: str = ""  # ISO-8601 timestamp
    triggered_by: str = "manual"  # manual | schedule | event
    schedule_id: str | None = None

    # Arbitrary kwargs the job function will receive. Must be
    # JSON-serialisable — the worker passes this dict straight to
    # the target function.
    kwargs: dict[str, Any] = field(default_factory=dict)

    # Retry / timeout (worker enforces)
    attempt: int = 0
    max_attempts: int = 3
    timeout_s: int = 300  # 5 minutes default

    # Optional: which function to run. Defaults to execute_workflow.
    # Reserved for future job types; today the worker hard-codes
    # execute_workflow and ignores anything else.
    fn_name: str = FN_EXECUTE_WORKFLOW

    # ── Serialization ──

    def to_json(self) -> str:
        """Serialize to a JSON string suitable for Redis storage."""
        return json.dumps(asdict(self), default=str)

    @classmethod
    def from_json(cls, s: str) -> "JobDescriptor":
        """Parse a JSON string back into a JobDescriptor.

        Silent: unknown keys in the JSON are ignored (forward-compat
        for adding new fields later). Missing required keys raise
        TypeError — let the caller decide to log+skip or fail.
        """
        data = json.loads(s)
        # Drop unknown keys so adding a new field in a newer version
        # doesn't break the current consumer's parse. Logs left to
        # the caller.
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in allowed})

    # ── QueuedJob interop ──

    @classmethod
    def from_queued_job(cls, job: "QueuedJob") -> "JobDescriptor":
        """Convert an in-process QueuedJob to a serializable descriptor.

        Drops ``_fn`` and ``_future`` — the live Python references
        that can't cross a process boundary. ``_kwargs`` is preserved
        as ``kwargs`` (must already be JSON-safe; if it contains
        DataFrames or connections, serialization fails — that's an
        upstream bug in the caller, not this class's concern).
        """
        return cls(
            id=job.id,
            workflow_id=job.workflow_id,
            workflow_name=job.workflow_name,
            project_id=job.project_id,
            workspace_id=job.workspace_id,
            environment=job.environment,
            priority=job.priority,
            queued_at=job.queued_at.isoformat(),
            triggered_by=job.triggered_by,
            schedule_id=job.schedule_id,
            kwargs=job._kwargs or {},
        )

    def to_queued_job(self) -> "QueuedJob":
        """Reconstruct a QueuedJob for admin-page rendering.

        The ``_fn`` field is set to None — callers (the worker daemon)
        are responsible for resolving the actual callable from
        ``fn_name``. In-process dispatch should not use descriptors;
        it still uses the live QueuedJob all the way through.
        """
        from fpulse.engine.worker_pool import QueuedJob
        return QueuedJob(
            id=self.id,
            workflow_id=self.workflow_id,
            workflow_name=self.workflow_name,
            project_id=self.project_id,
            workspace_id=self.workspace_id,
            environment=self.environment,
            priority=self.priority,
            queued_at=(
                datetime.fromisoformat(self.queued_at)
                if self.queued_at
                else datetime.now(timezone.utc)
            ),
            triggered_by=self.triggered_by,
            schedule_id=self.schedule_id,
            _fn=None,
            _kwargs=self.kwargs,
        )


def new_id() -> str:
    """Helper — canonical job id format. 12-char hex from uuid4.

    Matches QueuedJob's default id generation so Redis-backed jobs
    and in-process jobs share an id namespace.
    """
    return uuid.uuid4().hex[:12]
