"""Tests for the workspace datastore (Y1-Y3 2026-05-23).

Three layers under test:

  * ``DataStore`` CRUD on storage_objects / storage_tables / storage_columns
  * ``WorkspacePaths`` filesystem-layout helpers + traversal guard
  * Reconciliation back-fill from disk

The IR-node mode tests (replace / append / merge) live in
``test_local_table_node.py`` to keep DuckDB-touching tests isolated.
"""

from __future__ import annotations

import os

import pytest

from fpulse.datastore.models import (
    OBJECT_KIND_FILE,
    OBJECT_KIND_OUTPUT,
    StorageColumn,
    StorageObject,
    StorageTable,
)
from fpulse.datastore.paths import (
    format_from_filename,
    safe_filename,
    safe_join_under,
    safe_schema_or_table_name,
    workspace_paths,
)


# ── Path helpers ──────────────────────────────────────────────────────────


class TestPaths:
    def test_safe_filename_strips_unsafe_chars(self):
        assert safe_filename("Q1 2026 Sales!.csv") == "Q1_2026_Sales_.csv"
        assert safe_filename("../etc/passwd") == "passwd"
        assert safe_filename("") == "upload.bin"

    def test_safe_schema_normalises_case_and_chars(self):
        assert safe_schema_or_table_name("My Sales 2026") == "my_sales_2026"
        assert safe_schema_or_table_name("") == "default"
        assert safe_schema_or_table_name("__weird__") == "weird"

    def test_safe_join_under_blocks_traversal(self, tmp_path):
        root = str(tmp_path)
        # Allowed
        ok = safe_join_under(root, "uploads", "default", "x.csv")
        assert ok.startswith(os.path.abspath(root))
        # Blocked
        with pytest.raises(ValueError, match="path traversal"):
            safe_join_under(root, "..", "outside.txt")
        with pytest.raises(ValueError, match="path traversal"):
            safe_join_under(root, "uploads", "..", "..", "outside.txt")

    def test_workspace_paths_has_four_roots(self, tmp_path):
        wp = workspace_paths(str(tmp_path), "default")
        assert wp.uploads.endswith(os.path.join("uploads", "default"))
        assert wp.outputs.endswith(os.path.join("outputs", "default"))
        assert wp.tables.endswith(os.path.join("tables", "default"))
        assert wp.trash.endswith(os.path.join("trash", "default"))
        # ensure() creates them.
        wp.ensure()
        for p in (wp.uploads, wp.outputs, wp.tables, wp.trash):
            assert os.path.isdir(p)

    def test_table_dir_sanitises_names(self, tmp_path):
        wp = workspace_paths(str(tmp_path), "default").ensure()
        td = wp.table_dir("My Sales", "Big Table")
        assert os.path.basename(td) == "big_table"
        assert os.path.basename(os.path.dirname(td)) == "my_sales"

    def test_relative_to_data_dir_uses_forward_slashes(self, tmp_path):
        wp = workspace_paths(str(tmp_path), "default")
        abs_p = os.path.join(wp.uploads, "x.csv")
        rel = wp.relative_to_data_dir(abs_p)
        assert "\\" not in rel
        assert rel.startswith("uploads/default/")

    def test_format_from_filename(self):
        assert format_from_filename("orders.csv") == "csv"
        assert format_from_filename("data.parquet") == "parquet"
        assert format_from_filename("notes.xlsx") == "excel"
        assert format_from_filename("README") is None


# ── DataStore CRUD ────────────────────────────────────────────────────────


