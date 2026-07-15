"""
Data Lineage — column-level lineage tracking across pipeline executions.

Tracks which columns flow from source → transform → sink, building a
directed acyclic graph (DAG) of column-level dependencies. The lineage
graph is persisted in SQLite and exposed via API for React Flow visualization.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)


class LineageStore:
    """SQLite-backed column-level lineage storage."""

    def __init__(self, db):
        self._db = db
        self._ensure_tables()

    def _ensure_tables(self):
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS lineage_nodes (
                id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                step_id TEXT NOT NULL,
                step_label TEXT DEFAULT '',
                step_type TEXT DEFAULT '',
                columns TEXT DEFAULT '[]',
                created_at REAL NOT NULL
            )
        """)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS lineage_edges (
                id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                source_node_id TEXT NOT NULL,
                target_node_id TEXT NOT NULL,
                source_column TEXT DEFAULT '',
                target_column TEXT DEFAULT '',
                transform_type TEXT DEFAULT 'passthrough',
                expression TEXT DEFAULT '',
                created_at REAL NOT NULL
            )
        """)
        self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_lineage_nodes_wf
            ON lineage_nodes(workflow_id)
        """)
        self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_lineage_edges_wf
            ON lineage_edges(workflow_id)
        """)
        # L1 (2026-06-08, docs/design/lineage-1.2.md) - runtime
        # lineage events. Distinct from the design-time graph
        # (lineage_nodes / lineage_edges, which represents what the
        # workflow IR says will happen) - this table records what
        # ACTUALLY ran, keyed by run_id. Lets the UI render a "this
        # specific execution" view in addition to the "intent" view.
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS lineage_step_runs (
                id            TEXT PRIMARY KEY,
                workflow_id   TEXT NOT NULL,
                run_id        TEXT NOT NULL,
                step_id       TEXT NOT NULL,
                step_label    TEXT DEFAULT '',
                step_type     TEXT DEFAULT '',
                columns_in    TEXT DEFAULT '[]',
                columns_out   TEXT DEFAULT '[]',
                rows_in       INTEGER DEFAULT 0,
                rows_out      INTEGER DEFAULT 0,
                started_at    REAL NOT NULL,
                completed_at  REAL DEFAULT 0,
                error         TEXT DEFAULT ''
            )
        """)
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_lineage_runs_run ON lineage_step_runs(run_id)")
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_lineage_runs_wf ON lineage_step_runs(workflow_id)")

    # ── Write ──────────────────────────────────────────────────────────

    def record_step(
        self,
        workflow_id: str,
        step_id: str,
        step_label: str,
        step_type: str,
        columns: list[str],
    ) -> str:
        """Record a lineage node (one per pipeline step)."""
        node_id = f"ln_{uuid.uuid4().hex[:8]}"
        self._db.execute(
            "INSERT OR REPLACE INTO lineage_nodes (id, workflow_id, step_id, step_label, step_type, columns, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (node_id, workflow_id, step_id, step_label, step_type, json.dumps(columns), time.time()),
        )
        return node_id

    def record_edge(
        self,
        workflow_id: str,
        source_step_id: str,
        target_step_id: str,
        source_column: str = "",
        target_column: str = "",
        transform_type: str = "passthrough",
        expression: str = "",
    ) -> str:
        """Record a column-level lineage edge between two steps."""
        # Resolve lineage node IDs from step IDs
        src = self._db.fetchone(
            "SELECT id FROM lineage_nodes WHERE workflow_id=? AND step_id=? ORDER BY created_at DESC LIMIT 1",
            (workflow_id, source_step_id),
        )
        tgt = self._db.fetchone(
            "SELECT id FROM lineage_nodes WHERE workflow_id=? AND step_id=? ORDER BY created_at DESC LIMIT 1",
            (workflow_id, target_step_id),
        )
        if not src or not tgt:
            return ""

        edge_id = f"le_{uuid.uuid4().hex[:8]}"
        self._db.execute(
            "INSERT INTO lineage_edges (id, workflow_id, source_node_id, target_node_id, source_column, target_column, transform_type, expression, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (edge_id, workflow_id, src["id"], tgt["id"], source_column, target_column, transform_type, expression, time.time()),
        )
        return edge_id

    def build_from_workflow(self, workflow) -> dict:
        """Auto-build lineage graph from a workflow IR.

        Walks steps + connections to infer column flow. For transforms/filters
        we mark columns as derived; for sources/sinks as origin/terminal.
        """
        wf_id = workflow.id

        # Clear existing lineage for this workflow
        self._db.execute("DELETE FROM lineage_edges WHERE workflow_id=?", (wf_id,))
        self._db.execute("DELETE FROM lineage_nodes WHERE workflow_id=?", (wf_id,))

        step_map: dict[str, Any] = {}
        for step in workflow.steps:
            cols = self._infer_columns(step)
            self.record_step(wf_id, step.id, step.label, step.type.value, cols)
            step_map[step.id] = step

        # Record edges from connections
        for conn in workflow.connections:
            src_step = step_map.get(conn.from_step)
            tgt_step = step_map.get(conn.to_step)
            if not src_step or not tgt_step:
                continue

            src_cols = self._infer_columns(src_step)
            tgt_type = tgt_step.type.value

            # Determine transform type based on target node
            xform = "passthrough"
            if tgt_type in ("filter", "validate", "sample", "deduplicate"):
                xform = "filter"
            elif tgt_type in ("transform", "derived_column", "typecast", "rename"):
                xform = "transform"
            elif tgt_type in ("aggregate", "window", "pivot", "unpivot"):
                xform = "aggregate"
            elif tgt_type in ("join", "union", "lookup"):
                xform = "join"

            # Record column-level edges
            for col in src_cols:
                tgt_col = self._map_column(col, tgt_step)
                self.record_edge(wf_id, conn.from_step, conn.to_step, col, tgt_col, xform)

            # If no columns known, record a step-level edge
            if not src_cols:
                self.record_edge(wf_id, conn.from_step, conn.to_step, "", "", xform)

        return self.get_graph(wf_id)

    def _infer_columns(self, step) -> list[str]:
        """Infer column list from step params."""
        params = step.params or {}

        # Explicit columns in params
        if "columns" in params and isinstance(params["columns"], list):
            return [c if isinstance(c, str) else c.get("name", "") for c in params["columns"]]

        # Aggregate functions
        if "functions" in params:
            cols = []
            group_by = params.get("group_by", [])
            if isinstance(group_by, list):
                cols.extend(group_by)
            for fn in params["functions"]:
                if isinstance(fn, dict):
                    alias = fn.get("alias") or fn.get("column", "")
                    if alias:
                        cols.append(alias)
            return cols

        # Select columns
        if "select_columns" in params:
            return params["select_columns"]

        # Rename map
        if "renames" in params:
            return list(params["renames"].values())

        return []

    def _map_column(self, col: str, target_step) -> str:
        """Map a source column to its target column name after a transform."""
        params = target_step.params or {}

        # Rename: check if this column is being renamed
        renames = params.get("renames", {})
        if col in renames:
            return renames[col]

        # Derived column: check expression references
        if target_step.type.value == "derived_column":
            return params.get("new_column", col)

        return col

    # ── Read ───────────────────────────────────────────────────────────

    def get_graph(self, workflow_id: str) -> dict:
        """Get the full lineage graph for a workflow (React Flow format)."""
        nodes = self._db.fetchall(
            "SELECT * FROM lineage_nodes WHERE workflow_id=? ORDER BY created_at",
            (workflow_id,),
        )
        edges = self._db.fetchall(
            "SELECT * FROM lineage_edges WHERE workflow_id=? ORDER BY created_at",
            (workflow_id,),
        )

        rf_nodes = []
        for n in nodes:
            cols = json.loads(n.get("columns", "[]")) if isinstance(n.get("columns"), str) else n.get("columns", [])
            rf_nodes.append({
                "id": n["id"],
                "type": "lineageNode",
                "data": {
                    "step_id": n["step_id"],
                    "label": n["step_label"],
                    "step_type": n["step_type"],
                    "columns": cols,
                },
                "position": {"x": 0, "y": 0},  # Client does layout
            })

        rf_edges = []
        for e in edges:
            label = ""
            if e.get("source_column") and e.get("target_column"):
                if e["source_column"] == e["target_column"]:
                    label = e["source_column"]
                else:
                    label = f"{e['source_column']} → {e['target_column']}"
            elif e.get("source_column"):
                label = e["source_column"]

            rf_edges.append({
                "id": e["id"],
                "source": e["source_node_id"],
                "target": e["target_node_id"],
                "label": label,
                "data": {
                    "source_column": e.get("source_column", ""),
                    "target_column": e.get("target_column", ""),
                    "transform_type": e.get("transform_type", "passthrough"),
                    "expression": e.get("expression", ""),
                },
            })

        return {"nodes": rf_nodes, "edges": rf_edges, "workflow_id": workflow_id}

    def get_column_lineage(self, workflow_id: str, column_name: str) -> dict:
        """Trace a single column through the entire pipeline — upstream and downstream."""
        edges = self._db.fetchall(
            "SELECT * FROM lineage_edges WHERE workflow_id=? AND (source_column=? OR target_column=?)",
            (workflow_id, column_name, column_name),
        )
        node_ids = set()
        for e in edges:
            node_ids.add(e["source_node_id"])
            node_ids.add(e["target_node_id"])

        nodes = []
        for nid in node_ids:
            n = self._db.fetchone("SELECT * FROM lineage_nodes WHERE id=?", (nid,))
            if n:
                nodes.append(n)

        return {
            "column": column_name,
            "workflow_id": workflow_id,
            "nodes": [{"id": n["id"], "step_id": n["step_id"], "label": n["step_label"], "type": n["step_type"]} for n in nodes],
            "edges": [{"id": e["id"], "from": e["source_node_id"], "to": e["target_node_id"], "source_col": e.get("source_column"), "target_col": e.get("target_column"), "transform": e.get("transform_type")} for e in edges],
        }

    # ── Runtime lineage events (L1, 2026-06-08) ───────────────────────

    def record_step_run(
        self,
        *,
        workflow_id: str,
        run_id: str,
        step_id: str,
        step_label: str = "",
        step_type: str = "",
        columns_in: list[str] | None = None,
        columns_out: list[str] | None = None,
        rows_in: int = 0,
        rows_out: int = 0,
        started_at: float | None = None,
        completed_at: float | None = None,
        error: str = "",
    ) -> str:
        """Record one step's runtime lineage fact. Called by node
        implementations from inside execute(). Returns the row id.

        Distinct from `record_step()` which records design-time intent;
        this records what actually ran on a specific run_id.
        """
        rid = f"lsr_{uuid.uuid4().hex[:8]}"
        self._db.execute(
            "INSERT INTO lineage_step_runs (id, workflow_id, run_id, step_id, step_label, step_type, "
            "columns_in, columns_out, rows_in, rows_out, started_at, completed_at, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rid, workflow_id, run_id, step_id, step_label, step_type,
                json.dumps(columns_in or []), json.dumps(columns_out or []),
                int(rows_in or 0), int(rows_out or 0),
                started_at if started_at is not None else time.time(),
                completed_at if completed_at is not None else 0.0,
                error or "",
            ),
        )
        return rid

    def get_runtime_lineage(self, run_id: str) -> dict:
        """All step-runs for a specific execution. Used by the UI's
        per-run lineage panel + by OpenLineage exporters."""
        rows = self._db.fetchall(
            "SELECT * FROM lineage_step_runs WHERE run_id=? ORDER BY started_at",
            (run_id,),
        )
        out = []
        for r in rows:
            out.append({
                "id":           r["id"],
                "workflow_id":  r["workflow_id"],
                # 2026-06-08 (L2 fix) - run_id included so per-row
                # dict is self-describing; downstream consumers
                # (OpenLineage exporter) don't need to thread the
                # outer run_id through every call.
                "run_id":       r["run_id"],
                "step_id":      r["step_id"],
                "step_label":   r.get("step_label", ""),
                "step_type":    r.get("step_type", ""),
                "columns_in":   json.loads(r.get("columns_in") or "[]"),
                "columns_out":  json.loads(r.get("columns_out") or "[]"),
                "rows_in":      r.get("rows_in", 0),
                "rows_out":     r.get("rows_out", 0),
                "started_at":   r["started_at"],
                "completed_at": r.get("completed_at", 0.0),
                "error":        r.get("error", ""),
            })
        return {"run_id": run_id, "step_runs": out}

    def get_runs_for_workflow(self, workflow_id: str, *, limit: int = 50) -> list[str]:
        """List the distinct run_ids that have lineage recorded for a
        workflow, most recent first. Powers a "pick a run to inspect"
        dropdown."""
        rows = self._db.fetchall(
            "SELECT run_id, MAX(started_at) AS latest "
            "FROM lineage_step_runs WHERE workflow_id=? "
            "GROUP BY run_id ORDER BY latest DESC LIMIT ?",
            (workflow_id, int(limit)),
        )
        return [r["run_id"] for r in rows]

    def delete_workflow_lineage(self, workflow_id: str):
        """Remove all lineage data for a workflow (design-time AND runtime)."""
        self._db.execute("DELETE FROM lineage_edges WHERE workflow_id=?", (workflow_id,))
        self._db.execute("DELETE FROM lineage_nodes WHERE workflow_id=?", (workflow_id,))
        self._db.execute("DELETE FROM lineage_step_runs WHERE workflow_id=?", (workflow_id,))

    # ── Consumer self-attestation (L3, 2026-06-08) ────────────────────
    #
    # F-Pulse outputs are read by downstream consumers we don't own:
    # other F-Pulse pipelines, Snowflake VIEWs, Tableau dashboards,
    # Python notebooks. They post themselves here so F-Pulse can answer
    # "if we change this output's schema, what downstream breaks?"
    #
    # The protocol is honest: consumers WHO ARE POLITE register; we
    # don't auto-discover. Plus tier (per docs/design/lineage-1.2.md
    # L4) will add the Snowflake QUERY_HISTORY scraper for real
    # auto-discovery.

    def _ensure_consumer_table(self):
        # Lazy migration - older deployments won't have this table
        # at startup. Called once per record_consumer / list_consumers
        # call; idempotent (CREATE IF NOT EXISTS).
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS lineage_consumers (
                id              TEXT PRIMARY KEY,
                output_id       TEXT NOT NULL,
                consumer_id     TEXT NOT NULL,
                consumer_type   TEXT NOT NULL,
                last_read_at    REAL,
                attested_at     REAL NOT NULL,
                attested_by     TEXT DEFAULT '',
                notes           TEXT DEFAULT '',
                UNIQUE(output_id, consumer_id, consumer_type)
            )
        """)
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_consumers_output "
            "ON lineage_consumers(output_id)"
        )

    def record_consumer(
        self,
        *,
        output_id: str,
        consumer_id: str,
        consumer_type: str,
        last_read_at: float | None = None,
        attested_by: str = "",
        notes: str = "",
    ) -> str:
        """Register (or update) one downstream consumer of a F-Pulse
        output. Idempotent on (output_id, consumer_id, consumer_type):
        re-attestation updates last_read_at + attested_at without
        creating a duplicate row.

        Returns the row id (stable across re-attestations).
        """
        self._ensure_consumer_table()
        # Stable id derived from the natural key so re-attestation
        # updates the same row.
        natural = f"{output_id}|{consumer_id}|{consumer_type}"
        rid = "cn_" + (uuid.uuid5(uuid.NAMESPACE_URL, natural).hex[:10])
        now = time.time()
        # UPSERT pattern via INSERT OR REPLACE - SQLite's UPSERT
        # (ON CONFLICT) is supported but using REPLACE keeps the
        # query portable to other backends.
        self._db.execute(
            "INSERT OR REPLACE INTO lineage_consumers "
            "(id, output_id, consumer_id, consumer_type, "
            " last_read_at, attested_at, attested_by, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rid, output_id, consumer_id, consumer_type,
                last_read_at if last_read_at is not None else now,
                now, attested_by, notes,
            ),
        )
        return rid

    def list_consumers(self, output_id: str) -> list[dict]:
        """Return every registered consumer of the given output_id,
        most-recently-attested first."""
        self._ensure_consumer_table()
        rows = self._db.fetchall(
            "SELECT * FROM lineage_consumers WHERE output_id=? "
            "ORDER BY attested_at DESC",
            (output_id,),
        )
        return [
            {
                "id":             r["id"],
                "output_id":      r["output_id"],
                "consumer_id":    r["consumer_id"],
                "consumer_type":  r["consumer_type"],
                "last_read_at":   r.get("last_read_at"),
                "attested_at":    r["attested_at"],
                "attested_by":    r.get("attested_by", ""),
                "notes":          r.get("notes", ""),
            }
            for r in rows
        ]

    def list_all_outputs_with_consumers(self) -> list[dict]:
        """One row per output that has at least one registered
        consumer, with the consumer count. Powers a "show me everything
        downstream knows about" overview."""
        self._ensure_consumer_table()
        rows = self._db.fetchall(
            "SELECT output_id, COUNT(*) AS consumer_count, "
            "       MAX(attested_at) AS last_attested_at "
            "FROM lineage_consumers GROUP BY output_id "
            "ORDER BY last_attested_at DESC"
        )
        return [dict(r) for r in rows]

    def delete_consumer(self, output_id: str, consumer_id: str,
                          consumer_type: str) -> bool:
        """Remove one consumer registration. Returns True if a row
        was removed."""
        self._ensure_consumer_table()
        cur = self._db.execute(
            "DELETE FROM lineage_consumers WHERE output_id=? "
            "AND consumer_id=? AND consumer_type=?",
            (output_id, consumer_id, consumer_type),
        )
        try:
            return cur.rowcount > 0
        except Exception:
            return False
