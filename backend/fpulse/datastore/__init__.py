"""Workspace datastore — the Storage surface backing the Storage page.

Why this isn't called ``fpulse.storage``:

  * ``fpulse.storage`` is already taken by the SQLite database layer
    (``storage/database.py`` etc). That module is the system-level
    persistence engine.

  * This package is the *user-visible* data home — uploads, managed
    Parquet tables, pipeline outputs, soft-deleted trash — the kind of
    thing a user sees on the Storage page and references from pipeline
    nodes by name (``default.customers``) rather than by filesystem path.

The two are deliberately separate. The database is for system state
(workflows, executions, connection rows); the datastore is for the
data itself.

Layout under ``$FPULSE_DATA_DIR``:

    uploads/{workspace_id}/{stored_filename}        — raw user uploads
    outputs/{workspace_id}/{pipeline_id}/{run_id}/  — pipeline-generated
    tables/{workspace_id}/{schema}/{name}/part-*.parquet — managed tables
    trash/{workspace_id}/{stored_filename}          — soft-deleted files

Metadata lives in SQLite (storage_objects / storage_tables /
storage_columns tables; added in schema v25). Filesystem is the
source of truth for bytes; SQLite is the index for everything else
(size, tags, soft-delete state, promoted-table back-reference).

Public surface:

  models.StorageObject / StorageTable / StorageColumn
  store.get_store() / DataStore class
  paths.workspace_paths() — resolves all four roots given a data_dir
  reconcile.reconcile_objects() — back-fills storage_objects from disk
"""

from fpulse.datastore.models import (
    StorageObject,
    StorageTable,
    StorageColumn,
    OBJECT_KIND_FILE,
    OBJECT_KIND_OUTPUT,
)
from fpulse.datastore.paths import workspace_paths, WorkspacePaths
from fpulse.datastore.store import DataStore, get_store

__all__ = [
    "DataStore",
    "OBJECT_KIND_FILE",
    "OBJECT_KIND_OUTPUT",
    "StorageColumn",
    "StorageObject",
    "StorageTable",
    "WorkspacePaths",
    "get_store",
    "workspace_paths",
]
