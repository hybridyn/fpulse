"""Self-documenting pipelines — deterministic Markdown export.

Synthesizes a human-readable README/spec for a pipeline from its IR
alone: name, business purpose, owner, tags, the freeform README, the
declared input parameters, a per-node table, and the version change-log.

No LLM and no network — the pipeline documents itself from what is
already stored, so the output is stable, reviewable, and safe to run in
an air-gap. Feeds ``GET /api/workflows/{id}/docs``.

The one external lookup is best-effort: node display-name/description
come from the node registry when the type is registered, and fall back
to a humanized type string otherwise — so a doc still renders for an
unknown/custom node type.
"""

from __future__ import annotations

from typing import Any

# Source-like node types (no upstream). Imported for accurate role
# classification; falls back to a suffix heuristic if the import moves.
try:  # pragma: no cover - defensive import
    from fpulse.ir.node_metadata import NO_INPUT_NODES as _NO_INPUT
except Exception:  # pragma: no cover
    _NO_INPUT = frozenset()


def _humanize(t: str) -> str:
    return (t or "").replace("_", " ").strip().title()


def _cell(v: Any) -> str:
    """Escape a value for a Markdown table cell (pipes + newlines)."""
    s = "" if v is None else str(v)
    return s.replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def _node_role(step_type: str) -> str:
    """Coarse role for the node table: Source / Sink / Trigger / Transform."""
    tl = (step_type or "").lower()
    if tl.endswith("_sink"):
        return "Sink"
    if tl.endswith("_trigger"):
        return "Trigger"
    if tl in _NO_INPUT or tl.endswith("_source") or tl in {"source", "api_source"}:
        return "Source"
    return "Transform"


def _node_meta(step_type: str) -> tuple[str, str]:
    """(display_label, description) for a node type — best-effort.

    Reads the node class off the registry when the type is registered;
    otherwise returns a humanized type string and an empty description.
    """
    try:
        from fpulse.ir.schema import StepType
        from fpulse.nodes.registry import NodeRegistry

        cls = NodeRegistry.get(StepType(step_type))
        label = getattr(cls, "display_name", "") or _humanize(step_type)
        desc = getattr(cls, "description", "") or ""
        return label, desc
    except Exception:
        return _humanize(step_type), ""


def _get(obj: Any, name: str, default: Any = "") -> Any:
    """getattr that also tolerates a plain dict."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _fmt_when(v: Any) -> str:
    """Trim an ISO timestamp to minute precision for readability."""
    s = "" if v is None else str(v)
    if "T" in s:
        date, _, rest = s.partition("T")
        return f"{date} {rest[:5]}".strip()
    return s[:16]


def render_workflow_markdown(workflow: Any, versions: list[dict] | None = None) -> str:
    """Render a pipeline as a Markdown document.

    ``workflow`` is a ``Workflow`` model (a dict is also tolerated).
    ``versions`` is the list returned by ``WorkflowStore.get_versions``
    (dicts with ``version`` / ``created_at`` / ``created_by`` /
    ``change_summary``); pass ``None`` to omit the change-log.
    """
    name = _get(workflow, "name", "Untitled Pipeline") or "Untitled Pipeline"
    purpose = (_get(workflow, "business_purpose", "") or "").strip()
    description = (_get(workflow, "description", "") or "").strip()
    readme = (_get(workflow, "readme", "") or "").strip()
    tags = _get(workflow, "tags", []) or []
    owner_name = (_get(workflow, "owner_name", "") or "").strip()
    owner_id = (_get(workflow, "owner_id", "") or "").strip()
    status = _get(workflow, "status", "")
    status = getattr(status, "value", status) or "draft"
    steps = _get(workflow, "steps", []) or []
    parameters = _get(workflow, "parameters", []) or []
    updated_at = _get(workflow, "updated_at", "")

    owner = owner_name or owner_id or "—"
    if owner_name and owner_id and owner_id != owner_name:
        owner = f"{owner_name} ({owner_id})"

    version_label = "—"
    if versions:
        latest = max((int(_get(v, "version", 0) or 0) for v in versions), default=0)
        version_label = f"{latest} (latest)" if latest else "—"

    out: list[str] = []
    out.append(f"# {name}")
    out.append("")
    if purpose:
        out.append(f"> **Purpose:** {purpose}")
        out.append("")

    # Metadata table
    out.append("| | |")
    out.append("|---|---|")
    out.append(f"| **Status** | {_cell(status)} |")
    out.append(f"| **Version** | {_cell(version_label)} |")
    out.append(f"| **Owner** | {_cell(owner)} |")
    out.append(f"| **Tags** | {(', '.join(f'`{_cell(t)}`' for t in tags)) if tags else '—'} |")
    out.append(f"| **Nodes** | {len(steps)} |")
    out.append(f"| **Inputs** | {len(parameters)} |")
    out.append(f"| **Last updated** | {_cell(_fmt_when(updated_at))} |")
    out.append("")

    if description:
        out.append("## Overview")
        out.append("")
        out.append(description)
        out.append("")

    if readme:
        out.append("## Documentation")
        out.append("")
        out.append(readme)  # verbatim Markdown
        out.append("")

    # Parameters (declared inputs)
    if parameters:
        out.append("## Inputs")
        out.append("")
        out.append("| Name | Type | Default | Description |")
        out.append("|------|------|---------|-------------|")
        for p in parameters:
            out.append(
                f"| `{_cell(_get(p, 'name'))}` | {_cell(_get(p, 'type', 'string'))} "
                f"| {_cell(_get(p, 'default'))} | {_cell(_get(p, 'description'))} |"
            )
        out.append("")

    # Pipeline steps
    out.append("## Pipeline steps")
    out.append("")
    if steps:
        out.append("| # | Node | Type | Role | What it does |")
        out.append("|---|------|------|------|--------------|")
        for i, s in enumerate(steps, start=1):
            st = _get(s, "type", "")
            st = getattr(st, "value", st) or ""
            label = _get(s, "label", "") or ""
            type_label, type_desc = _node_meta(st)
            shown_label = label or type_label
            out.append(
                f"| {i} | {_cell(shown_label)} | `{_cell(st)}` "
                f"| {_node_role(st)} | {_cell(type_desc)} |"
            )
    else:
        out.append("_This pipeline has no steps yet._")
    out.append("")

    # Change log
    if versions:
        out.append("## Change log")
        out.append("")
        out.append("| Version | When | By | Summary |")
        out.append("|---------|------|----|---------|")
        for v in sorted(versions, key=lambda x: int(_get(x, "version", 0) or 0), reverse=True):
            out.append(
                f"| {_cell(_get(v, 'version'))} | {_cell(_fmt_when(_get(v, 'created_at')))} "
                f"| {_cell(_get(v, 'created_by', 'user'))} "
                f"| {_cell(_get(v, 'change_summary') or '—')} |"
            )
        out.append("")

    out.append("---")
    out.append("_Generated by F-Pulse from the pipeline definition._")
    out.append("")
    return "\n".join(out)
