"""
F-Pulse Test Configuration — shared fixtures for all tests.

Fixtures wire a real per-test SQLite Database into every store. The
older bare-store setup (Apr 16) left `store._db = None` and every
CRUD test AttributeError'd — the systemic "E1" failure flagged in
conftest_fixtures_v2.py. We fix it here at the shared-fixture level
so no test file needs to opt in, and so tests that use multiple
stores in one function (e.g. alert on workflow) see a consistent
shared database.

Design choices:
  - Function-scope Database: one fresh SQLite file per test → strict
    isolation, no cross-test pollution. ~50-100 ms per test; worth it
    for a clean slate each run.
  - Shared Database across stores in the same test: all store
    fixtures in a single test depend on the same _fpulse_test_db, so
    a workflow_store INSERT and an alert_store SELECT in the same
    test hit the same tables.
  - Path via pytest's built-in tmp_path: pytest handles cleanup, and
    every test gets a unique directory so even parallel xdist runs
    don't collide.
  - Stores constructed with db= in __init__ (not set_db after): the
    constructor path is what fpulse.main uses in production startup,
    so tests exercise the same code path as prod.

conftest_fixtures_v2.py remains untouched; tests that imported its
db_fixture / app_v2 / authed_client continue to work.
"""

import os
import sys
import tempfile
import pytest

# Ensure backend is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Set FPULSE_MODE=dev BEFORE any fpulse import so that conditional
# behaviours that latch at import time (e.g. /docs + /openapi.json
# endpoint registration in fpulse/main.py:1015) see the dev posture.
# Without this, tests like test_anonymous_access_blocked's
# test_public_endpoints_still_public[/docs] get 404 because the route
# was never registered. The old workaround (sys.modules wipe in
# conftest_fixtures_v2.py's db_fixture) was load-bearing for this AND
# for re-binding stores, but it caused ~37 test-isolation contamination
# failures (stale module references after the wipe orphaned singletons).
# Setting the env var here, before any import, is the cleaner fix.
os.environ.setdefault("FPULSE_MODE", "dev")


def pytest_configure(config):
    """Default --reruns 2 in CI so a transient flake doesn't red the gate.

    The parallel fast gate occasionally hits non-deterministic failures under
    load — xdist worker crashes from C-extension memory pressure, and
    import-order registration races — that pass cleanly on a re-run. We retry
    the failing test (on a fresh worker) up to twice; a test that fails all 3
    attempts is a real failure and still fails CI.

    Deliberately NOT in pytest.ini addopts: the security-scan job runs
    `pip install pytest` + `pytest --noconftest` WITHOUT pytest-rerunfailures,
    and a global `--reruns` addopt makes that pytest abort with "unrecognized
    arguments: --reruns". A conftest hook is skipped under --noconftest, so that
    job stays clean, while every normal run still gets the retry. Guarded on the
    plugin being installed (and on the caller not overriding) so it's a no-op
    when pytest-rerunfailures is absent.
    """
    if not config.pluginmanager.hasplugin("rerunfailures"):
        return
    if getattr(config.option, "reruns", None) in (None, 0):
        config.option.reruns = 2
        config.option.reruns_delay = 2


from fpulse.storage.database import Database
from fpulse.ir.schema import Workflow, Step, StepType, StepConnection, PipelineStatus
from fpulse.ir.versioning import WorkflowStore
from fpulse.ir.lifecycle import LifecycleStore
from fpulse.projects.models import Project
from fpulse.projects.store import ProjectStore
from fpulse.scheduling.models import Schedule, ScheduleType
from fpulse.scheduling.store import ScheduleStore
from fpulse.alerts.models import AlertRule, AlertLog, AlertChannel, AlertCondition
from fpulse.alerts.store import AlertStore
from fpulse.monitoring.store import ExecutionStore, ExecutionRecord, StepLog
from fpulse.auth.models import User
from fpulse.auth.store import UserStore
from fpulse.variables.models import Variable
from fpulse.variables.store import VariableStore
from fpulse.credentials.models import Credential
from fpulse.credentials.store import CredentialStore
from fpulse.connections.store import ConnectionStore
from fpulse.intelligence.schema_contract import SchemaContractStore
from fpulse.intelligence.schema_history import SchemaHistoryStore


# ── Shared DB ──

@pytest.fixture
def _fpulse_test_db(tmp_path):
    """Per-test SQLite Database with full schema + migrations.

    Returns a live fpulse.storage.database.Database bound to a file
    inside pytest's tmp_path. Teardown closes all cached connections
    and drains the WAL. The tempfile is cleaned up by pytest.

    Every store fixture in this file depends on _fpulse_test_db, so a
    single test function sees one database shared across all stores.
    """
    db = Database(str(tmp_path / "fpulse.db"))
    # Sprint 1 / Gate 1: the checkpoint_store module-level singleton is
    # wired to the production DB at app startup. Tests that exercise it
    # via the executor (or directly) need the singleton bound to the
    # per-test DB so writes don't bleed across tests. Reset on teardown
    # so a later non-checkpoint test sees a clean unwired store.
    try:
        from fpulse.engine.checkpoint_store import checkpoint_store
        checkpoint_store.set_db(db)
    except Exception:
        pass
    try:
        yield db
    finally:
        try:
            from fpulse.engine.checkpoint_store import checkpoint_store
            checkpoint_store.set_db(None)
        except Exception:
            pass
        try:
            db.close()
        except Exception:
            # If teardown fails (e.g. a test left a cursor open) we
            # don't want the fixture error to mask the test's real
            # failure. Tempfile goes with the tmp_path anyway.
            pass


