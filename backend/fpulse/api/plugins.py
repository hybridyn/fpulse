"""Plugin System API — manage and discover installed plugins."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/plugins", tags=["plugins"])


def _get_manager(request: Request):
    # Stage 2: feature flag guard. Gated by FPULSE_ENABLE_PLUGINS.
    from fpulse.feature_flags import require
    from fpulse.main import app_state
    require("plugins")
    return app_state["plugin_manager"]


@router.get("")
async def list_plugins(request: Request):
    """List all installed plugins with status."""
    mgr = _get_manager(request)
    return mgr.list_plugins()


@router.post("/reload")
async def reload_plugins(request: Request):
    """Re-discover and reload all plugins."""
    mgr = _get_manager(request)
    return mgr.load_all()


@router.post("/{plugin_id}/enable")
async def enable_plugin(plugin_id: str, request: Request):
    mgr = _get_manager(request)
    mgr.enable_plugin(plugin_id)
    return {"status": "enabled", "plugin_id": plugin_id}


@router.post("/{plugin_id}/disable")
async def disable_plugin(plugin_id: str, request: Request):
    mgr = _get_manager(request)
    mgr.disable_plugin(plugin_id)
    return {"status": "disabled", "plugin_id": plugin_id}


@router.get("/scaffold")
async def get_scaffold(request: Request):
    """Get a scaffold template for creating a new plugin."""
    mgr = _get_manager(request)
    return mgr.get_scaffold()