class TestStorageObjectCRUD:
    def test_save_and_get_round_trips(self, datastore):
        obj = StorageObject(
            workspace_id="default",
            kind=OBJECT_KIND_FILE,
            name="orders.csv",
            path="uploads/default/orders-20260523.csv",
            format="csv",
            size_bytes=1234,
        )
        datastore.save_object(obj)
        loaded = datastore.get_object(obj.id, workspace_id="default")
        assert loaded is not None
        assert loaded.name == "orders.csv"
        assert loaded.size_bytes == 1234
        assert loaded.deleted_at is None

    def test_get_respects_workspace_scope(self, datastore):
        obj = StorageObject(workspace_id="alpha", kind="file", name="a.csv",
                            path="uploads/alpha/a.csv")
        datastore.save_object(obj)
        # Wrong workspace → not found.
        assert datastore.get_object(obj.id, workspace_id="beta") is None
        # Right workspace → found.
        assert datastore.get_object(obj.id, workspace_id="alpha") is not None

    def test_list_objects_filters_by_kind(self, datastore):
        datastore.save_object(StorageObject(
            workspace_id="default", kind="file", name="up.csv",
            path="uploads/default/up.csv"))
        datastore.save_object(StorageObject(
            workspace_id="default", kind="output", name="out.parquet",
            path="outputs/default/pid/rid/out.parquet",
            pipeline_id="pid", run_id="rid"))

        files = datastore.list_objects("default", kind="file")
        outputs = datastore.list_objects("default", kind="output")
        assert len(files) == 1 and files[0].kind == "file"
        assert len(outputs) == 1 and outputs[0].kind == "output"

    def test_soft_delete_then_list_with_include_deleted(self, datastore):
        obj = StorageObject(workspace_id="default", kind="file",
                            name="x.csv", path="uploads/default/x.csv")
        datastore.save_object(obj)
        assert datastore.soft_delete_object(obj.id) is True
        # Default list_objects hides soft-deleted.
        live = datastore.list_objects("default", kind="file")
        assert obj.id not in {o.id for o in live}
        # include_deleted=True shows it back.
        all_rows = datastore.list_objects("default", kind="file", include_deleted=True)
        assert obj.id in {o.id for o in all_rows}
        # Soft delete is idempotent.
        assert datastore.soft_delete_object(obj.id) is True

    def test_outputs_grouped_by_pipeline_and_run(self, datastore):
        for i in range(3):
            datastore.save_object(StorageObject(
                workspace_id="default", kind="output",
                name=f"out-{i}.parquet",
                path=f"outputs/default/pid-a/run-1/out-{i}.parquet",
                pipeline_id="pid-a", run_id="run-1", size_bytes=100 + i,
            ))
        datastore.save_object(StorageObject(
            workspace_id="default", kind="output",
            name="other.parquet",
            path="outputs/default/pid-b/run-2/other.parquet",
            pipeline_id="pid-b", run_id="run-2", size_bytes=500,
        ))
        groups = datastore.list_outputs_grouped("default")
        assert len(groups) == 2
        pid_a = next(g for g in groups if g["pipeline_id"] == "pid-a")
        assert pid_a["object_count"] == 3
        assert pid_a["size_bytes"] == 303  # 100 + 101 + 102


class TestStorageTableCRUD:
    def test_unique_index_blocks_duplicate(self, datastore):
        import sqlite3
        t1 = StorageTable(workspace_id="default", schema_name="default",
                          name="customers", path="tables/default/default/customers")
        datastore.save_table(t1)
        t2 = StorageTable(workspace_id="default", schema_name="default",
                          name="customers", path="tables/default/default/customers")
        # Composite unique index on (workspace, schema, name).
        with pytest.raises(sqlite3.IntegrityError):
            datastore.save_table(t2)

    def test_find_by_name_returns_match(self, datastore):
        t = StorageTable(workspace_id="default", schema_name="sales",
                         name="orders", path="tables/default/sales/orders")
        datastore.save_table(t)
        found = datastore.find_table_by_name("default", "sales", "orders")
        assert found is not None
        assert found.id == t.id
        # Wrong schema → no match.
        assert datastore.find_table_by_name("default", "hr", "orders") is None

    def test_hard_delete_cascades_columns(self, datastore):
        t = StorageTable(workspace_id="default", schema_name="default",
                         name="customers", path="tables/default/default/customers")
        datastore.save_table(t)
        datastore.save_columns([
            StorageColumn(workspace_id="default", table_id=t.id, name="id", type="BIGINT", ordinal=0),
            StorageColumn(workspace_id="default", table_id=t.id, name="email", type="VARCHAR", ordinal=1),
        ], table_id=t.id)
        assert len(datastore.list_columns(table_id=t.id)) == 2
        datastore.hard_delete_table(t.id)
        # Table gone.
        assert datastore.get_table(t.id) is None
        # Columns cascaded.
        assert datastore.list_columns(table_id=t.id) == []


