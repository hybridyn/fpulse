"""Pipeline preflight checks — V14 of the F-Pulse product vision.

Catches structural problems before Save and Run. Sits alongside
`validate_workflow()` in `ir/validator.py`:

- `validate_workflow()` — required params, broken refs, cycles
- `preflight_workflow()` — graph structure (orphans, disconnected sinks)

The new `POST /api/workflows/{id}/preflight` endpoint runs both and
merges the findings. The older `/validate` endpoint runs only
`validate_workflow()` for backward compatibility.

Finding codes are stable strings so the UI can branch on type
(e.g., "show a confirm dialog if there's an orphaned-node warning").
Add new codes here; never rename or remove an existing one in a
non-breaking release.
"""

from __future__ import annotations

from collections import defaultdict

from .schema import StepType, Workflow
from .validator import _READ_REQUIRED_TYPES, _WRITE_REQUIRED_TYPES


class PreflightCode:
    """Stable machine-readable codes for preflight findings."""

    EMPTY_PIPELINE = "empty_pipeline"
    ORPHANED_NODE = "orphaned_node"
    TRANSFORM_WITHOUT_INPUT = "transform_without_input"
    SINK_WITHOUT_SOURCE = "sink_without_source"
    UNCONNECTED_SOURCE = "unconnected_source"


def _build_adjacency(
    workflow: Workflow,
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Return (forward, reverse) adjacency lists keyed by step id.

    Forward[a] = list of nodes a flows into.
    Reverse[a] = list of nodes that flow into a.
    """
    forward: dict[str, list[str]] = defaultdict(list)
    reverse: dict[str, list[str]] = defaultdict(list)
    for conn in workflow.connections:
        forward[conn.from_step].append(conn.to_step)
        reverse[conn.to_step].append(conn.from_step)
    return forward, reverse


def _is_source_shaped(step_type: StepType) -> bool:
    return step_type in _READ_REQUIRED_TYPES or step_type == StepType.SOURCE


def _is_sink_shaped(step_type: StepType) -> bool:
    return step_type in _WRITE_REQUIRED_TYPES or step_type == StepType.DESTINATION


def preflight_workflow(workflow: Workflow) -> list[dict]:
    """Run structural preflight checks on a workflow IR.

    Returns a list of finding dicts:
        {step_id: str, code: str, severity: "error" | "warning", message: str}

    Does NOT call `validate_workflow()` — callers wanting the full
    picture should run both and concatenate (the preflight endpoint does).

    Cost: O(steps + connections) for the adjacency build, plus O(steps²)
    worst case for the sink-ancestor BFS. Pipelines with thousands of
    steps would feel this; current OSS limits keep us well under.
    """
    findings: list[dict] = []

    # An empty workflow short-circuits the rest — there's nothing else
    # to check, and we don't want to report N "orphaned" findings on
    # a fresh canvas.
    if not workflow.steps:
        findings.append(
            {
                "step_id": "",
                "code": PreflightCode.EMPTY_PIPELINE,
                "severity": "error",
                "message": "Pipeline has no steps. Add at least a source to start.",
            }
        )
        return findings

    forward, reverse = _build_adjacency(workflow)
    step_by_id = {s.id: s for s in workflow.steps}

    for step in workflow.steps:
        is_source = _is_source_shaped(step.type)
        is_sink = _is_sink_shaped(step.type)

        has_in = len(reverse.get(step.id, [])) > 0
        has_out = len(forward.get(step.id, [])) > 0

        # 1. Orphaned: no incoming AND no outgoing connections.
        if not has_in and not has_out:
            # A source on its own isn't truly orphaned — it just hasn't
            # been wired to a sink yet. Surface as a warning so the
            # editor can flag it without blocking a Save.
            if is_source:
                findings.append(
                    {
                        "step_id": step.id,
                        "code": PreflightCode.UNCONNECTED_SOURCE,
                        "severity": "warning",
                        "message": (
                            "Source has no downstream — the pipeline will "
                            "load data but write nothing."
                        ),
                    }
                )
            else:
                findings.append(
                    {
                        "step_id": step.id,
                        "code": PreflightCode.ORPHANED_NODE,
                        "severity": "error",
                        "message": (
                            "Node is not connected — connect it upstream "
                            "and downstream, or remove it."
                        ),
                    }
                )
            # An orphaned node can't fail the other checks; skip them.
            continue

        # 2. Transform-without-input: any non-source step needs an
        # upstream connection. The validator catches some of this via
        # the connection-target check but not the "node exists with
        # zero inputs" case.
        if not has_in and not is_source:
            findings.append(
                {
                    "step_id": step.id,
                    "code": PreflightCode.TRANSFORM_WITHOUT_INPUT,
                    "severity": "error",
                    "message": (
                        f"{step.type.value} has no upstream connection — "
                        "connect a source or upstream node."
                    ),
                }
            )

        # 3. Sink-without-source: every sink needs a source somewhere
        # upstream. BFS backwards until we either find one or exhaust
        # the ancestry.
        if is_sink:
            visited: set[str] = set()
            stack = list(reverse.get(step.id, []))
            ancestor_has_source = False
            while stack:
                cur_id = stack.pop()
                if cur_id in visited:
                    continue
                visited.add(cur_id)
                cur_step = step_by_id.get(cur_id)
                if cur_step is None:
                    # Dangling reference — validator catches this
                    # separately; preflight can ignore.
                    continue
                if _is_source_shaped(cur_step.type):
                    ancestor_has_source = True
                    break
                stack.extend(reverse.get(cur_id, []))
            if not ancestor_has_source:
                findings.append(
                    {
                        "step_id": step.id,
                        "code": PreflightCode.SINK_WITHOUT_SOURCE,
                        "severity": "error",
                        "message": (
                            "Sink has no source ancestor — every sink "
                            "needs a source somewhere upstream."
                        ),
                    }
                )

    return findings


__all__ = ["PreflightCode", "preflight_workflow"]
