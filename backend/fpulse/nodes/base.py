"""Base class for all F-Pulse nodes."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, TYPE_CHECKING

# Stage 2.5b: duckdb only used for type annotations on BaseNode.execute,
# ExecutionContext.conn / .set_result / .get_input. ExecutionContext
# stores conn at construction time — the runtime conn comes from the
# executor that created it, so this module never needs to import duckdb
# itself. `from __future__ import annotations` makes every annotation
# below a deferred string.
if TYPE_CHECKING:
    import duckdb


class BaseNode(ABC):
    """Base node — every node type implements this."""

    display_name: str = "Node"
    category: str = "general"  # source | transform | output
    description: str = ""

    def __init__(self, params: dict[str, Any]):
        self.params = params

    @abstractmethod
    def execute(self, ctx: "ExecutionContext") -> duckdb.DuckDBPyRelation:
        """Execute this node and return a DuckDB relation."""
        ...

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {}

    @staticmethod
    def param_schema() -> list[dict]:
        """Return parameter definitions for the UI."""
        return []

    def is_preview(self, ctx: "ExecutionContext") -> bool:
        """R8 (2026-05-30) — helper for side-effect nodes.

        Returns True when the executor was dispatched in preview mode
        (Plan-only / dry-run). Side-effect nodes should call this at
        the top of execute() and short-circuit without firing the real
        side effect when True. Pure transforms ignore this flag — they
        always run because they only mutate the in-process relation,
        which is what preview mode WANTS to inspect.
        """
        return bool(getattr(ctx, "preview_mode", False))

    @staticmethod
    def preview_message(params: dict[str, Any], row_count: int) -> str | None:
        """X4 (2026-05-30) — node-specific preview-mode message.

        When the executor short-circuits a side-effect node in preview
        mode (see executor._run_node_once R8b block), it calls this
        hook to get a human-readable description of what the node
        WOULD have done. The default returns None → executor uses the
        generic "side effect skipped (preview run)" message.

        Subclasses that want to be more helpful override this with
        something like ``f"would have written {row_count} rows to "
        f"{params.get('file_path')}"``. The string lands in the
        per-step run log so the operator can verify the dry-run did
        what they expected.

        Argument ``row_count`` is the size of the upstream relation
        the node would have consumed.
        """
        return None

    @staticmethod
    def expected_output_schema(
        input_schemas: list[list[dict]],
        params: dict[str, Any],
    ) -> list[dict] | None:
        """Predict what columns this node would emit, without running it.

        R5 (2026-05-30) — schema preview without execution.

        Arguments
        ---------
        input_schemas
            One per upstream input. Each is a ``[{name, type}, ...]``
            column list. Empty list for source nodes (no inputs).
        params
            The node's saved params. Same dict the node uses at
            ``execute()`` time.

        Return
        ------
        ``[{name, type}, ...]`` predicted output column list, OR
        ``None`` when the node can't infer statically (executable-only
        nodes like ``code_script``, ``api_source`` whose schema lives
        in the remote response).

        Subclasses override this when the schema is deducible from
        params + input shape — e.g. ``filter`` passes through,
        ``derived_column`` appends, ``aggregate`` rewrites to
        ``group_by + agg``. Default returns None so the schema-preview
        endpoint reports "schema will be known after the first run."
        """
        return None


class ExecutionContext:
    """Shared context passed through a workflow execution."""

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        data_dir: str = ".",
        full_run: bool = False,
        app_state: dict[str, Any] | None = None,
        run_id: str | None = None,
        preview_mode: bool = False,
    ):
        self.conn = conn
        self.data_dir = data_dir
        # When True, source nodes skip the DEV_SAMPLE_ROWS limit and
        # load the full dataset.  Set by the "Run Full" button in the UI.
        self.full_run = full_run
        self._results: dict[str, duckdb.DuckDBPyRelation] = {}
        # Label → step_id map, populated by the executor so expressions can
        # reference upstream nodes by their display label (`$('Node Name')`).
        self.node_labels: dict[str, str] = {}
        # Workspace variables for $vars.FOO templates.
        self.vars: dict[str, Any] = {}
        # Reference to app-level stores (workflow store, etc.) so nodes
        # like ExecutePipeline can access other workflows.
        self.app_state: dict[str, Any] = app_state or {}
        # Per-run identifier, propagated from WorkflowExecutor. Optional
        # because some test paths instantiate ExecutionContext directly.
        # The connection pool needs this to scope cached driver
        # connections to the current run (per `DESIGN_CONNECTION_POOLING.md`).
        self.run_id: str | None = run_id
        # R8 (2026-05-30) — side-effect dry-run flag. When True, any
        # node in SIDE_EFFECT_CLASS (sinks, send_email, slack_notify,
        # etc.) should NOT fire its real side effect. Instead it logs
        # what it would have done and passes the input relation
        # through unchanged. Set by the executor when the run was
        # dispatched via the Preview / Plan-only path. Pure transforms
        # ignore this flag.
        self.preview_mode: bool = preview_mode
        # Z33 (2026-05-23) — sink nodes need to see workflow-level
        # context to record provenance. `workflow_id` lets the sink
        # back-link the managed table it writes to the pipeline that
        # produced it. `workflow_metadata` carries scaffold-set hints
        # like `source_object_id` (set by the Storage Z1 wand so the
        # file row can show "Prepared as schema.name" later).
        # `step_params` is keyed by step_id and exposes each step's
        # `params` dict so a sink can peek at its upstream Wrangler's
        # recipe without depending on the full IR being threaded.
        # All three default to empty so test paths that build
        # ExecutionContext directly stay green.
        self.workflow_id: str | None = None
        self.workflow_metadata: dict[str, Any] = {}
        self.step_params: dict[str, dict[str, Any]] = {}
        # Incremental-sync watermarks BUFFERED during the run and committed by
        # the executor only after the whole run (incl. sinks) succeeds. Advancing
        # a source cursor at read-time would skip rows whose downstream sink then
        # failed — silent data loss. Empty for non-incremental / direct-ctx tests.
        self.pending_sync_cursors: list = []
        # 2026-06-11 (multi-output branch routing): per-step routed-input
        # override. The executor sets this just before a node executes when
        # the node consumes a non-`output` branch port of an upstream node
        # (e.g. the True branch of an if_condition). It maps upstream
        # step_id → the routed+stripped relation, so EVERY node sees the
        # right rows through the normal get_input(s) path — not just the
        # handful that opted into get_routed_inputs. Left None for ordinary
        # (single-output) pipelines, making the whole mechanism a strict
        # no-op for everything that doesn't wire a branch port.
        self._routed_override: dict[str, Any] | None = None
        # 2026-06-15 (C1 heterogeneous multi-output): secondary named
        # outputs keyed by ``step_id -> {port_name: relation}``. Distinct
        # from ``_split_output`` branch routing, which only partitions ONE
        # schema into row-subsets. A node whose ports carry DIFFERENT
        # schemas (e.g. Data Profile: data passthrough on ``output`` +
        # column-stats on ``report``) registers each secondary relation
        # here via :meth:`set_named_output`; the executor's routing override
        # consults :meth:`get_named_output` FIRST and only falls back to the
        # ``_split_output`` filter when no named output exists for the port.
        # The PRIMARY output always remains the ``execute()`` return value
        # (stored in ``_results``), so single-output nodes are untouched.
        self._named_outputs: dict[str, dict[str, Any]] = {}
        # Set by the executor immediately before each node runs (see
        # scoped_name). Optional — test paths that build a context by hand and
        # call execute() directly may leave it unset, which is fine because a
        # single isolated node can't self-collide on a shared internal name.
        self.current_step_id: str | None = None

    def scoped_name(self, base_name: str) -> str:
        """Return a per-step-unique DuckDB identifier derived from ``base_name``.

        Several nodes stage their input through a FIXED internal view / temp
        table name (e.g. ``"__derived_input"``) and then return a LAZY relation
        such as ``ctx.conn.sql("SELECT ... FROM __derived_input")``. When a
        pipeline chains TWO nodes of the SAME type, the second node re-registers
        that shared name with an input relation that still references the first
        node's view of the *same* name → DuckDB raises ``Binder Error: infinite
        recursion detected: attempting to recursively bind view
        "__derived_input"`` (or, for ``CREATE OR REPLACE TEMP TABLE``, silently
        clobbers the first node's staged data) and the run fails.

        Scoping the name by the active step id makes each node's internal object
        unique, so two same-type nodes never collide. The step id is sanitised
        to an identifier-safe suffix; when it's absent the bare ``base_name`` is
        used.
        """
        step_id = getattr(self, "current_step_id", None)
        if step_id:
            suffix = "".join(
                ch if (ch.isalnum() or ch == "_") else "_" for ch in str(step_id)
            )
            return f"{base_name}_{suffix}" if suffix else base_name
        return base_name

    def register_scoped(self, base_name: str, relation) -> str:
        """Register ``relation`` under a per-step-unique view name and return it.

        Thin wrapper over :meth:`scoped_name` + ``conn.register``. Callers MUST
        reference the returned name in their SQL, not the bare ``base_name``.
        See :meth:`scoped_name` for the recursion bug this prevents.
        """
        name = self.scoped_name(base_name)
        self.conn.register(name, relation)
        return name

    def set_result(self, step_id: str, relation: duckdb.DuckDBPyRelation):
        self._results[step_id] = relation

    def set_named_output(self, step_id: str, port: str, relation) -> None:
        """Register a SECONDARY output relation for ``step_id`` on ``port``.

        Used by heterogeneous multi-output nodes (C1, 2026-06-15) to emit a
        relation whose schema differs from the primary ``execute()`` return.
        ``port`` must NOT be ``"output"`` (that's reserved for the primary
        return value) — a guard drops such calls so a node can't accidentally
        shadow its own primary output. Downstream steps wired to ``port`` then
        receive this relation through the normal ``get_input(s)`` path.
        """
        if not port or port == "output":
            return
        self._named_outputs.setdefault(step_id, {})[port] = relation

    def get_named_output(self, step_id: str, port: str):
        """Return the secondary relation registered for ``(step_id, port)``,
        or ``None`` when the step emitted no named output on that port."""
        if not port or port == "output":
            return None
        return self._named_outputs.get(step_id, {}).get(port)

    def get_input(self, step_id: str) -> duckdb.DuckDBPyRelation | None:
        if self._routed_override is not None and step_id in self._routed_override:
            return self._routed_override[step_id]
        return self._results.get(step_id)

    def get_inputs(self, step_ids: list[str]) -> list[duckdb.DuckDBPyRelation]:
        out: list[duckdb.DuckDBPyRelation] = []
        for sid in step_ids:
            if self._routed_override is not None and sid in self._routed_override:
                out.append(self._routed_override[sid])
            elif sid in self._results:
                out.append(self._results[sid])
        return out

    def route_relation(self, relation, from_port):
        """Filter a branch-tagged relation down to a single output port.

        2026-06-11 branch-output routing. STRICTLY ADDITIVE: this only
        changes the relation when it carries a ``_split_output`` column
        (emitted by branch nodes such as conditional_split) AND ``from_port``
        is a real branch name — never the legacy ``"output"``. For every
        existing pipeline (single output handle => from_port="output", no
        ``_split_output`` column) it returns the relation UNCHANGED. Any
        failure falls back to the unrouted relation so routing can never
        break a run.
        """
        if not from_port or from_port == "output" or relation is None:
            return relation
        try:
            cols = list(relation.columns)
        except Exception:
            return relation
        if "_split_output" not in cols:
            return relation
        keep = [c for c in cols if c != "_split_output"]
        safe_port = str(from_port).replace("'", "''")
        try:
            filtered = relation.filter(f"_split_output = '{safe_port}'")
            if not keep:
                return filtered
            return filtered.project(", ".join(f'"{c}"' for c in keep))
        except Exception:
            return relation

    def get_routed_inputs(self, step_ids, ports=None):
        """Like :meth:`get_inputs`, but filters each input by its branch port.

        ``ports`` is the consuming node's ``_input_step_ports`` —
        ``[(from_step, from_port, to_port), ...]`` stamped by the executor
        (executor.py:_build_input_map). Backward-compatible: inputs whose
        from_port is ``"output"`` (or that carry no ``_split_output`` column)
        are returned unchanged.
        """
        port_by_step: dict = {}
        for entry in (ports or []):
            try:
                port_by_step.setdefault(entry[0], entry[1])
            except Exception:
                continue
        routed = []
        for sid in step_ids:
            if sid not in self._results:
                continue
            routed.append(self.route_relation(self._results[sid], port_by_step.get(sid, "output")))
        return routed

    def results_as_rows(self) -> dict[str, list[dict[str, Any]]]:
        """Snapshot every result as a list of row-dicts (for expression resolution).

        Uses DuckDB's native row/column introspection. Failures per-relation are
        swallowed — an unreadable upstream simply yields an empty list.
        """
        out: dict[str, list[dict[str, Any]]] = {}
        for sid, rel in self._results.items():
            try:
                cols = rel.columns
                rows = rel.fetchall()
                out[sid] = [dict(zip(cols, r)) for r in rows]
            except Exception:
                out[sid] = []
        return out

    def emit_lineage_step_run(
        self,
        *,
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
    ) -> None:
        """L1.1 (2026-06-08, docs/design/lineage-1.2.md) - record a
        runtime lineage event for the step that just ran.

        Best-effort: silently no-ops if the lineage store isn't
        configured (e.g. tests, embedded builds, lineage feature
        disabled). The executor calls this at the success / error
        boundary so every step that produced a result emits one row
        into the lineage_step_runs table without each individual
        node having to know about lineage.

        Read back via ``GET /api/lineage/runs/{run_id}``.
        """
        workflow_id = self.workflow_id or ""
        run_id = self.run_id or ""
        if not (workflow_id and run_id):
            return  # no anchor; nothing to record
        store = None
        try:
            store = (self.app_state or {}).get("lineage_store")
        except Exception:
            return
        if store is None:
            return
        try:
            store.record_step_run(
                workflow_id=workflow_id,
                run_id=run_id,
                step_id=step_id,
                step_label=step_label,
                step_type=step_type,
                columns_in=columns_in or [],
                columns_out=columns_out or [],
                rows_in=rows_in,
                rows_out=rows_out,
                started_at=started_at,
                completed_at=completed_at,
                error=error,
            )
        except Exception:
            # Lineage is observational. Never fail the run over a
            # write error in the lineage path.
            pass
