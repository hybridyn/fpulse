"""
Session-context block — May 4 2026.

Layer 1 of the chat knowledge architecture (see
project_fpulse_local_only_lock + the May 4 chat-UX work):

    Layer 1  User/env/product context     ALWAYS injected — this module
    Layer 2  Product knowledge RAG        retrieved per turn
    Layer 3  Live workspace state         tool calls

This module owns Layer 1. Every chat turn builds a structured Markdown
block describing WHO is asking, WHERE they are, WHAT tier they're on,
and WHAT they can vs. can't do. The block goes at the top of the
system prompt so the LLM never has to ask the user "what tier are you
on?" or "are you a developer or admin?" — it already knows.

Token budget: ~400-600 tokens for the full block. Stays well inside
qwen2.5:7b's effective context window (32K tokens at the 2026-05-19
tool-use floor) and adds <15s of prompt-processing on CPU.

Public surface:
    build_session_block(page_context, app_state=None) -> str
    SessionSnapshot (dataclass with the same fields the block renders)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from fpulse.ai.context import PageContext

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Static product facts — version-tagged so a release-note diff is clear
# ─────────────────────────────────────────────────────────────────────

# Identity blurb — kept short. The LLM doesn't need marketing copy, it
# needs accurate positioning so it doesn't mis-describe the product to
# the user. Aligned with `project_fpulse_positioning_lock_2026-05-03.md`.
_PRODUCT_IDENTITY = (
    "F-Pulse is a self-hosted, single-tenant data workflow platform with "
    "deterministic execution, AI-assisted logic, and full audit lineage. "
    "By default, no data leaves the host — local-first is the lock, cloud "
    "LLM providers are an explicit opt-in escape hatch."
)


# Tier capability map — what the OSS edition does and does NOT include.
# Source of truth is `edition-matrix.md` + `project_fpulse_oss_plus_split_2026-05-03.md`.
# This is kept brief on purpose: the LLM doesn't need every Plus feature,
# it needs to know the BOUNDARY so it doesn't promise things OSS can't do.
_OSS_FREE_INCLUDES = [
    "single-user pipelines, projects, schedules, alerts, connections",
    "a broad node palette across source / transform / combine / control-flow / action / AI categories (the exact live list is injected below) — most OSS, a few Plus-only (Python Transform, CDC, Vector Sink, JDBC dialect registry)",
    "Bulk Loader for Postgres + Snowflake (other warehouse dialects on the post-1.0 roadmap)",
    "DEV environment (PROD + DEV→PROD promotion is Plus-only per EDITION_MATRIX)",
    "credentials + AI provider API keys encrypted at rest with Fernet (AES-128-CBC + HMAC-SHA256)",
    "agent-tool RBAC (4 roles × 2 envs × 3 tool tiers) — distinct from workspace RBAC",
    "AI Copilot with local Ollama (default) or cloud provider opt-in",
    "RAG: nomic-embed-text + sqlite-vec + recall_history tool",
    "trace store, activity timeline, eval harness, cert matrix, trust posture endpoint",
    "telemetry off by default, opt-in only",
]

_OSS_FREE_EXCLUDES = [
    "PROD environment + DEV→PROD promotion (Plus)",
    "two-gate approval flow with Sandbox dry-run (Plus)",
    "workspace RBAC (5-tier: Super Admin → Viewer) (Plus)",
    "audit log retention + sigstore-signed export (Plus)",
    "SSO / SAML / OIDC (Plus)",
    "IP allowlist, session controls, password policy (Plus)",
    "Vault credential references (HashiCorp / AWS / Azure / GCP) (Plus)",
    "Lineage (Marquez-compatible) (Plus)",
    "Enterprise connectors: SAP, NetSuite, Workday, Dynamics 365, ServiceNow (Plus)",
    "Python Transform, CDC source, Vector DB sinks, JDBC dialect registry (Plus)",
    "team collaboration: shared projects, comments, sticky notes (Plus)",
    "containerized worker pool + horizontal scaling (Plus)",
    "Llama-Guard, cross-session conversational memory (Plus)",
    "compute-usage alerts, drift detection, scheduled backup with retention (Plus)",
    "custom report builder + scheduler (Plus)",
]


# ─────────────────────────────────────────────────────────────────────
# Snapshot dataclass
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SessionSnapshot:
    """Structured Layer-1 context. The block-builder turns this into
    Markdown the LLM reads. Tests construct SessionSnapshots directly
    and assert on the rendered block."""

    user_id: str
    user_role: str
    user_display_name: str
    workspace_id: str
    environment: str
    tier: str                              # "free" | "plus"
    page: str
    page_summary: str
    visible_count: int
    selected_count: int
    filters_active: int
    allowed_tool_tiers: tuple[str, ...]    # ("read",) / ("read","safe_write") / ...
    can_approve: bool
    can_deploy_prod: bool
    workspace_counts: dict[str, int] = field(default_factory=dict)
    # Compact "what's on screen right now" rendering. Built from
    # PageContext.visible_items so the LLM can answer page questions
    # without a tool call to discover screen state.
    page_items_block: str = ""
    # CP-P2 (2026-06-16) — Layer-1 grounding. The agent used to see only
    # COUNTS, so it couldn't answer "I need a lookup from my SQL Server
    # table" without a tool call it rarely made. Inject the user's actual
    # connections (name + type) and the LIVE node-type palette (the same
    # registry /api/node-types reads) so the LLM is grounded up front and
    # stops hallucinating connections / a fixed "37 node types" count.
    connections: tuple[tuple[str, str], ...] = ()      # (name, type)
    node_catalog: dict[str, tuple[str, ...]] = field(default_factory=dict)  # category -> labels


# ─────────────────────────────────────────────────────────────────────
# Snapshot builder — stitches PageContext + app_state into a clean dataclass
# ─────────────────────────────────────────────────────────────────────


def _detect_tier(app_state: dict[str, Any] | None) -> str:
    """Mirrors the heuristic used elsewhere — license_manager.is_plus → "plus",
    else "free". Defensive on missing app_state (test paths)."""
    if not app_state:
        return "free"
    try:
        lm = app_state.get("license_manager")
        if lm is not None and getattr(lm, "is_plus", False):
            return "plus"
    except Exception:  # noqa: BLE001
        pass
    return "free"


def _user_display_name(app_state: dict[str, Any] | None, user_id: str) -> str:
    """Look up the user's friendly name from the user store; fall back to id."""
    if not app_state or user_id == "anonymous":
        return user_id
    try:
        user_store = app_state.get("user_store")
        if user_store is None:
            return user_id
        rec = user_store.get_by_id(user_id)
        if rec is None:
            return user_id
        return getattr(rec, "name", None) or getattr(rec, "username", None) or user_id
    except Exception:  # noqa: BLE001
        return user_id


