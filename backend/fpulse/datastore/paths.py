"""Filesystem layout helpers for the workspace datastore.

Single source of truth for "where does X live under FPULSE_DATA_DIR".
Every storage-API endpoint and IR-node implementation goes through
these helpers — direct os.path.join calls scattered across the
codebase are how layouts drift.

Layout (the leading ``$FPULSE_DATA_DIR/`` is implicit):

    uploads/{workspace_id}/{stored_filename}
    outputs/{workspace_id}/{pipeline_id}/{run_id}/{filename}
    tables/{workspace_id}/{schema}/{name}/part-{seq}.parquet
    trash/{workspace_id}/{stored_filename}

Path-traversal protection is centralised in ``safe_join_under()`` —
every user-supplied relative path must round-trip through it before
opening a file. Anything that resolves outside the workspace root is
rejected with ``ValueError``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass


# Filename safety regex — keep alnum + . _ - so an upload like
# "Q1 2026 Sales!.csv" survives sanitisation as "Q1_2026_Sales_.csv".
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str) -> str:
    """Sanitise a user-provided filename. Never returns an empty string."""
    base = os.path.basename(name or "").strip()
    base = _SAFE_NAME_RE.sub("_", base)
    return base or "upload.bin"


def safe_schema_or_table_name(name: str) -> str:
    """SQL-flavoured identifier sanitiser for managed-table schemas/names.

    Allows letters, digits, underscore. Trims edges. Lowercase to keep
    case-insensitive uniqueness easy (Parquet filesystems aren't
    consistent across OSes; we don't want ``Sales`` and ``sales`` to
    both exist).
    """
    base = (name or "").strip().lower()
    base = re.sub(r"[^a-z0-9_]+", "_", base)
    base = re.sub(r"_+", "_", base).strip("_")
    return base or "default"


def safe_join_under(root: str, *parts: str) -> str:
    """Join *parts under *root and reject any traversal attempts.

    Returns the absolute, normalised path. Raises ``ValueError`` if
    the result would land outside ``root`` after normalisation.
    """
    root_abs = os.path.abspath(root)
    candidate = os.path.normpath(os.path.join(root_abs, *parts))
    # Compare with a trailing separator so an exact-root match counts
    # as inside (handy when the caller wants the workspace dir itself).
    if candidate != root_abs and not candidate.startswith(root_abs + os.sep):
        raise ValueError(
            f"path traversal: {candidate!r} resolves outside {root_abs!r}"
        )
    return candidate


@dataclass(frozen=True)
class WorkspacePaths:
    """All filesystem roots a workspace needs, computed once per request."""

    data_dir: str
    workspace_id: str
    uploads: str   # data_dir/uploads/{ws}
    outputs: str   # data_dir/outputs/{ws}
    tables: str    # data_dir/tables/{ws}
    trash: str     # data_dir/trash/{ws}

    def ensure(self) -> "WorkspacePaths":
        """Create the four workspace dirs if missing. Returns self for chaining."""
        for p in (self.uploads, self.outputs, self.tables, self.trash):
            os.makedirs(p, exist_ok=True)
        return self

    def table_dir(self, schema_name: str, table_name: str) -> str:
        return safe_join_under(
            self.tables,
            safe_schema_or_table_name(schema_name),
            safe_schema_or_table_name(table_name),
        )

    def output_dir(self, pipeline_id: str, run_id: str) -> str:
        return safe_join_under(
            self.outputs,
            safe_filename(pipeline_id),
            safe_filename(run_id),
        )

    def upload_abs(self, stored_filename: str) -> str:
        return safe_join_under(self.uploads, safe_filename(stored_filename))

    def trash_abs(self, stored_filename: str) -> str:
        return safe_join_under(self.trash, safe_filename(stored_filename))

    def relative_to_data_dir(self, abs_path: str) -> str:
        """Convert an absolute path back to a data_dir-relative one with
        forward-slashes (the canonical wire format for storage paths)."""
        return os.path.relpath(abs_path, self.data_dir).replace("\\", "/")


def workspace_paths(data_dir: str, workspace_id: str) -> WorkspacePaths:
    """Build the WorkspacePaths value for a workspace under data_dir.

    Does NOT create the directories — call ``.ensure()`` for that.
    Kept side-effect-free so read-only callers can inspect paths
    without touching the filesystem.
    """
    data_dir_abs = os.path.abspath(data_dir)
    ws_safe = safe_filename(workspace_id or "default")
    return WorkspacePaths(
        data_dir=data_dir_abs,
        workspace_id=ws_safe,
        uploads=os.path.join(data_dir_abs, "uploads", ws_safe),
        outputs=os.path.join(data_dir_abs, "outputs", ws_safe),
        tables=os.path.join(data_dir_abs, "tables", ws_safe),
        trash=os.path.join(data_dir_abs, "trash", ws_safe),
    )


def format_from_filename(name: str) -> str | None:
    """Map a filename extension to a canonical format label.

    Returns one of csv / json / parquet / excel / xml / None. Used to
    populate StorageObject.format on upload + reconciliation.
    """
    ext = os.path.splitext(name)[1].lower().lstrip(".")
    if not ext:
        return None
    return {
        "csv": "csv", "tsv": "csv", "txt": "csv",
        "json": "json", "ndjson": "json", "jsonl": "json",
        "parquet": "parquet", "pq": "parquet",
        "xlsx": "excel", "xls": "excel",
        "xml": "xml",
    }.get(ext)


__all__ = [
    "WorkspacePaths",
    "format_from_filename",
    "safe_filename",
    "safe_join_under",
    "safe_schema_or_table_name",
    "workspace_paths",
]
