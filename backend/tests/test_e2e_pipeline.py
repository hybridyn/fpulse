"""
End-to-End Pipeline Tests — validates complete user workflows.

Tests the full lifecycle:
  Create Project → Create Pipeline → Add Steps → Validate → Execute →
  Schedule → Monitor → Alert → Archive → Restore
"""

import os
import tempfile
import pytest
from fastapi.testclient import TestClient

# Module-level marker — full pipeline lifecycle.
pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def client():
    tmpdir = tempfile.mkdtemp()
    os.environ["FPULSE_DATA_DIR"] = tmpdir
    # Create test data files
    with open(os.path.join(tmpdir, "sales.csv"), "w") as f:
        f.write("date,product,quantity,price,region\n")
        f.write("2026-01-01,Widget A,10,29.99,North\n")
        f.write("2026-01-01,Widget B,5,49.99,South\n")
        f.write("2026-01-02,Widget A,8,29.99,North\n")
        f.write("2026-01-02,Widget C,3,99.99,East\n")
        f.write("2026-01-03,Widget B,12,49.99,West\n")
        f.write("2026-01-03,Widget A,6,29.99,South\n")
    with open(os.path.join(tmpdir, "customers.csv"), "w") as f:
        f.write("id,name,email,region,active\n")
        f.write("1,Alice,alice@test.com,North,true\n")
        f.write("2,Bob,bob@test.com,South,false\n")
        f.write("3,Charlie,charlie@test.com,East,true\n")
        f.write("4,Diana,diana@test.com,West,true\n")
    from fpulse.main import app, app_state
    from fpulse.auth.models import User
    # Loopback Host required by LocalOriginGuardMiddleware (DNS-rebinding
    # defense). TestClient's default "testserver" Host is 403'd; present
    # "localhost" like a real browser. See test_e2e_complete.py fixture.
    with TestClient(app, base_url="http://localhost") as c:
        # Rotate the seeded admin (random bootstrap password) to a known
        # strong value, then attach the bearer token to every request so
        # endpoints behind require_auth/require_admin work in this module.
        admin_password = "Bootstrap!Pass26"
        user_store = app_state["user_store"]
        admin = user_store.get_user("admin")
        admin.password_hash = User.hash_password(admin_password)
        user_store._save_user(admin)

        login = c.post("/api/auth/login", json={
            "email": "admin@fpulse.local",
            "password": admin_password,
        })
        assert login.status_code == 200, f"test admin login failed: {login.text}"
        c.headers.update({"Authorization": f"Bearer {login.json()['token']}"})
        yield c


