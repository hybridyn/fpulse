"""
Regression test for A8 — update-workflow doesn't persist changes.

Before Week 2 Day 2: PUT /api/workflows/{id} returns 422 or returns stale data.
After Week 2 Day 2: update persists, creates v2, diff shows changes.
"""
from __future__ import annotations

import pytest
from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client,
)


@pytest.fixture
def initial_workflow(authed_client):
    r = authed_client.post("/api/workflows", json={
        "name": "update-regression-test",
        "steps": [
            {"id": "src", "type": "csv_source",
             "config": {"file_path": "orders.csv"}},
            {"id": "flt", "type": "filter",
             "config": {"condition": "amount > 100"},
             "depends_on": ["src"]},
        ],
    })
    if r.status_code not in (200, 201):
        pytest.skip(f"workflow create failed: {r.status_code}")
    body = r.json()
    return body.get("id") or body.get("workflow_id")


class TestUpdatePersistence:

    def test_rename_persists(self, authed_client, initial_workflow):
        ur = authed_client.put(f"/api/workflows/{initial_workflow}", json={
            "name": "renamed-workflow",
            "steps": [
                {"id": "src", "type": "csv_source",
                 "config": {"file_path": "orders.csv"}},
                {"id": "flt", "type": "filter",
                 "config": {"condition": "amount > 100"},
                 "depends_on": ["src"]},
            ],
        })
        assert ur.status_code in (200, 201), (
            f"A8 REGRESSION — update returned {ur.status_code}: {ur.text[:200]}"
        )

        got = authed_client.get(f"/api/workflows/{initial_workflow}").json()
        assert got["name"] == "renamed-workflow", (
            f"Update did not persist rename. Got name={got.get('name')!r}"
        )

    def test_add_step_persists(self, authed_client, initial_workflow):
        new_steps = [
            {"id": "src", "type": "csv_source",
             "config": {"file_path": "orders.csv"}},
            {"id": "flt", "type": "filter",
             "config": {"condition": "amount > 100"},
             "depends_on": ["src"]},
            {"id": "sort", "type": "sort",
             "config": {"columns": ["amount"]},
             "depends_on": ["flt"]},
        ]
        ur = authed_client.put(f"/api/workflows/{initial_workflow}", json={
            "name": "update-regression-test",
            "steps": new_steps,
        })
        assert ur.status_code in (200, 201)

        got = authed_client.get(f"/api/workflows/{initial_workflow}").json()
        assert len(got["steps"]) == 3, (
            f"Added step not persisted. len(steps)={len(got['steps'])}"
        )
        step_ids = {s["id"] for s in got["steps"]}
        assert step_ids == {"src", "flt", "sort"}

    def test_remove_step_persists(self, authed_client, initial_workflow):
        ur = authed_client.put(f"/api/workflows/{initial_workflow}", json={
            "name": "update-regression-test",
            "steps": [
                {"id": "src", "type": "csv_source",
                 "config": {"file_path": "orders.csv"}},
            ],  # dropped the filter
        })
        assert ur.status_code in (200, 201)

        got = authed_client.get(f"/api/workflows/{initial_workflow}").json()
        assert len(got["steps"]) == 1
        assert got["steps"][0]["id"] == "src"

    def test_modify_step_config_persists(self, authed_client, initial_workflow):
        ur = authed_client.put(f"/api/workflows/{initial_workflow}", json={
            "name": "update-regression-test",
            "steps": [
                {"id": "src", "type": "csv_source",
                 "config": {"file_path": "orders.csv"}},
                {"id": "flt", "type": "filter",
                 "config": {"condition": "amount > 500"},   # changed
                 "depends_on": ["src"]},
            ],
        })
        assert ur.status_code in (200, 201)

        got = authed_client.get(f"/api/workflows/{initial_workflow}").json()
        flt = next(s for s in got["steps"] if s["id"] == "flt")
        assert "500" in flt["config"]["condition"], (
            f"Filter condition not updated: {flt['config']}"
        )


class TestVersioning:

    def test_update_creates_new_version(self, authed_client, initial_workflow):
        authed_client.put(f"/api/workflows/{initial_workflow}", json={
            "name": "v2-name",
            "steps": [
                {"id": "src", "type": "csv_source",
                 "config": {"file_path": "orders.csv"}},
                {"id": "flt", "type": "filter",
                 "config": {"condition": "amount > 200"},
                 "depends_on": ["src"]},
            ],
        })

        versions = authed_client.get(f"/api/workflows/{initial_workflow}/versions")
        if versions.status_code != 200:
            pytest.skip("versions endpoint not available")

        v_list = versions.json()
        if isinstance(v_list, dict):
            v_list = v_list.get("versions", [])
        assert len(v_list) >= 2, f"expected ≥2 versions, got {len(v_list)}"


class TestDiffEndpoint:

    def test_diff_endpoint_returns_200(self, authed_client, initial_workflow):
        """A10 regression — /api/workflows/{id}/diff was returning 404."""
        # Create v2
        authed_client.put(f"/api/workflows/{initial_workflow}", json={
            "name": "update-regression-test",
            "steps": [
                {"id": "src", "type": "csv_source",
                 "config": {"file_path": "orders.csv"}},
            ],
        })

        diff = authed_client.get(f"/api/workflows/{initial_workflow}/diff?v1=1&v2=2")
        if diff.status_code == 404:
            pytest.fail(
                "A10 REGRESSION — /api/workflows/{id}/diff returns 404. "
                "Endpoint must exist after Week 2 Day 2."
            )
        assert diff.status_code == 200

    def test_diff_shows_removed_step(self, authed_client, initial_workflow):
        authed_client.put(f"/api/workflows/{initial_workflow}", json={
            "name": "update-regression-test",
            "steps": [
                {"id": "src", "type": "csv_source",
                 "config": {"file_path": "orders.csv"}},
            ],  # removed filter
        })

        diff = authed_client.get(f"/api/workflows/{initial_workflow}/diff?v1=1&v2=2")
        if diff.status_code != 200:
            pytest.skip("diff endpoint not returning 200")

        body = diff.json()
        text = str(body).lower()
        assert "flt" in text or "filter" in text or "removed" in text, (
            f"Diff doesn't mention the removed step: {body}"
        )
