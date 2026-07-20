"""
Pipeline parameter resolution.

Workflow.parameters declares typed inputs the user (or an API caller) can
override per run. Step params reference these with ``${param.<name>}``
placeholders. Before execution, this module:

  1. Validates required parameters are present
  2. Coerces values to declared types (string / int / float / bool / json)
  3. Walks every step's params dict and substitutes placeholders

System-supplied placeholders supported in defaults:
  ${utcnow:%Y-%m-%d}     → current UTC date in the given strftime format
  ${utcnow}              → ISO-8601 UTC timestamp
  ${run_id}              → uuid4 hex (regenerated per run)
"""

from __future__ import annotations

import json
import re
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from fpulse.ir.schema import Workflow, WorkflowParameter


_PLACEHOLDER_RE = re.compile(r"\$\{param\.([A-Za-z_][A-Za-z0-9_]*)\}")
_SYSTEM_RE = re.compile(r"\$\{(utcnow|run_id)(?::([^}]+))?\}")


class ParameterError(ValueError):
    """Raised when parameter validation fails."""


def _coerce(value: Any, type_: str) -> Any:
    """Coerce a raw parameter value to its declared type. Raises ValueError."""
    t = (type_ or "string").lower()
    if value is None:
        return None
    if t == "string":
        return str(value)
    if t == "int":
        return int(value) if not isinstance(value, bool) else int(value)
    if t == "float":
        return float(value)
    if t == "bool":
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in ("true", "1", "yes", "y", "on"):
            return True
        if s in ("false", "0", "no", "n", "off", ""):
            return False
        raise ValueError(f"Cannot coerce {value!r} to bool")
    if t == "json":
        if isinstance(value, (dict, list)):
            return value
        return json.loads(str(value))
    return str(value)


def _resolve_system_placeholders(value: str, run_id: str) -> str:
    """Replace ${utcnow[:fmt]} and ${run_id} in a string."""
    def _sub(m: re.Match[str]) -> str:
        kind = m.group(1)
        fmt = m.group(2)
        if kind == "utcnow":
            now = datetime.now(timezone.utc)
            return now.strftime(fmt) if fmt else now.isoformat()
        if kind == "run_id":
            return run_id
        return m.group(0)

    return _SYSTEM_RE.sub(_sub, value)


def _resolve_param_placeholders(value: str, resolved: dict[str, Any]) -> Any:
    """Replace ${param.<name>} in a string. If the entire string IS a single
    placeholder, return the raw typed value (preserves int/bool/dict). If
    embedded inside other text, stringify the value and substitute in place.
    """
    m = _PLACEHOLDER_RE.fullmatch(value)
    if m:
        name = m.group(1)
        if name not in resolved:
            raise ParameterError(f"Unknown parameter reference ${{param.{name}}}")
        return resolved[name]

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in resolved:
            raise ParameterError(f"Unknown parameter reference ${{param.{name}}}")
        v = resolved[name]
        if isinstance(v, (dict, list)):
            return json.dumps(v, default=str)
        return str(v) if v is not None else ""

    return _PLACEHOLDER_RE.sub(_sub, value)


def _walk(value: Any, resolved: dict[str, Any], run_id: str) -> Any:
    """Recursively substitute placeholders inside nested params."""
    if isinstance(value, str):
        return _resolve_param_placeholders(_resolve_system_placeholders(value, run_id), resolved)
    if isinstance(value, list):
        return [_walk(v, resolved, run_id) for v in value]
    if isinstance(value, dict):
        return {k: _walk(v, resolved, run_id) for k, v in value.items()}
    return value


def resolve_parameter_values(
    parameters: list[WorkflowParameter],
    overrides: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the final {name: value} dict from declared params + overrides.

    Args:
      parameters: declared WorkflowParameter list (from Workflow.parameters)
      overrides: caller-supplied values (from API body / scheduler / Run dialog)

    Returns:
      dict of resolved values, type-coerced.

    Raises:
      ParameterError on missing required, unknown override, or coercion failure.
    """
    overrides = dict(overrides or {})
    declared = {p.name: p for p in (parameters or [])}

    # Reject overrides that don't match a declared parameter so typos surface
    # immediately rather than silently being ignored.
    unknown = sorted(set(overrides) - set(declared))
    if unknown:
        raise ParameterError(
            f"Unknown parameter(s): {', '.join(unknown)}. "
            f"Declared: {', '.join(sorted(declared)) or '(none)'}"
        )

    resolved: dict[str, Any] = {}
    missing: list[str] = []
    for p in declared.values():
        if p.name in overrides:
            raw = overrides[p.name]
        elif p.default is not None:
            raw = p.default
        elif p.required:
            missing.append(p.name)
            continue
        else:
            raw = None

        try:
            resolved[p.name] = _coerce(raw, p.type)
        except (ValueError, TypeError) as e:
            raise ParameterError(
                f"Parameter {p.name!r} (type={p.type!r}): cannot coerce {raw!r} ({e})"
            )

    if missing:
        raise ParameterError(f"Required parameter(s) missing: {', '.join(missing)}")

    return resolved


def find_parameter_references(workflow: Workflow) -> dict[str, list[str]]:
    """Scan every step's params for ``${param.<name>}`` references.

    Returns a `{parameter_name: [step_id, ...]}` map listing which steps
    reference each parameter. Used by the workflow-edit API to warn the
    user before deleting a parameter that's still in use.

    Walks strings, lists, and dicts — same recursion shape as `_walk()`,
    so a reference inside a nested object (e.g. mapping rules, key lists,
    SQL bodies, file paths) is detected too.

    Empty dict if no references found. Never raises — corrupt step.params
    is silently treated as having no references rather than blocking the
    UI scan.
    """
    refs: dict[str, list[str]] = {}

    def _scan(value: Any, step_id: str) -> None:
        if isinstance(value, str):
            for m in _PLACEHOLDER_RE.finditer(value):
                name = m.group(1)
                refs.setdefault(name, [])
                if step_id not in refs[name]:
                    refs[name].append(step_id)
        elif isinstance(value, list):
            for v in value:
                _scan(v, step_id)
        elif isinstance(value, dict):
            for v in value.values():
                _scan(v, step_id)

    for step in (workflow.steps or []):
        try:
            _scan(step.params, step.id)
        except Exception:  # noqa: BLE001 — best-effort scan
            continue
    return refs


def resolve_workflow_parameters(
    workflow: Workflow,
    overrides: dict[str, Any] | None = None,
) -> Workflow:
    """Return a copy of `workflow` with all step params resolved.

    Source-of-truth: `workflow.parameters` (declared) + `overrides` (caller).
    Step params are walked recursively; placeholders ${param.<name>} and
    ${utcnow[:fmt]} / ${run_id} are substituted. Original is never mutated.
    """
    resolved = resolve_parameter_values(workflow.parameters or [], overrides)
    run_id = uuid.uuid4().hex

    out = deepcopy(workflow)
    for step in out.steps:
        try:
            step.params = _walk(step.params, resolved, run_id)
        except ParameterError as e:
            raise ParameterError(
                f"Step {step.id} ({step.label or step.type}): {e}"
            )

    # Stash the resolved values on metadata so the execution log can record
    # exactly what was passed (audit + replay).
    md = dict(out.metadata or {})
    md["_resolved_parameters"] = resolved
    md["_run_id"] = run_id
    out.metadata = md
    return out
