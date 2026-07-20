"""Dashboard summary API.

2026-05-22 (audit J2 + L1) — single authoritative aggregation
endpoint. Replaces the previous client-side "stitch 15 APIs in
Promise.all" pattern that:

  * showed an empty workspace when any single dependency failed
  * counted failures with different semantics on different cards
  * had no way to consume environment / project filters consistently
  * had no per-section freshness so the UI couldn't tell "stale"
    from "broken"

The endpoint returns a versioned envelope. Each subsection carries
its own status (loaded / stale / failed) so the frontend can render
a per-card warning chip instead of silently zeroing the count.

Section semantics:
  * ``loaded``  — the data is fresh
  * ``stale``   — the data is older than expected but still
                  usable (last-known-good)
  * ``failed``  — the data couldn't be fetched at all; consumers
                  should render an error chip, not a zero

The fetch is best-effort per section so one broken store doesn't
break the whole dashboard. The outer endpoint still returns 200 with
the failed sections marked accordingly — the alternative (return 500
for one broken sub-store) would mean a single bad credential check
blanks the entire screen.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from fpulse.auth.deps import current_user_optional, current_workspace_id, require_auth

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

logger = logging.getLogger(__name__)


def _safe_workspace_id(request: Request) -> str:
    try:
        return current_workspace_id(request)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("workspace resolve failed")
        raise HTTPException(500, "workspace resolve failed") from exc


# ── Section helpers ──────────────────────────────────────────────────


def _section_failed(reason: str, exc_type: str = "Exception") -> dict:
    """Build a uniform 'this section failed' envelope so the frontend
    can render a warning chip without special-casing.
    """
    return {
        "status": "failed",
        "error": f"{exc_type}: {reason}"[:300],
        "data": None,
    }


def _section_loaded(data: Any) -> dict:
    return {"status": "loaded", "data": data}


def _filter_by_project(rows: list, project_id: str | None) -> list:
    if not project_id or project_id == "default":
        return rows
    return [r for r in rows if r.get("project_id") == project_id]


def _filter_by_env(rows: list, environment: str | None, key: str = "environment") -> list:
    """Apply environment filter to rows that carry an ``environment`` field.

    Policy (locked 2026-05-22, audit O3):

      * ``None`` / ``"all"`` passes every row through (no filter).
      * ``"dev"`` or ``"prod"`` keeps rows whose env equals that value
        AND rows tagged ``"all"`` (meant for both envs).
      * Rows whose env is ``None`` / empty / missing — i.e. **legacy
        untagged rows** — are ALSO included. The OSS contract is that
        untagged rows pre-date the dev/prod split and operators see
        them in every env until they migrate the tags.

    This matches the connector model (``VALID_ENVIRONMENTS = {dev,
    prod, all}``) and is the single source of truth referenced by
    both the Dashboard summary and the Connections page. Plus tier
    can tighten this to "untagged = hidden" via a workspace setting,
    but OSS stays permissive so a fresh install doesn't look empty.
    """
    if not environment or environment == "all":
        return rows
    return [
        r for r in rows
        if r.get(key) in (environment, "all", None) or not r.get(key)
    ]


# ── Inventory section ─────────────────────────────────────────────────


def _build_inventory(workspace_id: str, project_id: str | None, environment: str | None) -> dict:
    from fpulse.main import app_state

    out: dict[str, Any] = {}
    try:
        wf_store = app_state.get("store")
        if wf_store is None:
            raise RuntimeError("workflow store unavailable")
        wfs = wf_store.list_all(workspace_id=workspace_id)
        if project_id:
            wfs = _filter_by_project(wfs, project_id)
        out["workflows"] = len(wfs)
        out["deployed_workflows"] = sum(
            1 for w in wfs if w.get("deployed_version") is not None
        )
    except Exception as exc:
        return _section_failed(str(exc), type(exc).__name__)

    try:
        proj_store = app_state.get("project_store")
        if proj_store is not None:
            projs = proj_store.list_all(workspace_id=workspace_id)
            active = [p for p in projs if (p.get("status") or "active") != "archived"]
            out["projects"] = len(active)
        else:
            out["projects"] = 0
    except Exception as exc:
        # Inventory degrades gracefully — partial counts are better
        # than no card.
        out["projects"] = None
        out["projects_error"] = f"{type(exc).__name__}: {exc}"[:200]

    try:
        conn_store = app_state.get("connection_store")
        if conn_store is not None:
            conns = conn_store.list_all(workspace_id=workspace_id)
            if isinstance(conns, list):
                if project_id:
                    # Connections can be global (project_id == None) OR
                    # project-scoped. Both appear when filtering by a
                    # specific project.
                    conns = [
                        c for c in conns
                        if (c.get("project_id") in (None, project_id))
                    ]
                if environment:
                    conns = _filter_by_env(conns, environment)
                out["connections"] = len(conns)
            else:
                out["connections"] = 0
        else:
            out["connections"] = 0
    except Exception as exc:
        out["connections"] = None
        out["connections_error"] = f"{type(exc).__name__}: {exc}"[:200]

    try:
        cred_store = app_state.get("credential_store")
        if cred_store is not None:
            creds = cred_store.list_all(workspace_id=workspace_id)
            if isinstance(creds, list):
                if environment:
                    creds = _filter_by_env(creds, environment)
                out["credentials"] = len(creds)
            else:
                out["credentials"] = 0
        else:
            out["credentials"] = 0
    except Exception as exc:
        out["credentials"] = None
        out["credentials_error"] = f"{type(exc).__name__}: {exc}"[:200]

    try:
        sched_store = app_state.get("schedule_store")
        if sched_store is not None:
            scheds = sched_store.list_all(workspace_id=workspace_id)
            if project_id and isinstance(scheds, list):
                scheds = _filter_by_project(scheds, project_id)
            out["schedules"] = len(scheds) if isinstance(scheds, list) else 0
            out["active_schedules"] = sum(
                1 for s in (scheds or []) if (s.get("enabled") if isinstance(s, dict) else getattr(s, "enabled", False))
            )
        else:
            out["schedules"] = 0
            out["active_schedules"] = 0
    except Exception as exc:
        out["schedules"] = None
        out["schedules_error"] = f"{type(exc).__name__}: {exc}"[:200]

    return _section_loaded(out)


# ── Execution stats section ─────────────────────────────────────────


def _build_executions(workspace_id: str, project_id: str | None, hours: int) -> dict:
    from fpulse.monitoring.status import normalize_status
    from fpulse.state import get_execution_store

    try:
        store = get_execution_store()
        stats = store.get_stats(hours, workspace_id=workspace_id)
    except Exception as exc:
        return _section_failed(str(exc), type(exc).__name__)

    # When project_id is supplied, get_stats currently returns
    # workspace-wide numbers. Re-compute project-filtered stats from
    # the raw list. This is heavier but accurate; the alternative is
    # threading project_id through get_stats which is a wider change.
    if project_id:
        try:
            all_recent = store.list_all(500, workspace_id=workspace_id)
            cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
            recent = []
            for d in all_recent:
                if d.get("project_id") != project_id:
                    continue
                started = d.get("started_at", "")
                try:
                    ts = datetime.fromisoformat(started).timestamp()
                    if ts > cutoff:
                        recent.append(d)
                except (ValueError, TypeError):
                    pass
            cats = {k: 0 for k in (
                "success", "failed", "running", "queued",
                "cancelled", "skipped", "unknown",
            )}
            for e in recent:
                cats[normalize_status(e.get("status"))] += 1
            total = len(recent)
            terminal = cats["success"] + cats["failed"] + cats["cancelled"] + cats["skipped"]
            stats = {
                "total": total,
                "success": cats["success"],
                "failed": cats["failed"],
                "running": cats["running"],
                "queued": cats["queued"],
                "cancelled": cats["cancelled"],
                "skipped": cats["skipped"],
                "unknown": cats["unknown"],
                "success_rate": round(cats["success"] / terminal * 100, 1) if terminal else 0,
                "avg_duration_ms": 0,  # skip avg duration for project-scoped variant
                "period_hours": hours,
            }
        except Exception as exc:
            return _section_failed(str(exc), type(exc).__name__)

    return _section_loaded(stats)


# ── Top failed / slowest / stale schedules (audit M1) ────────────────


def _build_top_failed(workspace_id: str, project_id: str | None, hours: int) -> dict:
    """Top 5 pipelines by failure count over the window."""
    from fpulse.monitoring.status import is_failed
    from fpulse.state import get_execution_store

    try:
        store = get_execution_store()
        rows = store.list_all(500, workspace_id=workspace_id)
        cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
        counter: dict[str, dict] = {}
        for r in rows:
            if project_id and r.get("project_id") != project_id:
                continue
            if not is_failed(r.get("status")):
                continue
            started = r.get("started_at", "")
            try:
                ts = datetime.fromisoformat(started).timestamp()
                if ts <= cutoff:
                    continue
            except (ValueError, TypeError):
                continue
            wid = r.get("workflow_id") or ""
            if not wid:
                continue
            if wid not in counter:
                counter[wid] = {
                    "workflow_id": wid,
                    "workflow_name": r.get("workflow_name", ""),
                    "failure_count": 0,
                    "last_failed_at": started,
                    "last_error": r.get("error_message", ""),
                }
            counter[wid]["failure_count"] += 1
        top = sorted(counter.values(), key=lambda x: x["failure_count"], reverse=True)[:5]
        return _section_loaded(top)
    except Exception as exc:
        return _section_failed(str(exc), type(exc).__name__)


def _build_slowest(workspace_id: str, project_id: str | None, hours: int) -> dict:
    """5 slowest completed runs in the window."""
    from fpulse.state import get_execution_store

    try:
        store = get_execution_store()
        rows = store.list_all(500, workspace_id=workspace_id)
        cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
        candidates = []
        for r in rows:
            if project_id and r.get("project_id") != project_id:
                continue
            if (r.get("duration_ms") or 0) <= 0:
                continue
            started = r.get("started_at", "")
            try:
                ts = datetime.fromisoformat(started).timestamp()
                if ts <= cutoff:
                    continue
            except (ValueError, TypeError):
                continue
            candidates.append({
                "execution_id": r.get("id"),
                "workflow_id": r.get("workflow_id"),
                "workflow_name": r.get("workflow_name", ""),
                "duration_ms": r.get("duration_ms"),
                "started_at": started,
                "status": r.get("status"),
            })
        slowest = sorted(candidates, key=lambda x: x["duration_ms"], reverse=True)[:5]
        return _section_loaded(slowest)
    except Exception as exc:
        return _section_failed(str(exc), type(exc).__name__)


# ── Pool + system signals ────────────────────────────────────────────


def _build_pool() -> dict:
    try:
        from fpulse.main import app_state
        pool = app_state.get("worker_pool")
        if pool is None:
            return _section_loaded(None)
        if hasattr(pool, "get_status"):
            return _section_loaded(pool.get_status())
        return _section_loaded(None)
    except Exception as exc:
        return _section_failed(str(exc), type(exc).__name__)


def _build_system() -> dict:
    """Process + host snapshot for the Dashboard's System tile.

    Field names match the frontend contract documented on
    ``DashboardPage.tsx:82`` (``DashboardStats['system']``):

      * ``rss_mb``           — process RSS in MB
      * ``vms_mb``           — process virtual-memory in MB
      * ``threads``          — live thread count
      * ``uptime_seconds``   — wall-clock seconds since process start
      * ``host.cpu_count``   — logical CPU count
      * ``host.total_memory_mb`` — host RAM in MB (drives the
        memory-% calculation on the dashboard tile)
      * ``db_files``         — ``[{path, size_bytes, size_mb}]`` —
        sum of size_bytes powers the "DB size" tile

    2026-05-28: previously returned ``uptime_sec`` (frontend reads
    ``uptime_seconds``) and omitted ``db_files`` entirely (frontend
    reads ``sys.db_files``), so both the Uptime and DB size tiles
    rendered "—" even on a healthy install. The field-name typo +
    missing key were the entire bug — psutil was healthy, the
    section was loading, the data was simply landing under the
    wrong names.
    """
    try:
        import os
        import time
        import psutil  # type: ignore
        from pathlib import Path

        proc = psutil.Process(os.getpid())
        mem = proc.memory_info()

        # Walk the data dir for .db / .db-wal / .db-shm files. Same
        # algorithm as health_memory._db_files but the dashboard tile
        # only needs total bytes, so we keep this tight (one rglob,
        # no WAL/SHM split).
        data_dir = ""
        try:
            from fpulse.main import app_state
            data_dir = app_state.get("data_dir") or os.environ.get("FPULSE_DATA_DIR", "")
        except Exception:
            data_dir = os.environ.get("FPULSE_DATA_DIR", "")

        db_files: list[dict] = []
        if data_dir:
            try:
                root = Path(data_dir)
                if root.is_dir():
                    for p in root.rglob("*.db*"):
                        if not p.is_file():
                            continue
                        try:
                            sz = p.stat().st_size
                        except OSError:
                            continue
                        db_files.append({
                            "path": str(p),
                            "size_bytes": sz,
                            # size_mb retained for legacy callers and
                            # for matching the /health/memory shape.
                            "size_mb": round(sz / (1024 * 1024), 2),
                        })
            except Exception:
                db_files = []

        return _section_loaded({
            "rss_mb": round(mem.rss / 1024 / 1024, 1),
            "vms_mb": round(mem.vms / 1024 / 1024, 1),
            "threads": proc.num_threads(),
            # 2026-05-28: renamed from `uptime_sec` to match the
            # frontend `sys.uptime_seconds` read in DashboardPage.tsx.
            "uptime_seconds": int((datetime.now(timezone.utc).timestamp() - proc.create_time())),
            # 2026-05-28: nested under `host` to match the same
            # frontend contract — `sys.host.total_memory_mb` is what
            # the memory-% calc reads.
            "host": {
                "cpu_count": psutil.cpu_count(logical=True) or 0,
                "total_memory_mb": round(psutil.virtual_memory().total / (1024 * 1024)),
            },
            # Convenience copies — the legacy tile callers still read
            # these from the flat shape.
            "host_cpu_pct": psutil.cpu_percent(interval=None),
            "host_mem_pct": psutil.virtual_memory().percent,
            # 2026-05-28: added — the DB-size dashboard tile sums
            # `f.size_bytes` across this list. Was missing entirely
            # so the tile rendered "—" on every install.
            "db_files": db_files,
        })
    except Exception as exc:
        # psutil missing is a common dev environment case — don't
        # mark the whole dashboard as failing on it.
        return _section_failed(str(exc), type(exc).__name__)


# ── The endpoint ─────────────────────────────────────────────────────


@router.get("/summary")
async def dashboard_summary(
    request: Request,
    environment: str | None = None,
    project_id: str | None = None,
    hours: int = 24,
    _user = Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Single authoritative dashboard summary.

    Query params:
      * ``environment`` — ``dev`` / ``prod`` / ``all``. None ≡ all.
      * ``project_id``  — scope to a single project. None ≡ workspace-
        wide. "default" is treated as workspace-wide (the seed
        project sentinel).
      * ``hours``       — trend window. 24 / 168 / 720 are the UI
        presets. 1..2160 accepted.

    Returns a stable JSON shape:

    ```
    {
      "version": 1,
      "generated_at": "...",
      "scope": { "workspace_id": "...", "environment": "...",
                 "project_id": "..." },
      "inventory":   { "status": "loaded", "data": {...} },
      "executions":  { "status": "loaded", "data": {...} },
      "top_failed":  { "status": "loaded", "data": [...] },
      "slowest":     { "status": "loaded", "data": [...] },
      "pool":        { "status": "loaded", "data": {...} },
      "system":      { "status": "loaded", "data": {...} }
    }
    ```

    Any section may return ``{"status": "failed", "error": "...",
    "data": null}`` instead — the rest of the payload is unaffected.
    The frontend renders a small warning chip on failed sections
    rather than zeroing the KPI.
    """
    if hours < 1 or hours > 2160:
        raise HTTPException(400, "hours must be between 1 and 2160")

    # Validate environment filter against the same set the Connections
    # API uses, but allow None ≡ all envs.
    if environment is not None and environment not in {"dev", "prod", "all"}:
        raise HTTPException(400, f"invalid environment {environment!r}; must be dev/prod/all")
    if environment == "all":
        environment = None  # internal None ≡ no filter

    # Project ACL gate — same helper as workflows/list (audit E1).
    if project_id and project_id != "default":
        try:
            user = current_user_optional(request)
            if user is not None:
                from fpulse.projects.acl import assert_project_access
                assert_project_access(project_id, workspace_id, user, action="dashboard_view")
        except HTTPException:
            raise
        except Exception:
            # Best-effort ACL — fall through. The data section helpers
            # still scope by workspace_id which is the load-bearing
            # boundary.
            pass

    return {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": {
            "workspace_id": workspace_id,
            "environment": environment or "all",
            "project_id": project_id or None,
            "hours": hours,
        },
        "inventory":  _build_inventory(workspace_id, project_id, environment),
        "executions": _build_executions(workspace_id, project_id, hours),
        "top_failed": _build_top_failed(workspace_id, project_id, hours),
        "slowest":    _build_slowest(workspace_id, project_id, hours),
        "pool":       _build_pool(),
        "system":     _build_system(),
    }


