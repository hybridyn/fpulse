"""
Atlas core — Topic dataclass, enums, matcher, loader.

The atlas itself is assembled at import time from two sources:

  * ``topics_handwritten.py`` — pages, glossary, how-tos, features,
    edition boundary. Authored by hand because the answer ISN'T in any
    single source file — it's product knowledge.

  * ``topics_autogen.py`` — tools, node types, connector manifests,
    doc files. Generated from the underlying source so adding a new
    tool / node / connector / doc automatically adds a topic.

Both sources return tuples of Topic; the loader merges them, asserts
id uniqueness (drift guard), and freezes the result as ``ATLAS``.

The matcher (``find_topics_by_alias``) mirrors the scoring tiers used
in ``fast_router._score_match`` so behaviour is predictable across the
two surfaces.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

logger = logging.getLogger(__name__)


class TopicCategory(str, Enum):
    """High-level grouping. Drives icon + grouping in any future UI,
    and lets tests filter / iterate per category."""
    PAGE = "page"          # A page or route in the app
    GLOSSARY = "glossary"  # A product term (Connection, Workspace, …)
    HOWTO = "howto"        # Step-by-step task playbook
    DOC = "doc"            # Pointer to a docs/*.md file
    TOOL = "tool"          # One of the 23 Copilot tools
    NODE = "node"          # One pipeline node type
    CONNECTOR = "connector"  # One connector integration
    FEATURE = "feature"    # A cross-cutting capability (scheduling, alerts)
    EDITION = "edition"    # OSS vs Plus boundary content


class Tier(str, Enum):
    """OSS / Plus boundary. Used by the matcher to filter out Plus-only
    topics for OSS users (so the Copilot doesn't tease unbuilt features)."""
    OSS = "oss"
    PLUS = "plus"
    BOTH = "both"


@dataclass(frozen=True)
class Topic:
    """One discrete piece of product knowledge.

    Invariants enforced by ``_validate_atlas`` at import time:
      * ``id`` is unique across the whole atlas
      * ``id`` is namespaced (e.g. ``page.dashboard``, ``glossary.connection``)
      * ``aliases`` is non-empty and lowercased
      * ``body`` is non-empty
    """
    id: str
    category: TopicCategory
    title: str
    # Phrases the user might type to find this topic. The matcher scores
    # these like fast_router triggers. Aliases should be SHORT and
    # discriminating — "settings" is fine, "the settings page where I
    # configure things" is too long. Synonyms are encouraged
    # ("docs", "documentation", "manual").
    aliases: tuple[str, ...]
    # The actual answer. Markdown. 3-8 sentences ideal. The fast-lane
    # returns this verbatim, so make it useful as a standalone reply.
    body: str
    # OSS / Plus / both. Plus-only topics are filtered out for OSS users
    # so the Copilot doesn't surface unbuilt features.
    tier: Tier = Tier.OSS
    # Related topic ids — surfaced as "See also" in the response.
    see_also: tuple[str, ...] = ()
    # Source pointer ("hand-written", "auto:INITIAL_TOOLS",
    # "auto:StepType", "auto:manifests/salesforce.json",
    # "auto:docs/quickstart.md"). Used by the drift guard test.
    source: str = "hand-written"


# ─────────────────────────────────────────────────────────────────────
# Loader
# ─────────────────────────────────────────────────────────────────────


def _load_atlas() -> tuple[Topic, ...]:
    """Build the unified atlas tuple.

    Tries auto-generators in a try/except — atlas should still load
    even if a manifest file is malformed or a tool import fails. The
    drift guard test surfaces missing autogen entries explicitly.
    """
    from .topics_handwritten import HANDWRITTEN_TOPICS

    topics: list[Topic] = list(HANDWRITTEN_TOPICS)

    try:
        from .topics_autogen import autogen_topics
        topics.extend(autogen_topics())
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Atlas auto-generator failed; using hand-written topics only: %s",
            exc,
        )

    _validate_atlas(tuple(topics))
    return tuple(topics)


def _validate_atlas(topics: tuple[Topic, ...]) -> None:
    """Raise on duplicate ids or empty aliases — fail fast at import."""
    seen: set[str] = set()
    for t in topics:
        if t.id in seen:
            raise ValueError(f"Duplicate atlas topic id: {t.id!r}")
        seen.add(t.id)
        if not t.aliases:
            raise ValueError(f"Topic {t.id!r} has empty aliases tuple")
        if not t.body or not t.body.strip():
            raise ValueError(f"Topic {t.id!r} has empty body")
        # Aliases must be lowercased — the matcher lowercases the prompt
        # but compares verbatim against aliases.
        for a in t.aliases:
            if a != a.lower():
                raise ValueError(
                    f"Topic {t.id!r} alias {a!r} must be lowercase"
                )


# Assembled at import time. Tests import this directly.
ATLAS: tuple[Topic, ...] = _load_atlas()


