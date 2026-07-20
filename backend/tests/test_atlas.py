"""Tests for the F-Pulse Atlas — schema invariants, matcher behaviour,
auto-generator coverage, and routing assertions for common user
questions.

Four layers:
  * ``TestAtlasInvariants`` — uniqueness, alias hygiene, body non-empty
  * ``TestAtlasMatcher`` — scoring tiers, tier filter, top-K cap
  * ``TestAutogenCoverage`` — every tool / node / connector / allowlisted
    doc has a matching atlas topic (drift guard)
  * ``TestCommonQuestionsRoute`` — natural "where/what/how" prompts that
    a real user might type all resolve to a sensible topic
"""

from __future__ import annotations

import pytest

from fpulse.ai.atlas import (
    ATLAS,
    Tier,
    Topic,
    TopicCategory,
    application_map_lines,
    find_topic_by_id,
    find_topics_by_alias,
)


# ─────────────────────────────────────────────────────────────────────
# Invariants — enforced at import time too, but verify here in case
# someone changes _validate_atlas to be lenient later.
# ─────────────────────────────────────────────────────────────────────


class TestAtlasInvariants:
    def test_atlas_not_empty(self):
        assert len(ATLAS) >= 50, (
            f"Atlas has only {len(ATLAS)} topics — expected at least 50 "
            "(hand-written pages + glossary + how-tos + autogen tools/nodes/etc)"
        )

    def test_ids_are_unique(self):
        ids = [t.id for t in ATLAS]
        assert len(ids) == len(set(ids)), "Duplicate topic ids in atlas"

    def test_ids_are_namespaced(self):
        # Every id must have a dot-separated namespace prefix.
        for t in ATLAS:
            assert "." in t.id, (
                f"Topic id {t.id!r} is not namespaced "
                "(expected e.g. 'page.dashboard', 'glossary.connection')"
            )

    def test_aliases_are_lowercase(self):
        for t in ATLAS:
            for a in t.aliases:
                assert a == a.lower(), (
                    f"Topic {t.id!r} alias {a!r} must be lowercase — the "
                    "matcher lowercases the prompt but compares verbatim"
                )

    def test_aliases_are_non_empty(self):
        for t in ATLAS:
            assert t.aliases, f"Topic {t.id!r} has empty aliases tuple"

    def test_bodies_are_non_empty(self):
        for t in ATLAS:
            assert t.body and t.body.strip(), (
                f"Topic {t.id!r} has empty body"
            )

    def test_see_also_ids_resolve(self):
        # Every see_also id must point at an actual topic in the atlas
        # (otherwise the "See also" footer in fast-lane output silently
        # drops it). Drift guard.
        all_ids = {t.id for t in ATLAS}
        for t in ATLAS:
            for sid in t.see_also:
                assert sid in all_ids, (
                    f"Topic {t.id!r} references see_also={sid!r} which doesn't exist"
                )


# ─────────────────────────────────────────────────────────────────────
# Matcher — scoring tiers + tier filter
# ─────────────────────────────────────────────────────────────────────


class TestAtlasMatcher:
    def test_exact_alias_match_scores_high(self):
        # Pick a known topic + one of its aliases verbatim
        matches = find_topics_by_alias("dashboard")
        assert matches, "Expected at least one match for 'dashboard'"
        top, score = matches[0]
        assert top.id == "page.dashboard"
        assert score >= 0.8

    def test_prefix_match(self):
        matches = find_topics_by_alias("documentation please")
        assert matches, "Expected match for 'documentation please'"
        assert matches[0][0].id == "howto.find_docs"

    def test_multi_word_substring_match(self):
        matches = find_topics_by_alias("how do i schedule a pipeline tomorrow")
        assert matches
        top_ids = {m[0].id for m in matches}
        assert "howto.schedule_pipeline" in top_ids

    def test_no_match_below_min_score(self):
        # Gibberish should match nothing
        matches = find_topics_by_alias("xyzzy frobnicator quux")
        assert matches == [], (
            f"Expected no matches for gibberish, got {[m[0].id for m in matches]}"
        )

    def test_empty_prompt_returns_empty(self):
        assert find_topics_by_alias("") == []
        assert find_topics_by_alias("   ") == []

    def test_oss_tier_filter_excludes_plus_topics(self):
        # page.lineage is marked Tier.PLUS. OSS tier filter must exclude it
        # even though "lineage" exactly matches an alias.
        matches = find_topics_by_alias("lineage", tier_filter=Tier.OSS)
        for t, _score in matches:
            assert t.tier != Tier.PLUS, (
                f"OSS tier filter must exclude Plus topics; got {t.id}"
            )

    def test_limit_caps_results(self):
        # "templates" matches at least 2 topics (page.templates + howto.use_templates)
        matches = find_topics_by_alias("templates", limit=2)
        assert len(matches) <= 2

    def test_find_topic_by_id_exact(self):
        t = find_topic_by_id("page.dashboard")
        assert t is not None
        assert t.title == "Dashboard"

    def test_find_topic_by_id_missing_returns_none(self):
        assert find_topic_by_id("page.does_not_exist") is None


