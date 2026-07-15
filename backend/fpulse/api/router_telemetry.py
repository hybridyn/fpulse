"""
Router telemetry — May 6 2026.

Logs every routing decision so we can see which prompts hit the agent
loop (the slow path), spot low-confidence matches that might be
mis-routes, and surface the most common fall-throughs as candidates
for new triggers.

This closes the feedback loop without requiring the user to type a
prompt, see it fall to agent, and tell me. The endpoint surfaces
those decisions automatically.

Schema (SQLite):
    router_telemetry
      id           INTEGER PRIMARY KEY
      created_at   TEXT (ISO 8601 UTC)
      prompt       TEXT
      page         TEXT
      chosen_path  TEXT       — fast-lane / clarify / auto-pin / single-shot / agent / refsub / slotfill
      intent       TEXT
      confidence   REAL
      latency_ms   INTEGER
      served_from_page  INTEGER (0/1)
      reason       TEXT       — short tag from the matcher (e.g. "exact='hi'")

Privacy: prompts are stored verbatim. Workspace-scoped by tenant when
available. No PII filter beyond what the router itself sees — the
router never sees row-level data, only on-screen entity names.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends

from fpulse.auth.deps import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(require_admin)])


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS router_telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    workspace_id TEXT,
    user_id TEXT,
    prompt TEXT NOT NULL,
    page TEXT,
    chosen_path TEXT NOT NULL,
    intent TEXT,
    confidence REAL DEFAULT 0.0,
    latency_ms INTEGER DEFAULT 0,
    served_from_page INTEGER DEFAULT 0,
    reason TEXT
);
"""

_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_router_tel_path ON router_telemetry(chosen_path, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_router_tel_time ON router_telemetry(created_at DESC)",
]


def _ensure_schema(db) -> None:
    try:
        db.execute(_SCHEMA_SQL)
        for sql in _INDEX_SQL:
            db.execute(sql)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("router_telemetry: schema init failed: %s", exc)


