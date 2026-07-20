"""
Initial tool registrations for the F-Pulse agent loop.

Per round-3 reviewer ("start with 4 read-only tools; add write tools only
after Step 1.5b governance is complete"), the initial registry holds:
  - 3 READ tools:        summarize_pipeline, inspect_connections, query_metrics
  - 1 SAFE_WRITE tool:   compose_report (draft-only, idempotency required)

NO HIGH_IMPACT_WRITE tools yet. Those land in Step 4 (create_schedule,
send_to_destination) once governance + confirmation gates exist.

Importing this package auto-registers tools into the default registry and
their output schemas into normalize.py.
"""

from __future__ import annotations

from fpulse.ai.normalize import register_output_schema
from fpulse.ai.tools.apply_pipeline_draft import DEFINITION as APPLY_PIPELINE_DRAFT
from fpulse.ai.tools.base import ToolContext, ToolDefinition, ToolTier
from fpulse.ai.tools.compose_report import DEFINITION as COMPOSE_REPORT
from fpulse.ai.tools.draft_alert_rule import DEFINITION as DRAFT_ALERT_RULE
from fpulse.ai.tools.draft_pipeline_from_intent import DEFINITION as DRAFT_PIPELINE_FROM_INTENT
from fpulse.ai.tools.modify_pipeline_step import DEFINITION as MODIFY_PIPELINE_STEP
from fpulse.ai.tools.get_installation_health import DEFINITION as GET_INSTALLATION_HEALTH
from fpulse.ai.tools.get_next_scheduled import DEFINITION as GET_NEXT_SCHEDULED
from fpulse.ai.tools.get_running_executions import DEFINITION as GET_RUNNING_EXECUTIONS
from fpulse.ai.tools.get_user_role import DEFINITION as GET_USER_ROLE
from fpulse.ai.tools.list_catalog import DEFINITION as LIST_CATALOG
from fpulse.ai.tools.inspect_connections import DEFINITION as INSPECT_CONNECTIONS
from fpulse.ai.tools.list_alerts import DEFINITION as LIST_ALERTS
from fpulse.ai.tools.list_executions import DEFINITION as LIST_EXECUTIONS
from fpulse.ai.tools.list_pipelines import DEFINITION as LIST_PIPELINES
from fpulse.ai.tools.list_projects import DEFINITION as LIST_PROJECTS
from fpulse.ai.tools.list_schedules import DEFINITION as LIST_SCHEDULES
from fpulse.ai.tools.list_steward_findings import DEFINITION as LIST_STEWARD_FINDINGS
from fpulse.ai.tools.list_storage import DEFINITION as LIST_STORAGE
from fpulse.ai.tools.list_templates import DEFINITION as LIST_TEMPLATES
from fpulse.ai.tools.lookup_help_topic import DEFINITION as LOOKUP_HELP_TOPIC
from fpulse.ai.tools.query_metrics import DEFINITION as QUERY_METRICS
from fpulse.ai.tools.recall_history import DEFINITION as RECALL_HISTORY
from fpulse.ai.tools.registry import (
    ToolNotFoundError,
    ToolRegistry,
    default_registry,
    reset_default_registry,
)
from fpulse.ai.tools.summarize_pipeline import DEFINITION as SUMMARIZE_PIPELINE
from fpulse.ai.tools.validate_pipeline import DEFINITION as VALIDATE_PIPELINE
from fpulse.ai.tools.explain_step import DEFINITION as EXPLAIN_STEP
from fpulse.ai.tools.workspace_overview import DEFINITION as GET_WORKSPACE_OVERVIEW

