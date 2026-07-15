"""
F-Pulse v1.0.0 — Complete End-to-End Test Suite

Tests every feature area across the full API surface:
  1. Health & System
  2. Projects CRUD + lifecycle
  3. Workflows CRUD + versioning + diff
  4. Pipeline Execution (full, single-step, pre-validation)
  5. Template Gallery (list, categories, use, import, export)
  6. Scheduling (CRUD, toggle, run-now)
  7. Alerts (CRUD, test, logs)
  8. Monitor (executions, stats, multi-stats, failed)
  9. Credentials (CRUD, masking, test)
  10. Connections (types, CRUD, test, reports)
  11. Variables (CRUD, resolve, scoping)
  12. Auth (register, login, session, logout, invite)
  13. Schema Contracts (create, validate, drift)
  14. Intelligence (detect-schema, flatten, suggest)
  15. Logs (list, detail, events, compare, stats)
  16. Backup (create, list, restore)
  17. AI (status, suggest, diagnose)
  18. Planner (generate, templates, chat)
  19. File Upload & Management
  20. WebSocket Info
  21. Cross-Feature Integration (project→pipeline→execute→monitor→alert)
  22. Edge Cases & Error Handling
  23. Concurrent Operations
  24. Data Integrity Checks

Run: pytest tests/test_e2e_complete.py -v
"""

import os
import json
import time
import tempfile
import uuid
import pytest
from fastapi.testclient import TestClient

# Module-level marker (2026-05-22) — this file covers 24 feature areas
# through full lifespan TestClient cycles. Tagged ``e2e`` so the fast
# CI gate (`pytest -m "not stress and not external"`) still includes
# it but a developer wanting only unit tests can `pytest -m unit`.
pytestmark = pytest.mark.e2e


# ═══════════════════════════════════════════
# Test Fixtures
# ═══════════════════════════════════════════

@pytest.fixture(scope="module")
def client():
    """Create a fresh TestClient with isolated data directory."""
    tmpdir = tempfile.mkdtemp()
    os.environ["FPULSE_DATA_DIR"] = tmpdir

    # Create test data files
    with open(os.path.join(tmpdir, "orders.csv"), "w") as f:
        f.write("id,product,quantity,price,region,status\n")
        f.write("1,Widget A,10,29.99,North,active\n")
        f.write("2,Widget B,5,49.99,South,active\n")
        f.write("3,Widget C,3,99.99,East,inactive\n")
        f.write("4,Widget D,8,19.99,West,active\n")
        f.write("5,Widget E,15,39.99,North,active\n")
        f.write("6,Widget F,2,79.99,South,inactive\n")

    with open(os.path.join(tmpdir, "customers.csv"), "w") as f:
        f.write("id,name,email,region,tier,active\n")
        f.write("1,Alice,alice@test.com,North,gold,true\n")
        f.write("2,Bob,bob@test.com,South,silver,true\n")
        f.write("3,Charlie,charlie@test.com,East,bronze,false\n")
        f.write("4,Diana,diana@test.com,West,gold,true\n")
        f.write("5,Eve,eve@test.com,North,silver,true\n")

    with open(os.path.join(tmpdir, "events.json"), "w") as f:
        events = [
            {"id": 1, "type": "click", "user": "alice", "page": "/home", "ts": "2026-01-01T10:00:00"},
            {"id": 2, "type": "purchase", "user": "bob", "page": "/checkout", "ts": "2026-01-01T11:00:00"},
            {"id": 3, "type": "click", "user": "charlie", "page": "/products", "ts": "2026-01-01T12:00:00"},
        ]
        json.dump(events, f)

    with open(os.path.join(tmpdir, "products.csv"), "w") as f:
        f.write("sku,name,category,price,stock\n")
        f.write("A001,Widget A,Electronics,29.99,100\n")
        f.write("B002,Widget B,Tools,49.99,50\n")
        f.write("C003,Widget C,Electronics,99.99,25\n")

    from fpulse.main import app, app_state
    from fpulse.auth.models import User
    # base_url must be a LOOPBACK host: the LocalOriginGuardMiddleware
    # (DNS-rebinding defense, fpulse/api/local_hardening.py) rejects any
    # Host header not in {localhost,127.0.0.1,::1} when loopback-bound
    # (the default). TestClient's default Host is "testserver", which the
    # middleware correctly 403s — so the client must present "localhost",
    # exactly as a real browser does. (Without this every API call 403s
    # at login and the whole e2e lifecycle errors at setup.)
    with TestClient(app, base_url="http://localhost") as c:
        # The seeded admin now boots with a random password
        # (secrets.token_urlsafe(18)). Rotate it to a known strong value
        # that satisfies the password policy, then log in once and attach
        # the bearer token to every subsequent request from this client.
        # Without this, every endpoint behind require_auth/require_admin
        # 401s and the cascading state[*] keys never get populated.
        user_store = app_state["user_store"]
        admin = user_store.get_user("admin")
        admin.password_hash = User.hash_password(ADMIN_TEST_PASSWORD)
        user_store._save_user(admin)

        login = c.post("/api/auth/login", json={
            "email": "admin@fpulse.local",
            "password": ADMIN_TEST_PASSWORD,
        })
        assert login.status_code == 200, f"test admin login failed: {login.text}"
        token = login.json()["token"]
        c.headers.update({"Authorization": f"Bearer {token}"})
        yield c


# Strong-password value used to rotate the seeded admin in the client
# fixture and asserted against from TestAuth. Has lower + upper + digit +
# symbol and is 16 chars — comfortably above the 12-char floor.
ADMIN_TEST_PASSWORD = "Bootstrap!Pass26"


@pytest.fixture(scope="module")
def state():
    """Shared state across tests in this module."""
    return {}


# ═══════════════════════════════════════════
# 1. Health & System
# ═══════════════════════════════════════════

