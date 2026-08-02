# Self-documenting pipelines

F-Pulse pipelines carry their own documentation. On top of the versioning,
change-notes, and ownership that every workflow already has, four
first-class fields make a pipeline explain itself — and one endpoint turns
that into a shareable Markdown document. No external wiki, no drift.

## What's on a pipeline

| Field | Meaning |
|-------|---------|
| `name`, `description` | Existing — the title and a short blurb. |
| `owner_id` / `owner_name` | Existing — who created and maintains it. |
| version history + `change_summary` | Existing — every save is a version with a "what changed" note. |
| **`business_purpose`** | **New** — the *why*, one line. Required before a pipeline can be published (see the gate below). |
| **`readme`** | **New** — freeform Markdown shown beside the canvas and folded verbatim into the generated doc. |
| **`tags`** | **New** — a first-class, filterable list (promoted out of the freeform `metadata` blob). |

All three new fields are optional and default empty, so pipelines created
before they existed round-trip unchanged — no migration, no forced re-save.
A workflow that previously stashed tags in `metadata["tags"]` has them
hoisted to the first-class `tags` list automatically on the next load.

### Setting them

- **Create:** `POST /api/workflows` accepts `business_purpose`, `readme`,
  and `tags` alongside `name` / `steps`.
- **Update:** `PUT /api/workflows/{id}` takes the full workflow blob, so the
  fields save like any other.

## The publish gate

A pipeline may not go live without a stated purpose:

```
POST /api/workflows/{id}/publish
→ 400  "A business purpose is required before publishing."   (when business_purpose is empty)
```

The check fires **at the publish action only** — it never re-validates
pipelines that are already published, so existing pipelines keep working;
it is the *next* publish that must carry a purpose. (Backfill-safe by
construction: the field was added optional and is enforced on new
publishes.) Publishing still also requires a passing test, exactly as
before.

## Markdown doc export

```
GET /api/workflows/{id}/docs
```

Synthesizes a Markdown document from the pipeline IR alone — deterministic,
no LLM, safe in an air-gap:

- title + **business purpose**
- a metadata table (status, version, owner, tags, node/input counts, last updated)
- **Overview** (`description`) and **Documentation** (`readme`, verbatim)
- **Inputs** — the declared pipeline parameters
- **Pipeline steps** — a per-node table (label, type, role, and the node's
  own description from the registry)
- **Change log** — every version with its change summary

Query parameters:

| Param | Effect |
|-------|--------|
| *(none)* | Returns `text/markdown`. |
| `?format=json` | Wraps it as `{"workflow_id", "filename", "markdown"}` for a UI that renders it inline. |
| `?download=1` | Adds `Content-Disposition: attachment; filename="<slug>.md"`. |

The renderer lives in `fpulse/ir/docs.py` (`render_workflow_markdown`) and is
covered by `tests/test_workflow_documentation.py`.
