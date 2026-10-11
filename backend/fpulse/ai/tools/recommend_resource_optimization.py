"""recommend_resource_optimization — read-only.

Turns a run's *real* resource telemetry (the ResourceMonitor peak-RAM / CPU
metrics + per-step durations and row counts already captured on the execution
record) into concrete, deterministic tuning recommendations — root-cause and
optimization from the actual run, not generic advice. The Copilot explains
*above* this; the analysis itself is plain code (no LLM in the decision path),
so it can't hallucinate a bottleneck.

Given a `pipeline_id` (analyzes its most recent completed run) or an explicit
`execution_id`, it reports the bottleneck step, memory-limit/spill pressure,
CPU-bound vs I/O-bound shape, and empty/inefficient steps.
"""

from __future__ import annotations

from typing import Any

from fpulse.ai.tools.base import ToolContext, ToolDefinition, ToolTier

# DuckDB's default per-run memory_limit (worker_pool default floor). Peaks near
# this imply the run spilled to disk. It's the *default*; a priority tier may be
# higher, so this is a heuristic reference, not an exact ceiling.
_DEFAULT_DUCKDB_LIMIT_MB = 512.0
_COMPLETED = {"success", "error", "failed", "timeout", "cancelled", "canceled"}


def _blank(reason: str = "") -> dict[str, Any]:
    return {
        "analyzed": False,
        "reason": reason,
        "execution_id": "",
        "workflow_id": "",
        "status": "",
        "duration_ms": 0,
        "peak_memory_mb": 0.0,
        "cpu_seconds": 0.0,
        "steps_analyzed": 0,
        "recommendations": [],
    }


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    workspace_id = inputs.get("workspace_id") or ctx.workspace_id or ctx.tenant_id or "default"
    execution_id = (inputs.get("execution_id") or "").strip() or None
    pipeline_id = (inputs.get("pipeline_id") or "").strip() or None
    if not execution_id and not pipeline_id:
        return _blank("provide pipeline_id or execution_id")

    try:
        from fpulse.main import app_state  # type: ignore
        store = app_state.get("execution_store") if isinstance(app_state, dict) else None
        if store is None:
            return _blank("execution store unavailable")

        rec = None
        if execution_id:
            rec = store.get(execution_id, workspace_id=workspace_id)
        else:
            rows = store.list_by_workflow(pipeline_id, limit=10, workspace_id=workspace_id)
            target = next((r for r in rows if (r.get("status") or "").lower() in _COMPLETED), None)
            target = target or (rows[0] if rows else None)
            if target:
                rec = store.get(target.get("id", ""), workspace_id=workspace_id)
        if rec is None:
            return _blank("no matching run found")

        meta = getattr(rec, "metadata", None) or {}
        peak_mem = float(meta.get("peak_memory_mb") or 0)
        cpu_s = float(meta.get("cpu_seconds") or 0)
        dur_ms = float(getattr(rec, "duration_ms", 0) or 0)
        steps = [
            {
                "step": (s.step_name or s.step_id),
                "duration_ms": float(s.duration_ms or 0),
                "rows": int(s.rows_processed or 0),
                "status": (s.status or ""),
            }
            for s in (getattr(rec, "step_logs", None) or [])
        ]

        recs: list[dict[str, Any]] = []

        # 1) Bottleneck — the step owning the largest share of wall time.
        if steps and dur_ms > 0:
            slowest = max(steps, key=lambda x: x["duration_ms"])
            if slowest["duration_ms"] > 0:
                share = round(slowest["duration_ms"] / dur_ms * 100, 1)
                if share >= 40:
                    recs.append({
                        "severity": "info", "area": "bottleneck", "step": slowest["step"],
                        "message": (
                            f"Step '{slowest['step']}' took {round(slowest['duration_ms']/1000, 1)}s "
                            f"= {share}% of the {round(dur_ms/1000, 1)}s run. Optimize here first."
                        ),
                    })

        # 2) Memory / spill pressure.
        if peak_mem >= 0.8 * _DEFAULT_DUCKDB_LIMIT_MB:
            recs.append({
                "severity": "warn", "area": "memory", "step": "",
                "message": (
                    f"Peak RAM {round(peak_mem)} MB is near the ~{int(_DEFAULT_DUCKDB_LIMIT_MB)} MB default "
                    "DuckDB per-run limit — this run likely spilled to disk. Raise the worker memory_limit "
                    "for this priority, or shrink the working set (filter/select earlier, avoid wide cross-joins)."
                ),
            })

        # 3) CPU-bound vs I/O-bound shape.
        if dur_ms > 1000 and cpu_s > 0:
            wall_s = dur_ms / 1000
            if cpu_s >= 0.8 * wall_s:
                recs.append({
                    "severity": "info", "area": "cpu", "step": "",
                    "message": (
                        f"CPU time {round(cpu_s, 1)}s ≈ wall {round(wall_s, 1)}s — CPU-bound and largely "
                        "single-threaded. Reduce work (pushdown, pre-aggregate) rather than expecting more "
                        "cores to help one DuckDB run."
                    ),
                })
            elif cpu_s <= 0.3 * wall_s:
                recs.append({
                    "severity": "info", "area": "io", "step": "",
                    "message": (
                        f"CPU time {round(cpu_s, 1)}s is well below wall {round(wall_s, 1)}s — the run is "
                        "I/O-bound (waiting on a source/sink/network). Look at connector latency, batch "
                        "sizes, or the slowest source rather than compute."
                    ),
                })

        # 4) Empty-output + 5) slow-relative-to-rows steps.
        for sr in steps:
            if sr["status"] == "success" and sr["rows"] == 0:
                recs.append({
                    "severity": "warn", "area": "empty_output", "step": sr["step"],
                    "message": (
                        f"Step '{sr['step']}' completed but produced 0 rows — a filter or join may be "
                        "dropping everything upstream."
                    ),
                })
            elif sr["duration_ms"] >= 5000 and 0 < sr["rows"] < 1000:
                recs.append({
                    "severity": "info", "area": "inefficient_step", "step": sr["step"],
                    "message": (
                        f"Step '{sr['step']}' took {round(sr['duration_ms']/1000, 1)}s for only {sr['rows']} "
                        "rows — check for a missing filter/pushdown or a row-by-row operation."
                    ),
                })

        if not recs:
            recs.append({
                "severity": "info", "area": "none", "step": "",
                "message": (
                    "No obvious resource issue: within memory limits, no single-step bottleneck, and no "
                    "empty/inefficient steps detected."
                ),
            })

        return {
            "analyzed": True,
            "reason": "",
            "execution_id": getattr(rec, "id", "") or "",
            "workflow_id": getattr(rec, "workflow_id", "") or "",
            "status": getattr(rec, "status", "") or "",
            "duration_ms": int(dur_ms),
            "peak_memory_mb": round(peak_mem, 1),
            "cpu_seconds": round(cpu_s, 1),
            "steps_analyzed": len(steps),
            "recommendations": recs,
        }
    except Exception as exc:  # noqa: BLE001 — a read tool must never raise
        return _blank(f"could not analyze: {exc}")


