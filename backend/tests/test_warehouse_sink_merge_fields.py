"""B2 + B4 foundation test (2026-06-08): warehouse sink merge UX fields.

AUDIT FINDING (docs/design/backfill-ux-1.2.md): per-dialect MERGE SQL
already exists in engine/bulk_load/dialects (postgres ON CONFLICT,
mssql MERGE, snowflake MERGE), all keyed on BulkLoadRequest.primary_key.
The gap was purely UX - the warehouse sink couldn't select 'merge'
mode and had no field for the merge key.

This test pins the foundation: the fields exist + are shaped right.
The execute() wire-in (merge_key -> request.primary_key + tombstone
DELETE) is B2.1 / B4.1, deferred.

Contracts pinned:
  * 'merge' is a selectable write mode
  * merge_key field exists, shown only in merge mode
  * tombstone_column field exists, shown only in merge mode
"""
from __future__ import annotations

import pytest

from fpulse.nodes.sinks import WarehouseSinkNode


def _schema_by_name():
    return {f["name"]: f for f in WarehouseSinkNode.param_schema()}


class TestMergeMode:
    def test_merge_is_a_write_mode_option(self):
        fields = _schema_by_name()
        mode = fields["mode"]
        assert "merge" in mode["options"], (
            "B2 regression - 'merge' must be selectable as a write mode "
            "(the bulk loaders already implement it)"
        )

    def test_existing_modes_still_present(self):
        # Additive change - don't break the existing three modes
        mode = _schema_by_name()["mode"]
        for m in ("create", "append", "truncate"):
            assert m in mode["options"], f"existing mode {m} must remain"

    def test_default_mode_unchanged(self):
        # Don't silently change the default behaviour for existing pipelines
        assert _schema_by_name()["mode"]["default"] == "create"


class TestMergeKeyField:
    def test_merge_key_field_exists(self):
        assert "merge_key" in _schema_by_name()

    def test_merge_key_only_shown_in_merge_mode(self):
        mk = _schema_by_name()["merge_key"]
        assert mk.get("show_when") == {"mode": ["merge"]}

    def test_merge_key_is_text(self):
        assert _schema_by_name()["merge_key"]["type"] == "text"


class TestTombstoneField:
    def test_tombstone_column_field_exists(self):
        assert "tombstone_column" in _schema_by_name()

    def test_tombstone_only_shown_in_merge_mode(self):
        ts = _schema_by_name()["tombstone_column"]
        assert ts.get("show_when") == {"mode": ["merge"]}


class TestSchemaIntegrity:
    def test_all_fields_have_required_keys(self):
        # Every param entry must have name + type + label (the UI relies
        # on these). Pin that the new fields didn't break the contract.
        for f in WarehouseSinkNode.param_schema():
            assert "name" in f and "type" in f and "label" in f, f

    def test_module_imports_cleanly(self):
        # Smoke: importing the sinks module (widely imported) must not
        # raise after the edit.
        import importlib
        import fpulse.nodes.sinks as sinks_mod
        importlib.reload(sinks_mod)
        assert hasattr(sinks_mod, "WarehouseSinkNode")
