"""Snowflake bulk-load via PUT to user stage + COPY INTO target.

Why Snowflake first for Gate 2 (one warehouse to depth-5):
  * Most-requested by the regulated/enterprise segment that drives Plus.
  * Snowflake's recommended bulk path is PUT-to-stage + COPY INTO, not
    the (deprecated) batch INSERT. Going row-by-row at >10k rows is
    100-1000× slower than the staged path.
  * Pure-Python stack: snowflake-connector-python streams the local
    CSV via PUT, no pandas / pyarrow required for the OSS path.

Pipeline:
  1. Write rows to a local CSV under `<staging_dir>/<call_uuid>.csv`.
  2. `CREATE STAGE IF NOT EXISTS @~/fpulse_bulk` — user stage, no
     warehouse role / external stage permissions needed.
  3. `PUT file://<csv> @~/fpulse_bulk/<uuid>.csv AUTO_COMPRESS=TRUE`.
     Connector handles encryption, gzip, retry.
  4. `COPY INTO <target> FROM @~/fpulse_bulk/<uuid>.csv.gz
     FILE_FORMAT = (TYPE=CSV ...) ON_ERROR='ABORT_STATEMENT'`.
  5. `REMOVE @~/fpulse_bulk/<uuid>.csv.gz` so the stage doesn't grow.
  6. Mode='merge' uses a temp table + MERGE INTO target USING temp.

Optional dependency: `snowflake-connector-python`. Listed under
`[project.optional-dependencies] snowflake = [...]` so installs that
don't use Snowflake skip the binary wheel.

Limits documented at the top of `bulk_load/__init__.py` apply: COPY
INTO over CSV is text-typed; for typed columns, pre-create the target
and use mode='append'/'merge' rather than 'create'.
"""

from __future__ import annotations

import csv
import logging
import os
import tempfile
import uuid
from typing import Any

from ..registry import register
from ..types import (
    BulkLoaderNotAvailable,
    BulkLoadRequest,
    BulkLoadResult,
)

logger = logging.getLogger(__name__)

_USER_STAGE = "@~/fpulse_bulk"


def _try_import_driver():
    try:
        import snowflake.connector  # type: ignore[import-not-found]
        return snowflake.connector
    except ImportError:
        return None


def _quote_ident(name: str) -> str:
    """Snowflake identifier: double-quote, escape internal quotes."""
    return '"' + str(name).replace('"', '""') + '"'


def _qualified_table(schema_name: str, table: str) -> str:
    """Resolve `schema.table` from request.

    Snowflake supports up to three components: database.schema.table.
    Pass-through if the caller already provided them.
    """
    parts = table.split(".")
    if len(parts) == 1:
        return f"{_quote_ident(schema_name)}.{_quote_ident(parts[0])}"
    return ".".join(_quote_ident(p) for p in parts)


