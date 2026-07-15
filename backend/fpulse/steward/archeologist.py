"""Archeologist sub-agent — duplicate-source / duplicate-pipeline detection.

The first concrete Steward capability. Ships in F-Pulse OSS v1.1 as the
headline "this is different from every other orchestrator" feature.

# What it detects

Two flavours of duplicate, both lineage-based (NOT just table-name
matching — that's the false-positive trap reviewer R2 flagged):

1. **Duplicate source**: two or more pipelines read the SAME logical
   source (same connection_id + same object name) into DIFFERENT
   destinations. Cause for review — likely opportunity to consolidate
   via a shared managed table or single ingestion + downstream copy.

2. **Duplicate pipeline**: two or more pipelines have effectively
   identical shape (same source, same sink, same critical params).
   Often an accident — two engineers built the same flow.

# What it does NOT flag (the false-positive guards)

* Linear chains of `raw → staging → cleansed → modeled` reading from
  the same source — that's a single logical dataset traversing layers,
  not a duplicate. Detected by checking whether the downstream pipelines
  share an upstream parent.
* Workflows the user has explicitly marked as "intentional duplicate"
  (DR copies, data-vault patterns, audit replication). The dismissal
  is recorded by the Curator and persists across re-scans.

# How it runs

Pure code. No LLM. No external calls. Reads the workflows table from the
workspace store and produces a deterministic list of findings. Safe to
re-run on every workflow save — the operation is O(N log N) where N is
the number of workflows, and a typical OSS workspace has <100 workflows.
"""
from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

from .models import (
    FindingKind,
    FindingLevel,
    FindingSeverity,
    FindingStatus,
    StewardFinding,
    level_for_kind,
)


def _utc_now_iso() -> str:
    """ISO 8601 UTC timestamp — matches the format used elsewhere in the
    audit log and the executor metadata."""
    return datetime.now(timezone.utc).isoformat()


