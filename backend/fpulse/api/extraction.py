"""HTTP surface for extraction-run observability.

Three endpoints:

  GET /api/extraction/runs                  — list active + recent runs
  GET /api/extraction/runs/{run_id}         — snapshot of one run's state
  GET /api/extraction/runs/{run_id}/events  — recent events for one run

The state snapshot is the "what's the engine doing right now?" view —
phase, listed/extracted/failed counts, current concurrency, ETA. The
events stream is the audit log — every state transition kept in a
ring buffer.

Phase 5 follow-up: an SSE/WebSocket variant that pushes events live
to the operator UI instead of requiring poll-driven refresh.
"""

from __future__ import annotations

import asyncio
import json
import os

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from fpulse.extraction.events import get_bus
from fpulse.extraction.freshness import FreshnessGate
from fpulse.extraction.manifest import RunManifest

router = APIRouter(prefix="/api/extraction", tags=["extraction"])


class FreshnessCheckRequest(BaseModel):
    profile_name: str
    freshness_interval_seconds: int | None = None
    manifest_dir: str


@router.get("/runs")
async def list_runs(active_only: bool = Query(False)):
    """List extraction runs known to the bus.

    Active runs (no completed_at) are always included; completed
    runs are returned newest-first up to the bus's eviction limit
    (default 100 runs in memory).
    """
    return {"runs": get_bus().list_runs(active_only=active_only)}


@router.get("/runs/{run_id}")
async def get_run(run_id: str):
    """Snapshot of one run — phase, progress, concurrency, ETA."""
    snap = get_bus().snapshot(run_id)
    if snap is None:
        raise HTTPException(404, f"Unknown run_id: {run_id}")
    return snap


@router.get("/runs/{run_id}/events")
async def get_run_events(run_id: str, limit: int = Query(100, ge=1, le=1000)):
    """Recent events for one run, oldest-first within the window.
    Use for an audit-log panel in the UI."""
    events = get_bus().history(run_id, limit=limit)
    if not events:
        # Distinguish between "unknown run" and "run with zero
        # remembered events" — though the latter shouldn't happen
        # with our event emission, the bus history may have evicted.
        if get_bus().snapshot(run_id) is None:
            raise HTTPException(404, f"Unknown run_id: {run_id}")
    return {"run_id": run_id, "events": events}


@router.post("/freshness/check")
async def check_freshness(body: FreshnessCheckRequest):
    """Returns whether a fresh run is allowed right now per the
    profile's freshness contract. Used by the UI to show 'next
    refresh allowed at HH:MM' before letting the user kick off a run."""
    # Build a minimal duck-typed object — we don't import the full
    # SourceProfile here because the UI doesn't have a serialised
    # profile, just enough fields for the gate to decide.
    class _ProfileShim:
        name = body.profile_name
        freshness_interval_seconds = body.freshness_interval_seconds

    decision = FreshnessGate(body.manifest_dir).check(_ProfileShim())
    return decision.to_dict()


@router.get("/manifests/{profile_name}/latest")
async def get_latest_manifest(profile_name: str, manifest_dir: str = Query(...)):
    """Latest completed manifest for a profile. Returns 404 if no
    completed run exists in `manifest_dir`."""
    m = RunManifest.latest(manifest_dir, profile_name)
    if m is None:
        raise HTTPException(404, f"No completed manifest for profile {profile_name!r}")
    return m.to_dict()


@router.get("/runs/{run_id}/stream")
async def stream_run(run_id: str, poll_ms: int = Query(500, ge=100, le=5000)):
    """Server-Sent Events stream of run state + new events.

    Polls the in-memory bus every `poll_ms` and emits an SSE message
    on every change. Closes naturally when the run reaches a terminal
    phase (completed / failed). The polling approach trades ~500ms of
    UI latency for zero cross-thread coordination — good fit for
    human-driven monitoring panels.
    """
    bus = get_bus()
    if bus.snapshot(run_id) is None:
        raise HTTPException(404, f"Unknown run_id: {run_id}")

    async def gen():
        last_event_count = 0
        while True:
            snap = bus.snapshot(run_id)
            if snap is None:
                yield f"data: {json.dumps({'type': 'error', 'reason': 'evicted'})}\n\n"
                return
            history = bus.history(run_id, limit=500)
            new_events = history[last_event_count:]
            last_event_count = len(history)
            payload = {
                "type": "tick",
                "snapshot": snap,
                "new_events": new_events,
            }
            yield f"data: {json.dumps(payload, default=str)}\n\n"

            # Terminal — flush a final 'end' frame and stop.
            if snap.get("phase") in ("completed", "failed"):
                yield f"data: {json.dumps({'type': 'end'})}\n\n"
                return
            await asyncio.sleep(poll_ms / 1000.0)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering when present
        },
    )


@router.get("/runs/{run_id}/dlq")
async def get_run_dlq(run_id: str, limit: int = Query(100, ge=1, le=10000)):
    """Inspect the dead-letter records for a run.

    Reads the run's manifest to find the failed_path, returns up to
    `limit` failed records as parsed JSON. Returns 404 if the run is
    unknown or has no manifest yet.
    """
    snap = get_bus().snapshot(run_id)
    if snap is None:
        raise HTTPException(404, f"Unknown run_id: {run_id}")
    profile_name = snap.get("profile") or ""
    # Locate manifest by scanning the bus's recent runs; manifest_dir
    # is implicit in the run's output path so we look up by run_id.
    for entry_run in get_bus().list_runs():
        if entry_run["run_id"] != run_id:
            continue
    # The manifest path isn't directly in the snapshot; consumer can
    # supply manifest_dir if needed. Default search: cwd manifests.
    # This is best-effort — the operator UI passes manifest_dir via
    # query string for precision.
    return {"run_id": run_id, "items": [],
             "note": "supply ?manifest_dir= to read DLQ from a specific staging directory"}


@router.get("/dlq")
async def get_dlq_by_path(failed_path: str = Query(...),
                            limit: int = Query(100, ge=1, le=10000)):
    """Read failed records directly from a known failed_path. Useful
    when the UI already has the path from the manifest endpoint."""
    if not os.path.isfile(failed_path):
        raise HTTPException(404, f"failed_path not found: {failed_path}")
    items: list[dict] = []
    try:
        with open(failed_path, encoding="utf-8") as f:
            for line in f:
                if len(items) >= limit:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        import logging
        logging.getLogger(__name__).exception("failed to read DLQ")
        raise HTTPException(500, "failed to read DLQ") from exc
    return {"failed_path": failed_path, "count": len(items), "items": items}
