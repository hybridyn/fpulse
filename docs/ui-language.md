# UI Language Style Guide

The locked glossary for **user-visible** strings in F-Pulse. Locked here so it doesn't regress over time.

> **Related:** [abstraction-boundary.md](abstraction-boundary.md) — the execution-vs-user-architecture contract (hide physical execution, expose logical results + trust signals, defer advanced controls). The "two languages" idea below is the *wording* side of that same boundary.

## Purpose

F-Pulse speaks two languages: an internal engineering language (precise, technical, exposed in API responses / logs / code / docs-for-developers) and a user-facing language (clear, human, exposed in UI labels / messages / marketing / user-guides).

The two are not the same. A user reading "IR" in the UI has no idea what it means; an engineer reading "pipeline snapshot" in the API has no idea which field maps to it.

This guide governs **only the user-facing language**. Internals stay precise.

## Where this applies

| Surface | Apply this guide? | Why |
|---|---|---|
| JSX text content (`<span>Run</span>`) | **Yes** | User reads it |
| String literals used as `title`, `label`, `placeholder`, `aria-label`, error messages | **Yes** | User reads it |
| Toast / alert / dialog copy | **Yes** | User reads it |
| Marketing copy, README, user-guides under `docs/user-guides/` | **Yes** | User reads it |
| Code comments (`//`, `/* */`, `"""`) | **No** | Engineers read these |
| JSDoc / Python docstrings | **No** | Engineers read these |
| Internal field names in API responses (`workspace_id`, `manifest_version`) | **No** | Engineers consuming the API need precise names |
| Log messages / structured log keys | **No** | Engineers debugging at 2am want technical precision |
| Test names, test descriptions | **No** | Engineers read these |
| Internal design docs (`DESIGN_*.md`, `AUDIT_*.md`, `NODE_AUDIT_MATRIX.md`) | **No** | Engineers read these |
| Database column names, internal config keys | **No** | Schema is not UX |

If you're unsure: ask "does this string ever appear in front of a customer who hasn't read our codebase?" If yes, apply the guide. If no, leave it.

## Glossary — forbidden in UI, preferred replacement

| Internal term | UI replacement | Notes |
|---|---|---|
| **IR** | "pipeline snapshot" (when discussing a stored representation), or just "pipeline" | The execution engine's intermediate representation is invisible to users. They see pipelines. |
| **manifest** | "connector definition" (in connector authoring), "configuration" elsewhere | Manifests are a developer concept. Users configure connectors. |
| **scaffold** | "starter template", "draft", or "starting point" | Scaffolding is a builder-tooling word. Users see templates and drafts. |
| **agent** | "Copilot" (when referring to F-Pulse Copilot), or drop entirely | The agent is the implementation. The product feature is the Copilot. |
| **ephemeral** | "temporary" | Users don't read engineering papers. |
| **router** (the LLM/intent router) | drop, or "rule" / "routing rule" if user-configured | Routing internals are invisible. If a user configures it, call it a rule. |
| **MCP** | drop entirely | MCP is a protocol name; users have no use for it in F-Pulse copy. |
| **trace store** | "run history" | The trace store IS the run history backend. Users see the history. |
| **workspace_id** | the workspace's display name, or "workspace" generically | Users have names; IDs are for our routing. |
| **kernel** (as in pipeline kernel) | "execution engine" or just "F-Pulse" | Engineering term; users see "F-Pulse runs your pipeline". |
| **handle** (as in execution handle) | "run" or "run ID" | A handle is a pointer; a run is what the user cares about. |
| **fixture** | "test data" or "sample" | Fixture is a testing-framework word. |
| **stream** (Airbyte-style stream) | "table" or "object" (depending on context) | OK in advanced docs; avoid in main UI flows. |
| **idempotency key** | "request ID" (if shown) | Most users don't need this surfaced. |

## Competitor naming & claim honesty

Unlike the glossary above (which governs only user-facing strings), this rule applies **everywhere — including code comments, docstrings, and test labels.**

1. **Don't name competitor products** (Azure Data Factory / ADF, Talend, Airbyte, Fivetran, n8n, Informatica, Matillion, Power Automate, Zapier, SSIS, Fabric, …). Describe the *behavior* or *concept*, not "like X". Our control-flow nodes deliberately mirror established orchestrator semantics — keep that fidelity in the **names and behavior**, but don't advertise the lineage in copy or comments. Write "If Condition", not "(ADF If Condition)"; "per-row loop", not "ADF ForEach / n8n Loop Over Items".
2. **No unbenchmarked superlatives.** "dramatically faster", "far faster", "blazing", "10×" without a citation are claims we can't defend. Replace with mechanism-grounded language: "faster on analytic workloads (DuckDB vectorised columnar vs JVM row-by-row)". State *why* it's faster, not just *that* it is.
3. **No false / aspirational capability claims.** If a detector, feature, or integration is contract-only / roadmap / not wired into live execution, label it as such. Active is "active"; planned is "planned". (See `steward/pitches.md` for the model — "Active in 1.1.x today" trust-marker.)

