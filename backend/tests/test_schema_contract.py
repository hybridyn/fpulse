"""Unit tests for SchemaContractStore — contracts, validation, drift detection."""

import pytest
from fpulse.intelligence.schema_contract import SchemaContractStore


class TestSchemaContractCRUD:
    def test_create_contract(self, contract_store):
        c = contract_store.create_contract(
            workflow_id="wf-001", step_id="s1",
            columns=[
                {"name": "id", "type": "INTEGER", "nullable": False},
                {"name": "name", "type": "VARCHAR"},
            ],
        )
        assert c.workflow_id == "wf-001"
        assert len(c.expected_columns) == 2

    def test_get_contract(self, contract_store):
        c = contract_store.create_contract("wf-001", "s1", [{"name": "id", "type": "INT"}])
        found = contract_store.get_contract(c.id)
        assert found is not None
        assert found.step_id == "s1"

    def test_get_nonexistent(self, contract_store):
        assert contract_store.get_contract("nope") is None

    def test_list_contracts(self, contract_store):
        contract_store.create_contract("wf-001", "s1", [{"name": "a", "type": "INT"}])
        contract_store.create_contract("wf-001", "s2", [{"name": "b", "type": "INT"}])
        contract_store.create_contract("wf-002", "s1", [{"name": "c", "type": "INT"}])
        result = contract_store.list_contracts("wf-001")
        assert len(result) == 2

    def test_list_contracts_for_step(self, contract_store):
        contract_store.create_contract("wf-001", "s1", [{"name": "a", "type": "INT"}])
        contract_store.create_contract("wf-001", "s2", [{"name": "b", "type": "INT"}])
        result = contract_store.list_contracts_for_step("wf-001", "s1")
        assert len(result) == 1

    def test_update_contract(self, contract_store):
        c = contract_store.create_contract("wf-001", "s1", [{"name": "id", "type": "INT"}])
        updated = contract_store.update_contract(c.id, columns=[
            {"name": "id", "type": "BIGINT"},
            {"name": "email", "type": "VARCHAR"},
        ])
        assert updated is not None
        assert len(updated.expected_columns) == 2

    def test_update_nonexistent(self, contract_store):
        assert contract_store.update_contract("nope") is None

    def test_delete_contract(self, contract_store):
        c = contract_store.create_contract("wf-001", "s1", [{"name": "id", "type": "INT"}])
        assert contract_store.delete_contract(c.id) is True
        assert contract_store.get_contract(c.id) is None

    def test_delete_nonexistent(self, contract_store):
        assert contract_store.delete_contract("nope") is False


class TestSchemaValidation:
    def test_valid_schema(self, contract_store):
        c = contract_store.create_contract("wf-001", "s1", [
            {"name": "id", "type": "INTEGER", "nullable": False},
            {"name": "name", "type": "VARCHAR"},
        ])
        result = contract_store.validate_against(c.id, [
            {"name": "id", "type": "INTEGER", "nullable": False},
            {"name": "name", "type": "VARCHAR", "nullable": True},
        ])
        assert result.valid is True
        assert len(result.drifts) == 0

    def test_column_removed_is_breaking(self, contract_store):
        c = contract_store.create_contract("wf-001", "s1", [
            {"name": "id", "type": "INT"},
            {"name": "name", "type": "VARCHAR"},
        ])
        result = contract_store.validate_against(c.id, [
            {"name": "id", "type": "INT"},
        ])
        assert result.valid is False
        assert any(d.drift_type == "column_removed" for d in result.drifts)

    def test_column_added_is_info(self, contract_store):
        c = contract_store.create_contract("wf-001", "s1", [
            {"name": "id", "type": "INT"},
        ])
        result = contract_store.validate_against(c.id, [
            {"name": "id", "type": "INT"},
            {"name": "extra", "type": "VARCHAR"},
        ])
        assert result.valid is True
        assert any(d.drift_type == "column_added" and d.severity == "info" for d in result.drifts)

    def test_type_changed_is_breaking(self, contract_store):
        c = contract_store.create_contract("wf-001", "s1", [
            {"name": "id", "type": "INTEGER"},
        ])
        result = contract_store.validate_against(c.id, [
            {"name": "id", "type": "VARCHAR"},
        ])
        assert result.valid is False
        assert any(d.drift_type == "type_changed" for d in result.drifts)

    def test_compatible_types(self, contract_store):
        """INT and BIGINT should be compatible."""
        c = contract_store.create_contract("wf-001", "s1", [
            {"name": "id", "type": "INTEGER"},
        ])
        result = contract_store.validate_against(c.id, [
            {"name": "id", "type": "BIGINT"},
        ])
        assert result.valid is True

    def test_nullable_changed(self, contract_store):
        c = contract_store.create_contract("wf-001", "s1", [
            {"name": "id", "type": "INT", "nullable": False},
        ])
        result = contract_store.validate_against(c.id, [
            {"name": "id", "type": "INT", "nullable": True},
        ])
        drifts = [d for d in result.drifts if d.drift_type == "nullable_changed"]
        assert len(drifts) == 1
        assert drifts[0].severity == "warning"

    def test_missing_contract(self, contract_store):
        result = contract_store.validate_against("nonexistent", [])
        assert result.valid is False
        assert any(d.drift_type == "contract_missing" for d in result.drifts)

    def test_updates_contract_status(self, contract_store):
        c = contract_store.create_contract("wf-001", "s1", [
            {"name": "id", "type": "INT"},
        ])
        contract_store.validate_against(c.id, [{"name": "id", "type": "VARCHAR"}])
        assert contract_store.get_contract(c.id).status == "violated"


class TestSchemaDrift:
    def test_detect_drift(self, contract_store):
        c = contract_store.create_contract("wf-001", "s1", [
            {"name": "id", "type": "INT"},
            {"name": "name", "type": "VARCHAR"},
        ])
        drifts = contract_store.detect_drift(c.id, [
            {"name": "id", "type": "INT"},
            {"name": "email", "type": "VARCHAR"},
        ])
        types = {d.drift_type for d in drifts}
        assert "column_removed" in types
        assert "column_added" in types

    def test_detect_drift_missing_contract(self, contract_store):
        drifts = contract_store.detect_drift("nope", [])
        assert len(drifts) == 1
        assert drifts[0].drift_type == "contract_missing"


class TestAutoCreate:
    def test_auto_create_from_schema(self, contract_store):
        c = contract_store.auto_create_from_schema("wf-001", "s1", [
            {"name": "id", "type": "INTEGER", "nullable": False},
            {"name": "name", "type": "VARCHAR"},
        ])
        assert len(c.expected_columns) == 2
        assert c.expected_columns[0].name == "id"
        assert "Auto-created" in c.description
