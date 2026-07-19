"""
Tests for the agent tool registry + 4 initial tool handlers.

Covers:
  fpulse.ai.tools.base       — ToolTier, ToolDefinition validation
  fpulse.ai.tools.registry   — ToolRegistry register/get/list_by_tier/filter
  fpulse.ai.tools.{4 tools}  — handler shape + dry-run + required args
  fpulse.ai.tools (package)  — register_initial_tools auto-wires schemas

No LLM, no network, no DB.
"""

from __future__ import annotations

import asyncio

import pytest

from fpulse.ai.normalize import clear_registry, normalize_tool_output
from fpulse.ai.tools import (
    COMPOSE_REPORT,
    GET_INSTALLATION_HEALTH,
    INITIAL_TOOLS,
    INSPECT_CONNECTIONS,
    LIST_PIPELINES,
    QUERY_METRICS,
    SUMMARIZE_PIPELINE,
    ToolContext,
    ToolDefinition,
    ToolNotFoundError,
    ToolRegistry,
    ToolTier,
    register_initial_tools,
)


# ---------------------------------------------------------------------------
# base.py — ToolDefinition validation
# ---------------------------------------------------------------------------

def test_tool_definition_requires_idempotency_for_write_tier():
    async def noop(inputs, ctx):
        return {}

    with pytest.raises(ValueError, match="MUST require idempotency_key"):
        ToolDefinition(
            name="bad_write",
            tier=ToolTier.SAFE_WRITE,
            description="x",
            input_schema={},
            output_schema={},
            handler=noop,
            requires_idempotency_key=False,  # offending value
        )


def test_tool_definition_rejects_raw_string_tier():
    async def noop(inputs, ctx):
        return {}
    with pytest.raises(TypeError):
        ToolDefinition(
            name="bad",
            tier="read",  # type: ignore[arg-type]
            description="x",
            input_schema={},
            output_schema={},
            handler=noop,
        )


def test_tool_definition_renders_anthropic_schema():
    schema = SUMMARIZE_PIPELINE.to_anthropic_schema()
    assert schema["name"] == "summarize_pipeline"
    assert "description" in schema
    assert "input_schema" in schema
    assert "pipeline_id" in schema["input_schema"]["properties"]


# ---------------------------------------------------------------------------
# registry.py — ToolRegistry
# ---------------------------------------------------------------------------

def test_registry_register_get_roundtrip():
    reg = ToolRegistry()
    reg.register(SUMMARIZE_PIPELINE)
    assert "summarize_pipeline" in reg
    assert reg.get("summarize_pipeline").name == "summarize_pipeline"


def test_registry_get_missing_raises():
    reg = ToolRegistry()
    with pytest.raises(ToolNotFoundError):
        reg.get("nope")


def test_registry_register_initial_loads_all_tools():
    """The initial set has grown from 11 → 24 since the round-3 review:
    +5 READ (get_running_executions, get_next_scheduled, list_catalog,
    recall_history, get_installation_health) + further READ adds
    (validate_pipeline, explain_step, list_templates, lookup_help_topic),
    +3 SAFE_WRITE (modify_pipeline_step, draft_alert_rule,
    draft_pipeline_from_intent), +1 HIGH_IMPACT_WRITE (apply_pipeline_draft).
    Distribution: 21 READ + 4 SAFE_WRITE + 1 HIGH_IMPACT_WRITE = 26.
    (2026-06-16 — +list_steward_findings READ tool.)
    """
    reg = ToolRegistry()
    register_initial_tools(reg)
    assert len(reg) == 26
    for name in (
        # Original 10 READ
        "get_user_role", "get_workspace_overview",
        "list_pipelines", "list_projects", "list_schedules",
        "list_alerts", "list_executions", "inspect_connections",
        "summarize_pipeline", "query_metrics",
        # Added READ (May 1 + 4 + 9 batches)
        "get_running_executions", "get_next_scheduled",
        "list_catalog", "recall_history",
        "get_installation_health",
        # Editor assistant Phase 1 (2026-05-12)
        "validate_pipeline", "explain_step",
        # Templates discovery
        "list_templates",
        # Atlas lookup (2026-05-17)
        "lookup_help_topic",
        # Storage inventory (2026-05-25) — single consolidated tool that
        # answers "what files / tables / outputs do I have?" so the
        # Copilot can reach Storage without three separate calls.
        "list_storage",
        # Steward advisories (2026-06-16) — duplicate sources, governance,
        # connector health, user-rule matches (read-only).
        "list_steward_findings",
        # SAFE_WRITE
        "compose_report", "draft_pipeline_from_intent",
        "modify_pipeline_step", "draft_alert_rule",
        # HIGH_IMPACT_WRITE
        "apply_pipeline_draft",
    ):
        assert name in reg, f"missing tool: {name}"


