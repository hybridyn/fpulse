"""Clarify-answers → enterprise-template bridge (Phase 2F, May 18 2026).

Connects two features that shipped on May 17 2026 but were left
disconnected:

  * `clarify_draft.py` detects "build a pipeline from X to Y" prompts
    and asks focused config questions before drafting.
  * `templates.py` ships 4 hardened enterprise scaffolds (Oracle BIP →
    SQL Server, SQL Server upsert/MERGE, SCD2 dimension, CDC).

Today's gap: when the user answers the clarification questions, the
rule planner runs on whatever they typed and falls back to generic
scaffolding. The Oracle BIP template only fires when the user
explicitly picks it from the Templates page.

This module bridges them:

  1. ``match_template_from_intent(source_type, sink_type)``
     — pure mapping from detected types → template key.
  2. ``parse_answers_freeform(text, questions)``
     — best-effort regex extraction of answer values from the user's
     follow-up message. Returns a ``{question_field: value}`` dict.
  3. ``populate_template(template_key, answers)``
     — calls ``create_from_template()`` then replaces ``<your-...>``
     placeholders with the parsed answer values where possible.

Honest scope of v1:
  * Answer parsing is regex-based. Users typing structured replies
    ("auth: basic, format: xml, write_mode: upsert") get clean
    extraction. Free-form prose works for the common cases but isn't
    perfect.
  * Template selection is binary — either we recognise the source+sink
    pair, or we don't. No fuzzy / partial matches.
  * Placeholders the user didn't answer are LEFT intact (still
    ``<your-...>``) so the pipeline won't validate until they're
    filled. That's the safety contract.

This module is pure-Python, no LLM, no I/O — same hot-path discipline
as ``clarify_draft.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from fpulse.ai.clarify_draft import ClarificationQuestion


# ── Template selection table ──────────────────────────────────────────────
#
# Maps (source_type, sink_type) → template key. The detected types come
# from clarify_draft's _SOURCE_PATTERNS / _SINK_PATTERNS. Add new pairs
# here as new templates are added to templates.py.
_TEMPLATE_SELECTION: dict[tuple[str | None, str | None], str] = {
    ("oracle_bip", "sql_server"): "oracle_bip_to_sql_server",
    # Generic upsert pattern works for any source → SQL Server when the
    # write mode says "upsert" / "merge" — see _is_upsert_request().
    # That dynamic case is handled in match_template_from_intent_and_answers
    # rather than the static table.
}


def match_template_from_intent(
    source_type: str | None,
    sink_type: str | None,
) -> str | None:
    """Static source/sink → template lookup. Returns None when no
    template matches the pair."""
    return _TEMPLATE_SELECTION.get((source_type, sink_type))


def match_template_from_intent_and_answers(
    source_type: str | None,
    sink_type: str | None,
    answers: dict[str, str],
) -> str | None:
    """Smarter template picker — considers user answers too.

    Beyond the static pair lookup, this picks:
      * `sql_server_upsert` when the sink is SQL Server AND the user
        answered upsert / merge for the write-mode question.
      * `scd2_dimension` when the user mentions SCD2 / type 2 / history
        in any answer (covers prompts that pre-empted the question).
      * `cdc_incremental` when the user mentions CDC / change-data-capture
        / debezium in any answer.

    Falls back to the static table when none of the dynamic rules fire.
    """
    # Dynamic rules first — they're more specific than the static pairs.
    answers_blob = " ".join(answers.values()).lower()

    if re.search(r"\bscd2\b|type\s*2|slowly\s*changing", answers_blob):
        return "scd2_dimension"
    if re.search(r"\bcdc\b|change\s*data\s*capture|debezium", answers_blob):
        return "cdc_incremental"

    # SQL Server + upsert intent → the generic upsert template.
    if sink_type == "sql_server":
        write_mode = answers.get("sql_server_write_mode", "").lower()
        if any(w in write_mode for w in ("upsert", "merge", "update if")):
            # If the source is specifically Oracle BIP, the dedicated template
            # is richer (includes XML flatten + DQ split + MERGE). Use it.
            if source_type == "oracle_bip":
                return "oracle_bip_to_sql_server"
            return "sql_server_upsert"

    # Fall back to the static table.
    return match_template_from_intent(source_type, sink_type)


# ── Answer parsing ────────────────────────────────────────────────────────


# Common synonyms for each chip option text → canonical token.
# Keeps the matching robust against minor wording variations.
_AUTH_SYNONYMS = {
    "basic": "basic",
    "username": "basic",
    "password": "basic",
    "oauth": "oauth2",
    "oauth2": "oauth2",
    "token": "oauth2",  # bearer token
    "bearer": "oauth2",
    "cookie": "cookie",
    "session": "cookie",
    "api key": "api_key",
    "api-key": "api_key",
    "no auth": "none",
    "public": "none",
}

_FORMAT_SYNONYMS = {
    "xml": "xml",
    "csv": "csv",
    "json": "json",
    "excel": "excel",
    "xlsx": "excel",
}

_WRITE_MODE_SYNONYMS = {
    "append": "append",
    "insert": "append",
    "upsert": "upsert",
    "merge": "upsert",
    "update": "upsert",
    "overwrite": "overwrite",
    "truncate": "overwrite",
    "replace": "overwrite",
    "staging": "staging_merge",
    "stage": "staging_merge",
}


@dataclass(frozen=True)
class ParsedAnswers:
    """Parsed answer set returned by parse_answers_freeform."""
    values: dict[str, str]            # field name → canonical value
    matched_fields: tuple[str, ...]   # fields that got a hit
    unmatched_fields: tuple[str, ...] # fields with no detected answer


def parse_answers_freeform(
    text: str,
    questions: tuple[ClarificationQuestion, ...],
) -> ParsedAnswers:
    """Extract answers from a free-form user reply.

    Strategy:
      1. For each question, scan the text for synonym keywords matching
         its chip options.
      2. Also detect explicit "field: value" assignments
         (e.g., "auth: basic, format: xml").
      3. URLs / table names / connection names get picked up via regex
         when the corresponding field is one of the placeholder questions.

    Returns a ParsedAnswers — caller knows which questions still need
    answers (unmatched_fields).
    """
    if not text or not questions:
        return ParsedAnswers(
            values={},
            matched_fields=(),
            unmatched_fields=tuple(q.field for q in questions),
        )

    t_lower = text.lower()
    values: dict[str, str] = {}

    # ── Synonym scan per question field ────────────────────────────────
    for q in questions:
        field = q.field

        # Auth-style fields: pick the first matching auth synonym.
        if field in ("oracle_bip_auth", "api_auth"):
            for key, canon in _AUTH_SYNONYMS.items():
                if key in t_lower:
                    values[field] = canon
                    break

        # Format-style field.
        elif field == "oracle_bip_format":
            for key, canon in _FORMAT_SYNONYMS.items():
                if re.search(rf"\b{re.escape(key)}\b", t_lower):
                    values[field] = canon
                    break

        # Write-mode fields.
        elif field in ("sql_server_write_mode", "db_write_mode"):
            for key, canon in _WRITE_MODE_SYNONYMS.items():
                if re.search(rf"\b{re.escape(key)}\b", t_lower):
                    values[field] = canon
                    break

        # Oracle "which kind".
        elif field == "oracle_kind":
            if re.search(r"\bbip\b|publisher", t_lower):
                values[field] = "oracle_bip"
            elif "fusion" in t_lower:
                values[field] = "oracle_fusion"
            elif re.search(r"\bdb\b|direct|sql", t_lower):
                values[field] = "oracle_db"

        # Schedule interval — chip-style ("every 6 hours") or cron.
        elif field == "schedule_interval":
            m = re.search(
                r"every\s+(\d+)\s+(minute|minutes|hour|hours|day|days|week|weeks)",
                t_lower,
            )
            if m:
                values[field] = f"every {m.group(1)} {m.group(2)}"
            elif "daily" in t_lower:
                values[field] = "daily"
            elif "hourly" in t_lower:
                values[field] = "hourly"
            elif "weekly" in t_lower or "monday" in t_lower:
                values[field] = "weekly"

        # Cleaning rules — picks the chip option that matches.
        elif field == "cleaning_rules":
            if re.search(r"no cleaning|as.?is", t_lower):
                values[field] = "none"
            elif re.search(r"full|all rules|define them", t_lower):
                values[field] = "custom"
            elif re.search(r"null.*dedup|drop null", t_lower):
                values[field] = "nulls_and_dedupe"
            elif re.search(r"duplicat", t_lower):
                values[field] = "dedupe_only"

        # Connection name / report path / table name — free text after
        # a label, or in quotes.
        elif field in (
            "sql_server_connection", "db_connection",
            "sql_server_table", "oracle_bip_report_path",
        ):
            # Look for an explicit field-name marker, then take the
            # next quoted or backticked token. Each field has a list of
            # likely "short names" the user types — e.g. for
            # `sql_server_connection` the user writes "connection: ..."
            # not "server_connection: ...". Try each, longest first so
            # multi-word labels win over their suffix.
            _SHORT_NAMES: dict[str, tuple[str, ...]] = {
                "sql_server_connection": ("connection",),
                "db_connection": ("connection",),
                "sql_server_table": ("table",),
                "oracle_bip_report_path": ("report path", "report_path", "path"),
            }
            short_names = _SHORT_NAMES.get(field, (field.rsplit("_", 1)[-1],))
            found = False
            for short_name in short_names:
                sn = re.escape(short_name)
                patterns = [
                    rf"{sn}\s*[:=]\s*['\"]([^'\"]+)['\"]",
                    rf"{sn}\s*[:=]\s*`([^`]+)`",
                    rf"{sn}\s*[:=]\s*([A-Za-z0-9_./-]+)",
                    # No-separator phrasings: `connection "prod-mssql"` /
                    # `table "dbo.employees"`. Common in user-typed
                    # sentences without colons.
                    rf"{sn}\s+['\"]([^'\"]+)['\"]",
                    rf"{sn}\s+`([^`]+)`",
                ]
                for pat in patterns:
                    m = re.search(pat, text, re.IGNORECASE)
                    if m:
                        values[field] = m.group(1).strip()
                        found = True
                        break
                if found:
                    break

    matched = tuple(q.field for q in questions if q.field in values)
    unmatched = tuple(q.field for q in questions if q.field not in values)
    return ParsedAnswers(values=values, matched_fields=matched, unmatched_fields=unmatched)


# ── Template population ───────────────────────────────────────────────────


# Map: template_key → {answer_field → set of placeholder strings to replace}.
# Each placeholder gets replaced with the parsed answer value across all
# step params (string and list values). Placeholders left in the template
# are intentional — they enforce the "user must fill in real config" rule.
_PLACEHOLDER_MAPPING: dict[str, dict[str, tuple[str, ...]]] = {
    "oracle_bip_to_sql_server": {
        "sql_server_connection": ("<your-sql-server-connection>",),
        "sql_server_table": ("<your-staging-table>", "<your-target-table>"),
        "oracle_bip_report_path": ("<your-bip-report-path>",),
    },
    "sql_server_upsert": {
        "sql_server_connection": ("<your-sql-server-connection>",),
        "sql_server_table": ("<your-staging-table>", "<your-target-table>"),
    },
    "scd2_dimension": {
        "db_connection": ("<your-sql-server-connection>",),
        "sql_server_table": ("<your-dimension-table>",),
    },
    "cdc_incremental": {
        "db_connection": ("<your-target-connection>",),
        "sql_server_table": ("<your-target-table>",),
    },
}


def populate_template(template_key: str, answers: dict[str, str]):
    """Build a workflow from the named template, then substitute
    placeholder strings with values from the parsed answers.

    Placeholders without a matching answer stay as ``<your-...>`` so
    the user is forced to fill them in (safety contract). Returns the
    resulting Workflow, or None when the template key is unknown.
    """
    from fpulse.planner.templates import create_from_template

    wf = create_from_template(template_key)
    if wf is None:
        return None

    mapping = _PLACEHOLDER_MAPPING.get(template_key, {})
    if not mapping or not answers:
        # Nothing to substitute — return the template as-is. Caller can
        # still surface unfilled placeholders to the user.
        return wf

    # Build a flat placeholder → value map for fast string replacement.
    replacements: dict[str, str] = {}
    for answer_field, placeholders in mapping.items():
        value = answers.get(answer_field)
        if not value:
            continue
        for ph in placeholders:
            replacements[ph] = value

    if not replacements:
        return wf

    # Walk every step's params, substituting in any string / list-of-strings
    # values. We mutate in place; the workflow object is freshly created.
    for step in wf.steps:
        params = step.params or {}
        for key, val in list(params.items()):
            if isinstance(val, str):
                new_val = val
                for ph, new in replacements.items():
                    new_val = new_val.replace(ph, new)
                if new_val != val:
                    params[key] = new_val
            elif isinstance(val, list):
                new_list = []
                changed = False
                for item in val:
                    if isinstance(item, str):
                        new_item = item
                        for ph, new in replacements.items():
                            new_item = new_item.replace(ph, new)
                        if new_item != item:
                            changed = True
                        new_list.append(new_item)
                    else:
                        new_list.append(item)
                if changed:
                    params[key] = new_list

    return wf
