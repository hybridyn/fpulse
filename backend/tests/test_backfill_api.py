"""Integration tests for the Backfill API and the sink-safety guardrail
on the Schedule create endpoint.

We wire a tiny FastAPI app that hosts just the two routers under test
and bind their dependencies (workflow store, schedule store, the
backfill store singleton) to the per-test SQLite fixture from conftest.

The orchestrator's background thread + real WorkflowExecutor is NOT
exercised here — those have their own tests in
``tests/test_executor*.py``. We monkey-patch ``run_backfill_async``
inside ``fpulse.api.backfills`` to a no-op so create_backfill returns
synchronously and we can assert on the parent + child rows the API
wrote to the store.

Each test rebuilds app_state via monkeypatch.setitem so mutations are
torn down on test exit — see the same pattern in
``tests/test_ai_new_endpoints.py``.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fpulse.api import backfills as backfills_api
from fpulse.api import schedules as schedules_api
from fpulse.backfills.store import get_backfill_store
from fpulse.ir.schema import Workflow, Step, StepType, StepConnection
from fpulse.ir.versioning import WorkflowStore
from fpulse.scheduling.store import ScheduleStore


# ─────────────────────────────────────────────────────────────────────
# Local fixtures — bootstrap a minimal app + wire app_state
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def workspace_id() -> str:
    """Workspace this test acts on. Anonymous callers in OSS get 'default'."""
    return "default"


@pytest.fixture
def app(_fpulse_test_db, monkeypatch):
    """Build a small FastAPI app holding just the backfill + schedule routers.

    Side-effects (cleaned up by monkeypatch + the _fpulse_test_db
    fixture's teardown):
      - app_state["store"] → a real WorkflowStore on the per-test DB.
      - app_state["schedule_store"] → a real ScheduleStore on the same DB.
      - app_state["execution_store"] → None (orchestrator stubbed out so
        this is never read).
      - app_state["data_dir"] → an inert path; orchestrator is stubbed.
      - The module-level BackfillStore singleton is wired to the same
        DB; reset back to None on teardown so a later test doesn't see
        a stale handle.
      - ``run_backfill_async`` inside fpulse.api.backfills is patched to
        a no-op so the API does NOT spawn a background thread that would
        try to use a real executor.
    """
    # Import app_state lazily so a missing fpulse.main doesn't import-time
    # crash tests that don't touch it.
    from fpulse.main import app_state

    workflow_store = WorkflowStore(db=_fpulse_test_db)
    schedule_store = ScheduleStore(db=_fpulse_test_db)
    monkeypatch.setitem(app_state, "store", workflow_store)
    monkeypatch.setitem(app_state, "schedule_store", schedule_store)
    monkeypatch.setitem(app_state, "execution_store", None)
    monkeypatch.setitem(app_state, "data_dir", str(_fpulse_test_db._path) if hasattr(_fpulse_test_db, "_path") else ".")

    # Wire the BackfillStore singleton.
    bf_store = get_backfill_store()
    bf_store.set_db(_fpulse_test_db)

    # Stub the orchestrator dispatch — we test the API contract, not the
    # background executor. The real path is covered by executor tests.
    monkeypatch.setattr(
        backfills_api, "run_backfill_async",
        lambda *args, **kwargs: None,
    )

    app = FastAPI()
    app.include_router(backfills_api.router)
    app.include_router(schedules_api.router)

    yield app

    # Teardown: unwire the singleton so other tests in the run don't
    # see a closed DB handle.
    bf_store.set_db(None)


@pytest.fixture
def client(app) -> TestClient:
    # R10 / P1 follow-up (2026-05-30) — the schedules + backfills
    # routers now require auth at the router level (router-wide
    # require_auth + per-route require_min_rank("developer")).
    # This test's mini-app contains only those two routers, so we
    # override their dependencies to bypass auth. The business logic
    # the tests assert on (sink safety, window enumeration) is what
    # matters here; the full RBAC path is covered by
    # tests/test_anonymous_access_blocked.py against the real app.
    from fpulse.auth.deps import require_auth

    class _FakeUser:
        id = "test"
        email = "test@fpulse.local"
        role = "super_admin"
        is_active = True

    def _allow_any():
        return _FakeUser()

    # require_auth is a module-level function — single identity, easy.
    app.dependency_overrides[require_auth] = _allow_any
    # _AUTHOR / _AUTH on each router are Depends() wrappers — their
    # .dependency is the underlying callable. Override by that callable
    # so the per-route dep checks resolve to our allow-any.
    for mod_name in ("schedules", "backfills"):
        try:
            mod = __import__(f"fpulse.api.{mod_name}", fromlist=["_AUTH", "_AUTHOR"])
            for name in ("_AUTH", "_AUTHOR"):
                dep = getattr(mod, name, None)
                if dep is not None and hasattr(dep, "dependency"):
                    app.dependency_overrides[dep.dependency] = _allow_any
        except Exception:
            pass
    return TestClient(app)


def _save_workflow(workflow_store: WorkflowStore, wf: Workflow) -> str:
    """Save and return the workflow id — the workspace_id stamped on the row
    will be the workflow's own field, which we set to 'default' to match
    the anonymous-caller workspace resolution."""
    versioned = workflow_store.save(wf)
    return versioned.workflow.id


@pytest.fixture
def workflow_store_handle(_fpulse_test_db) -> WorkflowStore:
    """Direct handle to the same workflow store the app sees — for writing
    workflows that the API will then look up."""
    return WorkflowStore(db=_fpulse_test_db)


def _safe_pipeline() -> Workflow:
    """A pipeline whose source references the cursor params AND whose sink
    is backfill-safe (LOCAL_TABLE_SINK with mode=replace).

    Both guardrails pass: find_unsafe_sinks returns [], the preflight's
    cursor check returns None. Useful for the happy-path test."""
    return Workflow(
        id="wf-safe-001",
        name="Safe Backfill Pipeline",
        workspace_id="default",
        project_id="default",
        steps=[
            Step(id="s1", type=StepType.DB_SOURCE, params={
                "query": "SELECT * FROM o WHERE ts >= '${param.window_start}' AND ts < '${param.window_end}'",
                "connection_id": "c1",
            }),
            Step(id="s2", type=StepType.LOCAL_TABLE_SINK, params={
                "table_name": "orders_bf",
                "mode": "replace",
            }),
        ],
        connections=[StepConnection(from_step="s1", to_step="s2")],
    )


def _no_cursor_pipeline() -> Workflow:
    """Source that does NOT reference any cursor param. Sink is safe so
    the cursor check is what should trip."""
    return Workflow(
        id="wf-nocursor-001",
        name="No Cursor Pipeline",
        workspace_id="default",
        project_id="default",
        steps=[
            Step(id="s1", type=StepType.DB_SOURCE, params={
                "query": "SELECT * FROM o",  # no cursor reference
                "connection_id": "c1",
            }),
            Step(id="s2", type=StepType.LOCAL_TABLE_SINK, params={
                "table_name": "out_bf",
                "mode": "replace",
            }),
        ],
        connections=[StepConnection(from_step="s1", to_step="s2")],
    )


def _email_sink_pipeline() -> Workflow:
    """Pipeline ending in EMAIL_SINK — classifier flags it as 'external'.

    Source references the cursor, so cursor check passes; only the
    unsafe-sink check should trip."""
    return Workflow(
        id="wf-email-001",
        name="Email Sink Pipeline",
        workspace_id="default",
        project_id="default",
        steps=[
            Step(id="s1", type=StepType.DB_SOURCE, params={
                "query": "SELECT * FROM o WHERE ts >= '${param.window_start}'",
                "connection_id": "c1",
            }),
            Step(id="s2", type=StepType.EMAIL_SINK, params={
                "to": "ops@example.com",
                "subject": "daily digest",
            }),
        ],
        connections=[StepConnection(from_step="s1", to_step="s2")],
    )


def _append_warehouse_pipeline(idempotent_override: bool = False) -> Workflow:
    """WAREHOUSE_SINK with mode=append → 'append_risky'.

    If idempotent_override=True, the classifier short-circuits to 'safe'."""
    sink_params = {
        "table_name": "events",
        "mode": "append",
    }
    if idempotent_override:
        sink_params["idempotent_override"] = True
    return Workflow(
        id="wf-append-001",
        name="Append Warehouse Pipeline",
        workspace_id="default",
        project_id="default",
        steps=[
            Step(id="s1", type=StepType.DB_SOURCE, params={
                "query": "SELECT * FROM o WHERE ts >= '${param.window_start}'",
                "connection_id": "c1",
            }),
            Step(id="s2", type=StepType.WAREHOUSE_SINK, params=sink_params),
        ],
        connections=[StepConnection(from_step="s1", to_step="s2")],
    )


# ─────────────────────────────────────────────────────────────────────
# POST /api/executions/backfill
# ─────────────────────────────────────────────────────────────────────


class TestCreateBackfillHappyPath:
    def test_happy_path(self, client, workflow_store_handle):
        """Safe pipeline + valid date range → 200 with backfill_id and
        the right total_windows."""
        _save_workflow(workflow_store_handle, _safe_pipeline())

        resp = client.post("/api/executions/backfill", json={
            "pipeline_id": "wf-safe-001",
            "start_date": "2026-01-01",
            "end_date": "2026-01-03",
            "window_size": "daily",
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["pipeline_id"] == "wf-safe-001"
        assert body["backfill_id"]
        # Jan 1 → Jan 3 inclusive at daily granularity = 3 windows.
        assert body["total_windows"] == 3
        assert body["status"] == "pending"


class TestCreateBackfillErrors:
    def test_404_pipeline_not_found(self, client):
        resp = client.post("/api/executions/backfill", json={
            "pipeline_id": "wf-ghost",
            "start_date": "2026-01-01",
            "end_date": "2026-01-03",
        })
        assert resp.status_code == 404
        assert "wf-ghost" in resp.text

    def test_400_no_cursor_usage(self, client, workflow_store_handle):
        """Source doesn't reference cursor + no acknowledgement → 400
        with the canonical violation code."""
        _save_workflow(workflow_store_handle, _no_cursor_pipeline())
        resp = client.post("/api/executions/backfill", json={
            "pipeline_id": "wf-nocursor-001",
            "start_date": "2026-01-01",
            "end_date": "2026-01-03",
        })
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["code"] == "no_source_uses_cursor_param"
        assert detail["sources_checked"]

    def test_acknowledged_no_cursor_usage(self, client, workflow_store_handle):
        """Same pipeline + acknowledge_no_cursor_usage=true → 200."""
        _save_workflow(workflow_store_handle, _no_cursor_pipeline())
        resp = client.post("/api/executions/backfill", json={
            "pipeline_id": "wf-nocursor-001",
            "start_date": "2026-01-01",
            "end_date": "2026-01-03",
            "acknowledge_no_cursor_usage": True,
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["total_windows"] == 3

    def test_400_unsafe_sink(self, client, workflow_store_handle):
        """email_sink → 400 with code=unsafe_for_backfill.

        Note: WorkflowStore.save runs ``migrate_legacy_node_types`` which
        remaps ``email_sink`` → ``destination`` + connector_type=email.
        With no ``mode`` set on a generic destination, the classifier
        returns ``append_risky`` — still unsafe, still trips the guard;
        the step_type just surfaces as ``destination`` in the response."""
        _save_workflow(workflow_store_handle, _email_sink_pipeline())
        resp = client.post("/api/executions/backfill", json={
            "pipeline_id": "wf-email-001",
            "start_date": "2026-01-01",
            "end_date": "2026-01-03",
        })
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["code"] == "unsafe_for_backfill"
        # Sink list surfaces the offending step by id.
        assert detail["unsafe_sinks"]
        assert any(u["step_id"] == "s2" for u in detail["unsafe_sinks"])

    def test_acknowledged_unsafe_sink_passes_sink_check(
        self, client, workflow_store_handle,
    ):
        """The email-sink pipeline DOES reference the cursor — once the
        unsafe-sink check is acknowledged, the cursor check passes too
        and the create succeeds (i.e. the two guards are wired in series
        and acknowledging one doesn't bypass the other)."""
        _save_workflow(workflow_store_handle, _email_sink_pipeline())
        resp = client.post("/api/executions/backfill", json={
            "pipeline_id": "wf-email-001",
            "start_date": "2026-01-01",
            "end_date": "2026-01-03",
            "acknowledge_side_effects": True,
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["total_windows"] == 3

    def test_400_invalid_window(self, client, workflow_store_handle):
        _save_workflow(workflow_store_handle, _safe_pipeline())
        resp = client.post("/api/executions/backfill", json={
            "pipeline_id": "wf-safe-001",
            "start_date": "2026-01-10",
            "end_date": "2026-01-01",  # before start
        })
        assert resp.status_code == 400
        assert "end_date" in resp.text or "window" in resp.text.lower()

    def test_idempotent_override_bypasses_unsafe(
        self, client, workflow_store_handle,
    ):
        """A WAREHOUSE_SINK with mode=append AND idempotent_override=true is
        classified as 'safe' — the unsafe-sink check must not fire."""
        _save_workflow(
            workflow_store_handle,
            _append_warehouse_pipeline(idempotent_override=True),
        )
        resp = client.post("/api/executions/backfill", json={
            "pipeline_id": "wf-append-001",
            "start_date": "2026-01-01",
            "end_date": "2026-01-02",
        })
        # Cursor check passes (source references window_start), unsafe
        # check is short-circuited by idempotent_override → 200.
        assert resp.status_code == 200, resp.text
        assert resp.json()["total_windows"] == 2


# ─────────────────────────────────────────────────────────────────────
# GET /api/executions/backfill  +  GET /api/executions/backfill/{id}
# ─────────────────────────────────────────────────────────────────────


class TestListAndGetBackfill:
    def test_list_get(self, client, workflow_store_handle):
        """Create one, list all, get by id — round-trip the same object."""
        _save_workflow(workflow_store_handle, _safe_pipeline())
        create = client.post("/api/executions/backfill", json={
            "pipeline_id": "wf-safe-001",
            "start_date": "2026-01-01",
            "end_date": "2026-01-02",
            "window_size": "daily",
        })
        assert create.status_code == 200, create.text
        backfill_id = create.json()["backfill_id"]

        listed = client.get("/api/executions/backfill")
        assert listed.status_code == 200
        ids = [b["id"] for b in listed.json()]
        assert backfill_id in ids

        fetched = client.get(f"/api/executions/backfill/{backfill_id}")
        assert fetched.status_code == 200
        body = fetched.json()
        assert body["backfill"]["id"] == backfill_id
        assert body["backfill"]["pipeline_id"] == "wf-safe-001"
        # 2 daily windows for Jan 1 → Jan 2 inclusive.
        assert len(body["windows"]) == 2


# ─────────────────────────────────────────────────────────────────────
# POST /api/schedules/ — sink-safety guardrail
# ─────────────────────────────────────────────────────────────────────


class TestCreateScheduleSinkSafety:
    def test_400_unsafe_schedule(self, client, workflow_store_handle):
        """email_sink pipeline + no acknowledgement → 400 with
        code=unsafe_for_schedule.

        Same migration caveat as ``test_400_unsafe_sink``: the saved
        workflow's sink is rewritten to a generic ``destination`` which
        with no mode classifies as ``append_risky`` — still unsafe."""
        _save_workflow(workflow_store_handle, _email_sink_pipeline())
        resp = client.post("/api/schedules/", json={
            "workflow_id": "wf-email-001",
            "schedule_type": "cron",
            "cron_expression": "0 9 * * *",
        })
        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"]
        assert detail["code"] == "unsafe_for_schedule"
        assert detail["unsafe_sinks"]
        assert any(u["step_id"] == "s2" for u in detail["unsafe_sinks"])

    def test_acknowledged_unsafe_schedule(self, client, workflow_store_handle):
        """Same pipeline + acknowledge_side_effects=true → 200."""
        _save_workflow(workflow_store_handle, _email_sink_pipeline())
        resp = client.post("/api/schedules/", json={
            "workflow_id": "wf-email-001",
            "schedule_type": "cron",
            "cron_expression": "0 9 * * *",
            "acknowledge_side_effects": True,
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["workflow_id"] == "wf-email-001"
        assert body["id"]
