"""F-Pulse Steward - governance-level detectors (2026-06-07).

Activates two of the four contract-only governance FindingKinds:

  * ENV_CROSSING        - a single workflow uses connections tagged
                          with multiple environments (e.g. dev cred
                          referenced inside a prod-destined pipeline)
  * UNAPPROVED_DESTINATION - a workflow writes to a destination not
                          in the workspace's approved-destinations
                          allowlist (when one is configured)

The other two governance kinds (PII_LEAK, CREDENTIAL_SPRAWL) need a
real regex/credential catalog plus tuning to avoid noisy false
positives; deliberately deferred so the two cheaper ones can ship
clean tonight.

# Configuration

Per-workspace governance policy lives at
``<data_dir>/steward/<ws>/governance.json``:

  {
    "env_tags": {
      "conn-1234": "dev",
      "conn-5678": "prod",
      "conn-9012": "staging"
    },
    "approved_destinations": [
      "snowflake-prod-warehouse",
      "bigquery-analytics"
    ]
  }

Both maps are optional and independent:
  * No env_tags ⇒ no env_crossing findings
  * Empty approved_destinations ⇒ unapproved_destination is disabled
    (treated as "no allowlist enforced", not "everything is unapproved")

# Why state-derived, not event-driven

Unlike schema_drift / quality / connector_health (where the
interesting moment is when an external event arrives), governance
violations are visible from a workflow snapshot alone. So this
detector runs at every scan with the existing workflow set, same
shape as Archeologist.
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .models import (
    FindingKind,
    FindingLevel,
    FindingSeverity,
    FindingStatus,
    StewardFinding,
)


_FILE_LOCK = threading.Lock()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Policy ──────────────────────────────────────────────────────────


class GovernancePolicy(BaseModel):
    """Per-workspace governance configuration."""

    env_tags: dict[str, str] = Field(default_factory=dict)
    approved_destinations: list[str] = Field(default_factory=list)


class GovernancePolicyStore:
    """File-backed policy at ``<workspace>/governance.json``.

    Default policy (file missing) = empty maps = no findings emitted,
    which is the right "I haven't configured this yet" behaviour."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> GovernancePolicy:
        if not self.path.exists():
            return GovernancePolicy()
        try:
            return GovernancePolicy.model_validate_json(
                self.path.read_text(encoding="utf-8")
            )
        except Exception:
            return GovernancePolicy()

    def save(self, policy: GovernancePolicy) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        with _FILE_LOCK:
            with tmp.open("w", encoding="utf-8") as fp:
                json.dump(policy.model_dump(), fp, indent=2, ensure_ascii=False)
            tmp.replace(self.path)


# ── Helpers shared with archeologist for step shape ─────────────────