def test_registry_filter_by_tiers_excludes_writes_for_read_only():
    reg = ToolRegistry()
    register_initial_tools(reg)
    read_only = reg.filter_by_tiers([ToolTier.READ])
    names = {t.name for t in read_only}
    expected_read = {
        "get_user_role", "get_workspace_overview",
        "list_pipelines", "list_projects", "list_schedules",
        "list_alerts", "list_executions", "inspect_connections",
        "summarize_pipeline", "query_metrics",
        "get_running_executions", "get_next_scheduled",
        "list_catalog", "recall_history",
        "get_installation_health",
        # Added since the original test was written
        "validate_pipeline", "explain_step",
        "list_templates",
        "lookup_help_topic",  # Atlas lookup (2026-05-17)
        "list_storage",       # Storage inventory (2026-05-25)
        "list_steward_findings",  # Steward advisories (2026-06-16)
    }
    assert names == expected_read


def test_registry_filter_by_tiers_includes_safe_write_when_allowed():
    reg = ToolRegistry()
    register_initial_tools(reg)
    rw = reg.filter_by_tiers([ToolTier.READ, ToolTier.SAFE_WRITE])
    # 21 READ + 4 SAFE_WRITE = 25
    # 2026-05-25 — bumped after +list_storage (Storage inventory tool).
    # 2026-06-16 — bumped after +list_steward_findings (Steward advisories).
    assert len(rw) == 25


def test_rbac_unknown_role_allows_all_tiers_on_oss():
    """OSS open-world default (2026-05-17).

    Per feedback_oss_no_admin_role.md, OSS is single-bootstrap-user and
    'RBAC + roles are Plus features'. So unknown role names on OSS must
    NOT block tool calls — they should get full access.

    The matrix is still defined for KNOWN roles (viewer/developer/admin/…)
    so OSS code is byte-identical to Plus code (open-core), but unknown
    roles flip from DENY to ALLOW on OSS. Plus keeps closed-world.
    """
    from fpulse.ai.rbac import allowed_tiers_for, authorize_tool_call

    # No license manager in test env → OSS → unknown role gets all tiers.
    tiers = allowed_tiers_for("data_engineer", "dev")
    assert ToolTier.READ in tiers, (
        "Unknown role on OSS must include READ — see rbac.py module "
        "docstring and feedback_oss_no_admin_role.md"
    )
    assert ToolTier.SAFE_WRITE in tiers
    assert ToolTier.HIGH_IMPACT_WRITE in tiers

    # Single-call authorize check.
    assert authorize_tool_call(
        tool_tier=ToolTier.READ,
        user_role="data_engineer",
        environment="dev",
    ) is True

    # Known roles still hit the matrix unchanged.
    viewer_tiers = allowed_tiers_for("viewer", "dev")
    assert viewer_tiers == (ToolTier.READ,), (
        "Known role 'viewer' must keep the matrix-defined tier set "
        "(READ only); only UNKNOWN roles flip to open-world on OSS"
    )


def test_registry_includes_one_high_impact_write_in_initial_set():
    """The initial set has exactly one HIGH_IMPACT_WRITE: apply_pipeline_draft.
    Round-3 reviewer originally said zero; the constraint was relaxed once
    the dry-run-by-default + confirmation card + idempotency-cache layers
    landed (Step 1.5b governance). The single allowed entry is gated by
    all three layers — see ai-ops-contract.md.
    """
    reg = ToolRegistry()
    register_initial_tools(reg)
    high = reg.list_by_tier(ToolTier.HIGH_IMPACT_WRITE)
    names = {t.name for t in high}
    assert names == {"apply_pipeline_draft"}


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def _ctx(*, dry_run: bool = False) -> ToolContext:
    return ToolContext(
        tenant_id="t-1",
        user_id="u-1",
        workspace_id="ws-1",
        environment="dev",
        dry_run=dry_run,
    )


