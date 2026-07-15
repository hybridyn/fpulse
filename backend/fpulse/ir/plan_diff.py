"""Plan-stage diff — what will change if this proposed workflow IR is saved.

Pure function. No I/O, no DB, no globals. Caller hands in the current
stored Workflow (or None for first save) and the proposed Workflow,
gets back a structured diff that the API layer then enriches with
validator output and execution baseline.

Memory rules (per the production-readiness plan):
- Iterate, don't collect. We walk steps once each side.
- Cap every list. Huge pipelines truncate with a `truncated` flag rather
  than blowing the response payload.
- No deep copies. We compare param dicts key-by-key, not by re-serialising
  whole IR blobs.
"""

from __future__ import annotations

from typing import Any

from .schema import Workflow


# Hard caps — protect the response payload and the approver's eyes.
# A pipeline with > 200 changed steps in one save isn't a refactor,
# it's a new pipeline; truncating is the right call.
_MAX_LIST = 200
_MAX_MODIFIED_FIELDS = 20

# Step.params keys that change every save without representing a real
# behavioural change. Excluded from the "modified" detection so a save
# that only re-saved the same node doesn't show up as a diff.
_NOISE_PARAM_KEYS = frozenset({
    "_node_position", "_canvas_x", "_canvas_y",
})


def _step_index(workflow: Workflow | None) -> dict[str, Any]:
    """Build {step_id: step} for one side of the diff. Empty when the
    workflow is None (first-ever save scenario)."""
    if workflow is None:
        return {}
    return {s.id: s for s in workflow.steps}


def _connection_key(c: Any) -> tuple[str, str, str, str]:
    """Stable identity for a StepConnection — used to detect added /
    removed edges. Includes ports because a node can have multiple
    input ports and re-wiring matters."""
    return (
        getattr(c, "from_step", "") or "",
        getattr(c, "to_step", "") or "",
        getattr(c, "from_port", "") or "output",
        getattr(c, "to_port", "") or "input",
    )


def _diff_params(old: dict, new: dict) -> list[str]:
    """Return the list of param keys that differ between two steps.
    Skips noise keys (canvas position, etc.). Capped at
    _MAX_MODIFIED_FIELDS so a step with 500 columns doesn't dump them all."""
    changed: list[str] = []
    seen: set[str] = set()
    for k, v in (new or {}).items():
        if k in _NOISE_PARAM_KEYS:
            continue
        seen.add(k)
        if (old or {}).get(k) != v:
            changed.append(k)
            if len(changed) >= _MAX_MODIFIED_FIELDS:
                return changed
    for k in (old or {}):
        if k in _NOISE_PARAM_KEYS or k in seen:
            continue
        changed.append(k)
        if len(changed) >= _MAX_MODIFIED_FIELDS:
            return changed
    return changed


def compute_plan_diff(
    current: Workflow | None,
    proposed: Workflow,
) -> dict:
    """Compute what changes if `proposed` is saved over `current`.

    First-save case: `current` is None → everything counts as added.
    """
    cur_steps = _step_index(current)
    new_steps = _step_index(proposed)

    added_steps: list[dict] = []
    removed_steps: list[dict] = []
    modified_steps: list[dict] = []
    steps_truncated = False

    # Added + modified — walk proposed once.
    for sid, ns in new_steps.items():
        if sid not in cur_steps:
            if len(added_steps) < _MAX_LIST:
                added_steps.append({
                    "step_id": sid,
                    "type": ns.type.value if hasattr(ns.type, "value") else str(ns.type),
                    "name": getattr(ns, "name", "") or sid,
                })
            else:
                steps_truncated = True
            continue
        os = cur_steps[sid]
        type_changed = (
            (getattr(os, "type", None) != getattr(ns, "type", None))
        )
        fields = _diff_params(getattr(os, "params", {}) or {}, getattr(ns, "params", {}) or {})
        if type_changed or fields:
            if len(modified_steps) < _MAX_LIST:
                mod_fields = list(fields)
                if type_changed:
                    mod_fields.insert(0, "__type__")
                modified_steps.append({
                    "step_id": sid,
                    "type": ns.type.value if hasattr(ns.type, "value") else str(ns.type),
                    "name": getattr(ns, "name", "") or sid,
                    "fields": mod_fields[:_MAX_MODIFIED_FIELDS],
                })
            else:
                steps_truncated = True

    # Removed — anything in current that's gone in proposed.
    for sid, os in cur_steps.items():
        if sid in new_steps:
            continue
        if len(removed_steps) < _MAX_LIST:
            removed_steps.append({
                "step_id": sid,
                "type": os.type.value if hasattr(os.type, "value") else str(os.type),
                "name": getattr(os, "name", "") or sid,
            })
        else:
            steps_truncated = True

    # Edges.
    cur_edges = {_connection_key(c) for c in (current.connections if current else [])}
    new_edges = {_connection_key(c) for c in proposed.connections}
    added_edges_raw = list(new_edges - cur_edges)
    removed_edges_raw = list(cur_edges - new_edges)
    edges_truncated = (
        len(added_edges_raw) > _MAX_LIST
        or len(removed_edges_raw) > _MAX_LIST
    )
    added_edges = [
        {"from_step": k[0], "to_step": k[1], "from_port": k[2], "to_port": k[3]}
        for k in added_edges_raw[:_MAX_LIST]
    ]
    removed_edges = [
        {"from_step": k[0], "to_step": k[1], "from_port": k[2], "to_port": k[3]}
        for k in removed_edges_raw[:_MAX_LIST]
    ]

    # Saved-connection refs (the `connection_id` param on connector nodes).
    # Approvers want "what credentials does this pipeline now reach?"
    cur_conn_ids = _collect_connection_refs(current)
    new_conn_ids = _collect_connection_refs(proposed)
    added_refs = sorted(new_conn_ids - cur_conn_ids)
    removed_refs = sorted(cur_conn_ids - new_conn_ids)

    return {
        "steps": {
            "added": added_steps,
            "removed": removed_steps,
            "modified": modified_steps,
            "truncated": steps_truncated,
        },
        "connections": {
            "added": added_edges,
            "removed": removed_edges,
            "truncated": edges_truncated,
        },
        "connection_refs": {
            "added": added_refs[:_MAX_LIST],
            "removed": removed_refs[:_MAX_LIST],
        },
        "summary": {
            "added_steps": len(added_steps),
            "removed_steps": len(removed_steps),
            "modified_steps": len(modified_steps),
            "added_connections": len(added_edges),
            "removed_connections": len(removed_edges),
            "added_connection_refs": len(added_refs),
            "removed_connection_refs": len(removed_refs),
        },
    }


def _collect_connection_refs(workflow: Workflow | None) -> set[str]:
    """Set of saved-connection IDs referenced anywhere in the workflow.
    Iterates once, no list intermediates."""
    if workflow is None:
        return set()
    out: set[str] = set()
    for s in workflow.steps:
        cid = (getattr(s, "params", None) or {}).get("connection_id")
        if cid:
            out.add(cid)
    return out
