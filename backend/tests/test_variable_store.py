"""Unit tests for VariableStore."""

import pytest
from fpulse.variables.models import Variable
from fpulse.variables.store import VariableStore


class TestVariableStore:
    def test_create(self, variable_store, sample_variable):
        v = variable_store.create(sample_variable)
        assert v.key == "DB_HOST"
        assert variable_store.count() == 1

    def test_get(self, variable_store, sample_variable):
        variable_store.create(sample_variable)
        v = variable_store.get("var-001")
        assert v is not None
        assert v.value == "localhost"

    def test_get_nonexistent(self, variable_store):
        assert variable_store.get("nope") is None

    def test_list_all(self, variable_store, sample_variable):
        variable_store.create(sample_variable)
        v2 = Variable(id="var-002", key="API_KEY", value="abc123", type="secret", scope="global")
        variable_store.create(v2)
        result = variable_store.list_all()
        assert len(result) == 2

    def test_list_all_sorted_by_key(self, variable_store):
        variable_store.create(Variable(id="v1", key="ZEBRA", value="z", scope="global"))
        variable_store.create(Variable(id="v2", key="ALPHA", value="a", scope="global"))
        result = variable_store.list_all()
        assert result[0]["key"] == "ALPHA"
        assert result[1]["key"] == "ZEBRA"

    def test_list_by_scope(self, variable_store):
        variable_store.create(Variable(id="v1", key="G1", value="g", scope="global"))
        variable_store.create(Variable(id="v2", key="P1", value="p", scope="project", project_id="proj-1"))
        result = variable_store.list_all(scope="global")
        assert len(result) == 1
        assert result[0]["key"] == "G1"

    def test_list_by_project(self, variable_store):
        variable_store.create(Variable(id="v1", key="P1", value="a", scope="project", project_id="proj-1"))
        variable_store.create(Variable(id="v2", key="P2", value="b", scope="project", project_id="proj-2"))
        result = variable_store.list_all(project_id="proj-1")
        assert len(result) == 1

    def test_secret_masking(self, variable_store):
        variable_store.create(Variable(id="v1", key="TOKEN", value="supersecretvalue", type="secret", scope="global"))
        result = variable_store.list_all()
        assert result[0]["value"] == "su***ue"

    def test_secret_masking_short(self, variable_store):
        variable_store.create(Variable(id="v1", key="KEY", value="abc", type="secret", scope="global"))
        result = variable_store.list_all()
        assert result[0]["value"] == "***"

    def test_update(self, variable_store, sample_variable):
        variable_store.create(sample_variable)
        updated = variable_store.update("var-001", {"value": "192.168.1.1"})
        assert updated is not None
        assert updated.value == "192.168.1.1"

    def test_update_nonexistent(self, variable_store):
        assert variable_store.update("nope", {"value": "x"}) is None

    def test_delete(self, variable_store, sample_variable):
        variable_store.create(sample_variable)
        assert variable_store.delete("var-001") is True
        assert variable_store.get("var-001") is None

    def test_delete_nonexistent(self, variable_store):
        assert variable_store.delete("nope") is False


class TestVariableResolution:
    def test_resolve_global(self, variable_store):
        variable_store.create(Variable(id="v1", key="DB_HOST", value="global-host", scope="global"))
        assert variable_store.resolve("DB_HOST") == "global-host"

    def test_resolve_project_overrides_global(self, variable_store):
        variable_store.create(Variable(id="v1", key="DB_HOST", value="global-host", scope="global"))
        variable_store.create(Variable(id="v2", key="DB_HOST", value="project-host", scope="project", project_id="p1"))
        assert variable_store.resolve("DB_HOST", project_id="p1") == "project-host"

    def test_resolve_falls_back_to_global(self, variable_store):
        variable_store.create(Variable(id="v1", key="DB_HOST", value="global-host", scope="global"))
        assert variable_store.resolve("DB_HOST", project_id="p1") == "global-host"

    def test_resolve_nonexistent(self, variable_store):
        assert variable_store.resolve("NOPE") is None
