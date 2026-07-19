"""Tests for fpulse.ai.clarify_draft.detect_missing_draft_fields.

The detection engine is purely heuristic — no LLM, no I/O. Tests focus
on whether the right question set fires for the canonical patterns in
the prompt bank (Oracle BIP → SQL Server, REST API, file → DB, etc.)
and that vague vs detailed prompts are correctly distinguished.
"""

from __future__ import annotations

from fpulse.ai.clarify_draft import (
    ClarificationSet,
    detect_missing_draft_fields,
    render_clarification_card,
)


# ── Negative cases — should return None ───────────────────────────────────


def test_empty_prompt_returns_none():
    assert detect_missing_draft_fields("") is None
    assert detect_missing_draft_fields("   ") is None


def test_non_build_prompt_returns_none():
    """'Why did my pipeline fail' is a question, not a build request."""
    assert detect_missing_draft_fields("why did my pipeline fail?") is None


def test_question_about_pipelines_returns_none():
    """'What is a pipeline' is informational, not a build request."""
    assert detect_missing_draft_fields("what is a pipeline?") is None


def test_show_failures_returns_none():
    """Listing / monitoring prompts are not build requests."""
    assert detect_missing_draft_fields("show me failed pipelines") is None


def test_fully_specified_csv_to_parquet_returns_none():
    """Pure CSV → Parquet has no auth, no DB write mode, no schedule
    questions — the rule planner can handle it straight."""
    result = detect_missing_draft_fields(
        "Build a pipeline that reads sales.csv and writes to a Parquet file."
    )
    # Should not ask cleaning questions either (no "clean" keyword).
    assert result is None


# ── Positive cases — the canonical drift prompts ──────────────────────────


def test_oracle_bip_to_sql_server_asks_full_question_set():
    """The headline scenario: Oracle BIP + SQL Server + scheduled.
    Should ask source auth + report path + format + sink connection +
    write mode + target table."""
    result = detect_missing_draft_fields(
        "Create a pipeline to fetch Oracle BIP employee data every 6 hours "
        "and load into SQL Server."
    )
    assert result is not None
    assert result.source_type == "oracle_bip"
    assert result.sink_type == "sql_server"
    # Should have at least 5 questions (3 Oracle BIP + 3 SQL Server,
    # minus overlap).
    assert len(result.questions) >= 5
    fields = {q.field for q in result.questions}
    assert "oracle_bip_auth" in fields
    assert "oracle_bip_report_path" in fields
    assert "oracle_bip_format" in fields
    assert "sql_server_connection" in fields
    assert "sql_server_write_mode" in fields


def test_oracle_bip_with_explicit_schedule_skips_schedule_question():
    """User said 'every 6 hours' so don't ask for schedule again."""
    result = detect_missing_draft_fields(
        "Create a pipeline to fetch Oracle BIP every 6 hours and load to SQL Server."
    )
    assert result is not None
    fields = {q.field for q in result.questions}
    assert "schedule_interval" not in fields


def test_recurring_without_interval_asks_schedule():
    """'Recurring' / 'periodically' = needs schedule but no interval given."""
    result = detect_missing_draft_fields(
        "Build a recurring pipeline from a REST API into a database."
    )
    assert result is not None
    fields = {q.field for q in result.questions}
    assert "schedule_interval" in fields


def test_clean_keyword_without_specifics_asks_cleaning():
    """'Clean the data' with no rules → ask what cleaning."""
    result = detect_missing_draft_fields(
        "Build a pipeline that pulls from the API, cleans the data, and writes to SQL Server."
    )
    assert result is not None
    fields = {q.field for q in result.questions}
    assert "cleaning_rules" in fields


def test_clean_with_specific_rules_skips_cleaning_question():
    """User already said WHAT cleaning — don't re-ask."""
    result = detect_missing_draft_fields(
        "Build a pipeline that pulls from API, removes duplicates and drops null rows, "
        "and writes to SQL Server."
    )
    assert result is not None
    fields = {q.field for q in result.questions}
    assert "cleaning_rules" not in fields


def test_rest_api_to_postgres_asks_api_auth_and_db_write_mode():
    result = detect_missing_draft_fields(
        "Create a pipeline that calls a REST API and loads results into Postgres."
    )
    assert result is not None
    assert result.source_type in ("rest_api", "api")
    assert result.sink_type == "postgres"
    fields = {q.field for q in result.questions}
    assert "api_auth" in fields
    assert "db_write_mode" in fields


def test_ambiguous_oracle_asks_which_kind():
    """Just 'Oracle' without BIP/Fusion specifier → ask which kind."""
    result = detect_missing_draft_fields(
        "Build a pipeline from Oracle into SQL Server."
    )
    assert result is not None
    assert result.source_type == "oracle"
    fields = {q.field for q in result.questions}
    assert "oracle_kind" in fields


def test_csv_to_sql_server_asks_only_sink_questions():
    """File source has no auth questions — only the sink needs clarifying."""
    result = detect_missing_draft_fields(
        "Build a pipeline that loads daily.csv into SQL Server."
    )
    assert result is not None
    assert result.source_type == "csv_file"
    assert result.sink_type == "sql_server"
    fields = {q.field for q in result.questions}
    # No source auth question for a local CSV
    assert "api_auth" not in fields
    assert "oracle_bip_auth" not in fields
    # But sink questions ARE present
    assert "sql_server_connection" in fields
    assert "sql_server_write_mode" in fields


# ── Render output ─────────────────────────────────────────────────────────


def test_render_card_has_detected_intent_echo():
    cset = detect_missing_draft_fields(
        "Build a pipeline to fetch Oracle BIP every 6 hours into SQL Server."
    )
    assert cset is not None
    rendered = render_clarification_card(cset)
    assert "oracle_bip" in rendered
    assert "sql_server" in rendered
    assert "A few quick questions before I draft it" in rendered


def test_render_card_includes_all_chip_options():
    cset = detect_missing_draft_fields(
        "Build a pipeline from REST API into Postgres."
    )
    assert cset is not None
    rendered = render_clarification_card(cset)
    # First chip of api_auth question should be present
    assert "Bearer token" in rendered
    # First chip of db_write_mode
    assert "Append" in rendered


def test_render_card_marks_optional_questions():
    cset = detect_missing_draft_fields(
        "Build a pipeline to fetch Oracle BIP into SQL Server."
    )
    assert cset is not None
    rendered = render_clarification_card(cset)
    # oracle_bip_report_path and sql_server_table are optional
    assert "optional" in rendered.lower()
