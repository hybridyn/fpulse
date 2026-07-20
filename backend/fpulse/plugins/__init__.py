"""
Plugin System — dynamic node loading from external packages.

Plugins are directories or zip files containing:
  - manifest.json  — plugin metadata, node declarations
  - nodes/         — Python modules with BaseNode subclasses

Plugin manifest format:
{
  "id": "my-plugin",
  "name": "My Plugin",
  "version": "1.0.0",
  "author": "developer",
  "description": "Custom nodes for ...",
  "nodes": [
    {
      "module": "nodes.my_node",
      "class": "MyNode",
      "step_type": "my_custom_node"
    }
  ]
}
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class PluginManifest:
    """Parsed plugin manifest."""
    def __init__(self, data: dict, path: str):
        self.id = data.get("id", "")
        self.name = data.get("name", "Unknown Plugin")
        self.version = data.get("version", "0.0.0")
        self.author = data.get("author", "")
        self.description = data.get("description", "")
        self.nodes = data.get("nodes", [])
        self.path = path
        self.loaded = False
        self.error = ""
        self.loaded_at = 0.0


class PluginManager:
    """Discovers, loads, and manages F-Pulse plugins."""

    def __init__(self, plugins_dir: str = "plugins", db=None):
        self._plugins_dir = plugins_dir
        self._db = db
        self._plugins: dict[str, PluginManifest] = {}
        self._loaded_nodes: list[str] = []
        if db:
            self._ensure_tables()

    def _ensure_tables(self):
        if not self._db:
            return
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS plugins (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                version TEXT DEFAULT '0.0.0',
                author TEXT DEFAULT '',
                description TEXT DEFAULT '',
                path TEXT DEFAULT '',
                is_enabled INTEGER DEFAULT 1,
                node_count INTEGER DEFAULT 0,
                installed_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)

    # ── Discovery ─────────────────────────────────────────────────────

    def discover(self) -> list[PluginManifest]:
        """Scan the plugins directory for valid plugin manifests."""
        if not os.path.isdir(self._plugins_dir):
            os.makedirs(self._plugins_dir, exist_ok=True)
            return []

        discovered = []
        for entry in os.listdir(self._plugins_dir):
            plugin_path = os.path.join(self._plugins_dir, entry)
            if not os.path.isdir(plugin_path):
                continue

            manifest_path = os.path.join(plugin_path, "manifest.json")
            if not os.path.isfile(manifest_path):
                continue

            try:
                with open(manifest_path, "r") as f:
                    data = json.load(f)
                manifest = PluginManifest(data, plugin_path)
                self._plugins[manifest.id] = manifest
                discovered.append(manifest)
                logger.info("Discovered plugin: %s v%s (%d nodes)",
                           manifest.name, manifest.version, len(manifest.nodes))
            except Exception as e:
                logger.warning("Failed to load plugin manifest at %s: %s", manifest_path, e)

        return discovered

    # ── Loading ───────────────────────────────────────────────────────

    def load_all(self) -> dict:
        """Discover and load all plugins."""
        discovered = self.discover()
        results = {"loaded": 0, "failed": 0, "nodes": 0, "errors": []}

        for manifest in discovered:
            # Check if disabled in DB
            if self._db:
                row = self._db.fetchone("SELECT is_enabled FROM plugins WHERE id=?", (manifest.id,))
                if row and not row["is_enabled"]:
                    continue

            try:
                nodes_loaded = self._load_plugin(manifest)
                manifest.loaded = True
                manifest.loaded_at = time.time()
                results["loaded"] += 1
                results["nodes"] += nodes_loaded

                # Record in DB
                if self._db:
                    self._db.execute(
                        "INSERT OR REPLACE INTO plugins "
                        "(id, name, version, author, description, path, is_enabled, node_count, installed_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                        (manifest.id, manifest.name, manifest.version, manifest.author,
                         manifest.description, manifest.path, nodes_loaded, time.time(), time.time()),
                    )
            except Exception as e:
                manifest.error = str(e)
                results["failed"] += 1
                results["errors"].append({"plugin": manifest.id, "error": str(e)})
                logger.error("Failed to load plugin %s: %s", manifest.id, e)

        return results

    def _load_plugin(self, manifest: PluginManifest) -> int:
        """Load a single plugin's node modules into the registry."""
        from fpulse.ir.schema import StepType
        from fpulse.nodes.registry import _REGISTRY
        from fpulse.nodes.base import BaseNode

        nodes_loaded = 0
        plugin_path = manifest.path

        # Add plugin path to sys.path temporarily
        if plugin_path not in sys.path:
            sys.path.insert(0, plugin_path)

        for node_def in manifest.nodes:
            module_name = node_def.get("module", "")
            class_name = node_def.get("class", "")
            step_type_str = node_def.get("step_type", "")

            if not all([module_name, class_name, step_type_str]):
                continue

            try:
                # Dynamic import
                spec = importlib.util.find_spec(module_name)
                if spec is None:
                    # Try relative to plugin path
                    module_path = os.path.join(plugin_path, module_name.replace(".", os.sep) + ".py")
                    if os.path.isfile(module_path):
                        spec = importlib.util.spec_from_file_location(module_name, module_path)

                if spec is None:
                    logger.warning("Cannot find module %s for plugin %s", module_name, manifest.id)
                    continue

                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                cls = getattr(module, class_name, None)
                if cls is None:
                    logger.warning("Class %s not found in %s", class_name, module_name)
                    continue

                # Ensure it's a BaseNode subclass
                if not (isinstance(cls, type) and issubclass(cls, BaseNode)):
                    logger.warning("%s is not a BaseNode subclass", class_name)
                    continue

                # Register with a dynamic StepType
                # For plugins, we store as a string in a separate dict
                try:
                    stype = StepType(step_type_str)
                except ValueError:
                    # Not in the enum — register as a dynamic type
                    stype = step_type_str

                _REGISTRY[stype] = cls
                self._loaded_nodes.append(step_type_str)
                nodes_loaded += 1
                logger.info("Loaded plugin node: %s.%s as %s", module_name, class_name, step_type_str)
            except Exception as e:
                logger.warning("Failed to load node %s from plugin %s: %s", class_name, manifest.id, e)

        return nodes_loaded

    # ── Management ────────────────────────────────────────────────────

    def list_plugins(self) -> list[dict]:
        """List all discovered plugins with status."""
        result = []
        for pid, manifest in self._plugins.items():
            result.append({
                "id": manifest.id,
                "name": manifest.name,
                "version": manifest.version,
                "author": manifest.author,
                "description": manifest.description,
                "path": manifest.path,
                "loaded": manifest.loaded,
                "error": manifest.error,
                "node_count": len(manifest.nodes),
                "nodes": [n.get("step_type", "") for n in manifest.nodes],
            })

        # Also include DB-registered plugins not yet discovered
        if self._db:
            rows = self._db.fetchall("SELECT * FROM plugins ORDER BY installed_at DESC")
            seen = {p["id"] for p in result}
            for r in rows:
                if r["id"] not in seen:
                    result.append({
                        "id": r["id"], "name": r["name"], "version": r["version"],
                        "author": r["author"], "description": r["description"],
                        "path": r["path"], "loaded": False, "error": "Not discovered",
                        "node_count": r["node_count"], "nodes": [],
                        "is_enabled": bool(r["is_enabled"]),
                    })
        return result

    def enable_plugin(self, plugin_id: str):
        if self._db:
            self._db.execute("UPDATE plugins SET is_enabled=1, updated_at=? WHERE id=?",
                           (time.time(), plugin_id))

    def disable_plugin(self, plugin_id: str):
        if self._db:
            self._db.execute("UPDATE plugins SET is_enabled=0, updated_at=? WHERE id=?",
                           (time.time(), plugin_id))

    def get_scaffold(self) -> dict:
        """Return a scaffold / template for creating a new plugin."""
        return {
            "manifest.json": {
                "id": "my-plugin",
                "name": "My Custom Plugin",
                "version": "1.0.0",
                "author": "Your Name",
                "description": "Custom nodes for F-Pulse",
                "nodes": [
                    {
                        "module": "nodes.my_node",
                        "class": "MyCustomNode",
                        "step_type": "my_custom_node",
                    }
                ],
            },
            "nodes/my_node.py": (
                'from fpulse.nodes.base import BaseNode, ExecutionContext\n'
                'import duckdb\n\n'
                'class MyCustomNode(BaseNode):\n'
                '    display_name = "My Custom Node"\n'
                '    category = "custom"\n'
                '    description = "A custom processing node"\n\n'
                '    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:\n'
                '        upstream = self.params.get("_input_step_ids", [])\n'
                '        if upstream:\n'
                '            rel = ctx.get_input(upstream[0])\n'
                '            if rel is not None:\n'
                '                return rel\n'
                '        return ctx.conn.sql("SELECT \'hello\' AS message")\n\n'
                '    @staticmethod\n'
                '    def default_params() -> dict:\n'
                '        return {"custom_param": ""}\n\n'
                '    @staticmethod\n'
                '    def param_schema() -> list[dict]:\n'
                '        return [\n'
                '            {"name": "custom_param", "type": "text", "label": "Custom Parameter",\n'
                '             "default": "", "description": "Your custom configuration"}\n'
                '        ]\n'
            ),
        }
