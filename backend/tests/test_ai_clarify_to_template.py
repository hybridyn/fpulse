"""Tests for fpulse.ai.clarify_to_template (Phase 2F, May 18 2026).

Covers the bridge between clarify_draft's question-asking and
templates.py's enterprise scaffolds:

  * match_template_from_intent — static source/sink → key lookup
  * match_template_from_intent_and_answers — dynamic picker (write-mode
    keyword, SCD2 / CDC mention)
  * parse_answers_freeform — regex extraction of answer values from
    user's free-form reply
  * populate_template — placeholder substitution into a real Workflow
"""

from __future__ import annotations

from fpulse.ai.clarify_draft import (
    detect_missing_draft_fields,
)
from fpulse.ai.clarify_to_template import (
    match_template_from_intent,
    match_template_from_intent_and_answers,
    parse_answers_freeform,
    populate_template,
)


# ── Static template selection ─────────────────────────────────────────────


def test_match_oracle_bip_to_sql_server():
    assert match_template_from_intent("oracle_bip", "sql_server") == "oracle_bip_to_sql_server"


def test_match_unknown_pair_returns_none():
    assert match_template_from_intent("csv_file", "parquet") is None
    assert match_template_from_intent(None, None) is None


# ── Dynamic template selection ────────────────────────────────────────────


def test_dynamic_picks_scd2_when_user_mentions_type_2():
    """SCD2 mention in answers wins regardless of source/sink."""
    key = match_template_from_intent_and_answers(
        "csv_file", "sql_server",
        {"cleaning_rules": "SCD2 tracking with type 2 history"},
    )
    assert key == "scd2_dimension"


def test_dynamic_picks_cdc_when_user_mentions_change_data_capture():
    key = match_template_from_intent_and_answers(
        "postgres", "snowflake",
        {"sql_server_write_mode": "use CDC / change-data-capture"},
    )
    assert key == "cdc_incremental"


def test_dynamic_picks_sql_server_upsert_when_user_says_merge():
    """Non-BIP source + SQL Server + upsert/merge answer → generic
    sql_server_upsert template (not the BIP-specific one)."""
    key = match_template_from_intent_and_answers(
        "csv_file", "sql_server",
        {"sql_server_write_mode": "upsert / merge with key"},
    )
    assert key == "sql_server_upsert"


def test_dynamic_picks_bip_template_when_oracle_bip_plus_upsert():
    """Oracle BIP source + upsert intent → the BIP template (richer)."""
    key = match_template_from_intent_and_answers(
        "oracle_bip", "sql_server",
        {"sql_server_write_mode": "upsert"},
    )
    assert key == "oracle_bip_to_sql_server"


def test_dynamic_falls_back_to_static_for_known_pair():
    """No special keywords in answers → use static table."""
    key = match_template_from_intent_and_answers(
        "oracle_bip", "sql_server",
        {"oracle_bip_auth": "basic"},  # no upsert / scd2 / cdc keyword
    )
    assert key == "oracle_bip_to_sql_server"


# ── Answer parsing ────────────────────────────────────────────────────────


def _oracle_bip_questions():
    """Convenience — get the canonical question set for Oracle BIP →
    SQL Server, so tests reflect the real shape clarify_draft emits."""
    cset = detect_missing_draft_fields(
        "Build a pipeline to fetch Oracle BIP into SQL Server."
    )
    assert cset is not None
    return cset.questions


def test_parse_extracts_auth_synonym():
    questions = _oracle_bip_questions()
    parsed = parse_answers_freeform(
        "Use basic auth with admin / password123, and XML format please.",
        questions,
    )
    assert parsed.values.get("oracle_bip_auth") == "basic"
    assert parsed.values.get("oracle_bip_format") == "xml"


def test_parse_extracts_write_mode_upsert():
    questions = _oracle_bip_questions()
    parsed = parse_answers_freeform(
        "I want upsert/merge by employee_id, basic auth, XML.",
        questions,
    )
    assert parsed.values.get("sql_server_write_mode") == "upsert"


def test_parse_extracts_write_mode_truncate():
    questions = _oracle_bip_questions()
    parsed = parse_answers_freeform(
        "Just overwrite — truncate and load fresh each run.",
        questions,
    )
    assert parsed.values.get("sql_server_write_mode") == "overwrite"


def test_parse_finds_table_name_in_quotes():
    questions = _oracle_bip_questions()
    parsed = parse_answers_freeform(
        'Use connection "prod-mssql", table: "dbo.employees", XML format.',
        questions,
    )
    assert parsed.values.get("sql_server_connection") == "prod-mssql"
    assert parsed.values.get("sql_server_table") == "dbo.employees"


