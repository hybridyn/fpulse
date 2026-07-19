"""Pydantic models for backfill runs.

A Backfill is the user-facing object: a date range + window size + a
pipeline reference. Every Backfill expands into N BackfillRun rows, one
per window, each of which dispatches a normal workflow execution with
the window's cursor params bound.

The two row shapes share the ``backfill_runs`` SQLite table — a parent
row carries the config (parent_backfill_id = ''), every child row carries
the same parent_backfill_id pointing at the parent. The model below
represents both shapes; ``window_start`` / ``window_end`` cover the
parent's full range on parent rows and a single window's range on child
rows.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class WindowSize(str, Enum):
    """Granularity of each backfill window."""
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    HOURLY = "hourly"
    CUSTOM = "custom"


class OnFailure(str, Enum):
    """Policy when a window fails."""
    STOP = "stop"           # halt the whole backfill on the first window failure
    CONTINUE = "continue"   # keep going; mark the window failed and move on
    RETRY_ONCE = "retry_once"  # one extra attempt before applying the next rule


class BackfillStatus(str, Enum):
    """Aggregate or per-window status."""
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"     # parent only: some windows succeeded, some failed
    CANCELLED = "cancelled"
    SKIPPED = "skipped"     # child only


class BackfillRun(BaseModel):
    """One row of the ``backfill_runs`` table.

    Used for both parent rows (the backfill itself) and child rows (each
    time window). Distinguish by inspecting ``parent_backfill_id`` —
    empty string means this row IS the parent.
    """
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    pipeline_id: str
    parent_backfill_id: str = ""    # '' = this row is the parent backfill itself

    # Window bounds. On parent rows, these span the whole user-requested
    # range. On child rows, they describe just that one window.
    window_start: str               # ISO 8601 timestamp (UTC)
    window_end: str                 # ISO 8601 timestamp (UTC, exclusive)

    # Carried through to the executor so per-window placeholders resolve.
    # On parent rows holds the template (window_start/window_end NOT yet
    # bound — they're symbolic). On child rows holds the resolved
    # parameter_values dict for the run.
    params_template: dict[str, Any] = Field(default_factory=dict)

    status: BackfillStatus = BackfillStatus.PENDING

    # Parent-only config fields (empty/default on child rows).
    window_size: WindowSize = WindowSize.DAILY
    window_size_hours: int = 24     # used when window_size == CUSTOM
    cursor_param_names: list[str] = Field(default_factory=lambda: [
        "window_start", "window_end",
    ])
    concurrency: int = 1
    on_failure: OnFailure = OnFailure.STOP
    acknowledge_side_effects: bool = False  # set True to override idempotency block

    # Child-only — the execution_id of the run dispatched for this window.
    execution_id: str = ""

    started_at: str | None = None
    completed_at: str | None = None
    error_message: str = ""

    workspace_id: str = "default"
    project_id: str = "default"

    # Aggregate counters (parent only) — convenience for the UI so it
    # doesn't have to count children on every render. Updated by the
    # store after each child status change.
    total_windows: int = 0
    succeeded_windows: int = 0
    failed_windows: int = 0
    skipped_windows: int = 0

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# Friendly alias — code outside this package reads better with ``Backfill``
# meaning "the parent row" and ``BackfillRun`` meaning "a window row",
# even though the underlying type is the same.
Backfill = BackfillRun


class BackfillCreate(BaseModel):
    """Request body for POST /api/executions/backfill."""
    pipeline_id: str
    start_date: str                          # ISO date or timestamp
    end_date: str                            # ISO date or timestamp (inclusive)
    window_size: WindowSize = WindowSize.DAILY
    window_size_hours: int = 24              # only used when window_size=custom
    cursor_param_names: list[str] = Field(default_factory=lambda: [
        "window_start", "window_end",
    ])
    concurrency: int = 1
    on_failure: OnFailure = OnFailure.STOP
    # Extra parameter overrides to pass to every windowed run (in addition
    # to the cursor params). Useful when the pipeline takes "dataset" and
    # the user wants to backfill a specific one.
    parameter_values: dict[str, Any] = Field(default_factory=dict)
    # Required when the pipeline contains an append_risky or external sink.
    # The API returns 400 without this flag for those pipelines.
    acknowledge_side_effects: bool = False
    # 2026-05-26 — Required when no source step references the cursor
    # parameter(s) in its params. Without a reference, every backfill
    # window would reprocess the same full dataset; the preflight check
    # surfaces this as HTTP 400 unless explicitly acknowledged.
    acknowledge_no_cursor_usage: bool = False
