"""HTTP surface for OAuth runtime health.

Three endpoints surface what the OAuth substrate now publishes:

  GET /api/auth/health                  — list health for every tracked connection
  GET /api/auth/health/{connection_id}  — one connection's health snapshot
  GET /api/auth/health/_summary         — counts by status (healthy / expiring_soon / stale / failed)

The status field is derived (`healthy` | `expiring_soon` | `stale` |
`failed` | `unknown`) so the UI doesn't have to re-classify on every
render.
"""

from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, HTTPException

from fpulse.connections.oauth_health import get_registry

router = APIRouter(prefix="/api/auth/health", tags=["auth-health"])


@router.get("")
async def list_health():
    """All tracked connections — newest activity first."""
    items = [h.to_dict() for h in get_registry().list_all()]
    items.sort(
        key=lambda x: (x.get("last_refresh_at") or x.get("last_failure_at") or 0),
        reverse=True,
    )
    return {"items": items, "count": len(items)}


@router.get("/_summary")
async def health_summary():
    """Counts grouped by status — drives the operator dashboard tile."""
    items = [h.to_dict() for h in get_registry().list_all()]
    counts: Counter = Counter(item["status"] for item in items)
    return {
        "total": len(items),
        "healthy":        counts.get("healthy", 0),
        "expiring_soon":  counts.get("expiring_soon", 0),
        "stale":          counts.get("stale", 0),
        "failed":         counts.get("failed", 0),
        "unknown":        counts.get("unknown", 0),
    }


@router.get("/{connection_id}")
async def get_health(connection_id: str):
    h = get_registry().get(connection_id)
    if h is None:
        raise HTTPException(404, f"No auth-health record for connection {connection_id!r}")
    return h.to_dict()
