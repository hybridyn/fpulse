"""
Agent tool registry.

Single source of truth for which tools the agent can call. Per
docs/ai-boundary-contract.md §2 the registry is the normative list of
tools — adding a new tool requires updating the boundary contract too.

Step 1.5a: register the four initial read-only tools (round-3 reviewer
recommendation: "start with 4 read-only tools; add write tools only after
Step 1.5b governance is complete").

The registry is a per-process singleton via module-level _DEFAULT. Tests
construct their own ToolRegistry instances for isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from fpulse.ai.tools.base import ToolDefinition, ToolTier


class ToolNotFoundError(KeyError):
    """Raised when an agent tries to invoke an unregistered tool."""


@dataclass
class ToolRegistry:
    """Holds registered tools. Thread-unsafe; intended for single-process use."""

    tools: dict[str, ToolDefinition] = field(default_factory=dict)

    def register(self, tool: ToolDefinition) -> None:
        """Insert or replace a tool. Idempotent (re-registration replaces)."""
        self.tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition:
        """Return tool by name. Raises ToolNotFoundError if missing."""
        try:
            return self.tools[name]
        except KeyError as e:
            raise ToolNotFoundError(f"No tool registered with name {name!r}") from e

    def list_all(self) -> list[ToolDefinition]:
        return list(self.tools.values())

    def list_by_tier(self, tier: ToolTier) -> list[ToolDefinition]:
        return [t for t in self.tools.values() if t.tier == tier]

    def filter_by_tiers(self, allowed_tiers: Iterable[ToolTier]) -> list[ToolDefinition]:
        """Return only the tools whose tier is in `allowed_tiers`.

        Used by the agent loop to enforce workspace tool_tier_max policy
        (e.g. "this workspace allows read-only tools only").
        """
        allowed = set(allowed_tiers)
        return [t for t in self.tools.values() if t.tier in allowed]

    def __len__(self) -> int:
        return len(self.tools)

    def __contains__(self, name: str) -> bool:
        return name in self.tools


_DEFAULT_REGISTRY: ToolRegistry | None = None


def default_registry() -> ToolRegistry:
    """Return the per-process default registry. Lazy — first call creates it.

    Tests should NOT use this; they construct their own ToolRegistry. The
    default registry is for production wiring where the agent endpoint reads
    from a single canonical source.
    """
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = ToolRegistry()
    return _DEFAULT_REGISTRY


def reset_default_registry() -> None:
    """Test helper. Forces the next default_registry() call to construct fresh."""
    global _DEFAULT_REGISTRY
    _DEFAULT_REGISTRY = None
