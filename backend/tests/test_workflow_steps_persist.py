"""
Regression test for A6 — POST /api/workflows silently drops `steps`.

Before Week 1 Track E: `test_get_workflow` showed len(steps)==0 after
creating with 3 steps. This cascaded to 8 e2e executor failures.

After Week 1 Track E: steps roundtrip cleanly through create / get /
update / version diff.
"""
from __future__ import annotations

import pytest
from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client,
)


SAMPLE_STEPS = [
    {
        "id": "src",
        "type": "csv_source",
        "config": {"file_path": "orders.csv"},
    },
    {
        "id": "flt",
        "type": "filter",
        "config": {"condition": "region = 'US'"},
        "depends_on": ["src"],
    },
    {
        "id": "agg",
        "type": "aggregate",
        "config": {"group_by": ["region"], "aggregations": [
            {"column": "amount", "op": "sum"},
        ]},
        "depends_on": ["flt"],
    },
]


# ─────────────────────────────────────────────────────────────────────────
# Roundtrip: create → get → steps preserved
# ─────────────────────────────────────────────────────────────────────────

def test_workflow_create_persists_steps(authed_client):
    r = authed_client.post("/api/workflows", json={
        "name": "persist-test",
        "steps": SAMPLE_STEPS,
    })
    assert r.status_code in (200, 201), f"create failed: {r.status_code} {r.text[:200]}"
    wf = r.json()
    wf_id = wf.get("id") or wf.get("workflow_id")
    assert wf_id, f"create response missing id: {wf}"

    got = authed_client.get(f"/api/workflows/{wf_id}").json()
    assert "steps" in got, "GET response missing 'steps' field"
    assert len(got["steps"]) == 3, (
        f"A6 REGRESSION — posted 3 steps, got {len(got['steps'])} back. "
        f"Response: {got}"
    )


def test_workflow_step_contents_preserved(authed_client):
    r = authed_client.post("/api/workflows", json={
        "name": "content-test",
        "steps": SAMPLE_STEPS,
    })
    wf_id = r.json()["id"]
    got = authed_client.get(f"/api/workflows/{wf_id}").json()

    # Verify each field of each step survived the roundtrip
    by_id = {s["id"]: s for s in got["steps"]}
    assert "src" in by_id and "flt" in by_id and "agg" in by_id

    assert by_id["src"]["type"] == "csv_source"
    assert by_id["src"]["config"]["file_path"] == "orders.csv"

    assert by_id["flt"]["config"]["condition"] == "region = 'US'"
    assert "src" in by_id["flt"]["depends_on"]

    agg_cfg = by_id["agg"]["config"]
    assert agg_cfg["group_by"] == ["region"]
    assert agg_cfg["aggregations"][0]["op"] == "sum"


def test_empty_steps_list_also_persists(authed_client):
    """Edge case: some workflows genuinely start empty. Don't coerce to None."""
    r = authed_client.post("/api/workflows", json={
        "name": "empty-test",
        "steps": [],
    })
    wf_id = r.json()["id"]
    got = authed_client.get(f"/api/workflows/{wf_id}").json()
    assert got["steps"] == []


# ─────────────────────────────────────────────────────────────────────────
# Update preserves steps unless changed (A8 regression too)
# ─────────────────────────────────────────────────────────────────────────

def test_update_name_does_not_wipe_steps(authed_client):
    r = authed_client.post("/api/workflows", json={
        "name": "update-test", "steps": SAMPLE_STEPS,
    })
    wf_id = r.json()["id"]

    # Update only the name
    ur = authed_client.put(f"/api/workflows/{wf_id}", json={"name": "renamed"})
    if ur.status_code == 422:
        # Server may require full payload; try with steps
        ur = authed_client.put(f"/api/workflows/{wf_id}", json={
            "name": "renamed", "steps": SAMPLE_STEPS,
        })
    assert ur.status_code in (200, 201), f"update failed: {ur.status_code}"

    got = authed_client.get(f"/api/workflows/{wf_id}").json()
    assert got["name"] == "renamed"
    assert len(got["steps"]) == 3, (
        f"Update dropped steps. After rename, len(steps)={len(got['steps'])}"
    )


def test_add_step_via_update_creates_v2(authed_client):
    r = authed_client.post("/api/workflows", json={
        "name": "v2-test", "steps": SAMPLE_STEPS,
    })
    wf_id = r.json()["id"]

    new_steps = SAMPLE_STEPS + [{
        "id": "out",
        "type": "csv_sink",
        "config": {"file_path": "out.csv"},
        "depends_on": ["agg"],
    }]
    ur = authed_client.put(f"/api/workflows/{wf_id}", json={
        "name": "v2-test", "steps": new_steps,
    })
    assert ur.status_code in (200, 201), f"update failed: {ur.status_code}"

    got = authed_client.get(f"/api/workflows/{wf_id}").json()
    assert len(got["steps"]) == 4, "added step not persisted (A8 regression)"

    # Check versioning
    versions = authed_client.get(f"/api/workflows/{wf_id}/versions")
    if versions.status_code == 200:
        v_list = versions.json()
        if isinstance(v_list, list):
            assert len(v_list) >= 2, f"expected ≥2 versions, got {len(v_list)}"


# ─────────────────────────────────────────────────────────────────────────
# Execution prerequisites: the real blast radius of A6
# ─────────────────────────────────────────────────────────────────────────

def test_workflow_with_steps_executes(authed_client):
    """
    This is the test that was cascading 8 failures before A6 was fixed.
    If steps are dropped, the executor has nothing to run → status=error.
    """
    r = authed_client.post("/api/workflows", json={
        "name": "exec-test", "steps": SAMPLE_STEPS,
    })
    wf_id = r.json()["id"]

    exec_r = authed_client.post(f"/api/execute/workflow/{wf_id}")
    assert exec_r.status_code in (200, 201, 202), (
        f"execute endpoint returned {exec_r.status_code}"
    )
    result = exec_r.json()
    # Accept: 'success' (executed fine) OR 'error' with a NON-empty-steps reason
    status = result.get("status")
    if status == "error":
        err = str(result.get("error", "")).lower()
        assert "no steps" not in err and "empty" not in err, (
            f"A6 REGRESSION — executor saw empty steps: {result}"
        )
