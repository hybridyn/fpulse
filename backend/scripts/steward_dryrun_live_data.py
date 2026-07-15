"""Run the Steward Archeologist against the live workflow store and
print every finding it would emit.

This is a dry-run — read-only, doesn't touch settings.json, doesn't
write to memory.jsonl. It exists for one purpose: give you VISIBLE
proof of what the Steward sees in your actual workspace, without
requiring the API server to be running.

Usage:
    .venv\\Scripts\\python.exe backend\\scripts\\steward_dryrun_live_data.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Make `fpulse` importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fpulse.steward.archeologist import detect_duplicate_sources


def main() -> int:
    # Find the workflow DB. Same env-var the app respects.
    data_dir = Path(
        os.environ.get("FPULSE_DATA_DIR")
        or Path.home() / ".fpulse"
    )
    db_path = data_dir / "fpulse.db"
    if not db_path.is_file():
        # Try the alternate common location
        alt = Path.cwd() / "backend" / ".fpulse-data" / "fpulse.db"
        if alt.is_file():
            db_path = alt

    if not db_path.is_file():
        print("=" * 70)
        print("Could not find fpulse.db. Checked:")
        print(f"  {data_dir / 'fpulse.db'}")
        print(f"  {Path.cwd() / 'backend' / '.fpulse-data' / 'fpulse.db'}")
        print()
        print("Set FPULSE_DATA_DIR to your data directory and re-run.")
        return 1

    print("=" * 70)
    print("F-Pulse Steward — DRY RUN against live workflow data")
    print("=" * 70)
    print(f"Data dir: {data_dir}")
    print(f"DB:       {db_path}")
    print()

    # Read workflows directly from SQLite. F-Pulse stores every
    # workflow save as a row in `workflow_versions`; the latest row
    # per workflow_id is the current state. Mirrors what the API does
    # internally — see backend/fpulse/state/workflow_store.py.
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT workflow_id, version, data, workspace_id "
        "FROM workflow_versions "
        "ORDER BY workflow_id, version DESC"
    )
    all_rows = cur.fetchall()
    conn.close()

    # Take only the latest version per workflow_id
    raw_rows: list[sqlite3.Row] = []
    seen: set[str] = set()
    for row in all_rows:
        if row["workflow_id"] in seen:
            continue
        seen.add(row["workflow_id"])
        raw_rows.append(row)

    print(f"Workflows in store: {len(raw_rows)} (from {len(all_rows)} total versions)")
    print()

    # Normalise to the dict shape the Archeologist expects.
    #
    # The `workflow_versions.data` JSON has shape:
    #   {version, workflow: {id, name, steps, ...},
    #    created_by, created_at, change_summary}
    #
    # i.e. the real workflow lives under a `workflow` envelope. The
    # API path (WorkflowStore.list_all) unwraps it implicitly by
    # returning Workflow model objects, so this normalisation only
    # matters for raw-SQLite consumers like this dry-run script.
    workflows: list[dict] = []
    for row in raw_rows:
        try:
            envelope = json.loads(row["data"])
        except (json.JSONDecodeError, TypeError):
            continue
        wf = envelope.get("workflow") or envelope
        nodes = wf.get("nodes") or wf.get("steps") or []
        workflows.append({
            "id": row["workflow_id"],
            "name": wf.get("name") or row["workflow_id"],
            "nodes": nodes,
        })

    # Print a short summary of each workflow's sources/sinks for context
    print("Per-workflow source/sink inventory:")
    print("-" * 70)
    from fpulse.steward.archeologist import _step_type_and_params
    for wf in workflows:
        sources, sinks = [], []
        for node in wf["nodes"]:
            if not isinstance(node, dict):
                continue
            t, params, _label = _step_type_and_params(node)
            ident = (
                params.get("table")
                or params.get("file_path")
                or params.get("query", "")[:30]
                or params.get("url", "")[:30]
                or "?"
            )
            if t == "source" or t.endswith("_source"):
                sources.append(f"{t}({params.get('connection_id','?')}.{ident})")
            elif t in ("output", "destination", "db_sink", "sink") or t.endswith("_sink"):
                sinks.append(f"{t}({params.get('connection_id','?')}.{ident})")
        print(f"  {wf['name'][:40]:40s} src={sources}  sink={sinks}")
    print()

    # Run the actual detector
    findings = detect_duplicate_sources(workflows, workspace_id="default")

    print(f"Steward findings: {len(findings)}")
    print("=" * 70)
    if not findings:
        print()
        print("No duplicate-source or duplicate-pipeline findings in this workspace.")
        print("That can mean:")
        print("  - Your pipelines all read distinct sources (healthy!)")
        print("  - All sources lack `connection_id` / `table` / `file_path` params")
        print("    (manifest-driven sources might use different field names)")
        return 0

    for i, f in enumerate(findings, 1):
        print()
        print(f"[{i}] {f.severity.value.upper()}  {f.kind.value}  {f.title}")
        print(f"    id: {f.id}")
        print(f"    level: {f.level.value}")
        print(f"    confidence: {f.confidence} (score={f.confidence_score})")
        print(f"    evidence_count: {f.evidence_count}")
        wfs = f.evidence.get("workflows") or []
        print(f"    affected workflows ({len(wfs)}):")
        for w in wfs:
            print(f"      - {w.get('name', '?')}  (id={w.get('id', '?')[:12]})")
        print(f"    proposed actions:")
        for a in f.proposed_actions:
            print(f"      - {a.get('label')}")

    print()
    print("=" * 70)
    print("Dry run complete. Re-run after creating/editing pipelines to see")
    print("how the findings change.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
