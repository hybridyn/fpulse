"""
Node AI Assist grounding + connector-aware fallback.

User report (2026-06-16): asking the node-level AI Assist "I need to connect
salesforce source?" returned a generic canned tip ("pick a saved Connection…")
that ignored the question. Two fixes pinned here:

  1. The deterministic fallback (used when no LLM is reachable) is now
     CONNECTOR-AWARE — it recognises a named system (Salesforce, csv, …) and
     answers specifically, instead of a pure boilerplate line.
  2. The grounding block injected into the LLM prompt names the REAL connector
     catalog + the node's fields, so a model answer can't be generic either.

No LLM / network here — these are the pure deterministic helpers.
"""

from __future__ import annotations

import pytest

from fpulse.nodes.registry import get_registry


@pytest.fixture(scope="module", autouse=True)
def _load_registry():
    get_registry()  # populate manifests + node registry
    yield


def test_fallback_recognises_a_saas_system_by_name():
    """The exact user scenario: a Source node asked about Salesforce should be
    pointed at the SaaS Connector node, not given the generic 'pick a
    connection' line."""
    from fpulse.api.ai import _assist_fallback

    msg = _assist_fallback("source", "I need to connect salesforce source?")
    low = msg.lower()
    assert "salesforce" in low
    assert "saas connector" in low  # names the correct node
    assert "pick a saved connection (or file path)" not in low  # NOT the boilerplate


def test_fallback_recognises_a_generic_source_type():
    from fpulse.api.ai import _assist_fallback

    msg = _assist_fallback("source", "how do I load a csv file?")
    assert "csv" in msg.lower()


def test_fallback_unknown_question_still_returns_a_useful_line():
    from fpulse.api.ai import _assist_fallback

    msg = _assist_fallback("filter", "what goes here?")
    assert "condition" in msg.lower()  # filter-specific default


def test_grounding_lists_real_connectors_for_a_source_node():
    from fpulse.api.ai import _fpulse_assist_grounding

    block = _fpulse_assist_grounding("source", "default")
    low = block.lower()
    assert "f-pulse facts" in low
    # SaaS catalog present (Salesforce is a shipped manifest)
    assert "saas connectors" in low and "salesforce" in low
    # generic source connector types present (csv is in SOURCE_MAP)
    assert "generic source connector types" in low and "csv" in low


def test_grounding_includes_node_fields():
    from fpulse.api.ai import _fpulse_assist_grounding, _node_param_fields

    fields = _node_param_fields("filter")
    assert fields, "filter should expose configurable fields"
    block = _fpulse_assist_grounding("filter", "default")
    assert "configurable fields" in block.lower()
