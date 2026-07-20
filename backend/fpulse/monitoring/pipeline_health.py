"""Per-pipeline health aggregator.

Reads the existing ExecutionStore and derives operability metrics so
the operator UI can render health badges, success-rate trends, and
top-failing pipelines without re-querying raw rows on every render.

Five metrics, computed from a rolling window of the most-recent N
executions per pipeline (default 50):

  - last_run_status      : success | failed | running | never_run
  - success_rate         : completed-successfully / total in window
  - avg_duration_s       : mean of completed runs
  - p95_duration_s       : 95th-percentile of completed runs
  - last_failure_reason  : message from the most recent failure
  - mttr_s               : mean time from failure to next success
                            (None if no recovery in the window)

The status enum maps to UI badge color:
  - healthy        : success_rate ≥ 0.95 AND last_run_status='success'
  - degraded       : 0.5 ≤ success_rate < 0.95
  - unhealthy      : success_rate < 0.5 OR last_run_status='failed'
  - running        : a run is currently in flight
  - never_run      : no executions on record
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Literal

logger = logging.getLogger(__name__)


HealthBadge = Literal[
    "healthy", "degraded", "unhealthy", "running", "never_run"
]


@dataclass
class PipelineHealth:
    pipeline_id: str
    last_run_status: str = "never_run"  # success | failed | running | never_run
    last_run_at: float | None = None
    last_run_duration_s: float | None = None
    last_failure_at: float | None = None
    last_failure_reason: str | None = None
    success_count: int = 0
    failure_count: int = 0
    running_count: int = 0
    total_runs: int = 0
    success_rate: float | None = None      # None when total_runs==0
    avg_duration_s: float | None = None
    p95_duration_s: float | None = None
    mttr_s: float | None = None             # mean time to recovery
    rolling_window: int = 50

    def derive_badge(self) -> HealthBadge:
        if self.running_count > 0 and self.total_runs == self.running_count:
            return "running"
        if self.total_runs == 0:
            return "never_run"
        if self.last_run_status == "failed" and (self.success_rate or 0) < 0.5:
            return "unhealthy"
        if (self.success_rate or 0) >= 0.95 and self.last_run_status == "success":
            return "healthy"
        if (self.success_rate or 0) >= 0.5:
            return "degraded"
        return "unhealthy"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["badge"] = self.derive_badge()
        return d


# ── Aggregation primitives ──────────────────────────────────────────

def _parse_ts(value: Any) -> float | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            s = value.replace("Z", "+00:00") if value.endswith("Z") else value
            return datetime.fromisoformat(s).timestamp()
        except (TypeError, ValueError):
            return None
    if isinstance(value, datetime):
        return value.timestamp()
    return None


def _percentile(sorted_values: list[float], p: float) -> float | None:
    """Plain old percentile — k * (n-1) interpolation, no numpy."""
    if not sorted_values:
        return None
    n = len(sorted_values)
    k = (p / 100.0) * (n - 1)
    f = int(k)
    c = min(f + 1, n - 1)
    if f == c:
        return sorted_values[f]
    return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)


def compute_health(pipeline_id: str, executions: Iterable[dict],
                     *, window: int = 50) -> PipelineHealth:
    """Derive PipelineHealth from a list of execution rows.

    Caller fetches the rows from ExecutionStore.list_by_workflow() and
    passes them here. Pure function — no I/O — so it's trivially
    testable with synthetic execution sequences.
    """
    rows = list(executions)[:window]
    health = PipelineHealth(pipeline_id=pipeline_id, rolling_window=window)

    if not rows:
        return health

    health.total_runs = len(rows)

    # Most-recent first per ExecutionStore's ORDER BY started_at DESC.
    most_recent = rows[0]
    health.last_run_status = _normalise_status(most_recent.get("status", ""))
    health.last_run_at = _parse_ts(most_recent.get("started_at"))
    if most_recent.get("duration_ms"):
        health.last_run_duration_s = float(most_recent["duration_ms"]) / 1000.0

    durations: list[float] = []
    for row in rows:
        status = _normalise_status(row.get("status", ""))
        if status == "success":
            health.success_count += 1
            if row.get("duration_ms"):
                durations.append(float(row["duration_ms"]) / 1000.0)
        elif status == "failed":
            health.failure_count += 1
            ts = _parse_ts(row.get("completed_at") or row.get("started_at"))
            if ts and (health.last_failure_at is None or ts > health.last_failure_at):
                health.last_failure_at = ts
                health.last_failure_reason = (row.get("error_message") or "")[:300] or None
        elif status == "running":
            health.running_count += 1

    completed = health.success_count + health.failure_count
    if completed > 0:
        health.success_rate = round(health.success_count / completed, 4)

    if durations:
        durations.sort()
        health.avg_duration_s = round(sum(durations) / len(durations), 2)
        health.p95_duration_s = round(_percentile(durations, 95) or 0.0, 2)

    health.mttr_s = _compute_mttr(rows)
    return health


def _normalise_status(s: str) -> str:
    """Map backend status enum to the 4-state UI vocabulary.

    ExecutionStore uses 'error' for failed runs; we expose 'failed' to
    clients because that's what operators actually call them. Keep
    'queued' and 'cancelled' grouped under 'failed' for health-rate
    purposes — both indicate the pipeline didn't deliver value.
    """
    s = (s or "").lower()
    if s == "success":
        return "success"
    if s in ("error", "failed", "cancelled", "canceled"):
        return "failed"
    if s in ("running", "queued", "pending"):
        return "running"
    return "never_run"


def _compute_mttr(rows: list[dict]) -> float | None:
    """Mean time to recovery — average gap between a failure and the
    next subsequent success, in seconds. Returns None when there's no
    failure-then-success pair in the window."""
    # Rows arrive newest-first; reverse to walk chronologically.
    chronological = list(reversed(rows))
    gaps: list[float] = []
    pending_failure_ts: float | None = None
    for row in chronological:
        status = _normalise_status(row.get("status", ""))
        ts = _parse_ts(row.get("completed_at") or row.get("started_at"))
        if ts is None:
            continue
        if status == "failed" and pending_failure_ts is None:
            pending_failure_ts = ts
        elif status == "success" and pending_failure_ts is not None:
            gaps.append(ts - pending_failure_ts)
            pending_failure_ts = None
    if not gaps:
        return None
    return round(sum(gaps) / len(gaps), 2)


# ── Bulk computation across pipelines ──────────────────────────────

def compute_all_health(execution_store, *, workspace_id: str | None = None,
                          window: int = 50) -> dict[str, PipelineHealth]:
    """Returns {pipeline_id: PipelineHealth} across every pipeline that
    has executions in the workspace.

    Uses `list_recent` to discover the unique pipeline_ids, then runs
    compute_health for each. Performance is fine for the typical
    workspace (≤100 pipelines × 50 rolling-window rows = 5k row reads
    per dashboard refresh).
    """
    rows: list[dict] = []
    try:
        if workspace_id is not None:
            rows = execution_store.list_recent(
                limit=window * 100, workspace_id=workspace_id,
            )
        else:
            rows = execution_store.list_recent(limit=window * 100)
    except Exception:  # noqa: BLE001
        logger.exception("failed to load executions for health aggregation")
        return {}

    grouped: dict[str, list[dict]] = {}
    for row in rows:
        pid = row.get("workflow_id")
        if pid:
            grouped.setdefault(pid, []).append(row)

    return {pid: compute_health(pid, executions, window=window)
            for pid, executions in grouped.items()}


def health_summary(health_map: dict[str, PipelineHealth]) -> dict[str, Any]:
    """Counts by badge — drives the dashboard tile."""
    counts = {"healthy": 0, "degraded": 0, "unhealthy": 0,
              "running": 0, "never_run": 0}
    for h in health_map.values():
        counts[h.derive_badge()] += 1
    return {"total": len(health_map), **counts}
