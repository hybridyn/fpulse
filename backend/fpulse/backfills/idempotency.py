"""Server-side mirror of frontend ``classifyIdempotency``.

Used by the backfill API to refuse pipelines whose sinks aren't safe
to re-run on every window — `append_risky` (each window duplicates rows)
and `external` (each window fires a real-world side effect). The
canonical reference is ``frontend/src/utils/idempotency.ts``; the two
must stay in lock-step. Kept deliberately small and free of any
imports beyond stdlib so the executor and API can call it without
pulling in heavy modules.
"""

from __future__ import annotations

from typing import Any, Iterable, Literal

IdempotencyClass = Literal[
    "safe", "replace", "merge", "append_risky", "external",
]


# Step types that are "managed-table-ish" sinks: behaviour depends on mode.
# Note: no `parquet_sink` — parquet is handled by local_table_sink, s3_sink,
# or generic `destination` with file_format=parquet.
_TABLE_LIKE_SINKS = {
    "local_table_sink", "delta_sink",
    "warehouse_sink", "db_sink",
}

# File sinks default to REPLACE; append-mode flips to risky.
_FILE_SINKS = {
    "csv_sink", "json_sink", "excel_sink", "file_sink",
    "s3_sink", "adls_gen2_sink", "azure_blob_sink", "gcs_sink",
    "sharepoint_sink", "onedrive_sink", "gdrive_sink",
    "dropbox_sink", "box_sink",
}

# Sinks that always fire a real-world side effect on every run.
_EXTERNAL_SINKS = {
    "email_sink", "api_sink", "kafka_sink", "webhook_sink",
}

# Generic placeholders the canvas uses before the user picks a concrete
# connector. Same mode-driven behaviour as the table-like sinks.
_GENERIC_SINKS = {"output", "destination"}


def classify(step_type: str, params: dict[str, Any] | None = None) -> IdempotencyClass | None:
    """Return the idempotency class for a sink given its current params.

    Returns ``None`` for non-sink types (transforms, sources). Callers
    treat ``None`` as "not relevant to backfill safety".

    2026-05-26 — Two behaviour changes to align with the frontend:
      1. Honour `params.idempotent_override = true` as an explicit
         author attestation that an upstream guard (e.g. a TRUNCATE
         via execute_sql_task) keeps the sink idempotent. Same hatch
         the frontend badge uses.
      2. Treat `mode in {create, truncate}` as REPLACE for table-like
         sinks. The warehouse_sink backend does CREATE OR REPLACE on
         `create` and DELETE+INSERT on `truncate` — both fully replace
         the target every run, so they're idempotent. The previous
         classifier missed these and blocked backfills against the OSS
         demo pipelines (most of which ship with mode=create).
    """
    p = params or {}
    if p.get("idempotent_override") is True:
        return "safe"

    mode_raw = p.get("mode") or p.get("write_mode") or ""
    mode = str(mode_raw).lower()

    if step_type in _TABLE_LIKE_SINKS:
        if mode in ("replace", "overwrite", "create", "truncate"):
            return "replace"
        if mode in ("merge", "upsert"):
            keys = p.get("merge_on") or p.get("upsert_keys") or p.get("keys")
            return "merge" if isinstance(keys, list) and len(keys) > 0 else "merge"
        if mode in ("append", ""):
            return "append_risky"
        return "append_risky"

    if step_type in _FILE_SINKS:
        if mode == "append":
            return "append_risky"
        return "replace"

    if step_type in _EXTERNAL_SINKS:
        return "external"

    if step_type in _GENERIC_SINKS:
        if mode in ("replace", "overwrite", "create", "truncate"):
            return "replace"
        if mode in ("merge", "upsert"):
            return "merge"
        return "append_risky"

    return None


def find_unsafe_sinks(steps: Iterable[Any]) -> list[dict[str, str]]:
    """Walk a workflow's steps and return every sink that's risky for backfill.

    A sink is risky when its idempotency class is ``append_risky`` or
    ``external``. Returns a list of ``{step_id, step_type, idempotency}``
    dicts; empty list means the pipeline is backfill-safe by default.

    ``steps`` may be a list of Step pydantic objects or plain dicts —
    the function reads ``id``, ``type``, and ``params`` flexibly.
    """
    unsafe: list[dict[str, str]] = []
    for s in steps or []:
        step_id = getattr(s, "id", None) or (s.get("id") if isinstance(s, dict) else None) or ""
        step_type = getattr(s, "type", None)
        if step_type is None and isinstance(s, dict):
            step_type = s.get("type")
        # Pydantic StepType enum → string value.
        type_str = step_type.value if hasattr(step_type, "value") else str(step_type or "")
        params = getattr(s, "params", None) or (s.get("params") if isinstance(s, dict) else {}) or {}
        cls = classify(type_str, params)
        if cls in ("append_risky", "external"):
            unsafe.append({
                "step_id": step_id,
                "step_type": type_str,
                "idempotency": cls,
            })
    return unsafe