class TestStorageColumns:
    def test_save_columns_replaces_existing_set(self, datastore):
        oid = "obj_test"
        datastore.save_columns([
            StorageColumn(workspace_id="default", object_id=oid,
                          name="a", type="VARCHAR", ordinal=0),
        ], object_id=oid)
        datastore.save_columns([
            StorageColumn(workspace_id="default", object_id=oid,
                          name="x", type="BIGINT", ordinal=0),
            StorageColumn(workspace_id="default", object_id=oid,
                          name="y", type="DOUBLE", ordinal=1),
        ], object_id=oid)
        cols = datastore.list_columns(object_id=oid)
        assert [c.name for c in cols] == ["x", "y"]

    def test_save_columns_requires_exactly_one_owner(self, datastore):
        with pytest.raises(ValueError, match="exactly one"):
            datastore.save_columns([], table_id=None, object_id=None)
        with pytest.raises(ValueError, match="exactly one"):
            datastore.save_columns([], table_id="t", object_id="o")


class TestWorkspaceSummary:
    def test_summary_aggregates_all_kinds(self, datastore):
        datastore.save_object(StorageObject(
            workspace_id="default", kind="file", name="a.csv",
            path="uploads/default/a.csv", size_bytes=100))
        datastore.save_object(StorageObject(
            workspace_id="default", kind="file", name="b.csv",
            path="uploads/default/b.csv", size_bytes=200))
        datastore.save_object(StorageObject(
            workspace_id="default", kind="output", name="o.parquet",
            path="outputs/default/p/r/o.parquet",
            pipeline_id="p", run_id="r", size_bytes=500))
        datastore.save_table(StorageTable(
            workspace_id="default", schema_name="default", name="c",
            path="tables/default/default/c", size_bytes=1000))

        summary = datastore.workspace_summary("default")
        assert summary["file_count"] == 2
        assert summary["file_size_bytes"] == 300
        assert summary["output_count"] == 1
        assert summary["output_size_bytes"] == 500
        assert summary["table_count"] == 1
        assert summary["table_size_bytes"] == 1000
        assert summary["total_size_bytes"] == 300 + 500 + 1000


# ── Reconciler ────────────────────────────────────────────────────────────


class TestReconciler:
    def test_reconcile_indexes_existing_uploads(self, tmp_path, datastore):
        from fpulse.datastore.reconcile import reconcile_all

        # Lay down two files that exist on disk but aren't in the index.
        ws_dir = tmp_path / "uploads" / "default"
        ws_dir.mkdir(parents=True)
        (ws_dir / "orders.csv").write_text("a,b\n1,2\n")
        (ws_dir / "events.json").write_text('{"x": 1}')

        reconcile_all(datastore, str(tmp_path), force=True)

        files = datastore.list_objects("default", kind="file")
        names = {f.name for f in files}
        assert names == {"orders.csv", "events.json"}
        for f in files:
            assert f.size_bytes > 0
            assert f.format in {"csv", "json"}

    def test_reconcile_is_idempotent_and_add_only(self, tmp_path, datastore):
        from fpulse.datastore.reconcile import reconcile_all

        ws_dir = tmp_path / "uploads" / "default"
        ws_dir.mkdir(parents=True)
        (ws_dir / "a.csv").write_text("col\n1\n")
        reconcile_all(datastore, str(tmp_path), force=True)
        first_pass = {o.id for o in datastore.list_objects("default", kind="file")}

        # Run again — sentinel exists now, but force=True re-runs.
        reconcile_all(datastore, str(tmp_path), force=True)
        second_pass = {o.id for o in datastore.list_objects("default", kind="file")}
        assert first_pass == second_pass  # no duplicate rows

    def test_reconcile_indexes_pipeline_outputs(self, tmp_path, datastore):
        from fpulse.datastore.reconcile import reconcile_all

        out_dir = tmp_path / "outputs" / "default" / "pid-a" / "run-1"
        out_dir.mkdir(parents=True)
        (out_dir / "result.parquet").write_bytes(b"PAR1\x00")

        reconcile_all(datastore, str(tmp_path), force=True)
        outputs = datastore.list_objects("default", kind="output")
        assert len(outputs) == 1
        assert outputs[0].pipeline_id == "pid-a"
        assert outputs[0].run_id == "run-1"
