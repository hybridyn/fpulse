"""SQL Server connector smoke-test (2026-06-08, P3 deepening pass #2).

End-to-end probe of one saved SQL Server connection (or an ad-hoc
connection string) reporting PASS/FAIL per check. Mirrors the
PostgreSQL smoke test so operators get the same answer shape across
connectors.

USAGE
-----

  # Against a saved F-Pulse connection (run from the repo root):
  python backend/scripts/sqlserver_smoke_test.py \
      --connection-id conn-1234

  # Against an explicit ODBC DSN-style connection string:
  python "...sqlserver_smoke_test.py" --connection-string \
      "DRIVER={ODBC Driver 18 for SQL Server};SERVER=db.example.com,1433;DATABASE=orders;UID=app;PWD=pass;Encrypt=yes;TrustServerCertificate=no"

  # Focus on one specific (schema, table):
  python "...sqlserver_smoke_test.py" --connection-string ... \
      --probe-schema dbo --probe-table Customers

EXIT CODE
---------

  0  every check passed
  1  one or more checks failed (details printed)

CHECKS
------

  [1] connect              - can we open a connection at all?
  [2] version              - SELECT @@VERSION round-trips a string
  [3] list_schemas         - INFORMATION_SCHEMA.SCHEMATA query succeeds
  [4] list_tables          - tables in --probe-schema (default 'dbo')
  [5] describe_table       - INFORMATION_SCHEMA.COLUMNS for --probe-table
  [6] read_sample          - SELECT TOP 5 * FROM probe_table

Read-only. Safe to run against production. If FPULSE_URL +
FPULSE_TOKEN are set AND --connection-id was used, the final
pass/fail is also POSTed to /api/steward/connector-health so the
result feeds Steward's connector-health detector.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any


GREEN = "\033[92m"
RED = "\033[91m"
DIM = "\033[2m"
RESET = "\033[0m"


def _color(s: str, c: str) -> str:
    if not sys.stdout.isatty():
        return s
    return f"{c}{s}{RESET}"


def _print_step(num: int, name: str, ok: bool, ms: float, detail: str = "") -> None:
    tag = _color("PASS", GREEN) if ok else _color("FAIL", RED)
    print(f"  [{num}] {name:<20} {tag}  ({ms:.1f} ms)")
    if detail:
        print(_color(f"        {detail}", DIM))


def _resolve_connection_string(args: argparse.Namespace) -> tuple[str, str]:
    if args.connection_string:
        return args.connection_string, "<connection string from --connection-string>"
    if not args.connection_id:
        sys.exit("ERROR: pass either --connection-string or --connection-id")
    try:
        from fpulse.main import app_state
        conn_store = app_state.get("connection_store")
        cred_store = app_state.get("credential_store")
        if conn_store is None:
            sys.exit("ERROR: connection_store not available - is the F-Pulse process running?")
        connection = conn_store.get(args.connection_id)
        if connection is None:
            sys.exit(f"ERROR: connection {args.connection_id} not found")
        cfg = dict(connection.config or {})
        if connection.credential_id and cred_store:
            cred = cred_store.get_raw(connection.credential_id)
            if cred and cred.config:
                cfg.update(cred.config)
        # Build an ODBC connection string from standard fields.
        driver = cfg.get("driver") or "ODBC Driver 18 for SQL Server"
        server = cfg.get("server") or cfg.get("host", "localhost")
        port = cfg.get("port", 1433)
        db = cfg.get("database") or cfg.get("dbname") or "master"
        user = cfg.get("username") or cfg.get("user") or ""
        pwd = cfg.get("password") or ""
        encrypt = cfg.get("encrypt", "yes")
        trust = cfg.get("trust_server_certificate", "no")
        parts = [
            f"DRIVER={{{driver}}}",
            f"SERVER={server},{port}",
            f"DATABASE={db}",
            f"Encrypt={encrypt}",
            f"TrustServerCertificate={trust}",
        ]
        if user:
            parts.append(f"UID={user}")
            parts.append(f"PWD={pwd}")
        else:
            # Windows Authentication
            parts.append("Trusted_Connection=yes")
        cs = ";".join(parts)
        return cs, f"connection={args.connection_id} ({connection.name})"
    except SystemExit:
        raise
    except Exception as e:
        sys.exit(f"ERROR resolving connection: {e}")


def _connect(cs: str):
    """Open a pyodbc connection. Imported lazily so the script fails
    fast with a friendly message if pyodbc isn't installed."""
    try:
        import pyodbc  # type: ignore
    except ImportError:
        sys.exit(
            "ERROR: pyodbc is not installed. Install with:\n"
            "  pip install pyodbc\n"
            "and ensure the matching ODBC driver is on the system:\n"
            "  Windows: 'ODBC Driver 18 for SQL Server' (Microsoft installer)\n"
            "  Linux:   msodbcsql18 (apt/yum/zypper per distro)\n"
            "  macOS:   brew tap microsoft/mssql-release; brew install msodbcsql18"
        )
    return pyodbc.connect(cs)


def _run_step(name: str, fn) -> tuple[bool, float, str]:
    t0 = time.perf_counter()
    try:
        detail = fn() or ""
        return True, (time.perf_counter() - t0) * 1000, str(detail)
    except Exception as e:
        return False, (time.perf_counter() - t0) * 1000, f"{type(e).__name__}: {e}"


