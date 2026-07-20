"""User-defined storage settings — persistent override for FPULSE_DATA_DIR.

Z27 (2026-05-23). Settings → Storage location card was read-only:
users had to stop F-Pulse, set the env var, move the tree, restart.
The UI now lets the user pick a new data directory and save it as
an override; the change takes effect on next startup (we don't
hot-swap a live data_dir while open file handles + scheduled writes
exist — that's a foot-gun).

Settings persist at ``<data_dir>/storage_settings.json``. On boot
``fpulse.runtime_config`` reads it AFTER the env var so explicit
operator config still wins. The boot path is intentionally separate
from this module — we only own the read/write/validate of the
override file. Booting is a fpulse.runtime_config concern.

Why a flat file (vs. a SQLite row)?
  * The override must be readable BEFORE the database opens. A boot
    sequence that needs the DB to know where the DB lives is a
    chicken-and-egg problem.
  * Backup + restore mustn't accidentally move the data directory
    out from under a half-restored database.

Shape:
    {
      "data_dir":       "/srv/fpulse/data",   # absolute path
      "set_at":         "2026-05-23T...",     # ISO timestamp
      "active_backend": "local"               # reserved for Plus
    }

OSS surface is local-only. Plus adds cloud backends through this
same store; the `active_backend` field is reserved today so the
schema doesn't need a migration when remote backends ship.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _settings_path() -> str:
    """Resolve where storage_settings.json lives.

    Intentionally NOT inside data_dir — the whole point of this file
    is to override data_dir, so it must survive the user moving the
    data tree to a new location.

    Resolution order:
      1. ``$FPULSE_CONFIG_HOME/storage_settings.json``  (operator override)
      2. ``~/.fpulse/storage_settings.json``            (per-user default)

    The HOME path is consistent across docker-compose installs,
    pip-installed installs, and Windows / macOS / Linux desktops.
    """
    explicit = (os.environ.get("FPULSE_CONFIG_HOME") or "").strip()
    base = explicit if explicit else os.path.join(os.path.expanduser("~"), ".fpulse")
    return os.path.join(base, "storage_settings.json")


def load_override() -> dict[str, Any] | None:
    """Return the persisted override dict, or None if no override is
    set. Never raises — a corrupt file is treated as "no override"
    so the boot path always falls back to the env var / default."""
    path = _settings_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict) and (loaded.get("data_dir") or "").strip():
            return loaded
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("storage settings: load failed (%s)", exc)
        return None


def save_override(data_dir: str, active_backend: str = "local") -> dict[str, Any]:
    """Persist a new data_dir override. Caller is responsible for
    validating that the path exists + is writable; we only persist.

    Returns the saved record so the API can echo it back.
    """
    path = _settings_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError as exc:
        logger.warning("storage settings: cannot create dir for %s (%s)", path, exc)
        raise
    record = {
        "data_dir": os.path.abspath(data_dir),
        "set_at": datetime.now(timezone.utc).isoformat(),
        "active_backend": active_backend or "local",
    }
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        # Best-effort tmp cleanup.
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise
    return record


def clear_override() -> None:
    """Remove the persisted override, reverting to the env var / default
    on next boot. Idempotent — no-op if nothing's saved."""
    path = _settings_path()
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError as exc:
        logger.warning("storage settings: clear failed (%s)", exc)


def validate_path(candidate: str) -> dict[str, Any]:
    """Probe ``candidate`` for usability as a data_dir target.

    Checks (best-effort, all non-destructive):
      * absolute path (warn but allow relative — boot resolves vs CWD)
      * exists OR can be created
      * is a directory
      * the process can write a temp file in it

    Returns a dict the API can serialise directly:
        { "ok": bool, "path": "/abs/path", "issues": [str, ...],
          "writable": bool, "exists": bool, "free_bytes": int|None }
    """
    issues: list[str] = []
    if not candidate or not candidate.strip():
        return {"ok": False, "path": "", "issues": ["Empty path"], "writable": False, "exists": False, "free_bytes": None}

    abs_path = os.path.abspath(candidate)
    if not os.path.isabs(candidate):
        issues.append(f"Relative path resolved to {abs_path}")

    exists = os.path.exists(abs_path)
    if exists and not os.path.isdir(abs_path):
        return {
            "ok": False,
            "path": abs_path,
            "issues": ["Path exists but is not a directory"],
            "writable": False,
            "exists": True,
            "free_bytes": None,
        }

    # If it doesn't exist yet, try to create it. We don't permanently
    # create a dir the user only wanted to validate — clean up after.
    created_for_probe = False
    if not exists:
        try:
            os.makedirs(abs_path, exist_ok=True)
            created_for_probe = True
        except OSError as exc:
            return {
                "ok": False,
                "path": abs_path,
                "issues": [f"Cannot create directory: {exc}"],
                "writable": False,
                "exists": False,
                "free_bytes": None,
            }

    # Write probe.
    writable = False
    try:
        with tempfile.NamedTemporaryFile(prefix=".fpulse_probe_", dir=abs_path, delete=True) as _:
            writable = True
    except OSError as exc:
        issues.append(f"Not writable: {exc}")

    # Free space report.
    free_bytes: int | None
    try:
        import shutil as _sh
        free_bytes = _sh.disk_usage(abs_path).free
    except OSError:
        free_bytes = None

    # If we created the dir purely for the probe and it's still empty,
    # leave it — the user is about to point F-Pulse at it. Removing
    # would be surprising. Just report success.

    ok = writable and len(issues) == 0
    return {
        "ok": ok,
        "path": abs_path,
        "issues": issues,
        "writable": writable,
        "exists": True,
        "free_bytes": free_bytes,
        "created_for_probe": created_for_probe,
    }


__all__ = [
    "load_override",
    "save_override",
    "clear_override",
    "validate_path",
]
