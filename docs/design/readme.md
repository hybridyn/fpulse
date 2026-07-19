# F-Pulse OSS — Design Documentation

Engineering reference covering architecture, low-level design,
class diagrams, pseudo code, and end-to-end functional flows for
both frontend and backend.

## Documents

| # | Doc | When to read it |
|---|---|---|
| 00 | **[System Architecture Overview](00_overview.md)** | First read. Tech stack, process model, the four canonical entities (Workflow / Step / Connection / Run), trust boundaries, pipeline state machine. |
| 01 | **[Backend Low-Level Design](01_backend_lld.md)** | Backend onboarding. Full module map, IR class diagram, BaseNode hierarchy (99 nodes), WorkflowExecutor + ExecutionContext, store-layer pattern, API router template, pseudo code for `execute_workflow` / `_run_node_once` / `expected_output_schema` / macro discovery / preflight / provenance. |
| 02 | **[Frontend Low-Level Design](02_frontend_lld.md)** | Frontend onboarding. Module map, component tree, Zustand workflowStore, React Flow canvas integration, ConfigPanel dispatch pattern, hash routing, API client, pseudo code for `addNode` / BackfillModal preflight / SyncModeField. |
| 03 | **[Functional Flow Diagrams](03_functional_flows.md)** | End-to-end sequence diagrams for the five most-traveled flows: create+run, backfill preflight, incremental sync, AI Copilot answer, publish-as-macro, plus preview-mode dry-run and the system-wide functional view. |

## Source-of-truth pointers

These docs describe the code as of 2026-05-30. When in doubt, the
running code wins:

| Area | Authoritative source |
|---|---|
| Node contracts | `backend/fpulse/ir/node_metadata.py` (arity, side-effect class) + `tests/test_node_conformance.py` (pinned invariants) |
| Schema migrations | `backend/fpulse/storage/database.py` — search for `_migrate_vNN_*` |
| API endpoints | `GET /openapi.json` when `FPULSE_MODE=dev` |
| Cert matrix | `GET /api/connectors/cert-matrix` (returns the per-connector capability + known_gaps list) |
| Operational deltas | `changelog.md` + `docs/deployment.md` §6.5 (schema versions) |

## Conventions across these documents

- **Pseudo code** uses Python-ish syntax with → for return, arrows
  for sequence flow, and JS-style object literals where they're
  more readable than Python dicts.
- **Class diagrams** are ASCII boxes so they render in any viewer
  (terminal `cat`, GitHub web, IDE preview). The same shapes
  translate trivially to Mermaid / PlantUML if you want generated
  SVGs.
- **Sequence diagrams** in §3 show three lanes (Browser / Backend /
  Stores). Arrows are HTTP unless noted; nested arrows are
  in-process function calls; double-ended arrows are DB queries.
- **2026-05-30 markers** in the code (search `R5`, `P4`, `W1`,
  `X2`, `F3`, etc.) tag the session that introduced each addition.
  These match the [changelog.md](../../CHANGELOG.md) entries.