def test_parse_reports_unmatched_fields():
    questions = _oracle_bip_questions()
    parsed = parse_answers_freeform("XML", questions)  # only format is answered
    assert "oracle_bip_format" in parsed.matched_fields
    # Most other fields should be unmatched
    assert len(parsed.unmatched_fields) >= 3


def test_parse_empty_text_returns_all_unmatched():
    questions = _oracle_bip_questions()
    parsed = parse_answers_freeform("", questions)
    assert parsed.values == {}
    assert set(parsed.unmatched_fields) == {q.field for q in questions}


def test_parse_handles_oracle_kind_disambiguation():
    """When clarify asks 'which Oracle source', the user's answer maps
    to one of three canonical values."""
    from fpulse.ai.clarify_draft import ClarificationQuestion

    q = ClarificationQuestion(
        field="oracle_kind",
        question="Which Oracle source is this?",
        chips=("BIP", "Fusion REST", "DB direct"),
    )
    parsed = parse_answers_freeform("BIP report", (q,))
    assert parsed.values.get("oracle_kind") == "oracle_bip"

    parsed = parse_answers_freeform("Use Oracle Fusion", (q,))
    assert parsed.values.get("oracle_kind") == "oracle_fusion"


# ── Template population ───────────────────────────────────────────────────


def test_populate_oracle_bip_substitutes_connection_and_table():
    wf = populate_template("oracle_bip_to_sql_server", {
        "sql_server_connection": "prod-mssql",
        "sql_server_table": "employees",
    })
    assert wf is not None
    # Walk every step + check the placeholders were replaced.
    all_params = []
    for s in wf.steps:
        for v in (s.params or {}).values():
            if isinstance(v, str):
                all_params.append(v)
    joined = "\n".join(all_params)
    assert "<your-sql-server-connection>" not in joined
    assert "prod-mssql" in joined
    # Both staging + target placeholders share the sql_server_table value
    # since the user didn't distinguish them.
    assert "employees" in joined


def test_populate_leaves_unanswered_placeholders_intact():
    """The safety contract: placeholders the user didn't answer STAY
    as <your-...> so the pipeline won't validate until they're filled.
    No silent substitution with garbage values."""
    wf = populate_template("oracle_bip_to_sql_server", {
        "sql_server_connection": "prod-mssql",
        # No table answer, no BIP report path answer, no auth answer.
    })
    assert wf is not None
    placeholders_remaining = 0
    for s in wf.steps:
        for v in (s.params or {}).values():
            if isinstance(v, str) and "<your-" in v:
                placeholders_remaining += v.count("<your-")
    # At minimum: BIP URL, username, password secret, report path, table — left as-is
    assert placeholders_remaining >= 3


def test_populate_unknown_template_returns_none():
    assert populate_template("nonexistent_template_xyz", {}) is None


def test_populate_empty_answers_returns_pristine_template():
    """No answers → template returned as-is (all placeholders intact)."""
    wf = populate_template("sql_server_upsert", {})
    assert wf is not None
    # Should still have placeholders in every SQL/conn field
    has_placeholders = False
    for s in wf.steps:
        for v in (s.params or {}).values():
            if isinstance(v, str) and "<your-" in v:
                has_placeholders = True
                break
    assert has_placeholders


# ── End-to-end integration ────────────────────────────────────────────────


def test_full_flow_oracle_bip_to_sql_server():
    """Full sim: detect missing fields → user replies → parse → match
    template → populate."""
    original_prompt = (
        "Build a pipeline to fetch Oracle BIP employee data every 6 hours "
        "and load into SQL Server."
    )
    user_reply = (
        "Basic auth with our BIP service account, XML format, "
        "upsert into connection: \"prod-mssql\", table: \"employees\"."
    )
    cset = detect_missing_draft_fields(original_prompt)
    assert cset is not None
    parsed = parse_answers_freeform(user_reply, cset.questions)
    template_key = match_template_from_intent_and_answers(
        cset.source_type, cset.sink_type, parsed.values,
    )
    assert template_key == "oracle_bip_to_sql_server"
    wf = populate_template(template_key, parsed.values)
    assert wf is not None
    assert len(wf.steps) == 5  # full BIP template
    # Verify the user's answers landed in the IR
    all_text = "\n".join(
        v for s in wf.steps for v in (s.params or {}).values()
        if isinstance(v, str)
    )
    assert "prod-mssql" in all_text
    assert "employees" in all_text
