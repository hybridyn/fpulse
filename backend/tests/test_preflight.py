"""Unit tests for the structural preflight checks (V14).

Sister file to test_validator.py. The preflight catches problems
validate_workflow() doesn't — graph shape, orphans, missing source
ancestors. Both run on the new POST /api/workflows/{id}/preflight
endpoint; tests here cover the structural pass only.
"""

from fpulse.ir.preflight import PreflightCode, preflight_workflow
from fpulse.ir.schema import Step, StepConnection, StepType, Workflow


def _f(workflow):
    """Shorthand: run preflight and return findings."""
    return preflight_workflow(workflow)


def _by_code(findings, code):
    """Filter findings by code for tighter assertions."""
    return [f for f in findings if f["code"] == code]


class TestEmptyPipeline:
    def test_no_steps_returns_empty_pipeline_error(self):
        wf = Workflow(steps=[])
        findings = _f(wf)
        assert len(findings) == 1
        assert findings[0]["code"] == PreflightCode.EMPTY_PIPELINE
        assert findings[0]["severity"] == "error"

    def test_no_steps_short_circuits_other_checks(self):
        # An empty pipeline shouldn't produce N "orphan" findings on
        # top of the empty-pipeline error.
        wf = Workflow(steps=[])
        findings = _f(wf)
        assert _by_code(findings, PreflightCode.ORPHANED_NODE) == []


class TestOrphanedNodes:
    def test_isolated_transform_is_orphan(self):
        # A bare Transform with no connections — error.
        wf = Workflow(
            steps=[Step(id="t1", type=StepType.TRANSFORM, params={"expression": "1"})],
            connections=[],
        )
        findings = _f(wf)
        orphans = _by_code(findings, PreflightCode.ORPHANED_NODE)
        assert len(orphans) == 1
        assert orphans[0]["step_id"] == "t1"
        assert orphans[0]["severity"] == "error"

    def test_isolated_source_is_unconnected_warning_not_orphan(self):
        # A source on its own isn't broken — it's just untested. Warn,
        # don't block Save.
        wf = Workflow(
            steps=[
                Step(id="s1", type=StepType.CSV_SOURCE, params={"file_path": "x.csv"}),
            ],
            connections=[],
        )
        findings = _f(wf)
        # No orphan-error
        assert _by_code(findings, PreflightCode.ORPHANED_NODE) == []
        # But an unconnected-source warning
        unconnected = _by_code(findings, PreflightCode.UNCONNECTED_SOURCE)
        assert len(unconnected) == 1
        assert unconnected[0]["severity"] == "warning"


class TestTransformWithoutInput:
    def test_transform_with_only_downstream_flagged(self):
        # A Transform that has outgoing connections but no incoming.
        # Different from orphan (which has neither).
        wf = Workflow(
            steps=[
                Step(id="t1", type=StepType.TRANSFORM, params={"expression": "1"}),
                Step(id="snk", type=StepType.CSV_SINK, params={"file_path": "o.csv"}),
            ],
            connections=[StepConnection(from_step="t1", to_step="snk")],
        )
        findings = _f(wf)
        no_input = _by_code(findings, PreflightCode.TRANSFORM_WITHOUT_INPUT)
        assert len(no_input) == 1
        assert no_input[0]["step_id"] == "t1"

    def test_source_without_input_is_not_flagged(self):
        # Sources legitimately have no inputs — they read from outside.
        # Don't flag them as transform-without-input.
        wf = Workflow(
            steps=[
                Step(id="s1", type=StepType.CSV_SOURCE, params={"file_path": "x.csv"}),
                Step(id="snk", type=StepType.CSV_SINK, params={"file_path": "o.csv"}),
            ],
            connections=[StepConnection(from_step="s1", to_step="snk")],
        )
        findings = _f(wf)
        assert _by_code(findings, PreflightCode.TRANSFORM_WITHOUT_INPUT) == []


class TestSinkWithoutSource:
    def test_sink_with_only_transform_upstream_flagged(self):
        # Two-node chain: TRANSFORM → SINK. The transform has no
        # source feeding it, so the sink has no source ancestor.
        # Both findings fire (transform-without-input on t1, sink-
        # without-source on snk).
        wf = Workflow(
            steps=[
                Step(id="t1", type=StepType.TRANSFORM, params={"expression": "1"}),
                Step(id="snk", type=StepType.CSV_SINK, params={"file_path": "o.csv"}),
            ],
            connections=[StepConnection(from_step="t1", to_step="snk")],
        )
        findings = _f(wf)
        no_source = _by_code(findings, PreflightCode.SINK_WITHOUT_SOURCE)
        assert len(no_source) == 1
        assert no_source[0]["step_id"] == "snk"

    def test_sink_with_source_ancestor_is_valid(self):
        # SOURCE → TRANSFORM → SINK. Sink has a source ancestor; no
        # sink-without-source finding.
        wf = Workflow(
            steps=[
                Step(id="s1", type=StepType.CSV_SOURCE, params={"file_path": "x.csv"}),
                Step(id="t1", type=StepType.TRANSFORM, params={"expression": "1"}),
                Step(id="snk", type=StepType.CSV_SINK, params={"file_path": "o.csv"}),
            ],
            connections=[
                StepConnection(from_step="s1", to_step="t1"),
                StepConnection(from_step="t1", to_step="snk"),
            ],
        )
        findings = _f(wf)
        assert _by_code(findings, PreflightCode.SINK_WITHOUT_SOURCE) == []

    def test_sink_with_indirect_source_ancestor_is_valid(self):
        # SOURCE → A → B → C → SINK. BFS walks all the way back to
        # find the source. Defensive depth coverage.
        wf = Workflow(
            steps=[
                Step(id="src", type=StepType.CSV_SOURCE, params={"file_path": "x.csv"}),
                Step(id="a", type=StepType.TRANSFORM, params={"expression": "1"}),
                Step(id="b", type=StepType.TRANSFORM, params={"expression": "2"}),
                Step(id="c", type=StepType.TRANSFORM, params={"expression": "3"}),
                Step(id="snk", type=StepType.CSV_SINK, params={"file_path": "o.csv"}),
            ],
            connections=[
                StepConnection(from_step="src", to_step="a"),
                StepConnection(from_step="a", to_step="b"),
                StepConnection(from_step="b", to_step="c"),
                StepConnection(from_step="c", to_step="snk"),
            ],
        )
        findings = _f(wf)
        assert _by_code(findings, PreflightCode.SINK_WITHOUT_SOURCE) == []


class TestHappyPath:
    def test_minimal_valid_pipeline_no_findings(self):
        # A well-shaped pipeline — SOURCE → SINK — produces nothing.
        wf = Workflow(
            steps=[
                Step(id="s1", type=StepType.CSV_SOURCE, params={"file_path": "x.csv"}),
                Step(id="snk", type=StepType.CSV_SINK, params={"file_path": "o.csv"}),
            ],
            connections=[StepConnection(from_step="s1", to_step="snk")],
        )
        findings = _f(wf)
        assert findings == []
