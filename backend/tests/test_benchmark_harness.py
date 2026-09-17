"""Smoke tests for the DuckDB benchmark harness (tools/benchmark.py).

Runs each scenario at a tiny row count so CI stays fast, and asserts the real
engine / read path actually executed and reported positive throughput.
"""

from __future__ import annotations

import sys
from pathlib import Path

_TOOLS = Path(__file__).resolve().parents[2] / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import benchmark as bm  # noqa: E402


def test_all_scenarios_run_and_report_throughput():
    results = bm.run_benchmarks(1000)
    scenarios = {r["scenario"] for r in results}
    assert scenarios == set(bm.SCENARIOS)
    for r in results:
        assert r["rows_per_sec"] > 0, r
        assert r["wall_ms"] >= 0, r


def test_sqlite_read_goes_through_the_real_node_and_returns_all_rows():
    r = bm.bench_sqlite_read_via_node(500)
    assert r["rows"] == 500          # the production DbSourceNode read every seeded row
    assert r["columns"] == 3
    assert r["rows_per_sec"] > 0


def test_single_scenario_selection():
    results = bm.run_benchmarks(1000, scenario="duckdb_join")
    assert len(results) == 1
    assert results[0]["scenario"] == "duckdb_join"
    assert results[0]["matched"] == 1000  # every fact row matched a dim key
