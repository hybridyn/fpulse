"""Filesystem-to-metadata reconciliation.

Run once at startup to back-fill ``storage_objects`` from files that
exist on disk but predate the datastore index. Three sources:

  1. ``uploads/{ws}/*``  — files written by the legacy upload endpoint
     (kept working for back-compat) but never indexed in the new table.
  2. ``outputs/{ws}/{pipeline}/{run}/*`` — anything pipeline runs have
     dropped under the outputs root. The IR nodes will be writing
     here going forward; reconciliation catches anything that landed
     before this code shipped.
  3. ``trash/{ws}/*`` — moved-to-trash files that never made it into
     the index (shouldn't happen on a normal install, but cleanup is
     cheap and the reconciler is idempotent).

The reconciler is **add-only** — it never deletes a metadata row even
if the file on disk is gone (deletion happens through the API + the
cleanup endpoint, not the reconciler). Existing rows are left
untouched; only paths the index doesn't already know about get
inserted.

A sentinel file ``$FPULSE_DATA_DIR/.datastore-reconciled`` is touched
on first successful run so subsequent boots skip the scan unless the
sentinel is removed. Operators can force a rescan by deleting it.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Iterable

from fpulse.datastore.models import (
    OBJECT_KIND_FILE,
    OBJECT_KIND_OUTPUT,
    StorageObject,
)
from fpulse.datastore.paths import format_from_filename, workspace_paths
from fpulse.datastore.store import DataStore

logger = logging.getLogger(__name__)

_SENTINEL_NAME = ".datastore-reconciled"


def _all_workspace_ids(data_dir: str) -> list[str]:
    """Discover workspaces by walking uploads/, outputs/, tables/ roots.

    We don't have a canonical "list of workspaces" available here
    (DataStore is built before the workspace store), so the
    reconciler enumerates by directory. Always seeds 'default' even
    when no dir exists — fresh installs need an empty index row.
    """
    workspaces: set[str] = {"default"}
    for root in ("uploads", "outputs", "tables", "trash"):
        base = os.path.join(data_dir, root)
        if not os.path.isdir(base):
            continue
        try:
            for entry in os.scandir(base):
                if entry.is_dir(follow_symlinks=False):
                    workspaces.add(entry.name)
        except OSError as exc:  # noqa: BLE001
            logger.debug("reconcile: scan %s failed: %s", base, exc)
    return sorted(workspaces)


def _existing_paths(store: DataStore, workspace_id: str) -> set[str]:
    """Set of relative paths the index already knows about for this workspace."""
    rows = store.list_objects(workspace_id, include_deleted=True)
    return {r.path for r in rows}


def _scan_uploads(
    store: DataStore, data_dir: str, workspace_id: str, known: set[str],
) -> int:
    """Back-fill storage_objects rows for unknown files under uploads/{ws}/."""
    ws = workspace_paths(data_dir, workspace_id)
    if not os.path.isdir(ws.uploads):
        return 0
    inserted = 0
    for entry in os.scandir(ws.uploads):
        if not entry.is_file(follow_symlinks=False):
            continue
        rel = ws.relative_to_data_dir(entry.path)
        if rel in known:
            continue
        try:
            stat = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        obj = StorageObject(
            workspace_id=workspace_id,
            kind=OBJECT_KIND_FILE,
            name=entry.name,
            path=rel,
            format=format_from_filename(entry.name),
            size_bytes=stat.st_size,
            created_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            description="(reconciled from disk)",
        )
        store.save_object(obj)
        known.add(rel)
        inserted += 1
    return inserted


def _scan_outputs(
    store: DataStore, data_dir: str, workspace_id: str, known: set[str],
) -> int:
    """Walk outputs/{ws}/{pipeline}/{run}/* and index unknown files."""
    ws = workspace_paths(data_dir, workspace_id)
    if not os.path.isdir(ws.outputs):
        return 0
    inserted = 0
    for pipe_entry in os.scandir(ws.outputs):
        if not pipe_entry.is_dir(follow_symlinks=False):
            continue
        for run_entry in os.scandir(pipe_entry.path):
            if not run_entry.is_dir(follow_symlinks=False):
                continue
            for file_entry in os.scandir(run_entry.path):
                if not file_entry.is_file(follow_symlinks=False):
                    continue
                rel = ws.relative_to_data_dir(file_entry.path)
                if rel in known:
                    continue
                try:
                    stat = file_entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                obj = StorageObject(
                    workspace_id=workspace_id,
                    kind=OBJECT_KIND_OUTPUT,
                    name=file_entry.name,
                    path=rel,
                    format=format_from_filename(file_entry.name),
                    size_bytes=stat.st_size,
                    pipeline_id=pipe_entry.name,
                    run_id=run_entry.name,
                    created_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                    description="(reconciled from disk)",
                )
                store.save_object(obj)
                known.add(rel)
                inserted += 1
    return inserted


def reconcile_workspace(store: DataStore, data_dir: str, workspace_id: str) -> dict:
    """Back-fill one workspace. Returns a count summary."""
    known = _existing_paths(store, workspace_id)
    uploads = _scan_uploads(store, data_dir, workspace_id, known)
    outputs = _scan_outputs(store, data_dir, workspace_id, known)
    return {"workspace_id": workspace_id, "uploads_indexed": uploads, "outputs_indexed": outputs}


def reconcile_all(store: DataStore, data_dir: str, *, force: bool = False) -> list[dict]:
    """Reconcile every workspace under data_dir, gated by the sentinel file.

    Returns a per-workspace summary list. The sentinel ensures a normal
    boot skips the scan; set ``force=True`` or delete the sentinel to
    re-run.
    """
    sentinel = os.path.join(data_dir, _SENTINEL_NAME)
    if os.path.isfile(sentinel) and not force:
        return []
    os.makedirs(data_dir, exist_ok=True)

    results: list[dict] = []
    for ws_id in _all_workspace_ids(data_dir):
        try:
            results.append(reconcile_workspace(store, data_dir, ws_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("reconcile: workspace %r failed: %s", ws_id, exc)
            results.append({"workspace_id": ws_id, "error": str(exc)})

    # Touch the sentinel — even on partial failures so we don't loop
    # forever on a permanent error. Operators delete the sentinel to
    # retry after fixing whatever broke.
    try:
        with open(sentinel, "w", encoding="utf-8") as f:
            f.write(datetime.now(timezone.utc).isoformat() + "\n")
    except OSError as exc:
        logger.warning("reconcile: could not write sentinel %s: %s", sentinel, exc)

    total_uploads = sum(r.get("uploads_indexed", 0) for r in results)
    total_outputs = sum(r.get("outputs_indexed", 0) for r in results)
    if total_uploads or total_outputs:
        logger.info(
            "datastore reconcile: indexed %d uploads + %d outputs across %d workspaces",
            total_uploads, total_outputs, len(results),
        )
    return results


__all__ = ["reconcile_all", "reconcile_workspace"]
