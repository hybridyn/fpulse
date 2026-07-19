"""lookup_help_topic — read-only. Look up a topic in the F-Pulse atlas.

This is the 24th tool. The atlas is a structured map of pages, glossary
terms, how-to playbooks, tools, nodes, connectors, and docs — see
``backend/fpulse/ai/atlas/``.

Two ways to call:
  * ``{"topic_id": "page.dashboard"}`` — exact lookup (preferred)
  * ``{"query": "where are the credentials"}`` — alias search, returns
    best match

The fast-lane already calls the atlas matcher BEFORE the agent loop,
so most user-facing knowledge questions never reach this tool. It's
here for the cases where the agent is mid-loop and decides it needs
documentation context — e.g. while drafting a pipeline it can call
``lookup_help_topic(topic_id="node.csv_source")`` to remind itself of
the node's purpose before writing the IR.
"""

from __future__ import annotations

from typing import Any

from fpulse.ai.tools.base import ToolContext, ToolDefinition, ToolTier


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    # Lazy import — atlas pulls in StepType, manifests, etc. at module
    # load; keep this deferred so a malformed atlas can't break tool
    # registry boot.
    from fpulse.ai.atlas import (
        Tier,
        find_topic_by_id,
        find_topics_by_alias,
    )

    topic_id = (inputs.get("topic_id") or "").strip()
    query = (inputs.get("query") or "").strip()

    # Path A: exact id lookup
    if topic_id:
        topic = find_topic_by_id(topic_id)
        if topic is None:
            return {
                "match_kind": "none",
                "topic_id": topic_id,
                "_error": (
                    f"No atlas topic with id {topic_id!r}. Try a `query` "
                    f"instead, or call lookup_help_topic with an empty body "
                    f"to get the topic catalog."
                ),
            }
        return _topic_to_response(topic, score=1.0, match_kind="exact_id")

    # Path B: alias / keyword search
    if query:
        matches = find_topics_by_alias(query, tier_filter=Tier.OSS, limit=3)
        if not matches:
            return {
                "match_kind": "none",
                "query": query,
                "_error": (
                    f"No atlas topic matched {query!r}. The user probably "
                    f"wants something outside the canned knowledge map — "
                    f"answer from your own reasoning or call a more "
                    f"specific tool."
                ),
            }
        top, score = matches[0]
        response = _topic_to_response(top, score=score, match_kind="alias_search")
        # Include neighbours so the model can offer follow-ups
        if len(matches) > 1:
            response["other_candidates"] = [
                {"id": t.id, "title": t.title, "score": s}
                for t, s in matches[1:]
            ]
        return response

    # No inputs at all — return the topic catalog as a directory.
    # Useful when the agent doesn't know which topic to ask for.
    from fpulse.ai.atlas import ATLAS, TopicCategory
    by_category: dict[str, list[dict[str, str]]] = {}
    for t in ATLAS:
        if t.tier == Tier.PLUS:
            continue
        by_category.setdefault(t.category.value, []).append(
            {"id": t.id, "title": t.title}
        )
    return {
        "match_kind": "catalog",
        "category_counts": {k: len(v) for k, v in by_category.items()},
        "topics_by_category": by_category,
    }


def _topic_to_response(topic, *, score: float, match_kind: str) -> dict[str, Any]:
    """Shared response shape for both lookup paths."""
    return {
        "match_kind": match_kind,
        "topic_id": topic.id,
        "category": topic.category.value,
        "title": topic.title,
        "body": topic.body,
        "see_also": list(topic.see_also),
        "tier": topic.tier.value,
        "source": topic.source,
        "score": round(score, 3),
    }


DEFINITION = ToolDefinition(
    name="lookup_help_topic",
    tier=ToolTier.READ,
    description=(
        "Look up F-Pulse product knowledge in the atlas — pages, glossary "
        "terms, how-to playbooks, tools, node types, connectors, and docs. "
        "Call with `topic_id` (e.g. \"page.dashboard\", \"glossary.connection\", "
        "\"howto.schedule_pipeline\") for an exact lookup, OR with `query` "
        "(natural language: \"where are the docs\", \"how do I schedule\") "
        "for an alias search. Call with NEITHER argument to get the full "
        "topic catalog. Use this whenever the user asks 'what is X', "
        "'where is Y', 'how do I Z', or you need a refresher on what a "
        "page / node / connector does before drafting work."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "topic_id": {
                "type": "string",
                "description": (
                    "Exact topic id, e.g. \"page.dashboard\" or "
                    "\"glossary.connection\". Optional."
                ),
            },
            "query": {
                "type": "string",
                "description": (
                    "Natural-language phrase to search aliases against, "
                    "e.g. \"where are the credentials\". Optional."
                ),
            },
        },
        # Neither is strictly required — empty call returns the catalog.
    },
    output_schema={
        "match_kind": "str",  # exact_id | alias_search | catalog | none
        "topic_id": "str",
        "category": "str",
        "title": "str",
        "body": "str",
        "see_also": "list",
        "tier": "str",
        "source": "str",
        "score": "float",
    },
    handler=_handler,
    requires_idempotency_key=False,
    tags=["knowledge", "read", "atlas"],
)
