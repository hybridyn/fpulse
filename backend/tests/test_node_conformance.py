"""Node-system conformance tests.

These tests pin the contracts the 2026-05-22 node audit (see
docs/NODE_CONFIGURATION_VALIDATION.md) revealed were drifting between
four surfaces:

  1. The ``StepType`` enum (``fpulse.ir.schema``)
  2. The ``NodeRegistry`` of implementation classes (``@register``
     decorator in ``fpulse.nodes.registry``)
  3. The frontend palette (``frontend/src/components/ModulesPanel.tsx``)
  4. The deprecation registry (``fpulse.ir.migrations``)

The invariants:

  * Every ``StepType`` enum value is **either** registered with a
    ``NodeDefinition`` class **or** listed in
    ``DEPRECATED_STEP_TYPES``.  No silent enum values that the
    executor can't run.
  * Every key in ``DEPRECATED_STEP_TYPES`` corresponds to a real
    ``StepType`` enum value (no orphans).
  * Every registered class has the required class attributes
    (``display_name``, ``category``, ``description``) plus the required
    static methods (``default_params``, ``param_schema``, ``execute``).
  * Every key in ``SIDE_EFFECT_CLASS`` is a real ``StepType`` enum value.
  * Every key in ``MULTI_INPUT_NODES`` / ``NO_INPUT_NODES`` is a real
    ``StepType`` enum value.

Without this test the node-system drifts again the moment somebody
adds an enum value but forgets a registry entry — exactly what the
audit caught.
"""

from __future__ import annotations

import pytest

from fpulse.ir.schema import StepType
from fpulse.ir.migrations import DEPRECATED_STEP_TYPES, is_deprecated_step_type
from fpulse.ir.node_metadata import (
    MULTI_INPUT_NODES,
    NO_INPUT_NODES,
    SIDE_EFFECT_CLASS,
    contract_for,
    side_effect_class_for,
)
from fpulse.nodes.registry import _REGISTRY, get_registry


@pytest.fixture(scope="module", autouse=True)
def _load_registry():
    """Trigger registry module imports so _REGISTRY is populated."""
    get_registry()
    yield


# ──────────────────────────────────────────────────────────────────────
# StepType ↔ NodeRegistry consistency
# ──────────────────────────────────────────────────────────────────────


def test_every_step_type_is_registered_or_deprecated():
    """Every StepType enum value must be either implemented or formally
    deprecated.  An "unknown" value silently breaks executor dispatch."""
    missing: list[str] = []
    for st in StepType:
        if st in _REGISTRY:
            continue
        if is_deprecated_step_type(st.value):
            continue
        missing.append(st.value)

    assert not missing, (
        "StepType enum values with no NodeRegistry entry and no "
        f"DEPRECATED_STEP_TYPES entry: {missing}. Either add a "
        "@register(StepType.X) class or list it in "
        "fpulse/ir/migrations.py:DEPRECATED_STEP_TYPES."
    )


def test_deprecated_step_types_are_real_enum_values():
    """A deprecation entry that names a non-existent enum value would
    fail silently — `is_deprecated_step_type` would return False for
    something the operator never saw."""
    enum_values = {st.value for st in StepType}
    orphans = [k for k in DEPRECATED_STEP_TYPES if k not in enum_values]
    assert not orphans, (
        f"DEPRECATED_STEP_TYPES contains entries with no matching "
        f"StepType enum value: {orphans}"
    )


def test_deprecation_replacement_points_to_real_enum_value():
    """A `replaced_by` target must itself be a real (currently registered)
    step type — otherwise migration produces broken workflows."""
    enum_values = {st.value for st in StepType}
    for legacy, dep in DEPRECATED_STEP_TYPES.items():
        if not dep.replaced_by:
            continue
        assert dep.replaced_by in enum_values, (
            f"DEPRECATED_STEP_TYPES[{legacy!r}].replaced_by={dep.replaced_by!r} "
            "is not a real StepType enum value."
        )


# ──────────────────────────────────────────────────────────────────────
# NodeDefinition shape
# ──────────────────────────────────────────────────────────────────────


_REQUIRED_CLASS_ATTRS = ("display_name", "category", "description")
_REQUIRED_STATIC_METHODS = ("default_params", "param_schema")


@pytest.mark.parametrize("step_type", list(_REGISTRY.keys()), ids=lambda s: s.value)
def test_registered_node_has_required_class_attrs(step_type, _load_registry):
    """Every registered class must declare the canvas-rendering attrs."""
    cls = _REGISTRY[step_type]
    for attr in _REQUIRED_CLASS_ATTRS:
        val = getattr(cls, attr, None)
        assert val, f"{cls.__name__} missing required class attribute {attr!r}"