**Allowed exceptions** (these are not name-drops):
- **Functional references** where the name *is* the thing the code touches — e.g. a connector that calls Airbyte's public connector registry API, or a connector-type ID string (`"fivetran"`).
- **Dedicated comparison docs** (`vs-talend.md`, `vs-airbyte.md`) name competitors by design — that's their purpose. They must still obey rule 2 (claim honesty) and rule 3.
- **Test fixtures** that simulate real user questions ("Can I migrate SSIS jobs?") — these are legitimate coverage of phrasings users actually type.

## AI framing addendum (V4)

The Copilot is a **feature**, not the **origin** of the product. Apply alongside the glossary above:

| Forbidden | Replacement |
|---|---|
| "AI-generated" | "draft created" or "suggested" |
| "AI-generated SQL" | "suggested SQL" |
| "AI-generated defaults" | "suggested defaults" |
| "AI-authored" | "Copilot-authored" |
| "AI-powered" (badge/header) | "Copilot" or drop |
| "AI-suggested" | "Suggested" |
| "Generated by AI" | "Draft" or "Suggested" |
| Provider names (Claude / OpenAI / Gemini / Ollama / OpenRouter) outside Settings | "Copilot" or drop |
| Provider names inside Settings → AI Provider | **Allowed** — that's the right place |

The internal `proposed_by: "copilot" \| "human"` field in the IR is fine — that's internal provenance, not user copy.

## Preferred user-facing lexicon

Use these terms consistently in UI copy:

- **Pipeline** — the unit of work users build
- **Connector** — the integration to an external system
- **Credential** — stored authentication for a connector
- **Connection** — a configured instance of a connector with a credential and parameters
- **Managed Table** — a Parquet-backed table in the Storage workspace
- **Run** — a single execution of a pipeline
- **Execution history** / **Run history** — the list of past runs (never "trace store")
- **Preview** — sample-data view, never "ephemeral output"
- **Draft** — unsaved or unpublished state, never "scaffold"
- **Published** — saved state, never "promoted to runtime"
- **Recipe** — a reusable data-prep configuration (when Recipe-as-first-class lands)
- **Copilot** — F-Pulse Copilot, the AI assistant feature
- **Template** — pre-built pipeline shape, never "scaffold"
- **Workspace** — the user's logical workspace, identified by its name (not ID)

## Examples

❌ "Trace store flushed."
✅ "Run history saved."

❌ "Scaffold created from manifest."
✅ "Draft created from connector definition."

❌ "Agent invoked with ephemeral context."
✅ "Copilot started." (the ephemeral context is an implementation detail)

❌ Toast: "IR for workspace_id 7f3a... committed."
✅ Toast: "Pipeline saved to '<workspace-name>'."

❌ Button label: "Run AI-generated SQL"
✅ Button label: "Run suggested SQL"

❌ Section header: "AI-powered builder"
✅ Section header: "Copilot builder" or just "Pipeline builder"

## Enforcement

A grep gate runs in CI (planned): any new commit introducing a forbidden term in `frontend/src/**/*.tsx`, `frontend/src/**/*.ts`, `docs/user-guides/**/*.md`, or the top-level `readme.md` fails the build unless it's inside a comment.

Until CI lands, the rule applies on every review: catch leaks at PR time. If the term shows up in a code comment explaining a deliberate decision (e.g., "the visible label says 'Copilot' but the internal route is `/agent/start`"), that's fine — the comment is for engineers.

## What this guide is **not**

- **Not** a translation guide. It only governs English source strings.
- **Not** a CSS / typography / iconography guide — see the design system docs for those.
- **Not** a content style guide for voice and tone. (Voice: clear, direct, calm. Tone: confident but not boastful. See marketing's separate guide.)
- **Not** about API field naming. Internal precision wins there; consistency with the existing schema wins over UI-friendliness.

## Change log

- **2026-05-26** — Initial lock. Captures the V4 (AI framing) and #11 (terminology) lessons from the F-Pulse product vision conversation. Updates require a PR to this file and a corresponding sweep of any affected UI surfaces.
