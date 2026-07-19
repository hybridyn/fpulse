"""Unit tests for IR workflow validation."""

import pytest
from fpulse.ir.schema import Workflow, Step, StepType, StepConnection
from fpulse.ir.validator import (
    validate_workflow,
    ValidationError,
    validate_capabilities,
)


class TestValidatorBasic:
    def test_valid_workflow(self, sample_workflow):
        errors = validate_workflow(sample_workflow)
        assert len(errors) == 0

    def test_empty_workflow(self):
        wf = Workflow(steps=[])
        errors = validate_workflow(wf)
        assert len(errors) == 1
        assert "no steps" in errors[0].message.lower()

    def test_missing_csv_file_path(self):
        wf = Workflow(steps=[
            Step(id="s1", type=StepType.CSV_SOURCE, params={}),
        ])
        errors = validate_workflow(wf)
        assert any("file_path" in e.message for e in errors)

    def test_missing_filter_condition(self):
        wf = Workflow(steps=[
            Step(id="s1", type=StepType.CSV_SOURCE, params={"file_path": "x.csv"}),
            Step(id="s2", type=StepType.FILTER, params={}),
        ], connections=[StepConnection(from_step="s1", to_step="s2")])
        errors = validate_workflow(wf)
        assert any("condition" in e.message for e in errors)

    def test_missing_transform_expression(self):
        wf = Workflow(steps=[
            Step(id="s1", type=StepType.TRANSFORM, params={}),
        ])
        errors = validate_workflow(wf)
        assert any("expression" in e.message for e in errors)

    def test_missing_deduplicate_key(self):
        wf = Workflow(steps=[
            Step(id="s1", type=StepType.DEDUPLICATE, params={}),
        ])
        errors = validate_workflow(wf)
        assert any("key" in e.message for e in errors)

    def test_missing_aggregate_params(self):
        # 2026-05-22: backend treats `group_by` as OPTIONAL — an empty
        # group_by is a legitimate global aggregate ("count all rows").
        # Only `functions` is hard-required now. The frontend validator
        # was updated to match in this same pass.
        wf = Workflow(steps=[
            Step(id="s1", type=StepType.AGGREGATE, params={}),
        ])
        errors = validate_workflow(wf)
        assert any("functions" in e.message for e in errors)
        # group_by must NOT raise on its own.
        assert not any(
            "group_by" in e.message and "functions" not in e.message
            for e in errors
        )

    def test_global_aggregate_no_group_by_is_valid(self):
        """Global aggregate (no group_by, just functions) — backend
        compiles this to `GROUP BY ALL` / nothing, which produces one
        result row. Validation should let it through."""
        wf = Workflow(steps=[
            Step(id="s1", type=StepType.CSV_SOURCE, params={"file_path": "x.csv"}),
            Step(id="s2", type=StepType.AGGREGATE, params={
                "functions": [{"column": "*", "function": "COUNT", "alias": "n"}],
            }),
        ], connections=[StepConnection(from_step="s1", to_step="s2")])
        errors = validate_workflow(wf)
        # No aggregate-specific errors — the only thing that should
        # surface is upstream-related (if any), not group_by missing.
        agg_errors = [e for e in errors if e.step_id == "s2"]
        assert not agg_errors, f"unexpected errors on aggregate: {agg_errors!r}"

    def test_missing_join_key(self):
        wf = Workflow(steps=[
            Step(id="s1", type=StepType.CSV_SOURCE, params={"file_path": "a.csv"}),
            Step(id="s2", type=StepType.CSV_SOURCE, params={"file_path": "b.csv"}),
            Step(id="s3", type=StepType.JOIN, params={}),
        ], connections=[
            StepConnection(from_step="s1", to_step="s3"),
            StepConnection(from_step="s2", to_step="s3"),
        ])
        errors = validate_workflow(wf)
        assert any("join_key" in e.message for e in errors)

    def test_missing_output_format(self):
        wf = Workflow(steps=[
            Step(id="s1", type=StepType.OUTPUT, params={}),
        ])
        errors = validate_workflow(wf)
        assert any("format" in e.message for e in errors)

    def test_missing_db_source_query(self):
        wf = Workflow(steps=[
            Step(id="s1", type=StepType.DB_SOURCE, params={}),
        ])
        errors = validate_workflow(wf)
        assert any("query" in e.message for e in errors)


