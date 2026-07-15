"""Unit tests for the cleanup-pipeline scaffolder (Z1, 2026-05-23).

Pure-builder coverage — no DB / fs. Verifies the workflow shape the
Editor's import flow expects: 3 steps, 2 connections, empty Wrangler,
source params correct for each file format, sane sink defaults.

Z32 (2026-05-23): connection-side scaffolder removed with its UI wand;
this file now exercises only the file-side path (Storage Z1).
"""

from __future__ import annotations

import pytest

from fpulse.datastore.scaffold import (
    build_file_cleanup_workflow,
    suggest_schema_from_connection,
    suggest_table_name,
)


# ── Snake-casing helpers ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Orders Q1 2026.csv", "orders_q1_2026"),
        ("people-1000.csv", "people_1000"),
        ("ALL_CAPS.json", "all_caps"),
        ("  weird   spaces.parquet", "weird_spaces"),
        ("emoji-😀-tags.csv", "emoji_tags"),
        ("public.orders", "public_orders"),  # qualified DB name
        ("", "untitled"),
    ],
)
def test_suggest_table_name(raw: str, expected: str):
    assert suggest_table_name(raw) == expected


def test_suggest_schema_from_connection():
    assert suggest_schema_from_connection("Sales Postgres (Prod)") == "sales_postgres_prod"
    assert suggest_schema_from_connection("") == "default"


# ── File-cleanup workflow shape ─────────────────────────────────────────


def test_file_cleanup_csv_shape():
    wf = build_file_cleanup_workflow(
        workspace_id="ws1",
        file_name="orders.csv",
        file_path="uploads/ws1/orders.csv",
        file_format="csv",
    )
    # Three steps in canonical order — all use the MODERN generic
    # `source` step type (post-Z17 fix). Format is selected via the
    # connector_type param so the canvas opens without triggering the
    # legacy-source migration in migrateLegacyNodes.ts.
    assert len(wf["steps"]) == 3
    assert [s["type"] for s in wf["steps"]] == [
        "source",
        "data_wrangler",
        "local_table_sink",
    ]
    # Two connections forming a linear DAG
    assert len(wf["connections"]) == 2
    src_id = wf["steps"][0]["id"]
    wrg_id = wf["steps"][1]["id"]
    snk_id = wf["steps"][2]["id"]
    assert wf["connections"][0]["from_step"] == src_id
    assert wf["connections"][0]["to_step"] == wrg_id
    assert wf["connections"][1]["from_step"] == wrg_id
    assert wf["connections"][1]["to_step"] == snk_id
    # CSV source got the file path + delimiter overlay + connector_type
    assert wf["steps"][0]["params"]["connector_type"] == "csv"
    assert wf["steps"][0]["params"]["file_path"] == "uploads/ws1/orders.csv"
    assert wf["steps"][0]["params"]["delimiter"] == ","
    # Wrangler starts empty so the user fills it in
    assert wf["steps"][1]["params"]["steps"] == []
    assert wf["steps"][1]["params"]["_input_step_ids"] == [src_id]
    # Sink defaults are sensible
    assert wf["steps"][2]["params"]["schema_name"] == "default"
    assert wf["steps"][2]["params"]["table_name"] == "orders"
    assert wf["steps"][2]["params"]["mode"] == "replace"
    # Top-level metadata
    assert wf["workspace_id"] == "ws1"
    assert wf["metadata"]["scaffolded_from"] == "storage_file"
    # 2026-05-25 — Data Prep redesign renamed the scaffolded workflow
    # from "Clean <file>" to "<file> data prep". The new copy is more
    # consistent with the Storage page's "Prep" row-action label and
    # the broader "data prep" vocabulary used elsewhere in the UI.
    assert "orders.csv data prep" in wf["name"]