class SnowflakeBulkLoader:
    """Snowflake PUT + COPY INTO bulk loader.

    Connection config keys (from F-Pulse `connections` store):
      account, user, password, warehouse, database, schema, role,
      authenticator (default 'snowflake'), private_key (key-pair auth)

    Per-call: staging path defaults to a tempdir; override via
    `request.staging_dir` for operators who want staging on a
    specific volume.
    """

    dialect = "snowflake"
    method = "PUT + COPY INTO"

    def is_available(self) -> bool:
        return _try_import_driver() is not None

    # ── Driver glue ──────────────────────────────────────────────────

    def _connect(self, config: dict[str, Any]):
        driver = _try_import_driver()
        if driver is None:
            raise BulkLoaderNotAvailable(
                "snowflake-connector-python is not installed. "
                "Install: pip install snowflake-connector-python"
            )
        kwargs: dict[str, Any] = {
            "account": config.get("account") or "",
            "user": config.get("user") or config.get("username") or "",
            "warehouse": config.get("warehouse") or "",
            "database": config.get("database") or "",
            "schema": config.get("schema") or "PUBLIC",
            "role": config.get("role") or "",
            "authenticator": config.get("authenticator") or "snowflake",
            "client_session_keep_alive": False,
        }
        if config.get("password"):
            kwargs["password"] = config["password"]
        if config.get("private_key"):
            # Key-pair auth — pass through; Snowflake driver decodes.
            kwargs["private_key"] = config["private_key"]
        # Drop empties so the driver applies its own defaults.
        kwargs = {k: v for k, v in kwargs.items() if v not in ("", None)}
        return driver.connect(**kwargs)

    # ── Row materialisation + CSV write ──────────────────────────────

    def _materialize_rows(
        self, request: BulkLoadRequest,
    ) -> tuple[list[str], list[tuple]]:
        rel = request.relation
        columns = list(request.columns) if request.columns else list(rel.columns)
        rows = rel.fetchall()
        return columns, rows

    def _write_csv(
        self,
        rows: list[tuple],
        columns: list[str],
        staging_dir: str | None,
    ) -> str:
        """Write rows to a local CSV file. Returns the absolute path.

        Snowflake COPY INTO with `FIELD_OPTIONALLY_ENCLOSED_BY='"'` +
        `TYPE=CSV` parses RFC-4180-style CSV correctly. We use the
        stdlib csv module to emit that format with `quoting=QUOTE_MINIMAL`
        and translate Python None → empty string (NULL_IF '' on the
        Snowflake side).
        """
        if staging_dir:
            os.makedirs(staging_dir, exist_ok=True)
            path = os.path.join(staging_dir, f"fpulse_{uuid.uuid4().hex}.csv")
        else:
            fd, path = tempfile.mkstemp(prefix="fpulse_bulk_", suffix=".csv")
            os.close(fd)
        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
            # Header row helps the operator if they ever inspect the
            # staged file; COPY INTO ignores it via `SKIP_HEADER = 1`.
            writer.writerow(columns)
            for row in rows:
                writer.writerow(["" if v is None else v for v in row])
        return path

    # ── Stage management ─────────────────────────────────────────────

    def _ensure_stage(self, cur) -> None:
        cur.execute(f"CREATE STAGE IF NOT EXISTS ~/fpulse_bulk")

    def _put_file(self, cur, local_path: str) -> str:
        """PUT the CSV onto the user stage. Returns the staged file name
        (without path prefix). Snowflake auto-compresses to .gz."""
        # PUT supports forward-slash file:// URIs on every OS.
        url = "file://" + local_path.replace("\\", "/")
        cur.execute(f"PUT '{url}' {_USER_STAGE} AUTO_COMPRESS=TRUE OVERWRITE=TRUE")
        # Snowflake renames the file on the stage to <basename>.gz.
        base = os.path.basename(local_path) + ".gz"
        return base

    def _remove_staged(self, cur, staged_name: str) -> None:
        try:
            cur.execute(f"REMOVE {_USER_STAGE}/{staged_name}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "snowflake bulk-load: REMOVE @~/.../%s failed (continuing): %s",
                staged_name, exc,
            )

    # ── COPY INTO ────────────────────────────────────────────────────

    def _build_file_format(self) -> str:
        # NULL_IF: empty string treated as NULL.
        # FIELD_OPTIONALLY_ENCLOSED_BY: handles values containing commas/newlines.
        # SKIP_HEADER: we wrote a header row in _write_csv.
        # COMPRESSION: AUTO matches the .gz produced by AUTO_COMPRESS.
        return (
            "(TYPE=CSV "
            "FIELD_DELIMITER=',' "
            "RECORD_DELIMITER='\\n' "
            "SKIP_HEADER=1 "
            "FIELD_OPTIONALLY_ENCLOSED_BY='\"' "
            "NULL_IF=('') "
            "COMPRESSION=AUTO)"
        )

    def _copy_into(self, cur, target_qual: str, staged_name: str, columns: list[str]) -> None:
        col_list = ", ".join(_quote_ident(c) for c in columns)
        sql = (
            f"COPY INTO {target_qual} ({col_list}) "
            f"FROM {_USER_STAGE}/{staged_name} "
            f"FILE_FORMAT = {self._build_file_format()} "
            f"ON_ERROR = 'ABORT_STATEMENT'"
        )
        cur.execute(sql)

    # ── Mode handlers ────────────────────────────────────────────────

    def _do_create(self, conn, target_qual: str, columns: list[str], staged_name: str) -> None:
        cur = conn.cursor()
        cur.execute(f"DROP TABLE IF EXISTS {target_qual}")
        col_defs = ", ".join(f"{_quote_ident(c)} VARCHAR" for c in columns)
        cur.execute(f"CREATE TABLE {target_qual} ({col_defs})")
        self._copy_into(cur, target_qual, staged_name, columns)

    def _do_append(self, conn, target_qual: str, columns: list[str], staged_name: str) -> None:
        cur = conn.cursor()
        self._copy_into(cur, target_qual, staged_name, columns)

    def _do_truncate(self, conn, target_qual: str, columns: list[str], staged_name: str) -> None:
        cur = conn.cursor()
        cur.execute(f"TRUNCATE TABLE IF EXISTS {target_qual}")
        self._copy_into(cur, target_qual, staged_name, columns)

    def _do_merge(
        self, conn, target_qual: str, columns: list[str],
        staged_name: str, primary_key: list[str],
    ) -> None:
        """Idempotent MERGE via temp staging table.

        Snowflake supports MERGE natively, including WHEN NOT MATCHED
        BY TARGET / WHEN MATCHED THEN UPDATE patterns. Our pattern:
          1. Create a temp table mirroring the column list.
          2. COPY the staged file into the temp table.
          3. MERGE target USING temp ON pk WHEN MATCHED THEN UPDATE,
             WHEN NOT MATCHED THEN INSERT.
          4. Temp table is session-scoped; gets dropped on close().
        """
        cur = conn.cursor()
        staging = "FPULSE_BULK_STAGE"
        col_defs = ", ".join(f"{_quote_ident(c)} VARCHAR" for c in columns)
        cur.execute(f"CREATE TEMPORARY TABLE IF NOT EXISTS {staging} ({col_defs})")
        cur.execute(f"TRUNCATE TABLE {staging}")
        self._copy_into(cur, staging, staged_name, columns)

        non_pk = [c for c in columns if c not in primary_key]
        on_clause = " AND ".join(
            f"target.{_quote_ident(c)} = src.{_quote_ident(c)}" for c in primary_key
        )
        col_list = ", ".join(_quote_ident(c) for c in columns)
        src_list = ", ".join(f"src.{_quote_ident(c)}" for c in columns)
        if non_pk:
            update_set = ", ".join(
                f"target.{_quote_ident(c)} = src.{_quote_ident(c)}" for c in non_pk
            )
            merge_sql = (
                f"MERGE INTO {target_qual} target "
                f"USING {staging} src "
                f"ON {on_clause} "
                f"WHEN MATCHED THEN UPDATE SET {update_set} "
                f"WHEN NOT MATCHED THEN INSERT ({col_list}) VALUES ({src_list})"
            )
        else:
            merge_sql = (
                f"MERGE INTO {target_qual} target "
                f"USING {staging} src "
                f"ON {on_clause} "
                f"WHEN NOT MATCHED THEN INSERT ({col_list}) VALUES ({src_list})"
            )
        cur.execute(merge_sql)

    # ── Entry point ──────────────────────────────────────────────────

    def load(self, request: BulkLoadRequest) -> BulkLoadResult:
        if not self.is_available():
            raise BulkLoaderNotAvailable(
                "SnowflakeBulkLoader.load: driver not installed"
            )

        columns, rows = self._materialize_rows(request)
        if not columns:
            raise ValueError("SnowflakeBulkLoader.load: relation has no columns")

        target_qual = _qualified_table(request.schema_name, request.table)
        warnings: list[str] = []
        if request.mode == "create":
            warnings.append(
                "Snowflake bulk 'create' mode created columns as VARCHAR. "
                "For typed columns pre-create the target and use 'append'/'merge'."
            )

        local_csv = self._write_csv(rows, columns, request.staging_dir)
        try:
            conn = self._connect(request.config)
            try:
                # Stage management is per-connection; safe to run every call.
                cur0 = conn.cursor()
                try:
                    self._ensure_stage(cur0)
                finally:
                    cur0.close()

                # PUT into the user stage.
                cur1 = conn.cursor()
                try:
                    staged_name = self._put_file(cur1, local_csv)
                finally:
                    cur1.close()

                # Dispatch the mode.
                try:
                    if request.mode == "create":
                        self._do_create(conn, target_qual, columns, staged_name)
                    elif request.mode == "append":
                        self._do_append(conn, target_qual, columns, staged_name)
                    elif request.mode == "truncate":
                        self._do_truncate(conn, target_qual, columns, staged_name)
                    elif request.mode == "merge":
                        self._do_merge(conn, target_qual, columns, staged_name, request.primary_key)
                    else:
                        raise ValueError(
                            f"SnowflakeBulkLoader: unsupported mode '{request.mode}'"
                        )
                    conn.commit()
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:  # noqa: BLE001
                        pass
                    raise
                finally:
                    # Best-effort stage cleanup so the user stage doesn't grow.
                    cur2 = conn.cursor()
                    try:
                        self._remove_staged(cur2, staged_name)
                    finally:
                        cur2.close()
            finally:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
        finally:
            try:
                os.remove(local_csv)
            except OSError:
                pass

        return BulkLoadResult(
            rows_loaded=len(rows),
            duration_ms=0,
            dialect=self.dialect,
            method=self.method,
            warnings=warnings,
            staged_files=[local_csv],
        )


register(SnowflakeBulkLoader())
