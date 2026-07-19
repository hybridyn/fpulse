"""Draft-clarification engine — May 17 2026.

The biggest UX gap surfaced by the 300-prompt sample run (May 17 2026):
when a user says *"Build a pipeline to fetch Oracle BIP every 6 hours and
load into SQL Server"*, the rule planner today silently guesses every
config (auth method, BIP report path, schedule precise cron, SQL Server
target table, write mode, cleaning rules). The user gets back a draft
they have to fix in 5 places.

This module changes the flow to **clarify BEFORE drafting**. Given a
build-pipeline prompt, ``detect_missing_draft_fields`` returns a list of
focused questions; the agent endpoint surfaces them as chips and only
calls the planner once the user has answered.

Sibling to ``clarify.py`` (which handles "which pipeline did you mean?"
disambiguation). This one handles "what details do you want?" for net-new
pipeline construction.

Design:
- **Heuristic, not ML.** Pure regex / keyword detection. No LLM in this
  hot path — clarification has to be sub-10ms or it adds latency to
  every build-pipeline request.
- **Question set is SMALL.** 5 question types max. More than that and
  users perceive it as "the bot is interrogating me." Each question
  must be a real blocker, not a nice-to-have.
- **Chip-friendly.** Every question has a recommended set of one-click
  answers so users don't have to type.

Public surface:
  * ``detect_missing_draft_fields(prompt) -> ClarificationSet | None``
  * ``ClarificationSet`` / ``ClarificationQuestion`` dataclasses
  * ``render_clarification_card(set) -> str`` — markdown for chat
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# ── Detection patterns ────────────────────────────────────────────────────
#
# Trigger words that tell us this is a build/create-pipeline request.
# Pattern is conservative — we'd rather miss a clarification opportunity
# than ask questions for prompts that aren't actually pipeline builds.
_BUILD_INTENT_RE = re.compile(
    r"\b(build|create|draft|make|generate|design|set\s?up|scaffold)\b"
    r".*\b(pipeline|workflow|etl|flow)\b",
    re.IGNORECASE | re.DOTALL,
)

# Source-type detection. Order matters — first match wins.
_SOURCE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("oracle_bip", re.compile(r"oracle\s*bip|bi\s*publisher", re.IGNORECASE)),
    ("oracle_fusion", re.compile(r"oracle\s*fusion|fusion\s*cloud", re.IGNORECASE)),
    ("oracle", re.compile(r"\boracle\b", re.IGNORECASE)),
    ("sap", re.compile(r"\bsap\b", re.IGNORECASE)),
    ("salesforce", re.compile(r"salesforce|sfdc", re.IGNORECASE)),
    ("servicenow", re.compile(r"servicenow|service\s*now", re.IGNORECASE)),
    ("rest_api", re.compile(r"rest\s*api|http\s*api|web\s*api", re.IGNORECASE)),
    ("api", re.compile(r"\bapi\b", re.IGNORECASE)),
    ("postgres", re.compile(r"postgres|postgresql", re.IGNORECASE)),
    ("mysql", re.compile(r"\bmysql\b", re.IGNORECASE)),
    ("sql_server_source", re.compile(r"sql\s*server.*(source|from)", re.IGNORECASE)),
    ("csv_file", re.compile(r"\bcsv\b|\.csv\b", re.IGNORECASE)),
    ("excel_file", re.compile(r"\bexcel\b|xlsx|\.xls\b", re.IGNORECASE)),
    ("json_file", re.compile(r"\bjson\b|\.json\b", re.IGNORECASE)),
    ("xml_file", re.compile(r"\bxml\b|\.xml\b", re.IGNORECASE)),
    ("s3", re.compile(r"\bs3\b|amazon\s*s3", re.IGNORECASE)),
    ("kafka", re.compile(r"\bkafka\b|redpanda", re.IGNORECASE)),
    ("sftp", re.compile(r"sftp|ftp\s|\bftp\b", re.IGNORECASE)),
)

# Sink-type detection — same pattern, different keywords.
_SINK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("sql_server", re.compile(r"sql\s*server|mssql|t-sql", re.IGNORECASE)),
    ("postgres", re.compile(r"postgres|postgresql", re.IGNORECASE)),
    ("mysql", re.compile(r"\bmysql\b", re.IGNORECASE)),
    ("snowflake", re.compile(r"snowflake", re.IGNORECASE)),
    ("bigquery", re.compile(r"big\s*query|gbq", re.IGNORECASE)),
    ("redshift", re.compile(r"redshift", re.IGNORECASE)),
    ("delta_lake", re.compile(r"delta\s*lake|delta\s*table", re.IGNORECASE)),
    ("parquet", re.compile(r"parquet", re.IGNORECASE)),
    ("warehouse", re.compile(r"warehouse|data\s*warehouse", re.IGNORECASE)),
    ("s3", re.compile(r"\bs3\b", re.IGNORECASE)),
    ("database", re.compile(r"\bdatabase\b|\bdb\b", re.IGNORECASE)),
)

# Schedule explicitly stated (don't ask if user already said it).
_SCHEDULE_MENTIONED_RE = re.compile(
    r"\b(every|daily|hourly|weekly|monthly|on\s+demand|schedule|cron|"
    r"each\s+day|each\s+hour|nightly|morning|evening|interval)\b",
    re.IGNORECASE,
)

# Write-mode mentioned (insert/upsert/merge/append/overwrite/truncate).
_WRITE_MODE_MENTIONED_RE = re.compile(
    r"\b(insert|upsert|merge|append|overwrite|truncate|replace|update)\b",
    re.IGNORECASE,
)

# Cleaning rules mentioned (filter X, remove Y, validate Z, dedupe by W).
_CLEANING_DETAIL_RE = re.compile(
    r"\b(filter|remove|drop|deduplicate|dedup|trim|normalize|standardize|"
    r"mask|encrypt|hash|cast|convert|where|status\s*=|null|validate)\b",
    re.IGNORECASE,
)


# ── Data shapes ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ClarificationQuestion:
    """One question to put to the user."""
    field: str              # internal key, e.g. "source_auth"
    question: str           # display text
    chips: tuple[str, ...]  # one-click answer options; first chip = recommended
    required: bool = True   # if False, "Skip" is also a valid answer


@dataclass(frozen=True)
class ClarificationSet:
    """The full clarification ask for one prompt."""
    source_type: str | None     # detected source kind ("oracle_bip", "csv_file", …)
    sink_type: str | None       # detected sink kind ("sql_server", "parquet", …)
    questions: tuple[ClarificationQuestion, ...]
    detected_intent: str        # short echo back to user
    original_prompt: str        # round-trip back to planner after answers


# ── Question generators per source/sink type ──────────────────────────────


def _questions_for_oracle_bip() -> list[ClarificationQuestion]:
    """Oracle BIP is a two-step report API; auth + report path are both
    required. Provide chip-style answers covering the common cases."""
    return [
        ClarificationQuestion(
            field="oracle_bip_auth",
            question="How should F-Pulse authenticate to Oracle BIP?",
            chips=(
                "Basic auth (username + password)",
                "OAuth2 token",
                "Session cookie",
                "I'll configure later",
            ),
        ),
        ClarificationQuestion(
            field="oracle_bip_report_path",
            question="What's the Oracle BIP report path (e.g. /Custom/HR/Workers)?",
            chips=("I'll fill in later",),
            required=False,
        ),
        ClarificationQuestion(
            field="oracle_bip_format",
            question="What output format does the BIP report return?",
            chips=("XML (most common)", "CSV", "JSON", "Excel"),
        ),
    ]


def _questions_for_rest_api() -> list[ClarificationQuestion]:
    return [
        ClarificationQuestion(
            field="api_auth",
            question="How does the API authenticate?",
            chips=(
                "Bearer token",
                "Basic auth",
                "API key in header",
                "OAuth2",
                "No auth (public)",
            ),
        ),
        ClarificationQuestion(
            field="api_pagination",
            question="Does the API paginate?",
            chips=("Yes — page param", "Yes — cursor / next_token", "No pagination"),
        ),
    ]


def _questions_for_sql_server_sink() -> list[ClarificationQuestion]:
    """SQL Server sink needs connection + table + write mode minimum."""
    return [
        ClarificationQuestion(
            field="sql_server_connection",
            question="Which SQL Server connection should I write to? (Or create one in **Connections** first.)",
            chips=("Use existing connection", "I'll set this up after the draft"),
        ),
        ClarificationQuestion(
            field="sql_server_write_mode",
            question="How should the data land in SQL Server?",
            chips=(
                "Append (insert new rows)",
                "Upsert / Merge (update if key exists)",
                "Overwrite (truncate + load)",
                "Staging table + MERGE statement",
            ),
        ),
        ClarificationQuestion(
            field="sql_server_table",
            question="What's the target table name?",
            chips=("I'll fill in later",),
            required=False,
        ),
    ]


def _questions_for_db_sink() -> list[ClarificationQuestion]:
    """Generic SQL DB sink — slightly lighter than SQL-Server-specific."""
    return [
        ClarificationQuestion(
            field="db_write_mode",
            question="How should the data land?",
            chips=("Append", "Upsert by key", "Overwrite (truncate + load)"),
        ),
        ClarificationQuestion(
            field="db_connection",
            question="Which database connection should I write to?",
            chips=("Use existing connection", "I'll configure after the draft"),
        ),
    ]


def _questions_for_schedule() -> list[ClarificationQuestion]:
    """User said something like 'on a schedule' or 'periodically' but
    didn't pin the interval. Common chips cover the 80% case."""
    return [
        ClarificationQuestion(
            field="schedule_interval",
            question="How often should this pipeline run?",
            chips=(
                "Every hour",
                "Every 6 hours",
                "Daily at 2 AM UTC",
                "Daily during business hours",
                "Weekly (Mondays)",
                "On demand only — no schedule",
            ),
        ),
    ]