def test_file_cleanup_json_uses_json_connector():
    wf = build_file_cleanup_workflow(
        workspace_id="ws1",
        file_name="posts.json",
        file_path="uploads/ws1/posts.json",
        file_format="json",
    )
    assert wf["steps"][0]["type"] == "source"
    assert wf["steps"][0]["params"]["connector_type"] == "json"
    assert wf["steps"][0]["params"]["format"] == "auto"


def test_file_cleanup_ndjson_uses_lines_mode():
    wf = build_file_cleanup_workflow(
        workspace_id="ws1",
        file_name="events.ndjson",
        file_path="uploads/ws1/events.ndjson",
        file_format="ndjson",
    )
    assert wf["steps"][0]["type"] == "source"
    assert wf["steps"][0]["params"]["connector_type"] == "json"
    assert wf["steps"][0]["params"]["format"] == "lines"


def test_file_cleanup_tsv_uses_tab_delim():
    wf = build_file_cleanup_workflow(
        workspace_id="ws1",
        file_name="dump.tsv",
        file_path="uploads/ws1/dump.tsv",
        file_format="tsv",
    )
    assert wf["steps"][0]["type"] == "source"
    assert wf["steps"][0]["params"]["connector_type"] == "csv"
    assert wf["steps"][0]["params"]["delimiter"] == "\t"


def test_file_cleanup_parquet_uses_parquet_connector():
    wf = build_file_cleanup_workflow(
        workspace_id="ws1",
        file_name="rows.parquet",
        file_path="uploads/ws1/rows.parquet",
        file_format="parquet",
    )
    assert wf["steps"][0]["type"] == "source"
    assert wf["steps"][0]["params"]["connector_type"] == "parquet"


def test_file_cleanup_excel_uses_excel_connector():
    wf = build_file_cleanup_workflow(
        workspace_id="ws1",
        file_name="rows.xlsx",
        file_path="uploads/ws1/rows.xlsx",
        file_format="xlsx",
    )
    assert wf["steps"][0]["type"] == "source"
    assert wf["steps"][0]["params"]["connector_type"] == "excel"


def test_file_cleanup_xml_uses_xml_connector():
    wf = build_file_cleanup_workflow(
        workspace_id="ws1",
        file_name="rows.xml",
        file_path="uploads/ws1/rows.xml",
        file_format="xml",
    )
    assert wf["steps"][0]["type"] == "source"
    assert wf["steps"][0]["params"]["connector_type"] == "xml"


def test_file_cleanup_unknown_format_falls_back_to_csv():
    wf = build_file_cleanup_workflow(
        workspace_id="ws1",
        file_name="mystery.xyz",
        file_path="uploads/ws1/mystery.xyz",
        file_format="xyz",
    )
    assert wf["steps"][0]["type"] == "source"
    assert wf["steps"][0]["params"]["connector_type"] == "csv"


def test_file_cleanup_override_target_table():
    wf = build_file_cleanup_workflow(
        workspace_id="ws1",
        file_name="orders.csv",
        file_path="uploads/ws1/orders.csv",
        file_format="csv",
        target_schema="sales",
        target_table="orders_clean",
    )
    assert wf["steps"][2]["params"]["schema_name"] == "sales"
    assert wf["steps"][2]["params"]["table_name"] == "orders_clean"


# Z32 (2026-05-23) — `test_connection_cleanup_*` cases removed with
# `build_connection_cleanup_workflow`. The file-side scaffolder below
# remains under test.


def test_step_ids_are_unique_across_calls():
    """Two consecutive scaffolds must not share step ids (uuid-derived)."""
    a = build_file_cleanup_workflow(
        workspace_id="ws1",
        file_name="x.csv",
        file_path="uploads/ws1/x.csv",
        file_format="csv",
    )
    b = build_file_cleanup_workflow(
        workspace_id="ws1",
        file_name="x.csv",
        file_path="uploads/ws1/x.csv",
        file_format="csv",
    )
    a_ids = {s["id"] for s in a["steps"]}
    b_ids = {s["id"] for s in b["steps"]}
    assert a_ids.isdisjoint(b_ids)
    assert a["id"] != b["id"]