def _source_signature(
    node_params: dict[str, Any],
    *,
    workspace_id: str | None = None,
) -> str | None:
    """Build a stable identity for "what source does this node read?".

    Returns ``None`` for nodes that aren't sources (they don't
    contribute to duplicate-source detection). For sources, returns a
    short hash that's identical when two nodes pull the same logical
    object from the same connection in the same workspace - and
    distinct otherwise.

    Identity covers the fields that actually determine "is this the
    same source":
      * workspace_id      - which workspace owns it (2026-06-05, per
                            architectural review block 1B — defends
                            against Plus multi-workspace collisions
                            where two tenants might import the same
                            connection_id)
      * connection_id     - which Connection the read goes through
      * connector_type    - csv / postgres / snowflake / ...
      * table / file_path / query / object — the specific object name
      * url               - for API sources

    Excludes: pagination params, retry config, sample size, row counts,
    timestamps, execution run IDs - anything that doesn't change what
    dataset is being read. Per Review 1B the signature must remain
    independent of timing or volume noise.
    """
    if not isinstance(node_params, dict):
        return None
    # 2026-06-06 (V1-Gaps G1) — split identity into two groups:
    #
    #   * `object_identity_fields` — the "WHAT is being read" group.
    #     At least ONE of these MUST be present, otherwise the
    #     signature is meaningless. Without this guard a node
    #     declaring only `connector_type: csv` (no file_path, no
    #     table) produces a non-None hash that says "some CSV
    #     somewhere" — useless for duplicate detection, but happily
    #     matches every other content-less CSV node in the workspace
    #     and emits false-positive duplicate findings.
    #
    #   * `qualifier_fields` — connection / connector / schema. They
    #     refine the identity (same table in two connections =
    #     different identity) but ALONE they aren't enough.
    object_identity_fields = (
        "table",
        "table_name",
        "file_path",
        "query",
        "url",
        "endpoint",
        "object",
        "object_name",
    )
    qualifier_fields = (
        "connection_id",
        "connector_type",
        "schema",
        "schema_name",
    )
    # Must have at least one object-identity field with a real value
    has_object_identity = any(
        node_params.get(f) not in (None, "") for f in object_identity_fields
    )
    if not has_object_identity:
        return None

    parts: list[str] = []
    if workspace_id:
        # Prefix is purely structural — same workspace + same source
        # always hashes identically; different workspaces never collide.
        parts.append(f"workspace_id={workspace_id}")
    for f in (*qualifier_fields, *object_identity_fields):
        v = node_params.get(f)
        if v is None or v == "":
            continue
        parts.append(f"{f}={v}")
    if not parts or (workspace_id and len(parts) == 1):
        return None
    # Stable ordering so two semantically-equal sources hash identically
    parts.sort()
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _step_type_and_params(node: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
    """Normalise a step/node into ``(step_type, params, label)`` regardless of
    which storage shape it came from.

    Two formats coexist in F-Pulse and both are valid input here:

      * **F-Pulse step format** (authoritative on-disk, see
        ``WorkflowStore``): ``{id, type, label, params, position}`` —
        ``type`` is top-level (e.g. ``"source"``, ``"db_sink"``).
      * **React Flow node format** (canvas runtime + some legacy
        snapshots): ``{id, data: {stepType, label, params}}`` —
        ``stepType`` is nested under ``data``.

    Without this normalisation (2026-06-06 fix) the Archeologist
    silently scanned every real F-Pulse workflow as "no source nodes"
    because it only knew the React Flow shape. The on-disk store
    uses the step format, so every production scan returned zero
    findings — matching the user's "Last scanned: 0 findings" UI.
    """
    # F-Pulse step format first (top-level type / params)
    top_type = node.get("type") or node.get("stepType") or node.get("step_type") or ""
    if top_type:
        return (
            str(top_type),
            node.get("params") or {},
            node.get("label") or str(top_type),
        )
    # Fallback: React Flow node format
    data = node.get("data") or {}
    rf_type = data.get("stepType") or data.get("step_type") or ""
    return (
        str(rf_type),
        data.get("params") or {},
        data.get("label") or str(rf_type),
    )


def _extract_sources(
    workflow_id: str,
    workflow_name: str,
    nodes: Iterable[dict[str, Any]],
    *,
    workspace_id: str | None = None,
) -> list[dict[str, Any]]:
    """Pull out every source-typed node from a workflow with its source
    signature. Returns one dict per source node found.

    ``workspace_id`` (2026-06-05) is threaded into the signature so
    Plus multi-workspace deployments never see cross-workspace sig
    collisions even if two tenants import the same connection_id.
    """
    out: list[dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        step_type, params, label = _step_type_and_params(node)
        # Source-shaped step types: explicit `source`, or anything
        # ending in `_source` (csv_source, db_source, api_source, etc.)
        is_source = step_type == "source" or step_type.endswith("_source")
        if not is_source:
            continue
        sig = _source_signature(params, workspace_id=workspace_id)
        if sig is None:
            continue
        out.append({
            "workflow_id": workflow_id,
            "workflow_name": workflow_name,
            "node_id": node.get("id") or "",
            "node_label": label,
            "signature": sig,
            "step_type": step_type,
        })
    return out


def _extract_sinks(
    nodes: Iterable[dict[str, Any]],
    *,
    workspace_id: str | None = None,
) -> list[str]:
    """Pull out sink signatures — used for the duplicate-pipeline check
    (same source AND same sink = highly likely accident, vs same source
    different sink = intentional fan-out)."""
    out: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        step_type, params, _label = _step_type_and_params(node)
        is_sink = (
            step_type in ("output", "destination", "db_sink", "sink")
            or step_type.endswith("_sink")
        )
        if not is_sink:
            continue
        sig = _source_signature(params, workspace_id=workspace_id)
        if sig is not None:
            out.append(sig)
    return sorted(out)


def detect_duplicate_sources(
    workflows: list[dict[str, Any]],
    *,
    workspace_id: str = "default",
    suppressed_signatures: set[str] | None = None,
) -> list[StewardFinding]:
    """Scan a list of workflows and produce StewardFindings for each
    distinct duplicate-source / duplicate-pipeline pattern detected.

    Parameters
    ----------
    workflows
        Each entry is the workflow document as stored — must have ``id``,
        ``name``, and ``nodes`` (a list of React Flow node dicts with
        ``data.params``).
    workspace_id
        Tenant boundary. Findings are emitted with this workspace_id so
        the API filters correctly in multi-workspace deployments.
    suppressed_signatures
        Source signatures the user has explicitly marked as "intentional
        duplicate." The Curator persists these dismissals across re-scans
        so we don't keep nagging the user about a deliberate pattern
        (e.g. DR replication, data vault layering).

    Returns
    -------
    A list of ``StewardFinding`` records — empty if no duplicates found.
    Caller is responsible for persisting them (we don't write to disk
    here — keeps the function pure-functional, easy to test).
    """
    suppressed = suppressed_signatures or set()
    by_signature: dict[str, list[dict[str, Any]]] = defaultdict(list)
    workflow_shape: dict[str, dict[str, Any]] = {}

    for wf in workflows:
        wf_id = wf.get("id") or ""
        wf_name = wf.get("name") or wf_id or "untitled"
        nodes = wf.get("nodes") or []
        sources = _extract_sources(wf_id, wf_name, nodes, workspace_id=workspace_id)
        for s in sources:
            by_signature[s["signature"]].append(s)
        workflow_shape[wf_id] = {
            "name": wf_name,
            "source_signatures": sorted({s["signature"] for s in sources}),
            "sink_signatures": _extract_sinks(nodes, workspace_id=workspace_id),
        }

    findings: list[StewardFinding] = []
    now = _utc_now_iso()

    # ── Duplicate-source pass ───────────────────────────────────────
    # A source signature appearing in two or more DIFFERENT workflows
    # is a duplicate-source. Same source twice in one workflow is a
    # different question (handled by the validator, not the Steward).
    for sig, occurrences in by_signature.items():
        if sig in suppressed:
            continue
        unique_workflows = {occ["workflow_id"]: occ for occ in occurrences}
        if len(unique_workflows) < 2:
            continue
        wf_list = sorted(unique_workflows.values(), key=lambda o: o["workflow_name"])
        names_csv = ", ".join(o["workflow_name"] for o in wf_list[:3])
        if len(wf_list) > 3:
            names_csv += f", +{len(wf_list) - 3} more"
        sample = wf_list[0]
        # Deterministic ID — re-running the detector on the same input
        # produces the same finding ID. The persistence layer uses this
        # for upsert semantics (occurrence counter increments instead
        # of duplicating the finding).
        finding_id = f"dup-src-{sig}"
        findings.append(StewardFinding(
            id=finding_id,
            workspace_id=workspace_id,
            kind=FindingKind.DUPLICATE_SOURCE,
            level=level_for_kind(FindingKind.DUPLICATE_SOURCE),
            severity=FindingSeverity.P2,
            status=FindingStatus.OPEN,
            # Deterministic detector — same signature hash twice IS
            # a duplicate, no statistical uncertainty. evidence_count
            # = number of distinct workflows touching the signature.
            confidence="high",
            confidence_score=1.0,
            evidence_count=len(wf_list),
            baseline_window="instantaneous",
            title=f"{len(wf_list)} pipelines read the same source",
            body=(
                f"The same source object is read by **{len(wf_list)} pipelines** "
                f"({names_csv}). This usually means duplicate movement + duplicate "
                f"storage downstream. Consider consolidating to a shared managed "
                f"table that the downstream pipelines read from instead.\n\n"
                f"If this is intentional (e.g. DR replication, data-vault layering), "
                f"dismiss the finding — the Steward will remember and stop flagging "
                f"this signature."
            ),
            evidence={
                "source_signature": sig,
                "source_node_type": sample["step_type"],
                "source_node_label": sample["node_label"],
                "workflows": [
                    {"id": w["workflow_id"], "name": w["workflow_name"]}
                    for w in wf_list
                ],
            },
            proposed_actions=[
                {
                    "label": "Consolidate via Managed Table",
                    "action": "create_managed_table_from_source",
                    "params": {"source_signature": sig},
                },
                {
                    "label": "Dismiss (intentional duplicate)",
                    "action": "suppress_finding",
                    "params": {"finding_id": finding_id, "scope": "signature"},
                },
            ],
            first_seen=now,
            last_seen=now,
            occurrences=len(wf_list),
        ))

    # ── Duplicate-pipeline pass ─────────────────────────────────────
    # Two workflows are "duplicate pipelines" when they have the exact
    # same set of source signatures AND the exact same set of sink
    # signatures. Pure-overlap pairs that differ only in transform
    # detail are still distinct (that's the user's choice).
    by_shape: dict[tuple[tuple[str, ...], tuple[str, ...]], list[str]] = defaultdict(list)
    for wf_id, shape in workflow_shape.items():
        if not shape["source_signatures"] or not shape["sink_signatures"]:
            # Incomplete pipelines (no source or no sink) — not yet a
            # meaningful duplicate candidate. Skip.
            continue
        key = (tuple(shape["source_signatures"]), tuple(shape["sink_signatures"]))
        by_shape[key].append(wf_id)

    for (src_sigs, sink_sigs), wf_ids in by_shape.items():
        if len(wf_ids) < 2:
            continue
        shape_signature = hashlib.sha256(
            ("|".join(src_sigs) + "→" + "|".join(sink_sigs)).encode("utf-8"),
        ).hexdigest()[:16]
        if shape_signature in suppressed:
            continue
        wf_names = [workflow_shape[w]["name"] for w in wf_ids]
        finding_id = f"dup-pipe-{shape_signature}"
        findings.append(StewardFinding(
            id=finding_id,
            workspace_id=workspace_id,
            kind=FindingKind.DUPLICATE_PIPELINE,
            level=level_for_kind(FindingKind.DUPLICATE_PIPELINE),
            severity=FindingSeverity.P2,
            status=FindingStatus.OPEN,
            confidence="high",
            confidence_score=1.0,
            evidence_count=len(wf_ids),
            baseline_window="instantaneous",
            title=f"{len(wf_ids)} pipelines have identical source → sink shape",
            body=(
                f"**{', '.join(wf_names[:3])}**"
                f"{f' + {len(wf_names) - 3} more' if len(wf_names) > 3 else ''} "
                f"all read the same source(s) and write to the same destination(s). "
                f"This is often an accident — two engineers built equivalent flows. "
                f"Review the transforms; if they're equivalent, deprecate the duplicates. "
                f"If they differ meaningfully (different filters, different schedules), "
                f"dismiss this finding."
            ),
            evidence={
                "shape_signature": shape_signature,
                "source_signatures": list(src_sigs),
                "sink_signatures": list(sink_sigs),
                "workflows": [
                    {"id": w, "name": workflow_shape[w]["name"]}
                    for w in wf_ids
                ],
            },
            proposed_actions=[
                {
                    "label": "Compare transforms side-by-side",
                    "action": "open_diff_view",
                    "params": {"workflow_ids": wf_ids},
                },
                {
                    "label": "Dismiss (intentional)",
                    "action": "suppress_finding",
                    "params": {"finding_id": finding_id, "scope": "shape"},
                },
            ],
            first_seen=now,
            last_seen=now,
            occurrences=len(wf_ids),
        ))

    return findings


# Reserved finding-id namespace — exported so the persistence layer can
# distinguish Steward findings from other systems' alert IDs without
# heuristics. Stable across versions.
FINDING_ID_PREFIXES = {
    FindingKind.DUPLICATE_SOURCE: "dup-src-",
    FindingKind.DUPLICATE_PIPELINE: "dup-pipe-",
}
