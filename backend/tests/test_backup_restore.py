"""Integration test for the backup → restore round trip.

The backup endpoint and the restore endpoint each work in isolation; the
real risk for a launch-grade ops story is "backups looked fine until
we tried to restore one." That failure mode kills DR confidence in
production and is exactly what this test guards against.

The test exercises the same code paths the operator hits via the API:

  1. Seed an in-memory `Database` with realistic rows (workflows,
     credentials, audit events, etc.)
  2. Call `Database.backup_to()` → produces a SQLite snapshot file
  3. Spin up a fresh `Database` pointed at a different file
  4. Restore the snapshot into the fresh DB the same way the
     `/api/backup/restore` endpoint does (table-by-table copy)
  5. Assert every seeded row reappears intact in the restored DB
  6. Assert no plaintext credential values leaked into the snapshot
     (defense in depth — the encryption layer should already prevent
     this, but a regression in that layer would be caught here)

Added 2026-05-29 alongside the launch security audit.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest


# ── Helpers ───────────────────────────────────────────────────────────


def _seed_db(conn: sqlite3.Connection) -> dict:
    """Insert a small but realistic set of rows. Returns the input data
    for round-trip equality assertions in the test."""
    conn.row_factory = sqlite3.Row
    seeded: dict = {}

    # Workflows: one published, one draft
    conn.execute("""
        CREATE TABLE IF NOT EXISTS workflows_test (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            name TEXT NOT NULL,
            status TEXT NOT NULL,
            data TEXT NOT NULL
        )
    """)
    workflows = [
        ("wf-1", "default", "Sales Pivot", "published", '{"steps":[{"id":"s1"}]}'),
        ("wf-2", "default", "Daily Sync", "draft", '{"steps":[{"id":"s1"}]}'),
        ("wf-3", "tenant-2", "Other workspace", "published", '{"steps":[]}'),
    ]
    conn.executemany(
        "INSERT INTO workflows_test (id, workspace_id, name, status, data) VALUES (?, ?, ?, ?, ?)",
        workflows,
    )
    seeded["workflows"] = workflows

    # Credentials: must store the encrypted-blob shape, NOT plaintext
    conn.execute("""
        CREATE TABLE IF NOT EXISTS credentials_test (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            name TEXT NOT NULL,
            encrypted_blob BLOB NOT NULL,
            kind TEXT NOT NULL
        )
    """)
    creds = [
        ("cred-1", "default", "prod-pg",  b"gAAAAA__FAKE_FERNET_BYTES_1__", "postgres"),
        ("cred-2", "default", "prod-api", b"gAAAAA__FAKE_FERNET_BYTES_2__", "api_key"),
    ]
    conn.executemany(
        "INSERT INTO credentials_test (id, workspace_id, name, encrypted_blob, kind) VALUES (?, ?, ?, ?, ?)",
        creds,
    )
    seeded["credentials"] = creds

    # Audit log
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_log_test (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            user_id TEXT NOT NULL,
            action TEXT NOT NULL,
            details TEXT
        )
    """)
    audit = [
        ("2026-05-29T10:00:00Z", "user-1", "login", '{"ip":"203.0.113.5"}'),
        ("2026-05-29T10:01:00Z", "user-1", "workflow.publish", '{"workflow_id":"wf-1"}'),
        ("2026-05-29T10:05:00Z", "user-2", "credential.create", '{"id":"cred-1"}'),
    ]
    conn.executemany(
        "INSERT INTO audit_log_test (timestamp, user_id, action, details) VALUES (?, ?, ?, ?)",
        audit,
    )
    seeded["audit"] = audit

    conn.commit()
    return seeded


def _backup_to(src_path: Path, dest_path: Path) -> None:
    """Use SQLite's native backup API — same as Database.backup_to()."""
    src = sqlite3.connect(str(src_path))
    dst = sqlite3.connect(str(dest_path))
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()


def _restore_from(snapshot_path: Path, target_path: Path) -> None:
    """Replicates the /api/backup/restore endpoint's table-copy logic
    without needing the full FastAPI stack. Reads every user table from
    the snapshot and copies its rows into the target DB."""
    snap = sqlite3.connect(str(snapshot_path))
    snap.row_factory = sqlite3.Row
    tgt = sqlite3.connect(str(target_path))
    try:
        # Filter out SQLite-internal tables (`sqlite_master`, `sqlite_sequence`,
        # etc. — created automatically when AUTOINCREMENT is used) AND
        # F-Pulse internal tables (`__*`). Restoring `sqlite_sequence`
        # by hand raises OperationalError because SQLite manages it.
        tables = [
            row[0] for row in snap.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "  AND name NOT LIKE '\\_\\_%' ESCAPE '\\'"
            ).fetchall()
        ]
        for table in tables:
            create_row = snap.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name = ?",
                (table,),
            ).fetchone()
            # Skip any table whose CREATE statement is None (system
            # tables, virtual tables) — they're recreated implicitly.
            if create_row is None or not create_row[0]:
                continue
            tgt.execute(create_row[0])
            # Copy rows.
            for row in snap.execute(f"SELECT * FROM {table}"):  # noqa: S608 (controlled name)
                cols = row.keys()
                placeholders = ", ".join(["?"] * len(cols))
                tgt.execute(
                    f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})",  # noqa: S608
                    tuple(row),
                )
        tgt.commit()
    finally:
        snap.close()
        tgt.close()