DEFINITION = ToolDefinition(
    name="recommend_resource_optimization",
    tier=ToolTier.READ,
    description=(
        "Analyze a pipeline run's real resource telemetry (peak RAM, CPU time, "
        "per-step durations + row counts) and return concrete tuning "
        "recommendations: the bottleneck step, memory/spill pressure, CPU-bound "
        "vs I/O-bound shape, and empty/inefficient steps. USE THIS for 'why is X "
        "slow', 'how do I optimize this pipeline', 'is this run memory-bound'. "
        "Pass pipeline_id (analyzes its most recent completed run) or an explicit "
        "execution_id. Deterministic — no generic advice, only what the run shows."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pipeline_id": {"type": "string", "description": "Analyze this pipeline's most recent completed run."},
            "execution_id": {"type": "string", "description": "Analyze one specific run (overrides pipeline_id)."},
            "workspace_id": {"type": "string"},
        },
    },
    output_schema={
        "analyzed": "bool",
        "reason": "str",
        "execution_id": "str",
        "workflow_id": "str",
        "status": "str",
        "duration_ms": "int",
        "peak_memory_mb": "float",
        "cpu_seconds": "float",
        "steps_analyzed": "int",
        "recommendations": "list",
    },
    handler=_handler,
    requires_idempotency_key=False,
    tags=["execution", "read", "performance", "optimization"],
)
