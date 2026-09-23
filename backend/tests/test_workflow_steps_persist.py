"""Workflow persistence against the current versioned API contract."""
from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client,
)

SAMPLE_STEPS = [
    {"id": "src", "type": "source", "params": {
        "connector_type": "csv", "file_path": "orders.csv"}},
    {"id": "flt", "type": "filter", "params": {"condition": "region = 'US'"}},
    {"id": "agg", "type": "aggregate", "params": {
        "group_by": "region", "functions": {"amount": "sum"}}},
]
SAMPLE_CONNECTIONS = [
    {"from_step": "src", "to_step": "flt"},
    {"from_step": "flt", "to_step": "agg"},
]


def create_workflow(client, steps=None):
    response = client.post("/api/workflows", json={
        "name": "Persistence regression",
        "steps": SAMPLE_STEPS if steps is None else steps,
        "connections": SAMPLE_CONNECTIONS if steps is None else [],
    })
    assert response.status_code == 200, response.text
    return response.json()["id"]


def read_workflow(client, workflow_id, version=None):
    response = client.get(f"/api/workflows/{workflow_id}",
                          params={} if version is None else {"version": version})
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body["version"], int)
    return body["workflow"]


def test_workflow_create_persists_steps(authed_client):
    wid = create_workflow(authed_client)
    assert len(read_workflow(authed_client, wid)["steps"]) == 3


def test_workflow_step_contents_preserved(authed_client):
    wid = create_workflow(authed_client)
    workflow = read_workflow(authed_client, wid)
    actual = {step["id"]: step for step in workflow["steps"]}
    for expected in SAMPLE_STEPS:
        assert actual[expected["id"]]["type"] == expected["type"]
        assert actual[expected["id"]]["params"] == expected["params"]
    assert {(edge["from_step"], edge["to_step"]) for edge in workflow["connections"]} == {
        ("src", "flt"), ("flt", "agg")}


def test_empty_steps_list_also_persists(authed_client):
    wid = create_workflow(authed_client, steps=[])
    assert read_workflow(authed_client, wid)["steps"] == []


def test_update_name_does_not_wipe_steps(authed_client):
    wid = create_workflow(authed_client)
    workflow = read_workflow(authed_client, wid)
    workflow["name"] = "Renamed"
    response = authed_client.put(f"/api/workflows/{wid}", json={"workflow": workflow})
    assert response.status_code == 200, response.text
    actual = read_workflow(authed_client, wid)
    assert actual["name"] == "Renamed"
    assert actual["steps"] == workflow["steps"]
    assert actual["connections"] == workflow["connections"]


def test_partial_put_rejected_without_data_loss(authed_client):
    wid = create_workflow(authed_client)
    before = read_workflow(authed_client, wid)
    response = authed_client.put(f"/api/workflows/{wid}", json={"name": "Incomplete"})
    assert response.status_code == 400, response.text
    assert read_workflow(authed_client, wid) == before


def test_add_step_via_update_creates_v2(authed_client):
    wid = create_workflow(authed_client)
    workflow = read_workflow(authed_client, wid)
    workflow["steps"].append({"id": "sort", "type": "sort", "params": {"columns": ["region"]}})
    workflow["connections"].append({"from_step": "agg", "to_step": "sort"})
    response = authed_client.put(f"/api/workflows/{wid}", json={"workflow": workflow})
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 2
    assert len(read_workflow(authed_client, wid)["steps"]) == 4
    assert len(read_workflow(authed_client, wid, version=1)["steps"]) == 3


def test_workflow_with_steps_executes(authed_client):
    wid = create_workflow(authed_client)
    response = authed_client.post(f"/api/execute/workflow/{wid}")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "success", result
    assert result["step_results"]["src"]["row_count"] == 5
    assert result["step_results"]["flt"]["row_count"] == 2
    assert result["step_results"]["agg"]["row_count"] == 1
