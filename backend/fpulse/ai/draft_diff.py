"""Compute a human-readable diff for an AI-drafted pipeline change.

Powers the Diff Preview UX on the Copilot confirmation card. The
goal is "I can see exactly what will change before I click Confirm" —
not perfect text-diff, but a structured list of step + connection
changes the user can scan in under 10 seconds.

Inputs:
  * before_ir — the existing pipeline IR (None for net-new pipelines)
  * after_ir  — the drafted IR returned by draft_pipeline_from_intent /
                modify_pipeline_step

Output:
  ``DraftDiff`` — counts + per-element add/remove/modify lists.

The serializer (``to_jsonable``) returns a plain dict that the
frontend's ``DiffPreview.tsx`` consumes directly.

Trust contract: this module is pure-functional, no I/O, no DB
reads. The caller is responsible for fetching the before/after IRs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class StepChange:
    """One step entry in the diff. ``kind`` is add / remove / modify."""
    kind: str       # "add" | "remove" | "modify"
    step_id: str
    step_type: str  # the IR step.type — keeps the diff scannable
    label: str      # display name
    # For "modify" only: the parameter keys whose value changed. We
    # surface keys not values so we never leak credentials / SQL bodies
    # into the diff display.
    changed_param_keys: list[str] = field(default_factory=list)


@dataclass
class ConnectionChange:
    """One connection entry in the diff. ``kind`` is add / remove."""
    kind: str       # "add" | "remove"
    from_step: str
    to_step: str


@dataclass
class DraftDiff:
    """Top-level diff record returned by ``compute_diff``."""
    is_new_pipeline: bool
    steps_added: int
    steps_removed: int
    steps_modified: int
    connections_added: int
    connections_removed: int
    step_changes: list[StepChange] = field(default_factory=list)
    connection_changes: list[ConnectionChange] = field(default_factory=list)

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "is_new_pipeline": self.is_new_pipeline,
            "steps_added": self.steps_added,
            "steps_removed": self.steps_removed,
            "steps_modified": self.steps_modified,
            "connections_added": self.connections_added,
            "connections_removed": self.connections_removed,
            "step_changes": [
                {
                    "kind": c.kind,
                    "step_id": c.step_id,
                    "step_type": c.step_type,
                    "label": c.label,
                    "changed_param_keys": c.changed_param_keys,
                }
                for c in self.step_changes
            ],
            "connection_changes": [
                {
                    "kind": c.kind,
                    "from_step": c.from_step,
                    "to_step": c.to_step,
                }
                for c in self.connection_changes
            ],
        }


def _step_label(step: dict[str, Any]) -> str:
    """Pick the most user-meaningful label for a step."""
    return (
        str(step.get("label") or "").strip()
        or str(step.get("name") or "").strip()
        or str(step.get("id") or "").strip()
        or "(unnamed)"
    )


def _index_steps(ir: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """``{step_id: step}`` lookup, empty dict if ir is None."""
    if not ir:
        return {}
    steps = ir.get("steps") or []
    return {str(s.get("id") or ""): s for s in steps if s.get("id")}


def _index_connections(ir: dict[str, Any] | None) -> set[tuple[str, str]]:
    """Edges as ``{(from, to), ...}``. Lets us do set algebra cheaply."""
    if not ir:
        return set()
    conns = ir.get("connections") or []
    out: set[tuple[str, str]] = set()
    for c in conns:
        f = str(c.get("from_step") or "")
        t = str(c.get("to_step") or "")
        if f and t:
            out.add((f, t))
    return out


def _changed_param_keys(
    before_params: dict[str, Any] | None,
    after_params: dict[str, Any] | None,
) -> list[str]:
    """Sorted list of keys whose values differ between before and after.

    Keys present only in one side are also counted. Comparison is by
    JSON-serializable equality.
    """
    b = before_params or {}
    a = after_params or {}
    all_keys = sorted(set(b.keys()) | set(a.keys()))
    return [k for k in all_keys if b.get(k) != a.get(k)]


def compute_diff(
    *,
    before_ir: dict[str, Any] | None,
    after_ir: dict[str, Any],
) -> DraftDiff:
    """Compare two IRs (or None → after) and return a structured diff.

    For a NEW pipeline (before_ir is None), every step is reported as
    an "add" and every connection as an "add". Callers usually render
    a simpler "Will create N steps" header in that case — the per-step
    detail is still there for users who want to verify.
    """
    is_new = before_ir is None or not (before_ir.get("steps") or [])

    before_steps = _index_steps(before_ir)
    after_steps = _index_steps(after_ir)
    before_conns = _index_connections(before_ir)
    after_conns = _index_connections(after_ir)

    added_ids = sorted(set(after_steps) - set(before_steps))
    removed_ids = sorted(set(before_steps) - set(after_steps))
    common_ids = sorted(set(after_steps) & set(before_steps))

    step_changes: list[StepChange] = []
    for sid in added_ids:
        s = after_steps[sid]
        step_changes.append(StepChange(
            kind="add",
            step_id=sid,
            step_type=str(s.get("type") or ""),
            label=_step_label(s),
        ))
    for sid in removed_ids:
        s = before_steps[sid]
        step_changes.append(StepChange(
            kind="remove",
            step_id=sid,
            step_type=str(s.get("type") or ""),
            label=_step_label(s),
        ))
    modified_count = 0
    for sid in common_ids:
        b = before_steps[sid]
        a = after_steps[sid]
        # Type change is a modify (rare; usually a different op chain).
        # Param change is the common modify case. Label / position
        # changes are also surfaced so renames + canvas moves show up.
        type_changed = b.get("type") != a.get("type")
        param_keys = _changed_param_keys(b.get("params"), a.get("params"))
        label_changed = _step_label(b) != _step_label(a)
        if not (type_changed or param_keys or label_changed):
            continue
        modified_count += 1
        keys = list(param_keys)
        if type_changed:
            keys.append("__type")
        if label_changed:
            keys.append("__label")
        step_changes.append(StepChange(
            kind="modify",
            step_id=sid,
            step_type=str(a.get("type") or ""),
            label=_step_label(a),
            changed_param_keys=keys,
        ))

    conn_changes: list[ConnectionChange] = []
    for f, t in sorted(after_conns - before_conns):
        conn_changes.append(ConnectionChange(kind="add", from_step=f, to_step=t))
    for f, t in sorted(before_conns - after_conns):
        conn_changes.append(ConnectionChange(kind="remove", from_step=f, to_step=t))

    return DraftDiff(
        is_new_pipeline=is_new,
        steps_added=len(added_ids),
        steps_removed=len(removed_ids),
        steps_modified=modified_count,
        connections_added=len(after_conns - before_conns),
        connections_removed=len(before_conns - after_conns),
        step_changes=step_changes,
        connection_changes=conn_changes,
    )
