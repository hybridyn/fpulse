"""
WebSocket endpoint for real-time workflow execution streaming.

Clients connect to /ws/execution/{workflow_id} and receive live events
as each step starts, completes, or fails. Supports cancellation via
client messages.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from fpulse.engine.realtime import RealtimeExecutor

router = APIRouter(tags=["websocket"])
logger = logging.getLogger(__name__)


# WebSocket close codes
_WS_POLICY_VIOLATION = 1008  # RFC 6455 — used for auth/tenant failures


def _resolve_ws_workspace(token: str, explicit_workspace: str) -> tuple[str | None, str | None]:
    """Resolve (workspace_id, error) for a WebSocket handshake.

    Browsers can't set arbitrary headers on ``new WebSocket()``, so the
    client has to pass ``token`` and ``workspace_id`` as query params
    on the connect URL. This helper duplicates the logic in
    ``auth.deps.current_workspace_id`` without the FastAPI Request
    plumbing:

    - token must map to a live session
    - the user must be active
    - if explicit_workspace is set, the user must be a member (or an
      instance admin)
    - otherwise we pick the user's first membership, falling back to
      ``'default'`` so local-dev and fresh installs still work

    Returns ``(workspace_id, None)`` on success or ``(None, reason)``
    on failure — callers map the reason into a 1008 close frame.
    """
    from fpulse.main import app_state
    from fpulse.auth.deps import ADMIN_ROLES

    user_store = app_state.get("user_store")
    if not user_store:
        # Legacy install without auth — degrade to default workspace
        # so the pipeline still runs on local-dev setups.
        return (explicit_workspace or "default"), None

    if not token:
        return None, "auth token required (pass ?token=... on connect)"

    try:
        user = user_store.get_user_for_session(token)
    except Exception:
        user = None
    if not user:
        return None, "invalid or expired session"
    if not getattr(user, "is_active", True):
        return None, "account is deactivated"

    ws_store = app_state.get("workspace_store")
    if not ws_store:
        return (explicit_workspace or "default"), None

    explicit = (explicit_workspace or "").strip()
    if explicit:
        if user.role in ADMIN_ROLES or ws_store.is_member(explicit, user.id):
            return explicit, None
        return None, f"not a member of workspace {explicit!r}"

    try:
        memberships = ws_store.list_for_user(user.id)
        if memberships:
            return memberships[0].id, None
    except Exception:
        pass
    return "default", None


class ConnectionManager:
    """Manage WebSocket connections grouped by workflow ID."""

    def __init__(self):
        self.active_connections: dict[str, list[WebSocket]] = {}
        self._executors: dict[str, RealtimeExecutor] = {}

    async def connect(self, websocket: WebSocket, workflow_id: str):
        """Accept a WebSocket connection and register it."""
        await websocket.accept()
        if workflow_id not in self.active_connections:
            self.active_connections[workflow_id] = []
        self.active_connections[workflow_id].append(websocket)
        logger.info("WebSocket connected: workflow=%s (total=%d)",
                     workflow_id, len(self.active_connections[workflow_id]))

    def disconnect(self, websocket: WebSocket, workflow_id: str):
        """Remove a WebSocket connection."""
        conns = self.active_connections.get(workflow_id, [])
        if websocket in conns:
            conns.remove(websocket)
        if not conns:
            self.active_connections.pop(workflow_id, None)
        logger.info("WebSocket disconnected: workflow=%s", workflow_id)

    async def broadcast(self, workflow_id: str, message: dict[str, Any]):
        """Send a message to all connections watching a workflow."""
        conns = self.active_connections.get(workflow_id, [])
        if not conns:
            return

        payload = json.dumps(message, default=str)
        disconnected = []

        for ws in conns:
            try:
                await ws.send_text(payload)
            except Exception:
                disconnected.append(ws)

        # Clean up broken connections
        for ws in disconnected:
            self.disconnect(ws, workflow_id)

    def register_executor(self, workflow_id: str, executor: RealtimeExecutor):
        """Track an active executor for cancellation support."""
        self._executors[workflow_id] = executor

    def unregister_executor(self, workflow_id: str):
        """Remove an executor reference after execution completes."""
        self._executors.pop(workflow_id, None)

    def cancel_execution(self, workflow_id: str) -> bool:
        """Cancel a running execution if one exists."""
        executor = self._executors.get(workflow_id)
        if executor:
            executor.cancel()
            return True
        return False

    def get_connection_count(self, workflow_id: str) -> int:
        """Get the number of active connections for a workflow."""
        return len(self.active_connections.get(workflow_id, []))

    def get_all_connections(self) -> dict[str, int]:
        """Get connection counts for all workflows."""
        return {
            wf_id: len(conns)
            for wf_id, conns in self.active_connections.items()
        }


# Singleton manager
manager = ConnectionManager()


def _get_app_state():
    """Lazy import to avoid circular dependency."""
    from fpulse.main import app_state
    return app_state


@router.websocket("/ws/execution/{workflow_id}")
async def execution_ws(
    websocket: WebSocket,
    workflow_id: str,
    token: str = Query(default=""),
    workspace_id: str = Query(default=""),
):
    """WebSocket endpoint for real-time execution monitoring.

    Connect to receive live events during workflow execution.
    Send JSON messages to control execution:
        {"action": "execute"}              — start execution
        {"action": "execute_step", "step_id": "..."} — execute single step
        {"action": "cancel"}               — cancel running execution
        {"action": "ping"}                 — keepalive ping

    Authentication & workspace scoping:
        The browser can't set custom headers on ``new WebSocket()``, so
        callers must pass ``?token=...&workspace_id=...`` on the connect
        URL. We resolve the workspace exactly the same way the HTTP
        dependency does (membership check, admin bypass) and **bind it
        to the connection for its entire lifetime** — every subsequent
        action is executed in that workspace. The WS is closed with
        code 1008 if auth fails or the user isn't a member of the
        requested workspace, so a client can't silently fall through
        to the wrong tenant.
    """
    resolved_ws, error = _resolve_ws_workspace(token, workspace_id)
    if error:
        # Accept long enough to send a reason, then close with a policy
        # violation. A browser client reading `event.reason` will see
        # the human-readable message; raw clients just get the code.
        await websocket.accept()
        try:
            await websocket.send_text(json.dumps({
                "type": "error",
                "error": f"websocket auth failed: {error}",
            }))
        except Exception:
            pass
        await websocket.close(code=_WS_POLICY_VIOLATION, reason=error[:120])
        return

    await manager.connect(websocket, workflow_id)

    try:
        while True:
            raw = await websocket.receive_text()

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "error": "Invalid JSON message",
                }))
                continue

            action = msg.get("action", "")

            if action == "ping":
                await websocket.send_text(json.dumps({
                    "type": "pong",
                    "workflow_id": workflow_id,
                }))

            elif action == "execute":
                preview_limit = msg.get("preview_limit", 50)
                await _execute_workflow_realtime(
                    websocket, workflow_id, preview_limit,
                    workspace_id=resolved_ws,
                )

            elif action == "execute_step":
                step_id = msg.get("step_id")
                if not step_id:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "error": "step_id is required for execute_step",
                    }))
                    continue
                preview_limit = msg.get("preview_limit", 50)
                await _execute_step_realtime(
                    websocket, workflow_id, step_id, preview_limit,
                    workspace_id=resolved_ws,
                )

            elif action == "cancel":
                cancelled = manager.cancel_execution(workflow_id)
                await websocket.send_text(json.dumps({
                    "type": "cancel_ack",
                    "workflow_id": workflow_id,
                    "was_running": cancelled,
                }))

            elif action == "status":
                await websocket.send_text(json.dumps({
                    "type": "status",
                    "workflow_id": workflow_id,
                    "active_connections": manager.get_connection_count(workflow_id),
                    "is_executing": workflow_id in manager._executors,
                }))

            else:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "error": f"Unknown action: {action}",
                    "supported_actions": ["execute", "execute_step", "cancel", "ping", "status"],
                }))

    except WebSocketDisconnect:
        manager.disconnect(websocket, workflow_id)
    except Exception as e:
        logger.error("WebSocket error for workflow %s: %s", workflow_id, e)
        manager.disconnect(websocket, workflow_id)


async def _execute_workflow_realtime(
    websocket: WebSocket,
    workflow_id: str,
    preview_limit: int,
    workspace_id: str = "default",
):
    """Run a full workflow with real-time event streaming.

    ``workspace_id`` is the tenant resolved on the handshake and is
    **not** client-controlled after that point — it's bound to the
    connection, so a client cannot pivot to another tenant's pipeline
    mid-session by sending a different workflow id. A cross-tenant
    lookup surfaces as a plain "Workflow not found" error, identical
    to a genuinely missing record.
    """
    app_state = _get_app_state()
    # 2026-05-22: use fpulse.state helpers for raise-on-missing semantics
    # on the wired services; data_dir is a path string, still read direct.
    from fpulse.state import get_workflow_store, get_execution_store
    store = get_workflow_store()
    data_dir = app_state["data_dir"]
    exe_store = get_execution_store()

    v = store.get(workflow_id, workspace_id=workspace_id)
    if not v:
        await websocket.send_text(json.dumps({
            "type": "error",
            "error": "Workflow not found",
        }))
        return

    wf = v.workflow
    event_queue: asyncio.Queue = asyncio.Queue()

    def on_event(event: dict):
        """Callback from executor — put event on the async queue."""
        try:
            event_queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

    executor = RealtimeExecutor(
        data_dir=data_dir,
        on_event=on_event,
        step_output_store=app_state.get("step_output_store"),
    )
    manager.register_executor(workflow_id, executor)

    async def run_executor():
        """Run the synchronous executor in a thread."""
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, executor.execute_workflow, wf, preview_limit,
        )

    async def stream_events():
        """Forward events from the queue to WebSocket clients."""
        while True:
            try:
                event = await asyncio.wait_for(event_queue.get(), timeout=0.1)
                await manager.broadcast(workflow_id, event)
            except asyncio.TimeoutError:
                continue
            except Exception:
                break

    # Run executor and event streamer concurrently
    exec_task = asyncio.create_task(run_executor())
    stream_task = asyncio.create_task(stream_events())

    try:
        result = await exec_task

        # Drain remaining events
        await asyncio.sleep(0.1)
        while not event_queue.empty():
            event = event_queue.get_nowait()
            await manager.broadcast(workflow_id, event)

        # Store execution log if execution_log_store is available
        log_store = app_state.get("execution_log_store")
        if log_store:
            from fpulse.monitoring.store import ExecutionRecord, StepLog
            import time

            exe = ExecutionRecord(
                workflow_id=workflow_id,
                workflow_name=wf.name,
                project_id=getattr(wf, "project_id", "default"),
                status=result.status,
                steps_total=len(wf.steps),
                completed_at=result.completed_at,
                duration_ms=result.duration_ms or 0,
                steps_completed=len([
                    r for r in result.step_results.values()
                    if r.status == "success"
                ]),
                steps_failed=len([
                    r for r in result.step_results.values()
                    if r.status == "error"
                ]),
                workflow_snapshot=wf.model_dump(mode="json"),
            )

            # Build step logs
            for step in wf.steps:
                sr = result.step_results.get(step.id)
                if sr:
                    exe.step_logs.append(StepLog(
                        step_id=step.id,
                        step_name=step.label or step.id,
                        step_type=step.type.value if hasattr(step.type, "value") else str(step.type),
                        status=sr.status,
                        rows_processed=sr.row_count,
                        duration_ms=sr.duration_ms,
                        error_message=sr.error,
                    ))

            exe_store.record(exe)

            # Also store detailed execution log. Stamp the parent
            # workflow's workspace so tenant-scoped readers only see
            # activity for their own workspace — this is the only
            # path the WebSocket executor takes to log_execution.
            log_store.log_execution(
                execution_id=exe.id,
                workflow_id=workflow_id,
                workflow_name=wf.name,
                events=executor.collected_events,
                result=result.model_dump(mode="json"),
                triggered_by="websocket",
                workspace_id=getattr(wf, "workspace_id", "default") or "default",
            )

    finally:
        stream_task.cancel()
        manager.unregister_executor(workflow_id)


async def _execute_step_realtime(
    websocket: WebSocket,
    workflow_id: str,
    step_id: str,
    preview_limit: int,
    workspace_id: str = "default",
):
    """Run a single step with real-time event streaming.

    Like :func:`_execute_workflow_realtime`, ``workspace_id`` is
    resolved once on the handshake and bound for the lifetime of the
    connection — we never re-read it from client-supplied payloads.
    """
    app_state = _get_app_state()
    store = app_state["store"]
    data_dir = app_state["data_dir"]

    v = store.get(workflow_id, workspace_id=workspace_id)
    if not v:
        await websocket.send_text(json.dumps({
            "type": "error",
            "error": "Workflow not found",
        }))
        return

    wf = v.workflow
    step = next((s for s in wf.steps if s.id == step_id), None)
    if not step:
        await websocket.send_text(json.dumps({
            "type": "error",
            "error": f"Step {step_id} not found",
        }))
        return

    event_queue: asyncio.Queue = asyncio.Queue()

    def on_event(event: dict):
        try:
            event_queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

    executor = RealtimeExecutor(
        data_dir=data_dir,
        on_event=on_event,
        step_output_store=app_state.get("step_output_store"),
    )

    async def run_step():
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, executor.execute_workflow, wf, preview_limit,
        )

    exec_task = asyncio.create_task(run_step())

    try:
        result = await exec_task

        # Drain and broadcast remaining events
        await asyncio.sleep(0.05)
        while not event_queue.empty():
            event = event_queue.get_nowait()
            await manager.broadcast(workflow_id, event)

        # Send the specific step result
        step_result = result.step_results.get(step_id)
        if step_result:
            await websocket.send_text(json.dumps({
                "type": "step_result",
                "step_id": step_id,
                "result": step_result.model_dump(mode="json"),
            }, default=str))
    finally:
        pass


# ── HTTP info endpoint ──

from fastapi import APIRouter as _AR

info_router = APIRouter(prefix="/api/ws", tags=["websocket"])


@info_router.get("/connections")
async def ws_connections():
    """Get active WebSocket connection info."""
    return {
        "connections": manager.get_all_connections(),
        "total": sum(manager.get_all_connections().values()),
    }


@info_router.post("/cancel/{workflow_id}")
async def cancel_execution_http(workflow_id: str):
    """Cancel a running execution via HTTP (alternative to WebSocket)."""
    cancelled = manager.cancel_execution(workflow_id)
    return {
        "workflow_id": workflow_id,
        "cancelled": cancelled,
    }
