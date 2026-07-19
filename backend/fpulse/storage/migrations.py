"""
F-Pulse migration runner.

Applies numbered .sql files from ./migrations in order, tracked via
`schema_migrations` table. Called once on app startup.

Design principles:
  - Idempotent: re-running is a no-op after all migrations applied.
  - Fail-loud: a SQL error aborts startup — do NOT continue with partial schema.
  - Per-connection pragmas applied on every new connection (see apply_pragmas).
  - No external dependencies beyond stdlib + sqlite3.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Per-connection pragmas (applied on every open)
# ─────────────────────────────────────────────────────────────────────────

# WAL is set once in the DB file header by migration 001; the others below
# are connection-scoped and must be re-applied every time.
PER_CONNECTION_PRAGMAS = (
    "PRAGMA synchronous  = NORMAL",
    "PRAGMA cache_size   = -64000",
    "PRAGMA mmap_size    = 268435456",
    "PRAGMA temp_store   = MEMORY",
    "PRAGMA foreign_keys = ON",
    "PRAGMA busy_timeout = 5000",
)


def apply_pragmas(conn: sqlite3.Connection) -> None:
    """Call this on every newly-opened SQLite connection."""
    for stmt in PER_CONNECTION_PRAGMAS:
        conn.execute(stmt)


# ─────────────────────────────────────────────────────────────────────────
# Migration discovery + tracking
# ─────────────────────────────────────────────────────────────────────────

def _migrations_dir(backend_root: Path) -> Path:
    return backend_root / "migrations"


def _discover(backend_root: Path) -> list[Path]:
    """Return .sql files sorted by leading number."""
    d = _migrations_dir(backend_root)
    if not d.exists():
        return []
    files = sorted(p for p in d.glob("*.sql") if p.name[:3].isdigit())
    return files


def _applied_ids(conn: sqlite3.Connection) -> set[str]:
    """Read already-applied migration IDs. Returns empty set if table missing."""
    try:
        rows = conn.execute("SELECT id FROM schema_migrations").fetchall()
        return {r[0] for r in rows}
    except sqlite3.OperationalError:
        # First-ever run — table doesn't exist yet. Migration 001 creates it.
        return set()


def _migration_id(path: Path) -> str:
    """Strip .sql extension; file name IS the ID."""
    return path.stem


# ─────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────

def run_migrations(db_path: str | Path, backend_root: str | Path) -> list[str]:
    """
    Apply any pending migrations. Returns list of IDs that were applied.

    Called from fpulse.main startup before any store is instantiated.
    Safe to call on a brand-new DB (creates schema_migrations table
    implicitly via migration 001).
    """
    db_path = Path(db_path)
    backend_root = Path(backend_root)

    conn = sqlite3.connect(str(db_path))
    try:
        applied_before = _applied_ids(conn)
        applied_now: list[str] = []

        migrations = _discover(backend_root)
        if not migrations:
            log.warning("No migrations found in %s", _migrations_dir(backend_root))
            return []

        for path in migrations:
            mid = _migration_id(path)
            if mid in applied_before:
                continue

            log.info("Applying migration %s ...", mid)
            sql = path.read_text(encoding="utf-8")
            try:
                conn.executescript(sql)
                conn.commit()
            except sqlite3.Error as exc:
                conn.rollback()
                raise RuntimeError(
                    f"Migration {mid} failed: {exc}. "
                    f"Database left at previous state; investigate before retry."
                ) from exc

            # Some migrations (incl. 001) self-record in schema_migrations;
            # we INSERT OR IGNORE defensively in case a migration forgot.
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations (id, description) VALUES (?, ?)",
                (mid, f"Auto-recorded by runner"),
            )
            conn.commit()
            applied_now.append(mid)
            log.info("Migration %s applied.", mid)

        apply_pragmas(conn)
        return applied_now
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────
# CLI for manual ops
# ─────────────────────────────────────────────────────────────────────────

def _main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Run F-Pulse SQLite migrations")
    ap.add_argument("--db", required=True, help="Path to fpulse.db")
    ap.add_argument("--root", default=".", help="Backend root containing migrations/")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    applied = run_migrations(args.db, args.root)
    if applied:
        print(f"Applied: {', '.join(applied)}")
    else:
        print("No pending migrations.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