def test_summarize_pipeline_returns_expected_shape():
    out = asyncio.run(SUMMARIZE_PIPELINE.handler({"pipeline_id": "p-1"}, _ctx()))
    assert "node_count" in out
    assert isinstance(out["source_types"], list)
    assert isinstance(out["destination_types"], list)


def test_summarize_pipeline_requires_pipeline_id():
    with pytest.raises(ValueError, match="pipeline_id is required"):
        asyncio.run(SUMMARIZE_PIPELINE.handler({}, _ctx()))


def test_inspect_connections_returns_list():
    out = asyncio.run(INSPECT_CONNECTIONS.handler({}, _ctx()))
    assert isinstance(out["connections"], list)
    assert out["total"] == len(out["connections"])
    # Confirm no credentials in payload
    text = repr(out)
    assert "password" not in text.lower()
    assert "host" not in text.lower() or "host" in text.lower() and ":" not in text  # liberal
    for conn in out["connections"]:
        assert "host" not in conn
        assert "username" not in conn


def test_query_metrics_rejects_unknown_window():
    with pytest.raises(ValueError, match="Unsupported window"):
        asyncio.run(QUERY_METRICS.handler(
            {"scope": "workspace", "keys": ["runs"], "window": "last_year"},
            _ctx(),
        ))


def test_query_metrics_returns_per_key_metrics():
    out = asyncio.run(QUERY_METRICS.handler(
        {"scope": "workspace", "keys": ["runs", "success_rate"], "window": "last_24h"},
        _ctx(),
    ))
    assert len(out["metrics"]) == 2
    assert out["window"] == "last_24h"


def test_compose_report_requires_idempotency_key():
    with pytest.raises(ValueError, match="idempotency_key is required"):
        asyncio.run(COMPOSE_REPORT.handler({"template": "monthly"}, _ctx()))


def test_compose_report_dry_run_returns_stable_draft_id():
    inputs = {"template": "monthly", "idempotency_key": "free.u-1.compose.r-1.v1"}
    out = asyncio.run(COMPOSE_REPORT.handler(inputs, _ctx(dry_run=True)))
    assert out["draft_id"] == "dry-run-draft"


def test_compose_report_live_returns_fresh_draft_id():
    inputs = {"template": "monthly", "idempotency_key": "free.u-1.compose.r-1.v1"}
    out = asyncio.run(COMPOSE_REPORT.handler(inputs, _ctx(dry_run=False)))
    assert out["draft_id"]
    assert out["draft_id"] != "dry-run-draft"
    # Second call with same inputs returns a DIFFERENT draft_id (no real
    # idempotency layer in Step 1.5a — that's Step 1.5b's job).
    out2 = asyncio.run(COMPOSE_REPORT.handler(inputs, _ctx(dry_run=False)))
    assert out2["draft_id"] != out["draft_id"]


# ---------------------------------------------------------------------------
# normalize.py wiring
# ---------------------------------------------------------------------------

def test_register_initial_tools_also_registers_normalize_schemas():
    clear_registry()
    register_initial_tools(ToolRegistry())
    # Normalize knows the shape — feed a valid summarize_pipeline result
    # (output schema includes `parameters` since the May 1 expansion that
    # surfaced declared pipeline parameters in the summary).
    payload = {
        "node_count": 7,
        "source_types": ["csv_source"],
        "destination_types": ["db_sink"],
        "alerts_configured": True,
        "last_run_status": "success",
        "parameters": [],
    }
    out = normalize_tool_output("summarize_pipeline", payload)
    assert out == payload


def test_initial_tools_count():
    """Canonical count is 26 — 21 READ + 4 SAFE_WRITE + 1 HIGH_IMPACT_WRITE.
    See `docs/product_facts/10_ai_copilot.md` for the per-tier breakdown.

    Last bumped 2026-06-16: +list_steward_findings (read-only Steward
    advisories — duplicate sources, governance, connector health, user rules).
    Earlier bumps: +list_storage (2026-05-25), +lookup_help_topic (2026-05-17),
    +validate_pipeline + explain_step (2026-05-12), and +list_templates.
    """
    assert len(INITIAL_TOOLS) == 26


