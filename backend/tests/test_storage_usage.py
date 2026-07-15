"""Tests for the storage-usage scanner (Y12 2026-05-23).

The scanner answers "what workflows reference this file/table?" — the
operator gates destructive actions (Delete file, Drop table, Replace
bytes) on its output. Tests pin the four reference shapes the scanner
detects so a future refactor can't silently miss one.
"""

from __future__ import annotations

from fpulse.datastore.models import (
    OBJECT_KIND_FILE,
    StorageObject,
    StorageTable,
)
from fpulse.datastore.usage import _scan_workflow_for_refs


# ── _scan_workflow_for_refs ───────────────────────────────────────────────


class TestScanWorkflow:
    def test_local_table_source_picks_up_schema_name(self):
        wf = {
            "id": "wf1",
            "steps": [
                {
                    "id": "s1",
                    "type": "local_table_source",
                    "params": {"schema_name": "sales", "table_name": "orders"},
                },
            ],
        }
        tables, files = _scan_workflow_for_refs(wf)
        assert tables == [("sales", "orders", "source")]
        assert files == []

    def test_local_table_sink_picks_up_schema_name(self):
        wf = {
            "steps": [
                {
                    "id": "s1",
                    "type": "local_table_sink",
                    "params": {"schema_name": "default", "table_name": "customers"},
                },
            ],
        }
        tables, files = _scan_workflow_for_refs(wf)
        assert tables == [("default", "customers", "sink")]

    def test_generic_source_with_local_table_connector_type(self):
        # connector_type='local_table' dispatches through SOURCE_MAP →
        # LOCAL_TABLE_SOURCE. The scanner should treat it identically
        # to a direct local_table_source step.
        wf = {
            "steps": [
                {
                    "id": "s1",
                    "type": "source",
                    "params": {
                        "connector_type": "local_table",
                        "schema_name": "hr",
                        "table_name": "employees",
                    },
                },
            ],
        }
        tables, files = _scan_workflow_for_refs(wf)
        assert tables == [("hr", "employees", "source")]

    def test_generic_destination_with_local_table_connector_type(self):
        wf = {
            "steps": [
                {
                    "id": "s1",
                    "type": "destination",
                    "params": {
                        "connector_type": "local_table",
                        "schema_name": "default",
                        "table_name": "outputs",
                    },
                },
            ],
        }
        tables, _ = _scan_workflow_for_refs(wf)
        assert tables == [("default", "outputs", "sink")]

    def test_csv_source_file_path_picked_up(self):
        wf = {
            "steps": [
                {
                    "id": "s1",
                    "type": "csv_source",
                    "params": {"file_path": "uploads/default/orders.csv"},
                },
            ],
        }
        _, files = _scan_workflow_for_refs(wf)
        assert files == ["uploads/default/orders.csv"]

    def test_backslash_paths_normalised(self):
        wf = {
            "steps": [
                {
                    "type": "parquet_source",
                    "params": {"file_path": "uploads\\default\\events.parquet"},
                },
            ],
        }
        _, files = _scan_workflow_for_refs(wf)
        assert files == ["uploads/default/events.parquet"]

    def test_generic_source_picks_up_both_file_path_and_connector(self):
        # `source` with connector_type='csv' + file_path → just file ref.
        wf = {
            "steps": [
                {
                    "type": "source",
                    "params": {
                        "connector_type": "csv",
                        "file_path": "uploads/default/x.csv",
                    },
                },
            ],
        }
        tables, files = _scan_workflow_for_refs(wf)
        assert tables == []
        assert files == ["uploads/default/x.csv"]

    def test_empty_table_name_is_dropped(self):
        # A misconfigured step with empty table_name shouldn't crash
        # or fake a "" reference.
        wf = {
            "steps": [
                {"type": "local_table_source", "params": {"schema_name": "x"}},
                {"type": "local_table_source", "params": {"table_name": ""}},
            ],
        }
        tables, _ = _scan_workflow_for_refs(wf)
        assert tables == []

    def test_unrecognised_step_types_ignored(self):
        wf = {
            "steps": [
                {"type": "filter", "params": {"condition": "x > 0"}},
                {"type": "join", "params": {"on": ["id"]}},
            ],
        }
        tables, files = _scan_workflow_for_refs(wf)
        assert tables == [] and files == []

    def test_malformed_workflow_returns_empty(self):
        assert _scan_workflow_for_refs({}) == ([], [])
        assert _scan_workflow_for_refs({"steps": None}) == ([], [])
        assert _scan_workflow_for_refs({"steps": [None, "weird"]}) == ([], [])


