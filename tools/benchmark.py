#!/usr/bin/env python3
"""F-Pulse benchmark harness — measured DuckDB-native throughput.

Closes the "architected, not proven — no bench" gap and gives a MEASURED answer
to competitors' "native DuckDB speed" marketing (e.g. Duckle). Every scenario
runs against the REAL engine (DuckDB) and the REAL `DbSourceNode` read path —
not a mock — and reports rows/sec + wall time.

Usage
-----
    python tools/benchmark.py                          # default rows, all scenarios
    python tools/benchmark.py --rows 1000000
    python tools/benchmark.py --scenario duckdb_aggregate --json

Scenarios
---------
  duckdb_aggregate       scan + filter + GROUP BY over N rows (engine throughput)
  duckdb_join            N-row fact joined to a 1k dim (join throughput)
  sqlite_read_via_node   N rows read through the production DbSourceNode path
                         (proves the real connector read, not just raw DuckDB)

Numbers are machine-relative — record the host + commit alongside them. This is
a repeatable harness, not a published claim; a published claim needs a fixed
reference machine and a recorded baseline (see execution-plan T3).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

import duckdb  # noqa: E402

DEFAULT_ROWS = 200_000


def _timed(fn: Callable[[], Any]) -> tuple[Any, float]:
    start = time.perf_counter()
    result = fn()
    return result, time.perf_counter() - start


def _rate(rows: int, secs: float) -> int:
    return int(rows / secs) if secs > 0 else 0


def bench_duckdb_aggregate(rows: int) -> dict:
    con = duckdb.connect()
    try:
        con.execute(
            f"CREATE TABLE t AS "
            f"SELECT i AS id, i % 1000 AS grp, (i * 7) % 100 AS val FROM range({rows}) AS r(i)"
        )
        out, secs = _timed(lambda: con.execute(
            "SELECT grp, count(*) c, sum(val) s FROM t WHERE val > 10 GROUP BY grp"
        ).fetchall())
        return {"scenario": "duckdb_aggregate", "rows": rows, "groups": len(out),
                "wall_ms": round(secs * 1000, 1), "rows_per_sec": _rate(rows, secs)}
    finally:
        con.close()


def bench_duckdb_join(rows: int) -> dict:
    con = duckdb.connect()
    try:
        con.execute(f"CREATE TABLE a AS SELECT i AS id, i % 1000 AS k FROM range({rows}) AS r(i)")
        con.execute("CREATE TABLE b AS SELECT i AS k, ('dim' || i) AS label FROM range(1000) AS r(i)")
        out, secs = _timed(lambda: con.execute(
            "SELECT count(*) FROM a JOIN b USING (k)"
        ).fetchone())
        return {"scenario": "duckdb_join", "rows": rows, "matched": int(out[0]) if out else 0,
                "wall_ms": round(secs * 1000, 1), "rows_per_sec": _rate(rows, secs)}
    finally:
        con.close()


def bench_sqlite_read_via_node(rows: int) -> dict:
    from fpulse.nodes.db_source import DbSourceNode
    with tempfile.TemporaryDirectory(prefix="fpulse-bench-") as td:
        db_path = Path(td) / "bench.sqlite"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT, val INTEGER)")
            conn.executemany(
                "INSERT INTO items (id, name, val) VALUES (?, ?, ?)",
                ((i, f"n{i}", i % 100) for i in range(rows)),
            )
            conn.commit()
        finally:
            conn.close()

        node = DbSourceNode({"source_mode": "query", "query": "SELECT id, name, val FROM items"})
        query = "SELECT id, name, val FROM items"
        (rows_out, cols), secs = _timed(
            lambda: node._execute_real("sqlite", {"database": str(db_path)}, query,
                                       ctx=None, connection_id=None)
        )
        return {"scenario": "sqlite_read_via_node", "rows": len(rows_out), "columns": len(cols),
                "wall_ms": round(secs * 1000, 1), "rows_per_sec": _rate(len(rows_out), secs)}


SCENARIOS: dict[str, Callable[[int], dict]] = {
    "duckdb_aggregate": bench_duckdb_aggregate,
    "duckdb_join": bench_duckdb_join,
    "sqlite_read_via_node": bench_sqlite_read_via_node,
}


def run_benchmarks(rows: int, scenario: str | None = None) -> list[dict]:
    names = [scenario] if scenario else list(SCENARIOS)
    return [SCENARIOS[name](rows) for name in names]


def main() -> int:
    ap = argparse.ArgumentParser(prog="benchmark",
                                 description="Measured DuckDB-native throughput for F-Pulse.")
    ap.add_argument("--rows", type=int, default=DEFAULT_ROWS, help=f"Rows per scenario (default {DEFAULT_ROWS}).")
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), help="Run one scenario (default: all).")
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    args = ap.parse_args()

    results = run_benchmarks(args.rows, args.scenario)

    if args.json:
        print(json.dumps({"rows": args.rows, "duckdb": duckdb.__version__, "results": results}, indent=2))
        return 0

    print(f"F-Pulse benchmark  (rows={args.rows:,}  duckdb={duckdb.__version__})")
    print("=" * 66)
    print(f"{'scenario':24s} {'wall_ms':>10s} {'rows/sec':>14s}")
    print("-" * 66)
    for r in results:
        print(f"{r['scenario']:24s} {r['wall_ms']:>10.1f} {r['rows_per_sec']:>14,d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