INITIAL_TOOLS: tuple[ToolDefinition, ...] = (
    # Identity / overview first — common entrypoints for "who am I" / "what's here"
    GET_USER_ROLE,
    GET_WORKSPACE_OVERVIEW,
    # List tools — the agent's main read surface across the F-Pulse data model
    LIST_PIPELINES,
    LIST_PROJECTS,
    LIST_SCHEDULES,
    LIST_ALERTS,
    LIST_EXECUTIONS,
    # Storage inventory (files / managed tables / pipeline outputs).
    # One tool covers all three slices so the agent can answer
    # "what files / tables do I have?" without three separate calls.
    LIST_STORAGE,
    INSPECT_CONNECTIONS,
    # Public catalog awareness — agent can answer "what connectors do
    # you support" / "is Snowflake on Plus" without spelunking source.
    LIST_CATALOG,
    # Templates discovery — built-in starters + user-saved templates.
    # Lets the agent recommend a template when the user describes a
    # use case ("I need to sync Postgres to a warehouse").
    LIST_TEMPLATES,
    # Drill-down + metrics
    SUMMARIZE_PIPELINE,
    VALIDATE_PIPELINE,
    EXPLAIN_STEP,
    QUERY_METRICS,
    # RAG retrieval — workspace history + docs
    RECALL_HISTORY,
    # Atlas lookup — structured product-knowledge map (pages, glossary,
    # how-tos, tools, nodes, connectors, docs). Added May 17 2026.
    # See backend/fpulse/ai/atlas/ for the topic catalog.
    LOOKUP_HELP_TOPIC,
    # Operational reads — running-state + upcoming schedule visibility +
    # whole-install health rollup (score + punch list + top failures)
    GET_RUNNING_EXECUTIONS,
    GET_NEXT_SCHEDULED,
    GET_INSTALLATION_HEALTH,
    # Steward advisories — duplicate sources, governance, connector health,
    # user-rule matches (read-only; the only live-fed Steward detectors).
    LIST_STEWARD_FINDINGS,
    # Safe-write (draft only) — pipeline build, edit-in-place, alert scaffold, report compose
    DRAFT_PIPELINE_FROM_INTENT,
    MODIFY_PIPELINE_STEP,
    DRAFT_ALERT_RULE,
    COMPOSE_REPORT,
    # High-impact write — apply a confirmed pipeline draft (the only mutation
    # tool the agent has; gated by RBAC + dry-run-by-default + confirmation).
    APPLY_PIPELINE_DRAFT,
)


def register_initial_tools(registry: ToolRegistry | None = None) -> ToolRegistry:
    """Register the 26 initial tools + their output schemas. Idempotent.

    Pass `registry=None` (default) to register into the per-process default;
    pass an explicit ToolRegistry to register into a test-owned instance.

    Output schemas are pulled from each ToolDefinition.output_schema and
    forwarded to fpulse.ai.normalize so normalize_tool_output enforces them
    on every tool result.
    """
    target = registry if registry is not None else default_registry()
    for tool in INITIAL_TOOLS:
        target.register(tool)
        register_output_schema(tool.name, tool.output_schema)
    return target


__all__ = [
    "ToolContext",
    "ToolDefinition",
    "ToolRegistry",
    "ToolTier",
    "ToolNotFoundError",
    "default_registry",
    "reset_default_registry",
    "register_initial_tools",
    "INITIAL_TOOLS",
    # Identity / overview
    "GET_USER_ROLE",
    "GET_WORKSPACE_OVERVIEW",
    # List tools
    "LIST_PIPELINES",
    "LIST_PROJECTS",
    "LIST_SCHEDULES",
    "LIST_ALERTS",
    "LIST_EXECUTIONS",
    "LIST_STORAGE",
    "INSPECT_CONNECTIONS",
    "LIST_CATALOG",
    "LIST_TEMPLATES",
    # Drill-down
    "SUMMARIZE_PIPELINE",
    "QUERY_METRICS",
    "RECALL_HISTORY",
    "LOOKUP_HELP_TOPIC",
    # Operational reads
    "GET_RUNNING_EXECUTIONS",
    "GET_NEXT_SCHEDULED",
    "GET_INSTALLATION_HEALTH",
    "LIST_STEWARD_FINDINGS",
    # Safe-write
    "DRAFT_PIPELINE_FROM_INTENT",
    "MODIFY_PIPELINE_STEP",
    "DRAFT_ALERT_RULE",
    "COMPOSE_REPORT",
    # High-impact write
    "APPLY_PIPELINE_DRAFT",
]
