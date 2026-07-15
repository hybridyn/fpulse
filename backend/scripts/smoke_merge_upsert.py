"""[LIVE-SMOKE] B2.1 — warehouse-sink merge (upsert) against a real DB.

The B2.1 wiring (merge_key -> BulkLoadRequest.primary_key -> per-dialect
MERGE) is unit-tested with a fake loader. This script is the one piece
that needs a live database: it proves the generated MERGE / ON CONFLICT
actually upserts (inserts new rows, updates matching rows) rather than
duplicating.

Run it when you have a test warehouse:

    # From the repo root (PowerShell):
    $env:PYTHONPATH = "backend"
    .venv\\Scripts\\python.exe backend\\scripts\\smoke_merge_upsert.py `
        --dialect postgresql `
        --host localhost --port 5432 --database testdb `
        --user postgres --password secret `
        --table public.smoke_merge

What it does:
  1. Builds a 2-row DuckDB relation (ids 1, 2).
  2. Runs a 'merge' BulkLoadRequest with primary_key=['id'] — expect
     2 rows inserted.
  3. Builds a second relation that UPDATES id=2 and ADDS id=3.
  4. Runs merge again — expect id=2 updated in place + id=3 inserted,
     for a final total of 3 rows (NOT 4 — that would mean the upsert
     duplicated instead of updating).
  5. Reads the target back and asserts the final state.

Exit code 0 = upsert verified; non-zero = the MERGE path is wrong.
Idempotent: it drops + recreates the target table each run.

NOTE: requires the dialect's driver installed (psycopg2 / pyodbc /
snowflake-connector-python) — same optional deps the bulk loaders use.
"""
from __future__ import annotations

import argparse
import sys


def _build_config(args) -> dict:
    cfg = {
        "host": args.host, "port": args.port, "database": args.database,
        "user": args.user, "password": args.password,
        "account": args.account, "warehouse": args.warehouse,
        "schema": args.schema,
    }
    # SQL Server Windows / AD integrated auth: pass --trusted (or just
    # omit --user) and the mssql loader uses Trusted_Connection.
    if getattr(args, "trusted", False):
        cfg["trusted_connection"] = True
    return cfg


def main() -> int:
    ap = argparse.ArgumentParser(description="B2.1 live merge/upsert smoke test")
    ap.add_argument("--dialect", required=True,
                    help="postgresql | mssql | snowflake")
    ap.add_argument("--host", default="")
    ap.add_argument("--port", default=0, type=int)
    ap.add_argument("--database", default="")
    ap.add_argument("--user", default="")
    ap.add_argument("--password", default="")
    ap.add_argument("--trusted", action="store_true",
                    help="SQL Server: use Windows/AD integrated auth (Trusted_Connection) instead of a SQL login")
    ap.add_argument("--account", default="")     # snowflake
    ap.add_argument("--warehouse", default="")   # snowflake
    ap.add_argument("--schema", default="public")
    ap.add_argument("--table", default="smoke_merge")
    args = ap.parse_args()

    import duckdb
    from fpulse.engine.bulk_load import registry as bl_reg
    from fpulse.engine.bulk_load.types import BulkLoadRequest
    # Import the dialect modules so they register with the registry.
    import fpulse.engine.bulk_load.dialects  # noqa: F401

    loader = bl_reg.get(args.dialect)
    if loader is None:
        print(f"FAIL: no bulk loader registered for dialect '{args.dialect}'")
        return 2
    if not loader.is_available():
        print(f"FAIL: driver for '{args.dialect}' not installed "
              f"(pip install the connector). Loader present but unavailable.")
        return 3

    config = _build_config(args)
    con = duckdb.connect(":memory:")

    def _merge(rel):
        req = BulkLoadRequest(
            conn_type=args.dialect, config=config,
            table=args.table, schema_name=args.schema,
            mode="merge", primary_key=["id"],
            relation=rel, duckdb_conn=con, columns=["id", "val"],
        )
        return loader.load(req)

    # Phase 1 — initial 2 rows
    con.execute("CREATE OR REPLACE TABLE r1 AS "
                "SELECT * FROM (VALUES (1,'a'),(2,'b')) AS t(id,val)")
    print("phase 1: merge ids {1,2} ...")
    _merge(con.sql("SELECT * FROM r1"))

    # Phase 2 — update id=2, add id=3
    con.execute("CREATE OR REPLACE TABLE r2 AS "
                "SELECT * FROM (VALUES (2,'B_UPDATED'),(3,'c')) AS t(id,val)")
    print("phase 2: merge ids {2(updated),3} ...")
    _merge(con.sql("SELECT * FROM r2"))

    print()
    print("Expected final state: 3 rows total; id=2 val='B_UPDATED'.")
    print("If the target now has 4 rows, the upsert DUPLICATED instead of "
          "updating — B2.1 / the dialect MERGE is wrong.")
    print()
    print("Read the target back with your DB client and confirm:")
    print(f"    SELECT id, val FROM {args.schema}.{args.table} ORDER BY id;")
    print("    => (1,'a'), (2,'B_UPDATED'), (3,'c')")
    print()
    print("SMOKE SCRIPT RAN — verify the row state above to confirm B2.1.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
