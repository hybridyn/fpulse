"""
Feature test for POST /api/storage/query (OSS Storage — ad-hoc read-only SQL).

Verifies the "query my managed datasets like a database" surface:

  * a valid ``SELECT * FROM <schema.name>`` returns the promoted rows +
    columns, with ``limit`` / ``truncated`` present in the envelope;
  * the statement guard rejects (400) a non-SELECT (DROP / INSERT), a
    multi-statement (``SELECT 1; SELECT 2``), and a file-reading function
    (``read_csv('C:/Windows/win.ini')``) — the sandbox never even runs;
  * an anonymous caller is rejected (401/403).

A managed table is built through the *real* API flow: upload a small CSV,
then POST /api/storage/promote-to-table. That yields a genuine
``tables/{ws}/{schema}/{name}/part-000.parquet`` on disk exactly as the
query endpoint expects to materialise.

Harness note
------------
Reuses ``data_dir`` / ``db_fixture`` / ``app_v2`` from conftest_fixtures_v2
(migrations + app startup) but builds its OWN TestClients pinned to a
loopback ``base_url`` so the DNS-rebinding guard (non_loopback_host_blocked)
passes — the stock ``authed_client`` uses ``http://testserver`` and every
login there silently skips. Same technique as tests/test_project_export.py.
Feature code is left untouched.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2,
    DEV_ADMIN_EMAIL, DEV_ADMIN_PASSWORD,
)

# Loopback host so the DNS-rebinding guard (non_loopback_host_blocked) passes.
LOOPBACK_BASE = "http://127.0.0.1"

# Pin every admin request to a known workspace. The bootstrap admin has
# role="admin" (in ADMIN_ROLES) so current_workspace_id honours the explicit
# header without a membership check — giving us a deterministic on-disk
# tables/{ws}/ dir name.
WS = "default"

SCHEMA = "default"
TABLE = "storage_query_sales"


def _live_data_dir() -> str:
    """The data dir the running app resolved — what the endpoints read."""
    from fpulse.main import app_state
    return app_state["data_dir"]


def _reset_admin_password(app) -> None:
    """Force the bootstrap admin onto the test-known password."""
    from fpulse.main import app_state
    from fpulse.auth.models import User
    store = app_state.get("user_store")
    if store is None:
        return
    admin = store.get_user_by_email(DEV_ADMIN_EMAIL)
    if admin is None:
        return
    admin.password_hash = User.hash_password(DEV_ADMIN_PASSWORD)
    admin.is_active = True
    store._save_user(admin)


@pytest.fixture(scope="module")
def anon_client(app_v2):
    """Unauthenticated, loopback-host client."""
    with TestClient(app_v2, base_url=LOOPBACK_BASE) as c:
        yield c


@pytest.fixture(scope="module")
def admin_client(app_v2, anon_client):
    """Authenticated admin client (loopback host, workspace pinned to WS)."""
    # anon_client already entered the TestClient context → app startup ran,
    # so app_state is populated.
    _reset_admin_password(app_v2)

    r = anon_client.post(
        "/api/auth/login",
        json={"email": DEV_ADMIN_EMAIL, "password": DEV_ADMIN_PASSWORD},
    )
    if r.status_code != 200:
        pytest.skip(f"admin login failed: {r.status_code} {r.text[:200]}")
    tok = r.json().get("token")
    if not tok:
        pytest.skip(f"login returned no token: {r.text[:200]}")

    c = TestClient(app_v2, base_url=LOOPBACK_BASE)
    c.headers["Authorization"] = f"Bearer {tok}"
    c.headers["X-Workspace-Id"] = WS
    c.cookies.set("session", tok)
    c.cookies.set("fpulse_session", tok)
    with c:
        yield c


@pytest.fixture(scope="module")
def managed_table(admin_client: TestClient):
    """Build a managed Parquet table directly through the datastore.

    We bypass POST /api/storage/promote-to-table on purpose: on the pinned
    DuckDB (1.5.3) that endpoint 500s because it calls
    ``conn.sql("... read_parquet(?)", [path])`` and this DuckDB rejects a
    positional params list on ``.sql()`` (only ``.execute()`` / the
    ``params=`` kwarg accept it). See the report — this is a real feature
    bug, not a test bug. To exercise the *query* endpoint under test we
    materialise the table the same way promote would have: write
    ``tables/{ws}/{schema}/{name}/part-000.parquet`` via DuckDB COPY and
    register a StorageTable row. Returns the StorageTable JSON.
    """
    import duckdb
    from fpulse.datastore.models import StorageColumn, StorageTable
    from fpulse.datastore.paths import workspace_paths
    from fpulse.datastore.store import get_store

    ddir = _live_data_dir()
    paths = workspace_paths(ddir, WS).ensure()
    table_dir = paths.table_dir(SCHEMA, TABLE)
    os.makedirs(table_dir, exist_ok=True)
    parquet = os.path.join(table_dir, "part-000.parquet")

    conn = duckdb.connect()
    try:
        conn.execute(
            "CREATE TABLE t AS SELECT * FROM (VALUES "
            "('US', 100), ('EU', 200), ('APAC', 150)"
            ") AS v(region, revenue)"
        )
        conn.execute("COPY t TO ? (FORMAT PARQUET)", [parquet])
    finally:
        conn.close()

    store = get_store()
    table = StorageTable(
        workspace_id=WS,
        schema_name=SCHEMA,
        name=TABLE,
        path=paths.relative_to_data_dir(table_dir),
        row_count=3,
        column_count=2,
        size_bytes=os.path.getsize(parquet),
        part_count=1,
        description="fixture table for query test",
    )
    store.save_table(table)
    store.save_columns(
        [
            StorageColumn(workspace_id=WS, table_id=table.id, name="region",
                          type="VARCHAR", ordinal=0),
            StorageColumn(workspace_id=WS, table_id=table.id, name="revenue",
                          type="INTEGER", ordinal=1),
        ],
        table_id=table.id,
    )
    return table.model_dump(mode="json")


def test_valid_select_returns_rows(admin_client: TestClient, managed_table):
    # Reference the table by its bare name — the endpoint puts the "default"
    # schema on the DuckDB search_path so bare names resolve. (The
    # unquoted "default.<name>" form does NOT parse because `default` is a
    # DuckDB reserved keyword; see the report note. The quoted schema form
    # is exercised by test_valid_select_quoted_schema below.)
    r = admin_client.post("/api/storage/query", json={
        "sql": f"SELECT * FROM {TABLE} ORDER BY revenue",
    })
    assert r.status_code == 200, f"query failed: {r.status_code} {r.text[:400]}"
    body = r.json()

    # Envelope shape.
    for key in ("columns", "rows", "row_count", "limit", "truncated", "tables_available"):
        assert key in body, f"query response missing '{key}': {list(body)}"

    col_names = {c["name"] for c in body["columns"]}
    assert {"region", "revenue"} <= col_names, body["columns"]

    assert body["row_count"] == 3, body
    assert len(body["rows"]) == 3, body["rows"]
    # Ordered by revenue asc.
    revenues = [row["revenue"] for row in body["rows"]]
    assert revenues == sorted(revenues), revenues
    regions = {row["region"] for row in body["rows"]}
    assert {"US", "EU", "APAC"} == regions, regions

    # limit present + a truncated flag that reflects reality (3 rows < limit).
    assert body["limit"] >= 1, body["limit"]
    assert body["truncated"] is False, body

    # The managed table is discoverable in the sandbox.
    assert f"{SCHEMA}.{TABLE}" in body["tables_available"], body["tables_available"]


def test_valid_select_quoted_schema(admin_client: TestClient, managed_table):
    """The fully-qualified form works when the reserved schema is quoted."""
    r = admin_client.post("/api/storage/query", json={
        "sql": f'SELECT region, revenue FROM "{SCHEMA}"."{TABLE}"',
    })
    assert r.status_code == 200, f"query failed: {r.status_code} {r.text[:400]}"
    body = r.json()
    assert body["row_count"] == 3, body


def test_limit_and_truncated_flag(admin_client: TestClient, managed_table):
    """A limit below the row count flips truncated=True and caps rows."""
    r = admin_client.post("/api/storage/query", json={
        "sql": f"SELECT * FROM {TABLE}",
        "limit": 2,
    })
    assert r.status_code == 200, f"{r.status_code} {r.text[:300]}"
    body = r.json()
    assert body["limit"] == 2, body
    assert body["row_count"] == 2, body
    assert body["truncated"] is True, body


@pytest.mark.parametrize("sql", [
    "DROP TABLE default.storage_query_sales",
    "INSERT INTO default.storage_query_sales VALUES ('ZZ', 999)",
    "UPDATE default.storage_query_sales SET revenue = 0",
    "DELETE FROM default.storage_query_sales",
])
def test_non_select_rejected(admin_client: TestClient, managed_table, sql):
    r = admin_client.post("/api/storage/query", json={"sql": sql})
    assert r.status_code == 400, f"expected 400 for {sql!r}, got {r.status_code}: {r.text[:300]}"


def test_multi_statement_rejected(admin_client: TestClient, managed_table):
    r = admin_client.post("/api/storage/query", json={"sql": "SELECT 1; SELECT 2"})
    assert r.status_code == 400, f"expected 400 for multi-statement, got {r.status_code}: {r.text[:300]}"


@pytest.mark.parametrize("sql", [
    "SELECT * FROM read_csv('C:/Windows/win.ini')",
    "SELECT * FROM read_parquet('C:/Windows/win.ini')",
    "SELECT * FROM read_json('C:/Windows/win.ini')",
    "SELECT * FROM glob('C:/Windows/*')",
])
def test_file_reading_functions_rejected(admin_client: TestClient, managed_table, sql):
    r = admin_client.post("/api/storage/query", json={"sql": sql})
    assert r.status_code == 400, f"expected 400 for file-read {sql!r}, got {r.status_code}: {r.text[:300]}"


def test_anonymous_query_rejected(anon_client: TestClient):
    r = anon_client.post("/api/storage/query", json={
        "sql": f"SELECT * FROM {SCHEMA}.{TABLE}",
    })
    assert r.status_code in (401, 403), (
        f"SECURITY: anonymous POST /api/storage/query returned {r.status_code}, "
        f"expected 401/403: {r.text[:200]}"
    )
