"""Updates, immutable historical versions, and structured plan differences."""
import pytest
from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client,
)
from tests.test_workflow_steps_persist import create_workflow, read_workflow


@pytest.mark.parametrize("mutation", ["rename", "add", "remove", "params"])
def test_update_persists_and_preserves_old_version(authed_client, mutation):
    wid = create_workflow(authed_client)
    original = read_workflow(authed_client, wid)
    changed = read_workflow(authed_client, wid)
    if mutation == "rename":
        changed["name"] = "Renamed workflow"
    elif mutation == "add":
        changed["steps"].append({"id": "sort", "type": "sort", "params": {"columns": ["region"]}})
        changed["connections"].append({"from_step": "agg", "to_step": "sort"})
    elif mutation == "remove":
        changed["steps"] = changed["steps"][:1]
        changed["connections"] = []
    else:
        changed["steps"][1]["params"]["condition"] = "amount > 500"
    response = authed_client.put(f"/api/workflows/{wid}", json={
        "workflow": changed, "change_summary": mutation})
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 2
    actual = read_workflow(authed_client, wid)
    for field in ("name", "steps", "connections"):
        # Newly inserted steps and edges acquire model defaults.
        if field == "name" or mutation != "add":
            assert actual[field] == changed[field]
    if mutation == "add":
        assert actual["steps"][-1]["id"] == "sort"
        assert actual["steps"][-1]["params"] == {"columns": ["region"]}
        assert actual["connections"][-1]["to_step"] == "sort"
    assert read_workflow(authed_client, wid, version=1) == original
    versions = authed_client.get(f"/api/workflows/{wid}/versions")
    assert versions.status_code == 200, versions.text
    assert {v["version"] for v in versions.json()} == {1, 2}


def test_diff_shows_removed_step(authed_client):
    wid = create_workflow(authed_client)
    changed = read_workflow(authed_client, wid)
    changed["steps"] = changed["steps"][:1]
    changed["connections"] = []
    saved = authed_client.put(f"/api/workflows/{wid}", json={"workflow": changed})
    assert saved.status_code == 200, saved.text
    diff = authed_client.get(f"/api/workflows/{wid}/diff", params={"v1": 1, "v2": 2})
    assert diff.status_code == 200, diff.text
    assert set(diff.json()["removed_steps"]) == {"flt", "agg"}


def test_missing_historical_version_returns_404(authed_client):
    wid = create_workflow(authed_client)
    response = authed_client.get(f"/api/workflows/{wid}", params={"version": 999})
    assert response.status_code == 404, response.text