def log_decision(
    *,
    prompt: str,
    page: str | None,
    chosen_path: str,
    intent: str | None,
    confidence: float,
    latency_ms: int,
    served_from_page: bool,
    reason: str | None = None,
    workspace_id: str | None = None,
    user_id: str | None = None,
) -> None:
    """Fire-and-forget log of one routing decision.

    Never raises — if the DB is unavailable, the request still
    succeeds. Keeping this synchronous (no async queue) because SQLite
    inserts are sub-millisecond on modern hardware.
    """
    try:
        from fpulse.main import app_state  # type: ignore
        db = app_state.get("db") if app_state else None
        if db is None:
            return
        _ensure_schema(db)
        db.execute(
            """INSERT INTO router_telemetry
               (created_at, workspace_id, user_id, prompt, page, chosen_path,
                intent, confidence, latency_ms, served_from_page, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now(timezone.utc).isoformat(),
                workspace_id,
                user_id,
                (prompt or "")[:500],
                (page or "")[:64],
                chosen_path,
                intent,
                float(confidence or 0.0),
                int(latency_ms or 0),
                1 if served_from_page else 0,
                (reason or "")[:120],
            ),
        )
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.debug("router_telemetry log failed: %s", exc)


# ─────────────────────────────────────────────────────────────────────
# GET /api/admin/router-telemetry
# ─────────────────────────────────────────────────────────────────────


@router.get("/admin/router-telemetry")
async def router_telemetry(
    limit: int = 50,
    days: int = 7,
) -> dict[str, Any]:
    """Surface routing decisions for review.

    Returns:
      - ``summary.by_path``      counts per chosen_path in the window
      - ``summary.total``        rows in the window
      - ``top_fallthroughs``     prompts that landed on the agent loop,
                                 grouped + counted (your trigger gaps)
      - ``low_confidence``       fast-lane matches below 0.7 (potential mis-routes)
      - ``recent``               last N decisions, newest first
    """
    try:
        from fpulse.main import app_state  # type: ignore
        db = app_state.get("db") if app_state else None
    except Exception:  # noqa: BLE001
        db = None
    if db is None:
        return {"error": "telemetry store unavailable"}
    _ensure_schema(db)

    cutoff_clause = (
        "datetime(created_at) >= datetime('now', '-' || ? || ' day')"
    )
    days_arg = max(1, min(int(days or 7), 90))
    lim = max(1, min(int(limit or 50), 500))

    # by_path
    by_path: dict[str, int] = {}
    try:
        rows = db.execute(
            f"SELECT chosen_path, COUNT(*) AS n FROM router_telemetry "
            f"WHERE {cutoff_clause} GROUP BY chosen_path ORDER BY n DESC",
            (days_arg,),
        ).fetchall()
        for r in rows:
            by_path[str(r[0] if not isinstance(r, dict) else r["chosen_path"])] = int(
                r[1] if not isinstance(r, dict) else r["n"]
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("by_path query failed: %s", exc)

    total = sum(by_path.values())

    # top_fallthroughs — agent-loop hits grouped by prompt
    top_fallthroughs: list[dict[str, Any]] = []
    try:
        rows = db.execute(
            f"SELECT prompt, COUNT(*) AS n, MAX(created_at) AS last_seen "
            f"FROM router_telemetry WHERE chosen_path = 'agent' AND {cutoff_clause} "
            f"GROUP BY prompt ORDER BY n DESC, last_seen DESC LIMIT ?",
            (days_arg, lim),
        ).fetchall()
        for r in rows:
            d = dict(r) if isinstance(r, dict) else {"prompt": r[0], "n": r[1], "last_seen": r[2]}
            top_fallthroughs.append({
                "prompt": d.get("prompt") or "",
                "count": int(d.get("n") or 0),
                "last_seen": d.get("last_seen"),
            })
    except Exception as exc:  # noqa: BLE001
        logger.warning("fallthroughs query failed: %s", exc)

    # low-confidence fast-lane matches
    low_conf: list[dict[str, Any]] = []
    try:
        rows = db.execute(
            f"SELECT prompt, intent, confidence, reason, created_at "
            f"FROM router_telemetry WHERE chosen_path = 'fast-lane' "
            f"AND confidence < 0.7 AND {cutoff_clause} "
            f"ORDER BY created_at DESC LIMIT ?",
            (days_arg, min(lim, 100)),
        ).fetchall()
        for r in rows:
            d = dict(r) if isinstance(r, dict) else {
                "prompt": r[0], "intent": r[1], "confidence": r[2],
                "reason": r[3], "created_at": r[4],
            }
            low_conf.append({
                "prompt": d.get("prompt") or "",
                "intent": d.get("intent"),
                "confidence": float(d.get("confidence") or 0.0),
                "reason": d.get("reason") or "",
                "at": d.get("created_at"),
            })
    except Exception as exc:  # noqa: BLE001
        logger.warning("low_conf query failed: %s", exc)

    # recent
    recent: list[dict[str, Any]] = []
    try:
        rows = db.execute(
            f"SELECT prompt, page, chosen_path, intent, confidence, latency_ms, "
            f"served_from_page, reason, created_at "
            f"FROM router_telemetry WHERE {cutoff_clause} "
            f"ORDER BY created_at DESC LIMIT ?",
            (days_arg, lim),
        ).fetchall()
        for r in rows:
            d = dict(r) if isinstance(r, dict) else {
                "prompt": r[0], "page": r[1], "chosen_path": r[2],
                "intent": r[3], "confidence": r[4], "latency_ms": r[5],
                "served_from_page": r[6], "reason": r[7], "created_at": r[8],
            }
            recent.append({
                "prompt": d.get("prompt") or "",
                "page": d.get("page"),
                "path": d.get("chosen_path"),
                "intent": d.get("intent"),
                "confidence": float(d.get("confidence") or 0.0),
                "ms": int(d.get("latency_ms") or 0),
                "served_from_page": bool(d.get("served_from_page") or 0),
                "reason": d.get("reason") or "",
                "at": d.get("created_at"),
            })
    except Exception as exc:  # noqa: BLE001
        logger.warning("recent query failed: %s", exc)

    return {
        "summary": {"total": total, "by_path": by_path, "window_days": days_arg},
        "top_fallthroughs": top_fallthroughs,
        "low_confidence": low_conf,
        "recent": recent,
    }