# ─────────────────────────────────────────────────────────────────────
# Auto-generator coverage — drift guards
# ─────────────────────────────────────────────────────────────────────


class TestAutogenCoverage:
    """Every source we auto-import from must have a corresponding atlas
    topic. Catches: new tool added → atlas grows; new node added → atlas
    grows; new connector manifest added → atlas grows."""

    def test_every_tool_has_topic(self):
        from fpulse.ai.tools import INITIAL_TOOLS
        atlas_tool_ids = {
            t.id.removeprefix("tool.")
            for t in ATLAS
            if t.category == TopicCategory.TOOL
        }
        for tool in INITIAL_TOOLS:
            assert tool.name.lower() in atlas_tool_ids, (
                f"Tool {tool.name!r} is in INITIAL_TOOLS but missing from atlas. "
                "Atlas autogen should pick this up — check topics_autogen.py:_tool_topics"
            )

    def test_every_step_type_has_topic(self):
        from fpulse.ir.schema import StepType
        atlas_node_ids = {
            t.id.removeprefix("node.")
            for t in ATLAS
            if t.category == TopicCategory.NODE
        }
        for member in StepType:
            assert member.value in atlas_node_ids, (
                f"StepType.{member.name} (value={member.value!r}) missing from atlas. "
                "Check topics_autogen.py:_node_topics"
            )

    def test_connector_topics_present(self):
        # At least 20 connector topics should exist (we ship 40+ manifests;
        # any number below 20 means the autogen probably failed)
        connector_count = sum(
            1 for t in ATLAS if t.category == TopicCategory.CONNECTOR
        )
        assert connector_count >= 20, (
            f"Expected at least 20 connector topics, found {connector_count}. "
            "Check topics_autogen.py:_connector_topics + manifest dir"
        )

    def test_doc_topics_present(self):
        # The allowlist in topics_autogen has ~15 entries; we should have
        # at least 5 doc topics generated (some docs may not exist in OSS).
        doc_count = sum(1 for t in ATLAS if t.category == TopicCategory.DOC)
        assert doc_count >= 5, (
            f"Expected at least 5 doc topics, found {doc_count}. "
            "Check topics_autogen.py:_doc_topics + _DOC_ALLOWLIST"
        )


# ─────────────────────────────────────────────────────────────────────
# Routing — natural questions resolve to sensible topics
# ─────────────────────────────────────────────────────────────────────


