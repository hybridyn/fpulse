"""
F-Pulse Atlas — structured knowledge map of the application.

Replaces the "model knows nothing about the app" problem with a flat,
testable list of `Topic` entries that cover every page, glossary term,
common task, tool, node type, connector, and key doc.

Design goals:
  * **Deterministic.** No vector store, no embeddings, no LLM in the
    lookup path. RAG is explicitly a Plus feature
    (see project_plus_roadmap_2026-05-01.md). OSS gets keyword + alias
    matching against a hand-curated + auto-generated atlas.
  * **Idempotent.** Auto-generators read the same source files every
    time (INITIAL_TOOLS, StepType enum, connector manifests, docs/).
    Re-running them never duplicates topics.
  * **Drift-guarded.** Tests assert that every HelpPage section, every
    INITIAL_TOOLS entry, every StepType, and every connector manifest
    has a corresponding atlas topic — so adding a new feature without
    documenting it fails CI.
  * **Compact.** Bodies are 3-8 sentences each, ~300 chars. The whole
    atlas is meant to fit in memory and stream to the LLM as needed
    without prompt bloat.

Three callable paths:

  1. Fast-lane intent ``lookup_topic`` (fast_router.py) — scores user
     prompts against ``aliases`` of every topic; on match, returns
     ``body`` verbatim with zero LLM calls. Sub-100 ms.

  2. 24th agent tool ``lookup_help_topic(topic_id)`` — for cases the
     fast-lane couldn't classify, the LLM can call this tool with a
     topic id (the system prompt advertises the topic catalog).

  3. System prompt augmentation — a compact "F-Pulse application map"
     (one line per page + one line per glossary term) gets appended to
     the agent system prompt so the model knows what exists by name.

Public surface:
  * ``Topic`` — dataclass.
  * ``TopicCategory`` — enum (page, glossary, howto, doc, tool, node,
    connector, feature, edition).
  * ``Tier`` — enum (oss, plus, both).
  * ``ATLAS`` — frozen tuple of every Topic, hand-written + auto-gen.
  * ``find_topic_by_id(id)`` — lookup helper.
  * ``find_topics_by_alias(prompt)`` — keyword/alias matcher, returns
    [(topic, score), …] sorted descending. Used by fast_router.
  * ``application_map_lines()`` — compact one-line index for prompt
    augmentation.
"""

from __future__ import annotations

from .schema import (
    ATLAS,
    Tier,
    Topic,
    TopicCategory,
    application_map_lines,
    find_topic_by_id,
    find_topics_by_alias,
)

__all__ = [
    "ATLAS",
    "Tier",
    "Topic",
    "TopicCategory",
    "application_map_lines",
    "find_topic_by_id",
    "find_topics_by_alias",
]
