"""[LIVE-SMOKE] E3.1 — driver-level cancellation of a blocked query.

The E3.1 wiring (register_connection_cancel + the executor's step-
boundary check) is unit-tested with a fake connection. This script is
the one piece that needs a live database: it proves that cancelling a
run actually INTERRUPTS a query already blocked inside the driver —
the whole point of driver-level cancel (a cooperative flag alone can't
reach a thread parked in a 30-second SELECT).

Run it when you have a test Postgres:

    # From the repo root (PowerShell):
    $env:PYTHONPATH = "backend"
    .venv\\Scripts\\python.exe backend\\scripts\\smoke_cancel_query.py `
        --host localhost --port 5432 --database testdb `
        --user postgres --password secret

What it does:
  1. Opens a psycopg2 connection and creates a CancellationToken for a
     fake run_id, registering conn.cancel() via register_connection_cancel.
  2. Spawns a thread that runs SELECT pg_sleep(30) — a 30-second block.
  3. After 2 seconds, calls cancel_run(run_id) from the main thread.
  4. Asserts the query thread returns within ~5 seconds (interrupted),
     NOT after the full 30 — proving the native cancel reached the
     blocked driver call.

Exit code 0 = cancellation verified; non-zero = the query ran to
completion (cancel did NOT propagate).

Supports --dialect postgres (psycopg2 Connection.cancel()) and
--dialect mssql (pyodbc Cursor.cancel(); pass --trusted for Windows
auth). Both register their thread-safe cancel via the same E3.1
register_connection_cancel() helper.
"""
from __future__ import annotations

import argparse
import sys
import threading
import time


def main() -> int:
    ap = argparse.ArgumentParser(description="E3.1 live query-cancel smoke test")
    ap.add_argument("--dialect", default="postgres", help="postgres | mssql")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", default=0, type=int, help="default 5432 (postgres) / 1433 (mssql)")
    ap.add_argument("--database", default="")
    ap.add_argument("--user", default="")
    ap.add_argument("--password", default="")
    ap.add_argument("--trusted", action="store_true",
                    help="mssql: Windows/AD integrated auth (Trusted_Connection)")
    ap.add_argument("--sleep", default=30, type=int, help="block duration seconds")
    ap.add_argument("--cancel-after", default=2.0, type=float)
    ap.add_argument("--deadline", default=10.0, type=float,
                    help="max seconds the query may take before we call it a FAIL")
    args = ap.parse_args()

    from fpulse.engine.cancellation import (
        create_token, cancel_run, clear_token, register_connection_cancel,
    )

    run_id = "smoke-cancel-run"
    create_token(run_id)

    dialect = args.dialect.lower()
    # Build (connection, cancelable-object, blocking-SQL) per dialect.
    # The "cancelable" is whatever the connector would register: psycopg2
    # Connection.cancel() is thread-safe; for pyodbc it's Cursor.cancel().
    if dialect in ("postgres", "postgresql"):
        try:
            import psycopg2  # type: ignore
        except ImportError:
            print("FAIL: psycopg2 not installed (pip install psycopg2-binary)")
            return 3
        conn = psycopg2.connect(
            host=args.host, port=args.port or 5432,
            dbname=args.database or "postgres",
            user=args.user or "postgres", password=args.password,
            connect_timeout=10,
        )
        cursor = conn.cursor()
        cancelable = conn
        blocking_sql = f"SELECT pg_sleep({args.sleep})"
    elif dialect in ("mssql", "sqlserver"):
        try:
            import pyodbc  # type: ignore
        except ImportError:
            print("FAIL: pyodbc not installed")
            return 3
        auth = ("Trusted_Connection=yes" if (args.trusted or not args.user)
                else f"UID={args.user};PWD={args.password}")
        cs = (f"DRIVER={{ODBC Driver 18 for SQL Server}};"
              f"SERVER={args.host},{args.port or 1433};"
              f"DATABASE={args.database or 'tempdb'};{auth};"
              f"Encrypt=yes;TrustServerCertificate=yes")
        conn = pyodbc.connect(cs)
        cursor = conn.cursor()
        cancelable = cursor  # pyodbc Cursor.cancel() is the thread-safe interrupt
        s = int(args.sleep)
        blocking_sql = "WAITFOR DELAY '%02d:%02d:%02d'" % (s // 3600, (s % 3600) // 60, s % 60)
    else:
        print(f"FAIL: unknown dialect '{args.dialect}' (use postgres or mssql)")
        return 2

    register_connection_cancel(run_id, cancelable)

    outcome = {"done": False, "elapsed": None, "error": None}

    def _run_query():
        t0 = time.time()
        try:
            cursor.execute(blocking_sql)
            try:
                cursor.fetchall()
            except Exception:
                pass
        except Exception as e:  # noqa: BLE001 — cancel surfaces as an error; expected
            outcome["error"] = repr(e)
        finally:
            outcome["elapsed"] = time.time() - t0
            outcome["done"] = True

    th = threading.Thread(target=_run_query, daemon=True)
    th.start()

    time.sleep(args.cancel_after)
    print(f"cancelling run after {args.cancel_after}s (query asked for "
          f"{args.sleep}s) ...")
    cancel_run(run_id)

    th.join(timeout=args.deadline)
    clear_token(run_id)
    try:
        conn.close()
    except Exception:
        pass

    if not outcome["done"]:
        print(f"FAIL: query did not return within {args.deadline}s — cancel "
              f"did NOT interrupt the blocked driver call.")
        return 1
    elapsed = outcome["elapsed"] or 0.0
    if elapsed >= args.sleep - 1:
        print(f"FAIL: query ran ~{elapsed:.1f}s (full duration) — cancel "
              f"did NOT propagate.")
        return 1
    print(f"PASS: query interrupted after ~{elapsed:.1f}s "
          f"(error: {outcome['error']}). Driver-level cancel verified — E3.1 works.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
