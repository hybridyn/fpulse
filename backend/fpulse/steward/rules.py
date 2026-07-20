"""F-Pulse Steward - user-defined rules engine (YAML DSL).

The OSS foundation for what becomes a Plus authoring experience. Admins
write rules as YAML files under ``<data_dir>/steward/<workspace>/rules/``;
this module discovers, parses, and evaluates them against the workspace's
pipeline graph. Matches become regular ``StewardFinding`` records and
flow through the existing surface (lesson store, notification de-dup,
UI render) - no new downstream wiring needed.

# Why YAML, not Python plugins (architectural decision, 2026-06-07)

Several plugin shapes were considered and rejected:

  * **Python plugins**  - too dangerous. Admin code = arbitrary execution
    inside the backend process; can shell out, exfiltrate creds, crash
    the server. Sandboxing Python well is a research project. Enterprise
    procurement rejects this on day one.
  * **WASM / Rhai / embedded scripting** - safer than Python, more
    expressive than YAML - but adds a heavy runtime dependency in a
    language nobody uses elsewhere in F-Pulse. Wrong cost/benefit for
    the audience.
  * **UI-only rule builder** - looks friendly until you need to GitOps
    it, code-review it, or diff it across environments. Falls apart
    for serious enterprise use.

YAML wins because: (a) it's data, not code - no execution path, no
shell escape; (b) GitOps-native - rules live in version control,
reviewed via PRs, promoted staging->prod like any other config;
(c) reviewable by non-engineers - a team lead can read a YAML rule
and understand it; (d) reuses the existing StewardFinding contract -
no new downstream surface to build; (e) industry precedent works
(Prometheus alerts, Datadog monitors, Soda Core, dbt tests - all
YAML-first DSLs in this same shape).

The SQL escape hatch (for analytical rules that need to compare across
workflow runs) will land as a Plus-only feature later, sandboxed to a
read-only DuckDB view over pipeline metadata.

# What a rule looks like

A YAML file describing one finding pattern. Example:

    id: writes_to_prod_without_dev_counterpart
    title: "Pipeline writes to prod with no dev counterpart"
    description: |
        Each production pipeline should have a dev-environment equivalent
        so changes can be verified before they hit production data.
    level: governance
    severity: p2
    confidence: high
    enabled: true
    match:
        has_node:
            type: db_sink
            params_eq:
                environment: prod
        lacks_node:
            type: db_sink
            params_eq:
                environment: dev
    recommend:
        - "Create a dev counterpart pipeline before this lands"
        - "Or tag the pipeline `dev_required: false` to suppress"

# How a rule matches

For each workflow in the workspace:
  * Every condition in ``match`` must hold (logical AND).
  * ``has_node`` matches if AT LEAST ONE node in the workflow satisfies
    every node-level constraint (type, params_eq, params_in,
    params_contains).
  * ``lacks_node`` matches if NO node satisfies the constraint -
    absence detection.
  * Each (rule, workflow) match emits one finding. The finding ID is
    deterministic across scans so re-running doesn't create duplicates.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml
from pydantic import BaseModel, Field, field_validator

from .models import (
    FindingKind,
    FindingLevel,
    FindingSeverity,
    FindingStatus,
    StewardFinding,
)


# Rule IDs must be filesystem-safe and stable across syncs so the
# emitted finding IDs stay deterministic. Keep the pattern conservative.
_RULE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _step_type_and_params(node: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Mirror of the same helper in archeologist.py - support BOTH the
    F-Pulse step format (``{id, type, params}`` top-level) and the
    React Flow node format (``{id, data: {stepType, params}}``).

    Duplicated here on purpose: keeps rules.py self-contained so it
    doesn't depend on the Archeologist module's internals."""
    top_type = node.get("type") or node.get("stepType") or node.get("step_type") or ""
    if top_type:
        return str(top_type), (node.get("params") or {})
    data = node.get("data") or {}
    rf_type = data.get("stepType") or data.get("step_type") or ""
    return str(rf_type), (data.get("params") or {})


# ── DSL types ─────────────────────────────────────────────────────────