class TestValidatorConnections:
    def test_invalid_connection_from(self):
        wf = Workflow(
            steps=[Step(id="s1", type=StepType.CSV_SOURCE, params={"file_path": "x.csv"})],
            connections=[StepConnection(from_step="nonexistent", to_step="s1")],
        )
        errors = validate_workflow(wf)
        assert any("nonexistent" in e.message for e in errors)

    def test_invalid_connection_to(self):
        wf = Workflow(
            steps=[Step(id="s1", type=StepType.CSV_SOURCE, params={"file_path": "x.csv"})],
            connections=[StepConnection(from_step="s1", to_step="nonexistent")],
        )
        errors = validate_workflow(wf)
        assert any("nonexistent" in e.message for e in errors)

    def test_join_needs_2_inputs(self):
        wf = Workflow(
            steps=[
                Step(id="s1", type=StepType.CSV_SOURCE, params={"file_path": "a.csv"}),
                Step(id="s2", type=StepType.JOIN, params={"join_key": "id"}),
            ],
            connections=[StepConnection(from_step="s1", to_step="s2")],
        )
        errors = validate_workflow(wf)
        assert any("2 input" in e.message for e in errors)

    def test_join_rejects_more_than_2_inputs(self):
        # 2026-05-22: pre-fix, a JOIN with 3 inputs was silently
        # truncated to the first 2 at runtime — debuggers spent hours
        # hunting "missing rows" downstream. Now we surface the error
        # at save time so the user gets pointed at a Union instead.
        wf = Workflow(
            steps=[
                Step(id="s1", type=StepType.CSV_SOURCE, params={"file_path": "a.csv"}),
                Step(id="s2", type=StepType.CSV_SOURCE, params={"file_path": "b.csv"}),
                Step(id="s3", type=StepType.CSV_SOURCE, params={"file_path": "c.csv"}),
                Step(id="s4", type=StepType.JOIN, params={"join_key": "id"}),
            ],
            connections=[
                StepConnection(from_step="s1", to_step="s4"),
                StepConnection(from_step="s2", to_step="s4"),
                StepConnection(from_step="s3", to_step="s4"),
            ],
        )
        errors = validate_workflow(wf)
        # Should call out that exactly-2 is the contract and the user
        # has 3 connected.
        assert any("exactly 2 input" in e.message.lower() and "3" in e.message for e in errors)


class TestValidatorCycles:
    def test_cycle_detection(self):
        wf = Workflow(
            steps=[
                Step(id="s1", type=StepType.CSV_SOURCE, params={"file_path": "x.csv"}),
                Step(id="s2", type=StepType.FILTER, params={"condition": "x > 0"}),
            ],
            connections=[
                StepConnection(from_step="s1", to_step="s2"),
                StepConnection(from_step="s2", to_step="s1"),
            ],
        )
        errors = validate_workflow(wf)
        assert any("cycle" in e.message.lower() for e in errors)

    def test_no_cycle_in_linear(self, sample_workflow):
        errors = validate_workflow(sample_workflow)
        cycle_errors = [e for e in errors if "cycle" in e.message.lower()]
        assert len(cycle_errors) == 0


