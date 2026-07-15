"""
Page context model.

The frontend `usePageContext()` hook publishes this shape on every page.
The agent endpoint receives it on each request so the LLM knows what the
user is currently looking at without dumping the whole UI state.

Layered context model (per docs/ai-boundary-contract.md §1 and round-3
reviewer guidance):
  Tier 1 — base:    page name, role, environment           (always sent)
  Tier 2 — compact: visible IDs, selected IDs, filters     (sent when budget allows)
  Tier 3 — details: fetched on demand via tool calls       (never sent by default)

Tools resolve `selected_ids` / `visible_ids` against the underlying stores
when they need detail. The agent itself never sees raw rows; it sees
summary counts and IDs only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PageContext:
    """Snapshot of what the user is looking at when they invoke the agent.

    Frozen so tests + handlers can rely on immutability. Build a new
    PageContext for each request — never mutate.
    """

    page: str                                # e.g. "projects", "pipelines.detail"
    user_id: str
    tenant_id: str                           # workspace scope for cache keys + handles
    workspace_id: str | None = None
    environment: str = "dev"                 # "dev" / "prod"
    visible_ids: tuple[str, ...] = ()        # tuple for hashability
    selected_ids: tuple[str, ...] = ()
    filters: dict[str, Any] = field(default_factory=dict)
    role: str = "viewer"
    # Rich snapshot of currently-visible entities. Each item: {id, name?,
    # status?, kind?, meta?}. Lets the fast-lane router answer questions
    # like "which connections are broken?" without a discovery tool call.
    visible_items: tuple[dict[str, Any], ...] = ()
    # Page-supplied richer context (2026-05-22). Open-shape dict so a
    # page can attach anything its assistant could use: current
    # workflow IR on editor, selected node params, validation errors,
    # last-run summary, drift event, etc. Renders as a YAML-ish block
    # under "Page-specific context" in the system prompt. Sanitized
    # before reaching the LLM (per AI_BOUNDARY_CONTRACT §2) so secrets
    # and PII don't leak from page payloads.
    #
    # Keep entries small: each top-level key gets a few hundred tokens
    # of budget. For large payloads (the full IR of a 50-step pipeline,
    # 1000-row preview), publish a SUMMARY here and let the model fetch
    # detail via tools.
    extra_context: dict[str, Any] = field(default_factory=dict)
    # Rolling conversation memory (2026-05-22). Two layers:
    #   - ``recent_turns`` — verbatim last N user/assistant pairs the
    #     frontend persists. Each item: {"role": "user"|"assistant",
    #     "content": str}.
    #   - ``conversation_summary`` — free-form compressed summary of
    #     OLDER turns the frontend rolled out of the recent window.
    # Either or both may be empty. Renders as a "Conversation so far"
    # block ahead of the page-specific context so the LLM has continuity
    # before topical details. Sanitized at the API boundary.
    recent_turns: tuple[dict[str, str], ...] = ()
    conversation_summary: str = ""

    def to_compact_summary(self) -> str:
        """Render a short, deterministic summary string for the LLM.

        Goes into the Tier 2 budget section. Stable shape so the LLM sees
        a consistent template across requests.
        """
        status_breakdown = ""
        if self.visible_items:
            counts: dict[str, int] = {}
            for it in self.visible_items:
                s = it.get("status")
                if s:
                    counts[s] = counts.get(s, 0) + 1
            if counts:
                parts = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
                status_breakdown = f" | Statuses: {parts}"
        return (
            f"Page: {self.page} | "
            f"Role: {self.role} | "
            f"Env: {self.environment} | "
            f"Visible: {len(self.visible_ids)} item(s) | "
            f"Selected: {len(self.selected_ids)} item(s) | "
            f"Filters: {len(self.filters)} active"
            f"{status_breakdown}"
        )

    def to_items_block(self, limit: int = 25) -> str:
        """Render the visible items as a compact list for the system prompt.

        Used by the single-shot LLM mode and fast-lane router to answer
        page-specific questions without tool calls. Bounded to ``limit``
        rows (caller-controlled) to keep prompt cost predictable.
        """
        if not self.visible_items:
            return ""
        rows: list[str] = []
        for it in self.visible_items[:limit]:
            label = it.get("name") or it.get("id") or "?"
            kind = it.get("kind")
            status = it.get("status")
            bits = [f"- {label}"]
            if kind:
                bits.append(f"({kind})")
            if status:
                bits.append(f"[{status}]")
            meta = it.get("meta") or {}
            if meta:
                meta_str = ", ".join(f"{k}={v}" for k, v in list(meta.items())[:4])
                bits.append(f"{{{meta_str}}}")
            rows.append(" ".join(bits))
        more = len(self.visible_items) - limit
        if more > 0:
            rows.append(f"… (+{more} more)")
        return "\n".join(rows)

    def to_base(self) -> str:
        """Tier 1 — minimal mandatory context."""
        return f"User on page '{self.page}' in '{self.environment}' env, role={self.role}."

    def to_conversation_block(
        self,
        *,
        max_turns: int = 12,
        max_chars_per_turn: int = 800,
        max_summary_chars: int = 1200,
    ) -> str:
        """Render the rolling conversation as a prompt section.

        Emits a "Conversation so far" block with:
          1. The compressed ``conversation_summary`` (if present).
          2. The last ``max_turns`` ``recent_turns``, each truncated to
             ``max_chars_per_turn`` characters.

        Server-side caps protect against runaway frontend payloads —
        a misbehaving caller can't blow the context window by sending
        500 turns or a 50 KB summary.

        Returns ``""`` when both layers are empty, so the budget assembler
        can skip the section entirely.
        """
        if not self.conversation_summary and not self.recent_turns:
            return ""

        out: list[str] = ["## Conversation so far\n"]

        if self.conversation_summary:
            summary = self.conversation_summary.strip()
            if len(summary) > max_summary_chars:
                summary = summary[:max_summary_chars].rstrip() + "…"
            out.append(f"_Earlier context (summarized):_\n{summary}\n")

        if self.recent_turns:
            out.append("\n_Recent turns:_\n")
            window = self.recent_turns[-max_turns:]
            for turn in window:
                role = (turn.get("role") or "").strip().lower()
                content = (turn.get("content") or "").strip()
                # 2026-05-25 — also skip turns with no/unknown role.
                # Previously an empty role fell through to the "Assistant"
                # default label and a malformed turn ("no role") was rendered
                # as a model utterance, polluting the local LLM's context
                # and biasing its next answer. Only well-formed user /
                # assistant turns are emitted.
                if not content:
                    continue
                if role not in ("user", "assistant"):
                    continue
                if len(content) > max_chars_per_turn:
                    content = content[:max_chars_per_turn].rstrip() + "…"
                # Two-line shape ("Role:\ntext") keeps the model from
                # treating long turns as inline narrative.
                label = "User" if role == "user" else "Assistant"
                out.append(f"\n**{label}:** {content}\n")

        return "".join(out)

    def to_extra_context_block(self, max_chars: int = 2400) -> str:
        """Render ``extra_context`` as a bounded prompt section.

        Each top-level key becomes its own subsection, JSON-encoded so
        the LLM sees a deterministic shape rather than free-form text.
        Total length is capped at ``max_chars`` — if the page sent more,
        we truncate the last subsection and append an "(…truncated)"
        marker so the model knows information was elided.

        Returns an empty string when ``extra_context`` is empty.
        """
        if not self.extra_context:
            return ""

        import json as _json
        out: list[str] = []
        used = 0
        for key in sorted(self.extra_context.keys()):
            try:
                payload = _json.dumps(
                    self.extra_context[key],
                    default=str,
                    sort_keys=True,
                    indent=2,
                )
            except (TypeError, ValueError):
                payload = repr(self.extra_context[key])
            section = f"### {key}\n```json\n{payload}\n```\n"
            if used + len(section) > max_chars:
                remaining = max(0, max_chars - used)
                if remaining > 40:  # only emit if we have space for something useful
                    out.append(section[:remaining] + "\n(…truncated)\n")
                else:
                    out.append("(…remaining extra_context truncated)\n")
                break
            out.append(section)
            used += len(section)

        if not out:
            return ""
        return "## Page-specific context\n\n" + "".join(out)
