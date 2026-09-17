#!/usr/bin/env python3
"""Compile an F-Pulse pipeline JSON to a readable-SQL preview.

    python tools/compile_sql.py path/to/pipeline.json
    python tools/compile_sql.py pipeline.json --out pipeline.sql

The compiled CTEs mirror what the engine runs; steps that aren't SQL-expressible
(API/AI sources, flow-control, complex transforms) are emitted as explicit
`-- [boundary]` comments. Exit is always 0 — a pipeline with boundaries is a
valid, honest partial preview, not an error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from fpulse.compile.sql_preview import compile_workflow_to_sql  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(prog="compile_sql",
                                 description="Compile a pipeline JSON to a readable-SQL preview.")
    ap.add_argument("workflow", help="Path to a workflow JSON (fpulse.ir.schema.Workflow shape).")
    ap.add_argument("--out", help="Write the SQL here instead of stdout.")
    args = ap.parse_args()

    workflow = json.loads(Path(args.workflow).read_text(encoding="utf-8"))
    result = compile_workflow_to_sql(workflow)

    if args.out:
        Path(args.out).write_text(result.sql, encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(result.sql)

    print(f"-- compiled {len(result.compiled_steps)} step(s), "
          f"{len(result.boundary_steps)} boundary; fully_compiled={result.fully_compiled}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