class TestHealthAndSystem:
    """System health, node types, and service readiness."""

    def test_health_endpoint(self, client):
        # /api/health is the LIVENESS probe — by design returns only the
        # lightweight `status / version / product / mode` shape and MUST NOT
        # touch the database or any external system (see main.py docstring).
        # The earlier assertions on `projects` and `scheduler` belonged on
        # the readiness probe; they're moved to test_health_readiness below.
        r = client.get("/api/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert "version" in data
        assert "product" in data
        assert "mode" in data

    def test_health_version_is_1_0_0(self, client):
        r = client.get("/api/health")
        assert r.json()["version"] == "1.0.0"

    def test_health_readiness_endpoint(self, client):
        # /api/health/ready is the READINESS probe — returns the rich shape
        # with database / scheduler / project-count details. This is the
        # right endpoint for assertions about workspace-state availability.
        r = client.get("/api/health/ready")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert "projects" in data
        assert "scheduler" in data
        assert "persistence" in data
        assert "runtime" in data

    def test_scheduler_status(self, client):
        r = client.get("/api/scheduler/status")
        assert r.status_code == 200
        data = r.json()
        assert "running" in data
        assert "total_schedules" in data

    def test_node_types_complete(self, client):
        r = client.get("/api/node-types")
        assert r.status_code == 200
        types = r.json()
        assert isinstance(types, list)
        assert len(types) >= 50  # v1.0.0 has 56 node types
        # Check structure
        for t in types:
            assert "type" in t
            assert "label" in t
        # Verify key node types exist
        type_ids = {t["type"] for t in types}
        assert "csv_source" in type_ids
        assert "filter" in type_ids
        assert "aggregate" in type_ids
        assert "db_source" in type_ids
        assert "db_sink" in type_ids
        assert "transform" in type_ids
        assert "join" in type_ids
        # Output is represented by concrete sink types (db_sink asserted
        # above); there is no generic "output" registered type, so the
        # old `assert "output" in type_ids` was checking a non-existent
        # type. db_sink already proves an output node is registered.

    def test_list_data_files(self, client):
        r = client.get("/api/files")
        assert r.status_code == 200
        files = r.json()
        filenames = [f["name"] for f in files]
        assert "orders.csv" in filenames
        assert "customers.csv" in filenames
        assert "events.json" in filenames


# ═══════════════════════════════════════════
# 2. Projects CRUD
# ═══════════════════════════════════════════

class TestProjectsCRUD:
    """Full project lifecycle: create, read, update, list, delete."""

    def test_default_project_exists(self, client):
        r = client.get("/api/projects/")
        assert r.status_code == 200
        projects = r.json()
        assert any(p["id"] == "default" for p in projects)

    def test_create_project(self, client, state):
        r = client.post("/api/projects/", json={
            "name": "E2E Analytics",
            "description": "End-to-end test project",
            "color": "#3b82f6",
            "icon": "chart-bar",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "E2E Analytics"
        assert "id" in data
        assert data["color"] == "#3b82f6"
        state["project_id"] = data["id"]

    def test_get_project(self, client, state):
        r = client.get(f"/api/projects/{state['project_id']}")
        assert r.status_code == 200
        assert r.json()["name"] == "E2E Analytics"

    def test_update_project(self, client, state):
        r = client.put(f"/api/projects/{state['project_id']}", json={
            "name": "E2E Analytics v2",
            "description": "Updated description",
        })
        assert r.status_code == 200
        assert r.json()["name"] == "E2E Analytics v2"

    def test_list_projects_includes_new(self, client, state):
        r = client.get("/api/projects/")
        assert r.status_code == 200
        projects = r.json()
        assert any(p["id"] == state["project_id"] for p in projects)

    def test_cannot_delete_default_project(self, client):
        # The default project is protected — deleting it would cascade
        # into broken alert/schedule/workflow creation (everything that
        # defaults to project_id="default"). The API refuses with 409.
        r = client.delete("/api/projects/default")
        assert r.status_code in (400, 403, 409)

    def test_project_not_found(self, client):
        r = client.get("/api/projects/nonexistent-id-xyz")
        assert r.status_code == 404

    def test_create_and_delete_project(self, client):
        cr = client.post("/api/projects/", json={"name": "Temp Project"})
        assert cr.status_code == 200
        pid = cr.json()["id"]
        r = client.delete(f"/api/projects/{pid}")
        assert r.status_code == 200
        # Verify deleted
        r2 = client.get(f"/api/projects/{pid}")
        assert r2.status_code == 404


# ═══════════════════════════════════════════
# 3. Workflows CRUD + Versioning
# ═══════════════════════════════════════════

class TestWorkflowsCRUD:
    """Workflow create, read, update, delete, versioning, and diff."""

    def test_create_simple_workflow(self, client, state):
        r = client.post("/api/workflows/", json={
            "name": "Sales Report Pipeline",
            "description": "Load sales, filter, aggregate",
            "project_id": state.get("project_id", "default"),
            "steps": [
                {"id": "load", "type": "csv_source", "label": "Load Orders",
                 "params": {"file_path": "orders.csv"},
                 "position": {"x": 100, "y": 100}},
                {"id": "filter", "type": "filter", "label": "Active Only",
                 "params": {"condition": "status = 'active'"},
                 "position": {"x": 400, "y": 100}},
                {"id": "agg", "type": "aggregate", "label": "Revenue by Region",
                 "params": {"group_by": "region", "functions": {"price": "sum"}},
                 "position": {"x": 700, "y": 100}},
            ],
            "connections": [
                {"from_step": "load", "to_step": "filter"},
                {"from_step": "filter", "to_step": "agg"},
            ],
        })
        assert r.status_code == 200
        data = r.json()
        assert "id" in data
        assert data["version"] == 1
        state["workflow_id"] = data["id"]

    def test_get_workflow(self, client, state):
        r = client.get(f"/api/workflows/{state['workflow_id']}")
        assert r.status_code == 200
        data = r.json()
        wf = data["workflow"]
        assert wf["name"] == "Sales Report Pipeline"
        assert len(wf["steps"]) == 3
        assert len(wf["connections"]) == 2

    def test_list_workflows(self, client):
        r = client.get("/api/workflows/")
        assert r.status_code == 200
        workflows = r.json()
        assert isinstance(workflows, list)
        assert len(workflows) >= 1

    def test_update_workflow_creates_v2(self, client, state):
        r = client.put(f"/api/workflows/{state['workflow_id']}", json={
            "name": "Sales Report v2",
            "steps": [
                {"id": "load", "type": "csv_source", "label": "Load Orders",
                 "params": {"file_path": "orders.csv"}},
                {"id": "filter", "type": "filter", "label": "Active Only",
                 "params": {"condition": "status = 'active'"}},
                {"id": "agg", "type": "aggregate", "label": "Revenue by Region",
                 "params": {"group_by": "region", "functions": {"price": "sum"}}},
                {"id": "sort", "type": "sort", "label": "Sort by Revenue",
                 "params": {"column": "price", "order": "desc"}},
            ],
            "connections": [
                {"from_step": "load", "to_step": "filter"},
                {"from_step": "filter", "to_step": "agg"},
                {"from_step": "agg", "to_step": "sort"},
            ],
            "change_summary": "Added sort step",
        })
        assert r.status_code == 200
        assert r.json()["version"] == 2

    def test_get_version_history(self, client, state):
        r = client.get(f"/api/workflows/{state['workflow_id']}/versions")
        assert r.status_code == 200
        versions = r.json()
        assert len(versions) == 2
        assert versions[0]["version"] == 1
        assert versions[1]["version"] == 2

    def test_diff_versions(self, client, state):
        r = client.get(f"/api/workflows/{state['workflow_id']}/diff?v1=1&v2=2")
        assert r.status_code == 200
        diff = r.json()
        assert "sort" in diff.get("added_steps", [])

    def test_get_specific_version(self, client, state):
        r = client.get(f"/api/workflows/{state['workflow_id']}?version=1")
        assert r.status_code == 200
        wf = r.json()["workflow"]
        assert len(wf["steps"]) == 3  # v1 had 3 steps

    def test_create_empty_workflow(self, client):
        r = client.post("/api/workflows/", json={
            "name": "Empty Pipeline",
            "steps": [],
            "connections": [],
        })
        assert r.status_code == 200

    def test_delete_workflow(self, client):
        cr = client.post("/api/workflows/", json={
            "name": "Deletable",
            "steps": [],
            "connections": [],
        })
        wid = cr.json()["id"]
        r = client.delete(f"/api/workflows/{wid}")
        assert r.status_code == 200
        # Verify gone
        r2 = client.get(f"/api/workflows/{wid}")
        assert r2.status_code == 404

    def test_workflow_not_found(self, client):
        r = client.get("/api/workflows/nonexistent-wf-id")
        assert r.status_code == 404


# ═══════════════════════════════════════════
# 4. Pipeline Execution
# ═══════════════════════════════════════════

class TestPipelineExecution:
    """Execute workflows fully, per-step, and with pre-validation."""

    def test_execute_full_pipeline(self, client, state):
        r = client.post(f"/api/execute/workflow/{state['workflow_id']}")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "success"
        assert "step_results" in data
        # CSV source should load 6 rows
        load_result = data["step_results"].get("load", {})
        assert load_result.get("status") == "success"
        assert load_result.get("row_count") == 6
        state["execution_result"] = data

    def test_execute_single_step(self, client, state):
        r = client.post(f"/api/execute/workflow/{state['workflow_id']}/step/filter")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "success"
        # Filter active: 4 of 6 rows (Widget C and F are inactive)
        assert data["row_count"] == 4

    def test_pre_validate_pipeline(self, client, state):
        r = client.post(f"/api/execute/workflow/{state['workflow_id']}/pre-validate")
        assert r.status_code == 200
        data = r.json()
        assert "can_execute" in data

    def test_execute_nonexistent_workflow(self, client):
        r = client.post("/api/execute/workflow/nonexistent")
        assert r.status_code == 404

    def test_execute_csv_filter_aggregate(self, client):
        """Verify data flows correctly through a multi-step pipeline."""
        cr = client.post("/api/workflows/", json={
            "name": "Data Flow Test",
            "steps": [
                {"id": "src", "type": "csv_source", "label": "Load",
                 "params": {"file_path": "customers.csv"}},
                {"id": "flt", "type": "filter", "label": "Gold Only",
                 "params": {"condition": "tier = 'gold'"}},
            ],
            "connections": [{"from_step": "src", "to_step": "flt"}],
        })
        wid = cr.json()["id"]
        r = client.post(f"/api/execute/workflow/{wid}")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "success"
        # Alice and Diana are gold tier
        assert data["step_results"]["flt"]["row_count"] == 2

    def test_execute_json_source(self, client):
        """Verify JSON source loading works."""
        cr = client.post("/api/workflows/", json={
            "name": "JSON Load Test",
            "steps": [
                {"id": "src", "type": "json_source", "label": "Load Events",
                 "params": {"file_path": "events.json"}},
            ],
            "connections": [],
        })
        wid = cr.json()["id"]
        r = client.post(f"/api/execute/workflow/{wid}")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "success"
        assert data["step_results"]["src"]["row_count"] == 3

    def test_execute_with_transform(self, client):
        """Test SQL transform step."""
        cr = client.post("/api/workflows/", json={
            "name": "Transform Test",
            "steps": [
                {"id": "src", "type": "csv_source", "label": "Load",
                 "params": {"file_path": "orders.csv"}},
                {"id": "tx", "type": "transform", "label": "Enrich",
                 "params": {"expression": "SELECT *, quantity * price AS revenue FROM source_table"}},
            ],
            "connections": [{"from_step": "src", "to_step": "tx"}],
        })
        wid = cr.json()["id"]
        r = client.post(f"/api/execute/workflow/{wid}")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "success"

    def test_execute_with_sort(self, client):
        """Test sort step."""
        cr = client.post("/api/workflows/", json={
            "name": "Sort Test",
            "steps": [
                {"id": "src", "type": "csv_source", "label": "Load",
                 "params": {"file_path": "orders.csv"}},
                {"id": "srt", "type": "sort", "label": "Sort",
                 "params": {"column": "price", "order": "desc"}},
            ],
            "connections": [{"from_step": "src", "to_step": "srt"}],
        })
        wid = cr.json()["id"]
        r = client.post(f"/api/execute/workflow/{wid}")
        assert r.status_code == 200
        assert r.json()["status"] == "success"

    def test_execute_with_deduplicate(self, client):
        """Test deduplication step."""
        cr = client.post("/api/workflows/", json={
            "name": "Dedup Test",
            "steps": [
                {"id": "src", "type": "csv_source", "label": "Load",
                 "params": {"file_path": "orders.csv"}},
                {"id": "dd", "type": "deduplicate", "label": "Dedup",
                 "params": {"columns": ["region"]}},
            ],
            "connections": [{"from_step": "src", "to_step": "dd"}],
        })
        wid = cr.json()["id"]
        r = client.post(f"/api/execute/workflow/{wid}")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "success"
        # 4 unique regions: North, South, East, West
        assert data["step_results"]["dd"]["row_count"] == 4


# ═══════════════════════════════════════════
# 5. Template Gallery
# ═══════════════════════════════════════════

class TestTemplateImportExport:
    """Template import / export — the surviving template endpoints.

    PR 5 (2026-05-17) removed the in-backend template gallery surface:
      - GET /api/templates
      - GET /api/templates/categories
      - GET /api/templates/{id}
      - POST /api/templates/{id}/use

    Those endpoints are gone; the OSS frontend now reads templates
    directly from frontend/src/templates/catalog.ts (a static module).
    The legacy `TestTemplateGallery` tests for the removed endpoints
    have been deleted — they were 8 of the 90 reported pytest failures.

    What remains and is tested here:
      - POST /api/templates/import — accepts a JSON pipeline payload
      - POST /api/templates/export/{workflow_id} — serializes a workflow
      - GET/POST/DELETE /api/templates/user — user-saved-template CRUD
        (covered separately by TestUserTemplates if/when added).
    """

    def test_import_pipeline(self, client):
        r = client.post("/api/templates/import", json={
            "name": "Imported Pipeline",
            "description": "Imported from JSON",
            "steps": [
                {"type": "csv_source", "label": "Load", "params": {"file_path": "orders.csv"},
                 "position": {"x": 100, "y": 100}},
                {"type": "filter", "label": "Filter", "params": {"condition": "price > 30"},
                 "position": {"x": 400, "y": 100}},
            ],
            "connections": [{"from_step": 0, "to_step": 1}],
        })
        assert r.status_code == 200
        data = r.json()
        assert data["workflow"]["name"] == "Imported Pipeline"
        assert len(data["workflow"]["steps"]) == 2

    def test_export_workflow(self, client, state):
        r = client.post(f"/api/templates/export/{state['workflow_id']}")
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "Sales Report v2"
        assert "steps" in data
        assert "connections" in data

    def test_export_not_found(self, client):
        r = client.post("/api/templates/export/nonexistent")
        assert r.status_code == 404

    def test_import_and_reexport_roundtrip(self, client):
        """Import a pipeline, then export it — should produce equivalent structure."""
        original = {
            "name": "Roundtrip Test",
            "description": "Test import/export roundtrip",
            "steps": [
                {"type": "csv_source", "label": "Source", "params": {"file_path": "orders.csv"},
                 "position": {"x": 100, "y": 100}},
                {"type": "output", "label": "Output", "params": {"format": "csv"},
                 "position": {"x": 400, "y": 100}},
            ],
            "connections": [{"from_step": 0, "to_step": 1}],
        }
        imp = client.post("/api/templates/import", json=original)
        assert imp.status_code == 200
        wid = imp.json()["id"]

        exp = client.post(f"/api/templates/export/{wid}")
        assert exp.status_code == 200
        exported = exp.json()
        assert exported["name"] == "Roundtrip Test"
        assert len(exported["steps"]) == 2
        assert len(exported["connections"]) == 1


# ═══════════════════════════════════════════
# 6. Scheduling
# ═══════════════════════════════════════════

class TestScheduling:
    """Schedule CRUD, toggle, and manual trigger."""

    def test_create_schedule(self, client, state):
        r = client.post("/api/schedules/", json={
            "workflow_id": state["workflow_id"],
            "project_id": state.get("project_id", "default"),
            "name": "Daily Report",
            "schedule_type": "daily",
            "daily_time": "09:00",
            "timezone": "UTC",
            "enabled": True,
        })
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "Daily Report"
        state["schedule_id"] = data["id"]

    def test_list_schedules(self, client):
        r = client.get("/api/schedules/")
        assert r.status_code == 200
        assert isinstance(r.json(), list)
        assert len(r.json()) >= 1

    def test_get_schedule(self, client, state):
        r = client.get(f"/api/schedules/{state['schedule_id']}")
        assert r.status_code == 200
        assert r.json()["name"] == "Daily Report"

    def test_toggle_schedule(self, client, state):
        r = client.post(f"/api/schedules/{state['schedule_id']}/toggle")
        assert r.status_code == 200

    def test_update_schedule(self, client, state):
        r = client.put(f"/api/schedules/{state['schedule_id']}", json={
            "name": "Updated Schedule",
            "daily_time": "10:00",
        })
        assert r.status_code == 200

    def test_schedule_not_found(self, client):
        r = client.get("/api/schedules/nonexistent")
        assert r.status_code == 404

    def test_delete_schedule(self, client, state):
        # Schedule creation now validates the workflow exists (404 for a
        # bogus id), so use the real workflow created earlier rather than
        # the old "wf-tmp" placeholder.
        cr = client.post("/api/schedules/", json={
            "workflow_id": state["workflow_id"], "name": "Deletable Schedule",
        })
        sid = cr.json()["id"]
        r = client.delete(f"/api/schedules/{sid}")
        assert r.status_code == 200


# ═══════════════════════════════════════════
# 7. Alerts
# ═══════════════════════════════════════════

class TestAlerts:
    """Alert rules CRUD, test notification, and logs."""

    def test_create_alert_rule(self, client, state):
        r = client.post("/api/alerts/rules", json={
            "name": "Pipeline Failure Alert",
            "workflow_id": state["workflow_id"],
            "project_id": state.get("project_id", "default"),
            "condition": "on_failure",
            "channel": "email",
            "email_addresses": ["ops@company.com"],
        })
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "Pipeline Failure Alert"
        state["alert_rule_id"] = data["id"]

    def test_list_alert_rules(self, client):
        r = client.get("/api/alerts/rules")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_get_alert_rule(self, client, state):
        r = client.get(f"/api/alerts/rules/{state['alert_rule_id']}")
        assert r.status_code == 200

    def test_update_alert_rule(self, client, state):
        r = client.put(f"/api/alerts/rules/{state['alert_rule_id']}", json={
            "name": "Updated Alert",
            "channel": "email",
            "email_addresses": ["team@company.com"],
        })
        assert r.status_code == 200

    def test_test_alert(self, client, state):
        r = client.post(f"/api/alerts/rules/{state['alert_rule_id']}/test")
        assert r.status_code == 200

    def test_list_alert_logs(self, client):
        r = client.get("/api/alerts/logs")
        assert r.status_code == 200

    def test_delete_alert_rule(self, client):
        cr = client.post("/api/alerts/rules", json={
            "name": "Temp Alert", "channel": "email",
            "email_addresses": ["x@x.com"],
        })
        rid = cr.json()["id"]
        r = client.delete(f"/api/alerts/rules/{rid}")
        assert r.status_code == 200


# ═══════════════════════════════════════════
# 8. Monitor
# ═══════════════════════════════════════════

class TestMonitor:
    """Execution monitoring, stats, and failure tracking."""

    def test_list_executions(self, client):
        r = client.get("/api/monitor/executions")
        assert r.status_code == 200
        execs = r.json()
        assert isinstance(execs, list)
        assert len(execs) >= 1  # At least one from our execution tests

    def test_get_stats(self, client):
        r = client.get("/api/monitor/stats")
        assert r.status_code == 200
        stats = r.json()
        assert "total" in stats
        assert "success_rate" in stats
        assert stats["total"] >= 1

    def test_get_multi_stats(self, client):
        r = client.get("/api/monitor/stats/multi")
        assert r.status_code == 200

    def test_active_schedules(self, client):
        r = client.get("/api/monitor/active-schedules")
        assert r.status_code == 200

    def test_failed_pipelines(self, client):
        r = client.get("/api/monitor/failed")
        assert r.status_code == 200

    def test_executions_alias(self, client):
        """GET /api/executions/ should work as alias for monitor."""
        r = client.get("/api/executions/")
        assert r.status_code == 200

    def test_filter_executions_by_workflow(self, client, state):
        r = client.get(f"/api/monitor/executions?workflow_id={state['workflow_id']}")
        assert r.status_code == 200
        execs = r.json()
        for e in execs:
            assert e.get("workflow_id") == state["workflow_id"]


# ═══════════════════════════════════════════
# 9. Credentials
# ═══════════════════════════════════════════

class TestCredentials:
    """Credential CRUD with secret masking and connectivity test."""

    def test_create_credential(self, client, state):
        r = client.post("/api/credentials/", json={
            "name": "Production DB",
            "type": "postgresql",
            "config": {
                "host": "db.production.internal",
                "port": 5432,
                "database": "analytics",
                "username": "admin",
                "password": "super_secret_password_123",
            },
            "project_id": state.get("project_id", "default"),
        })
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "Production DB"
        # Password MUST be masked
        assert data["config"]["password"] != "super_secret_password_123"
        assert "***" in data["config"]["password"] or len(data["config"]["password"]) < len("super_secret_password_123")
        state["credential_id"] = data["id"]

    def test_list_credentials(self, client):
        r = client.get("/api/credentials/")
        assert r.status_code == 200
        creds = r.json()
        # All passwords must be masked
        for c in creds:
            if "password" in c.get("config", {}):
                assert "super_secret" not in c["config"]["password"]

    def test_get_credential(self, client, state):
        r = client.get(f"/api/credentials/{state['credential_id']}")
        assert r.status_code == 200
        assert r.json()["name"] == "Production DB"

    def test_update_credential(self, client, state):
        r = client.put(f"/api/credentials/{state['credential_id']}", json={
            "name": "Production DB (Updated)",
            "config": {"host": "new-host", "port": 5432, "password": "new_secret"},
        })
        assert r.status_code == 200

    def test_test_credential(self, client, state):
        r = client.post(f"/api/credentials/{state['credential_id']}/test")
        assert r.status_code == 200

    def test_delete_credential(self, client):
        cr = client.post("/api/credentials/", json={
            "name": "Temp Cred", "type": "mysql",
            "config": {"host": "localhost"},
        })
        cid = cr.json()["id"]
        r = client.delete(f"/api/credentials/{cid}")
        assert r.status_code == 200


# ═══════════════════════════════════════════
# 10. Connections
# ═══════════════════════════════════════════

class TestConnections:
    """Connection types, CRUD, test, and saved reports."""

    def test_list_connection_types(self, client):
        r = client.get("/api/connections/types")
        assert r.status_code == 200
        types = r.json()
        assert isinstance(types, list)
        assert len(types) >= 5

    def test_create_connection(self, client, state):
        r = client.post("/api/connections/", json={
            "name": "Analytics DB",
            "type": "postgresql",
            "config": {"host": "localhost", "port": 5432, "database": "analytics"},
            "project_id": state.get("project_id", "default"),
            "description": "Primary analytics database",
            "tags": ["production", "analytics"],
        })
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "Analytics DB"
        state["connection_id"] = data["id"]

    def test_list_connections(self, client):
        r = client.get("/api/connections/")
        assert r.status_code == 200

    def test_get_connection(self, client, state):
        r = client.get(f"/api/connections/{state['connection_id']}")
        assert r.status_code == 200
        assert r.json()["name"] == "Analytics DB"

    def test_update_connection(self, client, state):
        r = client.put(f"/api/connections/{state['connection_id']}", json={
            "name": "Analytics DB v2",
            "config": {"host": "new-host", "port": 5432},
        })
        assert r.status_code == 200

    def test_test_connection(self, client, state):
        r = client.post(f"/api/connections/{state['connection_id']}/test")
        assert r.status_code == 200

    def test_create_rest_api_connection(self, client):
        r = client.post("/api/connections/", json={
            "name": "External API",
            "type": "rest_api",
            "config": {"base_url": "http://localhost:8001"},
        })
        assert r.status_code == 200

    def test_create_connection_report(self, client, state):
        r = client.post(f"/api/connections/{state['connection_id']}/reports", json={
            "name": "Monthly Summary",
            "query": "SELECT region, SUM(amount) FROM orders GROUP BY region",
        })
        assert r.status_code == 200
        state["report_id"] = r.json()["id"]

    def test_list_connection_reports(self, client, state):
        r = client.get(f"/api/connections/{state['connection_id']}/reports")
        assert r.status_code == 200
        reports = r.json()
        assert len(reports) >= 1

    def test_delete_connection(self, client):
        cr = client.post("/api/connections/", json={
            "name": "Temp Conn", "type": "mysql",
            "config": {"host": "localhost"},
        })
        cid = cr.json()["id"]
        r = client.delete(f"/api/connections/{cid}")
        assert r.status_code == 200


# ═══════════════════════════════════════════
# 11. Variables
# ═══════════════════════════════════════════

class TestVariables:
    """Variable CRUD, resolution, and scoping."""

    def test_create_global_variable(self, client, state):
        r = client.post("/api/variables/", json={
            "key": "COMPANY_NAME",
            "value": "Hybridyn",
            "type": "string",
            "scope": "global",
            "description": "Company name for reports",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["key"] == "COMPANY_NAME"
        assert data["value"] == "Hybridyn"
        state["variable_id"] = data["id"]

    def test_create_project_variable(self, client, state):
        r = client.post("/api/variables/", json={
            "key": "BATCH_SIZE",
            "value": "1000",
            "type": "number",
            "scope": "project",
            "project_id": state.get("project_id", "default"),
        })
        assert r.status_code == 200

    def test_list_variables(self, client):
        r = client.get("/api/variables/")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_get_variable(self, client, state):
        r = client.get(f"/api/variables/{state['variable_id']}")
        assert r.status_code == 200
        assert r.json()["key"] == "COMPANY_NAME"

    def test_update_variable(self, client, state):
        r = client.put(f"/api/variables/{state['variable_id']}", json={
            "value": "Hybridyn Inc.",
        })
        assert r.status_code == 200

    def test_resolve_variable(self, client):
        # `key` is a query parameter on this endpoint, not a JSON body field.
        r = client.post("/api/variables/resolve?key=COMPANY_NAME")
        assert r.status_code == 200

    def test_delete_variable(self, client):
        cr = client.post("/api/variables/", json={
            "key": "TEMP_VAR", "value": "temp", "type": "string", "scope": "global",
        })
        vid = cr.json()["id"]
        r = client.delete(f"/api/variables/{vid}")
        assert r.status_code == 200


# ═══════════════════════════════════════════
# 12. Authentication
# ═══════════════════════════════════════════

class TestAuth:
    """Register, login, session management, and user management."""

    def test_default_admin_login(self, client, state):
        r = client.post("/api/auth/login", json={
            "email": "admin@fpulse.local",
            "password": ADMIN_TEST_PASSWORD,
        })
        assert r.status_code == 200
        data = r.json()
        assert "token" in data
        assert "user" in data
        state["admin_token"] = data["token"]

    def test_wrong_password_rejected(self, client):
        r = client.post("/api/auth/login", json={
            "email": "admin@fpulse.local",
            "password": "WrongPass!2026X",
        })
        assert r.status_code == 401

    def test_register_new_user(self, client, state):
        r = client.post("/api/auth/register", json={
            "email": "testuser@fpulse.com",
            "password": "Secure!Pass2026",
            "name": "Test User",
        })
        assert r.status_code == 200
        data = r.json()
        assert "token" in data
        state["user_token"] = data["token"]

    def test_duplicate_registration_rejected(self, client):
        r = client.post("/api/auth/register", json={
            "email": "admin@fpulse.local",
            "password": "anything",
        })
        assert r.status_code == 409

    def test_get_current_user(self, client, state):
        r = client.get("/api/auth/me", headers={
            "Authorization": f"Bearer {state['admin_token']}",
        })
        assert r.status_code == 200
        assert r.json()["email"] == "admin@fpulse.local"

    def test_list_users(self, client):
        r = client.get("/api/auth/users")
        assert r.status_code == 200
        users = r.json()
        assert len(users) >= 2  # admin + registered user

    def test_logout_invalidates_session(self, client, state):
        # OSS enforces a seat limit (admin + testuser already fill the
        # default seats), so registering a throwaway 3rd user here hits
        # "Seat limit reached" (403). Log in as the already-registered
        # test user instead and exercise logout on that session.
        login = client.post("/api/auth/login", json={
            "email": "testuser@fpulse.com",
            "password": "Secure!Pass2026",
        })
        assert login.status_code == 200
        token = login.json()["token"]

        # Logout
        r = client.post("/api/auth/logout", headers={
            "Authorization": f"Bearer {token}",
        })
        assert r.status_code == 200

        # Session should be invalid
        r2 = client.get("/api/auth/me", headers={
            "Authorization": f"Bearer {token}",
        })
        assert r2.status_code == 401


# ═══════════════════════════════════════════
# 13. Schema Contracts
# ═══════════════════════════════════════════

class TestSchemaContracts:
    """Schema contract creation, validation, and drift detection."""

    def test_create_contract(self, client, state):
        r = client.post("/api/contracts/", json={
            "workflow_id": state["workflow_id"],
            "step_id": "load",
            "columns": [
                {"name": "id", "type": "INTEGER"},
                {"name": "product", "type": "VARCHAR"},
                {"name": "quantity", "type": "INTEGER"},
                {"name": "price", "type": "DOUBLE"},
                {"name": "region", "type": "VARCHAR"},
                {"name": "status", "type": "VARCHAR"},
            ],
            "description": "Orders schema contract",
        })
        assert r.status_code == 200
        state["contract_id"] = r.json()["id"]

    def test_list_contracts(self, client, state):
        r = client.get(f"/api/contracts/{state['workflow_id']}")
        assert r.status_code == 200
        contracts = r.json()
        assert len(contracts) >= 1

    def test_validate_contract_passes(self, client, state):
        r = client.post(f"/api/contracts/validate/{state['contract_id']}", json={
            "columns": [
                {"name": "id", "type": "BIGINT"},
                {"name": "product", "type": "VARCHAR"},
                {"name": "quantity", "type": "BIGINT"},
                {"name": "price", "type": "DOUBLE"},
                {"name": "region", "type": "VARCHAR"},
                {"name": "status", "type": "VARCHAR"},
            ],
        })
        assert r.status_code == 200
        assert r.json()["valid"] is True

    def test_check_drift(self, client, state):
        # Route is /contracts/drift/{id} (path order), not /{id}/drift.
        r = client.get(f"/api/contracts/drift/{state['contract_id']}")
        assert r.status_code == 200


# ═══════════════════════════════════════════
# 14. Intelligence
# ═══════════════════════════════════════════

class TestIntelligence:
    """Schema detection, data flattening, and pipeline suggestions."""

    def test_detect_csv_schema(self, client):
        # API model fields are raw_data + source_type (not data/format).
        r = client.post("/api/intelligence/detect-schema", json={
            "raw_data": "id,name,amount,active\n1,Alice,100.5,true\n2,Bob,200.0,false",
            "source_type": "csv",
        })
        assert r.status_code == 200

    def test_detect_json_schema(self, client):
        r = client.post("/api/intelligence/detect-schema", json={
            "raw_data": '[{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]',
            "source_type": "json",
        })
        assert r.status_code == 200

    def test_flatten_nested_json(self, client):
        # flatten takes raw_data (a string) + source_type, not a nested dict.
        r = client.post("/api/intelligence/flatten", json={
            "raw_data": json.dumps({
                "order": {
                    "id": 1,
                    "customer": {"name": "Alice", "address": {"city": "NYC"}},
                    "items": [{"sku": "A1", "qty": 2}],
                }
            }),
            "source_type": "json",
        })
        assert r.status_code == 200

    def test_suggest_pipeline(self, client):
        # suggest-pipeline expects a full DetectedSchema object under
        # "schema" (total_columns + columns[]), not a bare column list.
        # Feed it the output of detect-schema, which is exactly that shape.
        det = client.post("/api/intelligence/detect-schema", json={
            "raw_data": "id,name,amount,status\n1,Alice,10.5,active\n2,Bob,5.0,inactive",
            "source_type": "csv",
        })
        assert det.status_code == 200
        r = client.post("/api/intelligence/suggest-pipeline", json={"schema": det.json()})
        assert r.status_code == 200


# ═══════════════════════════════════════════
# 15. Logs
# ═══════════════════════════════════════════

class TestLogs:
    """Execution logs listing, detail, events, and comparison."""

    def test_list_execution_logs(self, client):
        r = client.get("/api/logs/executions")
        assert r.status_code == 200
        logs = r.json()
        assert isinstance(logs, list)

    def test_filter_logs_by_status(self, client):
        r = client.get("/api/logs/executions?status=success")
        assert r.status_code == 200

    def test_log_stats(self, client):
        # Execution stats live under the monitor router, not /api/logs.
        r = client.get("/api/monitor/stats")
        assert r.status_code == 200


# ═══════════════════════════════════════════
# 16. Backup
# ═══════════════════════════════════════════

class TestBackup:
    """Local backup create, list, and restore."""

    def test_create_backup(self, client):
        r = client.post("/api/backup/create", json={"provider": "local"})
        assert r.status_code == 200
        assert "backup_id" in r.json() or "path" in r.json() or "message" in r.json()

    def test_list_backups(self, client):
        r = client.get("/api/backup/list")
        assert r.status_code == 200

    def test_backup_status(self, client):
        r = client.get("/api/backup/status")
        assert r.status_code == 200


# ═══════════════════════════════════════════
# 17. AI
# ═══════════════════════════════════════════

class TestAI:
    """AI endpoint availability (works with or without LLM provider)."""

    def test_ai_status(self, client):
        r = client.get("/api/ai/status")
        assert r.status_code == 200

    def test_suggest_next_node(self, client):
        r = client.post("/api/ai/suggest-next", json={
            "current_nodes": [
                {"type": "csv_source", "label": "Load CSV"},
            ],
        })
        assert r.status_code == 200

    def test_diagnose_error(self, client):
        r = client.post("/api/ai/diagnose-error", json={
            "error_message": "Column 'price' not found in source data",
            "node_type": "filter",
        })
        assert r.status_code == 200

    def test_generate_sql(self, client):
        r = client.post("/api/ai/generate-sql", json={
            "natural_language": "Get top 10 customers by revenue",
            "available_columns": ["customer_id", "name", "revenue"],
        })
        assert r.status_code == 200


# ═══════════════════════════════════════════
# 18. Planner
# ═══════════════════════════════════════════

class TestPlanner:
    """AI planner: generate pipeline, templates, chat."""

    def test_generate_plan(self, client):
        r = client.post("/api/planner/generate", json={
            "intent": "Load orders.csv, filter active orders, aggregate revenue by region",
        })
        assert r.status_code == 200

    def test_list_planner_templates(self, client):
        r = client.get("/api/planner/templates")
        assert r.status_code == 200
        # Templates are returned as a dict keyed by template name
        # (simple_etl, dedup_pipeline, ...), not a list.
        assert isinstance(r.json(), dict)

    def test_chat(self, client):
        # /planner/chat takes a list of ChatMessage ({role, content}),
        # not a single {"message": ...} object.
        r = client.post("/api/planner/chat", json=[
            {"role": "user", "content": "I want to build a pipeline that loads CSV and filters rows"},
        ])
        assert r.status_code == 200

    def test_ai_status_via_planner(self, client):
        r = client.get("/api/planner/ai-status")
        assert r.status_code == 200


# ═══════════════════════════════════════════
# 19. File Upload & Management
# ═══════════════════════════════════════════

class TestFileManagement:
    """File upload, listing, and type validation."""

    def test_upload_csv(self, client):
        r = client.post("/api/upload", files={
            "file": ("uploaded_data.csv", "id,value\n1,100\n2,200", "text/csv"),
        })
        assert r.status_code == 200
        assert r.json()["filename"] == "uploaded_data.csv"

    def test_upload_json(self, client):
        r = client.post("/api/upload", files={
            "file": ("data.json", '[{"a":1}]', "application/json"),
        })
        assert r.status_code == 200

    def test_upload_reject_unsupported_type(self, client):
        r = client.post("/api/upload", files={
            "file": ("malware.exe", b"\x00\x01\x02", "application/octet-stream"),
        })
        assert r.status_code == 400

    def test_uploaded_file_appears_in_list(self, client):
        r = client.get("/api/files")
        assert r.status_code == 200
        filenames = [f["name"] for f in r.json()]
        assert "uploaded_data.csv" in filenames

    def test_upload_tsv(self, client):
        r = client.post("/api/upload", files={
            "file": ("data.tsv", "id\tname\n1\tAlice", "text/tab-separated-values"),
        })
        assert r.status_code == 200


# ═══════════════════════════════════════════
# 20. WebSocket Info
# ═══════════════════════════════════════════

class TestWebSocketInfo:
    """WebSocket metadata endpoints (not the WS itself)."""

    def test_ws_connections_info(self, client):
        # Endpoint is /api/ws/connections (returns {connections, total}).
        r = client.get("/api/ws/connections")
        assert r.status_code == 200

    def test_ws_stats(self, client):
        # No separate /stats endpoint — connection stats (total) are
        # carried by /api/ws/connections.
        r = client.get("/api/ws/connections")
        assert r.status_code == 200
        assert "total" in r.json()


# ═══════════════════════════════════════════
# 21. Cross-Feature Integration
# ═══════════════════════════════════════════

class TestCrossFeatureIntegration:
    """End-to-end flows spanning multiple feature areas."""

    def test_full_lifecycle_project_to_archive(self, client):
        """Project → Pipeline → Execute → Monitor → Schedule → Alert → Archive → Restore"""
        # 1. Create project
        proj = client.post("/api/projects/", json={
            "name": "Integration Test Project",
            "color": "#10b981",
        })
        assert proj.status_code == 200
        pid = proj.json()["id"]

        # 2. Create pipeline in project
        wf = client.post("/api/workflows/", json={
            "name": "Integration Pipeline",
            "project_id": pid,
            "steps": [
                {"id": "s1", "type": "csv_source", "label": "Load",
                 "params": {"file_path": "orders.csv"}},
                {"id": "s2", "type": "filter", "label": "Filter",
                 "params": {"condition": "quantity > 5"}},
            ],
            "connections": [{"from_step": "s1", "to_step": "s2"}],
        })
        assert wf.status_code == 200
        wid = wf.json()["id"]

        # 3. Execute pipeline
        ex = client.post(f"/api/execute/workflow/{wid}")
        assert ex.status_code == 200
        assert ex.json()["status"] == "success"

        # 4. Verify in monitor
        mon = client.get(f"/api/monitor/executions?workflow_id={wid}")
        assert mon.status_code == 200
        assert len(mon.json()) >= 1

        # 5. Create schedule
        sched = client.post("/api/schedules/", json={
            "workflow_id": wid, "project_id": pid,
            "name": "Daily Integration", "schedule_type": "daily",
        })
        assert sched.status_code == 200
        sid = sched.json()["id"]

        # 6. Create alert
        alert = client.post("/api/alerts/rules", json={
            "name": "Integration Alert", "workflow_id": wid,
            "condition": "on_failure", "channel": "email",
            "email_addresses": ["test@test.com"],
        })
        assert alert.status_code == 200
        aid = alert.json()["id"]

        # 7. Verify project pipelines
        pp = client.get(f"/api/projects/{pid}/pipelines")
        assert pp.status_code == 200
        assert len(pp.json()) >= 1

        # 8. Archive pipeline
        arch = client.post(f"/api/workflows/{wid}/archive")
        assert arch.status_code == 200

        # 9. Restore pipeline
        rest = client.post(f"/api/workflows/{wid}/restore")
        assert rest.status_code == 200

        # Cleanup
        client.delete(f"/api/schedules/{sid}")
        client.delete(f"/api/alerts/rules/{aid}")

    def test_template_import_to_execution(self, client):
        """Template Import → Execute created pipeline.

        Replaces the old test_template_to_execution which hit the removed
        backend gallery endpoints (GET /api/templates, POST /{id}/use —
        see PR 5 / 2026-05-17). The frontend now reads the template
        catalog from frontend/src/templates/catalog.ts; the only
        server-side template surface is the import/export pair.
        """
        # Import a minimal pipeline payload (same shape the frontend
        # POSTs when the user clicks "Use Template" on a static entry).
        imp = client.post("/api/templates/import", json={
            "name": "Cross-feature CSV template",
            "description": "Inline template for the cross-feature integration test.",
            "steps": [
                {"type": "csv_source", "label": "Load",
                 "params": {"file_path": "orders.csv"},
                 "position": {"x": 100, "y": 100}},
                {"type": "output", "label": "Output",
                 "params": {"format": "csv"},
                 "position": {"x": 400, "y": 100}},
            ],
            "connections": [{"from_step": 0, "to_step": 1}],
        })
        assert imp.status_code == 200, f"import failed: {imp.text}"
        wid = imp.json()["id"]

        # Verify workflow was created and is readable via the standard
        # workflows endpoint.
        get_wf = client.get(f"/api/workflows/{wid}")
        assert get_wf.status_code == 200

    def test_credential_to_connection(self, client):
        """Create credential → Create connection with credential → Test."""
        # Create credential
        cred = client.post("/api/credentials/", json={
            "name": "Integration Cred",
            "type": "postgresql",
            "config": {"host": "localhost", "port": 5432, "password": "secret"},
        })
        assert cred.status_code == 200
        cred_id = cred.json()["id"]

        # Create connection using credential
        conn = client.post("/api/connections/", json={
            "name": "Integration Connection",
            "type": "postgresql",
            "config": {"host": "localhost", "port": 5432, "database": "test"},
            "credential_id": cred_id,
        })
        assert conn.status_code == 200

    def test_variable_resolution_in_pipeline(self, client):
        """Create variable → Use in pipeline context."""
        # Create variable
        client.post("/api/variables/", json={
            "key": "FILTER_THRESHOLD",
            "value": "10",
            "type": "number",
            "scope": "global",
        })

        # Resolve it (key is a query parameter, not a JSON body field)
        r = client.post("/api/variables/resolve?key=FILTER_THRESHOLD")
        assert r.status_code == 200


# ═══════════════════════════════════════════
# 22. Edge Cases & Error Handling
# ═══════════════════════════════════════════

class TestEdgeCases:
    """Boundary conditions, error responses, and edge cases."""

    def test_empty_workflow_name(self, client):
        """Creating with empty name should still work or provide helpful error."""
        r = client.post("/api/workflows/", json={
            "name": "",
            "steps": [],
            "connections": [],
        })
        # Should either succeed with default name or return 400
        assert r.status_code in (200, 400, 422)

    def test_large_step_count(self, client):
        """Pipeline with many steps."""
        steps = [
            {"id": f"s{i}", "type": "csv_source", "label": f"Step {i}",
             "params": {"file_path": "orders.csv"},
             "position": {"x": i * 200, "y": 100}}
            for i in range(20)
        ]
        r = client.post("/api/workflows/", json={
            "name": "Large Pipeline",
            "steps": steps,
            "connections": [],
        })
        assert r.status_code == 200

    def test_special_characters_in_name(self, client):
        """Project name with special characters."""
        r = client.post("/api/projects/", json={
            "name": "Test & Dev (v2) — Special!",
        })
        assert r.status_code == 200
        assert "Test & Dev" in r.json()["name"]

    def test_unicode_in_variable(self, client):
        r = client.post("/api/variables/", json={
            "key": "GREETING", "value": "こんにちは世界",
            "type": "string", "scope": "global",
        })
        assert r.status_code == 200

    def test_concurrent_workflow_creation(self, client):
        """Multiple workflows created in sequence shouldn't conflict."""
        ids = set()
        for i in range(5):
            r = client.post("/api/workflows/", json={
                "name": f"Concurrent {i}",
                "steps": [],
                "connections": [],
            })
            assert r.status_code == 200
            wid = r.json()["id"]
            assert wid not in ids  # No duplicate IDs
            ids.add(wid)

    def test_workflow_with_invalid_step_type(self, client):
        """Invalid step type should fallback or error gracefully."""
        r = client.post("/api/workflows/", json={
            "name": "Invalid Type Test",
            "steps": [
                {"id": "s1", "type": "nonexistent_node_type", "label": "Bad Node",
                 "params": {}},
            ],
            "connections": [],
        })
        # Should either reject with 400/422 or accept with fallback
        assert r.status_code in (200, 400, 422)

    def test_circular_connection_handling(self, client):
        """Circular connection (A→B→A) should be handled."""
        r = client.post("/api/workflows/", json={
            "name": "Circular Test",
            "steps": [
                {"id": "a", "type": "filter", "label": "A", "params": {}},
                {"id": "b", "type": "filter", "label": "B", "params": {}},
            ],
            "connections": [
                {"from_step": "a", "to_step": "b"},
                {"from_step": "b", "to_step": "a"},
            ],
        })
        # Should either reject or accept (validation will catch it)
        assert r.status_code in (200, 400, 422)

    def test_delete_nonexistent_resource(self, client):
        """Deleting non-existent resources should return 404."""
        assert client.delete("/api/workflows/no-such-id").status_code == 404
        assert client.delete("/api/schedules/no-such-id").status_code == 404

    def test_execute_missing_file_workflow(self, client):
        """Pipeline referencing a non-existent file should fail gracefully."""
        cr = client.post("/api/workflows/", json={
            "name": "Missing File",
            "steps": [
                {"id": "s1", "type": "csv_source", "label": "Load",
                 "params": {"file_path": "nonexistent_file.csv"}},
            ],
            "connections": [],
        })
        wid = cr.json()["id"]
        r = client.post(f"/api/execute/workflow/{wid}")
        assert r.status_code == 200
        data = r.json()
        # Should report error, not crash
        assert data["status"] == "error" or data["step_results"]["s1"]["status"] == "error"


# ═══════════════════════════════════════════
# 23. Data Integrity Checks
# ═══════════════════════════════════════════

class TestDataIntegrity:
    """Verify data correctness across operations."""

    def test_filter_reduces_row_count(self, client):
        """Filter step must reduce rows from source."""
        cr = client.post("/api/workflows/", json={
            "name": "Row Count Test",
            "steps": [
                {"id": "src", "type": "csv_source", "label": "Load",
                 "params": {"file_path": "orders.csv"}},
                {"id": "flt", "type": "filter", "label": "Expensive",
                 "params": {"condition": "price > 40"}},
            ],
            "connections": [{"from_step": "src", "to_step": "flt"}],
        })
        wid = cr.json()["id"]
        r = client.post(f"/api/execute/workflow/{wid}")
        data = r.json()
        src_rows = data["step_results"]["src"]["row_count"]
        flt_rows = data["step_results"]["flt"]["row_count"]
        assert flt_rows <= src_rows
        # price > 40: Widget B(49.99), Widget C(99.99), Widget F(79.99) = 3 rows
        assert flt_rows == 3

    def test_aggregate_produces_grouped_output(self, client):
        """Aggregate step should have fewer rows than source."""
        cr = client.post("/api/workflows/", json={
            "name": "Agg Test",
            "steps": [
                {"id": "src", "type": "csv_source", "label": "Load",
                 "params": {"file_path": "orders.csv"}},
                {"id": "agg", "type": "aggregate", "label": "By Region",
                 "params": {"group_by": "region", "functions": {"price": "sum"}}},
            ],
            "connections": [{"from_step": "src", "to_step": "agg"}],
        })
        wid = cr.json()["id"]
        r = client.post(f"/api/execute/workflow/{wid}")
        data = r.json()
        assert data["status"] == "success"
        agg_rows = data["step_results"]["agg"]["row_count"]
        assert agg_rows == 4  # 4 unique regions: North, South, East, West

    def test_version_immutability(self, client):
        """After creating v2, v1 should still be retrievable unchanged."""
        cr = client.post("/api/workflows/", json={
            "name": "V1 Original",
            "steps": [
                {"id": "s1", "type": "csv_source", "label": "Original Step",
                 "params": {"file_path": "orders.csv"}},
            ],
            "connections": [],
        })
        wid = cr.json()["id"]

        # Update to v2
        client.put(f"/api/workflows/{wid}", json={
            "name": "V2 Updated",
            "steps": [
                {"id": "s1", "type": "csv_source", "label": "Changed Step",
                 "params": {"file_path": "customers.csv"}},
                {"id": "s2", "type": "filter", "label": "New Step",
                 "params": {"condition": "active = true"}},
            ],
            "connections": [{"from_step": "s1", "to_step": "s2"}],
        })

        # V1 should be unchanged
        v1 = client.get(f"/api/workflows/{wid}?version=1")
        assert v1.status_code == 200
        v1_wf = v1.json()["workflow"]
        assert v1_wf["name"] == "V1 Original"
        assert len(v1_wf["steps"]) == 1

        # V2 should have changes
        v2 = client.get(f"/api/workflows/{wid}?version=2")
        assert v2.status_code == 200
        v2_wf = v2.json()["workflow"]
        assert v2_wf["name"] == "V2 Updated"
        assert len(v2_wf["steps"]) == 2

    def test_project_pipeline_count_accurate(self, client):
        """Project pipeline count should reflect actual pipeline count."""
        # Create project
        proj = client.post("/api/projects/", json={"name": "Count Test"})
        pid = proj.json()["id"]

        # Add 3 pipelines
        for i in range(3):
            client.post("/api/workflows/", json={
                "name": f"Count Pipeline {i}",
                "project_id": pid,
                "steps": [],
                "connections": [],
            })

        # Verify count
        pp = client.get(f"/api/projects/{pid}/pipelines")
        assert pp.status_code == 200
        assert len(pp.json()) == 3

    def test_credential_secrets_never_leaked(self, client):
        """Secrets must never appear in any API response."""
        secret = "ULTRA_SECRET_KEY_12345"
        cr = client.post("/api/credentials/", json={
            "name": "Secret Test",
            "type": "api_key",
            "config": {"api_key": secret, "password": secret},
        })
        assert cr.status_code == 200

        # Check create response
        resp_text = json.dumps(cr.json())
        assert secret not in resp_text

        # Check list response
        listing = client.get("/api/credentials/")
        list_text = json.dumps(listing.json())
        assert secret not in list_text

        # Check get response
        cid = cr.json()["id"]
        get_resp = client.get(f"/api/credentials/{cid}")
        get_text = json.dumps(get_resp.json())
        assert secret not in get_text


# ═══════════════════════════════════════════
# 24. Workflow Lifecycle
# ═══════════════════════════════════════════

class TestWorkflowLifecycle:
    """Pipeline lifecycle: draft → test → publish → archive → restore."""

    def test_lifecycle_transitions(self, client):
        # Create (draft)
        cr = client.post("/api/workflows/", json={
            "name": "Lifecycle Test",
            "steps": [
                {"id": "s1", "type": "csv_source", "label": "Load",
                 "params": {"file_path": "orders.csv"}},
            ],
            "connections": [],
        })
        wid = cr.json()["id"]

        # Archive
        r = client.post(f"/api/workflows/{wid}/archive")
        assert r.status_code == 200

        # Restore
        r = client.post(f"/api/workflows/{wid}/restore")
        assert r.status_code == 200


# ═══════════════════════════════════════════
# Test summary
# ═══════════════════════════════════════════

"""
Total: 120+ test cases covering:

  ✅ System Health (5 tests)
  ✅ Projects CRUD (8 tests)
  ✅ Workflows CRUD + Versioning (10 tests)
  ✅ Pipeline Execution (9 tests)
  ✅ Template Gallery (12 tests)
  ✅ Scheduling (7 tests)
  ✅ Alerts (7 tests)
  ✅ Monitor (7 tests)
  ✅ Credentials (6 tests)
  ✅ Connections (10 tests)
  ✅ Variables (7 tests)
  ✅ Auth (7 tests)
  ✅ Schema Contracts (4 tests)
  ✅ Intelligence (4 tests)
  ✅ Logs (3 tests)
  ✅ Backup (3 tests)
  ✅ AI (4 tests)
  ✅ Planner (4 tests)
  ✅ File Management (5 tests)
  ✅ WebSocket Info (2 tests)
  ✅ Cross-Feature Integration (4 tests)
  ✅ Edge Cases (10 tests)
  ✅ Data Integrity (5 tests)
  ✅ Workflow Lifecycle (1 test)

Run with: pytest tests/test_e2e_complete.py -v --tb=short
"""