# ── Needs Attention (V11 — 2026-05-26) ──────────────────────────────────
#
# A focused payload for the V5 "health-driven dashboard layout" — a
# single API call that returns the items the operator should look at
# right now. Empty list = nothing to worry about (the dashboard renders
# a positive "Everything's running smoothly" state in that case).
#
# Each attention item has a stable shape:
#   {
#     "category":     str,   # stable machine code (see _CATEGORIES below)
#     "severity":     "critical" | "warning" | "info",
#     "title":        str,   # one-line human summary
#     "detail":       str,   # short follow-on sentence
#     "count":        int,   # how many things this is reporting on
#     "action_page":  str,   # frontend page hash to navigate to ("pipelines"...)
#     "action_label": str,   # what the action button should read ("View failures")
#   }
#
# The frontend renders the list newest-severity-first so critical
# items always lead. When there's nothing to show, the dashboard's
# "Needs Attention" card collapses to a single calm row.
#
# This is intentionally a separate endpoint from `/summary` rather than
# a sub-section: the dashboard refreshes it on a faster cadence than the
# heavy summary (every 30s vs every 5m), so coupling them would force
# the heavy aggregation to re-run on every poll.


def _category_recent_failures(workspace_id: str, project_id: str | None, environment: str | None) -> dict | None:
    """Pipelines that failed in the last 24h. Reuses the same execution
    store that drives `_build_top_failed`; we just count + summarise."""
    from fpulse.main import app_state

    store = app_state.get("execution_store")
    if store is None:
        return None
    try:
        # 24h window matches the dashboard's default trend lens.
        all_recent = store.list_recent(workspace_id=workspace_id, hours=24)
    except Exception:
        return None
    if not isinstance(all_recent, list):
        return None

    failures = [r for r in all_recent if (r.get("status") or "").lower() == "error"]
    if project_id and project_id != "default":
        failures = [r for r in failures if r.get("project_id") == project_id]
    if environment:
        failures = _filter_by_env(failures, environment)
    if not failures:
        return None

    # Distinct pipelines for the count headline; total runs in detail.
    distinct_pipelines = len({r.get("workflow_id") for r in failures if r.get("workflow_id")})
    return {
        "category": "recent_failures",
        "severity": "critical" if distinct_pipelines >= 3 else "warning",
        "title": (
            f"{distinct_pipelines} pipeline{'s' if distinct_pipelines != 1 else ''} failed"
        ),
        "detail": (
            f"{len(failures)} failed run{'s' if len(failures) != 1 else ''} in the last 24h."
        ),
        "count": distinct_pipelines,
        "action_page": "executions",
        "action_label": "View failures",
    }