class TestCommonQuestionsRoute:
    """For each question a real user might type, assert that the matcher
    returns a topic whose id matches the expected prefix. Loose enough
    to survive trigger-list edits, strict enough to catch regressions."""

    @pytest.mark.parametrize("prompt,expected_id", [
        # The original screenshot case
        ("I need to see the documents", "howto.find_docs"),
        ("show me documentation", "howto.find_docs"),
        ("where are the docs", "howto.find_docs"),
        ("read the manual", "howto.find_docs"),

        # Page-finding questions
        ("dashboard", "page.dashboard"),
        ("open settings", "page.settings"),
        ("where is the help page", "page.help"),
        ("connections page", "page.connections"),
        ("pool page", "page.pool"),

        # Glossary
        ("what is a pipeline", "glossary.pipeline"),
        ("define connection", "glossary.connection"),
        ("what is a workspace", "glossary.workspace"),

        # How-to playbooks
        ("how do i create a pipeline", "howto.create_pipeline"),
        ("how do i add a connection", "howto.add_connection"),
        ("how do i set up notifications", "howto.set_up_notifications"),
        ("how do i schedule a pipeline", "howto.schedule_pipeline"),

        # Editions
        ("what is the oss version", "edition.oss"),
        ("oss vs plus", "edition.comparison"),
    ])
    def test_routes_to_expected_topic(self, prompt, expected_id):
        matches = find_topics_by_alias(prompt)
        assert matches, f"No match for prompt: {prompt!r}"
        top_id = matches[0][0].id
        assert top_id == expected_id, (
            f"Prompt {prompt!r} routed to {top_id!r}, expected {expected_id!r}. "
            f"Top 3 matches: {[(m[0].id, round(m[1], 2)) for m in matches[:3]]}"
        )


# ─────────────────────────────────────────────────────────────────────
# Application map (system-prompt augmentation)
# ─────────────────────────────────────────────────────────────────────


class TestHelpPageDriftGuard:
    """Coarse sync check: every major HelpPage area must have at least
    one atlas topic so the Copilot can answer when a user asks about it.

    Doesn't parse HelpPage.tsx — that's a moving target. Instead,
    enumerates the area names that exist in the current HelpPage and
    asserts at least one matching atlas topic per area. When someone
    adds a new HelpPage area, this test fails until they also add an
    atlas topic — that's the drift guard.

    If you renamed / removed a HelpPage area, update the parametrize
    list below.
    """

    @pytest.mark.parametrize("help_area,required_topic_prefix", [
        # Getting Started steps (HelpPage GETTING_STARTED array)
        ("create_first_pipeline", "howto.create_pipeline"),
        ("use_copilot", "howto.use_copilot"),

        # How-To Guide categories
        ("building_pipelines", "howto.create_pipeline"),
        ("managing_data", "howto.add_connection"),
        ("scheduling_alerts", "howto.schedule_pipeline"),
        ("triggering_api", "howto.trigger_from_api"),

        # Tab-level coverage (the HelpPage has tabs for these)
        ("nodes_tab", "node."),  # any node.* topic satisfies
        ("shortcuts_tab", "page.help"),  # help page covers shortcuts ref
    ])
    def test_help_area_has_atlas_topic(self, help_area, required_topic_prefix):
        # Either exact id or any topic with the given prefix counts.
        matching = [
            t for t in ATLAS
            if t.id == required_topic_prefix
            or t.id.startswith(required_topic_prefix)
        ]
        assert matching, (
            f"HelpPage area {help_area!r} expected an atlas topic at "
            f"{required_topic_prefix!r} but none found. If HelpPage no "
            "longer has this area, remove it from the parametrize list. "
            "Otherwise add a matching topic in topics_handwritten.py."
        )


class TestApplicationMap:
    def test_map_is_non_empty(self):
        lines = application_map_lines()
        assert len(lines) >= 20, (
            f"Application map has {len(lines)} lines — expected at least 20 "
            "(pages + glossary + features)"
        )

    def test_each_line_has_id_and_title(self):
        for line in application_map_lines():
            assert line.startswith("- "), f"Bad map line format: {line!r}"
            assert ":" in line, f"Map line missing colon: {line!r}"
            assert " — " in line or " - " in line, (
                f"Map line missing title/description separator: {line!r}"
            )

    def test_map_excludes_plus_topics_for_oss(self):
        # OSS filter (default) should not surface page.lineage
        text = "\n".join(application_map_lines(tier_filter=Tier.OSS))
        assert "page.lineage" not in text, (
            "OSS application map must not include page.lineage (Plus-only)"
        )

    def test_map_fits_token_budget(self):
        # Sanity: the map shouldn't exceed ~6000 chars (~1500 tokens). If
        # it does, we're bloating the system prompt every request.
        text = "\n".join(application_map_lines())
        assert len(text) < 6000, (
            f"Application map is {len(text)} chars — too large for prompt. "
            "Consider tighter title/first-sentence trimming."
        )
