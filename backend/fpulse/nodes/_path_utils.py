"""Path-resolution helpers shared across source / activity nodes.

Centralizes the "resolve a user-provided relative file path" logic so
every source node behaves the same way and gets the same fallback
behaviour for free.

Historical bug this fixes (2026-05-26):
    Pipeline JSON paths like `samples/free-api-pipelines/data/foo.csv`
    are project-root-relative (that's where the OSS sample pack lives
    on disk), but the original `_resolve` blindly joined them with
    `ctx.data_dir` (`<project>/data/samples/`). The result was a
    doubled-up path `<project>/data/samples/samples/free-api-pipelines/
    data/foo.csv` that didn't exist, so every file_source in the new
    demo pipelines errored on first Run until the data files were
    hand-copied into the doubled location.

`resolve_input_path` now tries the data-dir-relative path FIRST
(preserves all existing behaviour) and falls back to the CWD-relative
path SECOND (catches sample-pack project-relative paths). The function
is read-only: it never returns a path that exists nowhere — when both
candidates miss, it returns the primary path so the caller's existing
"not found" error message stays sensible and points at the location
the user most likely expected.

Writes (sinks, output, table sinks) deliberately keep their original
data-dir-only behaviour — a sink writing to "report.csv" should always
land under data_dir, never accidentally drop a file into the project
source tree.
"""
from __future__ import annotations

import os


def resolve_input_path(file_path: str, data_dir: str) -> str:
    """Resolve a user-provided file path for READS, with smart fallback.

    Resolution order:
      1. Absolute path  → returned as-is.
      2. `<data_dir>/<file_path>` → returned if the file exists there.
      3. `<cwd>/<file_path>` → returned if the file exists there.
      4. Otherwise → returns the data_dir-joined path (which is what
         the caller will most often want to mention in the not-found
         error).
    """
    if not file_path:
        return file_path
    if os.path.isabs(file_path):
        return file_path

    primary = os.path.join(data_dir, file_path)
    if os.path.isfile(primary):
        return primary

    # Fallback — handles project-root-relative paths like
    # "samples/free-api-pipelines/data/orders.csv".
    fallback = os.path.abspath(file_path)
    if os.path.isfile(fallback):
        return fallback

    return primary
