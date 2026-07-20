"""
Stage 3b — Tier-aware database factory (2026-04-19).

Returns the right Database backend for the current process:
  • If FPULSE_DB_URL points at PostgreSQL → PostgresDatabase
  • Otherwise → in-process SQLite Database (OSS default, current Plus path)

This is the seam that lets us migrate stores from SQLite to PostgreSQL
one at a time. Today every store still uses the SQLite handle. As
each store is rewritten to talk to the PG backend, the factory's
returned Database will own both — the store decides which to use via
its own internal feature flag.

USAGE FROM main.py:

    from fpulse.storage.database_factory import build_database

    db, pg = build_database(data_dir)
    app_state["db"] = db          # SQLite — every existing store uses this
    app_state["pg"] = pg          # None today; PostgresDatabase once configured

The two-handle return is deliberate: the SQLite handle is the
backwards-compatible one (26 routers + 22 stores import it). The PG
handle is opt-in for new code paths and is None when the operator
hasn't enabled it. **Stores do not silently switch backends.** A
store that has been migrated will explicitly check `if pg is not None`
and pick its path.

Why not return a single "either-or" handle?
  Because changing the type returned by `app_state["db"]` would break
  every call site that currently assumes synchronous SQLite. Async PG
  must be opt-in per store, not surprise everyone at once.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from fpulse.storage.database import Database

if TYPE_CHECKING:
    from fpulse.storage.database_pg import PostgresDatabase

logger = logging.getLogger(__name__)


def build_database(data_dir: str) -> tuple[Database, "PostgresDatabase | None"]:
    """Construct the SQLite handle (always) + optionally a PostgreSQL handle.

    Returns:
        (sqlite_db, pg_db_or_none)

    The SQLite handle is ALWAYS returned — every existing store and
    router needs it. The PG handle is returned only when:
      • FPULSE_DB_URL is set AND points at a postgres:// or
        postgresql+asyncpg:// URL
      • The `pg` extra dependencies are importable
      • Construction succeeds without raising

    On any of those failing, we log clearly and return (sqlite, None).
    Failure is NEVER fatal — the process boots fine on SQLite, the
    operator just sees a warning that PG was requested but unavailable.
    """
    # Always create SQLite — load-bearing for every store today.
    sqlite_db = Database(os.path.join(data_dir, "fpulse.db"))

    # PG is opt-in via env var.
    db_url = os.environ.get("FPULSE_DB_URL", "").strip()
    if not db_url:
        return sqlite_db, None

    if not (db_url.startswith("postgres://") or db_url.startswith("postgresql")):
        logger.warning(
            "FPULSE_DB_URL is set but does not look like a PostgreSQL URL "
            "(got %r). Skipping PG initialisation.",
            db_url[:30] + "..." if len(db_url) > 30 else db_url,
        )
        return sqlite_db, None

    try:
        # Lazy import — only loaded when PG is actually wanted.
        from fpulse.storage.database_pg import PostgresDatabase, _pg_deps_available
        ok, err = _pg_deps_available()
        if not ok:
            logger.warning(
                "FPULSE_DB_URL is set but PG dependencies are missing: %s. "
                "Process will continue on SQLite. Install with: "
                "pip install -e '.[pg]'",
                err,
            )
            return sqlite_db, None

        pg_db = PostgresDatabase(db_url)
        # NOTE: we DO NOT call pg_db.init() here — that's async and must
        # run inside the lifespan. main.py calls it after build_database
        # returns. This keeps build_database synchronous and importable
        # from anywhere.
        logger.info(
            "F-Pulse Postgres handle constructed (init deferred to lifespan)"
        )
        return sqlite_db, pg_db
    except Exception as exc:
        logger.warning(
            "Failed to construct PostgresDatabase (continuing on SQLite): %s",
            exc,
        )
        return sqlite_db, None
