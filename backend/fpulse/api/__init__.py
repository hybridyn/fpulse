from .workflows import router as workflows_router
from .execution import router as execution_router
from .backfills import router as backfills_router
from .planner import router as planner_router
from .projects import router as projects_router
from .folders import router as folders_router
from .workspaces import router as workspaces_router
from .schedules import router as schedules_router
from .alerts import router as alerts_router
from .monitor import router as monitor_router
from .dashboard import router as dashboard_router
from .auth import router as auth_router
from .variables import router as variables_router
from .credentials import router as credentials_router
from .intelligence import router as intelligence_router
from .contracts import router as contracts_router
from .schema_history import router as schema_history_router
from .connections import router as connections_router
from .backup import router as backup_router
from .websocket import router as ws_router
from .websocket import info_router as ws_info_router
from .logs import router as logs_router
from .ai import router as ai_router
from .ai_config import router as ai_config_router
from .templates import router as templates_router
from .exports import router as exports_router
from .notifications import router as notifications_router
from .pool import router as pool_router
from .lineage import router as lineage_router
from .marketplace import router as marketplace_router
from .collaboration import router as collaboration_router
from .gateway import router as gateway_router
from .plugins import router as plugins_router
from .uploads import router as uploads_router
from .storage import router as storage_router
from .health_memory import router as health_memory_router
from .execution_manager import router as execution_manager_router
from .reports import router as reports_router
from .pool_allocation import router as pool_allocation_router
from .workspace_settings import router as workspace_settings_router
from .ai_cost_rates import router as ai_cost_rates_router
from .deployments import router as deployments_router
from .recipes import router as recipes_router
from .agent import router as agent_router
from .ollama import router as ollama_router
from .pre_publish import router as pre_publish_router
from .catalog import router as catalog_router
from .mcp import router as mcp_router
from .activity import router as activity_router
from .cert_matrix import router as cert_matrix_router
from .sync_state import router as sync_state_router
from .trust import router as trust_router
from .product_knowledge import router as product_knowledge_router
from .connector_authoring import router as connector_authoring_router
from .connector_drafts import router as connector_drafts_router
from .ai_web import router as ai_web_router
from .publish_policy import router as publish_policy_router
from .app_meta import router as app_meta_router
from .extraction import router as extraction_router
from .auth_health import router as auth_health_router
from .system import router as system_router
from .pipeline_health import router as pipeline_health_router
from .pipeline_health import per_pipeline_router as pipeline_health_per_router
from .types_meta import router as types_meta_router
from .expressions import router as expressions_router
# 2026-06-05 — F-Pulse Steward (v1.1 — Archeologist). Ships in OSS as the
# headline differentiator vs other open-source orchestrators. See
# docs/steward/overview.md for the full architecture + OSS vs Plus split.
from .steward import router as steward_router

__all__ = [
    "workflows_router",
    "execution_router",
    "backfills_router",
    "planner_router",
    "projects_router",
    "folders_router",
    "workspaces_router",
    "schedules_router",
    "alerts_router",
    "monitor_router",
    "auth_router",
    "variables_router",
    "credentials_router",
    "intelligence_router",
    "contracts_router",
    "schema_history_router",
    "connections_router",
    "backup_router",
    "ws_router",
    "ws_info_router",
    "logs_router",
    "ai_router",
    "ai_config_router",
    "templates_router",
    "exports_router",
    "notifications_router",
    "pool_router",
    "lineage_router",
    "marketplace_router",
    "collaboration_router",
    "gateway_router",
    "plugins_router",
    "uploads_router",
    "storage_router",
    "health_memory_router",
    "execution_manager_router",
    "reports_router",
    "pool_allocation_router",
    "workspace_settings_router",
    "ai_cost_rates_router",
    "agent_router",
    "ollama_router",
    "pre_publish_router",
    "catalog_router",
    "mcp_router",
    "activity_router",
    "cert_matrix_router",
    "trust_router",
    "product_knowledge_router",
    "connector_authoring_router",
    "connector_drafts_router",
    "ai_web_router",
    "publish_policy_router",
    "app_meta_router",
    "extraction_router",
    "auth_health_router",
    "system_router",
    "pipeline_health_router",
    "pipeline_health_per_router",
    "types_meta_router",
    "expressions_router",
]
