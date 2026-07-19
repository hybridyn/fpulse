"""F-Pulse Steward — storage intelligence (2026-06-18).

Watches the managed-table datastore for waste. The first (and most
defensible) signal: an **orphaned managed table** — one that exists on disk
but that NO pipeline reads or writes (no Managed Table Source or Managed
Table Sink references it). Those are almost always leftovers from a deleted or
edited pipeline, so the false-positive rate is low: a table written by a
current sink, or read by a current source, is referenced and never flagged.

State detector (re-derived from current tables + workflows on every scan,
like the duplicate-source Archeologist), so there is no journal — the read
side IS the detection. Pure function over (workflows, tables); the API layer
feeds it the live DataStore listing.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from .models import (
    FindingKind,
    FindingLevel,
    FindingSeverity,
    FindingStatus,
    StewardFinding,
)

_LOCAL_TABLE_TYPES = {"local_table_source", "local_table_sink"}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _node_type(node: dict[str, Any]) -> str:
    return str(node.get("type") or node.get("step_type") or "").strip().lower()


def _node_params(node: dict[str, Any]) -> dict[str, Any]:
    # Workflows reach the scan as normalized dicts; params live under a few
    # possible keys depending on whether the row came from the IR or a graph.
    p = node.get("params")
    if isinstance(p, dict):
        return p
    cfg = node.get("config")
    if isinstance(cfg, dict):
        return cfg
    data = node.get("data")
    if isinstance(data, dict):
        inner = data.get("params")
        return inner if isinstance(inner, dict) else data
    return {}


def detect_orphaned_tables(
    workflows: list[dict[str, Any]],
    tables: list[Any],
    *,
    workspace_id: str = "default",
    suppressed_signatures: set[str] | None = None,
) -> list[StewardFinding]:
    """Flag managed tables that no pipeline references (read or write)."""
    suppressed = suppressed_signatures or set()

    referenced: set[str] = set()
    for wf in workflows or []:
        for node in (wf.get("nodes") or []):
            if not isinstance(node, dict):
                continue
            if _node_type(node) in _LOCAL_TABLE_TYPES:
                p = _node_params(node)
                schema = str(p.get("schema_name") or "default").strip().lower()
                name = str(p.get("table_name") or "").strip().lower()
                if name:
                    referenced.add(f"{schema}.{name}")

    findings: list[StewardFinding] = []
    for t in tables or []:
        schema_raw = getattr(t, "schema_name", None) or "default"
        name_raw = getattr(t, "name", None) or ""
        name = str(name_raw).strip().lower()
        if not name:
            continue
        key = f"{str(schema_raw).strip().lower()}.{name}"
        if key in referenced:
            continue
        sig = f"orphan-table::{key}"
        if sig in suppressed:
            continue
        disp = f"{schema_raw}.{name_raw}"
        fid = "orphan-" + hashlib.sha256(sig.encode("utf-8")).hexdigest()[:16]
        findings.append(StewardFinding(
            id=fid,
            workspace_id=workspace_id,
            kind=FindingKind.ORPHANED_TABLE,
            level=FindingLevel.ARCHITECTURE,
            severity=FindingSeverity.P3,
            status=FindingStatus.OPEN,
            title=f"Managed table {disp} is not used by any pipeline",
            body=(
                f"The managed table **{disp}** exists in storage but no pipeline reads "
                f"or writes it — no Managed Table Source or Managed Table Sink references it. "
                f"It is most likely left over from a deleted or edited pipeline.\n\n"
                f"Review it on the Storage page: keep it if it is an intentional deliverable, "
                f"or delete it to reclaim space. Dismiss to acknowledge it as intentional."
            ),
            evidence={
                "source_signature": sig,
                "schema": schema_raw,
                "table": name_raw,
                "row_count": getattr(t, "row_count", None),
            },
            proposed_actions=[{
                "label": "Dismiss (intentional table)",
                "action": "suppress_finding",
                "params": {"finding_id": fid, "scope": "signature"},
            }],
            first_seen=_iso_now(),
            last_seen=_iso_now(),
            occurrences=1,
            confidence="high",
            confidence_score=1.0,
            evidence_count=1,
            baseline_window="current_state",
        ))
    return findings


__all__ = ["detect_orphaned_tables"]
