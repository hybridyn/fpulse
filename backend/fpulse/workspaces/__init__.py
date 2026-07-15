"""Workspace foundation — multi-tenant scoping layer for F-Pulse.

A Workspace is the unit of data isolation. Every project, pipeline,
schedule, alert, connection, variable, and credential belongs to
exactly one workspace, and queries are scoped to "workspaces this
user is a member of" by default.

The single-tenant install is preserved as a special case: every
existing F-Pulse install gets a "Default" workspace via the v2 schema
migration, every existing user is enrolled into it, and every
existing project gets tagged with workspace_id='default'. So users
who never create a second workspace experience zero behaviour change.

Files:
  models.py — Workspace + WorkspaceMember pydantic models
  store.py  — SQLite-backed CRUD for workspaces and memberships
"""