class NodeMatch(BaseModel):
    """Constraints on a single node. All present fields are AND'd; an
    empty NodeMatch matches every node.

    Field semantics:
      * ``type``           - exact string match on the node's step type
      * ``type_in``        - step type must be in the list
      * ``type_endswith``  - step type ends with this suffix
                             (convenience for ``"_source"`` / ``"_sink"``)
      * ``params_eq``      - for every (k, v): str(params[k]) == str(v)
      * ``params_in``      - for every (k, [v1, v2, ...]): params[k] in list
      * ``params_contains``- for every (k, substr): substr in str(params[k])
    """

    type: str | None = None
    type_in: list[str] | None = None
    type_endswith: str | None = None
    params_eq: dict[str, Any] | None = None
    params_in: dict[str, list[Any]] | None = None
    params_contains: dict[str, str] | None = None

    def matches(self, node: dict[str, Any]) -> bool:
        step_type, params = _step_type_and_params(node)
        if self.type is not None and step_type != self.type:
            return False
        if self.type_in is not None and step_type not in self.type_in:
            return False
        if self.type_endswith is not None and not step_type.endswith(self.type_endswith):
            return False
        if self.params_eq:
            for k, v in self.params_eq.items():
                if str(params.get(k)) != str(v):
                    return False
        if self.params_in:
            for k, choices in self.params_in.items():
                if params.get(k) not in choices:
                    return False
        if self.params_contains:
            for k, needle in self.params_contains.items():
                if str(needle) not in str(params.get(k, "")):
                    return False
        return True


class WorkflowMatch(BaseModel):
    """Constraints on a single workflow. All present fields are AND'd;
    an empty WorkflowMatch matches every workflow.

    Field semantics:
      * ``name_contains``   - substring of workflow name (case-insensitive)
      * ``has_node``        - at LEAST one node satisfies the NodeMatch
      * ``lacks_node``      - NO node satisfies the NodeMatch (absence)
      * ``node_count_min``  - workflow has at least N nodes
      * ``node_count_max``  - workflow has at most N nodes
    """

    name_contains: str | None = None
    has_node: NodeMatch | None = None
    lacks_node: NodeMatch | None = None
    node_count_min: int | None = None
    node_count_max: int | None = None

    def matches(self, workflow: dict[str, Any]) -> bool:
        if self.name_contains is not None:
            name = str(workflow.get("name") or "").lower()
            if self.name_contains.lower() not in name:
                return False
        nodes = workflow.get("nodes") or []
        if self.node_count_min is not None and len(nodes) < self.node_count_min:
            return False
        if self.node_count_max is not None and len(nodes) > self.node_count_max:
            return False
        if self.has_node is not None:
            if not any(self.has_node.matches(n) for n in nodes if isinstance(n, dict)):
                return False
        if self.lacks_node is not None:
            if any(self.lacks_node.matches(n) for n in nodes if isinstance(n, dict)):
                return False
        return True


class UserRule(BaseModel):
    """One YAML rule. The id is the stable filesystem-friendly handle;
    the emitted finding's id is derived from (rule.id, workflow.id) so
    repeat scans produce identical finding ids (de-dup-friendly).

    `level` and `severity` are picked by the rule author from the
    existing Steward taxonomy - user rules can live at any of the 7
    observability levels without inventing new ones.
    """

    id: str
    title: str
    description: str = ""
    level: FindingLevel = FindingLevel.PIPELINE
    severity: FindingSeverity = FindingSeverity.P2
    confidence: str = "medium"  # "high" | "medium" | "low"
    enabled: bool = True
    match: WorkflowMatch
    recommend: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _id_pattern(cls, v: str) -> str:
        if not _RULE_ID_PATTERN.match(v):
            raise ValueError(
                f"rule id {v!r} must match {_RULE_ID_PATTERN.pattern} "
                "(lowercase alphanumeric / underscore / hyphen, "
                "starting alphanumeric, max 63 chars)"
            )
        return v

    @field_validator("confidence")
    @classmethod
    def _confidence_value(cls, v: str) -> str:
        if v not in ("high", "medium", "low"):
            raise ValueError(f"confidence must be high|medium|low, got {v!r}")
        return v


class RuleLoadError(BaseModel):
    """One YAML file that failed to load - surfaced to the UI so admins
    see WHY their rule isn't taking effect (rather than the rule just
    silently being skipped)."""

    path: str
    message: str


# ── Loader ────────────────────────────────────────────────────────────


