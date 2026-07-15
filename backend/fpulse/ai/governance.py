"""
Lightweight policy engine for the agent loop.

Per architecture: rule-based, NOT full OPA. Each rule is a pure function
that returns ALLOW / DENY + a short reason string. The engine evaluates
rules in order; first DENY wins.

Authorization vs Policy split (round-3 reviewer):
  rbac.authorize_tool_call(...)        — "user is allowed to attempt this"
  governance.evaluate_policy(...)      — "this action is allowed in this context right now"

Both must pass before a tool executes. Each populates a different field in
the replay-safe trace step:
  RBAC failure   → `outcome=policy_block`, policy_rules_fired=["rbac:..."]
  Policy failure → `outcome=policy_block`, policy_rules_fired=["policy:rule_name: reason"]

Default rules ship enabled. Workspaces (Plus tier) can register additional
rules via PolicyEngine.add_rule() — that hook lives here so OSS users see
the same surface even though they don't get workspace-scoped customisation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


class PolicyDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True)
class PolicyContext:
    """Inputs every rule receives. Frozen so rules can rely on immutability."""

    tool_name: str
    tool_tier: str  # "read" / "safe_write" / "high_impact_write"
    environment: str  # "dev" / "prod"
    user_role: str
    workspace_id: str | None = None
    user_id: str | None = None
    is_dry_run: bool = False
    has_approval: bool = False  # set by the endpoint when caller proves an approval claim


# Rule signature — kept simple; complex rules can close over external state.
PolicyRule = Callable[[PolicyContext], tuple[PolicyDecision, str]]


@dataclass
class PolicyEngine:
    """Holds an ordered list of rules. First DENY wins."""

    rules: list[tuple[str, PolicyRule]] = field(default_factory=list)

    def add_rule(self, name: str, rule: PolicyRule) -> None:
        """Append a rule. Re-adding a rule with the same name is allowed; both run."""
        self.rules.append((name, rule))

    def evaluate(self, ctx: PolicyContext) -> tuple[PolicyDecision, list[str]]:
        """Returns (final_decision, list_of_rules_that_fired_with_reasons).

        On ALLOW the second element is empty. On DENY it contains exactly
        one entry — the first denying rule, formatted as
        "policy:{rule_name}: {reason}". Subsequent rules are not run.
        """
        for name, rule in self.rules:
            decision, reason = rule(ctx)
            if decision == PolicyDecision.DENY:
                return (PolicyDecision.DENY, [f"policy:{name}: {reason}"])
        return (PolicyDecision.ALLOW, [])

    def __len__(self) -> int:
        return len(self.rules)


# ---------------------------------------------------------------------------
# Default rules — F-Pulse OSS baseline
# ---------------------------------------------------------------------------


def rule_no_prod_writes_without_approval(ctx: PolicyContext) -> tuple[PolicyDecision, str]:
    """PROD writes need either prior approval OR dry-run. Read tools always pass."""
    if ctx.tool_tier in ("safe_write", "high_impact_write"):
        if ctx.environment.lower() == "prod" and not (ctx.has_approval or ctx.is_dry_run):
            return (PolicyDecision.DENY, "PROD write requires approval or dry-run")
    return (PolicyDecision.ALLOW, "")


def rule_high_impact_requires_developer_or_above(ctx: PolicyContext) -> tuple[PolicyDecision, str]:
    """high_impact_write requires developer role or higher anywhere.

    Note: this is belt-and-suspenders with rbac.authorize_tool_call which
    already gates by role. Keeping it here means a misconfigured RBAC
    matrix (or future widening) doesn't accidentally let viewers fire
    destructive tools.
    """
    if ctx.tool_tier == "high_impact_write":
        if (ctx.user_role or "").lower() in ("viewer",):
            return (PolicyDecision.DENY, f"role={ctx.user_role!r} cannot invoke high_impact_write")
    return (PolicyDecision.ALLOW, "")


def rule_anonymous_blocked_for_writes(ctx: PolicyContext) -> tuple[PolicyDecision, str]:
    """No writes without an authenticated user_id. Read tools may run anonymously."""
    if ctx.tool_tier in ("safe_write", "high_impact_write"):
        if not ctx.user_id:
            return (PolicyDecision.DENY, "anonymous user cannot invoke write tools")
    return (PolicyDecision.ALLOW, "")


def default_engine() -> PolicyEngine:
    """Construct the OSS-default policy engine with the three baseline rules."""
    eng = PolicyEngine()
    eng.add_rule("no_prod_writes_without_approval", rule_no_prod_writes_without_approval)
    eng.add_rule("high_impact_requires_developer_or_above", rule_high_impact_requires_developer_or_above)
    eng.add_rule("anonymous_blocked_for_writes", rule_anonymous_blocked_for_writes)
    return eng


# Module-level singleton — agent loop reads via this. Tests construct their own.
_DEFAULT_ENGINE: PolicyEngine | None = None


def get_default_engine() -> PolicyEngine:
    global _DEFAULT_ENGINE
    if _DEFAULT_ENGINE is None:
        _DEFAULT_ENGINE = default_engine()
    return _DEFAULT_ENGINE


def reset_default_engine_for_tests() -> None:
    global _DEFAULT_ENGINE
    _DEFAULT_ENGINE = None
