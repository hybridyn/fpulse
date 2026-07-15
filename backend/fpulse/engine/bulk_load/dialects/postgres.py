"""Postgres bulk-load via `COPY FROM STDIN`.

Why Postgres first:
  * `COPY FROM STDIN` is the fastest path Postgres exposes (10-100×
    faster than INSERT loops at >1k rows). Native binary protocol; no
    cloud staging required.
  * No per-row round-trip; the driver streams a single PG message
    containing all rows.
  * Idempotent re-run via temp table + INSERT … ON CONFLICT DO UPDATE
    when `primary_key` is declared.

Driver: `psycopg2-binary` (preferred) OR `psycopg` v3. Both expose
`copy_expert` / `copy()` with similar semantics. We try psycopg2 first
because it's the more widely-deployed of the two on Linux/Windows.

Optional dependency — listed in pyproject.toml `[project.optional-dependencies] postgres = ["psycopg2-binary"]` (see `pyproject.toml`). Without it,
`is_available()` returns False and the runner raises `BulkLoaderNotAvailable`.
The basic INSERT path still works in that case.
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Any

from ..registry import register
from ..types import BulkLoadRequest, BulkLoadResult, BulkLoaderProtocol

logger = logging.getLogger(__name__)


def _try_import_driver():
    """Attempt to import psycopg2 first, then psycopg v3. Returns the
    module or None. Cached at module scope after first call to avoid
    repeated import attempts on hosts without the driver."""
    try:
        import psycopg2  # type: ignore[import-not-found]
        return psycopg2
    except ImportError:
        pass
    try:
        import psycopg  # type: ignore[import-not-found]
        return psycopg
    except ImportError:
        pass
    return None


def _quote_ident(name: str) -> str:
    """Postgres-safe identifier quoter. Doubles embedded double-quotes."""
    return '"' + str(name).replace('"', '""') + '"'


def _qualified_table(schema_name: str, table: str) -> str:
    """If `table` already contains a schema (`schema.table`), use it
    verbatim (with quoting). Otherwise prepend `schema_name`. Quotes
    every identifier component to defeat injection / reserved-word edge
    cases."""
    if "." in table:
        parts = table.split(".", 1)
        return f"{_quote_ident(parts[0])}.{_quote_ident(parts[1])}"
    return f"{_quote_ident(schema_name)}.{_quote_ident(table)}"


class PostgresBulkLoader:
    """psycopg2/psycopg COPY FROM STDIN bulk loader.

    Three-phase write for atomicity in 'merge' mode:
      1. Load all rows into a per-call temp table (`pg_temp.<staging>`)
         via COPY FROM STDIN.
      2. INSERT … SELECT … ON CONFLICT DO UPDATE from the temp table
         to the target.
      3. (No DROP needed — pg_temp is auto-cleaned on session close.)

    For 'create' / 'append' / 'truncate' modes, the COPY targets the
    real table directly — no staging.
    """

    dialect = "postgresql"
    method = "COPY FROM STDIN"

    def is_available(self) -> bool:
        return _try_import_driver() is not None

    # ── Driver glue ──────────────────────────────────────────────────

    def _connect(self, config: dict[str, Any]):
        driver = _try_import_driver()
        if driver is None:
            from ..types import BulkLoaderNotAvailable
            raise BulkLoaderNotAvailable(
                "psycopg2-binary or psycopg is not installed. "
                "Install: pip install psycopg2-binary"
            )
        # Both psycopg2 and psycopg v3 share `connect(host=, port=, dbname=, user=, password=, sslmode=)`.
        return driver.connect(
            host=config.get("host", "localhost"),
            port=int(config.get("port", 5432)),
            dbname=config.get("database") or config.get("dbname") or "postgres",
            user=config.get("user") or config.get("username", "postgres"),
            password=config.get("password", ""),
            sslmode=config.get("sslmode", "prefer"),
            connect_timeout=int(config.get("connect_timeout", 10)),
        )

    # ── Row materialisation ──────────────────────────────────────────

    def _materialize_rows(
        self, request: BulkLoadRequest,
    ) -> tuple[list[str], list[tuple]]:
        """Pull rows + columns out of the DuckDB relation.

        Returns (columns, rows). The caller must NOT trust `request.columns`
        as ordering of values — the order returned here is the canonical
        order used by both COPY and the resulting CSV.
        """
        rel = request.relation
        columns = list(request.columns) if request.columns else list(rel.columns)
        # DuckDBPyRelation supports `.fetchall()` to materialize rows. For very
        # large tables this is bounded by the pipeline's memory budget; the
        # planned chunked-staging path (for >1M rows) lives in a follow-up.
        rows = rel.fetchall()
        return columns, rows

    # ── COPY helper ──────────────────────────────────────────────────

    def _copy_into(
        self,
        cur,
        target_qual: str,
        columns: list[str],
        rows: list[tuple],
    ) -> None:
        """Stream `rows` into `target_qual` via COPY FROM STDIN (CSV format)."""
        if not rows:
            return
        # Build CSV in memory. csv.writer handles quoting + None→empty
        # via setting quoting=csv.QUOTE_MINIMAL and translating None→''.
        buf = io.StringIO()
        writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
        for row in rows:
            writer.writerow(["" if v is None else v for v in row])
        buf.seek(0)
        col_list = ", ".join(_quote_ident(c) for c in columns)
        sql = f"COPY {target_qual} ({col_list}) FROM STDIN WITH (FORMAT CSV, NULL '')"

        # psycopg2: cur.copy_expert(sql, file). psycopg v3: cur.copy(sql) + write.
        if hasattr(cur, "copy_expert"):  # psycopg2
            cur.copy_expert(sql, buf)
        else:  # psycopg v3
            with cur.copy(sql) as cp:  # type: ignore[attr-defined]
                cp.write(buf.getvalue().encode("utf-8"))

    # ── Mode handlers ────────────────────────────────────────────────

    def _do_create(self, conn, target_qual: str, columns: list[str], rows: list[tuple]) -> int:
        cur = conn.cursor()
        try:
            # Drop & recreate. Types default to text — caller is expected to
            # have created the target with the right schema for typed columns;
            # we surface this in the warnings on the result.
            cur.execute(f"DROP TABLE IF EXISTS {target_qual}")
            col_defs = ", ".join(f"{_quote_ident(c)} text" for c in columns)
            cur.execute(f"CREATE TABLE {target_qual} ({col_defs})")
            self._copy_into(cur, target_qual, columns, rows)
            conn.commit()
            return len(rows)
        except Exception:
            conn.rollback()
            raise

    def _do_append(self, conn, target_qual: str, columns: list[str], rows: list[tuple]) -> int:
        cur = conn.cursor()
        try:
            self._copy_into(cur, target_qual, columns, rows)
            conn.commit()
            return len(rows)
        except Exception:
            conn.rollback()
            raise

    def _do_truncate(self, conn, target_qual: str, columns: list[str], rows: list[tuple]) -> int:
        cur = conn.cursor()
        try:
            cur.execute(f"TRUNCATE TABLE {target_qual}")
            self._copy_into(cur, target_qual, columns, rows)
            conn.commit()
            return len(rows)
        except Exception:
            conn.rollback()
            raise

    def _do_merge(
        self, conn, target_qual: str, columns: list[str],
        rows: list[tuple], primary_key: list[str],
    ) -> int:
        """Idempotent upsert via temp staging + ON CONFLICT DO UPDATE.

        Postgres has supported ON CONFLICT … DO UPDATE since 9.5. We
        prefer it over MERGE (15+) for compatibility with older
        production installs.
        """
        cur = conn.cursor()
        try:
            staging = "_fpulse_bulk_stage"
            col_defs = ", ".join(f"{_quote_ident(c)} text" for c in columns)
            cur.execute(f"CREATE TEMP TABLE {staging} ({col_defs}) ON COMMIT DROP")
            self._copy_into(cur, staging, columns, rows)

            non_pk_cols = [c for c in columns if c not in primary_key]
            col_list = ", ".join(_quote_ident(c) for c in columns)
            pk_list = ", ".join(_quote_ident(c) for c in primary_key)
            if non_pk_cols:
                set_clause = ", ".join(
                    f"{_quote_ident(c)} = EXCLUDED.{_quote_ident(c)}"
                    for c in non_pk_cols
                )
                upsert_sql = (
                    f"INSERT INTO {target_qual} ({col_list}) "
                    f"SELECT {col_list} FROM {staging} "
                    f"ON CONFLICT ({pk_list}) DO UPDATE SET {set_clause}"
                )
            else:
                # Every column is part of the PK — there's nothing to update,
                # so DO NOTHING is the correct semantics for re-run idempotency.
                upsert_sql = (
                    f"INSERT INTO {target_qual} ({col_list}) "
                    f"SELECT {col_list} FROM {staging} "
                    f"ON CONFLICT ({pk_list}) DO NOTHING"
                )
            cur.execute(upsert_sql)
            conn.commit()
            return len(rows)
        except Exception:
            conn.rollback()
            raise

    # ── Entry point ──────────────────────────────────────────────────

    def load(self, request: BulkLoadRequest) -> BulkLoadResult:
        if not self.is_available():
            from ..types import BulkLoaderNotAvailable
            raise BulkLoaderNotAvailable(
                "PostgresBulkLoader.load: driver not installed"
            )

        columns, rows = self._materialize_rows(request)
        if not columns:
            raise ValueError(
                "PostgresBulkLoader.load: relation has no columns"
            )

        target_qual = _qualified_table(request.schema_name, request.table)
        warnings: list[str] = []

        # 'create' mode emits typed-columns-as-text — flag it.
        if request.mode == "create":
            warnings.append(
                "Postgres bulk 'create' mode created columns as text. For "
                "typed columns, pre-create the table with explicit types and "
                "use mode='append' or 'truncate'."
            )

        conn = self._connect(request.config)
        try:
            if request.mode == "create":
                rows_loaded = self._do_create(conn, target_qual, columns, rows)
            elif request.mode == "append":
                rows_loaded = self._do_append(conn, target_qual, columns, rows)
            elif request.mode == "truncate":
                rows_loaded = self._do_truncate(conn, target_qual, columns, rows)
            elif request.mode == "merge":
                rows_loaded = self._do_merge(
                    conn, target_qual, columns, rows, request.primary_key,
                )
            else:
                raise ValueError(
                    f"PostgresBulkLoader: unsupported mode '{request.mode}'"
                )
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

        return BulkLoadResult(
            rows_loaded=rows_loaded,
            duration_ms=0,           # filled by runner
            dialect=self.dialect,
            method=self.method,
            warnings=warnings,
        )


# Register at import time. Safe even when the driver is missing —
# is_available() guards the actual driver import.
register(PostgresBulkLoader())
