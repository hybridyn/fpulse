"""SQLite-backed connection and report store."""

from __future__ import annotations

import json
import logging
import os
import random
import re
import socket
import sqlite3
import ssl
import time
import urllib.request
from datetime import datetime, timezone
from copy import deepcopy

from .models import Connection, ConnectionReport, ReportParameter, RunReportResult

logger = logging.getLogger(__name__)


def _demo_seed_enabled() -> bool:
    """Return True if the OSS install should auto-seed demo connections.

    Pre-2026-05-28 behaviour was "always seed on first boot", which
    landed every new install with 3 placeholder connections (Orders
    DB → db.example.com, Oracle Fusion → my-pod.fa.us2.oraclecloud.com,
    Snowflake DW → abc12345.us-east-1) that point at non-existent
    endpoints. The user flagged this on the Dashboard: a fresh
    workspace showed "5 connections" before they'd created any —
    and the 3 demo rows can't actually be used because the hosts
    don't resolve.

    Now: off by default. Set ``FPULSE_SEED_DEMO_DATA=1`` (or
    ``true`` / ``yes``) to opt back in — useful for demo
    deployments and screenshots where the user wants something
    populated in the table.

    Existing installs that already have the seeds keep them — the
    upstream caller is ``_seed_if_empty`` which gates on the
    connections table being literally empty, so a populated DB is
    never touched.
    """
    return os.environ.get("FPULSE_SEED_DEMO_DATA", "").strip().lower() in ("1", "true", "yes", "on")


def get_store() -> "ConnectionStore":
    """Return the live ConnectionStore from app_state.

    Added 2026-05-22 to repair a broken import: api/execution.py and the
    test_execution_ephemeral.py mock both reference
    ``fpulse.connections.store.get_store``, but the function never existed
    here — the import failed at runtime on the dry-run code path, and the
    test mock papered over the missing target. This helper now provides
    the canonical entry point.

    Raises ``RuntimeError`` with an actionable message when the lifespan
    hasn't populated app_state — matching the typed-accessor pattern in
    fpulse.state.
    """
    # Lazy import to avoid the circular dep through fpulse.main → connections.
    from fpulse.main import app_state
    store = app_state.get("connection_store")
    if store is None:
        raise RuntimeError(
            "app_state['connection_store'] is not initialized. The lifespan "
            "did not populate it. Check fpulse.main._populate_state."
        )
    return store