def _workspace_counts(app_state: dict[str, Any] | None, workspace_id: str) -> dict[str, int]:
    """Cheap counts — same logic as the workspace_overview tool but inline
    so we don't pay an extra tool-call hop. All-zeros on failure."""
    counts: dict[str, int] = {
        "pipelines": 0, "projects": 0, "schedules": 0,
        "alerts": 0, "connections": 0,
    }
    if not app_state:
        return counts

    def _safe_count(key: str, list_method: str = "list_all") -> int:
        try:
            store = app_state.get(key)
            if store is None:
                return 0
            method = getattr(store, list_method, None)
            if method is None:
                return 0
            return len(method(workspace_id=workspace_id))
        except Exception:  # noqa: BLE001
            return 0

    try:
        counts["pipelines"]   = _safe_count("store")
        counts["projects"]    = _safe_count("project_store")
        counts["schedules"]   = _safe_count("schedule_store")
        counts["alerts"]      = _safe_count("alert_store", list_method="list_rules")
        counts["connections"] = _safe_count("connection_store")
    except Exception:  # noqa: BLE001
        pass
    return counts


def _user_connections(
    app_state: dict[str, Any] | None, workspace_id: str, *, limit: int = 20,
) -> tuple[tuple[str, str], ...]:
    """The user's saved connections as (name, type) for Layer-1 grounding.

    Same store the counts use — but the LLM needs the actual names/types
    so it can answer "lookup from my SQL Server table" by pointing at the
    real connection instead of inventing "Prod Postgres". Empty on failure.
    """
    if not app_state:
        return ()
    try:
        store = app_state.get("connection_store")
        if store is None or not hasattr(store, "list_all"):
            return ()
        conns = store.list_all(workspace_id=workspace_id) or []
    except Exception:  # noqa: BLE001
        return ()
    out: list[tuple[str, str]] = []
    for c in conns:
        try:
            name = getattr(c, "name", None) or getattr(c, "id", None) or "connection"
            ctype = (
                getattr(c, "type", None)
                or getattr(c, "connection_type", None)
                or "unknown"
            )
            out.append((str(name), str(ctype)))
        except Exception:  # noqa: BLE001
            continue
        if len(out) >= limit:
            break
    return tuple(out)


