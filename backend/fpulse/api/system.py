"""Runtime configuration + dependency-health endpoints.

  GET /api/system/runtime       → { air_gapped, verify_ssl_default }
  GET /api/system/dependencies  → { checks: [...], summary: {...} }

Air-gapped mode is set by `FPULSE_AIR_GAPPED=1`. The frontend reads
this and shows an "offline-safe" badge so the operator knows
outbound license-check / telemetry / price-feed paths are
short-circuited.

verify_ssl_default reflects `FPULSE_VERIFY_SSL`. Per-connection
overrides via `connection.config.verify_ssl` win, but the default
is surfaced here for the deployment dashboard.

The dependencies endpoint (added 2026-05-23, P0 Day 3 of the full
product validation) gives the Dashboard a single place to surface
runtime gaps — missing DuckDB, unreachable local LLM, disk pressure
— so users see WHY a Run / Test / Promote button is disabled
instead of hitting a cryptic backend error.
"""

from __future__ import annotations

import os
import shutil
from typing import Any

import httpx
from fastapi import APIRouter

from fpulse.connections.runtime import runtime_status

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/runtime")
async def get_runtime():
    return runtime_status()


# ── Dependency health ─────────────────────────────────────────────────────


# Disk-pressure thresholds for the data_dir mount. The Dashboard card
# colours by these: < warn = green, warn..crit = amber, >= crit = red.
_DISK_WARN_FREE_GB = 5.0
_DISK_CRIT_FREE_GB = 1.0
_OLLAMA_PROBE_TIMEOUT_S = 0.6


def _check_duckdb() -> dict[str, Any]:
    try:
        import duckdb  # noqa: PLC0415
        return {
            "id": "duckdb",
            "label": "DuckDB",
            "status": "ok",
            "detail": f"v{duckdb.__version__}",
            "required": True,
            "blocks": [],
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "id": "duckdb",
            "label": "DuckDB",
            "status": "missing",
            "detail": f"Import failed: {exc}",
            "required": True,
            "blocks": [
                "Storage previews",
                "Promote-to-table",
                "Managed Table Source / Sink",
                "DuckDB Transform / SQL nodes",
            ],
        }


async def _check_ollama() -> dict[str, Any]:
    """Best-effort reachability probe against the local Ollama daemon.

    Failing isn't fatal — the user might be on cloud-only AI. We
    report the result so the Insights → AI Provider page can show
    a green/amber dot. Times out fast so the dependency check stays
    snappy even when Ollama isn't installed.
    """
    base = os.environ.get("FPULSE_OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=_OLLAMA_PROBE_TIMEOUT_S) as client:
            resp = await client.get(f"{base}/api/tags")
        if resp.status_code != 200:
            return {
                "id": "ollama",
                "label": "Local LLM (Ollama)",
                "status": "warn",
                "detail": f"Reachable but returned HTTP {resp.status_code}",
                "required": False,
                "blocks": [],
            }
        body = resp.json() if resp.content else {}
        models = body.get("models") or []
        model_count = len(models)
        return {
            "id": "ollama",
            "label": "Local LLM (Ollama)",
            "status": "ok" if model_count > 0 else "warn",
            "detail": (
                f"{model_count} model{'s' if model_count != 1 else ''} installed"
                if model_count > 0
                else "Reachable, but no models pulled (try qwen2.5:7b)"
            ),
            "required": False,
            "blocks": [],
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "id": "ollama",
            "label": "Local LLM (Ollama)",
            "status": "missing",
            "detail": f"Not reachable at {base} — install + run Ollama or pick a cloud provider",
            "required": False,
            "blocks": ["AI Copilot in local-only mode"],
        }


def _check_data_dir() -> dict[str, Any]:
    """Disk-pressure check on the FPULSE_DATA_DIR mount."""
    try:
        from fpulse.main import app_state  # local import to avoid circulars
        data_dir = app_state.get("data_dir") or os.environ.get("FPULSE_DATA_DIR", "./data")
    except Exception:
        data_dir = os.environ.get("FPULSE_DATA_DIR", "./data")
    abs_dir = os.path.abspath(data_dir)

    try:
        usage = shutil.disk_usage(abs_dir)
    except OSError as exc:
        return {
            "id": "data_dir",
            "label": "Data directory",
            "status": "missing",
            "detail": f"Cannot stat {abs_dir}: {exc}",
            "required": True,
            "blocks": ["File uploads", "Pipeline outputs", "Storage page"],
        }

    free_gb = usage.free / (1024 ** 3)
    total_gb = usage.total / (1024 ** 3)
    if free_gb < _DISK_CRIT_FREE_GB:
        status = "missing"
        detail = f"Critically low: {free_gb:.1f} GB free of {total_gb:.0f} GB"
    elif free_gb < _DISK_WARN_FREE_GB:
        status = "warn"
        detail = f"Low: {free_gb:.1f} GB free of {total_gb:.0f} GB"
    else:
        status = "ok"
        detail = f"{free_gb:.1f} GB free of {total_gb:.0f} GB"
    return {
        "id": "data_dir",
        "label": "Data directory",
        "status": status,
        "detail": detail,
        "required": True,
        "blocks": ["File uploads", "Promote-to-table", "Pipeline outputs"] if status != "ok" else [],
        "extra": {
            "path": abs_dir,
            "free_bytes": usage.free,
            "total_bytes": usage.total,
        },
    }


@router.get("/dependencies")
async def get_dependencies():
    """Return the live status of every runtime dependency the UI gates on.

    Response shape:
        {
          "checks": [
            {
              "id":     "duckdb" | "ollama" | "data_dir",
              "label":  human-readable name,
              "status": "ok" | "warn" | "missing" | "error",
              "detail": one-line explanation (version / path / error),
              "required": bool,
              "blocks": list of UI surfaces this gates when not ok,
              "extra":  optional structured payload (e.g. disk numbers),
            }
          ],
          "summary": { "ok": N, "warn": N, "missing": N, "total": N },
        }

    Fast — every check has a tight timeout so the Dashboard can refresh
    this on every paint without blocking. Ollama is the only network
    probe; everything else is local introspection.
    """
    duckdb_check = _check_duckdb()
    ollama_check = await _check_ollama()
    data_dir_check = _check_data_dir()
    checks = [duckdb_check, ollama_check, data_dir_check]

    summary = {
        "ok": sum(1 for c in checks if c["status"] == "ok"),
        "warn": sum(1 for c in checks if c["status"] == "warn"),
        "missing": sum(1 for c in checks if c["status"] in ("missing", "error")),
        "total": len(checks),
    }
    return {"checks": checks, "summary": summary}
