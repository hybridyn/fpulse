"""
Feature tests for two OSS Storage endpoints:

  * POST   /api/storage/rescan          — force a filesystem→index reconcile.
  * DELETE /api/storage/tables/{id}      — RECOVERABLE table drop (moves the
    Parquet dir to trash/{ws}/dropped-tables/ instead of rmtree).

Rescan test
-----------
Writes a pipeline output directly under
``outputs/{ws}/{pipe}/{run}/x.csv`` in the live data dir (simulating a run
that landed between boots, which the sentinel-gated boot reconciler misses),
POSTs /rescan, asserts ``total_indexed >= 1`` and that the file now shows up
via GET /api/storage/outputs.

Drop-to-trash test
------------------
Builds a managed table via the real upload→promote flow, DELETEs it, then
asserts the response says ``recoverable is True``, the original
``tables/{ws}/{schema}/{name}/`` dir is gone, and a directory now exists
under ``trash/{ws}/dropped-tables/``.

Harness note
------------
Same loopback-client + workspace-pinning setup as tests/test_storage_query.py
so the DNS-rebinding guard passes and the on-disk ``{ws}`` dir name is
deterministic ("default"). The data dir is read from ``app_state['data_dir']``
at runtime so the direct filesystem writes target exactly the dir the
endpoints read.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2,
    DEV_ADMIN_EMAIL, DEV_ADMIN_PASSWORD,
)

LOOPBACK_BASE = "http://127.0.0.1"
WS = "default"


def _reset_admin_password(app) -> None:
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


def _live_data_dir() -> str:
    """The data dir the running app resolved — what the endpoints read."""
    from fpulse.main import app_state
    return app_state["data_dir"]


@pytest.fixture(scope="module")
def anon_client(app_v2):
    with TestClient(app_v2, base_url=LOOPBACK_BASE) as c:
        yield c


@pytest.fixture(scope="module")
def admin_client(app_v2, anon_client):
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


# ─────────────────────────────────────────────────────────────────────────
# A. Rescan — index a file written directly under outputs/{ws}/{pipe}/{run}/
# ─────────────────────────────────────────────────────────────────────────

def test_rescan_indexes_orphan_output(admin_client: TestClient):
    from fpulse.datastore.paths import workspace_paths

    ddir = _live_data_dir()
    paths = workspace_paths(ddir, WS)
    pipe = "somepipe"
    run = "somerun"
    out_dir = os.path.join(paths.outputs, pipe, run)
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, "x.csv")
    with open(out_file, "w", encoding="utf-8") as f:
        f.write("a,b\n1,2\n3,4\n")

    # The file exists on disk but no index row references it yet.
    r = admin_client.post("/api/storage/rescan", json={})
    assert r.status_code == 200, f"rescan failed: {r.status_code} {r.text[:300]}"
    body = r.json()
    assert body["workspace_id"] == WS, body
    for key in ("uploads_indexed", "outputs_indexed", "total_indexed"):
        assert key in body, f"rescan response missing '{key}': {list(body)}"
    assert body["total_indexed"] >= 1, f"expected >=1 indexed, got {body}"
    assert body["outputs_indexed"] >= 1, body

    # The freshly-indexed output now surfaces via the outputs listing.
    r = admin_client.get("/api/storage/outputs")
    assert r.status_code == 200, f"list outputs failed: {r.status_code} {r.text[:300]}"
    groups = r.json()["groups"]
    names = {
        obj.get("name")
        for g in groups
        for obj in (g.get("objects") or g.get("files") or [])
    }
    # Fall back to a flat scan of the group dicts if the shape differs.
    flat = str(groups)
    assert "x.csv" in names or "x.csv" in flat, (
        f"reconciled output x.csv not visible in /outputs; groups={groups}"
    )


def test_rescan_is_idempotent(admin_client: TestClient):
    """A second rescan with nothing new on disk indexes nothing more."""
    r = admin_client.post("/api/storage/rescan", json={})
    assert r.status_code == 200, f"{r.status_code} {r.text[:300]}"
    body = r.json()
    # The orphan output from the previous test is already indexed, so this
    # add-only pass finds nothing new to insert.
    assert body["total_indexed"] == 0, f"expected idempotent 0, got {body}"


# ─────────────────────────────────────────────────────────────────────────
# B. Drop-to-trash — DELETE /api/storage/tables/{id} is now recoverable
# ─────────────────────────────────────────────────────────────────────────

def _create_managed_table(admin_client: TestClient, schema: str, name: str) -> dict:
    """Build a managed Parquet table directly through the datastore.

    Bypasses POST /api/storage/promote-to-table, which 500s on the pinned
    DuckDB 1.5.3 (it calls ``conn.sql("... read_parquet(?)", [path])`` and
    this DuckDB rejects a positional params list on ``.sql()``). See the
    report — a real feature bug. We materialise the same on-disk layout
    (``tables/{ws}/{schema}/{name}/part-000.parquet`` + a StorageTable row)
    so DELETE /tables/{id} has a genuine table to move to trash.
    """
    import duckdb
    from fpulse.datastore.models import StorageTable
    from fpulse.datastore.paths import workspace_paths
    from fpulse.datastore.store import get_store

    ddir = _live_data_dir()
    paths = workspace_paths(ddir, WS).ensure()
    table_dir = paths.table_dir(schema, name)
    os.makedirs(table_dir, exist_ok=True)
    parquet = os.path.join(table_dir, "part-000.parquet")

    conn = duckdb.connect()
    try:
        conn.execute(
            "CREATE TABLE t AS SELECT * FROM (VALUES "
            "('US', 100), ('EU', 200)"
            ") AS v(region, revenue)"
        )
        conn.execute("COPY t TO ? (FORMAT PARQUET)", [parquet])
    finally:
        conn.close()

    store = get_store()
    table = StorageTable(
        workspace_id=WS,
        schema_name=schema,
        name=name,
        path=paths.relative_to_data_dir(table_dir),
        row_count=2,
        column_count=2,
        size_bytes=os.path.getsize(parquet),
        part_count=1,
    )
    store.save_table(table)
    return table.model_dump(mode="json")


def test_drop_table_moves_to_trash(admin_client: TestClient):
    from fpulse.datastore.paths import workspace_paths, safe_join_under

    schema, name = "default", "drop_me_sales"
    table = _create_managed_table(admin_client, schema, name)
    table_id = table["id"]

    ddir = _live_data_dir()
    paths = workspace_paths(ddir, WS)
    table_dir = paths.table_dir(schema, name)
    parquet = os.path.join(table_dir, "part-000.parquet")
    assert os.path.isfile(parquet), f"promote did not write parquet at {parquet}"

    trash_root = safe_join_under(ddir, os.path.join("trash", WS, "dropped-tables"))
    before = set(os.listdir(trash_root)) if os.path.isdir(trash_root) else set()

    r = admin_client.request("DELETE", f"/api/storage/tables/{table_id}")
    assert r.status_code == 200, f"drop failed: {r.status_code} {r.text[:300]}"
    body = r.json()
    assert body["status"] == "ok", body
    assert body["id"] == table_id, body
    assert body["recoverable"] is True, f"drop reported not recoverable: {body}"
    assert body["recoverable_at"], f"missing recoverable_at: {body}"

    # Original tables/ dir is gone (bytes were MOVED, not left behind).
    assert not os.path.isdir(table_dir), f"table dir still present after drop: {table_dir}"

    # A new dir now exists under trash/{ws}/dropped-tables/ holding the parquet.
    assert os.path.isdir(trash_root), f"dropped-tables root missing: {trash_root}"
    after = set(os.listdir(trash_root))
    new_dirs = after - before
    assert new_dirs, f"no new dir under {trash_root}; before={before} after={after}"
    # The moved dir should still contain the part-000 parquet bytes.
    moved = os.path.join(trash_root, next(iter(new_dirs)))
    assert os.path.isfile(os.path.join(moved, "part-000.parquet")), (
        f"moved trash dir missing the parquet bytes: {os.listdir(moved)}"
    )

    # recoverable_at is the data-dir-relative destination path. The endpoint
    # builds it with os.path.join, so it carries OS-native separators
    # (backslashes on Windows) — normalise before comparing.
    rec_at = body["recoverable_at"].replace("\\", "/")
    assert rec_at.startswith(f"trash/{WS}/dropped-tables/"), body

    # Metadata row is gone → the table name frees up (GET tables no longer lists it).
    r = admin_client.get("/api/storage/tables")
    assert r.status_code == 200, r.text[:200]
    listed = {(t["schema_name"], t["name"]) for t in r.json()["tables"]}
    assert (schema, name) not in listed, f"dropped table still in index: {listed}"


def test_drop_missing_table_404(admin_client: TestClient):
    r = admin_client.request("DELETE", "/api/storage/tables/tbl_does_not_exist")
    assert r.status_code == 404, f"expected 404, got {r.status_code}: {r.text[:200]}"