class ConnectionStore:
    """Connection and report store backed by SQLite."""

    def __init__(self, db=None):
        self._db = db
        if db:
            self._seed_if_empty()

    def set_db(self, db):
        self._db = db
        self._seed_if_empty()

    def _seed_if_empty(self):
        """Seed with example data if no connections exist.

        Gated on the ``FPULSE_SEED_DEMO_DATA`` env var as of 2026-05-28.
        Default is off — a fresh OSS install lands with an empty
        connections table so the Dashboard's "Connections" tile reads
        zero until the user actually creates one. Set the env var
        to opt back into the original demo-data seed (used by demo
        deployments + screenshot harnesses).

        Either way the gate is "table is empty AND opt-in is set",
        so a populated DB is never re-seeded and an opt-in flag on
        a non-empty workspace is a no-op.
        """
        if self._db.count("connections") == 0 and _demo_seed_enabled():
            self._seed()

    def _save_connection(self, connection: Connection):
        data = connection.model_dump(mode="json")
        self._db.insert_json(
            "connections", connection.id, data,
            name=connection.name,
            type=connection.type,
            project_id=connection.project_id,
            workspace_id=connection.workspace_id or "default",
            created_at=connection.created_at.isoformat(),
            updated_at=connection.updated_at.isoformat(),
        )

    def _save_report(self, report: ConnectionReport):
        data = report.model_dump(mode="json")
        self._db.insert_json(
            "connection_reports", report.id, data,
            connection_id=report.connection_id,
            created_at=report.created_at.isoformat(),
            updated_at=report.updated_at.isoformat(),
        )

    # ── Connections CRUD ──

    def create(self, connection: Connection) -> Connection:
        # Store-layer uniqueness (May 6 2026) — auto-suffix the name if
        # another connection in the same workspace already uses it.
        # Catches every entry path (API create / import / programmatic).
        try:
            from fpulse.common.unique_name import ensure_unique_name
            ws_id = getattr(connection, "workspace_id", None) or "default"
            existing_names: set[str] = set()
            for c in self.list(workspace_id=ws_id):
                n = getattr(c, "name", None) or (c.get("name") if isinstance(c, dict) else None)
                if n:
                    existing_names.add(n)
            if connection.name:
                connection.name = ensure_unique_name(connection.name, existing_names)
        except Exception:  # noqa: BLE001
            pass
        self._save_connection(connection)
        return connection

    def get(
        self,
        connection_id: str,
        workspace_id: str | None = None,
    ) -> Connection | None:
        """Fetch a connection by id, optionally scoped to a workspace.

        When ``workspace_id`` is provided, a connection that belongs
        to a different workspace returns None — callers should
        surface that as 404, not 403, so tenant boundaries don't
        leak through error codes.
        """
        data = self._db.get_json("connections", connection_id)
        if data is None:
            return None
        conn = Connection(**data)
        if workspace_id is not None and (conn.workspace_id or "default") != workspace_id:
            return None
        return conn

    def list_all(
        self,
        conn_type: str | None = None,
        project_id: str | None = None,
        scope: str | None = None,
        workspace_id: str | None = None,
    ) -> list[dict]:
        """List connections with optional filters.

        When ``workspace_id`` is provided the query uses the indexed
        column so cross-tenant rows never reach the Python filter
        layer. The legacy filters (conn_type, project_id, scope)
        still apply on top of the workspace scope.
        """
        if workspace_id is not None:
            items = self._db.list_json(
                "connections",
                where="workspace_id = ?",
                params=(workspace_id,),
            )
        else:
            items = self._db.list_json("connections")
        result = []
        for c in items:
            if conn_type and c.get("type") != conn_type:
                continue
            if scope == "global" and c.get("project_id") is not None:
                continue
            if scope == "project" and c.get("project_id") is None:
                continue
            if project_id and c.get("project_id") is not None and c.get("project_id") != project_id:
                continue
            # Count reports
            c["report_count"] = self._db.count(
                "connection_reports", "connection_id = ?", (c["id"],)
            )
            result.append(c)
        return sorted(result, key=lambda x: x.get("name", ""))

    def update(
        self,
        connection_id: str,
        updates: dict,
        workspace_id: str | None = None,
    ) -> Connection | None:
        """Apply a partial update to a connection.

        2026-05-22 (audit G2): the API layer now passes ``exclude_unset=True``
        from Pydantic, so ``updates`` only contains keys the caller
        actually wrote in the request body. Inside this method we honour
        explicit None (clear the field) for the keys where that makes
        sense — previously every None was silently dropped, so a user
        couldn't move a connection back from a project scope to global.

        Keys where None means "clear":
          * project_id  — None ⇒ global
          * credential_id — None ⇒ inline config only
          * environment — None ⇒ unscoped / all envs

        Other keys still ignore None (e.g. name, type), since clearing
        them would put the connection into an invalid state.
        """
        connection = self.get(connection_id, workspace_id=workspace_id)
        if not connection:
            return None
        _NULLABLE_KEYS = ("project_id", "credential_id", "environment")
        for key, value in updates.items():
            # Refuse to let the request body reassign the workspace —
            # the workspace_id is pinned to the row that already exists.
            if key == "workspace_id":
                continue
            if not hasattr(connection, key):
                continue
            if value is None and key not in _NULLABLE_KEYS:
                # Backward-compat: skip None for non-nullable keys.
                continue
            setattr(connection, key, value)
        connection.updated_at = datetime.now(timezone.utc)
        self._save_connection(connection)
        return connection

    def delete(
        self,
        connection_id: str,
        workspace_id: str | None = None,
    ) -> bool:
        # Enforce tenant boundary before the delete — otherwise a
        # caller that guesses a connection_id in a foreign workspace
        # could wipe it.
        if workspace_id is not None:
            existing = self.get(connection_id, workspace_id=workspace_id)
            if not existing:
                return False
        # Cascade-delete reports
        self._db.execute(
            "DELETE FROM connection_reports WHERE connection_id = ?",
            (connection_id,),
        )
        result = self._db.delete_row("connections", connection_id)
        return result

    def count(self) -> int:
        return self._db.count("connections")

    # ── Reports CRUD ──

    def create_report(self, report: ConnectionReport) -> ConnectionReport:
        self._save_report(report)
        return report

    def get_report(self, report_id: str) -> ConnectionReport | None:
        data = self._db.get_json("connection_reports", report_id)
        if data is None:
            return None
        return ConnectionReport(**data)

    def list_reports(self, connection_id: str) -> list[dict]:
        return self._db.list_json(
            "connection_reports", "connection_id = ?", (connection_id,),
        )

    def update_report(self, report_id: str, updates: dict) -> ConnectionReport | None:
        report = self.get_report(report_id)
        if not report:
            return None
        for key, value in updates.items():
            if value is not None and hasattr(report, key):
                setattr(report, key, value)
        report.updated_at = datetime.now(timezone.utc)
        self._save_report(report)
        return report

    def delete_report(self, report_id: str) -> bool:
        return self._db.delete_row("connection_reports", report_id)

    # ── Run Report ──

    def run_report(self, connection_id: str, report_id: str, params: dict) -> RunReportResult:
        """Run a report — executes against real DB when available, falls back to sample data."""
        start = time.time()

        connection = self.get(connection_id)
        report = self.get_report(report_id)

        if not connection or not report:
            return RunReportResult(
                connection_id=connection_id,
                report_id=report_id,
                status="error",
                error="Connection or report not found",
            )

        if report.connection_id != connection_id:
            return RunReportResult(
                connection_id=connection_id,
                report_id=report_id,
                status="error",
                error="Report does not belong to this connection",
            )

        # Validate required parameters
        for p in report.parameters:
            if p.required and p.name not in params and p.default is None:
                return RunReportResult(
                    connection_id=connection_id,
                    report_id=report_id,
                    status="error",
                    error=f"Missing required parameter: {p.name}",
                )

        # 2026-05-22 — safe parameter substitution + policy gate
        # (audit H1). Replaces the previous raw str.replace which
        # took user-supplied param values straight into SQL.
        try:
            resolved_query = self._safe_substitute_params(
                report.query_template, params, report.parameters,
            )
        except ValueError as exc:
            return RunReportResult(
                connection_id=connection_id,
                report_id=report_id,
                status="error",
                error=f"Parameter validation failed: {exc}",
            )

        # 2026-05-22 — does this connection actually carry a credential
        # or inline config the driver can use? "Has credential" decides
        # the real-vs-demo path below (audit H3). A connection with
        # neither credential_id nor a non-empty config is treated as a
        # demo / sample connection.
        has_real_target = bool(
            connection.credential_id or
            (isinstance(connection.config, dict) and connection.config)
        )

        # Try real execution for database connections
        if connection.type in ("postgresql", "mysql", "sqlite", "mssql"):
            # Policy gate before we hand the SQL to any driver. Note:
            # the policy gate runs AFTER substitution so it catches
            # any disallowed statement that a param value injected,
            # but we also want a stable error rather than a driver-
            # specific syntax exception for bad input.
            try:
                self._validate_report_sql_policy(resolved_query)
            except ValueError as exc:
                return RunReportResult(
                    connection_id=connection_id,
                    report_id=report_id,
                    status="error",
                    error=f"Report SQL rejected by policy: {exc}",
                )

            try:
                result = self._execute_real_query(connection, resolved_query)
                if result is not None:
                    columns, rows, duration = result
                    return RunReportResult(
                        connection_id=connection_id,
                        report_id=report_id,
                        status="success",
                        row_count=len(rows),
                        columns=columns,
                        sample_data=rows,
                        duration_ms=max(duration, 1),
                    )
                # 2026-05-22 (audit H2): a None result from the real
                # executor means the driver/credential setup is
                # incomplete (no DB path, missing host, etc.). Bubble
                # that up as an honest error instead of substituting
                # mock data. Previously this silently fell through to
                # _generate_mock_data and returned status="success"
                # with fake rows — actively dangerous for operators.
                if has_real_target:
                    return RunReportResult(
                        connection_id=connection_id,
                        report_id=report_id,
                        status="error",
                        error=(
                            "Could not run real query — credential or config "
                            "is incomplete. Open the connection and verify "
                            "host/database/credentials."
                        ),
                    )
            except Exception as exc:
                logger.warning(
                    "Real query failed for connection %s (report %s): %s",
                    connection_id, report_id, exc,
                )
                # H2: real failure surfaces as a real error. No mock
                # fallback. Operators relying on a report dashboard
                # need to see "the database is down" not "here's some
                # fake numbers, all green."
                if has_real_target:
                    return RunReportResult(
                        connection_id=connection_id,
                        report_id=report_id,
                        status="error",
                        error=f"Query execution failed: {exc}",
                    )

        # Try REST API execution. 2026-05-23 (T4 + U1): oracle_fusion is
        # the canonical Oracle REST connector; oracle_api is the legacy
        # alias kept for back-compat. Both route through the same
        # generic REST execution path.
        if connection.type in ("rest_api", "oracle_api", "oracle_fusion") and re.match(r"^(GET|POST)\s+", resolved_query):
            try:
                result = self._execute_rest_query(connection, resolved_query)
                if result is not None:
                    columns, rows, duration = result
                    return RunReportResult(
                        connection_id=connection_id,
                        report_id=report_id,
                        status="success",
                        row_count=len(rows),
                        columns=columns,
                        sample_data=rows,
                        duration_ms=max(duration, 1),
                    )
                if has_real_target:
                    return RunReportResult(
                        connection_id=connection_id,
                        report_id=report_id,
                        status="error",
                        error=(
                            "Could not run real API request — credential or "
                            "config is incomplete. Open the connection and "
                            "verify URL/credentials."
                        ),
                    )
            except Exception as exc:
                logger.warning(
                    "REST query failed for connection %s (report %s): %s",
                    connection_id, report_id, exc,
                )
                if has_real_target:
                    return RunReportResult(
                        connection_id=connection_id,
                        report_id=report_id,
                        status="error",
                        error=f"REST call failed: {exc}",
                    )

        # 2026-05-22 (audit H3) — mock path only fires for connections
        # with neither a credential nor inline config. That's a true
        # "demo / no-creds preview" surface. Real connections always
        # get a real error (the H2 branches above). Mock generation
        # itself is unchanged from earlier behaviour.
        try:
            sample_data, columns = self._generate_mock_data(connection, report, params)
        except Exception as e:
            return RunReportResult(
                connection_id=connection_id,
                report_id=report_id,
                status="error",
                error=str(e),
            )

        duration_ms = int((time.time() - start) * 1000)

        return RunReportResult(
            connection_id=connection_id,
            report_id=report_id,
            status="success",
            row_count=len(sample_data),
            columns=columns,
            sample_data=sample_data,
            duration_ms=max(duration_ms, 1),
        )

    # ── Real Query Execution ──

    # 2026-05-22 (audit H1) — report query safety policy.
    #
    # Previously the report template was rendered with raw `str.replace`
    # (line 278 of the old run_report), then passed straight to the
    # DB driver. Two problems:
    #
    #   1. SQL injection via user-supplied param values. A template
    #      like `SELECT * FROM t WHERE id = '{{user_id}}'` and a
    #      param value `1' OR '1'='1` produced a wildcard query.
    #   2. No statement-type gate, so a malicious or careless template
    #      could ship UPDATE / DELETE / DROP through this endpoint.
    #
    # Defence layers (cheap to expensive, applied in this order):
    #   * Length cap   — refuse silly-long queries up front.
    #   * Statement gate — top-level must be SELECT / WITH / SHOW /
    #     PRAGMA (sqlite read-only introspection).
    #   * Multi-statement gate — strip trailing ; then forbid ;
    #     mid-statement to prevent stacked queries.
    #   * Safe substitution — every parameter is type-validated and
    #     quoted per its declared type before it lands in the SQL.
    #     Numeric types are coerced via int/float and rejected on
    #     non-numeric input; strings are single-quote doubled; date /
    #     boolean values are formatted with their canonical literal
    #     form. (Driver-native parameter binding is the right long-
    #     term answer; this is the smallest landable fix.)
    _MAX_QUERY_LEN = 16 * 1024
    _ALLOWED_LEADING_KEYWORDS = ("select", "with", "show", "pragma")

    @classmethod
    def _validate_report_sql_policy(cls, query: str) -> None:
        """Raise ValueError if ``query`` violates the read-only policy.

        Caller is expected to surface the ValueError as a RunReportResult
        with status=error so the user sees the real reason.
        """
        if not query or not query.strip():
            raise ValueError("Report query is empty")
        if len(query) > cls._MAX_QUERY_LEN:
            raise ValueError(
                f"Report query is too long ({len(query)} > {cls._MAX_QUERY_LEN} chars)"
            )

        # Strip line + block comments before policy checks so a
        # comment can't carry a `;` past the multi-statement gate.
        # This is a deliberately simple stripper — production
        # databases parse SQL much more thoroughly, but for the
        # report-template surface we only need to defeat trivial
        # comment-shadowing tricks.
        import re as _re
        cleaned = _re.sub(r"--[^\n]*", "", query)        # line comments
        cleaned = _re.sub(r"/\*.*?\*/", "", cleaned, flags=_re.S)  # block comments
        cleaned = cleaned.strip().rstrip(";").strip()
        if not cleaned:
            raise ValueError("Report query is empty after stripping comments")

        # Statement-type gate.
        first_word = cleaned.split(None, 1)[0].lower()
        if first_word not in cls._ALLOWED_LEADING_KEYWORDS:
            raise ValueError(
                f"Report queries must be read-only — got {first_word.upper()!r}. "
                f"Allowed: {', '.join(k.upper() for k in cls._ALLOWED_LEADING_KEYWORDS)}."
            )

        # Multi-statement gate. After trimming trailing semicolons we
        # forbid any further ; so a payload can't stack a DML/DDL
        # after the SELECT.
        if ";" in cleaned:
            raise ValueError(
                "Multi-statement SQL is not allowed in reports. "
                "Submit one SELECT (or WITH/SHOW/PRAGMA) per report."
            )

    @staticmethod
    def _safe_substitute_params(
        template: str,
        params: dict,
        parameter_specs: "list",
    ) -> str:
        """Type-validate and literal-escape parameters before splicing
        them into the SQL.

        This is a stopgap. The proper fix is driver-native parameter
        binding — see the TODO at the top of _execute_real_query. The
        stopgap is still a large step up from raw str.replace because:
          * integer / float types reject anything that doesn't parse
          * boolean types map to TRUE/FALSE literals
          * date / datetime types format to ISO-8601 quoted strings
          * string types single-quote-double-escape (SQL standard),
            so `1' OR '1'='1` becomes `'1'' OR ''1''=''1'`.

        Parameters declared on the report but missing from params
        fall back to the report's `default`. If still missing and
        required, the caller has already rejected the run, so we
        just substitute empty string.
        """
        from datetime import date, datetime, time as _time

        def _lit(value, ptype: str) -> str:
            if value is None:
                return "NULL"
            t = (ptype or "string").lower()
            if t in ("integer", "int"):
                try:
                    return str(int(value))
                except (ValueError, TypeError) as e:
                    raise ValueError(f"Expected integer for parameter, got {value!r}") from e
            if t in ("float", "number", "decimal"):
                try:
                    return repr(float(value))
                except (ValueError, TypeError) as e:
                    raise ValueError(f"Expected number for parameter, got {value!r}") from e
            if t in ("boolean", "bool"):
                # Accept true / "true" / 1 / "1" forms; everything
                # else is treated as falsy.
                truthy = value if isinstance(value, bool) else (
                    str(value).strip().lower() in ("true", "1", "yes", "on")
                )
                return "TRUE" if truthy else "FALSE"
            if t in ("date", "datetime", "timestamp"):
                if isinstance(value, (date, datetime)):
                    iso = value.isoformat()
                else:
                    # Reject obvious junk; let the driver parse the
                    # ISO string from there. Single-quote escape it
                    # as a string literal.
                    iso = str(value)
                escaped = iso.replace("'", "''")
                return f"'{escaped}'"
            # Default: treat as string. Double single quotes per SQL
            # standard.
            escaped = str(value).replace("'", "''")
            return f"'{escaped}'"

        resolved = template
        for spec in parameter_specs:
            name = spec.name
            value = params.get(name, spec.default)
            literal = _lit(value, getattr(spec, "type", "string"))
            resolved = resolved.replace(f"{{{{{name}}}}}", literal)
        return resolved

    def _get_credential(self, connection: Connection) -> dict | None:
        """Retrieve raw credential config for a connection, if available.

        2026-05-22 (audit O4) — workspace-aware. The API layer now
        validates ``credential_id`` against the caller's workspace at
        create/update time (audit G1), but this helper is called from
        report-execution paths that have the connection in hand and
        not the original API request. Without the workspace gate
        here, a legacy bad row that references a credential in a
        different workspace would still resolve at run time.

        Policy:
          * If the credential exists AND belongs to the same workspace
            as the connection → return its config.
          * If the credential exists in a different workspace OR
            doesn't exist → log a warning and return None. Returning
            None makes the caller fall back to inline `connection.config`
            (or fail closed when no config), which is the safer of the
            two options.
          * No credential_id on the connection → return None silently
            (inline-config-only path).
        """
        if not connection.credential_id:
            return None
        try:
            from fpulse.main import app_state
            cred_store = app_state.get("credential_store")
            if not cred_store:
                return None
            cred = cred_store.get_raw(connection.credential_id)
            if cred is None:
                logger.warning(
                    "Connection %s references missing credential %s — "
                    "falling back to inline config.",
                    connection.id, connection.credential_id,
                )
                return None
            # Workspace boundary check. Legacy credential rows without
            # workspace_id back-fill to 'default' (per the v6 migration
            # contract), so missing == 'default'.
            conn_ws = (getattr(connection, "workspace_id", None) or "default")
            cred_ws = (getattr(cred, "workspace_id", None) or "default")
            if cred_ws != conn_ws:
                logger.warning(
                    "Cross-workspace credential refused: connection %s "
                    "(workspace=%r) references credential %s "
                    "(workspace=%r). Falling back to inline config; fix "
                    "the connection's credential_id to a credential in "
                    "the same workspace.",
                    connection.id, conn_ws,
                    connection.credential_id, cred_ws,
                )
                return None
            return cred.config if cred else None
        except Exception as exc:
            logger.warning(
                "Credential resolution failed for connection %s: %s",
                getattr(connection, "id", "?"), exc,
            )
            return None

    def _execute_real_query(
        self, connection: Connection, query: str
    ) -> tuple[list[str], list[dict], int] | None:
        config = connection.config or {}
        cred = self._get_credential(connection)

        if connection.type == "sqlite":
            return self._execute_sqlite(config, query)
        elif connection.type in ("postgresql", "mysql", "mssql"):
            return self._execute_db(connection.type, config, cred, query)
        return None

    def _execute_sqlite(
        self, config: dict, query: str
    ) -> tuple[list[str], list[dict], int] | None:
        import os

        db_path = config.get("database") or config.get("file")
        if not db_path or not os.path.isfile(db_path):
            return None

        start = time.time()
        conn = sqlite3.connect(db_path)
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(query)
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            rows = [dict(row) for row in cursor.fetchall()]
            duration_ms = int((time.time() - start) * 1000)
            return columns, rows, duration_ms
        finally:
            conn.close()

    def _execute_db(
        self,
        db_type: str,
        config: dict,
        cred: dict | None,
        query: str,
    ) -> tuple[list[str], list[dict], int] | None:
        merged = {**config}
        if cred:
            merged.update(cred)

        host = merged.get("host")
        database = merged.get("database")

        if not host or not database:
            return None

        result = self._try_native_driver(db_type, merged, query)
        if result is not None:
            return result

        try:
            import duckdb

            start = time.time()
            duck_result = duckdb.sql(query)
            columns = [desc[0] for desc in duck_result.description]
            rows = [dict(zip(columns, row)) for row in duck_result.fetchall()]
            duration_ms = int((time.time() - start) * 1000)
            return columns, rows, duration_ms
        except Exception:
            return None

    def _try_native_driver(
        self,
        db_type: str,
        config: dict,
        query: str,
    ) -> tuple[list[str], list[dict], int] | None:
        host = config.get("host")
        port = config.get("port")
        database = config.get("database")
        user = config.get("user") or config.get("username")
        password = config.get("password")
        schema = config.get("schema")

        try:
            if db_type == "postgresql":
                import psycopg2  # type: ignore
                start = time.time()
                conn = psycopg2.connect(
                    host=host, port=port or 5432,
                    dbname=database, user=user, password=password,
                    connect_timeout=5,
                    options=f"-c search_path={schema}" if schema else None,
                )
                try:
                    cur = conn.cursor()
                    cur.execute(query)
                    columns = [desc[0] for desc in cur.description] if cur.description else []
                    rows = [dict(zip(columns, row)) for row in cur.fetchall()]
                    duration_ms = int((time.time() - start) * 1000)
                    return columns, rows, duration_ms
                finally:
                    conn.close()

            elif db_type == "mysql":
                import pymysql  # type: ignore
                start = time.time()
                conn = pymysql.connect(
                    host=host, port=int(port or 3306),
                    database=database, user=user, password=password,
                    connect_timeout=5,
                )
                try:
                    cur = conn.cursor()
                    cur.execute(query)
                    columns = [desc[0] for desc in cur.description] if cur.description else []
                    rows = [dict(zip(columns, row)) for row in cur.fetchall()]
                    duration_ms = int((time.time() - start) * 1000)
                    return columns, rows, duration_ms
                finally:
                    conn.close()

            elif db_type == "mssql":
                import pyodbc  # type: ignore
                start = time.time()
                conn_str = (
                    f"DRIVER={{ODBC Driver 17 for SQL Server}};"
                    f"SERVER={host},{port or 1433};"
                    f"DATABASE={database};"
                    f"UID={user};PWD={password};"
                    f"Connection Timeout=5;"
                )
                conn = pyodbc.connect(conn_str)
                try:
                    cur = conn.cursor()
                    cur.execute(query)
                    columns = [desc[0] for desc in cur.description] if cur.description else []
                    rows = [dict(zip(columns, row)) for row in cur.fetchall()]
                    duration_ms = int((time.time() - start) * 1000)
                    return columns, rows, duration_ms
                finally:
                    conn.close()

        except ImportError:
            return None
        except Exception:
            return None

        return None

    def _execute_rest_query(
        self, connection: Connection, resolved_query: str
    ) -> tuple[list[str], list[dict], int] | None:
        config = connection.config or {}
        cred = self._get_credential(connection)

        base_url = config.get("base_url", "").rstrip("/")
        if not base_url:
            return None

        match = re.match(r"^(GET|POST)\s+(.+)$", resolved_query.strip())
        if not match:
            return None

        method = match.group(1)
        path = match.group(2).strip()
        url = f"{base_url}{path}" if path.startswith("/") else f"{base_url}/{path}"

        headers = {"Accept": "application/json", "User-Agent": "F-Pulse/0.5.0"}

        auth_config = cred or config
        if auth_config.get("api_key"):
            headers["Authorization"] = f"Bearer {auth_config['api_key']}"
        elif auth_config.get("token"):
            headers["Authorization"] = f"Bearer {auth_config['token']}"

        start = time.time()
        try:
            req = urllib.request.Request(url, method=method, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None

        duration_ms = int((time.time() - start) * 1000)

        if isinstance(body, list):
            rows = body
        elif isinstance(body, dict):
            for key in ("data", "results", "items", "records", "rows"):
                if key in body and isinstance(body[key], list):
                    rows = body[key]
                    break
            else:
                rows = [body]
        else:
            return None

        if not rows:
            return [], [], duration_ms

        columns = list(rows[0].keys()) if rows else []
        clean_rows = [r for r in rows if isinstance(r, dict)]
        return columns, clean_rows, duration_ms

    def _generate_mock_data(
        self, connection: Connection, report: ConnectionReport, params: dict
    ) -> tuple[list[dict], list[str]]:
        try:
            import duckdb

            mock_sql = self._build_mock_query(connection, report, params)
            result = duckdb.sql(mock_sql)
            columns = [desc[0] for desc in result.description]
            rows = [dict(zip(columns, row)) for row in result.fetchall()]
            return rows, columns
        except ImportError:
            return self._fallback_mock_data(connection, report, params)

    def _build_mock_query(
        self, connection: Connection, report: ConnectionReport, params: dict
    ) -> str:
        limit = min(int(params.get("limit", 10)), 100)
        report_lower = report.name.lower()

        if "order" in report_lower:
            return f"""
                SELECT
                    'ORD-' || printf('%04d', i) AS order_id,
                    CASE (i % 5)
                        WHEN 0 THEN 'Alice' WHEN 1 THEN 'Bob'
                        WHEN 2 THEN 'Carol' WHEN 3 THEN 'Dave'
                        ELSE 'Eve'
                    END AS customer,
                    round(random() * 500 + 10, 2) AS amount,
                    CASE (i % 3)
                        WHEN 0 THEN 'completed' WHEN 1 THEN 'pending'
                        ELSE 'shipped'
                    END AS status,
                    current_date - (i % 30) AS order_date
                FROM generate_series(1, {limit}) AS t(i)
            """
        elif "customer" in report_lower or "user" in report_lower:
            return f"""
                SELECT
                    'USR-' || printf('%04d', i) AS user_id,
                    CASE (i % 4)
                        WHEN 0 THEN 'Alice Smith' WHEN 1 THEN 'Bob Jones'
                        WHEN 2 THEN 'Carol White' ELSE 'Dave Brown'
                    END AS name,
                    CASE (i % 4)
                        WHEN 0 THEN 'alice@example.com' WHEN 1 THEN 'bob@example.com'
                        WHEN 2 THEN 'carol@example.com' ELSE 'dave@example.com'
                    END AS email,
                    CASE (i % 3) WHEN 0 THEN 'active' WHEN 1 THEN 'inactive' ELSE 'pending' END AS status
                FROM generate_series(1, {limit}) AS t(i)
            """
        elif "invoice" in report_lower or "payment" in report_lower:
            return f"""
                SELECT
                    'INV-' || printf('%06d', i) AS invoice_id,
                    'CUST-' || printf('%03d', (i % 20) + 1) AS customer_id,
                    round(random() * 10000 + 100, 2) AS total,
                    CASE (i % 4)
                        WHEN 0 THEN 'paid' WHEN 1 THEN 'overdue'
                        WHEN 2 THEN 'pending' ELSE 'draft'
                    END AS status,
                    current_date - (i % 90) AS invoice_date,
                    current_date - (i % 90) + 30 AS due_date
                FROM generate_series(1, {limit}) AS t(i)
            """
        elif "product" in report_lower or "inventory" in report_lower:
            return f"""
                SELECT
                    'SKU-' || printf('%04d', i) AS sku,
                    CASE (i % 5)
                        WHEN 0 THEN 'Widget A' WHEN 1 THEN 'Gadget B'
                        WHEN 2 THEN 'Tool C' WHEN 3 THEN 'Part D'
                        ELSE 'Supply E'
                    END AS product_name,
                    (i * 7 % 500) AS quantity,
                    round(random() * 200 + 5, 2) AS unit_price,
                    CASE (i % 3)
                        WHEN 0 THEN 'Electronics' WHEN 1 THEN 'Hardware'
                        ELSE 'Supplies'
                    END AS category
                FROM generate_series(1, {limit}) AS t(i)
            """
        else:
            return f"""
                SELECT
                    i AS id,
                    'Record ' || i AS name,
                    round(random() * 1000, 2) AS value,
                    CASE (i % 3)
                        WHEN 0 THEN 'active' WHEN 1 THEN 'inactive' ELSE 'pending'
                    END AS status,
                    current_date - (i % 60) AS created_date
                FROM generate_series(1, {limit}) AS t(i)
            """

    def _fallback_mock_data(
        self, connection: Connection, report: ConnectionReport, params: dict
    ) -> tuple[list[dict], list[str]]:
        limit = min(int(params.get("limit", 10)), 100)
        columns = ["id", "name", "value", "status"]
        rows = [
            {
                "id": i,
                "name": f"Record {i}",
                "value": round(random.random() * 1000, 2),
                "status": random.choice(["active", "inactive", "pending"]),
            }
            for i in range(1, limit + 1)
        ]
        return rows, columns

    # ── Test Connection (real) ──

    def test_connection(self, connection_id: str) -> dict:
        try:
            connection = self.get(connection_id)
            if not connection:
                return {"status": "error", "message": "Connection not found"}

            config = dict(connection.config)
            if connection.credential_id:
                try:
                    from fpulse.main import app_state
                    cred_store = app_state["credential_store"]
                    cred = cred_store.get_raw(connection.credential_id)
                    if cred and cred.config:
                        config.update(cred.config)
                except Exception:
                    pass

            conn_type = connection.type.lower()

            if conn_type == "custom":
                return {
                    "status": "ok",
                    "message": "Custom connections cannot be auto-tested. Marked as reachable.",
                    "latency_ms": 0,
                }

            # 2026-05-23: HTTP-flavoured fallbacks. The proper testers
            # (in connections/tester.py) own the Oracle/SAP product
            # families; this branch is for legacy code paths that still
            # round-trip through the older "is the URL reachable" probe.
            if conn_type in ("rest_api", "graphql", "oracle_api", "oracle_fusion", "oracle_bip"):
                base_url = config.get("base_url") or config.get("url") or config.get("endpoint")
                if not base_url:
                    return {"status": "error", "message": "No base_url configured for HTTP test"}
                return self._test_http(base_url, connection)

            host, port = self._resolve_host_port(conn_type, config)
            if not host:
                return {"status": "error", "message": f"No host configured for {conn_type} connection"}

            return self._test_tcp(host, port, connection)

        except Exception as exc:
            return {"status": "error", "message": f"Connection test error: {exc}"}

    def _resolve_host_port(self, conn_type: str, config: dict) -> tuple[str | None, int]:
        defaults = {
            "postgresql": 5432, "mysql": 3306, "mssql": 1433, "oracle": 1521,
            "sqlite": 0, "mongodb": 27017, "s3": 443, "gcs": 443,
            "azure_blob": 443, "kafka": 9092, "ftp": 21, "snowflake": 443,
            "bigquery": 443, "redshift": 5439, "databricks": 443,
        }

        if conn_type == "sqlite":
            return ("localhost", 0)

        if conn_type == "s3":
            endpoint = config.get("endpoint") or config.get("endpoint_url")
            if endpoint:
                return self._parse_url_host_port(endpoint, 443)
            region = config.get("region", "us-east-1")
            return (f"s3.{region}.amazonaws.com", 443)

        if conn_type == "gcs":
            endpoint = config.get("endpoint")
            if endpoint:
                return self._parse_url_host_port(endpoint, 443)
            return ("storage.googleapis.com", 443)

        if conn_type == "azure_blob":
            account = config.get("account_name") or config.get("account")
            if account:
                return (f"{account}.blob.core.windows.net", 443)
            endpoint = config.get("endpoint")
            if endpoint:
                return self._parse_url_host_port(endpoint, 443)
            return (None, 443)

        if conn_type == "snowflake":
            account = config.get("account", "")
            if account:
                host = account if "." in account and "snowflake" in account else f"{account}.snowflakecomputing.com"
                return (host, 443)
            return (config.get("host"), 443)

        if conn_type == "bigquery":
            return ("bigquery.googleapis.com", 443)

        if conn_type == "databricks":
            workspace = config.get("host") or config.get("workspace_url") or config.get("endpoint")
            if workspace:
                return self._parse_url_host_port(workspace, 443)
            return (None, 443)

        if conn_type == "redshift":
            host = config.get("host") or config.get("endpoint")
            return (host, int(config.get("port", defaults.get(conn_type, 5439))))

        if conn_type == "kafka":
            brokers = config.get("brokers") or config.get("bootstrap_servers") or config.get("host")
            if brokers:
                first = str(brokers).split(",")[0].strip()
                if ":" in first:
                    parts = first.rsplit(":", 1)
                    return (parts[0], int(parts[1]))
                return (first, 9092)
            return (None, 9092)

        host = config.get("host") or config.get("server")
        port = int(config.get("port", defaults.get(conn_type, 0)))
        return (host, port)

    @staticmethod
    def _parse_url_host_port(url: str, default_port: int) -> tuple[str, int]:
        clean = url
        for prefix in ("https://", "http://"):
            if clean.lower().startswith(prefix):
                clean = clean[len(prefix):]
                break
        clean = clean.split("/")[0]
        if ":" in clean:
            parts = clean.rsplit(":", 1)
            try:
                return (parts[0], int(parts[1]))
            except ValueError:
                return (parts[0], default_port)
        return (clean, default_port)

    @staticmethod
    def _test_tcp(host: str, port: int, connection) -> dict:
        if connection.type.lower() == "sqlite":
            return {
                "status": "ok",
                "message": f"SQLite connection '{connection.name}' validated (file-based, no network test needed)",
                "latency_ms": 0,
            }

        start = time.time()
        try:
            sock = socket.create_connection((host, port), timeout=5)
            latency_ms = int((time.time() - start) * 1000)
            sock.close()
            return {
                "status": "ok",
                "message": f"TCP connection to {host}:{port} succeeded for '{connection.name}'",
                "latency_ms": max(latency_ms, 1),
            }
        except socket.timeout:
            return {"status": "error", "message": f"Connection timed out reaching {host}:{port} (5s timeout)"}
        except OSError as exc:
            return {"status": "error", "message": f"Connection failed to {host}:{port}: {exc}"}

    @staticmethod
    def _test_http(url: str, connection) -> dict:
        start = time.time()
        try:
            req = urllib.request.Request(url, method="HEAD")
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            resp = urllib.request.urlopen(req, timeout=5, context=ctx)
            latency_ms = int((time.time() - start) * 1000)
            return {
                "status": "ok",
                "message": f"HTTP {resp.status} from {url} for '{connection.name}'",
                "latency_ms": max(latency_ms, 1),
            }
        except urllib.error.HTTPError as exc:
            latency_ms = int((time.time() - start) * 1000)
            if exc.code in (401, 403, 405):
                return {
                    "status": "ok",
                    "message": f"Server reachable (HTTP {exc.code}) at {url} — auth required",
                    "latency_ms": max(latency_ms, 1),
                }
            return {"status": "error", "message": f"HTTP error {exc.code} from {url}: {exc.reason}"}
        except urllib.error.URLError as exc:
            return {"status": "error", "message": f"Cannot reach {url}: {exc.reason}"}
        except socket.timeout:
            return {"status": "error", "message": f"HTTP request timed out for {url} (5s timeout)"}
        except Exception as exc:
            return {"status": "error", "message": f"HTTP test failed for {url}: {exc}"}

    # ── Seed Data ──

    def _seed(self):
        orders_conn = Connection(
            id="conn_orders_db",
            name="Orders DB",
            type="postgresql",
            project_id=None,
            config={
                "host": "db.example.com",
                "port": 5432,
                "database": "orders",
                "schema": "public",
            },
            description="Production orders database (Global)",
            tags=["production", "orders", "global"],
        )
        self._save_connection(orders_conn)

        r1 = ConnectionReport(
            id="rpt_recent_orders",
            connection_id=orders_conn.id,
            name="Recent Orders",
            description="Fetch recent orders with optional status filter",
            query_template="SELECT * FROM orders WHERE status = '{{status}}' ORDER BY order_date DESC LIMIT {{limit}}",
            parameters=[
                ReportParameter(name="status", type="string", default="completed", required=False),
                ReportParameter(name="limit", type="integer", default=25, required=False),
            ],
        )
        r2 = ConnectionReport(
            id="rpt_order_summary",
            connection_id=orders_conn.id,
            name="Order Summary by Customer",
            description="Aggregate order totals grouped by customer",
            query_template="SELECT customer, COUNT(*) as order_count, SUM(amount) as total FROM orders WHERE order_date >= '{{start_date}}' GROUP BY customer ORDER BY total DESC LIMIT {{limit}}",
            parameters=[
                ReportParameter(name="start_date", type="date", default="2025-01-01", required=True),
                ReportParameter(name="limit", type="integer", default=10, required=False),
            ],
        )
        self._save_report(r1)
        self._save_report(r2)

        # 2026-05-23 (T4 + U1): seed targets the canonical oracle_fusion
        # type now that the family was split. Legacy installs that saved
        # under `oracle_api` continue to load via the DEPRECATED_TYPE_ALIASES
        # resolver — no migration sweep needed.
        erp_conn = Connection(
            id="conn_oracle_erp",
            name="Oracle Fusion (sample)",
            type="oracle_fusion",
            project_id="default",
            config={
                "base_url": "https://my-pod.fa.us2.oraclecloud.com",
                "api_family": "fscm",
                "auth_type": "basic",
                "timeout_seconds": 30,
            },
            description="Oracle Fusion Cloud REST API — placeholder seed.",
            tags=["erp", "oracle", "fusion", "finance"],
        )
        self._save_connection(erp_conn)

        snow_conn = Connection(
            id="conn_snowflake_dw",
            name="Snowflake Data Warehouse",
            type="snowflake",
            project_id=None,
            config={
                "account": "abc12345.us-east-1",
                "warehouse": "COMPUTE_WH",
                "database": "ANALYTICS",
                "schema": "PUBLIC",
            },
            description="Shared Snowflake warehouse for analytics (Global)",
            tags=["analytics", "snowflake", "global"],
        )
        self._save_connection(snow_conn)

        r3 = ConnectionReport(
            id="rpt_open_invoices",
            connection_id=erp_conn.id,
            name="Open Invoices",
            description="List open invoices from Oracle ERP above a threshold",
            query_template="GET /invoices?status=open&min_amount={{min_amount}}&limit={{limit}}",
            parameters=[
                ReportParameter(name="min_amount", type="float", default=1000.0, required=False),
                ReportParameter(name="limit", type="integer", default=20, required=False),
            ],
        )
        r4 = ConnectionReport(
            id="rpt_payment_status",
            connection_id=erp_conn.id,
            name="Payment Status",
            description="Check payment status for a specific customer",
            query_template="GET /payments?customer_id={{customer_id}}&from_date={{from_date}}",
            parameters=[
                ReportParameter(name="customer_id", type="string", required=True),
                ReportParameter(name="from_date", type="date", default="2025-01-01", required=False),
            ],
        )
        self._save_report(r3)
        self._save_report(r4)
