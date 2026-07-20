"""
Auto-generated atlas topics from existing repo sources.

Each generator reads a structured source and emits Topic objects.
This keeps the atlas in sync with the rest of the repo automatically:

  * Add a new tool to ``INITIAL_TOOLS`` → atlas gains a ``tool.<name>``
    topic on next process start.
  * Add a new ``StepType`` enum member → atlas gains a ``node.<name>``
    topic.
  * Drop a new ``.json`` into ``backend/fpulse/connectors/manifests/``
    → atlas gains a ``connector.<id>`` topic.
  * Drop a new ``.md`` into ``docs/`` → atlas gains a ``doc.<basename>``
    topic seeded with the first H1.

All generators are defensive — one bad manifest file shouldn't break
atlas load. Errors are logged and the affected entry is skipped.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from .schema import Tier, Topic, TopicCategory

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Tools — read INITIAL_TOOLS
# ─────────────────────────────────────────────────────────────────────


def _tool_topics() -> list[Topic]:
    """One topic per ToolDefinition in INITIAL_TOOLS. Aliases derived
    from the tool name + a couple of natural-language framings."""
    out: list[Topic] = []
    try:
        from fpulse.ai.tools import INITIAL_TOOLS
    except Exception as exc:  # noqa: BLE001
        logger.warning("Atlas autogen: failed to import INITIAL_TOOLS: %s", exc)
        return out
    for tool in INITIAL_TOOLS:
        name = tool.name
        # Pretty form: "list_pipelines" → "List Pipelines"
        pretty = " ".join(w.capitalize() for w in name.split("_"))
        aliases = (
            name.lower(),
            f"{name.lower()} tool",
            f"what does {name.lower()} do",
            f"{pretty.lower()} tool",
        )
        body = (
            f"**{pretty}** is one of the Copilot's backend tools (`{name}`). "
            f"{tool.description.strip()} "
            f"Tier: `{tool.tier.value if hasattr(tool.tier, 'value') else tool.tier}`. "
            f"The Copilot calls this automatically when it judges the user's "
            f"question fits — you can also reference it explicitly."
        )
        out.append(Topic(
            id=f"tool.{name.lower()}",
            category=TopicCategory.TOOL,
            title=f"Tool: {pretty}",
            aliases=aliases,
            body=body,
            tier=Tier.OSS,
            source="auto:INITIAL_TOOLS",
        ))
    return out


# ─────────────────────────────────────────────────────────────────────
# Node types — read StepType enum + the inline comments in ir/schema.py
# ─────────────────────────────────────────────────────────────────────


def _node_topics() -> list[Topic]:
    """One topic per StepType enum member. Descriptions pulled from the
    inline ``# comment`` after each enum declaration in ir/schema.py."""
    out: list[Topic] = []
    try:
        from fpulse.ir.schema import StepType
    except Exception as exc:  # noqa: BLE001
        logger.warning("Atlas autogen: failed to import StepType: %s", exc)
        return out

    # Parse inline comments out of the source file. Each enum line looks
    # like: ``    FILTER = "filter"      # Drops rows that don't match…``
    descriptions: dict[str, str] = {}
    try:
        schema_path = Path(__file__).parent.parent.parent / "ir" / "schema.py"
        if schema_path.exists():
            for line in schema_path.read_text(encoding="utf-8").splitlines():
                m = re.match(
                    r'^\s*([A-Z_][A-Z_0-9]*)\s*=\s*"([^"]+)"(?:\s*#\s*(.+))?',
                    line,
                )
                if m:
                    member_name = m.group(1)
                    inline_comment = (m.group(3) or "").strip()
                    if inline_comment:
                        descriptions[member_name] = inline_comment
    except Exception as exc:  # noqa: BLE001
        logger.warning("Atlas autogen: StepType comment parse failed: %s", exc)

    for member in StepType:
        value = member.value
        member_name = member.name
        desc = descriptions.get(member_name) or "A pipeline node type."
        # Pretty form: "csv_source" → "CSV Source"
        pretty = " ".join(w.upper() if len(w) <= 3 else w.capitalize() for w in value.split("_"))
        aliases = (
            value,
            f"{value} node",
            f"{value} step",
            f"what is {value}",
            f"how does {value} work",
        )
        body = (
            f"**{pretty} node** (`{value}`). {desc} "
            f"Drag it onto the canvas from the Editor's node palette, then configure "
            f"it in the right-hand panel."
        )
        out.append(Topic(
            id=f"node.{value}",
            category=TopicCategory.NODE,
            title=f"Node: {pretty}",
            aliases=aliases,
            body=body,
            tier=Tier.OSS,
            source="auto:StepType",
        ))
    return out


# ─────────────────────────────────────────────────────────────────────
# Connectors — read manifest JSON files
# ─────────────────────────────────────────────────────────────────────