def test_list_pipelines_handler_returns_shape():
    out = asyncio.run(LIST_PIPELINES.handler({}, _ctx()))
    assert "pipelines" in out
    assert isinstance(out["pipelines"], list)
    assert isinstance(out["total"], int)
    assert out["workspace_id"]
    # Real pipelines come from WorkflowStore via app_state. In test env without
    # app_state populated, the handler returns empty list (best-effort).


def test_list_pipelines_name_filter_is_case_insensitive():
    # Inject a fake app_state with a fake store so the handler hits the real path
    import fpulse.main as fp_main
    if not hasattr(fp_main, "app_state"):
        fp_main.app_state = {}

    class _FakeStore:
        def list_all(self, workspace_id=None):
            return [
                {"id": "p-1", "name": "Sales ETL",     "status": "published", "step_count": 5, "updated_at": "2026-04-29T10:00:00Z"},
                {"id": "p-2", "name": "Marketing Sync", "status": "draft",     "step_count": 3, "updated_at": "2026-04-29T11:00:00Z"},
                {"id": "p-3", "name": "sales report",  "status": "published", "step_count": 7, "updated_at": "2026-04-29T12:00:00Z"},
            ]

    saved_store = fp_main.app_state.get("store")
    fp_main.app_state["store"] = _FakeStore()
    try:
        out = asyncio.run(LIST_PIPELINES.handler({"name_filter": "SALES"}, _ctx()))
        assert out["total"] == 2
        names = {p["name"] for p in out["pipelines"]}
        assert names == {"Sales ETL", "sales report"}
    finally:
        if saved_store is None:
            fp_main.app_state.pop("store", None)
        else:
            fp_main.app_state["store"] = saved_store


def test_list_pipelines_definition_is_read_tier():
    assert LIST_PIPELINES.tier == ToolTier.READ
    assert not LIST_PIPELINES.requires_idempotency_key


# ---------------------------------------------------------------------------
# get_installation_health — wraps InventoryCollector
# ---------------------------------------------------------------------------


def _stub_report(*, totals=None, health=None, recent_failures=()):
    """Build a stand-in InventoryReport for tool tests.

    Imports happen inside the helper because fpulse.reports.inventory pulls
    in optional deps (reportlab, etc.) only available in full test installs.
    """
    from fpulse.reports.inventory import (
        FailureAnalysis,
        InventoryReport,
        OperationalAudit,
        RecentFailure,
    )
    audit = OperationalAudit(
        window_hours=24,
        total_executions=10,
        successful_executions=8,
        failed_executions=2,
        success_rate_pct=80.0,
        recent_failures=[
            RecentFailure(
                workflow_id=f["workflow_id"],
                workflow_name=f["workflow_name"],
                failed_at=f["failed_at"],
                error=f["error"],
            )
            for f in recent_failures
        ],
    )
    return InventoryReport(
        generated_at="2026-05-09T00:00:00Z",
        generated_by="system",
        scope="admin",
        workspace_id="ws-1",
        workspace_name="Default",
        fpulse_version="1.0.0",
        schema_version=22,
        tier="free",
        env_filter="all",
        totals=totals or {},
        health=health or {"score": 100, "issue_count": 0, "issues": []},
        operational_audit=audit,
        failure_analysis=FailureAnalysis(),
    )


def _patch_collector(monkeypatch, report):
    """Replace InventoryCollector.collect() with one that returns `report`."""
    from fpulse.reports import inventory as inv_mod

    class _FakeCollector:
        def __init__(self, *a, **kw):
            pass
        def collect(self):
            return report

    monkeypatch.setattr(inv_mod, "InventoryCollector", _FakeCollector)


def _ensure_app_state():
    import fpulse.main as fp_main
    if not hasattr(fp_main, "app_state"):
        fp_main.app_state = {}
    return fp_main


def test_get_installation_health_clean_install_scores_100(monkeypatch):
    _ensure_app_state()
    _patch_collector(monkeypatch, _stub_report(
        totals={
            "projects": 0, "pipelines": 0, "pipelines_deployed": 0,
            "pipelines_in_prod": 0, "connections": 0,
            "connections_inline_creds": 0, "schedules": 0,
            "schedules_enabled": 0, "alerts": 0, "alerts_enabled": 0,
        },
        health={"score": 100, "issue_count": 0, "issues": []},
    ))
    out = asyncio.run(GET_INSTALLATION_HEALTH.handler({}, _ctx()))
    assert out["score"] == 100
    assert out["issue_count"] == 0
    assert out["issues"] == []
    assert out["totals"]["pipelines"] == 0
    assert out["top_failing_pipelines"] == []
    assert out["tier"] == "free"
    assert out["environment"] == "all"


