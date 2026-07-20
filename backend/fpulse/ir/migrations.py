"""Workflow IR migrations and deprecation policy.

Two related concerns live here:

1. **Deprecated StepTypes** — enum values that exist for backward
   compatibility but should not be used in new workflows. A deprecated
   type usually has either no backend implementation or a planned
   replacement that the operator should prefer.

2. **Legacy-node migration** — workflows saved before the
   2026-05-22 generic-source/sink consolidation can carry concrete
   step types like ``csv_source`` / ``db_sink`` / ``webhook_trigger``.
   ``migrate_legacy_node_types(workflow)`` rewrites those into the
   modern shape (generic ``source`` / ``destination`` with
   ``connector_type``) and emits a deprecation log entry per remap so
   the operator can see what was rewritten and when.

Both are idempotent — calling them on an already-migrated workflow is
a no-op. Wired into the workflow-load path in api/workflows.py so old
saves, templates, and AI drafts all get the same treatment.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fpulse.ir.schema import Workflow


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Deprecation:
    """One entry in the deprecation registry.

    Attributes:
        reason: short operator-readable explanation of why this type
            shouldn't be used.
        replaced_by: the canonical step type a workflow should use
            instead. When set, ``migrate_legacy_node_types`` will
            rewrite the type and (optionally) inject params.
        injected_params: params to merge into the migrated step.
            Used by source/sink remaps to set ``connector_type``.
    """
    reason: str
    replaced_by: str | None = None
    injected_params: dict | None = None


# ── Deprecation registry ──────────────────────────────────────────────────
#
# Keep this list aligned with the StepType enum. Any enum value that
# has no @register(...) implementation AND isn't on this list will fail
# the node conformance test in tests/test_node_conformance.py.
#
# When a new deprecation lands here, also: (a) remove it from any
# frontend palette entry, (b) add a release-notes line to changelog.md.
DEPRECATED_STEP_TYPES: dict[str, _Deprecation] = {
    "webhook_trigger": _Deprecation(
        reason=(
            "Inbound webhook receiver infrastructure (URL routing, "
            "signature verification, replay protection) isn't in v1.0. "
            "Pull-style integration via api_source is the canonical path."
        ),
        replaced_by="api_source",
    ),
    "webhook_sink": _Deprecation(
        reason="Use the generic Destination node with connector_type='webhook'.",
        replaced_by="destination",
        injected_params={"connector_type": "webhook"},
    ),
    "output": _Deprecation(
        reason=(
            "Legacy generic-output node. Use the generic Destination node "
            "with a concrete connector_type (csv / parquet / etc)."
        ),
        replaced_by="destination",
        injected_params={"connector_type": "csv"},
    ),
}


# ── Legacy-node migration ─────────────────────────────────────────────────
#
# Mapping of specific-source / specific-sink step types to the generic
# Source/Destination shape they collapse into. The 2026-05-22 audit
# called out the need for this — old templates, AI drafts, and saved
# workflows still carry the specific types and there was no migration.
#
# Format: legacy_type → (target_type, connector_type_value).
# The connector_type value matches the dispatch keys in
# backend/fpulse/nodes/generic.py:SOURCE_MAP / DEST_MAP.
_SOURCE_REMAP: dict[str, tuple[str, str]] = {
    "csv_source":         ("source", "csv"),
    "json_source":        ("source", "json"),
    "parquet_source":     ("source", "parquet"),
    "excel_source":       ("source", "excel"),
    "xml_source":         ("source", "xml"),
    "db_source":          ("source", "database"),
    "api_source":         ("source", "rest_api"),
    "s3_source":          ("source", "s3"),
    "azure_blob_source":  ("source", "azure_blob"),
    "gcs_source":         ("source", "gcs"),
    "sharepoint_source":  ("source", "sharepoint"),
    "onedrive_source":    ("source", "onedrive"),
    "kafka_source":       ("source", "kafka"),
    "ftp_source":         ("source", "ftp"),
    "gsheet_source":      ("source", "gsheet"),
    "delta_source":       ("source", "delta"),
    # 2026-05-22 — generic Microsoft Graph source. Old workflows
    # that referenced microsoft_graph_source directly get remapped
    # to the canonical `source` + connector_type=microsoft_graph
    # shape on load.
    "microsoft_graph_source": ("source", "microsoft_graph"),
}

_SINK_REMAP: dict[str, tuple[str, str]] = {
    "csv_sink":         ("destination", "csv"),
    "json_sink":        ("destination", "json"),
    "excel_sink":       ("destination", "excel"),
    "file_sink":        ("destination", "parquet"),  # default to parquet for file_sink
    "db_sink":          ("destination", "database"),
    "s3_sink":          ("destination", "s3"),
    "azure_blob_sink":  ("destination", "azure_blob"),
    "gcs_sink":         ("destination", "gcs"),
    "sharepoint_sink":  ("destination", "sharepoint"),
    "onedrive_sink":    ("destination", "onedrive"),
    "kafka_sink":       ("destination", "kafka"),
    "api_sink":         ("destination", "rest_api"),
    "email_sink":       ("destination", "email"),
    "delta_sink":       ("destination", "delta"),
    "warehouse_sink":   ("destination", "warehouse"),
}


def migrate_legacy_node_types(workflow_data: dict) -> dict:
    """Rewrite legacy step types into the modern generic shape.

    Accepts a workflow dict (the wire-format shape, with ``steps`` list
    each having ``type`` and ``params``). Returns the same dict shape
    with legacy types remapped. Idempotent — passing an already-modern
    workflow is a no-op.

    Migrations applied (in order):

      1. **Deprecation remaps.** Anything in ``DEPRECATED_STEP_TYPES``
         with a ``replaced_by`` gets rewritten + any ``injected_params``
         merged into the step's params.
      2. **Source/sink consolidation.** Legacy ``csv_source`` →
         ``source`` + ``connector_type=csv``; legacy ``db_sink`` →
         ``destination`` + ``connector_type=database``; etc.

    Logs a WARNING per remap with the old/new types and step id so
    operators can see what was rewritten when they open an old workflow.
    """
    if not isinstance(workflow_data, dict) or "steps" not in workflow_data:
        return workflow_data

    steps = workflow_data.get("steps") or []
    remap_count = 0
    for step in steps:
        if not isinstance(step, dict):
            continue
        original_type = step.get("type")
        if not original_type:
            continue

        # 1. Deprecation remap
        if original_type in DEPRECATED_STEP_TYPES:
            dep = DEPRECATED_STEP_TYPES[original_type]
            if dep.replaced_by:
                step["type"] = dep.replaced_by
                if dep.injected_params:
                    params = step.get("params") or {}
                    if not isinstance(params, dict):
                        params = {}
                    # Existing user params win over injected defaults so
                    # a partially-configured node survives migration intact.
                    for k, v in dep.injected_params.items():
                        params.setdefault(k, v)
                    step["params"] = params
                logger.warning(
                    "migration: step %r remapped from deprecated type %r → %r (%s)",
                    step.get("id"), original_type, dep.replaced_by, dep.reason,
                )
                remap_count += 1
                continue
            else:
                # Deprecated with no replacement — still log so the operator
                # can see why their workflow may fail at runtime.
                logger.warning(
                    "migration: step %r uses deprecated type %r with no "
                    "replacement (%s). Pipeline may fail to execute.",
                    step.get("id"), original_type, dep.reason,
                )

        # 2. Source consolidation
        if original_type in _SOURCE_REMAP:
            target_type, connector_type = _SOURCE_REMAP[original_type]
            step["type"] = target_type
            params = step.get("params") or {}
            if not isinstance(params, dict):
                params = {}
            params.setdefault("connector_type", connector_type)
            step["params"] = params
            logger.warning(
                "migration: step %r remapped from legacy type %r → %r "
                "(connector_type=%r)",
                step.get("id"), original_type, target_type, connector_type,
            )
            remap_count += 1
            continue

        # 3. Sink consolidation
        if original_type in _SINK_REMAP:
            target_type, connector_type = _SINK_REMAP[original_type]
            step["type"] = target_type
            params = step.get("params") or {}
            if not isinstance(params, dict):
                params = {}
            params.setdefault("connector_type", connector_type)
            step["params"] = params
            logger.warning(
                "migration: step %r remapped from legacy type %r → %r "
                "(connector_type=%r)",
                step.get("id"), original_type, target_type, connector_type,
            )
            remap_count += 1
            continue

    # 4. Branch-port back-compat (2026-06-15). `if_condition` became
    #    a true/false brancher (emits `_split_output`). Legacy edges from an
    #    if_condition carry the schema-default `from_port='output'`, which the
    #    router leaves UNROUTED — so both branches + the tag column would leak
    #    downstream. Remap those legacy edges onto the 'true' branch so old
    #    "keep matching rows" pipelines behave exactly as before.
    if_ids = {
        s.get("id")
        for s in steps
        if isinstance(s, dict) and s.get("type") == "if_condition" and s.get("id")
    }
    if if_ids:
        for conn in (workflow_data.get("connections") or []):
            if not isinstance(conn, dict):
                continue
            if conn.get("from_step") in if_ids and (conn.get("from_port") or "output") == "output":
                conn["from_port"] = "true"
                remap_count += 1
                logger.warning(
                    "migration: if_condition edge %r→%r from_port 'output'→'true' "
                    "(If is now a true/false brancher)",
                    conn.get("from_step"), conn.get("to_step"),
                )

    if remap_count > 0:
        logger.info(
            "migration: workflow %r had %d step(s) remapped from legacy types.",
            workflow_data.get("id") or workflow_data.get("name") or "<unnamed>",
            remap_count,
        )

    return workflow_data


def is_deprecated_step_type(step_type: str) -> bool:
    """Quick check for the conformance test + UI deprecation banners."""
    return step_type in DEPRECATED_STEP_TYPES


__all__ = [
    "DEPRECATED_STEP_TYPES",
    "is_deprecated_step_type",
    "migrate_legacy_node_types",
]