def load_rules(rules_dir: Path) -> tuple[list[UserRule], list[RuleLoadError]]:
    """Load every ``*.yaml`` / ``*.yml`` file in ``rules_dir``.

    Each file is parsed independently - a malformed rule produces a
    RuleLoadError that gets returned alongside the valid rules. This
    means one bad rule never silences all the others (which would be
    a nasty failure mode at 3 AM when someone's `git push` lands a
    typo).

    Missing directory is fine - returns empty lists. Errors include
    YAML parse failures, schema-validation errors, and duplicate ids
    within the directory.
    """
    rules: list[UserRule] = []
    errors: list[RuleLoadError] = []
    if not rules_dir.exists() or not rules_dir.is_dir():
        return rules, errors

    seen_ids: set[str] = set()
    paths = sorted([*rules_dir.glob("*.yaml"), *rules_dir.glob("*.yml")])
    for path in paths:
        try:
            with path.open("r", encoding="utf-8") as fp:
                raw = yaml.safe_load(fp)
            if raw is None:
                # Empty file - skip silently (mirrors how empty YAML
                # is treated everywhere else in the project).
                continue
            if not isinstance(raw, dict):
                raise ValueError(
                    f"expected a YAML mapping at top level, got {type(raw).__name__}"
                )
            rule = UserRule.model_validate(raw)
            if rule.id in seen_ids:
                raise ValueError(f"duplicate rule id {rule.id!r} (already loaded from another file)")
            seen_ids.add(rule.id)
            rules.append(rule)
        except yaml.YAMLError as e:
            errors.append(RuleLoadError(path=str(path), message=f"YAML parse error: {e}"))
        except Exception as e:
            errors.append(RuleLoadError(path=str(path), message=str(e)))
    return rules, errors


# ── Evaluator ─────────────────────────────────────────────────────────


def _finding_id_for(rule_id: str, workflow_id: str) -> str:
    """Deterministic - same (rule, workflow) pair across scans produces
    the same finding id, so existing memory-layer occurrence counters,
    notification de-dup keys, and dismissal-by-id all keep working."""
    h = hashlib.sha256(f"{rule_id}::{workflow_id}".encode("utf-8")).hexdigest()[:16]
    return f"usr-{h}"


def evaluate_rules(
    workflows: list[dict[str, Any]],
    rules: Iterable[UserRule],
    *,
    workspace_id: str = "default",
) -> list[StewardFinding]:
    """Run every enabled rule against every workflow.

    Returns a list of ``StewardFinding`` records - one per (rule, workflow)
    that matches. Order is deterministic (sorted by rule id, then by
    workflow id) so scan output is reproducible.

    The body of each finding is auto-rendered from the rule's
    ``description`` plus its ``recommend`` bullets - admins control
    what users see end-to-end.
    """
    out: list[StewardFinding] = []
    now = _iso_now()
    rules_sorted = sorted(rules, key=lambda r: r.id)
    workflows_sorted = sorted(
        (w for w in workflows if isinstance(w, dict)),
        key=lambda w: str(w.get("id") or ""),
    )

    for rule in rules_sorted:
        if not rule.enabled:
            continue
        for wf in workflows_sorted:
            if not rule.match.matches(wf):
                continue
            wf_id = str(wf.get("id") or "")
            wf_name = str(wf.get("name") or wf_id or "untitled")
            body = (rule.description or "").strip()
            if rule.recommend:
                if body:
                    body += "\n\n"
                body += "**Recommended actions:**\n" + "\n".join(
                    f"- {line}" for line in rule.recommend
                )
            out.append(
                StewardFinding(
                    id=_finding_id_for(rule.id, wf_id),
                    workspace_id=workspace_id,
                    kind=FindingKind.USER_DEFINED,
                    # Rule author picks the level explicitly - this is
                    # the whole reason we bypass KIND_TO_LEVEL for user
                    # rules. A user-defined rule can be GOVERNANCE,
                    # COST, ARCHITECTURE, etc.
                    level=rule.level,
                    severity=rule.severity,
                    status=FindingStatus.OPEN,
                    title=rule.title,
                    body=body,
                    evidence={
                        "rule_id": rule.id,
                        "rule_source": "user_defined",
                        "workflow_id": wf_id,
                        "workflow_name": wf_name,
                        # Source signature - lets dismiss/suppression
                        # treat each (rule, workflow) pair as its own
                        # silenceable unit (rule applies to N workflows;
                        # user may want to silence just one of them).
                        "source_signature": f"user_rule:{rule.id}:{wf_id}",
                    },
                    proposed_actions=[
                        {
                            "label": "Dismiss (intentional)",
                            "action": "suppress_finding",
                            "params": {
                                "finding_id": _finding_id_for(rule.id, wf_id),
                                "scope": "signature",
                            },
                        },
                    ],
                    first_seen=now,
                    last_seen=now,
                    occurrences=1,
                    confidence=rule.confidence,
                    confidence_score={"high": 1.0, "medium": 0.7, "low": 0.4}[rule.confidence],
                    evidence_count=1,
                    baseline_window="rule_match",
                )
            )
    return out
