"""Step-level cache — "Rerun from here" support.

Each successful step's output is persisted as Parquet under::

    <data_dir>/cache/<workflow_id>/<step_id>.parquet

alongside a ``manifest.json``::

    {
      "<step_id>": {
        "effective_hash": "<sha256>",     # params hash × upstream hashes
        "param_hash":     "<sha256>",
        "upstream_hashes": ["<sha256>", ...],
        "parquet_path":   "cache/<wf>/<step>.parquet",
        "row_count":      12345,
        "cached_at":      "2026-04-17T12:34:56Z"
      },
      ...
    }

Resume semantics: when the user asks to rerun node X using the cache,
the executor walks X's dependencies and for each dep compares the
**current** ``effective_hash`` (recomputed from the workflow IR) against
the stored one. Match → register the cached Parquet as that step's
result and skip its ``execute()``. Mismatch → execute normally and
overwrite the cache entry. The cascade happens automatically: editing
node 3's params invalidates everything downstream of 3.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

# Stage 2.5b: duckdb only referenced in string annotations (register_*
# helpers). The cache writes/reads Parquet files — all DuckDB work is
# done through the `conn` argument passed in by callers.
if TYPE_CHECKING:
    import duckdb

from fpulse.ir.schema import Step, Workflow

logger = logging.getLogger(__name__)

_MANIFEST_NAME = "manifest.json"

# Runtime-only param keys that must not contribute to the semantic hash —
# they are injected by the executor each call and don't affect output.
_EPHEMERAL_KEYS = {"_input_step_ids", "_node_labels", "_settings"}

# A few _settings fields DO change output (deactivated bypasses the node,
# continuing on error may emit an empty relation). These are re-included
# in the hash so the cache invalidates when the user toggles them.
# Timeout / retry timing is intentionally excluded — changing those should
# not throw away a perfectly good cached result.
_SETTINGS_AFFECTING_OUTPUT = ("deactivated", "on_error")


def _canonical_params(params: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(params, dict):
        return {}
    canonical = {k: v for k, v in params.items() if k not in _EPHEMERAL_KEYS}
    settings = params.get("_settings") or {}
    if isinstance(settings, dict):
        relevant = {k: settings.get(k) for k in _SETTINGS_AFFECTING_OUTPUT if k in settings}
        if relevant:
            canonical["_settings_subset"] = relevant
    return canonical


def _sha256_json(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class StepCache:
    """Per-workflow on-disk cache of step outputs.

    Thread-safe via a single instance-level lock; file I/O is localised
    to the workflow's cache dir so parallel pipelines don't stomp on
    each other.
    """

    def __init__(self, data_dir: str, workflow_id: str):
        self.data_dir = data_dir
        self.workflow_id = workflow_id
        self.cache_dir = os.path.join(data_dir, "cache", workflow_id)
        self._lock = threading.Lock()
        os.makedirs(self.cache_dir, exist_ok=True)

    # ── Paths ─────────────────────────────────────────────────────────

    @property
    def manifest_path(self) -> str:
        return os.path.join(self.cache_dir, _MANIFEST_NAME)

    def parquet_path(self, step_id: str) -> str:
        safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in step_id)
        return os.path.join(self.cache_dir, f"{safe}.parquet")

    # ── Manifest I/O ──────────────────────────────────────────────────

    def load_manifest(self) -> dict[str, dict]:
        if not os.path.isfile(self.manifest_path):
            return {}
        try:
            with open(self.manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("step_cache: manifest unreadable (%s) — starting fresh", exc)
            return {}

    def _save_manifest(self, manifest: dict[str, dict]) -> None:
        tmp = self.manifest_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
        os.replace(tmp, self.manifest_path)

    # ── Hashing ───────────────────────────────────────────────────────

    @staticmethod
    def param_hash(step: Step) -> str:
        return _sha256_json({
            "type": step.type.value,
            "params": _canonical_params(step.params),
        })

    def compute_effective_hashes(
        self,
        workflow: Workflow,
        input_map: dict[str, list[str]],
    ) -> dict[str, str]:
        """Return `{step_id: effective_hash}` for every step in the workflow.

        effective_hash cascades: changing a parent's params invalidates
        all descendants automatically.
        """
        step_map = {s.id: s for s in workflow.steps}
        effective: dict[str, str] = {}

        def walk(step_id: str) -> str:
            if step_id in effective:
                return effective[step_id]
            step = step_map.get(step_id)
            if step is None:
                effective[step_id] = ""
                return ""
            ph = self.param_hash(step)
            parents = [walk(uid) for uid in input_map.get(step_id, [])]
            eff = _sha256_json({"p": ph, "u": parents})
            effective[step_id] = eff
            return eff

        for s in workflow.steps:
            walk(s.id)
        return effective

    # ── Write ─────────────────────────────────────────────────────────

    def write(
        self,
        step: Step,
        conn: "duckdb.DuckDBPyConnection",
        relation: "duckdb.DuckDBPyRelation",
        effective_hash: str,
        upstream_hashes: list[str],
        row_count: int,
    ) -> str:
        """Persist a step's output as Parquet and update the manifest.

        Uses the register-then-COPY pattern the rest of the executor uses
        (see output.py) — more compatible across DuckDB versions than the
        ``write_parquet()`` shortcut.
        """
        path = self.parquet_path(step.id)
        # Write to a temp file and publish with an atomic rename — a
        # concurrent run of the same workflow (separate StepCache
        # instance, same cache dir) must never observe a half-written
        # parquet at the final path.
        tmp_path = path + ".tmp"
        safe_path = tmp_path.replace("'", "''")
        alias = f"__step_cache_{step.id.replace('-', '_')}"
        try:
            conn.register(alias, relation)
            conn.execute(f"COPY {alias} TO '{safe_path}' (FORMAT PARQUET)")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "step_cache: failed to write parquet for step %s (%s) — "
                "caching disabled for this step on this run",
                step.id, exc,
            )
            try:
                conn.unregister(alias)
            except Exception:
                pass
            try:
                if os.path.isfile(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            return ""
        else:
            try:
                conn.unregister(alias)
            except Exception:
                pass

        with self._lock:
            try:
                os.replace(tmp_path, path)
            except OSError as exc:
                logger.warning(
                    "step_cache: could not publish parquet for step %s (%s)",
                    step.id, exc,
                )
                return ""
            manifest = self.load_manifest()
            manifest[step.id] = {
                "effective_hash": effective_hash,
                "param_hash": self.param_hash(step),
                "upstream_hashes": upstream_hashes,
                "parquet_path": os.path.relpath(path, self.data_dir).replace("\\", "/"),
                "row_count": int(row_count),
                "cached_at": datetime.now(timezone.utc).isoformat(),
            }
            self._save_manifest(manifest)
        return path

    # ── Read ──────────────────────────────────────────────────────────

    def hit(self, step_id: str, expected_effective_hash: str) -> str | None:
        """Return the cached parquet path if the hash matches, else None."""
        # Read under the lock so a same-instance write() can't swap the
        # manifest between our hash check and the path lookup.
        with self._lock:
            manifest = self.load_manifest()
        entry = manifest.get(step_id)
        if not entry:
            return None
        if entry.get("effective_hash") != expected_effective_hash:
            return None
        rel_path = entry.get("parquet_path", "")
        abs_path = os.path.join(self.data_dir, rel_path) if rel_path else ""
        if abs_path and os.path.isfile(abs_path):
            return abs_path
        return None

    def load_relation(
        self, conn: "duckdb.DuckDBPyConnection", step_id: str, expected_effective_hash: str,
    ) -> "duckdb.DuckDBPyRelation | None":
        """Load a cached step output into the given DuckDB connection."""
        path = self.hit(step_id, expected_effective_hash)
        if not path:
            return None
        try:
            return conn.read_parquet(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("step_cache: could not read cached parquet for %s: %s", step_id, exc)
            return None

    # ── Eviction ──────────────────────────────────────────────────────

    def clear(self) -> None:
        """Delete every cached parquet + the manifest for this workflow."""
        import shutil
        with self._lock:
            if os.path.isdir(self.cache_dir):
                shutil.rmtree(self.cache_dir, ignore_errors=True)
            os.makedirs(self.cache_dir, exist_ok=True)

    def clear_step(self, step_id: str) -> None:
        """Evict a single step (e.g. after explicit user reset)."""
        with self._lock:
            manifest = self.load_manifest()
            entry = manifest.pop(step_id, None)
            if entry:
                path = os.path.join(self.data_dir, entry.get("parquet_path", ""))
                try:
                    if os.path.isfile(path):
                        os.remove(path)
                except OSError:
                    pass
                self._save_manifest(manifest)

    def summary(self, workflow: Workflow, input_map: dict[str, list[str]]) -> dict:
        """Human-readable cache state vs. current workflow for the UI."""
        manifest = self.load_manifest()
        effective = self.compute_effective_hashes(workflow, input_map)
        steps = []
        for s in workflow.steps:
            entry = manifest.get(s.id)
            if not entry:
                steps.append({
                    "step_id": s.id, "label": s.label or s.type.value,
                    "cached": False, "fresh": False,
                })
                continue
            fresh = entry.get("effective_hash") == effective.get(s.id)
            steps.append({
                "step_id": s.id,
                "label": s.label or s.type.value,
                "cached": True,
                "fresh": fresh,
                "row_count": entry.get("row_count", 0),
                "cached_at": entry.get("cached_at"),
            })
        return {"workflow_id": self.workflow_id, "steps": steps}
