"""
Tool output schema normalization.

Every tool in the agent registry declares a JSON-shaped output schema.
``normalize_tool_output`` validates a tool's raw return against that schema
and reshapes it to a stable form before the agent loop sees it. Prevents
"agentic failures from malformed tool responses" per round-3 reviewer.

Tool registry (the schema source) lands in Step 1.5a as ToolRegistry. For
Step 1, this module exposes:
  - register_output_schema(tool_name, schema)  — for tests / future registry
  - normalize_tool_output(tool_name, result)   — the call site API
  - SchemaError                                 — typed failure
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class SchemaError(ValueError):
    """Raised when a tool result violates its declared output schema.

    Callers should catch this and emit a `tool_failure` outcome in the trace.
    """


# A schema is a dict describing expected types. For Step 1 we keep this
# intentionally small — full JSON Schema is overkill until the registry
# matures. Supported leaf types: "str", "int", "float", "bool", "list", "dict".
Schema = dict[str, Any]


@dataclass
class _RegisteredTool:
    output_schema: Schema


_REGISTRY: dict[str, _RegisteredTool] = {}


def register_output_schema(tool_name: str, schema: Schema) -> None:
    """Register a tool's output schema. Idempotent — re-registration replaces.

    The full ToolRegistry in Step 1.5a will subsume this module-level dict.
    """
    _REGISTRY[tool_name] = _RegisteredTool(output_schema=schema)


def _matches(value: Any, expected: Any) -> bool:
    if expected == "str":
        return isinstance(value, str)
    if expected == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "bool":
        return isinstance(value, bool)
    if expected == "list":
        return isinstance(value, list)
    if expected == "dict":
        return isinstance(value, dict)
    if expected == "any":
        return True
    if isinstance(expected, dict):
        # Nested schema
        return isinstance(value, dict)
    return False


def normalize_tool_output(tool_name: str, result: Any) -> Any:
    """Validate `result` against `tool_name`'s declared schema.

    Behaviour:
      - If tool_name not registered: returns result unchanged (no enforcement
        until ToolRegistry lands; logged-warning path comes in Step 1.5a).
      - If schema is `dict`-shaped: every required key must be present with
        the matching leaf type. Extra keys are dropped silently.
      - If schema is a leaf type: value itself is checked.

    Raises:
      SchemaError if validation fails.
    """
    reg = _REGISTRY.get(tool_name)
    if reg is None:
        return result

    schema = reg.output_schema

    # Leaf-typed schema: validate the whole result
    if isinstance(schema, str):
        if not _matches(result, schema):
            raise SchemaError(
                f"Tool {tool_name!r} expected {schema}, got {type(result).__name__}"
            )
        return result

    # Dict-shaped schema: enforce required keys, drop extras
    if isinstance(schema, dict):
        if not isinstance(result, dict):
            raise SchemaError(
                f"Tool {tool_name!r} expected dict, got {type(result).__name__}"
            )
        normalized: dict[str, Any] = {}
        for key, expected in schema.items():
            if key not in result:
                raise SchemaError(f"Tool {tool_name!r} missing required key {key!r}")
            value = result[key]
            if not _matches(value, expected):
                raise SchemaError(
                    f"Tool {tool_name!r} key {key!r} expected {expected}, got {type(value).__name__}"
                )
            # Recurse into nested-dict schemas
            if isinstance(expected, dict):
                value = _normalize_nested(value, expected, key, tool_name)
            normalized[key] = value
        return normalized

    raise SchemaError(f"Tool {tool_name!r} has malformed schema: {schema!r}")


def _normalize_nested(value: dict, schema: dict, parent_key: str, tool_name: str) -> dict:
    out: dict[str, Any] = {}
    for key, expected in schema.items():
        if key not in value:
            raise SchemaError(
                f"Tool {tool_name!r} {parent_key}.{key} missing"
            )
        v = value[key]
        if not _matches(v, expected):
            raise SchemaError(
                f"Tool {tool_name!r} {parent_key}.{key} expected {expected}, got {type(v).__name__}"
            )
        out[key] = v
    return out


def clear_registry() -> None:
    """Test helper. Resets the module-level registry between tests."""
    _REGISTRY.clear()