def _post_steward_outcome(connection_id: str, ok: bool, error_message: str) -> None:
    url = os.environ.get("FPULSE_URL")
    token = os.environ.get("FPULSE_TOKEN")
    if not url or not token or not connection_id:
        return
    import urllib.request
    body = json.dumps({
        "connection_id": connection_id,
        "ok": ok,
        "error_message": error_message[:500] if not ok else "",
    }).encode()
    req = urllib.request.Request(
        f"{url.rstrip('/')}/api/steward/connector-health",
        data=body,
        headers={"Content-Type": "application/json",
                  "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="SQL Server connector smoke-test")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--connection-id", help="Saved F-Pulse connection id")
    src.add_argument("--connection-string", help="Full ODBC connection string")
    ap.add_argument("--probe-schema", default="dbo",
                     help="Schema for list_tables / describe_table (default: dbo)")
    ap.add_argument("--probe-table", default=None,
                     help="Specific table to describe + sample. If omitted, "
                          "we pick the first table in --probe-schema.")
    args = ap.parse_args()

    cs, label = _resolve_connection_string(args)
    print(f"\n  SQL Server smoke-test")
    print(f"  Target: {label}")
    print(f"  Schema: {args.probe_schema}")
    print()

    overall_ok = True

    conn = None
    ok, ms, detail = _run_step("connect", lambda: (
        globals().__setitem__("conn", _connect(cs)) or "connected"
    ))
    _print_step(1, "connect", ok, ms, detail if not ok else "")
    if not ok:
        _post_steward_outcome(args.connection_id or "", False, detail)
        return 1
    conn = globals()["conn"]

    try:
        cur = conn.cursor()

        # [2] version
        ok, ms, detail = _run_step("version", lambda: (
            cur.execute("SELECT @@VERSION") or (str(cur.fetchone()[0])[:60])
        ))
        _print_step(2, "version", ok, ms, detail)
        overall_ok = overall_ok and ok

        # [3] list_schemas
        schemas: list[str] = []
        def _list_schemas():
            cur.execute(
                "SELECT schema_name FROM INFORMATION_SCHEMA.SCHEMATA "
                "ORDER BY schema_name"
            )
            rows = cur.fetchall()
            schemas[:] = [r[0] for r in rows]
            return f"{len(schemas)} schemas"
        ok, ms, detail = _run_step("list_schemas", _list_schemas)
        _print_step(3, "list_schemas", ok, ms, detail)
        overall_ok = overall_ok and ok

        # [4] list_tables in probe-schema
        tables: list[str] = []
        def _list_tables():
            cur.execute(
                "SELECT TOP 50 TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = ? AND TABLE_TYPE = 'BASE TABLE' "
                "ORDER BY TABLE_NAME",
                (args.probe_schema,),
            )
            rows = cur.fetchall()
            tables[:] = [r[0] for r in rows]
            return f"{len(tables)} tables in {args.probe_schema}"
        ok, ms, detail = _run_step("list_tables", _list_tables)
        _print_step(4, "list_tables", ok, ms, detail)
        overall_ok = overall_ok and ok

        probe_table = args.probe_table
        if probe_table is None and tables:
            probe_table = tables[0]

        # [5] describe_table
        if probe_table:
            def _describe():
                cur.execute(
                    "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE "
                    "FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? "
                    "ORDER BY ORDINAL_POSITION",
                    (args.probe_schema, probe_table),
                )
                rows = cur.fetchall()
                return f"{len(rows)} columns in {args.probe_schema}.{probe_table}"
            ok, ms, detail = _run_step("describe_table", _describe)
            _print_step(5, "describe_table", ok, ms, detail)
            overall_ok = overall_ok and ok
        else:
            _print_step(5, "describe_table", True, 0.0,
                          f"skipped (no tables in {args.probe_schema})")

        # [6] read_sample - use QUOTENAME for safe identifier escaping
        # (pyodbc parameterised queries don't substitute identifiers).
        if probe_table:
            def _sample():
                # QUOTENAME() escapes brackets; pass schema + table as
                # values, then concat in pure SQL via QUOTENAME for
                # safety against weird identifier characters.
                cur.execute(
                    "DECLARE @s NVARCHAR(128) = ?; "
                    "DECLARE @t NVARCHAR(128) = ?; "
                    "DECLARE @sql NVARCHAR(MAX) = "
                    "  N'SELECT TOP 5 * FROM ' + QUOTENAME(@s) + N'.' + QUOTENAME(@t); "
                    "EXEC sp_executesql @sql",
                    (args.probe_schema, probe_table),
                )
                rows = cur.fetchall()
                return f"{len(rows)} rows fetched"
            ok, ms, detail = _run_step("read_sample", _sample)
            _print_step(6, "read_sample", ok, ms, detail)
            overall_ok = overall_ok and ok
        else:
            _print_step(6, "read_sample", True, 0.0, "skipped (no probe table)")

        cur.close()
    finally:
        try:
            conn.close()
        except Exception:
            pass

    print()
    if overall_ok:
        print(f"  Overall: {_color('PASS', GREEN)} - all checks passed")
    else:
        print(f"  Overall: {_color('FAIL', RED)} - one or more checks failed")
    print()

    _post_steward_outcome(args.connection_id or "", overall_ok, "" if overall_ok else "smoke-test failed")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