class TestE2EFullLifecycle:
    """Complete pipeline lifecycle from project creation to archival."""

    def test_step_01_create_project(self, client):
        r = client.post("/api/projects/", json={
            "name": "Sales Analytics",
            "description": "End-to-end test project",
            "color": "#f59e0b",
            "icon": "chart",
        })
        assert r.status_code == 200
        data = r.json()
        self.__class__.project_id = data["id"]
        assert data["name"] == "Sales Analytics"

    def test_step_02_create_pipeline(self, client):
        r = client.post("/api/workflows/", json={
            "name": "Daily Sales Report",
            "description": "Load sales, filter, aggregate by region",
            "project_id": self.__class__.project_id,
            "steps": [
                {"id": "load", "type": "csv_source", "label": "Load Sales",
                 "params": {"file_path": "sales.csv"}},
                {"id": "filter", "type": "filter", "label": "Active Regions",
                 "params": {"condition": "quantity > 5"}},
                {"id": "agg", "type": "aggregate", "label": "Revenue by Region",
                 "params": {"group_by": "region", "functions": {"price": "sum"}}},
            ],
            "connections": [
                {"from_step": "load", "to_step": "filter"},
                {"from_step": "filter", "to_step": "agg"},
            ],
        })
        assert r.status_code == 200
        data = r.json()
        self.__class__.workflow_id = data["id"]
        assert data["version"] == 1

    def test_step_03_validate_pipeline(self, client):
        r = client.post(f"/api/workflows/{self.__class__.workflow_id}/validate")
        assert r.status_code == 200
        data = r.json()
        assert data.get("valid") is True or data.get("errors", []) == []

    def test_step_04_execute_pipeline(self, client):
        r = client.post(f"/api/execute/workflow/{self.__class__.workflow_id}")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "success"
        # Verify step results
        assert data["step_results"]["load"]["status"] == "success"
        assert data["step_results"]["load"]["row_count"] == 6
        assert data["step_results"]["filter"]["status"] == "success"
        assert data["step_results"]["filter"]["row_count"] == 4  # qty > 5
        assert data["step_results"]["agg"]["status"] == "success"
        self.__class__.execution_result = data

    def test_step_05_execute_single_step(self, client):
        r = client.post(f"/api/execute/workflow/{self.__class__.workflow_id}/step/filter")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "success"
        assert data["row_count"] == 4

    def test_step_06_check_monitor(self, client):
        r = client.get("/api/monitor/executions")
        assert r.status_code == 200
        execs = r.json()
        assert len(execs) > 0

    def test_step_07_create_schedule(self, client):
        r = client.post("/api/schedules/", json={
            "workflow_id": self.__class__.workflow_id,
            "project_id": self.__class__.project_id,
            "name": "Daily Sales Run",
            "schedule_type": "daily",
            "daily_time": "06:00",
            "timezone": "UTC",
            "enabled": True,
        })
        assert r.status_code == 200
        self.__class__.schedule_id = r.json()["id"]

    def test_step_08_create_alert(self, client):
        r = client.post("/api/alerts/rules", json={
            "name": "Sales Pipeline Failure Alert",
            "workflow_id": self.__class__.workflow_id,
            "project_id": self.__class__.project_id,
            "condition": "on_failure",
            "channel": "email",
            "email_addresses": ["ops@company.com"],
        })
        assert r.status_code == 200
        self.__class__.alert_rule_id = r.json()["id"]

    def test_step_09_create_variable(self, client):
        r = client.post("/api/variables/", json={
            "key": "SALES_THRESHOLD",
            "value": "5",
            "type": "number",
            "scope": "project",
            "project_id": self.__class__.project_id,
            "description": "Min quantity filter threshold",
        })
        assert r.status_code == 200

    def test_step_10_create_credential(self, client):
        r = client.post("/api/credentials/", json={
            "name": "Sales DB",
            "type": "postgresql",
            "config": {
                "host": "localhost",
                "port": 5432,
                "database": "sales",
                "password": "secret_pw_123",
            },
            "project_id": self.__class__.project_id,
        })
        assert r.status_code == 200
        # Verify password is masked in response
        assert r.json()["config"]["password"] != "secret_pw_123"

    def test_step_11_update_pipeline(self, client):
        """Add a sort step and save as v2."""
        r = client.put(f"/api/workflows/{self.__class__.workflow_id}", json={
            "name": "Daily Sales Report v2",
            "project_id": self.__class__.project_id,
            "steps": [
                {"id": "load", "type": "csv_source", "label": "Load Sales",
                 "params": {"file_path": "sales.csv"}},
                {"id": "filter", "type": "filter", "label": "Active Regions",
                 "params": {"condition": "quantity > 5"}},
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
            "change_summary": "Added sort step for revenue ranking",
        })
        assert r.status_code == 200
        assert r.json()["version"] == 2

    def test_step_12_diff_versions(self, client):
        r = client.get(f"/api/workflows/{self.__class__.workflow_id}/diff?v1=1&v2=2")
        assert r.status_code == 200
        diff = r.json()
        assert "sort" in diff["added_steps"]

    def test_step_13_test_alert(self, client):
        r = client.post(f"/api/alerts/rules/{self.__class__.alert_rule_id}/test")
        assert r.status_code == 200

    def test_step_14_archive_pipeline(self, client):
        r = client.post(f"/api/workflows/{self.__class__.workflow_id}/archive")
        assert r.status_code == 200

    def test_step_15_restore_pipeline(self, client):
        r = client.post(f"/api/workflows/{self.__class__.workflow_id}/restore")
        assert r.status_code == 200

    def test_step_16_verify_health(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        # /api/health doesn't carry a project count; verify projects
        # exist via the projects API instead.
        pr = client.get("/api/projects/")
        assert pr.status_code == 200
        assert len(pr.json()) >= 1

    def test_step_17_verify_stats(self, client):
        r = client.get("/api/monitor/stats")
        assert r.status_code == 200
        stats = r.json()
        assert stats["total"] > 0

    def test_step_18_get_project_pipelines(self, client):
        r = client.get(f"/api/projects/{self.__class__.project_id}/pipelines")
        assert r.status_code == 200
        pipelines = r.json()
        assert len(pipelines) >= 1

    def test_step_19_toggle_schedule(self, client):
        r = client.post(f"/api/schedules/{self.__class__.schedule_id}/toggle")
        assert r.status_code == 200

    def test_step_20_cleanup_delete(self, client):
        """Delete schedule, alert, and verify clean state."""
        client.delete(f"/api/schedules/{self.__class__.schedule_id}")
        client.delete(f"/api/alerts/rules/{self.__class__.alert_rule_id}")
        # Verify deletions
        r = client.get(f"/api/schedules/{self.__class__.schedule_id}")
        assert r.status_code == 404


class TestE2ESchemaContracts:
    """Test schema contract creation and drift detection after execution."""

    def test_create_and_validate_contract(self, client):
        # Create a workflow
        cr = client.post("/api/workflows/", json={
            "name": "Contract Test", "steps": [
                {"id": "s1", "type": "csv_source", "params": {"file_path": "sales.csv"}},
            ], "connections": [],
        })
        wid = cr.json()["id"]

        # Execute to get actual schema
        client.post(f"/api/execute/workflow/{wid}")

        # Create a contract
        r = client.post("/api/contracts/", json={
            "workflow_id": wid, "step_id": "s1",
            "columns": [
                {"name": "date", "type": "VARCHAR"},
                {"name": "product", "type": "VARCHAR"},
                {"name": "quantity", "type": "INTEGER"},
                {"name": "price", "type": "DOUBLE"},
                {"name": "region", "type": "VARCHAR"},
            ],
        })
        assert r.status_code == 200
        cid = r.json()["id"]

        # Validate against actual schema (route is /contracts/validate/{id})
        r = client.post(f"/api/contracts/validate/{cid}", json={
            "columns": [
                {"name": "date", "type": "VARCHAR"},
                {"name": "product", "type": "VARCHAR"},
                {"name": "quantity", "type": "BIGINT"},  # compatible with INTEGER
                {"name": "price", "type": "DOUBLE"},
                {"name": "region", "type": "VARCHAR"},
            ],
        })
        assert r.status_code == 200
        assert r.json()["valid"] is True


class TestE2EDataIntelligence:
    """Test intelligence features: schema detection, pipeline suggestions."""

    def test_detect_csv_schema(self, client):
        # API model fields: raw_data + source_type (not data/format).
        r = client.post("/api/intelligence/detect-schema", json={
            "raw_data": "id,name,amount,active\n1,Alice,100.5,true\n2,Bob,200.0,false",
            "source_type": "csv",
        })
        assert r.status_code == 200
        schema = r.json()
        assert isinstance(schema, (list, dict))

    def test_flatten_nested_json(self, client):
        import json as _json
        r = client.post("/api/intelligence/flatten", json={
            "raw_data": _json.dumps({
                "order": {
                    "id": 1,
                    "customer": {"name": "Alice", "address": {"city": "NYC", "zip": "10001"}},
                    "items": [{"sku": "A1", "qty": 2}, {"sku": "B2", "qty": 1}],
                }
            }),
            "source_type": "json",
        })
        assert r.status_code == 200


class TestE2EAuth:
    """Test complete auth flow: register → login → session → logout."""

    def test_full_auth_flow(self, client):
        # Register. Password must satisfy the policy (upper+lower+digit+
        # special) — the old "securepass123" is now rejected with 400.
        r = client.post("/api/auth/register", json={
            "email": "e2e-user@test.com",
            "password": "Secure!Pass2026",
            "name": "E2E Test User",
        })
        assert r.status_code == 200
        token = r.json()["token"]

        # Get current user
        r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["email"] == "e2e-user@test.com"

        # Logout
        r = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200

        # Session should be invalid now
        r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401