# ── compute_workspace_usage with a real workflow_store + datastore ────────


class _FakeWorkflowStore:
    """Minimal stand-in for WorkflowStore.list_all().

    Tests build a list of workflow dicts and the scanner reads through
    this fake instead of hitting the real DB. Real WorkflowStore
    fixtures are too heavy for a pure scanner unit test.
    """

    def __init__(self, workflows: list[dict]):
        self._workflows = workflows

    def list_all(self, workspace_id: str | None = None) -> list[dict]:
        return list(self._workflows)


def _install_fake_workflow_store(monkeypatch, workflows: list[dict]):
    """Patch app_state['workflow_store'] for one test."""
    from fpulse.main import app_state
    monkeypatch.setitem(app_state, "workflow_store", _FakeWorkflowStore(workflows))


class TestComputeWorkspaceUsage:
    def test_table_reference_maps_to_table_id(self, datastore, monkeypatch):
        # Seed the datastore with a managed table.
        from fpulse.datastore.store import get_store as get_datastore
        from fpulse.main import app_state
        monkeypatch.setitem(app_state, "datastore", datastore)

        table = StorageTable(
            workspace_id="default",
            schema_name="sales",
            name="orders",
            path="tables/default/sales/orders",
        )
        datastore.save_table(table)

        _install_fake_workflow_store(monkeypatch, [
            {
                "id": "wf1",
                "name": "Orders ETL",
                "steps": [
                    {
                        "type": "local_table_source",
                        "params": {"schema_name": "sales", "table_name": "orders"},
                    },
                ],
            },
        ])

        from fpulse.datastore.usage import compute_workspace_usage
        result = compute_workspace_usage("default")

        assert table.id in result["tables"]
        refs = result["tables"][table.id]
        assert refs == [{"workflow_id": "wf1", "name": "Orders ETL", "role": "source"}]

    def test_file_path_reference_maps_to_object_id(self, datastore, monkeypatch):
        from fpulse.main import app_state
        monkeypatch.setitem(app_state, "datastore", datastore)

        obj = StorageObject(
            workspace_id="default",
            kind=OBJECT_KIND_FILE,
            name="orders.csv",
            path="uploads/default/orders.csv",
            format="csv",
        )
        datastore.save_object(obj)

        _install_fake_workflow_store(monkeypatch, [
            {
                "id": "wf1",
                "name": "Daily import",
                "steps": [
                    {
                        "type": "csv_source",
                        "params": {"file_path": "uploads/default/orders.csv"},
                    },
                ],
            },
        ])

        from fpulse.datastore.usage import compute_workspace_usage
        result = compute_workspace_usage("default")

        assert obj.id in result["files"]
        assert result["files"][obj.id][0]["workflow_id"] == "wf1"

    def test_promote_provenance_surfaces_downstream_pipelines(self, datastore, monkeypatch):
        """Case 4 — a file that seeded a managed table inherits the
        table's pipeline list, so deleting the file warns about
        downstream damage."""
        from fpulse.main import app_state
        monkeypatch.setitem(app_state, "datastore", datastore)

        obj = StorageObject(
            workspace_id="default",
            kind=OBJECT_KIND_FILE,
            name="seed.csv",
            path="uploads/default/seed.csv",
            format="csv",
        )
        datastore.save_object(obj)
        table = StorageTable(
            workspace_id="default",
            schema_name="default",
            name="seeded",
            path="tables/default/default/seeded",
            created_from_object_id=obj.id,
        )
        datastore.save_table(table)

        _install_fake_workflow_store(monkeypatch, [
            {
                "id": "wf1",
                "name": "Reads seeded table",
                "steps": [
                    {
                        "type": "local_table_source",
                        "params": {"schema_name": "default", "table_name": "seeded"},
                    },
                ],
            },
        ])

        from fpulse.datastore.usage import compute_workspace_usage
        result = compute_workspace_usage("default")

        # The table's pipeline shows up.
        assert table.id in result["tables"]
        # The file inherits it via the via_table edge.
        assert obj.id in result["files"]
        seeded_refs = result["files"][obj.id]
        assert len(seeded_refs) == 1
        assert seeded_refs[0]["via_table"] == "default.seeded"

    def test_no_references_returns_empty_dicts(self, datastore, monkeypatch):
        from fpulse.main import app_state
        monkeypatch.setitem(app_state, "datastore", datastore)
        _install_fake_workflow_store(monkeypatch, [])

        from fpulse.datastore.usage import compute_workspace_usage
        result = compute_workspace_usage("default")
        assert result == {"files": {}, "tables": {}}
