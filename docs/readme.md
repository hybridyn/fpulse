# F-Pulse Documentation

Welcome to F-Pulse — the open-source data pipeline orchestrator. This is
the entry point to every document available from the Help page.

## Quick start

If you've just installed F-Pulse and want to run your first pipeline:

1. Sign in with the bootstrap credentials from `INITIAL_ADMIN_PASSWORD.txt`
   (located next to the SQLite database file). Change the password and
   delete the file once you're in.
2. Open **Workflows** from the top navigation, then create or open a
   pipeline to enter the canvas editor.
3. Drag a Source node onto the canvas, configure it, then add a Transform
   and a Destination. Click **Run** to execute.

See the [full Quickstart guide](quickstart.md) for end-to-end install + first pipeline + AI setup.

## User guides

| Guide | Audience | What it covers |
|---|---|---|
| [Projects](user-guides/projects.md) | All users | Create, manage, share, archive, and delete projects. |
| [Pipelines](user-guides/pipelines.md) | All users | Build, test, validate, run, schedule, monitor, archive, clone, export. |
| [Connections](user-guides/connections.md) | All users | Create, test, scope by project. Connector status badges (Certified / Beta). |
| [Connectors catalog](connectors.md) | All users | Every connector family — Oracle / SAP / Microsoft Graph / SaaS — with auth + scope notes. |
| [Node reference](nodes.md) | Pipeline authors | Every node type with parameters: sources, transforms, outputs (incl. Managed Table Source/Sink for managed tables). |

## Reference

| Document | Purpose |
|---|---|
| [API Reference](api.md) | Every HTTP endpoint, request/response shape, status codes. |
| [Reliability features (1.2)](reliability-1.2.md) | Data lineage, OpenLineage export, failure classification, retry policy, cancellation, backfill lookback / resume / merge / soft-delete — with GA vs foundation status per capability. |
| [Developer Guide](DEV-GUIDE.md) | Architecture, conventions, how to extend F-Pulse with custom nodes. |
| [Architecture overview](architecture.md) | OSS execution model: worker pool, scheduler, single-node design, open-core boundary. |
| [Testing](TESTING.md) | How tests are organized and how to add new ones. |

## Where to ask questions

- Pipeline-level help — use the **AI Assist** floating button on any page
- Repository issues — open a GitHub issue with reproduction steps
- Security reports — see the `security.md` policy at the repository root
  (do **not** post security issues publicly)

## What's not in F-Pulse

Some operational features live in **F-Pulse+**, the commercial extension:
multi-user team workspaces, role-based access control, deployment
approvals, audit log, sandbox runs, drift detection, and SLA-backed
support. The Dashboard's bottom card describes these — F-Pulse OSS itself
stays free forever, with no feature crippling.

---

*Generated for F-Pulse v1.0.0.*