def _questions_for_cleaning() -> list[ClarificationQuestion]:
    return [
        ClarificationQuestion(
            field="cleaning_rules",
            question="What cleaning should I apply before writing?",
            chips=(
                "Just remove duplicates",
                "Drop null rows + dedupe",
                "Full data-quality rules (I'll define them)",
                "No cleaning — load as-is",
            ),
        ),
    ]


# ── Main entry point ──────────────────────────────────────────────────────


def detect_missing_draft_fields(prompt: str) -> ClarificationSet | None:
    """Inspect a build-pipeline prompt and return the questions to ask
    before drafting. Returns None when the prompt is either:
      * not a build-pipeline request, or
      * already specifies enough detail to draft directly.

    Sub-10ms. No LLM, no I/O, no tool calls.
    """
    if not prompt or not _BUILD_INTENT_RE.search(prompt):
        return None

    p_lower = prompt.lower()

    # Detect source + sink types.
    source_type: str | None = None
    for name, pat in _SOURCE_PATTERNS:
        if pat.search(p_lower):
            source_type = name
            break

    sink_type: str | None = None
    for name, pat in _SINK_PATTERNS:
        if pat.search(p_lower):
            sink_type = name
            break

    questions: list[ClarificationQuestion] = []

    # Source-specific questions.
    if source_type == "oracle_bip":
        questions.extend(_questions_for_oracle_bip())
    elif source_type in ("api", "rest_api"):
        questions.extend(_questions_for_rest_api())
    elif source_type == "oracle":
        # Generic Oracle — ask whether it's BIP or direct DB.
        questions.append(ClarificationQuestion(
            field="oracle_kind",
            question="Which Oracle source is this?",
            chips=("Oracle BIP report API", "Oracle Fusion REST", "Oracle DB (direct SQL)"),
        ))

    # Sink-specific questions.
    if sink_type == "sql_server":
        questions.extend(_questions_for_sql_server_sink())
    elif sink_type in ("postgres", "mysql", "snowflake", "bigquery", "redshift",
                       "warehouse", "database"):
        questions.extend(_questions_for_db_sink())

    # Schedule question — only if the user said this should be recurring
    # but didn't pin an interval.
    schedule_hint_re = re.compile(
        r"\b(recurring|periodically|automatically|on\s+a\s+schedule|"
        r"on\s+intervals?|regularly|continuously)\b",
        re.IGNORECASE,
    )
    if schedule_hint_re.search(p_lower) and not _SCHEDULE_MENTIONED_RE.search(p_lower):
        questions.extend(_questions_for_schedule())

    # Cleaning — user said "clean" but didn't say what to clean.
    if "clean" in p_lower and not _CLEANING_DETAIL_RE.search(p_lower):
        questions.extend(_questions_for_cleaning())

    if not questions:
        return None

    detected = _build_intent_summary(source_type, sink_type)
    return ClarificationSet(
        source_type=source_type,
        sink_type=sink_type,
        questions=tuple(questions),
        detected_intent=detected,
        original_prompt=prompt,
    )