def _category_ai_provider(workspace_id: str) -> dict | None:
    """AI provider configured but unreachable. Best-effort — if the
    probe itself fails, we say nothing rather than misreporting."""
    from fpulse.main import app_state

    # The AI config store is one of the optional services in OSS; if it
    # isn't wired up, skip silently.
    cfg_store = app_state.get("ai_config_store")
    if cfg_store is None:
        return None
    try:
        cfg = cfg_store.get(workspace_id=workspace_id)
    except Exception:
        return None
    if not isinstance(cfg, dict):
        return None

    provider = (cfg.get("provider") or "").strip().lower()
    if not provider or provider == "none":
        return None  # No provider configured ≡ no expectation of one being reachable.

    # If the config carries a last-probe field that says unreachable,
    # surface it. We don't run a fresh probe here — that would be slow
    # and add load on every dashboard tick. Workspace-health pollers
    # update the last_probe_* fields out-of-band.
    last_ok = cfg.get("last_probe_ok")
    if last_ok is False:
        last_err = (cfg.get("last_probe_error") or "Provider unreachable.")[:200]
        return {
            "category": "ai_provider",
            "severity": "warning",
            "title": f"AI provider unreachable ({provider})",
            "detail": last_err,
            "count": 1,
            "action_page": "settings",
            "action_label": "Open AI settings",
        }
    return None