class TestValidateCapabilities:
    """Apr 22 2026: source/sink capability split. A source-shaped node
    must reference a connection that has 'read' capability; a sink-shaped
    node must reference one with 'write'. Direction-agnostic nodes are
    skipped. Legacy connections (empty capabilities list) are forgiven."""

    def _conn(self, conn_id, name, conn_type, capabilities):
        """Tiny stand-in for a Connection object — only attrs the validator
        actually reads via getattr."""
        class _C:
            pass
        c = _C()
        c.name = name
        c.type = conn_type
        c.capabilities = capabilities
        return c

    def test_read_capable_source_passes(self):
        wf = Workflow(steps=[
            Step(id="s1", type=StepType.DB_SOURCE, params={
                "query": "select 1", "connection_id": "c1",
            }),
        ])
        conn = self._conn("c1", "Prod DB", "postgresql", ["read", "write"])
        errors = validate_capabilities(wf, lambda cid: conn if cid == "c1" else None)
        assert errors == []

    def test_write_only_conn_on_source_node_errors(self):
        wf = Workflow(steps=[
            Step(id="s1", type=StepType.DB_SOURCE, params={
                "query": "select 1", "connection_id": "c1",
            }),
        ])
        # Slack is write-only — it can't be a SOURCE
        conn = self._conn("c1", "Alerts Channel", "slack", ["write"])
        errors = validate_capabilities(wf, lambda cid: conn if cid == "c1" else None)
        assert len(errors) == 1
        assert errors[0].step_id == "s1"
        assert "read" in errors[0].message.lower()

    def test_read_only_conn_on_sink_node_errors(self):
        wf = Workflow(steps=[
            Step(id="s1", type=StepType.DB_SINK, params={
                "table_name": "out", "connection_id": "c1",
            }),
        ])
        conn = self._conn("c1", "Read replica", "postgresql", ["read"])
        errors = validate_capabilities(wf, lambda cid: conn if cid == "c1" else None)
        assert len(errors) == 1
        assert "write" in errors[0].message.lower()

    def test_write_capable_sink_passes(self):
        wf = Workflow(steps=[
            Step(id="s1", type=StepType.SLACK_NOTIFY, params={
                "connection_id": "c1",
            }),
        ])
        conn = self._conn("c1", "Alerts", "slack", ["write"])
        errors = validate_capabilities(wf, lambda cid: conn if cid == "c1" else None)
        assert errors == []

    def test_legacy_empty_capabilities_forgiven(self):
        """Pre-Apr-22 connection rows have capabilities=[]. They must
        pass for both read and write nodes — otherwise existing pipelines
        break the moment v16 lands."""
        wf = Workflow(steps=[
            Step(id="s1", type=StepType.DB_SOURCE, params={
                "query": "x", "connection_id": "legacy",
            }),
            Step(id="s2", type=StepType.DB_SINK, params={
                "table_name": "y", "connection_id": "legacy",
            }),
        ])
        legacy_conn = self._conn("legacy", "Old Conn", "postgresql", [])
        errors = validate_capabilities(wf, lambda cid: legacy_conn)
        assert errors == []

    def test_missing_connection_no_error(self):
        """If lookup returns None we don't generate a capability error
        — that's a separate concern (the pipeline-loader catches
        connection-missing). validate_capabilities only complains about
        wrong-direction connections that DO exist."""
        wf = Workflow(steps=[
            Step(id="s1", type=StepType.DB_SOURCE, params={
                "query": "x", "connection_id": "ghost",
            }),
        ])
        errors = validate_capabilities(wf, lambda cid: None)
        assert errors == []

    def test_no_connection_id_no_error(self):
        """Steps without a connection_id (e.g. file paths) bypass the
        capability check entirely."""
        wf = Workflow(steps=[
            Step(id="s1", type=StepType.CSV_SOURCE, params={"file_path": "x.csv"}),
        ])
        errors = validate_capabilities(wf, lambda cid: None)
        assert errors == []

    def test_lookup_called_once_per_unique_connection(self):
        """Memory rule: same connection_id used by 5 steps = 1 lookup,
        not 5. Important for big pipelines that all hit one DB."""
        lookups = []
        def lookup(cid):
            lookups.append(cid)
            class _C:
                name = "shared"
                type = "postgresql"
                capabilities = ["read", "write"]
            return _C()

        wf = Workflow(steps=[
            Step(id=f"s{i}", type=StepType.DB_SOURCE, params={
                "query": "x", "connection_id": "shared",
            })
            for i in range(5)
        ])
        validate_capabilities(wf, lookup)
        assert len(lookups) == 1, f"expected 1 lookup, got {len(lookups)}"

    def test_direction_agnostic_node_skipped(self):
        """HTTP_REQUEST and EXECUTE_SQL_TASK can be either GET/POST or
        SELECT/UPDATE — the validator deliberately doesn't enforce
        either direction on them."""
        wf = Workflow(steps=[
            Step(id="s1", type=StepType.HTTP_REQUEST, params={
                "url": "https://api.example.com",
                "connection_id": "c1",
            }),
        ])
        # Even a write-only conn shouldn't trigger an error here
        conn = self._conn("c1", "Slack API", "rest_api", ["write"])
        errors = validate_capabilities(wf, lambda cid: conn)
        assert errors == []