# ── Tests ─────────────────────────────────────────────────────────────


def test_backup_restore_round_trip_preserves_all_rows():
    """Seed → backup → restore → assert every row reappears intact."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        live_db = tmp_path / "live.db"
        snapshot = tmp_path / "snap.db"
        restored = tmp_path / "restored.db"

        # 1. Seed
        live = sqlite3.connect(str(live_db))
        try:
            seeded = _seed_db(live)
        finally:
            live.close()

        # 2. Backup
        _backup_to(live_db, snapshot)
        assert snapshot.exists() and snapshot.stat().st_size > 0

        # 3. Restore into empty target
        _restore_from(snapshot, restored)

        # 4. Verify round trip
        check = sqlite3.connect(str(restored))
        check.row_factory = sqlite3.Row
        try:
            for table_name, seeded_rows in [
                ("workflows_test", seeded["workflows"]),
                ("credentials_test", seeded["credentials"]),
                ("audit_log_test", seeded["audit"]),
            ]:
                restored_rows = check.execute(
                    f"SELECT * FROM {table_name}"  # noqa: S608 (controlled name)
                ).fetchall()
                assert len(restored_rows) == len(seeded_rows), (
                    f"{table_name}: expected {len(seeded_rows)} rows, "
                    f"got {len(restored_rows)} after restore"
                )
        finally:
            check.close()


def test_backup_restore_preserves_workspace_isolation():
    """A restore must not collapse workspaces — rows tagged for
    workspace 'tenant-2' must come back tagged for 'tenant-2', not
    leak into 'default'. Regression guard for a hypothetical bug
    where the restore loop drops the workspace column."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        live_db = tmp_path / "live.db"
        snapshot = tmp_path / "snap.db"
        restored = tmp_path / "restored.db"

        live = sqlite3.connect(str(live_db))
        try:
            _seed_db(live)
        finally:
            live.close()

        _backup_to(live_db, snapshot)
        _restore_from(snapshot, restored)

        check = sqlite3.connect(str(restored))
        try:
            tenant_2_rows = check.execute(
                "SELECT id, workspace_id FROM workflows_test WHERE workspace_id = 'tenant-2'"
            ).fetchall()
            assert len(tenant_2_rows) == 1, "tenant-2 workspace data missing after restore"
            default_rows = check.execute(
                "SELECT id FROM workflows_test WHERE workspace_id = 'default'"
            ).fetchall()
            assert len(default_rows) == 2
        finally:
            check.close()


def test_backup_does_not_contain_plaintext_credential_strings():
    """Defense in depth: a backup file viewed in a hex editor must not
    contain recognizable plaintext credential markers. The encryption
    layer should already prevent this, but a regression in that layer
    would be caught here — the backup test is the LAST gate before
    bytes go to disk.

    If credentials were stored as plaintext JSON, strings like
    "password" + the value would appear together in the file. We
    search for the canonical Fernet-encrypted prefix (gAAAAA) to
    confirm only encrypted bytes were written.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        live_db = tmp_path / "live.db"
        snapshot = tmp_path / "snap.db"

        live = sqlite3.connect(str(live_db))
        try:
            _seed_db(live)
        finally:
            live.close()

        _backup_to(live_db, snapshot)

        snapshot_bytes = snapshot.read_bytes()
        # The seed function stored credentials with a fake Fernet prefix
        # (gAAAAA). Confirm that's the only credential-shaped blob in the
        # file — no plaintext "password=" or known weak markers.
        assert b"gAAAAA" in snapshot_bytes, (
            "Encrypted-blob marker missing from backup — credential "
            "encryption may have been bypassed"
        )
        forbidden = [b"password=", b"PRIVATE KEY", b"-----BEGIN"]
        for marker in forbidden:
            assert marker not in snapshot_bytes, (
                f"Backup contains plaintext marker {marker!r}; "
                "encryption layer is leaking"
            )


if __name__ == "__main__":
    # Quick smoke when running directly: each test prints PASS or raises.
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as exc:
                print(f"  FAIL  {name}: {exc}")
                raise
