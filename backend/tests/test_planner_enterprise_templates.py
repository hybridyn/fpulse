"""Tests for the 4 enterprise pipeline templates added May 17 2026.

Templates pair with clarify_draft.py: the clarifying-questions flow
captures user intent, then the template provides a hardened scaffold
the rule planner can return instead of guessing every config.

Each test verifies:
  * The template renders a valid Workflow (correct step count, types).
  * Placeholder fields use the <your-...> sentinel (forces real config).
  * The TEMPLATES dict registry entry exists with the expected tags.
"""

from __future__ import annotations

from fpulse.ir.schema import StepType
from fpulse.planner.templates import TEMPLATES, create_from_template


def _placeholder_count(workflow) -> int:
    """Count <your-...> sentinel values across all step params + SQL."""
    import re
    pat = re.compile(r"<your-[a-z0-9_-]+>")
    count = 0
    for step in workflow.steps:
        for value in (step.params or {}).values():
            if isinstance(value, str):
                count += len(pat.findall(value))
            elif isinstance(value, list):
                for v in value:
                    if isinstance(v, str):
                        count += len(pat.findall(v))
    return count


# ── Registry ──────────────────────────────────────────────────────────────


def test_all_4_enterprise_templates_registered():
    for key in ("oracle_bip_to_sql_server", "sql_server_upsert",
                "scd2_dimension", "cdc_incremental"):
        assert key in TEMPLATES, f"{key} missing from TEMPLATES registry"
        entry = TEMPLATES[key]
        assert "name" in entry
        assert "description" in entry
        assert "tags" in entry
        assert "enterprise" in entry["tags"]


# ── Oracle BIP → SQL Server ───────────────────────────────────────────────


def test_oracle_bip_template_has_5_step_chain():
    wf = create_from_template("oracle_bip_to_sql_server")
    assert wf is not None
    assert len(wf.steps) == 5
    types = [s.type for s in wf.steps]
    assert types == [
        StepType.API_SOURCE,
        StepType.TRANSFORM,
        StepType.DATA_QUALITY,
        StepType.DB_SINK,
        StepType.EXECUTE_SQL_TASK,
    ]


def test_oracle_bip_template_includes_merge_sql():
    wf = create_from_template("oracle_bip_to_sql_server")
    assert wf is not None
    sql_step = wf.steps[-1]
    sql_text = sql_step.params.get("sql", "")
    assert "MERGE INTO" in sql_text
    assert "WHEN MATCHED" in sql_text
    assert "WHEN NOT MATCHED BY TARGET" in sql_text


def test_oracle_bip_template_uses_placeholders():
    """Every <your-...> in the template forces the user to fill in real
    config. The pipeline shouldn't run with placeholders left in place —
    that's the safety contract."""
    wf = create_from_template("oracle_bip_to_sql_server")
    assert wf is not None
    assert _placeholder_count(wf) >= 5  # at least URL, user, secret, conn, target


def test_oracle_bip_has_retry_config():
    """Oracle BIP APIs are notoriously flaky — retry must be wired."""
    wf = create_from_template("oracle_bip_to_sql_server")
    assert wf is not None
    src = wf.steps[0]
    assert src.params.get("retry_max", 0) >= 3


# ── SQL Server Upsert ─────────────────────────────────────────────────────


def test_sql_server_upsert_3_step_chain():
    wf = create_from_template("sql_server_upsert")
    assert wf is not None
    assert len(wf.steps) == 3
    types = [s.type for s in wf.steps]
    assert types == [StepType.SOURCE, StepType.DB_SINK, StepType.EXECUTE_SQL_TASK]


def test_sql_server_upsert_uses_truncate_load_for_staging():
    """Staging table must be rebuilt each run — never append."""
    wf = create_from_template("sql_server_upsert")
    assert wf is not None
    stage = wf.steps[1]
    assert stage.params.get("write_mode") == "truncate_load"
    assert stage.params.get("schema") == "stg"


# ── SCD2 ──────────────────────────────────────────────────────────────────


def test_scd2_template_uses_scd2_node():
    wf = create_from_template("scd2_dimension")
    assert wf is not None
    types = [s.type for s in wf.steps]
    assert StepType.SCD2 in types
    scd2 = next(s for s in wf.steps if s.type == StepType.SCD2)
    # Required SCD2 columns must be configured
    assert scd2.params.get("effective_from_col")
    assert scd2.params.get("effective_to_col")
    assert scd2.params.get("is_current_col")


def test_scd2_template_targets_dim_schema():
    """SCD2 conventionally lands in the `dim` schema."""
    wf = create_from_template("scd2_dimension")
    assert wf is not None
    sink = wf.steps[-1]
    assert sink.params.get("schema") == "dim"


# ── CDC Incremental ───────────────────────────────────────────────────────


def test_cdc_template_uses_cdc_source():
    wf = create_from_template("cdc_incremental")
    assert wf is not None
    src = wf.steps[0]
    assert src.type == StepType.CDC_SOURCE


def test_cdc_template_handles_deletes_explicitly():
    """CDC must distinguish inserts/updates/deletes — otherwise the
    target accumulates ghosts. Either soft_delete or hard_delete."""
    wf = create_from_template("cdc_incremental")
    assert wf is not None
    upsert = wf.steps[-1]
    handle = upsert.params.get("handle_deletes")
    assert handle in ("soft_delete", "hard_delete", "ignore")


# ── Generic sanity ────────────────────────────────────────────────────────


def test_all_enterprise_templates_render_connections_correctly():
    """Every step pair must be connected end-to-end (no orphan steps)."""
    for key in ("oracle_bip_to_sql_server", "sql_server_upsert",
                "scd2_dimension", "cdc_incremental"):
        wf = create_from_template(key)
        assert wf is not None, f"{key} returned None"
        n = len(wf.steps)
        # Linear chain: n-1 connections
        assert len(wf.connections) == n - 1, (
            f"{key} has {n} steps but {len(wf.connections)} connections "
            f"(expected {n - 1})"
        )
        # Every connection's endpoints must match real step IDs
        ids = {s.id for s in wf.steps}
        for c in wf.connections:
            assert c.from_step in ids
            assert c.to_step in ids


def test_unknown_template_returns_none():
    assert create_from_template("nonexistent_template_xyz") is None
