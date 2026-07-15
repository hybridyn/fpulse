"""Node type registry — maps StepType to execution logic."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from fpulse.ir.schema import StepType

if TYPE_CHECKING:
    from fpulse.nodes.base import BaseNode

# Process-wide singleton dict. Stashed on the `sys` module so that even
# if `fpulse.nodes.registry` itself gets re-imported (test fixtures that
# wipe sys.modules between test files, multiple worker processes that
# fork an already-initialised parent, etc.), every copy of this module
# shares the SAME underlying registry dict. Without this, pre-wipe
# imports (e.g. WorkflowExecutor held by a pre-wipe test module) read
# from an empty Mv1 dict while the populated registry lives in Mv2 —
# every execute_workflow() then raises "No node registered for type".
_SENTINEL = "_fpulse_node_registry"
if not hasattr(sys, _SENTINEL):
    setattr(sys, _SENTINEL, {})
_REGISTRY: dict[StepType, type["BaseNode"]] = getattr(sys, _SENTINEL)


def register(step_type: StepType):
    """Decorator to register a node class for a step type."""
    def decorator(cls):
        _REGISTRY[step_type] = cls
        return cls
    return decorator


class NodeRegistry:
    """Access registered node implementations."""

    @staticmethod
    def get(step_type: StepType) -> type["BaseNode"]:
        # Lazy populate on first miss — covers the case where the executor
        # gets called before anything triggered the explicit node-module
        # imports. Idempotent (re-import of an already-loaded module is a
        # no-op). The process-wide singleton _REGISTRY (see module top)
        # ensures the populated entries are visible regardless of which
        # module instance is holding this method's __globals__.
        if step_type not in _REGISTRY:
            get_registry()
        if step_type not in _REGISTRY:
            raise ValueError(f"No node registered for type: {step_type}")
        return _REGISTRY[step_type]

    @staticmethod
    def all_types() -> list[dict]:
        """Return metadata for all registered node types.

        Also expands REST_CONNECTOR into one virtual palette entry per manifest,
        so each SaaS connector (Salesforce, HubSpot, Stripe, ...) appears as its
        own draggable item in the frontend palette.
        """
        result = []
        for stype, cls in _REGISTRY.items():
            result.append({
                "type": stype.value,
                "label": cls.display_name,
                "category": cls.category,
                "description": cls.description,
                "default_params": cls.default_params(),
                "param_schema": cls.param_schema(),
            })

        # NOTE: Per-manifest virtual entries (`rest:salesforce`, etc.) are no
        # longer emitted. The single `saas_connector` node now exposes all
        # manifests via its own dropdown — see /api/saas/manifests for the data.
        return result


def get_registry() -> NodeRegistry:
    # Force import all node modules to trigger registration
    from fpulse.nodes import (
        csv_source, db_source, filter_node, transform,
        deduplicate, aggregate, join, output,
        activities, flow_control, sources, sinks,
        quality, ai, control_extras, cloud_storage,
        file_node, cloud_files,
        advanced_transforms, retry_handler,
        generic,
        scd2,  # Sprint 1 / Gate 1 — slowly-changing dimension Type 2
        data_wrangler,  # Stepwise visible transform (design-data-wrangler-node.md)
        local_table,  # 2026-05-23 (Y3): managed-table source + sink
    )
    # Connector framework: REST manifests + JDBC + CDC + OpenAPI + Vector DBs
    try:
        from fpulse.connectors import rest_framework  # noqa: F401
        from fpulse.connectors import jdbc  # noqa: F401
        from fpulse.connectors import cdc  # noqa: F401
        from fpulse.connectors import openapi_source  # noqa: F401
        from fpulse.connectors import vector_db  # noqa: F401
        # Pre-load REST manifests so list_manifests() is populated for /api/node-types
        from fpulse.connectors.rest_framework import load_manifests
        load_manifests()
    except Exception as e:
        print(f"[registry] connector modules failed to load: {e}")
    return NodeRegistry()