def _build_intent_summary(source_type: str | None, sink_type: str | None) -> str:
    """One-line echo so the user knows we understood the request."""
    src = source_type or "an unknown source"
    dst = sink_type or "an unknown destination"
    return f"Build a pipeline from **{src}** to **{dst}**"


# ── Markdown rendering ────────────────────────────────────────────────────


def render_clarification_card(cset: ClarificationSet) -> str:
    """Render a ClarificationSet as the markdown card the chat panel
    shows. Each question gets a `### Q1:` heading and its chips listed as
    one-click options. Caller wraps these chips in the existing
    AgentChatPanel chip renderer.

    The first chip in each question's list is marked as the recommended
    answer (per `_questions_for_*` ordering)."""
    lines: list[str] = [
        f"_{cset.detected_intent}._ A few quick questions before I draft it:\n",
    ]
    for i, q in enumerate(cset.questions, start=1):
        opt = "" if q.required else " _(optional — say 'skip')_"
        lines.append(f"**Q{i}.** {q.question}{opt}")
        for chip in q.chips:
            lines.append(f"  • {chip}")
        lines.append("")
    lines.append(
        "_Reply with the option numbers (e.g. `1, 2a, 3`) or paste the answers in any order — "
        "I'll draft the pipeline once I have what I need._"
    )
    return "\n".join(lines)
