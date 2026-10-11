"""Migration / upgrade / rollback qualification (P1-2).

F-Pulse OSS applies **forward-only, idempotent** schema migrations at startup:
`Database._init_schema` walks an in-code ladder tracked in `_meta.schema_version`
up to `SCHEMA_VERSION`. Upgrades run automatically; there are no down-migrations.
These tests qualify the properties an operator's upgrade actually depends on, so
a schema change that breaks any of them fails CI instead of a production upgrade:

  1. A fresh DB initializes to the current SCHEMA_VERSION.
  2. Re-opening an already-current DB is idempotent — the path every restart
     takes must be a clean no-op (no error, no version drift).
  3. Data written before a re-open survives the migration path (steps must not
     drop/recreate data-bearing tables).
  4. Forward-only safety: opening a DB stamped NEWER than this build knows (the
     "old binary against a newer DB" case) never crashes and never drops data.

It also pins the standalone .sql runner's fail-loud contract
(`storage/migrations.run_migrations`): a bad migration raises and does not revert
migrations that already committed.
"""
from __future__ import annotations

import sqlite3

import pytest

from fpulse.storage.database import Database, SCHEMA_VERSION
from fpulse.storage import migrations as mig


def _dbfile(tmp_path) -> str:
    return str(tmp_path / "fpulse.db")


def _open_close(db_file: str) -> None:
    """Construct a Database (runs the migration ladder) and release its
    connections, so the next open / raw read never contends on the WAL."""
    db = Database(db_file)
    try:
        db.close()
    except Exception:  # noqa: BLE001 — cleanup only; close() is best-effort
        pass


def _schema_version(db_file: str) -> str | None:
    conn = sqlite3.connect(db_file)
    try:
        row = conn.execute("SELECT value FROM _meta WHERE key = 'schema_version'").fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _set_meta(db_file: str, key: str, value: str) -> None:
    conn = sqlite3.connect(db_file)
    try:
        conn.execute("INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
    finally:
        conn.close()


def _get_meta(db_file: str, key: str) -> str | None:
    conn = sqlite3.connect(db_file)
    try:
        row = conn.execute("SELECT value FROM _meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def test_fresh_db_reaches_current_schema_version(tmp_path):
    f = _dbfile(tmp_path)
    _open_close(f)
    assert _schema_version(f) == str(SCHEMA_VERSION)


def test_reopen_is_idempotent(tmp_path):
    f = _dbfile(tmp_path)
    _open_close(f)
    # Re-open: the ladder must be a no-op on an already-current DB — this is
    # the path every restart / redeploy exercises.
    _open_close(f)
    assert _schema_version(f) == str(SCHEMA_VERSION)


def test_data_survives_reopen(tmp_path):
    f = _dbfile(tmp_path)
    _open_close(f)
    _set_meta(f, "p1_2_marker", "survives")
    # Re-open re-runs the migration path; the marker must still be there.
    _open_close(f)
    assert _get_meta(f, "p1_2_marker") == "survives"


def test_forward_only_never_downgrades_or_drops(tmp_path):
    f = _dbfile(tmp_path)
    _open_close(f)
    _set_meta(f, "p1_2_marker", "keep")
    # Stamp the DB far NEWER than this build knows (old-binary-vs-newer-DB).
    _set_meta(f, "schema_version", str(SCHEMA_VERSION + 5000))
    # Opening must not crash, must not downgrade below current, must not drop data.
    _open_close(f)
    assert int(_schema_version(f)) >= SCHEMA_VERSION
    assert _get_meta(f, "p1_2_marker") == "keep"


def test_sql_runner_is_fail_loud_and_preserves_prior(tmp_path):
    root = tmp_path / "backend"
    (root / "migrations").mkdir(parents=True)
    (root / "migrations" / "001_ok.sql").write_text(
        "CREATE TABLE schema_migrations (id TEXT PRIMARY KEY, description TEXT);"
        "CREATE TABLE ok_table (x INTEGER);",
        encoding="utf-8",
    )
    (root / "migrations" / "002_bad.sql").write_text("THIS IS NOT VALID SQL;", encoding="utf-8")
    db_file = str(tmp_path / "m.db")

    with pytest.raises(RuntimeError):
        mig.run_migrations(db_file, root)

    # The good migration committed before the bad one aborted; it is NOT
    # reverted (fail-loud per file, not all-or-nothing across the set).
    conn = sqlite3.connect(db_file)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()}
    finally:
        conn.close()
    assert "ok_table" in tables


def test_sql_runner_idempotent(tmp_path):
    root = tmp_path / "backend"
    (root / "migrations").mkdir(parents=True)
    (root / "migrations" / "001_ok.sql").write_text(
        "CREATE TABLE schema_migrations (id TEXT PRIMARY KEY, description TEXT);"
        "CREATE TABLE t (x INTEGER);",
        encoding="utf-8",
    )
    db_file = str(tmp_path / "m.db")
    assert "001_ok" in mig.run_migrations(db_file, root)
    # Second run: nothing pending.
    assert mig.run_migrations(db_file, root) == []
