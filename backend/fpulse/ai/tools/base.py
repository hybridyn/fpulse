"""
Tool definition primitives for the agent loop.

A tool is a callable that the AI agent can invoke during its tool-use loop.
Every tool declares:
  - name (unique identifier)
  - tier (read / safe_write / high_impact_write)
  - description (passed to LLM verbatim)
  - input_schema (JSON Schema dict — passed to LLM via tools[].input_schema)
  - output_schema (consumed by normalize.normalize_tool_output)
  - handler (async callable receiving validated inputs)
  - requires_idempotency_key (True for write tiers)

Tier semantics (per ai-boundary-contract.md + project_fpulse_ai_operational_architecture.md):
  - READ:               permissive RBAC, no confirmation, standard audit
  - SAFE_WRITE:         standard RBAC, inline preview, standard audit
  - HIGH_IMPACT_WRITE:  strict RBAC, required confirmation card, elevated audit, dry-run by default

Step 1.5a foundation. The Step 1.5b governance layer adds permission checks,
idempotency enforcement, dry-run wrapping, and policy evaluation around these.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable


class ToolTier(str, Enum):
    READ = "read"
    SAFE_WRITE = "safe_write"
    HIGH_IMPACT_WRITE = "high_impact_write"


# Handler signature: receives the validated inputs dict + a ToolContext, returns
# a result dict matching output_schema. Step 1.5b wraps this with governance.
ToolHandler = Callable[[dict[str, Any], "ToolContext"], Awaitable[Any]]


@dataclass(frozen=True)
class ToolContext:
    """Runtime context passed to every tool handler.

    Tools should never reach into globals — everything they need comes via
    ToolContext. Makes them trivially mockable in tests and prevents cross-
    request state leakage.
    """

    tenant_id: str
    user_id: str | None
    workspace_id: str | None
    environment: str  # "dev" / "prod"
    dry_run: bool = False  # True when agent is in dry-run mode
    # Page-context bleed-through — lets tools default to the user's current
    # selection without requiring the LLM to thread IDs explicitly. Empty
    # tuple when the user is on a page with no selectable items (e.g. settings).
    selected_ids: tuple[str, ...] = ()
    visible_ids: tuple[str, ...] = ()
    # Page key (e.g. "pipelines.list") + rich snapshot of on-screen entities.
    # Lets the fast-lane router and direct-action layer answer page-specific
    # questions without making a tool call to discover screen state.
    page: str = ""
    visible_items: tuple[dict[str, Any], ...] = ()


@dataclass
class ToolDefinition:
    """Declared shape of a tool. Registered with ToolRegistry."""

    name: str
    tier: ToolTier
    description: str
    input_schema: dict[str, Any]  # Anthropic-compatible JSON schema
    output_schema: Any            # Consumed by ai.normalize
    handler: ToolHandler
    requires_idempotency_key: bool = False
    # Tags for grouping in UI / analytics. Free-form.
    tags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Tool name must not be empty")
        if not isinstance(self.tier, ToolTier):
            raise TypeError("tier must be ToolTier enum, not raw string")
        if self.tier in (ToolTier.SAFE_WRITE, ToolTier.HIGH_IMPACT_WRITE):
            if not self.requires_idempotency_key:
                raise ValueError(
                    f"Tool {self.name!r}: write-tier tools MUST require idempotency_key"
                )

    def to_anthropic_schema(self) -> dict[str, Any]:
        """Render in the shape Anthropic's tool-use API expects.

        Reference: https://docs.anthropic.com/en/docs/build-with-claude/tool-use
        Shape: {name, description, input_schema}
        """
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }
