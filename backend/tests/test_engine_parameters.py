"""Tests for fpulse.engine.parameters — pipeline parameter resolution."""

from __future__ import annotations

import json
import re

import pytest

from fpulse.engine.parameters import (
    ParameterError,
    resolve_parameter_values,
    resolve_workflow_parameters,
)
from fpulse.ir.schema import Step, StepType, Workflow, WorkflowParameter


# ---------------------------------------------------------------------------
# resolve_parameter_values
# ---------------------------------------------------------------------------


def test_resolve_uses_declared_default_when_override_absent():
    params = [
        WorkflowParameter(name="dataset", type="string", default="orders"),
        WorkflowParameter(name="batch_size", type="int", default=1000),
    ]
    out = resolve_parameter_values(params, None)
    assert out == {"dataset": "orders", "batch_size": 1000}


def test_resolve_override_wins_over_default():
    params = [WorkflowParameter(name="dataset", type="string", default="orders")]
    out = resolve_parameter_values(params, {"dataset": "orders_2026_04"})
    assert out == {"dataset": "orders_2026_04"}


def test_resolve_int_coerces_strings_from_query_string():
    params = [WorkflowParameter(name="batch_size", type="int", default=100)]
    out = resolve_parameter_values(params, {"batch_size": "5000"})
    assert out == {"batch_size": 5000}
    assert isinstance(out["batch_size"], int)


def test_resolve_float_coerces():
    params = [WorkflowParameter(name="threshold", type="float", default=0.5)]
    out = resolve_parameter_values(params, {"threshold": "0.95"})
    assert out["threshold"] == pytest.approx(0.95)


def test_resolve_bool_truthy_aliases():
    params = [WorkflowParameter(name="dry_run", type="bool", default=False)]
    for v in ("true", "True", "1", "yes", "y", "on"):
        out = resolve_parameter_values(params, {"dry_run": v})
        assert out["dry_run"] is True, f"{v!r} should coerce to True"
    for v in ("false", "False", "0", "no", "n", "off"):
        out = resolve_parameter_values(params, {"dry_run": v})
        assert out["dry_run"] is False, f"{v!r} should coerce to False"


def test_resolve_bool_invalid_raises():
    params = [WorkflowParameter(name="flag", type="bool", default=False)]
    with pytest.raises(ParameterError):
        resolve_parameter_values(params, {"flag": "maybe"})


def test_resolve_json_accepts_string_or_dict():
    params = [WorkflowParameter(name="config", type="json", default={"a": 1})]
    # As string
    out = resolve_parameter_values(params, {"config": '{"x": 2}'})
    assert out == {"config": {"x": 2}}
    # As already-parsed dict
    out2 = resolve_parameter_values(params, {"config": {"y": 3}})
    assert out2 == {"config": {"y": 3}}


def test_resolve_required_missing_raises():
    params = [WorkflowParameter(name="dataset", type="string", required=True)]
    with pytest.raises(ParameterError) as ei:
        resolve_parameter_values(params, {})
    assert "dataset" in str(ei.value)


def test_resolve_unknown_override_raises():
    params = [WorkflowParameter(name="dataset", type="string", default="x")]
    with pytest.raises(ParameterError) as ei:
        resolve_parameter_values(params, {"unknown_param": "abc"})
    assert "unknown_param" in str(ei.value).lower() or "Unknown" in str(ei.value)


def test_resolve_no_default_no_override_returns_none_for_optional():
    params = [WorkflowParameter(name="suffix", type="string")]
    out = resolve_parameter_values(params, None)
    assert out == {"suffix": None}


# ---------------------------------------------------------------------------
# resolve_workflow_parameters — full IR walk
# ---------------------------------------------------------------------------


def _wf_with_params(params: list[WorkflowParameter], steps: list[Step]) -> Workflow:
    return Workflow(
        id="wf1",
        name="t",
        parameters=params,
        steps=steps,
        connections=[],
    )


def test_workflow_substitutes_simple_string_placeholder():
    wf = _wf_with_params(
        [WorkflowParameter(name="dataset", type="string", default="orders")],
        [Step(id="s1", type=StepType.CSV_SOURCE, params={"path": "/data/${param.dataset}.csv"})],
    )
    out = resolve_workflow_parameters(wf)
    assert out.steps[0].params["path"] == "/data/orders.csv"


