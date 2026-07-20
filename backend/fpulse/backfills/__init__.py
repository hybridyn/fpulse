"""
Backfill — chunked re-execution of a pipeline over a historical date range.

Each backfill schedules N executions of the same pipeline, one per time
window, binding cursor parameters (``${param.window_start}`` /
``${param.window_end}``) to each window's bounds. Per-window state is
tracked in the ``backfill_runs`` table so a long-running backfill can be
monitored (and, in a later iteration, paused / resumed).

Two row shapes share the table:

  Parent row  — parent_backfill_id = '' (empty). Holds the user-facing
                config (start/end range, window size, concurrency,
                on_failure policy) and the aggregate status.
  Window row  — parent_backfill_id = <parent id>. One row per time
                window. Carries the cursor params binding + the
                execution_id of the actual run that processed it.

This split keeps the API simple (one table, one model) while letting the
UI render a parent backfill plus its children without a join.
"""

from .models import (
    Backfill,
    BackfillRun,
    BackfillStatus,
    WindowSize,
    OnFailure,
    BackfillCreate,
)
from .store import BackfillStore, get_backfill_store
from .windows import generate_windows, WindowBounds

__all__ = [
    "Backfill",
    "BackfillRun",
    "BackfillStatus",
    "WindowSize",
    "OnFailure",
    "BackfillCreate",
    "BackfillStore",
    "get_backfill_store",
    "generate_windows",
    "WindowBounds",
]
