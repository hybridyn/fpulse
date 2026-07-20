"""Unit tests for ProjectStore."""

import pytest
from fpulse.projects.models import Project
from fpulse.projects.store import ProjectStore


class TestProjectStore:
    def test_default_project_exists(self, project_store):
        default = project_store.get("default")
        assert default is not None
        assert default.name == "Default"

    def test_count_includes_default(self, project_store):
        assert project_store.count() == 1

    def test_create_project(self, project_store, sample_project):
        created = project_store.create(sample_project)
        assert created.id == "proj-test-001"
        assert project_store.count() == 2

    def test_get_project(self, project_store, sample_project):
        project_store.create(sample_project)
        p = project_store.get("proj-test-001")
        assert p is not None
        assert p.name == "Test Project"

    def test_get_nonexistent(self, project_store):
        assert project_store.get("nope") is None

    def test_list_all_sorted(self, project_store, sample_project):
        project_store.create(sample_project)
        result = project_store.list_all()
        assert len(result) == 2
        # "Default" comes before "Test Project" alphabetically
        assert result[0]["name"] == "Default"
        assert result[1]["name"] == "Test Project"

    def test_update_project(self, project_store, sample_project):
        project_store.create(sample_project)
        updated = project_store.update("proj-test-001", {"name": "Renamed"})
        assert updated is not None
        assert updated.name == "Renamed"

    def test_update_nonexistent(self, project_store):
        assert project_store.update("nope", {"name": "X"}) is None

    def test_update_ignores_none_values(self, project_store, sample_project):
        project_store.create(sample_project)
        project_store.update("proj-test-001", {"name": "New", "description": None})
        p = project_store.get("proj-test-001")
        assert p.name == "New"
        assert p.description == "A test project"  # unchanged

    def test_delete_project(self, project_store, sample_project):
        project_store.create(sample_project)
        assert project_store.delete("proj-test-001") is True
        assert project_store.get("proj-test-001") is None
        assert project_store.count() == 1

    def test_default_is_deletable(self, project_store):
        """As of 2026-05-09 the Default project IS deletable — the backend
        stops auto-recreating it once the user has at least one other
        project. Single-user installs that never created another project
        still keep Default (via the bootstrap path on next startup)."""
        assert project_store.delete("default") is True
        assert project_store.get("default") is None

    def test_delete_nonexistent(self, project_store):
        assert project_store.delete("nope") is False

    def test_deep_copy_isolation(self, project_store, sample_project):
        project_store.create(sample_project)
        sample_project.name = "Mutated"
        p = project_store.get("proj-test-001")
        assert p.name == "Test Project"
