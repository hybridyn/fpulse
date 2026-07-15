"""PostgreSQL connector smoke-test (2026-06-07, P3 deepening pass).

Runs an end-to-end probe of one saved PostgreSQL connection (or an
ad-hoc DSN) and reports PASS/FAIL per check. Designed to be the script
operators run to answer "did F-Pulse's PostgreSQL connector actually
work against MY database?" — not against test fixtures.

USAGE
-----

  # Against a saved connection (looks up config + credentials from
  # the running F-Pulse instance):
  python -m fpulse.scripts.postgres_smoke_test --connection-id conn-1234

  # Against a direct DSN (no F-Pulse state needed):
  python -m fpulse.scripts.postgres_smoke_test --dsn \
      "postgresql://user:pass@host:5432/dbname"

  # Optional - pick a specific schema/table to read 5 rows from:
  python -m fpulse.scripts.postgres_smoke_test --dsn ... \
      --probe-schema public --probe-table users

EXIT CODE
---------

  0  every check passed
  1  one or more checks failed (details printed)

CHECKS
------

  [1] connect              - can we open a connection at all?
  [2] version              - SELECT version() round-trips a string
  [3] list_schemas         - information_schema query succeeds
  [4] list_tables          - tables in --probe-schema (default 'public')
  [5] describe_table       - columns + types for --probe-table (if any
                              tables present; skipped otherwise)
  [6] read_sample          - SELECT * FROM probe_table LIMIT 5
                              (skipped if no probe_table)

Each check prints its result + wall-clock duration in milliseconds.
The script DOES NOT mutate any data - it's pure reads. Safe to run
against production.

If you also want this connector's outcome to feed Steward's
connector-health detector, set FPULSE_URL and FPULSE_TOKEN in the
environment - the script will POST the final pass/fail to
/api/steward/connector-health at the end. See the bottom of this file.
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


def _resolve_dsn(args: argparse.Namespace) -> tuple[str, str]:
    """Returns (dsn, label_for_output). Either from --dsn flag or by
    looking up a saved connection via app_state."""
    if args.dsn:
        return args.dsn, "<dsn from --dsn>"
    if not args.connection_id:
        sys.exit("ERROR: pass either --dsn or --connection-id")
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
        # Build a DSN from the standard pg connection fields.
        host = cfg.get("host", "localhost")
        port = cfg.get("port", 5432)
        db = cfg.get("database") or cfg.get("dbname") or "postgres"
        user = cfg.get("username") or cfg.get("user") or ""
        pwd = cfg.get("password") or ""
        # urllib.parse.quote so weird chars in pwd survive.
        from urllib.parse import quote
        creds = f"{quote(user)}:{quote(pwd)}@" if user else ""
        dsn = f"postgresql://{creds}{host}:{port}/{db}"
        return dsn, f"connection={args.connection_id} ({connection.name})"
    except SystemExit:
        raise
    except Exception as e:
        sys.exit(f"ERROR resolving connection: {e}")


def _connect(dsn: str):
    """Open a psycopg2 connection. Imported lazily so the script
    fails fast with a friendly message if psycopg2 isn't installed."""
    try:
        import psycopg2  # type: ignore
    except ImportError:
        sys.exit(
            "ERROR: psycopg2 is not installed. Install one of:\n"
            "  pip install psycopg2-binary   # easy; bundled libpq\n"
            "  pip install psycopg2          # needs system libpq-dev"
        )
    return psycopg2.connect(dsn)


def _run_step(name: str, fn) -> tuple[bool, float, str]:
    t0 = time.perf_counter()
    try:
        detail = fn() or ""
        return True, (time.perf_counter() - t0) * 1000, str(detail)
    except Exception as e:
        return False, (time.perf_counter() - t0) * 1000, f"{type(e).__name__}: {e}"


def _post_steward_outcome(connection_id: str, ok: bool, error_message: str) -> None:
    """Best-effort POST to /api/steward/connector-health so the smoke
    result feeds the connector-health detector. Silent if FPULSE_URL
    or FPULSE_TOKEN aren't set."""
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
    ap = argparse.ArgumentParser(description="PostgreSQL connector smoke-test")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--connection-id", help="Saved F-Pulse connection id")
    src.add_argument("--dsn", help="postgresql://user:pass@host:port/db")
    ap.add_argument("--probe-schema", default="public",
                     help="Schema for list_tables / describe_table (default: public)")
    ap.add_argument("--probe-table", default=None,
                     help="Specific table to describe + sample. If omitted, "
                          "we pick the first table in --probe-schema.")
    args = ap.parse_args()

    dsn, label = _resolve_dsn(args)
    print(f"\n  PostgreSQL smoke-test")
    print(f"  Target: {label}")
    print(f"  Schema: {args.probe_schema}")
    print()

    overall_ok = True
    overall_detail = ""

    # [1] connect
    conn = None
    ok, ms, detail = _run_step("connect", lambda: (
        globals().__setitem__("conn", _connect(dsn)) or "connected"
    ))
    _print_step(1, "connect", ok, ms, detail if not ok else "")
    if not ok:
        _post_steward_outcome(args.connection_id or "", False, detail)
        return 1
    conn = globals()["conn"]
    overall_detail = "all checks passed"

    try:
        cur = conn.cursor()

        # [2] version
        ok, ms, detail = _run_step("version", lambda: (
            cur.execute("SELECT version()") or (cur.fetchone()[0][:60])
        ))
        _print_step(2, "version", ok, ms, detail)
        overall_ok = overall_ok and ok

        # [3] list_schemas
        schemas: list[str] = []
        def _list_schemas():
            cur.execute(
                "SELECT schema_name FROM information_schema.schemata "
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
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = %s ORDER BY table_name LIMIT 50",
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
                    "SELECT column_name, data_type, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_schema = %s AND table_name = %s "
                    "ORDER BY ordinal_position",
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

        # [6] read_sample
        if probe_table:
            # Use identifier quoting via the AsIs trick - psycopg2's
            # default %s is for values, not identifiers; safer than
            # f-string concat against an arbitrary table name.
            from psycopg2 import sql  # type: ignore
            def _sample():
                query = sql.SQL("SELECT * FROM {}.{} LIMIT 5").format(
                    sql.Identifier(args.probe_schema),
                    sql.Identifier(probe_table),
                )
                cur.execute(query)
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
        print(f"  Overall: {_color('PASS', GREEN)} - {overall_detail}")
    else:
        print(f"  Overall: {_color('FAIL', RED)} - one or more checks failed")
    print()

    _post_steward_outcome(args.connection_id or "", overall_ok, "" if overall_ok else "smoke-test failed")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
