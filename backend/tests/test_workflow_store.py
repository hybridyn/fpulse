"""Unit tests for WorkflowStore — versioning, CRUD, diff, status."""

import pytest
from fpulse.ir.schema import Workflow, Step, StepType, StepConnection, PipelineStatus
from fpulse.ir.versioning import WorkflowStore


class TestWorkflowStoreCRUD:
    def test_save_creates_version_1(self, workflow_store, sample_workflow):
        v = workflow_store.save(sample_workflow, change_summary="Initial")
        assert v.version == 1
        assert v.workflow.name == "Test Pipeline"
        assert v.change_summary == "Initial"

    def test_save_increments_version(self, workflow_store, sample_workflow):
        workflow_store.save(sample_workflow, change_summary="v1")
        sample_workflow.name = "Updated Pipeline"
        v2 = workflow_store.save(sample_workflow, change_summary="v2")
        assert v2.version == 2
        assert v2.workflow.name == "Updated Pipeline"

    def test_get_latest(self, workflow_store, sample_workflow):
        workflow_store.save(sample_workflow)
        sample_workflow.name = "v2"
        workflow_store.save(sample_workflow)
        v = workflow_store.get("test-wf-001")
        assert v.version == 2
        assert v.workflow.name == "v2"

    def test_get_specific_version(self, workflow_store, sample_workflow):
        workflow_store.save(sample_workflow)
        sample_workflow.name = "v2"
        workflow_store.save(sample_workflow)
        v1 = workflow_store.get("test-wf-001", version=1)
        assert v1.version == 1
        assert v1.workflow.name == "Test Pipeline"

    def test_get_nonexistent_returns_none(self, workflow_store):
        assert workflow_store.get("nonexistent") is None

    def test_get_invalid_version_returns_none(self, workflow_store, sample_workflow):
        workflow_store.save(sample_workflow)
        assert workflow_store.get("test-wf-001", version=99) is None
        assert workflow_store.get("test-wf-001", version=0) is None

    def test_get_latest_workflow(self, workflow_store, sample_workflow):
        workflow_store.save(sample_workflow)
        wf = workflow_store.get_latest_workflow("test-wf-001")
        assert wf is not None
        assert wf.name == "Test Pipeline"

    def test_get_latest_workflow_nonexistent(self, workflow_store):
        assert workflow_store.get_latest_workflow("nope") is None

    def test_list_all(self, workflow_store, sample_workflow):
        workflow_store.save(sample_workflow)
        wf2 = Workflow(id="wf-002", name="Second Pipeline")
        workflow_store.save(wf2)
        result = workflow_store.list_all()
        assert len(result) == 2
        names = {r["name"] for r in result}
        assert "Test Pipeline" in names
        assert "Second Pipeline" in names

    def test_list_all_empty(self, workflow_store):
        assert workflow_store.list_all() == []

    def test_list_all_contains_expected_fields(self, workflow_store, sample_workflow):
        workflow_store.save(sample_workflow)
        result = workflow_store.list_all()[0]
        assert "id" in result
        assert "name" in result
        assert "version" in result
        assert "step_count" in result
        assert "status" in result
        assert "updated_at" in result

    def test_delete(self, workflow_store, sample_workflow):
        workflow_store.save(sample_workflow)
        assert workflow_store.delete("test-wf-001") is True
        assert workflow_store.get("test-wf-001") is None

    def test_delete_nonexistent(self, workflow_store):
        assert workflow_store.delete("nope") is False

    def test_deep_copy_isolation(self, workflow_store, sample_workflow):
        """Saved workflow should be isolated from mutations."""
        workflow_store.save(sample_workflow)
        sample_workflow.name = "Mutated"
        v = workflow_store.get("test-wf-001")
        assert v.workflow.name == "Test Pipeline"


