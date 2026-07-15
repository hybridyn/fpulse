"""One-shot: enrol the seeded admin into the Default workspace.

Run this when the backend isn't picking up the auth.py self-heal
(e.g. when --reload isn't active and a manual restart isn't possible).
Idempotent — re-running is a no-op.

Usage from D:\\Siva\\hybridyn-f-pulse\\backend:
    set FPULSE_DATA_DIR=D:\\Siva\\hybridyn-f-pulse\\data\\samples
    python enrol_admin.py
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone


def main() -> None:
    data_dir = os.environ.get(
        "FPULSE_DATA_DIR",
        os.path.join(os.getcwd(), "data"),
    )
    db_path = os.path.join(data_dir, "fpulse.db")
    if not os.path.exists(db_path):
        print(f"NO DB at {db_path}")
        return

    now = datetime.now(timezone.utc).isoformat()
    print(f"DB: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # Diagnostics
        all_ws = conn.execute("SELECT id, name FROM workspaces").fetchall()
        print(f"workspaces in db: {[dict(r) for r in all_ws]}")
        all_members = conn.execute(
            "SELECT workspace_id, user_id, role FROM workspace_members"
        ).fetchall()
        print(f"members in db: {[dict(r) for r in all_members]}")
        sv = conn.execute(
            "SELECT value FROM _meta WHERE key = 'schema_version'"
        ).fetchone()
        print(f"schema_version: {sv['value'] if sv else None}")

        admin = conn.execute(
            "SELECT id FROM users WHERE id = 'admin'"
        ).fetchone()
        if not admin:
            print("NO admin user — nothing to enrol")
            return
        ws = conn.execute(
            "SELECT id FROM workspaces WHERE id = 'default'"
        ).fetchone()
        if not ws:
            # Create it now with the same shape as the v2 migration.
            import json as _json
            data = {
                "id": "default",
                "name": "Default",
                "slug": "default",
                "plan": "free",
                "is_personal": 0,
                "owner_id": "admin",
                "domain_allowlist": [],
                "settings": {},
                "created_at": now,
                "updated_at": now,
            }
            conn.execute(
                """INSERT INTO workspaces
                   (id, name, slug, plan, is_personal, owner_id, domain_allowlist, settings, data, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "default", "Default", "default", "free", 0, "admin",
                    "[]", "{}", _json.dumps(data), now, now,
                ),
            )
            conn.commit()
            print("created Default workspace")
        existing = conn.execute(
            "SELECT 1 FROM workspace_members WHERE workspace_id = 'default' AND user_id = 'admin'"
        ).fetchone()
        if existing:
            print("admin already enrolled in default — no-op")
            return
        conn.execute(
            """INSERT INTO workspace_members
               (workspace_id, user_id, role, invited_by, invited_at, accepted_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("default", "admin", "super_admin", "system", now, now),
        )
        conn.commit()
        print("OK: enrolled admin in default as super_admin")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
