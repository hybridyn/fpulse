"""
Event taxonomy — the concrete event classes F-Pulse publishes.

Each event has:
  - A `topic` (dot-separated, lowercase, `fpulse.`-prefixed) so the
    bus can route on string matching.
  - A `DURABILITY` class-level constant pinned at definition time
    so callers can't mis-classify.
  - Plain JSON-serializable fields. No DB rows, no SQLAlchemy
    objects, no live cursors.

Adding a new event = adding a class here + bumping `EVENT_SCHEMA_VERSION`
if the shape of an existing one changes. Subscribers should tolerate
unknown fields (forward-compat) and ignore unknown event types.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import uuid
from typing import Any, ClassVar

from .bus import DurabilityClass


EVENT_SCHEMA_VERSION = 1


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


@dataclasses.dataclass
class Event:
    """Base class for every event on the bus.

    Subclasses set TOPIC and DURABILITY at the class level; the
    instance just carries the payload + identity fields.
    """

    # Class-level metadata — overridden in subclasses.
    TOPIC: ClassVar[str] = ""
    DURABILITY: ClassVar[DurabilityClass] = DurabilityClass.BEST_EFFORT

    # Instance fields — populated by either the caller or
    # __post_init__.
    event_id: str = dataclasses.field(default_factory=lambda: str(uuid.uuid4()))
    occurred_at: str = dataclasses.field(default_factory=_utc_now)
    schema_version: int = EVENT_SCHEMA_VERSION

    # Set by the bus when persisting (DURABLE) or routing
    # (BEST_EFFORT). Subscribers should treat as opaque.
    cursor: str | None = None

    @property
    def topic(self) -> str:
        """Instance-level topic for the bus to route on.

        Subclasses with parameterized topics (e.g. per-pipeline-id
        scoping for fan-out reduction) override this. Default is
        the class-level TOPIC.
        """
        return self.TOPIC

    def to_json(self) -> dict[str, Any]:
        """JSON-serializable dict. Used by the persistence layer
        and by the wire format on NATS."""
        d = dataclasses.asdict(self)
        d["_topic"] = self.topic
        d["_type"] = type(self).__name__
        d["_durability"] = self.DURABILITY.value
        return d

    @classmethod
    def from_json(cls, blob: dict[str, Any]) -> "Event":
        """Rehydrate from to_json() output. Looks up the concrete
        class by _type — see EVENT_REGISTRY."""
        type_name = blob.get("_type")
        target = EVENT_REGISTRY.get(type_name)
        if target is None:
            raise ValueError(f"Unknown event type: {type_name!r}")
        # Strip metadata fields the dataclass doesn't know about.
        clean = {k: v for k, v in blob.items() if not k.startswith("_")}
        return target(**clean)


# ── Pipeline run lifecycle (DURABLE) ────────────────────────────
# These five events are the audit trail. Losing one means a run
# silently appears or disappears in the UI / lineage / audit log.


@dataclasses.dataclass
class PipelineRunStarted(Event):
    TOPIC: ClassVar[str] = "fpulse.pipeline.run.started"
    DURABILITY: ClassVar[DurabilityClass] = DurabilityClass.DURABLE

    run_id: str = ""
    pipeline_id: str = ""
    pipeline_version: str = ""
    triggered_by: str = ""  # "user:<id>" / "schedule:<id>" / "api"
    project_id: str = ""
    environment: str = "dev"  # dev / prod / etc.
    params: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class PipelineRunCompleted(Event):
    TOPIC: ClassVar[str] = "fpulse.pipeline.run.completed"
    DURABILITY: ClassVar[DurabilityClass] = DurabilityClass.DURABLE

    run_id: str = ""
    pipeline_id: str = ""
    duration_ms: int = 0
    rows_processed: int = 0
    step_count: int = 0


@dataclasses.dataclass
class PipelineRunFailed(Event):
    TOPIC: ClassVar[str] = "fpulse.pipeline.run.failed"
    DURABILITY: ClassVar[DurabilityClass] = DurabilityClass.DURABLE

    run_id: str = ""
    pipeline_id: str = ""
    duration_ms: int = 0
    failed_step_id: str = ""
    error_class: str = ""
    error_message: str = ""


@dataclasses.dataclass
class PipelineRunCancelled(Event):
    TOPIC: ClassVar[str] = "fpulse.pipeline.run.cancelled"
    DURABILITY: ClassVar[DurabilityClass] = DurabilityClass.DURABLE

    run_id: str = ""
    pipeline_id: str = ""
    cancelled_by: str = ""
    reason: str = ""


# ── Step lifecycle (DURABLE) ────────────────────────────────────
# Step start + terminal events are durable because they're the
# unit of replay / retry. StepProgress (below) is BEST_EFFORT.


@dataclasses.dataclass
class StepStarted(Event):
    TOPIC: ClassVar[str] = "fpulse.step.started"
    DURABILITY: ClassVar[DurabilityClass] = DurabilityClass.DURABLE

    run_id: str = ""
    step_id: str = ""
    step_type: str = ""


@dataclasses.dataclass
class StepCompleted(Event):
    TOPIC: ClassVar[str] = "fpulse.step.completed"
    DURABILITY: ClassVar[DurabilityClass] = DurabilityClass.DURABLE

    run_id: str = ""
    step_id: str = ""
    step_type: str = ""
    duration_ms: int = 0
    row_count: int = 0
    output_columns: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class StepFailed(Event):
    TOPIC: ClassVar[str] = "fpulse.step.failed"
    DURABILITY: ClassVar[DurabilityClass] = DurabilityClass.DURABLE

    run_id: str = ""
    step_id: str = ""
    step_type: str = ""
    duration_ms: int = 0
    error_class: str = ""
    error_message: str = ""


@dataclasses.dataclass
class StepSkipped(Event):
    TOPIC: ClassVar[str] = "fpulse.step.skipped"
    DURABILITY: ClassVar[DurabilityClass] = DurabilityClass.DURABLE

    run_id: str = ""
    step_id: str = ""
    reason: str = ""  # "upstream_empty" / "deactivated" / "sample_elision"


# ── Step progress (BEST_EFFORT) ─────────────────────────────────
# High-volume telemetry. Dropping one is fine; the StepCompleted
# event carries the final numbers.


@dataclasses.dataclass
class StepProgress(Event):
    TOPIC: ClassVar[str] = "fpulse.step.progress"
    DURABILITY: ClassVar[DurabilityClass] = DurabilityClass.BEST_EFFORT

    run_id: str = ""
    step_id: str = ""
    rows_so_far: int = 0
    bytes_so_far: int = 0
    pct_complete: float | None = None  # None = indeterminate


# ── Approvals (DURABLE) ─────────────────────────────────────────


@dataclasses.dataclass
class ApprovalRequested(Event):
    TOPIC: ClassVar[str] = "fpulse.approval.requested"
    DURABILITY: ClassVar[DurabilityClass] = DurabilityClass.DURABLE

    approval_id: str = ""
    pipeline_id: str = ""
    requested_by: str = ""
    target_environment: str = ""


@dataclasses.dataclass
class ApprovalGranted(Event):
    TOPIC: ClassVar[str] = "fpulse.approval.granted"
    DURABILITY: ClassVar[DurabilityClass] = DurabilityClass.DURABLE

    approval_id: str = ""
    granted_by: str = ""
    comment: str = ""


@dataclasses.dataclass
class ApprovalDenied(Event):
    TOPIC: ClassVar[str] = "fpulse.approval.denied"
    DURABILITY: ClassVar[DurabilityClass] = DurabilityClass.DURABLE

    approval_id: str = ""
    denied_by: str = ""
    reason: str = ""


# ── Alerts (DURABLE) ────────────────────────────────────────────


@dataclasses.dataclass
class AlertFired(Event):
    TOPIC: ClassVar[str] = "fpulse.alert.fired"
    DURABILITY: ClassVar[DurabilityClass] = DurabilityClass.DURABLE

    alert_id: str = ""
    rule_id: str = ""
    severity: str = ""  # "info" / "warning" / "critical"
    title: str = ""
    description: str = ""
    related_run_id: str = ""


# ── Schema drift (DURABLE) ──────────────────────────────────────
# Emitted by sinks when their schema_policy detects (and applies, or
# rejects) a change in the incoming schema vs the destination's
# existing shape. DURABLE because the audit trail of schema changes
# is the same shape of fact as run-start/run-complete: an operator
# six months from now needs to know whether the prod table ever
# evolved silently.
#
# The event carries the change summary, not the full column lists —
# the full state lives in schema_history (queryable via the API).
# An event payload over ~16 KB is on the bus's "use object storage"
# side; the summary keeps us well under that.


@dataclasses.dataclass
class SchemaDriftDetected(Event):
    TOPIC: ClassVar[str] = "fpulse.schema.drift.detected"
    DURABILITY: ClassVar[DurabilityClass] = DurabilityClass.DURABLE

    run_id: str = ""
    step_id: str = ""
    workspace_id: str = "default"
    table_id: str = ""                 # storage_tables.id of the affected table
    table_name: str = ""               # ``schema.name`` display form
    policy: str = "add_columns"        # the SchemaPolicy that applied
    severity: str = "info"             # info | warning | critical
    applied: bool = True               # True = change went through, False = rejected
    schema_version: int = 0            # the new version number in schema_history
    added_columns: list[str] = dataclasses.field(default_factory=list)
    dropped_columns: list[str] = dataclasses.field(default_factory=list)
    type_changes: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    rejection_reason: str = ""         # populated when applied=False


# ── Registry ────────────────────────────────────────────────────


EVENT_REGISTRY: dict[str, type[Event]] = {
    cls.__name__: cls
    for cls in [
        PipelineRunStarted,
        PipelineRunCompleted,
        PipelineRunFailed,
        PipelineRunCancelled,
        StepStarted,
        StepCompleted,
        StepFailed,
        StepSkipped,
        StepProgress,
        ApprovalRequested,
        ApprovalGranted,
        ApprovalDenied,
        AlertFired,
        SchemaDriftDetected,
    ]
}


def serialize(event: Event) -> str:
    """Compact JSON line for the durable log + NATS wire format."""
    return json.dumps(event.to_json(), separators=(",", ":"))


def deserialize(line: str) -> Event:
    """Inverse of serialize()."""
    return Event.from_json(json.loads(line))