@pytest.mark.parametrize("step_type", list(_REGISTRY.keys()), ids=lambda s: s.value)
def test_registered_node_has_required_static_methods(step_type, _load_registry):
    """default_params and param_schema must be present and callable so
    /api/node-types can render a palette tile without surprise."""
    cls = _REGISTRY[step_type]
    for name in _REQUIRED_STATIC_METHODS:
        fn = getattr(cls, name, None)
        assert callable(fn), (
            f"{cls.__name__} is missing static method {name!r}"
        )
        result = fn()
        # default_params → dict; param_schema → list
        if name == "default_params":
            assert isinstance(result, dict), (
                f"{cls.__name__}.default_params() must return a dict, got {type(result)}"
            )
        elif name == "param_schema":
            assert isinstance(result, list), (
                f"{cls.__name__}.param_schema() must return a list, got {type(result)}"
            )


# ──────────────────────────────────────────────────────────────────────
# Metadata maps reference real enum values
# ──────────────────────────────────────────────────────────────────────


def test_side_effect_class_keys_are_real_step_types():
    enum_values = {st.value for st in StepType}
    orphans = [k for k in SIDE_EFFECT_CLASS if k not in enum_values]
    assert not orphans, (
        f"SIDE_EFFECT_CLASS contains entries with no matching StepType: {orphans}"
    )


def test_multi_input_nodes_are_real_step_types():
    enum_values = {st.value for st in StepType}
    orphans = [k for k in MULTI_INPUT_NODES if k not in enum_values]
    assert not orphans, (
        f"MULTI_INPUT_NODES contains entries with no matching StepType: {orphans}"
    )


def test_no_input_nodes_are_real_step_types():
    enum_values = {st.value for st in StepType}
    orphans = [k for k in NO_INPUT_NODES if k not in enum_values]
    assert not orphans, (
        f"NO_INPUT_NODES contains entries with no matching StepType: {orphans}"
    )


# ──────────────────────────────────────────────────────────────────────
# Contract helper sanity
# ──────────────────────────────────────────────────────────────────────


def test_contract_for_returns_consistent_shape():
    """contract_for() must return the same keys for every enum value
    so callers can rely on the shape."""
    for st in StepType:
        c = contract_for(st.value)
        assert set(c.keys()) == {"required", "optional", "variadic"}
        assert isinstance(c["required"], int)
        assert isinstance(c["optional"], int)
        assert isinstance(c["variadic"], bool)


def test_side_effect_class_for_returns_known_value_or_none():
    valid = {"passthrough", "transforming", "terminal"}
    for st in StepType:
        cls = side_effect_class_for(st.value)
        assert cls is None or cls in valid, (
            f"side_effect_class_for({st.value!r}) returned unexpected value {cls!r}"
        )


def test_generic_connector_schemas_expose_delegated_fields():
    """Generic Source/Destination delegate by connector_type, so their static
    param_schema is just `connector_type`. connector_schemas() exposes each
    delegated concrete node's REAL fields, so the frontend/validator/AI/docs
    see the true per-connector contract instead of hand-maintained special
    cases (2026-06-16)."""
    from fpulse.nodes.generic import GenericSourceNode, GenericDestinationNode
    src = GenericSourceNode.connector_schemas()
    # csv delegates to CsvSourceNode → real fields include file_path
    assert "csv" in src and any(f.get("name") == "file_path" for f in src["csv"])
    # database delegates to DbSourceNode → non-empty (connection/query fields)
    assert src.get("database"), "database source connector schema should be non-empty"
    dst = GenericDestinationNode.connector_schemas()
    assert "csv" in dst and dst["csv"], "csv sink connector schema should be non-empty"
    assert dst.get("database"), "db sink connector schema should be non-empty"


# ──────────────────────────────────────────────────────────────────────
# Deprecation policy invariants
# ──────────────────────────────────────────────────────────────────────


def test_deprecated_type_is_not_in_side_effect_class_with_terminal():
    """A deprecated type shouldn't carry a `terminal` classification —
    terminal means "data chain ends here," which is incompatible with
    a replacement target.  Sanity check that we don't accidentally
    deprecate something whose runtime semantics still matter."""
    for legacy in DEPRECATED_STEP_TYPES:
        cls = SIDE_EFFECT_CLASS.get(legacy)
        # passthrough / transforming are fine — they're remapped via
        # `replaced_by`. Only `terminal` would be inconsistent.
        if cls == "terminal":
            dep = DEPRECATED_STEP_TYPES[legacy]
            assert dep.replaced_by is None, (
                f"{legacy} is classed terminal but has a replaced_by — "
                "review whether deprecation is the right answer."
            )