def _node_catalog() -> dict[str, tuple[str, ...]]:
    """Live node-type palette grouped by category (label lists).

    Reads the SAME registry `/api/node-types` serves, so this can never
    drift from the palette the way the old hardcoded "37 node types" line
    did. Best-effort — returns {} if the registry can't be loaded (test
    paths without node modules imported).
    """
    try:
        from fpulse.nodes.registry import get_registry
        types = get_registry().all_types()
    except Exception:  # noqa: BLE001
        return {}
    groups: dict[str, list[str]] = {}
    seen: set[tuple[str, str]] = set()
    for t in types:
        try:
            cat = str(t.get("category") or "other")
            label = t.get("label") or t.get("type")
            if not label:
                continue
            key = (cat, str(label))
            if key in seen:
                continue
            seen.add(key)
            groups.setdefault(cat, []).append(str(label))
        except Exception:  # noqa: BLE001
            continue
    return {k: tuple(v) for k, v in groups.items()}


def _allowed_tool_tiers(role: str, environment: str) -> tuple[str, ...]:
    """Resolve via the existing RBAC matrix. Caller may also pass these
    in directly — this is a fallback when only PageContext is available."""
    try:
        from fpulse.ai.rbac import allowed_tiers_for
        from fpulse.ai.tools.base import ToolTier
        tiers = allowed_tiers_for(role, environment)
        return tuple(t.value if isinstance(t, ToolTier) else str(t) for t in tiers)
    except Exception:  # noqa: BLE001
        # Conservative fallback — read only.
        return ("read",)


def _can_approve(role: str) -> bool:
    """OSS Free has no approval gates at all (per
    `project_free_vs_plus_approval_matrix`). Returns True for admin/owner
    role names so the LLM can correctly tell a Plus admin "you can approve
    this", but the actual approval endpoints check tier separately."""
    return role.lower() in ("admin", "owner", "approver", "super_admin")


def _can_deploy_prod(role: str, environment: str) -> bool:
    """Deploy-to-PROD permission. PROD writes for non-admin roles need
    approval (Plus only). For OSS Free this collapses to "developer in DEV
    or admin in either env". """
    role_l = role.lower()
    if role_l in ("admin", "owner", "super_admin"):
        return True
    if environment == "dev" and role_l in ("developer", "engineer"):
        return True
    return False


def build_snapshot(
    page_context: PageContext,
    app_state: dict[str, Any] | None = None,
    *,
    allowed_tool_tiers: tuple[str, ...] | None = None,
) -> SessionSnapshot:
    """Build a SessionSnapshot for one chat turn. Pure factory — no I/O
    side-effects beyond the read-only store lookups it makes for counts /
    user name. Safe to call without app_state (test paths)."""
    user_id = page_context.user_id or "anonymous"
    workspace_id = (
        page_context.workspace_id or page_context.tenant_id or "default"
    )
    role = page_context.role or "viewer"
    env = page_context.environment or "dev"

    tiers = allowed_tool_tiers or _allowed_tool_tiers(role, env)

    return SessionSnapshot(
        user_id=user_id,
        user_role=role,
        user_display_name=_user_display_name(app_state, user_id),
        workspace_id=workspace_id,
        environment=env,
        tier=_detect_tier(app_state),
        page=page_context.page or "unknown",
        page_summary=page_context.to_compact_summary(),
        visible_count=len(page_context.visible_ids or ()),
        selected_count=len(page_context.selected_ids or ()),
        filters_active=len(page_context.filters or {}),
        allowed_tool_tiers=tuple(tiers),
        can_approve=_can_approve(role),
        can_deploy_prod=_can_deploy_prod(role, env),
        workspace_counts=_workspace_counts(app_state, workspace_id),
        page_items_block=page_context.to_items_block(limit=25),
        connections=_user_connections(app_state, workspace_id),
        node_catalog=_node_catalog(),
    )


# ─────────────────────────────────────────────────────────────────────
# Renderer — turns a SessionSnapshot into a Markdown block
# ─────────────────────────────────────────────────────────────────────


