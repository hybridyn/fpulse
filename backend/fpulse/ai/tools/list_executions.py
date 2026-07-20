"""list_executions — read-only. Recent pipeline runs, env-scoped by default."""

from __future__ import annotations

from typing import Any

from fpulse.ai.tools.base import ToolContext, ToolDefinition, ToolTier


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    workspace_id = inputs.get("workspace_id") or ctx.workspace_id or ctx.tenant_id or "default"
    limit = max(1, min(int(inputs.get("limit") or 20), 200))
    pipeline_id = inputs.get("pipeline_id") or None
    # Honour a status filter (e.g. "error") — previously ignored, so
    # "recent failures" returned ALL recent runs and the caller mislabelled
    # them as failures.
    status_filter = (inputs.get("status") or "").strip().lower() or None
    # The ExecutionStore persists 'error' for failed runs, but 'failed' /
    # 'timeout' / 'cancelled' also occur — treat any failure request as the
    # whole family so "recent failures" doesn't silently miss some.
    _FAILURE_STATUSES = {"error", "failed", "failure", "timeout", "cancelled", "canceled"}
    # Optional recency window — only runs started within the last N days.
    cutoff_iso: str | None = None
    _since_days = inputs.get("since_days")
    if _since_days:
        try:
            from datetime import datetime as _dt, timedelta as _td, timezone as _tz
            cutoff_iso = (_dt.now(_tz.utc) - _td(days=int(_since_days))).isoformat()
        except Exception:
            cutoff_iso = None

    # Default the env filter to the caller's current environment so DEV
    # users don't accidentally see PROD runs (and vice versa). The agent
    # can pass environment="all" to opt out when the user explicitly asks
    # for cross-env results. On OSS-only installs there is no PROD data;
    # the filter is a no-op there because no execution rows are tagged.
    env_filter = (inputs.get("environment") or ctx.environment or "").strip().lower()
    if env_filter == "all":
        env_filter = ""

    executions: list[dict[str, Any]] = []
    try:
        from fpulse.main import app_state  # type: ignore
        store = app_state.get("execution_store")
        if store is not None:
            # Widen the fetch when post-filtering by status / recency so we
            # still surface `limit` matches (failures can be sparse among the
            # most recent runs). list_all already returns started_at DESC.
            _fetch = limit * 2 if not (status_filter or cutoff_iso) else min(200, max(limit * 2, 100))
            rows = store.list_all(limit=_fetch, workspace_id=workspace_id)
            for r in rows:
                if pipeline_id and r.get("workflow_id") != pipeline_id:
                    continue
                if status_filter:
                    _rs = (r.get("status") or "").strip().lower()
                    if status_filter in _FAILURE_STATUSES:
                        if _rs not in _FAILURE_STATUSES:
                            continue
                    elif _rs != status_filter:
                        continue
                _started = r.get("started_at") or ""
                if cutoff_iso and _started and _started < cutoff_iso:
                    continue
                # env-tag is optional — only present in Plus where DEV/PROD
                # are real environments. When absent, treat as caller's env
                # (OSS is implicitly DEV).
                row_env = (r.get("environment") or ctx.environment or "dev").strip().lower()
                if env_filter and row_env != env_filter:
                    continue
                # Surface resource metrics if recorded by the psutil sampler.
                # These live in metadata for older rows; the newer schema may
                # have them as top-level columns. Read both.
                meta = r.get("metadata") or {}
                if isinstance(meta, str):
                    # Some stores serialize metadata as JSON; tolerate both.
                    try:
                        import json as _json
                        meta = _json.loads(meta) or {}
                    except Exception:
                        meta = {}
                peak_mem = (
                    r.get("peak_memory_mb")
                    or meta.get("peak_memory_mb")
                    or 0
                )
                cpu_seconds = (
                    r.get("cpu_seconds")
                    or meta.get("cpu_seconds")
                    or 0
                )
                rows_processed = (
                    r.get("total_rows_processed")
                    or meta.get("total_rows_processed")
                    or 0
                )
                executions.append({
                    "id": r.get("id", ""),
                    "workflow_id": r.get("workflow_id", ""),
                    "workflow_name": r.get("workflow_name", ""),
                    "status": r.get("status", ""),
                    "environment": row_env,
                    "started_at": r.get("started_at", ""),
                    "duration_ms": int(r.get("duration_ms", 0) or 0),
                    "peak_memory_mb": float(peak_mem) if peak_mem else 0.0,
                    "cpu_seconds": float(cpu_seconds) if cpu_seconds else 0.0,
                    "rows_processed": int(rows_processed) if rows_processed else 0,
                    "error": (r.get("error_message") or r.get("error") or "")[:200],
                    "trigger": r.get("trigger", ""),
                })
    except Exception:
        executions = []
    return {
        "executions": executions[:limit],
        "total": len(executions),
        "workspace_id": workspace_id,
        "environment_filter": env_filter or "all",
    }


DEFINITION = ToolDefinition(
    name="list_executions",
    tier=ToolTier.READ,
    description=(
        "List recent pipeline runs (executions). Returns id, workflow_id, "
        "workflow_name, status (success/failed/running), environment (dev/prod), "
        "started_at, duration_ms, peak_memory_mb (peak RAM via psutil sampler), "
        "cpu_seconds (cumulative CPU time), rows_processed, error (truncated), "
        "trigger (manual/scheduled/api). USE THIS for 'how much memory does X "
        "use', 'is anything slow', 'why did it fail' — read peak_memory_mb / "
        "cpu_seconds from each row. Defaults to caller's env — pass "
        "environment='all' for cross-env results."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pipeline_id": {"type": "string", "description": "Optional: scope to one pipeline."},
            "limit": {"type": "integer", "description": "Max rows to return (1-200, default 20)."},
            "status": {
                "type": "string",
                "enum": ["success", "failed", "error", "running"],
                "description": "Optional: only runs with this status. 'error'/'failed' matches any failure.",
            },
            "since_days": {
                "type": "integer",
                "description": "Optional: only runs started within the last N days (recency window).",
            },
            "environment": {
                "type": "string",
                "enum": ["dev", "prod", "all"],
                "description": "Filter by environment. Defaults to caller's current env.",
            },
            "workspace_id": {"type": "string"},
        },
    },
    output_schema={
        "executions": "list",
        "total": "int",
        "workspace_id": "str",
        "environment_filter": "str",
    },
    # Note: each item in `executions` carries id, workflow_id, workflow_name,
    # status, environment, started_at, duration_ms, peak_memory_mb,
    # cpu_seconds, rows_processed, error, trigger.
    handler=_handler,
    requires_idempotency_key=False,
    tags=["execution", "read", "list"],
)
