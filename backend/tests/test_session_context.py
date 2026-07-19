"""Unit tests for the chat session-context block — Layer 1.

These tests verify the rendered Markdown stays stable + accurate.
The block is injected into the agent's system prompt every turn, so
shape regressions are user-visible.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from fpulse.ai.context import PageContext
from fpulse.ai.session_context import (
    SessionSnapshot,
    build_session_block,
    build_snapshot,
    render_block,
)


def _page(role="developer", env="dev", **kwargs):
    return PageContext(
        page=kwargs.get("page", "pipelines"),
        user_id=kwargs.get("user_id", "u-test"),
        tenant_id=kwargs.get("tenant_id", "default"),
        workspace_id=kwargs.get("workspace_id", "default"),
        environment=env,
        role=role,
        visible_ids=tuple(kwargs.get("visible_ids", ())),
        selected_ids=tuple(kwargs.get("selected_ids", ())),
        filters=kwargs.get("filters", {}),
    )


# ── Snapshot factory ────────────────────────────────────────────────


class TestBuildSnapshot:
    def test_no_app_state_uses_defaults(self):
        snap = build_snapshot(_page())
        assert snap.user_id == "u-test"
        assert snap.user_role == "developer"
        assert snap.environment == "dev"
        # No license manager in app_state → "free".
        assert snap.tier == "free"
        assert snap.workspace_id == "default"

    def test_app_state_with_plus_license(self):
        app_state = {"license_manager": SimpleNamespace(is_plus=True)}
        snap = build_snapshot(_page(), app_state)
        assert snap.tier == "plus"

    def test_app_state_with_free_license(self):
        app_state = {"license_manager": SimpleNamespace(is_plus=False)}
        snap = build_snapshot(_page(), app_state)
        assert snap.tier == "free"

    def test_user_display_name_falls_back_to_id(self):
        snap = build_snapshot(_page(user_id="u-alice"))
        assert snap.user_display_name == "u-alice"

    def test_workspace_counts_zero_on_no_state(self):
        snap = build_snapshot(_page())
        assert all(v == 0 for v in snap.workspace_counts.values())

    def test_can_approve_admin(self):
        snap = build_snapshot(_page(role="admin"))
        assert snap.can_approve is True

    def test_can_approve_developer(self):
        snap = build_snapshot(_page(role="developer"))
        assert snap.can_approve is False

    def test_can_deploy_prod_admin_anywhere(self):
        snap_prod = build_snapshot(_page(role="admin", env="prod"))
        snap_dev = build_snapshot(_page(role="admin", env="dev"))
        assert snap_prod.can_deploy_prod is True
        assert snap_dev.can_deploy_prod is True

    def test_can_deploy_prod_developer_dev_only(self):
        snap_dev = build_snapshot(_page(role="developer", env="dev"))
        snap_prod = build_snapshot(_page(role="developer", env="prod"))
        assert snap_dev.can_deploy_prod is True
        assert snap_prod.can_deploy_prod is False


# ── CP-P2 grounding (connections + live node palette) ───────────────


class TestCPP2Grounding:
    """CP-P2 (2026-06-16) — the always-on block must inject the user's REAL
    connections and the LIVE node palette, so the agent stops hallucinating
    connections and stops citing the stale hardcoded "37 node types" count."""

    @staticmethod
    def _store(conns):
        return SimpleNamespace(list_all=lambda workspace_id=None: conns)

    def test_connections_rendered_with_name_and_type(self):
        conns = [
            SimpleNamespace(name="Orders DB", type="postgresql", id="c1"),
            SimpleNamespace(name="Sales API", type="rest_api", id="c2"),
        ]
        block = build_session_block(_page(), {"connection_store": self._store(conns)})
        assert "## Your data connections" in block
        assert "Orders DB — postgresql" in block
        assert "Sales API — rest_api" in block

    def test_no_connections_says_none_not_invented(self):
        block = build_session_block(_page(), {"connection_store": self._store([])})
        assert "No saved connections yet" in block

    def test_connections_capped_at_20(self):
        conns = [SimpleNamespace(name=f"c{i}", type="postgresql", id=str(i)) for i in range(40)]
        snap = build_snapshot(_page(), {"connection_store": self._store(conns)})
        assert len(snap.connections) == 20

    def test_live_node_catalog_replaces_stale_count(self):
        # No app_state needed — the catalog reads the node registry directly.
        block = build_session_block(_page())
        assert "## Node types available (live palette)" in block
        # the old hardcoded "37 node types" line must be gone
        assert "37 node types" not in block

    def test_node_catalog_has_real_labels(self):
        snap = build_snapshot(_page())
        assert snap.node_catalog, "registry should produce node-type groups"
        labels = [lbl for group in snap.node_catalog.values() for lbl in group]
        assert len(labels) > 10  # a real palette, not a stub


# ── Renderer ────────────────────────────────────────────────────────


def _make_snap(**overrides) -> SessionSnapshot:
    base = dict(
        user_id="u-test",
        user_role="developer",
        user_display_name="Alice",
        workspace_id="default",
        environment="dev",
        tier="free",
        page="pipelines",
        page_summary="Page: pipelines | Role: developer | …",
        visible_count=0,
        selected_count=0,
        filters_active=0,
        allowed_tool_tiers=("read",),
        can_approve=False,
        can_deploy_prod=True,
        workspace_counts={"pipelines": 0, "projects": 0, "schedules": 0,
                          "alerts": 0, "connections": 0},
    )
    base.update(overrides)
    return SessionSnapshot(**base)


class TestRenderBlock:
    def test_block_contains_identity_section(self):
        out = render_block(_make_snap())
        assert "## About the product" in out
        assert "F-Pulse" in out
        assert "self-hosted" in out

    def test_block_contains_active_session_section(self):
        out = render_block(_make_snap())
        assert "## Active session" in out
        assert "Alice" in out
        assert "developer" in out
        assert "DEV" in out

    def test_block_marks_oss_free_tier(self):
        out = render_block(_make_snap(tier="free"))
        assert "OSS Free" in out
        # Must clearly list excluded Plus features so the LLM doesn't
        # accidentally promise SSO / approvals to a Free user.
        assert "SSO" in out
        assert "approval" in out.lower()

    def test_block_marks_plus_tier(self):
        out = render_block(_make_snap(tier="plus"))
        assert "F-Pulse+" in out
        # Plus block lists the unlocked features.
        assert "RBAC" in out

    def test_workspace_state_counts(self):
        snap = _make_snap(workspace_counts={
            "pipelines": 12, "projects": 3, "schedules": 5,
            "alerts": 1, "connections": 4,
        })
        out = render_block(snap)
        assert "12 pipelines" in out
        assert "3 projects" in out

    def test_empty_workspace_message(self):
        out = render_block(_make_snap())
        assert "empty" in out.lower() or "no pipelines" in out.lower()

    def test_permissions_section(self):
        snap = _make_snap(allowed_tool_tiers=("read", "safe_write"))
        out = render_block(snap)
        assert "## What this user can do" in out
        assert "read" in out
        assert "safe_write" in out

    def test_dev_advisory_in_dev(self):
        out = render_block(_make_snap(environment="dev"))
        assert "DEV is a non-production sandbox" in out

    def test_prod_advisory_in_prod(self):
        out = render_block(_make_snap(environment="prod"))
        assert "PROD is the production environment" in out

    def test_block_is_bounded_in_size(self):
        # ~600 tokens upper bound — char count proxy. If this fails the
        # block has grown too large and is eating into tool-result budget.
        out = render_block(_make_snap())
        assert len(out) < 4_000, f"block too large ({len(out)} chars)"


# ── End-to-end build_session_block ──────────────────────────────────


class TestBuildSessionBlock:
    def test_returns_non_empty(self):
        out = build_session_block(_page())
        assert out
        assert "F-Pulse" in out

    def test_overrides_tool_tiers(self):
        out = build_session_block(
            _page(role="developer", env="prod"),
            allowed_tool_tiers=("read",),
        )
        assert "read" in out
        # Did NOT auto-resolve writes from the role+env.
        assert "safe_write" not in out