def _category_schedule_miss(workspace_id: str, project_id: str | None) -> dict | None:
    """Scheduled pipelines that didn't fire within their grace window.
    Reads from the notifications store — the watchdog writes
    ON_SCHEDULE_MISS events there when the scheduler's poll detects a
    skipped fire."""
    from fpulse.main import app_state

    notif_store = app_state.get("notification_store")
    if notif_store is None:
        return None
    try:
        recent = notif_store.list_recent(workspace_id=workspace_id, hours=24)
    except Exception:
        return None
    if not isinstance(recent, list):
        return None

    misses = [
        n for n in recent
        if (n.get("event") or "").upper() == "ON_SCHEDULE_MISS"
    ]
    if project_id and project_id != "default":
        misses = [n for n in misses if n.get("project_id") == project_id]
    if not misses:
        return None

    distinct = len({n.get("workflow_id") for n in misses if n.get("workflow_id")})
    return {
        "category": "schedule_miss",
        "severity": "warning",
        "title": (
            f"{distinct} schedule{'s' if distinct != 1 else ''} missed"
        ),
        "detail": (
            f"{len(misses)} schedule-miss event{'s' if len(misses) != 1 else ''} in the last 24h. "
            "Check the scheduler and the affected pipelines."
        ),
        "count": distinct,
        "action_page": "notifications",
        "action_label": "View schedule misses",
    }


