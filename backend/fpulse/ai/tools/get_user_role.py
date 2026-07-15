"""get_user_role — read-only. Caller identity + RBAC introspection."""

from __future__ import annotations

from typing import Any

from fpulse.ai.tools.base import ToolContext, ToolDefinition, ToolTier


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    # Lazy import — rbac.py imports tools.base which triggers this package's
    # __init__.py at module load. Top-level rbac import causes a circular
    # ImportError. Resolved lazily here so handler-call time has the full
    # symbol available.
    from fpulse.ai.rbac import allowed_tiers_for, role_rank
    # Pull role from PageContext indirectly: ctx doesn't carry role, but the
    # endpoint sets PageContext.role from the authenticated user. Tools can
    # only see ToolContext fields. We approximate "role" via ctx fields the
    # endpoint already populates plus a best-effort lookup of the live user.
    role = "unknown"
    is_authenticated = bool(ctx.user_id and ctx.user_id != "anonymous")
    try:
        if is_authenticated:
            from fpulse.main import app_state  # type: ignore
            user_store = app_state.get("user_store")
            if user_store is not None:
                u = user_store.get(ctx.user_id)
                if u is not None:
                    role = (getattr(u, "role", "") or "").lower() or "viewer"
        if role == "unknown":
            role = "viewer" if not is_authenticated else "viewer"
    except Exception:
        role = "viewer"

    env = ctx.environment or "dev"
    allowed = [t.value for t in allowed_tiers_for(role, env)]

    return {
        "user_id": ctx.user_id or "anonymous",
        "role": role,
        "environment": env,
        "workspace_id": ctx.workspace_id or ctx.tenant_id or "default",
        "is_authenticated": is_authenticated,
        "role_rank": role_rank(role),
        "allowed_tool_tiers": allowed,
    }


DEFINITION = ToolDefinition(
    name="get_user_role",
    tier=ToolTier.READ,
    description=(
        "Return who the caller is, their role, and which tool tiers they're allowed "
        "to invoke in the current environment. Use when the user asks 'who am I?', "
        "'what can I do?', 'what's my role?', or when you need to tailor a response "
        "to the caller's permission level."
    ),
    input_schema={"type": "object", "properties": {}},
    output_schema={
        "user_id": "str",
        "role": "str",
        "environment": "str",
        "workspace_id": "str",
        "is_authenticated": "bool",
        "role_rank": "int",
        "allowed_tool_tiers": "list",
    },
    handler=_handler,
    requires_idempotency_key=False,
    tags=["identity", "read"],
)