def _connector_topics() -> list[Topic]:
    """One topic per connector manifest JSON. Reads `id`, `name`,
    `description`, `category` directly. v2 variants are skipped (the
    base manifest already covers the connector)."""
    out: list[Topic] = []
    try:
        manifests_dir = (
            Path(__file__).parent.parent.parent
            / "connectors" / "manifests"
        )
        if not manifests_dir.exists():
            logger.warning("Atlas autogen: manifests dir not found: %s", manifests_dir)
            return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("Atlas autogen: failed to locate manifests dir: %s", exc)
        return out

    # Skip v2 variants — base manifests cover the connector. v2 is an
    # internal migration concept, not an extra connector.
    seen_ids: set[str] = set()
    for manifest_path in sorted(manifests_dir.glob("*.json")):
        if ".v2.json" in manifest_path.name:
            continue
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Atlas autogen: failed to parse %s: %s", manifest_path.name, exc,
            )
            continue
        conn_id = str(data.get("id") or manifest_path.stem).lower()
        if conn_id in seen_ids:
            continue
        seen_ids.add(conn_id)
        name = str(data.get("name") or conn_id.title())
        desc = str(data.get("description") or "").strip()
        category = str(data.get("category") or "other")
        aliases = (
            conn_id,
            name.lower(),
            f"{name.lower()} connector",
            f"{conn_id} connector",
            f"connect to {name.lower()}",
            f"how do i connect to {name.lower()}",
        )
        body = (
            f"**{name}** connector (`{conn_id}`)"
            + (f" — {desc}." if desc else ".")
            + f" Category: `{category}`. "
            "Configure a connection on the Connections page; all connectors are "
            "open in OSS (no Plus gating). See the Cert Matrix page for the "
            "production-readiness score."
        )
        out.append(Topic(
            id=f"connector.{conn_id}",
            category=TopicCategory.CONNECTOR,
            title=f"Connector: {name}",
            aliases=aliases,
            body=body,
            tier=Tier.OSS,
            source=f"auto:manifests/{manifest_path.name}",
        ))
    return out


# ─────────────────────────────────────────────────────────────────────
# Docs — read docs/*.md
# ─────────────────────────────────────────────────────────────────────


# Only auto-import top-level docs (NOT product_facts/, user-guides/, etc.
# — those are too numerous and would bloat the atlas).
_DOC_ALLOWLIST = frozenset({
    "quickstart.md", "connectors.md", "nodes.md", "ai.md",
    "scaling.md", "faq.md", "editions.md", "architecture.md",
    "deployment.md", "eval-harness.md", "trust.md", "compliance.md",
    "performance.md", "supported-models.md", "customer-faq.md",
})


def _find_repo_docs_dir() -> Path | None:
    """Walk up from this file until we find a directory containing both
    ``backend/`` and ``docs/``. Robust against being installed at any
    nesting depth (e.g. ``<repo>/backend/fpulse/ai/atlas/``).

    Counting ``.parent`` calls is fragile when the source layout
    changes (or when Windows / case sensitivity / symlinks intervene).
    This walk is the safe pattern.

    Returns the ``docs/`` Path on success, None otherwise (logged).
    """
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        candidate_docs = ancestor / "docs"
        candidate_backend = ancestor / "backend"
        if candidate_docs.is_dir() and candidate_backend.is_dir():
            return candidate_docs
    logger.warning(
        "Atlas autogen: could not locate docs/ dir by walking up from %s "
        "(looking for a parent containing both backend/ and docs/)",
        here,
    )
    return None


def _doc_topics() -> list[Topic]:
    """One topic per allowlisted ``docs/*.md`` file. Title = first H1
    in the doc; body = first 400 chars of the introductory paragraph."""
    out: list[Topic] = []
    docs_dir = _find_repo_docs_dir()
    if docs_dir is None:
        return out

    for doc_path in sorted(docs_dir.glob("*.md")):
        if doc_path.name not in _DOC_ALLOWLIST:
            continue
        try:
            text = doc_path.read_text(encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Atlas autogen: failed to read %s: %s", doc_path.name, exc)
            continue

        # Extract first H1
        h1_match = re.search(r"^#\s+(.+?)\s*$", text, flags=re.MULTILINE)
        title = h1_match.group(1).strip() if h1_match else doc_path.stem.replace("_", " ").title()

        # First non-heading paragraph (strip front-matter if any)
        body_text = text
        if h1_match:
            body_text = text[h1_match.end():]
        first_para = ""
        for chunk in body_text.split("\n\n"):
            stripped = chunk.strip()
            # Skip headings, tables, code fences, lists at top
            if not stripped:
                continue
            if stripped.startswith(("#", "|", "```", "-", "*", ">")):
                continue
            first_para = stripped
            break
        if len(first_para) > 400:
            first_para = first_para[:397] + "…"

        slug = doc_path.stem.lower().replace("_", "-")
        topic_id = f"doc.{slug}"
        aliases = (
            slug,
            f"{slug} docs",
            f"{title.lower()} docs",
            f"{title.lower()}",
            f"read about {title.lower()}",
        )
        body = (
            f"**{title}** (from `docs/{doc_path.name}`). "
            + (first_para or "Reference documentation.") + "\n\n"
            f"_See the full doc at `docs/{doc_path.name}` in the F-Pulse repo._"
        )
        out.append(Topic(
            id=topic_id,
            category=TopicCategory.DOC,
            title=f"Docs: {title}",
            aliases=aliases,
            body=body,
            tier=Tier.OSS,
            source=f"auto:docs/{doc_path.name}",
        ))
    return out


# ─────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────


def autogen_topics() -> list[Topic]:
    """All auto-generated topics, in a stable order: tools → nodes →
    connectors → docs. Order matters for the alias matcher's tie-break
    (sorted by topic id descending after score)."""
    topics: list[Topic] = []
    topics.extend(_tool_topics())
    topics.extend(_node_topics())
    topics.extend(_connector_topics())
    topics.extend(_doc_topics())
    return topics
