"""End-to-end verification for the Signed Artifacts feature (v15+).

Run this from the backend directory:

    cd D:\\Siva\\hybridyn-f-pulse\\backend
    python verify_signed_artifacts.py

Prints PASS / FAIL for each check. Read-only — never mutates the DB.

Memory: a few KB peak. One row read at a time, never loads the whole
workflow_versions table.

Checks:
  1. Schema is at version 15
  2. workflow_versions.content_hash column exists
  3. New rows (post-migration) carry a populated hash
  4. compute_workflow_hash() is deterministic on the same input
  5. verify_version_hash() agrees with stored hash for at least one row
  6. Hash changes when the workflow body changes (sanity: not a constant)
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path


# ── Locate the live SQLite DB ────────────────────────────────────────────
def find_db() -> Path:
    """The launcher (start.ps1) sets FPULSE_DATA_DIR=$ROOT\\data\\samples.
    Fall back to other plausible spots if that env var isn't set."""
    candidates = [
        os.environ.get("FPULSE_DATA_DIR"),
        Path(__file__).parent.parent / "data" / "samples",
        Path(__file__).parent / "data",
        Path.cwd() / "data" / "samples",
    ]
    for c in candidates:
        if not c:
            continue
        p = Path(c) / "fpulse.db"
        if p.is_file():
            return p
    raise SystemExit(
        "ERROR: could not locate fpulse.db. Set FPULSE_DATA_DIR or "
        "run from the backend dir."
    )


def main() -> int:
    db_path = find_db()
    print(f"DB: {db_path}\n")

    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = ""):
        marker = "PASS" if ok else "FAIL"
        print(f"  [{marker}] {label}{(' — ' + detail) if detail else ''}")
        if not ok:
            failures.append(label)

    # Open read-only via URI to be safe with WAL on a live process.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    try:
        # ── 1. Schema version ──
        row = conn.execute(
            "SELECT value FROM _meta WHERE key='schema_version'"
        ).fetchone()
        sv = int(row["value"]) if row else 0
        check("Schema is at v15+", sv >= 15, f"current={sv}")

        # ── 2. content_hash column exists ──
        cols = conn.execute("PRAGMA table_info(workflow_versions)").fetchall()
        col_names = {c["name"] for c in cols}
        check(
            "workflow_versions.content_hash column exists",
            "content_hash" in col_names,
            f"columns={sorted(col_names)}",
        )

        # ── 3. Hash population on new rows ──
        # We can't tell pre/post-migration apart per row, but we can show
        # how many of the most recent rows carry a populated hash.
        rows = conn.execute(
            "SELECT workflow_id, version, content_hash FROM workflow_versions "
            "ORDER BY rowid DESC LIMIT 20"
        ).fetchall()
        if not rows:
            check("Recent rows have hashes", True, "no rows yet — save a workflow to populate")
        else:
            hashed = sum(1 for r in rows if (r["content_hash"] or "").strip())
            print(
                f"     last {len(rows)} rows: {hashed} hashed, "
                f"{len(rows) - hashed} legacy (empty)"
            )
            check(
                "At least one recent row has a hash (or no rows exist)",
                hashed > 0 or len(rows) == 0,
                "save any workflow after migration to populate" if hashed == 0 else f"e.g. {rows[0]['workflow_id']} v{rows[0]['version']}: {(rows[0]['content_hash'] or '')[:16]}…",
            )

        # ── 4. compute_workflow_hash() determinism ──
        # Add backend dir to sys.path so we can import the helper.
        sys.path.insert(0, str(Path(__file__).parent))
        try:
            from fpulse.ir.versioning import compute_workflow_hash
            from fpulse.ir.schema import Workflow
        except Exception as e:  # noqa: BLE001
            check("Import compute_workflow_hash", False, str(e))
            return 1 if failures else 0
        check("Import compute_workflow_hash", True)

        # Build a minimal in-memory workflow for the deterministic test.
        wf_dict = {
            "id": "verify-test-id",
            "name": "verify-test",
            "steps": [],
            "connections": [],
            "workspace_id": "default",
        }
        wf_a = Workflow(**wf_dict)
        h1 = compute_workflow_hash(wf_a)
        h2 = compute_workflow_hash(wf_a)
        check("Hash is deterministic (h(wf) == h(wf))", h1 == h2, h1[:16] + "…")

        # ── 5. verify_version_hash matches a real row ──
        # Pick the most recent hashed row and re-verify.
        sample = conn.execute(
            "SELECT workflow_id, version, content_hash, data "
            "FROM workflow_versions WHERE content_hash != '' "
            "ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        if sample:
            try:
                from fpulse.ir.schema import WorkflowVersion
                wv = WorkflowVersion(**json.loads(sample["data"]))
                recomputed = compute_workflow_hash(wv.workflow)
                ok = recomputed == sample["content_hash"]
                check(
                    "Re-hash of latest hashed row matches stored value",
                    ok,
                    f"workflow={sample['workflow_id']} v{sample['version']} "
                    f"stored={sample['content_hash'][:12]}… recomputed={recomputed[:12]}…",
                )
            except Exception as e:  # noqa: BLE001
                check("Re-hash of latest hashed row", False, str(e))
        else:
            print("     [SKIP] no hashed rows to re-verify yet")

        # ── 6. Different content → different hash ──
        wf_b_dict = dict(wf_dict)
        wf_b_dict["name"] = "verify-test-CHANGED"
        wf_b = Workflow(**wf_b_dict)
        h_b = compute_workflow_hash(wf_b)
        check(
            "Different workflow body → different hash",
            h_b != h1,
            f"a={h1[:12]}… b={h_b[:12]}…",
        )

        # ── 7. Lifecycle-only fields don't affect hash ──
        # status / updated_at / deployed_* must be excluded — otherwise
        # rollback verification would false-positive on every published
        # workflow.
        wf_c_dict = dict(wf_dict)
        wf_c = Workflow(**wf_c_dict)
        # Mutate a lifecycle field manually
        wf_c.status = wf_a.status  # same value but assigned through a different code path
        try:
            wf_c.deployed_version = 99
        except Exception:
            pass
        h_c = compute_workflow_hash(wf_c)
        check(
            "Lifecycle field mutation does NOT change hash",
            h_c == h1,
            f"a={h1[:12]}… c={h_c[:12]}…",
        )

    finally:
        conn.close()

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s) — {', '.join(failures)}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