def render_block(snap: SessionSnapshot) -> str:
    """Render the Layer-1 context as a stable Markdown block.

    Output stays ~400-600 tokens. Stable shape so prompt-cache hits stay
    high across turns — only the dynamic counts and page summary vary.
    Renders the same way on free/plus so the LLM sees clear capability
    boundaries.
    """
    lines: list[str] = []

    # ── Product identity (static) ─────────────────────────────────
    lines.append("## About the product")
    lines.append(_PRODUCT_IDENTITY)
    lines.append("")

    # ── Active session ───────────────────────────────────────────
    lines.append("## Active session")
    name = snap.user_display_name
    role = snap.user_role
    tier_label = "F-Pulse+" if snap.tier == "plus" else "F-Pulse OSS Free"
    lines.append(f"- **User:** {name} (role: {role})")
    lines.append(f"- **Workspace:** {snap.workspace_id}")
    lines.append(f"- **Environment:** {snap.environment.upper()}")
    lines.append(f"- **Edition:** {tier_label}")
    lines.append(f"- **Page:** {snap.page}")

    # Live counts so the LLM knows scale before deciding to call tools.
    c = snap.workspace_counts
    if any(c.values()):
        bits = []
        for label, key in [
            ("pipelines", "pipelines"),
            ("projects", "projects"),
            ("schedules", "schedules"),
            ("alerts", "alerts"),
            ("connections", "connections"),
        ]:
            n = int(c.get(key, 0) or 0)
            if n > 0:
                bits.append(f"{n} {label}")
        if bits:
            lines.append(f"- **Workspace state:** {', '.join(bits)}")
    else:
        lines.append("- **Workspace state:** empty (no pipelines/projects yet)")

    if snap.visible_count or snap.selected_count or snap.filters_active:
        lines.append(
            f"- **Page state:** {snap.visible_count} visible item(s), "
            f"{snap.selected_count} selected, {snap.filters_active} filter(s) active"
        )
    lines.append("")

    # ── What's on screen (Layer-1 page-state injection) ──────────
    # When the frontend sends visible_items, render them inline so the
    # LLM can answer "which connections are broken?" / "what failed
    # today?" without a discovery tool call.
    if snap.page_items_block:
        lines.append("## What's on screen right now")
        lines.append(snap.page_items_block)
        lines.append("")

    # ── Your data connections (CP-P2 grounding) ──────────────────
    # The user's REAL connections, so "lookup from my SQL Server table"
    # gets answered against the actual connection instead of an invented
    # one. NEVER reference a connection not in this list.
    lines.append("## Your data connections")
    if snap.connections:
        lines.append("Saved connections you can reference (name — type):")
        for name, ctype in snap.connections:
            lines.append(f"- {name} — {ctype}")
        lines.append(
            "Use these exact names. Do NOT invent connections that aren't listed here."
        )
    else:
        lines.append(
            "No saved connections yet — the user adds one on the Connections page "
            "(+ Add Connection). Don't invent connection names."
        )
    lines.append("")

    # ── Permissions ─────────────────────────────────────────────
    lines.append("## What this user can do (right now)")
    tiers_str = ", ".join(snap.allowed_tool_tiers) or "none"
    lines.append(f"- Tool tiers allowed in this env: **{tiers_str}**")
    lines.append(f"- Can approve PROD changes: **{'yes' if snap.can_approve else 'no'}**")
    lines.append(f"- Can deploy to PROD: **{'yes' if snap.can_deploy_prod else 'no'}**")
    if snap.environment == "dev":
        lines.append("- DEV is a non-production sandbox. Iterate freely; promote to PROD when ready.")
    else:
        lines.append("- PROD is the production environment. Default to read-only unless explicitly authorised.")
    lines.append("")

    # ── Node palette (CP-P2 grounding) ───────────────────────────
    # The ACTUAL registered node types, grouped by category. Replaces the
    # old hardcoded "37 node types" line so the LLM uses real, current
    # names and never cites a stale count.
    if snap.node_catalog:
        lines.append("## Node types available (live palette)")
        lines.append(
            "These are the actual node types registered in this install — use "
            "these exact names when suggesting how to build something. Don't "
            "invent node types or quote a fixed count."
        )
        _cat_order = [
            "source", "transform", "combine", "control",
            "action", "ai", "destination", "other",
        ]

        def _cat_rank(c: str) -> tuple[int, str]:
            return (_cat_order.index(c) if c in _cat_order else len(_cat_order), c)

        for cat in sorted(snap.node_catalog.keys(), key=_cat_rank):
            labels = list(snap.node_catalog[cat])
            shown = labels[:14]
            more = len(labels) - len(shown)
            suffix = f", +{more} more" if more > 0 else ""
            lines.append(f"- **{cat}:** {', '.join(shown)}{suffix}")
        lines.append("")

    # ── Edition boundary ─────────────────────────────────────────
    lines.append("## Edition boundary")
    if snap.tier == "free":
        lines.append("This install is **F-Pulse OSS Free**. Available now:")
        for item in _OSS_FREE_INCLUDES:
            lines.append(f"- {item}")
        lines.append("")
        lines.append("Not available in OSS Free (these are F-Pulse+ features — DON'T offer them):")
        for item in _OSS_FREE_EXCLUDES:
            lines.append(f"- {item}")
    else:
        lines.append("This install is **F-Pulse+** (commercial edition). All OSS features plus:")
        for item in _OSS_FREE_EXCLUDES:
            lines.append(f"- {item}")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────