# ── Store Fixtures ──

@pytest.fixture
def workflow_store(_fpulse_test_db):
    return WorkflowStore(db=_fpulse_test_db)


@pytest.fixture
def lifecycle_store(_fpulse_test_db):
    return LifecycleStore(db=_fpulse_test_db)


@pytest.fixture
def project_store(_fpulse_test_db):
    return ProjectStore(db=_fpulse_test_db)


@pytest.fixture
def schedule_store(_fpulse_test_db):
    return ScheduleStore(db=_fpulse_test_db)


@pytest.fixture
def alert_store(_fpulse_test_db):
    return AlertStore(db=_fpulse_test_db)


@pytest.fixture
def execution_store(_fpulse_test_db):
    return ExecutionStore(db=_fpulse_test_db)


@pytest.fixture
def user_store(_fpulse_test_db):
    return UserStore(db=_fpulse_test_db)


@pytest.fixture
def variable_store(_fpulse_test_db):
    return VariableStore(db=_fpulse_test_db)


@pytest.fixture
def credential_store(_fpulse_test_db, monkeypatch):
    # 2026-05-31: Disable encryption-at-rest for these unit tests so the
    # masking assertions (e.g. test_list_all_masks_secrets expects
    # "secret123" → "se***") are deterministic regardless of whether a
    # prior test triggered fpulse.main.lifespan, which populates
    # app_state["encryptor"] with a Fernet instance. monkeypatch reverts
    # automatically after each test, so encryption-on tests stay
    # encryption-on.
    try:
        from fpulse.main import app_state
        monkeypatch.setitem(app_state, "encryptor", None)
    except Exception:
        # main not imported yet — nothing to clear.
        pass
    return CredentialStore(db=_fpulse_test_db)


@pytest.fixture
def connection_store(_fpulse_test_db):
    return ConnectionStore(db=_fpulse_test_db)


@pytest.fixture
def datastore(_fpulse_test_db):
    """Y1 (2026-05-23): workspace datastore for the Storage page tests."""
    from fpulse.datastore.store import DataStore
    return DataStore(db=_fpulse_test_db)


@pytest.fixture
def contract_store(_fpulse_test_db):
    return SchemaContractStore(db=_fpulse_test_db)


@pytest.fixture
def schema_history_store(_fpulse_test_db):
    """2026-05-27 — append-only audit log of managed-table schema versions."""
    return SchemaHistoryStore(db=_fpulse_test_db)


# ── Data Fixtures ──

@pytest.fixture
def sample_workflow():
    """A simple CSV→Filter→Output workflow."""
    return Workflow(
        id="test-wf-001",
        name="Test Pipeline",
        description="A test pipeline",
        project_id="default",
        steps=[
            Step(id="s1", type=StepType.CSV_SOURCE, label="Load CSV",
                 params={"file_path": "orders.csv"}),
            Step(id="s2", type=StepType.FILTER, label="Filter Active",
                 params={"condition": "status = 'active'"}),
            Step(id="s3", type=StepType.OUTPUT, label="Save Output",
                 params={"format": "csv", "path": "output.csv"}),
        ],
        connections=[
            StepConnection(from_step="s1", to_step="s2"),
            StepConnection(from_step="s2", to_step="s3"),
        ],
    )


@pytest.fixture
def sample_project():
    return Project(
        id="proj-test-001",
        name="Test Project",
        description="A test project",
        owner="tester",
        color="#ff0000",
        icon="rocket",
    )


@pytest.fixture
def sample_schedule():
    return Schedule(
        id="sched-001",
        workflow_id="test-wf-001",
        project_id="default",
        name="Daily Run",
        schedule_type=ScheduleType.DAILY,
        daily_time="09:00",
        enabled=True,
    )


@pytest.fixture
def sample_alert_rule():
    return AlertRule(
        id="alert-001",
        name="Failure Alert",
        workflow_id="test-wf-001",
        project_id="default",
        condition=AlertCondition.ON_FAILURE,
        conditions=[AlertCondition.ON_FAILURE],
        channel=AlertChannel.EMAIL,
        email_addresses=["test@example.com"],
    )


@pytest.fixture
def sample_variable():
    return Variable(
        id="var-001",
        key="DB_HOST",
        value="localhost",
        type="string",
        scope="global",
    )


@pytest.fixture
def sample_credential():
    return Credential(
        id="cred-001",
        name="Test DB",
        type="postgresql",
        config={"host": "localhost", "port": 5432, "database": "test", "password": "secret123"},
    )


@pytest.fixture
def temp_data_dir():
    """Create a temporary data directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def sample_csv_file(temp_data_dir):
    """Create a sample CSV file for execution tests."""
    csv_path = os.path.join(temp_data_dir, "orders.csv")
    with open(csv_path, "w") as f:
        f.write("id,name,amount,status\n")
        f.write("1,Alice,100.00,active\n")
        f.write("2,Bob,200.00,inactive\n")
        f.write("3,Charlie,150.00,active\n")
        f.write("4,Diana,300.00,active\n")
        f.write("5,Eve,50.00,inactive\n")
    return csv_path