def _step_type_and_params(node: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Mirror of archeologist._step_type_and_params - supports BOTH
    F-Pulse top-level shape and React Flow nested shape. Kept inline
    so governance.py stays self-contained."""
    top_type = node.get("type") or node.get("stepType") or node.get("step_type") or ""
    if top_type:
        return str(top_type), (node.get("params") or {})
    data = node.get("data") or {}
    rf_type = data.get("stepType") or data.get("step_type") or ""
    return str(rf_type), (data.get("params") or {})


def _connection_id_from(params: dict[str, Any]) -> str:
    """Connections are referenced under several param names across
    different node types. Try the known ones in priority order."""
    for key in ("connection_id", "connection", "credential_id"):
        v = params.get(key)
        if v:
            return str(v)
    return ""


def _is_sink_step(step_type: str) -> bool:
    return step_type == "sink" or step_type.endswith("_sink")


# ── Signature builders ─────────────────────────────────────────────


def _signature(*parts: str) -> str:
    raw = "::".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ── Detector ───────────────────────────────────────────────────────


def detect_governance(
    workflows: list[dict[str, Any]],
    policy: GovernancePolicy,
    *,
    workspace_id: str = "default",
    suppressed_signatures: set[str] | None = None,
) -> list[StewardFinding]:
    """Run env_crossing + unapproved_destination checks.

    workflows: list of {id, name, nodes} dicts (post-normalisation by
    _workflows_for_scan).
    policy: GovernancePolicy from the per-workspace file.
    """
    suppressed = suppressed_signatures or set()
    findings: list[StewardFinding] = []
    now = _iso_now()

    env_tags = policy.env_tags or {}
    approved = set(policy.approved_destinations or [])

    if not env_tags and not approved:
        # No policy configured ⇒ nothing to detect.
        return findings

    for wf in workflows:
        if not isinstance(wf, dict):
            continue
        wf_id = str(wf.get("id") or "")
        wf_name = str(wf.get("name") or wf_id or "untitled")
        nodes = wf.get("nodes") or []

        # ── env_crossing ──
        if env_tags:
            envs_in_wf: dict[str, list[str]] = {}  # env -> connection ids
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                _, params = _step_type_and_params(node)
                conn_id = _connection_id_from(params)
                if not conn_id:
                    continue
                env = env_tags.get(conn_id)
                if env:
                    envs_in_wf.setdefault(env, []).append(conn_id)
            distinct_envs = set(envs_in_wf.keys())
            if len(distinct_envs) > 1:
                sig = _signature("env_crossing", workspace_id, wf_id, *sorted(distinct_envs))
                if sig not in suppressed:
                    findings.append(StewardFinding(
                        id=f"gov-env-{sig[:12]}",
                        workspace_id=workspace_id,
                        kind=FindingKind.ENV_CROSSING,
                        level=FindingLevel.GOVERNANCE,
                        severity=FindingSeverity.P1,  # mixing envs is high-severity
                        status=FindingStatus.OPEN,
                        title=f"Pipeline crosses environments ({' + '.join(sorted(distinct_envs))}): {wf_name}",
                        body=(
                            f"Workflow **{wf_name}** references connections tagged with **multiple "
                            f"environments**: {', '.join(sorted(distinct_envs))}.\n\n"
                            f"That's almost always a mistake — dev data flowing into a prod sink, "
                            f"or a prod credential being read from a dev pipeline. Both directions "
                            f"are dangerous: dev→prod corrupts production data; prod→dev exposes "
                            f"production secrets / data to looser environments.\n\n"
                            f"If this crossing is intentional (e.g. a one-time migration), dismiss "
                            f"the finding — the signature stays suppressed for future scans."
                        ),
                        evidence={
                            "workflow_id": wf_id,
                            "workflow_name": wf_name,
                            "envs": sorted(distinct_envs),
                            "connections_by_env": envs_in_wf,
                            "source_signature": sig,
                        },
                        proposed_actions=[
                            {
                                "label": "Dismiss (intentional cross-env reference)",
                                "action": "suppress_finding",
                                "params": {"finding_id": f"gov-env-{sig[:12]}", "scope": "signature"},
                            },
                        ],
                        first_seen=now, last_seen=now,
                        occurrences=1,
                        confidence="high",
                        confidence_score=1.0,
                        evidence_count=sum(len(v) for v in envs_in_wf.values()),
                        baseline_window="workflow_snapshot",
                    ))

        # ── unapproved_destination ──
        if approved:
            unapproved_in_wf: list[str] = []
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                step_type, params = _step_type_and_params(node)
                if not _is_sink_step(step_type):
                    continue
                conn_id = _connection_id_from(params)
                if conn_id and conn_id not in approved:
                    unapproved_in_wf.append(conn_id)
            if unapproved_in_wf:
                unique_unapproved = sorted(set(unapproved_in_wf))
                sig = _signature("unapproved_destination", workspace_id, wf_id, *unique_unapproved)
                if sig not in suppressed:
                    findings.append(StewardFinding(
                        id=f"gov-dst-{sig[:12]}",
                        workspace_id=workspace_id,
                        kind=FindingKind.UNAPPROVED_DESTINATION,
                        level=FindingLevel.GOVERNANCE,
                        severity=FindingSeverity.P2,
                        status=FindingStatus.OPEN,
                        title=f"Pipeline writes to unapproved destination: {wf_name}",
                        body=(
                            f"Workflow **{wf_name}** writes to **{len(unique_unapproved)} "
                            f"destination(s) not on the workspace approved list**: "
                            f"{', '.join('`'+c+'`' for c in unique_unapproved)}.\n\n"
                            f"Either add these to `governance.json -> approved_destinations` if "
                            f"they're legitimate, or change the pipeline to point at an approved "
                            f"destination. Dismiss if this pipeline is an intentional exception."
                        ),
                        evidence={
                            "workflow_id": wf_id,
                            "workflow_name": wf_name,
                            "unapproved_connections": unique_unapproved,
                            "approved_destinations": sorted(approved),
                            "source_signature": sig,
                        },
                        proposed_actions=[
                            {
                                "label": "Dismiss (intentional exception)",
                                "action": "suppress_finding",
                                "params": {"finding_id": f"gov-dst-{sig[:12]}", "scope": "signature"},
                            },
                            {
                                "label": "Add to approved destinations",
                                "action": "extend_governance_policy",
                                "params": {"add_approved": unique_unapproved},
                            },
                        ],
                        first_seen=now, last_seen=now,
                        occurrences=1,
                        confidence="high",
                        confidence_score=1.0,
                        evidence_count=len(unique_unapproved),
                        baseline_window="workflow_snapshot",
                    ))

    return findings