# Severity → numeric so we can sort critical-first regardless of category order.
_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


@router.get("/needs-attention")
async def needs_attention(
    request: Request,
    environment: str | None = None,
    project_id: str | None = None,
    _user = Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict:
    """Aggregate the dashboard 'Needs Attention' payload (V11).

    Returns:
        {
          "version": 1,
          "generated_at": "<iso>",
          "items": [ ... ],            # severity-sorted; empty = all-clear
          "checked_categories": [...]  # so the UI can show "we looked at X"
        }

    Each item carries category / severity / title / detail / count /
    action_page / action_label — see the module docstring above for the
    contract. Frontend renders item.title prominently, item.detail as
    sub-text, action button labeled by action_label and navigating to
    action_page.
    """
    if environment is not None and environment not in {"dev", "prod", "all"}:
        raise HTTPException(400, "invalid environment; must be dev / prod / all")
    if environment == "all":
        environment = None

    items: list[dict] = []
    checked: list[str] = ["recent_failures", "ai_provider", "schedule_miss"]

    for builder, args in (
        (_category_recent_failures, (workspace_id, project_id, environment)),
        (_category_ai_provider, (workspace_id,)),
        (_category_schedule_miss, (workspace_id, project_id)),
    ):
        try:
            item = builder(*args)
            if item:
                items.append(item)
        except Exception as exc:
            # Don't let a broken builder mask the others — log and
            # continue so the UI gets a partial-but-honest payload.
            logger.warning("needs-attention builder %s crashed: %s", builder.__name__, exc)

    items.sort(key=lambda it: _SEVERITY_ORDER.get(it.get("severity", "info"), 2))

    return {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
        "checked_categories": checked,
    }