class TestWorkflowStoreVersioning:
    def test_get_versions(self, workflow_store, sample_workflow):
        workflow_store.save(sample_workflow, change_summary="v1")
        sample_workflow.name = "v2"
        workflow_store.save(sample_workflow, change_summary="v2")
        versions = workflow_store.get_versions("test-wf-001")
        assert len(versions) == 2
        assert versions[0]["version"] == 1
        assert versions[1]["version"] == 2

    def test_get_versions_empty(self, workflow_store):
        assert workflow_store.get_versions("nope") == []

    def test_diff_added_step(self, workflow_store, sample_workflow):
        workflow_store.save(sample_workflow, change_summary="v1")
        sample_workflow.steps.append(
            Step(id="s4", type=StepType.SORT, label="Sort", params={"column": "id"})
        )
        workflow_store.save(sample_workflow, change_summary="v2")
        diff = workflow_store.diff("test-wf-001", 1, 2)
        assert diff is not None
        assert "s4" in diff["added_steps"]

    def test_diff_removed_step(self, workflow_store, sample_workflow):
        workflow_store.save(sample_workflow)
        sample_workflow.steps = sample_workflow.steps[:2]
        workflow_store.save(sample_workflow)
        diff = workflow_store.diff("test-wf-001", 1, 2)
        assert "s3" in diff["removed_steps"]

    def test_diff_modified_step(self, workflow_store, sample_workflow):
        workflow_store.save(sample_workflow)
        sample_workflow.steps[1].params["condition"] = "status = 'closed'"
        workflow_store.save(sample_workflow)
        diff = workflow_store.diff("test-wf-001", 1, 2)
        assert "s2" in diff["modified_steps"]

    def test_diff_nonexistent_workflow(self, workflow_store):
        assert workflow_store.diff("nope", 1, 2) is None

    def test_diff_invalid_versions(self, workflow_store, sample_workflow):
        workflow_store.save(sample_workflow)
        assert workflow_store.diff("test-wf-001", 1, 99) is None


class TestWorkflowStoreStatus:
    def test_update_status(self, workflow_store, sample_workflow):
        workflow_store.save(sample_workflow)
        v = workflow_store.update_status("test-wf-001", PipelineStatus.PUBLISHED)
        assert v.workflow.status == PipelineStatus.PUBLISHED
        assert v.workflow.published_at is not None

    def test_update_status_with_test_results(self, workflow_store, sample_workflow):
        workflow_store.save(sample_workflow)
        results = {"passed": 3, "failed": 0}
        v = workflow_store.update_status("test-wf-001", PipelineStatus.TESTING, test_results=results)
        assert v.workflow.test_results == results

    def test_update_status_published_by(self, workflow_store, sample_workflow):
        workflow_store.save(sample_workflow)
        v = workflow_store.update_status("test-wf-001", PipelineStatus.PUBLISHED, published_by="admin")
        assert v.workflow.published_by == "admin"

    def test_update_status_nonexistent(self, workflow_store):
        assert workflow_store.update_status("nope", PipelineStatus.DRAFT) is None

    def test_status_lifecycle(self, workflow_store, sample_workflow):
        """Test full lifecycle: draft → testing → published → archived."""
        workflow_store.save(sample_workflow)
        workflow_store.update_status("test-wf-001", PipelineStatus.TESTING)
        workflow_store.update_status("test-wf-001", PipelineStatus.PUBLISHED)
        workflow_store.update_status("test-wf-001", PipelineStatus.ARCHIVED)
        v = workflow_store.get("test-wf-001")
        assert v.workflow.status == PipelineStatus.ARCHIVED


class TestStoreRejectsPlaceholderName:
    """Locked 2026-05-09: the workflows list never silently collects
    'Untitled Pipeline' rows. The store.save() chokepoint enforces this
    for every backend path (API create, template import, agent
    apply_pipeline_draft, programmatic save) — frontend guards are
    defense-in-depth only."""

    def test_save_rejects_untitled_pipeline(self, workflow_store):
        wf = Workflow(id="wf-x", name="Untitled Pipeline")
        with pytest.raises(ValueError, match="placeholder"):
            workflow_store.save(wf)

    def test_save_rejects_untitled_pipeline_case_insensitive(self, workflow_store):
        wf = Workflow(id="wf-x", name="UNTITLED pipeline")
        with pytest.raises(ValueError, match="placeholder"):
            workflow_store.save(wf)

    def test_save_rejects_whitespace_only_name(self, workflow_store):
        wf = Workflow(id="wf-x", name="   ")
        with pytest.raises(ValueError, match="required"):
            workflow_store.save(wf)

    def test_save_accepts_real_name(self, workflow_store):
        wf = Workflow(id="wf-x", name="Customer Sync")
        v = workflow_store.save(wf)
        assert v.version == 1
        assert v.workflow.name == "Customer Sync"

    def test_update_does_not_re_check_name(self, workflow_store, sample_workflow):
        """The name rule fires on first save only. Subsequent updates can
        re-save the same row (which already passed the gate) without
        being re-validated — otherwise legitimate edits would fail."""
        workflow_store.save(sample_workflow)
        # Direct rename via the model — store re-saves at version 2, no
        # name check applied (the gate is `if version_num == 1`).
        sample_workflow.name = "Renamed"
        v2 = workflow_store.save(sample_workflow)
        assert v2.version == 2
        assert v2.workflow.name == "Renamed"