def test_get_installation_health_surfaces_inline_credentials(monkeypatch):
    _ensure_app_state()
    _patch_collector(monkeypatch, _stub_report(
        totals={"connections": 3, "connections_inline_creds": 2},
        health={
            "score": 90,
            "issue_count": 1,
            "issues": [
                "2 connection(s) still hold inline credentials — migrate to Vault.",
            ],
        },
    ))
    out = asyncio.run(GET_INSTALLATION_HEALTH.handler({}, _ctx()))
    assert out["score"] == 90
    assert out["issue_count"] == 1
    assert "inline credentials" in out["issues"][0]
    assert out["totals"]["connections_inline_creds"] == 2


def test_get_installation_health_falls_back_to_recent_failures(monkeypatch):
    _ensure_app_state()
    _patch_collector(monkeypatch, _stub_report(
        recent_failures=[
            {"workflow_id": "p-1", "workflow_name": "Nightly ETL",
             "failed_at": "2026-05-08T22:14:00Z", "error": "DB timeout"},
            {"workflow_id": "p-2", "workflow_name": "Sales sync",
             "failed_at": "2026-05-08T18:02:00Z", "error": "401 Unauthorized"},
        ],
    ))
    out = asyncio.run(GET_INSTALLATION_HEALTH.handler({"top_n_failures": 5}, _ctx()))
    assert out["recent_failures_24h"] == 2
    assert len(out["top_failing_pipelines"]) == 2
    names = [p["pipeline_name"] for p in out["top_failing_pipelines"]]
    assert names == ["Nightly ETL", "Sales sync"]
    assert out["top_failing_pipelines"][0]["last_error"] == "DB timeout"


def test_get_installation_health_clamps_top_n_failures(monkeypatch):
    _ensure_app_state()
    _patch_collector(monkeypatch, _stub_report(
        recent_failures=[
            {"workflow_id": f"p-{i}", "workflow_name": f"P{i}",
             "failed_at": "2026-05-08T22:14:00Z", "error": "boom"}
            for i in range(20)
        ],
    ))
    # Out-of-range top_n is clamped to the module max (5).
    out = asyncio.run(GET_INSTALLATION_HEALTH.handler({"top_n_failures": 999}, _ctx()))
    assert len(out["top_failing_pipelines"]) == 5


def test_get_installation_health_returns_empty_payload_on_collector_failure(monkeypatch):
    _ensure_app_state()
    from fpulse.reports import inventory as inv_mod

    class _Boom:
        def __init__(self, *a, **kw):
            pass
        def collect(self):
            raise RuntimeError("simulated DB outage")

    monkeypatch.setattr(inv_mod, "InventoryCollector", _Boom)

    out = asyncio.run(GET_INSTALLATION_HEALTH.handler({}, _ctx()))
    assert out["score"] == 0
    assert out["issue_count"] == 1
    assert "RuntimeError" in out["issues"][0]
    assert out["totals"]["pipelines"] == 0


def test_get_installation_health_definition_is_read_tier():
    assert GET_INSTALLATION_HEALTH.tier == ToolTier.READ
    assert not GET_INSTALLATION_HEALTH.requires_idempotency_key
    schema = GET_INSTALLATION_HEALTH.to_anthropic_schema()
    assert schema["name"] == "get_installation_health"
    # Description must mention the user-facing trigger phrases so the LLM
    # picks this tool for "what needs my attention" / "punch list" prompts.
    desc = schema["description"].lower()
    assert "punch list" in desc
    assert "health" in desc


def test_get_installation_health_normalizes_against_registered_schema():
    register_initial_tools(ToolRegistry())  # ensures normalize schema is wired
    payload = {
        "score": 90,
        "issue_count": 1,
        "issues": ["x"],
        "totals": {"pipelines": 0},
        "top_failing_pipelines": [],
        "recent_failures_24h": 0,
        "success_rate_pct_24h": 0.0,
        "workspace_id": "ws-1",
        "environment": "all",
        "tier": "free",
    }
    assert normalize_tool_output("get_installation_health", payload) == payload
