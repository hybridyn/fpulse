"""SQLite-backed workflow store with versioning.

Version retention: after every save, old versions beyond the configured
``VERSION_RETENTION_COUNT`` are pruned UNLESS the version is the current
``deployed_version`` — that one is always kept so PROD rollback is safe.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from copy import deepcopy

from fpulse import runtime_config
from .schema import Workflow, WorkflowVersion, PipelineStatus

logger = logging.getLogger(__name__)


def compute_workflow_hash(workflow: Workflow) -> str:
    """SHA-256 hex digest of a workflow's structural content.

    Memory-cheap: dumps the model with ``sort_keys=True`` so the bytes
    fed into the hasher are deterministic regardless of dict order, then
    releases the intermediate string as soon as ``hashlib`` has consumed
    it. No long-lived buffers.

    Intentionally excludes *mutable* bookkeeping fields that change on
    every save even when the pipeline itself is unchanged:
      - ``updated_at`` — stamped on every write
      - ``deployed_at`` / ``deployed_by`` / ``deployed_version``
      - ``published_at`` / ``published_by``
      - ``rollback_from``
      - ``test_results``

    What remains is the *actual pipeline definition* — steps, connections,
    configs, schedule, connections, etc. Two saves that only flip the
    deployed_version pointer will produce the same hash, which is the
    correct semantics for "did the content change".
    """
    # model_dump gets us a plain dict without datetime-in-JSON pain.
    d = workflow.model_dump(mode="json")
    # Strip bookkeeping / lifecycle fields. None of these describe "what
    # the pipeline does" — they track *state* and change via update_status
    # without re-running save(). If we hashed them we'd get false-positive
    # mismatches on every rollback of a pipeline that's been published or
    # re-deployed since its last structural save.
    for k in (
        "status",
        "updated_at", "deployed_at", "deployed_by", "deployed_version",
        "published_at", "published_by", "rollback_from", "test_results",
    ):
        d.pop(k, None)
    # sort_keys=True makes the byte stream deterministic. default=str
    # catches any stray datetime the model_dump didn't serialize.
    payload = json.dumps(d, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class WorkflowStore:
    """Versioned workflow store backed by SQLite."""

    def __init__(self, db=None):
        self._db = db

    def set_db(self, db):
        self._db = db

    def save(self, workflow: Workflow, change_summary: str = "", created_by: str = "user") -> WorkflowVersion:
        """Save a new version of a workflow.

        On the **first** save of a workflow (version 1) — whether from
        the create endpoint, an import, a duplicate, an autosave on a
        brand-new draft, or any programmatic path — the name is
        auto-suffixed if another workflow in the same workspace
        already uses it. Subsequent saves (updates) don't rewrite the
        name; once a workflow has been saved, its name is stable
        unless the caller explicitly changes it.
        """
        # Get current max version
        row = self._db.fetchone(
            "SELECT MAX(version) as max_v FROM workflow_versions WHERE workflow_id = ?",
            (workflow.id,),
        )
        version_num = (row["max_v"] or 0) + 1 if row else 1

        # ── Store-layer name rules (May 18 2026, supersedes May 6) ─────
        # On the FIRST save of a new workflow only — the single chokepoint
        # every create path goes through (POST /workflows, POST
        # /templates/import, agent apply_pipeline_draft, autosave-on-new,
        # any direct programmatic save).
        #
        # Rule 1 (locked 2026-05-09): reject placeholder names so the
        # workflows list never silently collects anonymous rows. The
        # frontend's `requireNamedWorkflow` helper enforces this client-
        # side, but defense-in-depth here catches every non-UI path
        # (agent apply_pipeline_draft, CLI imports, future integrations).
        # Without this guard, the backend used to auto-suffix "Untitled
        # Pipeline" → "Untitled Pipeline (2)" etc. and produce the "five
        # Untitled rows" pattern the user keeps catching.
        #
        # Rule 2: dedupe real names by auto-suffixing — unchanged.
        if version_num == 1:
            raw_name = (getattr(workflow, "name", None) or "").strip()
            if not raw_name or raw_name.lower() == "untitled pipeline":
                raise ValueError(
                    "Pipeline name is required and cannot be the placeholder "
                    "'Untitled Pipeline'. Give it a descriptive name before saving."
                )
            try:
                from fpulse.common.unique_name import ensure_unique_name
                ws_id = workflow.workspace_id or "default"
                existing_names = {
                    w.get("name", "") for w in self.list_all(workspace_id=ws_id)
                }
                workflow.name = ensure_unique_name(raw_name, existing_names)
            except ValueError:
                raise
            except Exception:  # noqa: BLE001 — never fail a save on the dedupe helper
                pass

        workflow.updated_at = datetime.now(timezone.utc)
        version = WorkflowVersion(
            version=version_num,
            workflow=deepcopy(workflow),
            created_by=created_by,
            change_summary=change_summary,
        )

        data = version.model_dump(mode="json")
        # 2026-05-22: migrate legacy step types in the serialised blob
        # too. Belt-and-braces with the read-side migrate in get() —
        # this one ensures the on-disk row never grows new legacy
        # types from API callers that bypass the route handlers
        # (templates import, agent apply_pipeline_draft, programmatic
        # saves). Mutates in place; safe because ``data`` is a fresh
        # dict from model_dump().
        from fpulse.ir.migrations import migrate_legacy_node_types
        wf_blob = data.get("workflow")
        if isinstance(wf_blob, dict):
            migrate_legacy_node_types(wf_blob)
        # workspace_id denormalised into the row so list_all can filter
        # on an index. The Workflow model carries it through the JSON
        # blob too, so either source is a single point of truth.
        ws_id = workflow.workspace_id or "default"
        # Content hash (v15+) — SHA-256 over the structural fields only.
        # Cheap compute, stored so rollback can detect tamper / corruption
        # and admins can prove "this deployed version exactly matches the
        # one that was approved on date X."
        content_hash = compute_workflow_hash(workflow)
        self._db.execute(
            "INSERT OR REPLACE INTO workflow_versions (workflow_id, version, data, created_by, change_summary, created_at, workspace_id, content_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (workflow.id, version_num, json.dumps(data, default=str), created_by, change_summary, version.created_at.isoformat(), ws_id, content_hash),
        )
        self._db.commit()

        # Prune old versions beyond retention limit
        self._prune_versions(workflow.id)

        # 2026-05-23 (Y12 + Z25): invalidate the storage AND connection
        # lineage caches so both surfaces reflect the new pipeline
        # references immediately on the operator's next render.
        # Best-effort — never block a successful workflow save on cache
        # cleanup. Credential cache is keyed on the connection store
        # (not pipelines), so workflow saves don't need to invalidate it.
        try:
            from fpulse.datastore.usage import (
                invalidate as _invalidate_usage,
                invalidate_connection_usage as _invalidate_conn_usage,
            )
            _invalidate_usage(ws_id)
            _invalidate_conn_usage(ws_id)
        except Exception:  # noqa: BLE001
            pass

        return version

    def _prune_versions(self, workflow_id: str) -> int:
        """Delete versions older than the retention window.

        Keeps:
          - The newest N versions (``VERSION_RETENTION_COUNT``)
          - The deployed version (if any) — even if it falls outside the window

        Returns the number of versions deleted.
        """
        keep = runtime_config.VERSION_RETENTION_COUNT
        if keep <= 0:
            return 0

        # Find the deployed version so we can protect it
        latest = self.get(workflow_id)
        deployed_v = None
        if latest and latest.workflow:
            deployed_v = getattr(latest.workflow, "deployed_version", None)

        rows = self._db.fetchall(
            "SELECT version FROM workflow_versions WHERE workflow_id = ? ORDER BY version DESC",
            (workflow_id,),
        )
        all_versions = [r["version"] for r in rows]
        if len(all_versions) <= keep:
            return 0

        # Versions to keep: the top N plus the deployed version
        keep_set = set(all_versions[:keep])
        if deployed_v is not None:
            keep_set.add(deployed_v)

        to_delete = [v for v in all_versions if v not in keep_set]
        if not to_delete:
            return 0

        placeholders = ",".join("?" for _ in to_delete)
        self._db.execute(
            f"DELETE FROM workflow_versions WHERE workflow_id = ? AND version IN ({placeholders})",
            (workflow_id, *to_delete),
        )
        self._db.commit()
        logger.info(
            "version retention: pruned %d old version(s) of workflow %s (keeping %d + deployed=%s)",
            len(to_delete), workflow_id, keep, deployed_v,
        )
        return len(to_delete)

    def get(
        self,
        workflow_id: str,
        version: int | None = None,
        workspace_id: str | None = None,
    ) -> WorkflowVersion | None:
        """Get a specific version (or latest) of a workflow.

        When ``workspace_id`` is provided, the row is only returned if
        it belongs to that workspace. A caller that passes the wrong
        workspace gets None back, which the API layer should translate
        to 404 (NOT 403) so the existence of the workflow doesn't leak
        across tenant boundaries.
        """
        if version is None:
            if workspace_id is None:
                row = self._db.fetchone(
                    "SELECT data FROM workflow_versions WHERE workflow_id = ? ORDER BY version DESC LIMIT 1",
                    (workflow_id,),
                )
            else:
                row = self._db.fetchone(
                    "SELECT data FROM workflow_versions WHERE workflow_id = ? AND workspace_id = ? ORDER BY version DESC LIMIT 1",
                    (workflow_id, workspace_id),
                )
        else:
            if workspace_id is None:
                row = self._db.fetchone(
                    "SELECT data FROM workflow_versions WHERE workflow_id = ? AND version = ?",
                    (workflow_id, version),
                )
            else:
                row = self._db.fetchone(
                    "SELECT data FROM workflow_versions WHERE workflow_id = ? AND version = ? AND workspace_id = ?",
                    (workflow_id, version, workspace_id),
                )
        if row is None:
            return None
        data = json.loads(row["data"])
        # 2026-05-22: rewrite legacy step types (csv_source / db_sink /
        # webhook_trigger / etc.) into the modern generic shape before
        # the Pydantic model reconstructs them. Keeps old saved
        # workflows runnable without forcing a re-save. See
        # ``fpulse.ir.migrations.migrate_legacy_node_types`` for the
        # full remap policy + per-step warning log.
        from fpulse.ir.migrations import migrate_legacy_node_types
        wf_blob = data.get("workflow")
        if isinstance(wf_blob, dict):
            migrate_legacy_node_types(wf_blob)
        return WorkflowVersion(**data)

    def get_latest_workflow(self, workflow_id: str) -> Workflow | None:
        """Get the latest workflow object."""
        v = self.get(workflow_id)
        return v.workflow if v else None

    def list_all(self, workspace_id: str | None = None) -> list[dict]:
        """List all workflows with latest version info.

        When ``workspace_id`` is provided the result is restricted to
        that workspace — used by the workspace-scoped API router to
        enforce the tenant boundary. When None (legacy path / admin
        tooling), every workflow in the database is returned.
        """
        # Get latest version per workflow, optionally scoped to a
        # single workspace. The filter lives on the OUTER wv row
        # rather than the subquery so a workflow whose old versions
        # predate the workspace_id back-fill still gets counted by
        # the latest (back-filled) row.
        # Pulled in v15: also surface the latest version's content_hash so
        # listings (Admin Deployments tab, dashboards) can display the
        # signed-artifact short prefix without a second round-trip per row.
        # 2026-05-28 — added two correlated subqueries against
        # execution_logs so the Pipelines page's "Last Run" column has
        # the timestamp + status it expects. Previously the column was
        # always dashed because the list endpoint didn't join on the
        # execution log at all. Correlated subqueries are O(N) over
        # workflows but N is small for OSS (single-node, typically
        # tens-to-hundreds of workflows); upgrade to a window-function
        # join when the Plus scale gets us into the thousands.
        #
        # 2026-05-28 (later) — execution_logs is created lazily by
        # ExecutionLogger.__init__ rather than by the main TABLES /
        # migrations block. In production the lifespan always
        # instantiates ExecutionLogger before any list_all call, so
        # the JOIN works. In tests + admin tools that hit
        # WorkflowStore.list_all on a fresh DB without booting the
        # full app, the table doesn't exist and the JOIN crashes
        # with "no such table: execution_logs". Defensive: try the
        # enriched query first; on OperationalError fall back to a
        # plain query that emits last_run / last_run_status as NULL.
        # The frontend already handles those as "—".
        last_run_sql = (
            "(SELECT started_at FROM execution_logs "
            " WHERE workflow_id = wv.workflow_id "
            " ORDER BY started_at DESC LIMIT 1) AS last_run, "
            "(SELECT status FROM execution_logs "
            " WHERE workflow_id = wv.workflow_id "
            " ORDER BY started_at DESC LIMIT 1) AS last_run_status"
        )
        null_run_sql = "NULL AS last_run, NULL AS last_run_status"

        def _run(select_extra: str) -> list:
            if workspace_id is None:
                return self._db.fetchall(f"""
                    SELECT wv.data, wv.content_hash, {select_extra}
                    FROM workflow_versions wv
                    INNER JOIN (
                        SELECT workflow_id, MAX(version) as max_v
                        FROM workflow_versions GROUP BY workflow_id
                    ) latest ON wv.workflow_id = latest.workflow_id AND wv.version = latest.max_v
                """)
            return self._db.fetchall(
                f"""
                SELECT wv.data, wv.content_hash, {select_extra}
                FROM workflow_versions wv
                INNER JOIN (
                    SELECT workflow_id, MAX(version) as max_v
                    FROM workflow_versions GROUP BY workflow_id
                ) latest ON wv.workflow_id = latest.workflow_id AND wv.version = latest.max_v
                WHERE wv.workspace_id = ?
                """,
                (workspace_id,),
            )

        try:
            rows = _run(last_run_sql)
        except sqlite3.OperationalError as exc:
            # Most common cause: execution_logs hasn't been created
            # yet because ExecutionLogger wasn't instantiated (tests,
            # admin tools, fresh installs hit before the lifespan
            # boot). Any other OperationalError re-raises so we don't
            # mask a real schema bug.
            if "execution_logs" not in str(exc):
                raise
            rows = _run(null_run_sql)
        result = []
        for row in rows:
            data = json.loads(row["data"])
            wf = data.get("workflow", {})
            result.append({
                "id": wf.get("id", ""),
                "name": wf.get("name", ""),
                "description": wf.get("description", ""),
                # Documentation fields (self-documenting pipelines). Surfaced
                # here so listing consumers — the inventory report's "what &
                # why", the Pipelines page — get them without a per-row fetch.
                # `tags` was already read by the report but never emitted here,
                # so it was silently empty; fixed alongside.
                "business_purpose": wf.get("business_purpose", "") or "",
                "readme": wf.get("readme", "") or "",
                "tags": wf.get("tags", []) or [],
                "project_id": wf.get("project_id", "default"),
                "folder_id": wf.get("folder_id"),
                "version": data.get("version", 1),
                "step_count": len(wf.get("steps", [])),
                "status": wf.get("status", "draft"),
                "updated_at": wf.get("updated_at", ""),
                "created_at": wf.get("created_at", ""),
                "deployed_version": wf.get("deployed_version"),
                "deployed_at": wf.get("deployed_at"),
                "deployed_by": wf.get("deployed_by"),
                "rollback_from": wf.get("rollback_from"),
                "published_at": wf.get("published_at"),
                "published_by": wf.get("published_by"),
                # 2026-05-28 — owner_id / owner_name expose the
                # workflow's creator so the Pipelines page Author
                # column has data. Both fields already live on the
                # workflow schema (see ir/schema.py:277-278); they
                # were just dropped on the way out of list_all.
                "owner_id": wf.get("owner_id", ""),
                "owner_name": wf.get("owner_name", ""),
                # 2026-05-28 — Paused-pipeline badge on the Pipelines
                # page reads these. Defaults TRUE (matches the
                # behaviour in schema.py:313-314 — a workflow is
                # active by default in both environments).
                "is_active_dev": wf.get("is_active_dev", True),
                "is_active_prod": wf.get("is_active_prod", True),
                # Latest version's hash. Empty string on legacy rows
                # (pre-v15) where no hash was stored at save time.
                "content_hash": row["content_hash"] or "",
                # 2026-05-28 — last_run + last_run_status from the JOIN.
                # NULL on a workflow that's never been run; the frontend
                # TimeAgo component renders that as "—" which is correct
                # ("no runs yet"), distinct from the previous "always —
                # because the field never made it across the wire".
                "last_run": row["last_run"] or "",
                "last_run_status": row["last_run_status"] or "",
                # Surface metadata so the Pipelines listing can render the
                # priority chip without fetching each workflow individually.
                # Kept as the full dict (small — typically a handful of
                # keys) rather than just metadata.priority so future
                # metadata-aware UI surfaces don't need another schema
                # change. (May 11 2026.)
                "metadata": wf.get("metadata") or {},
            })
        return result

    def list_all_full(self, workspace_id: str | None = None) -> list[dict]:
        """List all workflows with their **full** workflow JSON, not just
        the listing-summary shape that ``list_all()`` returns.

        Z43 (2026-05-23) — the lineage scanners
        (``compute_workspace_usage`` for Storage USED BY, and
        ``compute_connection_usage`` for Connections USED BY) need the
        full ``steps`` array of every workflow so they can find
        connection_id / local_table_source / local_table_sink references.
        ``list_all()`` strips that out (returns ``step_count`` instead)
        so both scanners had been silently returning empty results.

        Returns a list of dicts shaped like::

            { "id": ..., "name": ..., "workspace_id": ..., "project_id": ...,
              "metadata": {...}, "status": ..., "version": ...,
              "steps": [ {id, type, params, ...}, ... ],
              "connections": [ ... ],
              "parameters": [ ... ] }

        i.e. the same ``data["workflow"]`` blob the SQL row stores, with
        the wrapper-level ``version`` and ``workspace_id`` flattened in
        so the caller doesn't need to dig through the wrapper. The
        version reflects whichever max_v row was selected — same as
        list_all().
        """
        if workspace_id is None:
            rows = self._db.fetchall("""
                SELECT wv.data, wv.workspace_id, wv.version FROM workflow_versions wv
                INNER JOIN (
                    SELECT workflow_id, MAX(version) as max_v
                    FROM workflow_versions GROUP BY workflow_id
                ) latest ON wv.workflow_id = latest.workflow_id AND wv.version = latest.max_v
            """)
        else:
            rows = self._db.fetchall("""
                SELECT wv.data, wv.workspace_id, wv.version FROM workflow_versions wv
                INNER JOIN (
                    SELECT workflow_id, MAX(version) as max_v
                    FROM workflow_versions GROUP BY workflow_id
                ) latest ON wv.workflow_id = latest.workflow_id AND wv.version = latest.max_v
                WHERE wv.workspace_id = ?
                """,
                (workspace_id,),
            )
        out: list[dict] = []
        for row in rows:
            try:
                data = json.loads(row["data"])
            except (TypeError, ValueError):
                # Corrupt row — skip rather than blow up the whole scan.
                continue
            wf = data.get("workflow") or {}
            if not isinstance(wf, dict):
                continue
            # Flatten wrapper metadata into the workflow dict so callers
            # have one consistent shape. Don't clobber a workflow that
            # already carries these fields (defensive — the IR layer
            # should always wrap, but tests pass bare dicts).
            wf.setdefault("workspace_id", row["workspace_id"])
            wf.setdefault("version", row["version"])
            out.append(wf)
        return out

    def get_versions(self, workflow_id: str) -> list[dict]:
        """List all versions of a workflow, including content_hash."""
        rows = self._db.fetchall(
            "SELECT data, content_hash FROM workflow_versions WHERE workflow_id = ? ORDER BY version ASC",
            (workflow_id,),
        )
        result = []
        for row in rows:
            data = json.loads(row["data"])
            wf = data.get("workflow", {})
            result.append({
                "version": data.get("version", 1),
                "created_by": data.get("created_by", "user"),
                "created_at": data.get("created_at", ""),
                "change_summary": data.get("change_summary", ""),
                "step_count": len(wf.get("steps", [])),
                # Empty string on legacy rows pre-v15 migration.
                "content_hash": row["content_hash"] or "",
            })
        return result

    def verify_version_hash(
        self,
        workflow_id: str,
        version: int,
        workspace_id: str | None = None,
    ) -> tuple[bool, str, str]:
        """Re-compute the hash of a stored version and compare to the
        stored ``content_hash`` column.

        Returns ``(match, stored_hash, recomputed_hash)``.

        Semantics:
          * stored_hash == ""        → legacy row (pre-v15); caller should
            treat as "no verification possible" and proceed (returns
            ``(True, "", recomputed_hash)`` so the match flag is forgiving).
          * stored_hash == recomputed → OK, content is intact.
          * stored_hash != recomputed → TAMPER/CORRUPTION detected.
            Caller (e.g. rollback endpoint) should refuse to proceed.

        Memory: one row read, one hash computed, both released on return.
        """
        row = self._db.fetchone(
            "SELECT data, content_hash FROM workflow_versions WHERE workflow_id = ? AND version = ?"
            + (" AND workspace_id = ?" if workspace_id else ""),
            (workflow_id, version, workspace_id) if workspace_id else (workflow_id, version),
        )
        if row is None:
            return (False, "", "")
        stored = row["content_hash"] or ""
        data = json.loads(row["data"])
        wv = WorkflowVersion(**data)
        recomputed = compute_workflow_hash(wv.workflow)
        if stored == "":
            # Legacy row — can't verify. Forgive so rollback of pre-v15
            # versions isn't blocked; a warning is logged by the caller.
            return (True, "", recomputed)
        return (stored == recomputed, stored, recomputed)

    def diff(self, workflow_id: str, v1: int, v2: int) -> dict | None:
        """Compute a simple diff between two versions."""
        ver1 = self.get(workflow_id, v1)
        ver2 = self.get(workflow_id, v2)
        if not ver1 or not ver2:
            return None

        w1 = ver1.workflow
        w2 = ver2.workflow

        steps1 = {s.id: s for s in w1.steps}
        steps2 = {s.id: s for s in w2.steps}

        added = [s.id for s in w2.steps if s.id not in steps1]
        removed = [sid for sid in steps1 if sid not in steps2]
        modified = []
        for sid in steps1:
            if sid in steps2:
                if steps1[sid].model_dump() != steps2[sid].model_dump():
                    modified.append(sid)

        return {
            "from_version": v1,
            "to_version": v2,
            "added_steps": added,
            "removed_steps": removed,
            "modified_steps": modified,
            "connections_changed": (
                [c.model_dump() for c in w1.connections] != [c.model_dump() for c in w2.connections]
            ),
        }

    def update_status(
        self,
        workflow_id: str,
        status: PipelineStatus,
        test_results: dict | None = None,
        published_by: str | None = None,
        deployed_version: int | None = None,
        rollback_from: int | None = None,
    ) -> WorkflowVersion | None:
        """Update the status of the latest workflow version in-place."""
        latest = self.get(workflow_id)
        if not latest:
            return None
        latest.workflow.status = status
        latest.workflow.updated_at = datetime.now(timezone.utc)
        if test_results is not None:
            latest.workflow.test_results = test_results
        if status == PipelineStatus.PUBLISHED:
            latest.workflow.published_at = datetime.now(timezone.utc)
            if published_by:
                latest.workflow.published_by = published_by
        # Deployment tracking
        if deployed_version is not None:
            latest.workflow.deployed_version = deployed_version
            latest.workflow.deployed_at = datetime.now(timezone.utc)
            latest.workflow.deployed_by = published_by
        if rollback_from is not None:
            latest.workflow.rollback_from = rollback_from

        # Update in place
        data = latest.model_dump(mode="json")
        self._db.execute(
            "UPDATE workflow_versions SET data = ? WHERE workflow_id = ? AND version = ?",
            (json.dumps(data, default=str), workflow_id, latest.version),
        )
        self._db.commit()
        return latest

    def delete(self, workflow_id: str) -> bool:
        """Delete a workflow and all its versions."""
        cursor = self._db.execute(
            "DELETE FROM workflow_versions WHERE workflow_id = ?",
            (workflow_id,),
        )
        self._db.commit()
        return cursor.rowcount > 0