def build_session_block(
    page_context: PageContext,
    app_state: dict[str, Any] | None = None,
    *,
    allowed_tool_tiers: tuple[str, ...] | None = None,
) -> str:
    """Build the full Layer-1 Markdown block for one chat turn.

    Convenience wrapper around build_snapshot + render_block. The agent's
    system-prompt builder calls this and inserts the result above the
    user's first message.
    """
    snap = build_snapshot(
        page_context, app_state, allowed_tool_tiers=allowed_tool_tiers,
    )
    return render_block(snap)


# ─────────────────────────────────────────────────────────────────────
# Inline-helper preamble — compact Layer-1 + optional Layer-2 RAG
# ─────────────────────────────────────────────────────────────────────


async def build_inline_context_preamble(
    *,
    user_id: str | None,
    workspace_id: str | None,
    query: str | None,
    app_state: dict[str, Any] | None = None,
    include_product_knowledge: bool = True,
    max_facts: int = 2,
) -> str:
    """Compact context preamble for inline AI helpers (suggest-sql,
    diagnose-error, post-run-summary, pre-run-validate, etc.).

    Inline helpers don't carry a full PageContext. This builds a
    lightweight ~150-token block from `app_state` + query:

      - Tier line (Free vs Plus) so the LLM doesn't suggest Plus features
      - Product-identity sentence (one line) so the LLM doesn't drift
      - Optional 1-2 product-knowledge chunks retrieved from the RAG store
        if the query has strong matches — stays empty otherwise

    Returns an empty string on any failure — inline helpers must not
    break when context is unavailable. Prepend the result to the
    helper's system_prompt (newline-separated).
    """
    parts: list[str] = []

    # Tier line — derived from app_state license_manager.
    tier_line = "Edition: F-Pulse OSS Free"
    try:
        if app_state and app_state.get("license_manager"):
            mgr = app_state["license_manager"]
            if getattr(mgr, "is_plus", False):
                tier_line = "Edition: F-Pulse+ (Plus license active)"
    except Exception:
        pass
    parts.append(tier_line)

    # Identity blurb — one sentence, prevents the LLM mis-describing the product.
    parts.append(
        "F-Pulse is a self-hosted single-tenant data workflow platform with "
        "deterministic execution and AI assistance; no data leaves the host by default."
    )

    # Product knowledge RAG — only when we have a query and embeddings.
    if include_product_knowledge and query and app_state:
        try:
            embedder = app_state.get("rag_embedder")
            store = app_state.get("rag_store")
            if embedder is not None and store is not None:
                from fpulse.ai.product_knowledge import retrieve_product_facts
                chunks = await retrieve_product_facts(
                    query=query,
                    embedder=embedder,
                    vector_store=store,
                    limit=max_facts,
                )
                if chunks:
                    parts.append("\nRelevant F-Pulse product facts:")
                    for c in chunks[:max_facts]:
                        # Each chunk is short — keep first 240 chars to bound
                        # the preamble at ~600 tokens worst case.
                        # retrieve_product_facts returns chunks keyed
                        # "content" (not "text") — reading only "text" here
                        # silently dropped every product-knowledge chunk
                        # from the preamble. Accept both, content first.
                        text = (
                            (c.get("content") or c.get("text")) if isinstance(c, dict)
                            else (getattr(c, "content", "") or getattr(c, "text", ""))
                        )
                        if text:
                            parts.append(f"- {str(text).strip()[:240]}")
        except Exception as exc:  # noqa: BLE001 — never break inline helpers
            logger.debug("inline preamble RAG retrieval failed: %s", exc)

    return "\n".join(parts)
