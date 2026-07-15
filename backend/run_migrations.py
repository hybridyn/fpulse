"""One-shot: open the SQLite DB so Database.__init__ runs every
pending schema migration. Idempotent — already-applied migrations
skip themselves via the _meta.schema_version marker.

Usage from D:\\Siva\\hybridyn-f-pulse\\backend:
    set FPULSE_DATA_DIR=D:\\Siva\\hybridyn-f-pulse\\data\\samples
    python run_migrations.py

Why this exists: when the running uvicorn worker doesn't reload
database.py (e.g. --reload is off, or the reloader is confused),
a new schema version added to the source code isn't applied until
the worker is restarted. This script performs the apply from a
separate process without touching the live worker — the live
worker's SQLite connection keeps working because SQLite handles
concurrent writers via its default locking.
"""
from __future__ import annotations

import os


def main() -> None:
    data_dir = os.environ.get(
        "FPULSE_DATA_DIR",
        os.path.join(os.getcwd(), "data"),
    )
    db_path = os.path.join(data_dir, "fpulse.db")
    print(f"DB: {db_path}")
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Inspect current state
    cols = conn.execute("PRAGMA table_info(workflow_versions)").fetchall()
    col_names = [c["name"] for c in cols]
    print(f"workflow_versions columns before: {col_names}")

    if "workspace_id" not in col_names:
        print("adding workspace_id column to workflow_versions...")
        conn.execute(
            "ALTER TABLE workflow_versions ADD COLUMN workspace_id TEXT DEFAULT 'default'"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_wv_workspace ON workflow_versions(workspace_id)"
        )
        conn.execute(
            "UPDATE workflow_versions SET workspace_id = 'default' WHERE workspace_id IS NULL OR workspace_id = ''"
        )
        conn.commit()
        print("  done")
    else:
        print("workspace_id already present — skipping")

    # Back-fill JSON blobs
    import json as _json
    rows = conn.execute(
        "SELECT workflow_id, version, data FROM workflow_versions"
    ).fetchall()
    patched = 0
    for r in rows:
        try:
            blob = _json.loads(r["data"])
            wf = blob.get("workflow") or {}
            if wf.get("workspace_id"):
                continue
            wf["workspace_id"] = "default"
            blob["workflow"] = wf
            conn.execute(
                "UPDATE workflow_versions SET data = ? WHERE workflow_id = ? AND version = ?",
                (_json.dumps(blob), r["workflow_id"], r["version"]),
            )
            patched += 1
        except Exception as exc:
            print(f"  skip {r['workflow_id']}/{r['version']}: {exc}")
    conn.commit()
    print(f"back-filled {patched} JSON blobs with workspace_id='default'")

    # ── v6: connections workspace_id ──
    cols = conn.execute("PRAGMA table_info(connections)").fetchall()
    col_names = [c["name"] for c in cols]
    print(f"connections columns before: {col_names}")

    if "workspace_id" not in col_names:
        print("adding workspace_id column to connections...")
        conn.execute(
            "ALTER TABLE connections ADD COLUMN workspace_id TEXT DEFAULT 'default'"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_connections_workspace ON connections(workspace_id)"
        )
        conn.execute(
            "UPDATE connections SET workspace_id = 'default' WHERE workspace_id IS NULL OR workspace_id = ''"
        )
        conn.commit()
        print("  done")
    else:
        print("connections.workspace_id already present — skipping")

    # Back-fill connection JSON blobs (data IS the Connection dict at
    # the top level — no nested unwrap like workflow_versions).
    c_rows = conn.execute("SELECT id, data FROM connections").fetchall()
    c_patched = 0
    for r in c_rows:
        try:
            blob = _json.loads(r["data"])
            if blob.get("workspace_id"):
                continue
            blob["workspace_id"] = "default"
            conn.execute(
                "UPDATE connections SET data = ? WHERE id = ?",
                (_json.dumps(blob), r["id"]),
            )
            c_patched += 1
        except Exception as exc:
            print(f"  skip connection {r['id']}: {exc}")
    conn.commit()
    print(f"back-filled {c_patched} connection JSON blobs with workspace_id='default'")

    c_cols_after = conn.execute("PRAGMA table_info(connections)").fetchall()
    print(f"connections columns after: {[c['name'] for c in c_cols_after]}")
    c_total = conn.execute("SELECT COUNT(*) AS c FROM connections").fetchone()
    c_scoped = conn.execute(
        "SELECT COUNT(*) AS c FROM connections WHERE workspace_id = 'default'"
    ).fetchone()
    print(f"connections: total={c_total['c']}, scoped_to_default={c_scoped['c']}")

    # ── v7: credentials workspace_id ──
    cr_cols = conn.execute("PRAGMA table_info(credentials)").fetchall()
    cr_col_names = [c["name"] for c in cr_cols]
    print(f"credentials columns before: {cr_col_names}")

    if "workspace_id" not in cr_col_names:
        print("adding workspace_id column to credentials...")
        conn.execute(
            "ALTER TABLE credentials ADD COLUMN workspace_id TEXT DEFAULT 'default'"
        )
        conn.commit()
        print("  done")
    else:
        print("credentials.workspace_id already present — skipping")

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_credentials_workspace ON credentials(workspace_id)"
    )
    conn.execute(
        "UPDATE credentials SET workspace_id = 'default' WHERE workspace_id IS NULL OR workspace_id = ''"
    )
    conn.commit()

    cr_rows = conn.execute("SELECT id, data FROM credentials").fetchall()
    cr_patched = 0
    for r in cr_rows:
        try:
            blob = _json.loads(r["data"])
            if blob.get("workspace_id"):
                continue
            blob["workspace_id"] = "default"
            conn.execute(
                "UPDATE credentials SET data = ? WHERE id = ?",
                (_json.dumps(blob), r["id"]),
            )
            cr_patched += 1
        except Exception as exc:
            print(f"  skip credential {r['id']}: {exc}")
    conn.commit()
    print(f"back-filled {cr_patched} credential JSON blobs with workspace_id='default'")

    cr_cols_after = conn.execute("PRAGMA table_info(credentials)").fetchall()
    print(f"credentials columns after: {[c['name'] for c in cr_cols_after]}")
    cr_total = conn.execute("SELECT COUNT(*) AS c FROM credentials").fetchone()
    cr_scoped = conn.execute(
        "SELECT COUNT(*) AS c FROM credentials WHERE workspace_id = 'default'"
    ).fetchone()
    print(f"credentials: total={cr_total['c']}, scoped_to_default={cr_scoped['c']}")

    # ── v8: schedules workspace_id ──
    s_cols = conn.execute("PRAGMA table_info(schedules)").fetchall()
    s_col_names = [c["name"] for c in s_cols]
    print(f"schedules columns before: {s_col_names}")

    if "workspace_id" not in s_col_names:
        print("adding workspace_id column to schedules...")
        conn.execute(
            "ALTER TABLE schedules ADD COLUMN workspace_id TEXT DEFAULT 'default'"
        )
        conn.commit()
        print("  done")
    else:
        print("schedules.workspace_id already present — skipping")

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_schedules_workspace ON schedules(workspace_id)"
    )
    conn.execute(
        "UPDATE schedules SET workspace_id = 'default' WHERE workspace_id IS NULL OR workspace_id = ''"
    )
    conn.commit()

    s_rows = conn.execute("SELECT id, data FROM schedules").fetchall()
    s_patched = 0
    for r in s_rows:
        try:
            blob = _json.loads(r["data"])
            if blob.get("workspace_id"):
                continue
            blob["workspace_id"] = "default"
            conn.execute(
                "UPDATE schedules SET data = ? WHERE id = ?",
                (_json.dumps(blob), r["id"]),
            )
            s_patched += 1
        except Exception as exc:
            print(f"  skip schedule {r['id']}: {exc}")
    conn.commit()
    print(f"back-filled {s_patched} schedule JSON blobs with workspace_id='default'")

    s_cols_after = conn.execute("PRAGMA table_info(schedules)").fetchall()
    print(f"schedules columns after: {[c['name'] for c in s_cols_after]}")
    s_total = conn.execute("SELECT COUNT(*) AS c FROM schedules").fetchone()
    s_scoped = conn.execute(
        "SELECT COUNT(*) AS c FROM schedules WHERE workspace_id = 'default'"
    ).fetchone()
    print(f"schedules: total={s_total['c']}, scoped_to_default={s_scoped['c']}")

    # ── v9-v13: generic workspace_id back-fill for the remaining tables ──
    #
    # v9  = alert_rules      (also alert_logs — audit)
    # v10 = executions       (monitoring audit, back-fill from workflow)
    # v11 = variables        (scope=global is per-workspace)
    # v12 = lifecycle_events (audit, back-fill from workflow)
    # v13 = schema_contracts (back-fill from workflow)
    remaining_tables = [
        ("alert_rules", "idx_alert_rules_workspace"),
        ("alert_logs", "idx_alert_logs_workspace"),
        ("executions", "idx_executions_workspace"),
        ("variables", "idx_variables_workspace"),
        ("lifecycle_events", "idx_lifecycle_workspace_new"),
        ("schema_contracts", "idx_contracts_workspace"),
    ]
    for t_name, idx_name in remaining_tables:
        # Table may not exist on very old DBs — skip if so
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (t_name,),
        ).fetchone()
        if not exists:
            print(f"skip {t_name} — table does not exist")
            continue

        t_cols = conn.execute(f"PRAGMA table_info({t_name})").fetchall()
        t_col_names = [c["name"] for c in t_cols]
        print(f"{t_name} columns before: {t_col_names}")

        if "workspace_id" not in t_col_names:
            print(f"adding workspace_id column to {t_name}...")
            try:
                conn.execute(
                    f"ALTER TABLE {t_name} ADD COLUMN workspace_id TEXT DEFAULT 'default'"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
            conn.commit()

        conn.execute(
            f"CREATE INDEX IF NOT EXISTS {idx_name} ON {t_name}(workspace_id)"
        )
        conn.execute(
            f"UPDATE {t_name} SET workspace_id = 'default' WHERE workspace_id IS NULL OR workspace_id = ''"
        )
        conn.commit()

        # Back-fill JSON blobs
        t_rows = conn.execute(f"SELECT id, data FROM {t_name}").fetchall()
        t_patched = 0
        for r in t_rows:
            try:
                blob = _json.loads(r["data"])
                if blob.get("workspace_id"):
                    continue
                blob["workspace_id"] = "default"
                conn.execute(
                    f"UPDATE {t_name} SET data = ? WHERE id = ?",
                    (_json.dumps(blob), r["id"]),
                )
                t_patched += 1
            except Exception as exc:
                print(f"  skip {t_name} {r['id']}: {exc}")
        conn.commit()
        print(f"back-filled {t_patched} {t_name} JSON blobs")

        t_total = conn.execute(f"SELECT COUNT(*) AS c FROM {t_name}").fetchone()
        t_scoped = conn.execute(
            f"SELECT COUNT(*) AS c FROM {t_name} WHERE workspace_id = 'default'"
        ).fetchone()
        print(f"{t_name}: total={t_total['c']}, scoped_to_default={t_scoped['c']}")

    # For executions, lifecycle_events, and schema_contracts, also
    # back-fill by joining to workflow_versions so audit records
    # inherit the correct workspace from their parent workflow rather
    # than the blanket 'default'.
    for t_name in ("executions", "lifecycle_events", "schema_contracts"):
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (t_name,),
        ).fetchone()
        if not exists:
            continue
        try:
            before = conn.execute(
                f"SELECT COUNT(*) AS c FROM {t_name} WHERE workspace_id = 'default'"
            ).fetchone()["c"]
            conn.execute(f"""
                UPDATE {t_name}
                SET workspace_id = COALESCE(
                    (SELECT wv.workspace_id
                     FROM workflow_versions wv
                     WHERE wv.workflow_id = {t_name}.workflow_id
                     ORDER BY wv.version DESC LIMIT 1),
                    'default'
                )
                WHERE workspace_id = 'default' OR workspace_id IS NULL
            """)
            conn.commit()
            after = conn.execute(
                f"SELECT COUNT(*) AS c FROM {t_name} WHERE workspace_id = 'default'"
            ).fetchone()["c"]
            print(f"{t_name}: re-mapped {before - after} rows from 'default' to parent-workflow workspace")
        except sqlite3.OperationalError as exc:
            print(f"  {t_name} parent-map skipped: {exc}")

    # Bump schema_version marker so the backend doesn't re-run v7-v13
    conn.execute(
        "INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', '13')"
    )
    conn.commit()

    # Sanity check
    cols = conn.execute("PRAGMA table_info(workflow_versions)").fetchall()
    col_names = [c["name"] for c in cols]
    print(f"workflow_versions columns after: {col_names}")
    total = conn.execute("SELECT COUNT(*) AS c FROM workflow_versions").fetchone()
    scoped = conn.execute(
        "SELECT COUNT(*) AS c FROM workflow_versions WHERE workspace_id = 'default'"
    ).fetchone()
    print(f"workflow_versions: total={total['c']}, scoped_to_default={scoped['c']}")

    # Simulate the new scoped list_all query to confirm it works
    scoped_rows = conn.execute("""
        SELECT wv.workflow_id FROM workflow_versions wv
        INNER JOIN (
            SELECT workflow_id, MAX(version) as max_v
            FROM workflow_versions GROUP BY workflow_id
        ) latest ON wv.workflow_id = latest.workflow_id AND wv.version = latest.max_v
        WHERE wv.workspace_id = ?
    """, ("default",)).fetchall()
    print(f"scoped list_all(workspace_id='default') returns {len(scoped_rows)} workflows")
    other = conn.execute("""
        SELECT wv.workflow_id FROM workflow_versions wv
        INNER JOIN (
            SELECT workflow_id, MAX(version) as max_v
            FROM workflow_versions GROUP BY workflow_id
        ) latest ON wv.workflow_id = latest.workflow_id AND wv.version = latest.max_v
        WHERE wv.workspace_id = ?
    """, ("other-ws",)).fetchall()
    print(f"scoped list_all(workspace_id='other-ws') returns {len(other)} workflows (should be 0)")

    conn.close()
    print("OK")


if __name__ == "__main__":
    main()