def test_workflow_substitutes_full_string_preserves_typed_value():
    """If a param value IS the entire placeholder, the resolved value
    keeps its declared type (int stays int, dict stays dict)."""
    wf = _wf_with_params(
        [WorkflowParameter(name="batch", type="int", default=500)],
        [Step(id="s1", type=StepType.FILTER, params={"chunk_size": "${param.batch}"})],
    )
    out = resolve_workflow_parameters(wf)
    assert out.steps[0].params["chunk_size"] == 500
    assert isinstance(out.steps[0].params["chunk_size"], int)


def test_workflow_unknown_placeholder_raises():
    wf = _wf_with_params(
        [WorkflowParameter(name="x", type="string", default="ok")],
        [Step(id="s1", type=StepType.FILTER, params={"q": "${param.does_not_exist}"})],
    )
    with pytest.raises(ParameterError) as ei:
        resolve_workflow_parameters(wf)
    assert "does_not_exist" in str(ei.value)


def test_workflow_system_utcnow_placeholder_substitutes():
    wf = _wf_with_params(
        [],
        [Step(id="s1", type=StepType.FILTER, params={"ts": "${utcnow:%Y-%m-%d}"})],
    )
    out = resolve_workflow_parameters(wf)
    val = out.steps[0].params["ts"]
    # YYYY-MM-DD shape
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", val), f"Expected ISO date, got {val!r}"


def test_workflow_system_utcnow_no_format_returns_iso():
    wf = _wf_with_params(
        [],
        [Step(id="s1", type=StepType.FILTER, params={"ts": "${utcnow}"})],
    )
    out = resolve_workflow_parameters(wf)
    val = out.steps[0].params["ts"]
    # ISO 8601 UTC — contains 'T' and '+00:00' or 'Z'
    assert "T" in val and ("+00:00" in val or "Z" in val), f"Expected ISO timestamp, got {val!r}"


def test_workflow_system_run_id_is_consistent_within_run():
    wf = _wf_with_params(
        [],
        [
            Step(id="s1", type=StepType.FILTER, params={"a": "${run_id}"}),
            Step(id="s2", type=StepType.FILTER, params={"b": "${run_id}"}),
        ],
    )
    out = resolve_workflow_parameters(wf)
    a = out.steps[0].params["a"]
    b = out.steps[1].params["b"]
    # Both steps should see the SAME run id (single resolution per run)
    assert a == b
    # uuid4 hex shape
    assert re.fullmatch(r"[0-9a-f]{32}", a), f"Expected uuid4 hex, got {a!r}"


def test_workflow_records_resolved_values_on_metadata():
    wf = _wf_with_params(
        [WorkflowParameter(name="dataset", type="string", default="orders")],
        [Step(id="s1", type=StepType.FILTER, params={})],
    )
    out = resolve_workflow_parameters(wf, {"dataset": "ABC"})
    assert out.metadata["_resolved_parameters"] == {"dataset": "ABC"}
    assert "_run_id" in out.metadata


def test_workflow_walks_nested_params():
    """${param.X} inside lists and nested dicts should also be substituted."""
    wf = _wf_with_params(
        [WorkflowParameter(name="env", type="string", default="prod")],
        [Step(id="s1", type=StepType.FILTER, params={
            "rules": [
                {"name": "rule_${param.env}", "active": True},
                "${param.env}_filter",
            ],
            "config": {"prefix": "[${param.env}]"},
        })],
    )
    out = resolve_workflow_parameters(wf)
    rules = out.steps[0].params["rules"]
    assert rules[0]["name"] == "rule_prod"
    assert rules[1] == "prod_filter"
    assert out.steps[0].params["config"]["prefix"] == "[prod]"


def test_workflow_no_parameters_no_overrides_is_passthrough():
    """Pipelines with empty parameters and no overrides should still resolve
    cleanly — the resolver runs but does no substitution."""
    wf = _wf_with_params(
        [],
        [Step(id="s1", type=StepType.FILTER, params={"path": "/static.csv"})],
    )
    out = resolve_workflow_parameters(wf)
    assert out.steps[0].params["path"] == "/static.csv"
