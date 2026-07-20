"""
Context budget manager.

Enforces the per-request and per-tool-output token budgets locked in
project_fpulse_ai_step0_locks.md §1. Priority-based inclusion: user intent
always wins; summary truncated next; on-demand details dropped first.

Tier 1 (always included): user intent, system prompt
Tier 2 (compact summary): pipeline summary, schema digest
Tier 3 (on-demand details): RAG chunks, full schemas, sample data

Token estimation is rough — `len(text) // 4` (1 token ≈ 4 chars). A real
tokenizer dependency is overkill at this layer; the goal is enforcement,
not exact counting. Provider-side response-truncation handles the precise
cut.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

# Defaults from project_fpulse_ai_step0_locks.md §1.
DEFAULT_BUDGET_FREE = 8_000          # tokens per request, OSS BYO key
DEFAULT_BUDGET_PLUS = 16_000         # tokens per request, Plus managed
DEFAULT_TOOL_OUTPUT_FREE = 2_000     # per tool output cap
DEFAULT_TOOL_OUTPUT_PLUS = 4_000

# Rough character-to-token ratio. Conservative side; better to under-cut than
# blow the budget.
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Cheap token estimate. ~1 token per 4 characters."""
    return max(1, len(text) // CHARS_PER_TOKEN)


@dataclass
class BudgetSection:
    """One slice of the prompt with a priority tier."""

    name: str
    text: str
    tier: int  # 1 = always, 2 = summary, 3 = on-demand details


@dataclass
class BudgetResult:
    """Result of enforce_budget.

    .sections are the surviving slices in input order.
    .total_tokens is the estimated count of the combined output.
    .dropped_sections lists names removed entirely.
    .truncated_sections lists names that were trimmed (not dropped).
    """

    sections: list[BudgetSection]
    total_tokens: int
    dropped_sections: list[str] = field(default_factory=list)
    truncated_sections: list[str] = field(default_factory=list)

    def render(self, separator: str = "\n\n") -> str:
        return separator.join(s.text for s in self.sections)


class BudgetExceededError(RuntimeError):
    """Raised when even Tier 1 alone exceeds the budget.

    Tier 1 is "user intent + system prompt" — if those alone exceed budget,
    something is structurally wrong (oversized system prompt, malicious
    user input). The caller should reject the request, not truncate it.
    """


def enforce_budget(
    sections: Iterable[BudgetSection],
    *,
    max_tokens: int,
) -> BudgetResult:
    """Trim `sections` to fit `max_tokens`.

    Algorithm:
      1. Always include all Tier 1 sections. If they alone exceed budget,
         raise BudgetExceededError (caller decides what to do).
      2. Add Tier 2 sections in order until budget is reached. Truncate the
         last one to fit if needed.
      3. Add Tier 3 sections in order until budget is reached. Drop any
         that don't fit; truncate the last partial fit.

    Token counting is conservative (CHARS_PER_TOKEN). Real tokenization
    happens provider-side.
    """
    if max_tokens < 1:
        raise ValueError("max_tokens must be >= 1")

    sections = list(sections)
    out: list[BudgetSection] = []
    dropped: list[str] = []
    truncated: list[str] = []
    used = 0

    # Tier 1 — non-negotiable
    tier1 = [s for s in sections if s.tier == 1]
    for s in tier1:
        used += estimate_tokens(s.text)
        out.append(s)
    if used > max_tokens:
        raise BudgetExceededError(
            f"Tier 1 sections alone need {used} tokens, budget is {max_tokens}"
        )

    # Tiers 2 and 3 — fill in priority order
    for tier in (2, 3):
        for s in sections:
            if s.tier != tier:
                continue
            cost = estimate_tokens(s.text)
            remaining = max_tokens - used
            if remaining <= 0:
                dropped.append(s.name)
                continue
            if cost <= remaining:
                used += cost
                out.append(s)
                continue
            # Partial fit — truncate the text to fit remaining budget
            keep_chars = remaining * CHARS_PER_TOKEN
            if keep_chars < 16:
                # Too little space to be useful — drop entirely
                dropped.append(s.name)
                continue
            truncated_text = s.text[:keep_chars] + f"\n[truncated, {len(s.text) - keep_chars} more chars]"
            truncated.append(s.name)
            used += estimate_tokens(truncated_text)
            out.append(BudgetSection(name=s.name, text=truncated_text, tier=s.tier))

    # Preserve input order
    by_id = {id(s): i for i, s in enumerate(sections)}
    out_sorted = sorted(out, key=lambda s: by_id.get(id(s), len(sections)))

    return BudgetResult(
        sections=out_sorted,
        total_tokens=used,
        dropped_sections=dropped,
        truncated_sections=truncated,
    )