# ─────────────────────────────────────────────────────────────────────
# Lookup helpers
# ─────────────────────────────────────────────────────────────────────


def find_topic_by_id(topic_id: str) -> Topic | None:
    """Exact id lookup. Used by the lookup_help_topic agent tool."""
    for t in ATLAS:
        if t.id == topic_id:
            return t
    return None


# Stopwords pulled from fast_router._TOKEN_STOPWORDS — keep aligned.
_ATLAS_STOPWORDS = frozenset({
    "a", "an", "the", "of", "to", "in", "on", "for", "with", "and", "or",
    "is", "are", "was", "were", "be", "been", "being",
    "i", "me", "my", "we", "us", "our", "you", "your",
    "show", "list", "give", "tell", "get",
    "please", "now", "today", "need", "want", "see",
})


def _content_tokens(text: str) -> set[str]:
    """Tokenise + drop stopwords. Mirrors fast_router._content_tokens."""
    out: set[str] = set()
    for tok in re.findall(r"[a-z][a-z']+", text.lower()):
        if len(tok) >= 3 and tok not in _ATLAS_STOPWORDS:
            out.add(tok)
    return out


def _score_alias(prompt_lower: str, prompt_stripped: str, alias: str) -> float:
    """Five-tier scorer mirroring fast_router._score_match:
       0.98 exact, 0.9 prefix, 0.85 multi-word substring,
       0.8 single-word boundary, 0.75 token-overlap (≥2 tokens)."""
    if prompt_stripped == alias:
        return 0.98
    if prompt_stripped.startswith(alias + " ") or prompt_stripped.startswith(alias + "?"):
        return 0.9
    if " " in alias:
        if alias in prompt_lower:
            return 0.85
    else:
        if re.search(rf"\b{re.escape(alias)}\b", prompt_lower):
            return 0.8
    # Token-overlap (last resort, only for multi-token aliases)
    alias_toks = _content_tokens(alias)
    if len(alias_toks) >= 2:
        prompt_toks = _content_tokens(prompt_lower)
        if alias_toks.issubset(prompt_toks):
            return 0.75
    return 0.0


# Minimum score to consider a topic a hit. Lower than fast_router's
# 0.6 because the atlas is wider-coverage / less curated, so partial
# matches are more likely to be the right answer than a hard fall-through.
ATLAS_MIN_SCORE = 0.7


def find_topics_by_alias(
    prompt: str,
    *,
    tier_filter: Tier = Tier.OSS,
    min_score: float = ATLAS_MIN_SCORE,
    limit: int = 5,
) -> list[tuple[Topic, float]]:
    """Score every topic's aliases against the prompt.

    Returns top-K matches sorted by descending score, filtered to
    topics matching ``tier_filter`` (OSS user → exclude Plus-only).
    Empty list if nothing scores at min_score or higher.
    """
    if not prompt or not prompt.strip():
        return []
    p_lower = prompt.lower().strip()
    p_stripped = re.sub(r"[.,;:!?]+$", "", p_lower).strip()

    scored: list[tuple[Topic, float]] = []
    for t in ATLAS:
        # Tier filter — OSS users never see plus-only topics; plus users
        # see both. BOTH topics are always visible.
        if tier_filter == Tier.OSS and t.tier == Tier.PLUS:
            continue
        # Best alias wins for this topic.
        best = 0.0
        for alias in t.aliases:
            s = _score_alias(p_lower, p_stripped, alias)
            if s > best:
                best = s
        if best >= min_score:
            scored.append((t, best))
    # Sort descending by score, then by topic id for stable output.
    scored.sort(key=lambda x: (-x[1], x[0].id))
    return scored[:limit]


# ─────────────────────────────────────────────────────────────────────
# System-prompt augmentation
# ─────────────────────────────────────────────────────────────────────


def application_map_lines(
    *,
    tier_filter: Tier = Tier.OSS,
    categories: Iterable[TopicCategory] = (
        TopicCategory.PAGE,
        TopicCategory.GLOSSARY,
        TopicCategory.FEATURE,
    ),
) -> list[str]:
    """Compact one-line-per-topic index for system-prompt augmentation.

    Format: ``- {id}: {title} — {first-sentence-of-body}``

    Only emits topics in the requested categories (default: page +
    glossary + feature). Tools, nodes, connectors, and docs are not
    included by default — they'd bloat the prompt without high signal.

    Stays under ~80 lines / ~3000 tokens for the default filter, which
    is the budget for prompt augmentation without blowing the context.
    """
    out: list[str] = []
    for t in ATLAS:
        if t.category not in categories:
            continue
        if tier_filter == Tier.OSS and t.tier == Tier.PLUS:
            continue
        # First sentence of body, truncated to 100 chars.
        first = t.body.split(".", 1)[0].strip()
        if len(first) > 100:
            first = first[:97] + "…"
        out.append(f"- {t.id}: {t.title} — {first}")
    return out
