"""test_connection tool — safe-write (live probe, no mutation).

Runs F-Pulse's existing connection tester against a SAVED connection so the
Copilot can verify a connector the user just wired up, and iterate on failures
(e.g. wrong pagination / data_path in a drafted manifest) — while a human stays
in control of activation.

Why SAFE_WRITE and not READ: it decrypts a stored credential and makes a live
outbound call (same treatment the audited /connections/{id}/test endpoint gets).
The agent NEVER receives the credential — `resolve_credentials` merges secrets
server-side and this tool returns only {success, message, suggestion, latency}.
"""

from __future__ import annotations

from typing import Any

from fpulse.ai.tools.base import ToolContext, ToolDefinition, ToolTier


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    connection_id = (inputs.get("connection_id") or "").strip()
    idempotency_key = inputs.get("idempotency_key")

    if not connection_id and ctx.selected_ids:
        connection_id = ctx.selected_ids[0]
    if not connection_id:
        raise ValueError("connection_id is required")
    if not idempotency_key:
        raise ValueError("idempotency_key is required for safe-write tools")

    if ctx.dry_run:
        return {
            "success": True,
            "connector_type": "",
            "message": "[dry-run] No live probe made.",
            "suggestion": "",
        }

    from fpulse.main import app_state

    store = app_state.get("connection_store")
    if not store:
        return {"success": False, "message": "Connection store unavailable.", "_error": "no_store"}

    conn = store.get(connection_id, workspace_id=ctx.workspace_id)
    if conn is None:
        return {
            "success": False,
            "message": f"No connection '{connection_id}' in this workspace.",
            "_error": "not_found",
        }

    from fpulse.connections.credentials import resolve_credentials

    # Secrets are merged here and stay server-side — never returned to the agent.
    config = resolve_credentials(conn)

    import anyio

    from fpulse.connections.tester import ConnectionTester

    tester = ConnectionTester()
    try:
        result = await anyio.to_thread.run_sync(tester.test_connection, conn.type, config)
    except Exception as exc:  # noqa: BLE001
        return {
            "success": False,
            "connector_type": conn.type,
            "message": f"Test raised {type(exc).__name__}: {exc}",
            "suggestion": "",
        }

    details = result.get("details") or {}
    return {
        "success": bool(result.get("success")),
        "connector_type": conn.type,
        "message": result.get("message") or result.get("error") or "",
        "suggestion": result.get("suggestion") or "",
        "latency_ms": details.get("latency_ms"),
    }


DEFINITION = ToolDefinition(
    name="test_connection",
    tier=ToolTier.SAFE_WRITE,
    description=(
        "Run a live connectivity + auth test against a SAVED connection and return whether "
        "it succeeded, a message, and a suggestion on failure. Use after the user has created "
        "a Connection (e.g. from a freshly activated drafted connector) and added its "
        "credentials, so you can confirm it works or help them fix it. Never returns "
        "credentials — only the test outcome."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "connection_id": {
                "type": "string",
                "description": "The saved connection's id. Defaults to the user's selected connection.",
            },
            "idempotency_key": {
                "type": "string",
                "description": "Required. Format: {tier}.{user_id}.{action}.{target_id}.{semver}",
            },
        },
        "required": ["idempotency_key"],
    },
    output_schema={
        "success": "bool",
        "connector_type": "str",
        "message": "str",
        "suggestion": "str",
        "latency_ms": "int?",
        "_error": "str?",
    },
    handler=_handler,
    requires_idempotency_key=True,
    tags=["connection", "test", "verify"],
)
