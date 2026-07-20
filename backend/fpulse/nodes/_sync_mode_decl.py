"""Shared sync_mode + cursor param_schema entries.

X3 (2026-05-30) — every source node that supports incremental sync
should declare the same three fields so the canvas UI renders one
consistent contract. This module returns the JSON the param_schema()
methods append.

Two helpers:

  sync_mode_entries()
      The standard 2-field block for sources that already implement
      cursor substitution end-to-end (db_source full path,
      api_source via {cursor}). Renders sync_mode + cursor_column
      under an "Incremental" tab.

  sync_mode_marker_entries()
      The declarative-only variant for sources where the engine
      can't yet auto-filter by cursor (cloud files, gsheet, etc.).
      Renders sync_mode + a placeholder note explaining the
      per-vendor mechanism the operator should embed in their config
      (often {cursor} in the path or modified_after query param).
      This makes the contract VISIBLE in the UI while we incrementally
      wire the per-vendor filtering.

Both helpers always return a list[dict] so call sites can splice them
in with ``[...existing, *sync_mode_entries()]``.
"""
from __future__ import annotations


def sync_mode_entries() -> list[dict]:
    """Full-contract sync_mode + cursor_column declarations.

    Use this in sources whose execute() already substitutes
    ``{cursor}`` into the request OR runs a SQL WHERE clause off
    the stored cursor (db_source, api_source).
    """
    return [
        {
            "name": "sync_mode", "type": "select", "label": "Sync Mode",
            "options": ["full_refresh", "incremental"],
            "default": "full_refresh", "tab": "Incremental",
            "description": (
                "full_refresh = fetch everything each run. "
                "incremental = the engine substitutes the persisted "
                "cursor value into your request and auto-saves the "
                "new max after a successful run."
            ),
        },
        {
            "name": "cursor_response_field", "type": "text",
            "label": "Cursor Field (in response)",
            "tab": "Incremental",
            "placeholder": "updated_at",
            "show_when": {"sync_mode": ["incremental"]},
            "description": (
                "Field name in each response row whose MAX value "
                "becomes the next run's cursor. Common values: "
                "updated_at, modified_time, id."
            ),
        },
        # B1 (2026-06-08, docs/design/backfill-ux-1.2.md) — lookback
        # window for late-arriving data. Default 0 = strict cursor
        # (current behaviour). Setting a non-zero value re-reads the
        # last N seconds on every incremental run so rows that arrive
        # at the source AFTER the cursor moved past don't get missed.
        # The dedupe store (sinks/dedupe_store.py) handles the
        # overlap so downstream sees each row once.
        {
            "name": "lookback_seconds", "type": "number",
            "label": "Re-read last N seconds (catches late-arriving data)",
            "tab": "Incremental",
            "default": 0, "min": 0, "max": 86400 * 7,
            "show_when": {"sync_mode": ["incremental"]},
            "description": (
                "Default 0 = strict cursor (no re-read). Set to a "
                "positive value to re-read the last N seconds on every "
                "incremental run — catches rows that landed at the "
                "source AFTER the watermark moved past (typical with "
                "clock skew on the source). Recommended: 86400 (24h) "
                "for sources with known skew; the dedupe store handles "
                "the overlap so downstream sees each row once."
            ),
        },
        # B4 (2026-06-08, docs/design/backfill-ux-1.2.md) — tombstone
        # column for soft-delete propagation. When the source has an
        # is_deleted-style flag, naming it here lets the sink delete
        # or mark-deleted on the destination instead of letting stale
        # rows accumulate. True CDC (hard-delete tracking via
        # pgoutput) is the right answer for sources without a
        # tombstone column; that ships in Plus.
        {
            "name": "tombstone_column", "type": "text",
            "label": "Tombstone column (soft-delete propagation)",
            "tab": "Incremental",
            "placeholder": "is_deleted",
            "show_when": {"sync_mode": ["incremental"]},
            "description": (
                "Optional. Column name on the source that flags soft-"
                "deletes (e.g. is_deleted / deleted_at). When the "
                "incremental read sees this column set on a row, the "
                "sink propagates the delete to the destination instead "
                "of leaving the stale row behind. Leave empty for sources "
                "without a tombstone column; for true hard-delete "
                "propagation use the CDC connector (Plus)."
            ),
        },
    ]


def sync_mode_marker_entries(per_vendor_hint: str) -> list[dict]:
    """Declarative-only sync_mode + an operator-facing hint.

    Use this in sources where the engine doesn't yet auto-filter by
    cursor and the operator must embed the cursor manually in their
    request — e.g. a SharePoint Drive listing with
    ``$filter=lastModifiedDateTime gt {cursor}``.

    `per_vendor_hint` is a short string (1-2 sentences) telling the
    operator HOW to wire the cursor for this specific source.
    Surfaces under the field's description in the UI.
    """
    return [
        {
            "name": "sync_mode", "type": "select", "label": "Sync Mode",
            "options": ["full_refresh", "incremental"],
            "default": "full_refresh", "tab": "Incremental",
            "description": (
                "Declarative only on this source today — see hint below. "
                "Full vendor-side auto-filtering will land per source "
                "in subsequent releases."
            ),
        },
        {
            "name": "cursor_hint_readonly", "type": "info",
            "label": "How to wire incremental on this source",
            "tab": "Incremental",
            "show_when": {"sync_mode": ["incremental"]},
            "description": per_vendor_hint,
        },
    ]
