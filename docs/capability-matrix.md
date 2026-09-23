# F-Pulse OSS — Capability Matrix

**What this is:** an honest, code-verified statement of what F-Pulse OSS does
and, just as importantly, what it does **not** do. Every row was checked
against the running backend, not against marketing copy. If a capability
isn't listed as **Supported**, don't sell it.

**What F-Pulse OSS is:** a single-operator **batch / ELT** tool — move data
from sources through SQL/transform steps into sinks, check its quality, and
orchestrate it on a schedule with retries. It runs on an in-process DuckDB
engine. It is *not* a streaming platform, a Spark distribution, a feature
store, or a governed lakehouse.

Legend: ✅ Supported · ◐ Partial / limited · ✕ Not in OSS

## Core data engineering

| Capability | Status | Notes |
|---|---|---|
| Batch / ELT pipelines (sources → transforms → sinks) | ✅ | JSON/CSV/Parquet/Excel/XML/S3/FTP/Google Sheets/Delta sources & sinks; joins, aggregates, SCD2, dedupe, SQL transform on DuckDB. |
| SQL transforms | ✅ | `SQL Transform` node runs SELECT/CTE SQL on DuckDB over named inputs. |
| Data quality / validation / profiling | ✅ | `DataQuality` node (13 checks; drop/fail/tag/reject/split modes, thresholds, per-row score) + data profiling. |
| Orchestration & scheduling | ✅ | Cron / interval / daily / weekly schedules, overlap watchdog, missed-run alerts, retries with failure classification. |
| Intra-pipeline DAG (dependency order, topological run) | ✅ | Steps run in dependency order within a pipeline. |
| Cross-pipeline orchestration (one pipeline triggers another) | ◐ | Only via an explicit `execute_pipeline` step inside a pipeline — there is **no** scheduler-level cross-pipeline dependency graph. |
| Change data capture (CDC) | ◐ | Kafka/Debezium + Postgres logical-decoding code exists, but reads a **bounded snapshot batch**, not a continuous stream. Edition docs mark CDC **Plus-only**. |
| Lineage | ◐ | A base run/column-level lineage store exists in OSS code, but the edition matrix currently lists lineage as Plus-only — see *Known inconsistencies*. |
| AI Copilot / assistants | ✅ | Pipeline-authoring Copilot + node helpers (explain, fix-error, suggest-SQL). LLM-first with deterministic rule fallbacks; embedding/guardrail/semantic-router nodes run offline via a hash-embedding fallback. This is an **assistant**, not model training. |

## Connectors

| | Status | Notes |
|---|---|---|
| Databases (Postgres, MySQL, SQL Server, plus 2 bulk-load dialects) | ✅ | Real drivers + tested query paths. |
| Generic REST connector runtime | ✅ | Works against arbitrary REST APIs; author one from an OpenAPI spec. |
| ~27 first-party SaaS connectors | ◐ | Shipped as **manifests** run through the generic adapter. They validate structurally but are **not** smoke-tested against live vendors (the cert matrix reports 0 Verified / 0 Production). Treat as Beta/Experimental. |

> The public README's "33 first-party connectors" counts these manifests.
> Only ~6 are genuinely production-functional today. Prefer wording like
> "6 production-tested connectors + 27 community/beta manifests + author your own."

## Explicitly NOT in F-Pulse OSS

State these plainly; do not imply otherwise.

| Capability | Status | Reality |
|---|---|---|
| Streaming / real-time stream processing | ✕ | Everything is batch on in-memory DuckDB. Kafka sources read a **bounded** batch then close. "Real-time" in the UI means live WebSocket **progress**, not stream processing. |
| Apache Spark / PySpark | ✕ | No Spark anywhere. The "Spark-style" worker-pool label is cosmetic UI naming only. |
| Feature store | ✕ | Not implemented, not claimed. |
| Governed lakehouse (Iceberg / Delta catalog / governance) | ✕ | Per-table Delta read/write exists, but there is **no** catalog or governance layer. A governed lakehouse is **Foundation**, a separate Hybridyn product — not F-Pulse. |
| ML model training | ✕ | Only embeddings + cosine routing. No training/fitting is offered or needed. |

## Known inconsistencies to resolve before a public launch

These aren't code bugs, but the story contradicts itself in places. Pick one
source of truth for each:

1. **Lineage edition.** OSS code implements a base lineage store
   (`lineage/__init__.py`, `api/lineage.py`), yet `docs/edition-matrix.md` and
   `docs/product_facts/25_anti_patterns.md` declare lineage **Plus-only**. The
   README says it "ships in the box." A reader can currently be told opposite
   things — decide whether base lineage is an OSS feature and align all three.
2. **Connector count vs. readiness.** See the Connectors note above — the
   headline "33 connectors / working connector in 90 seconds" overstates how
   many integrations are actually verified.
3. **Plus-only nodes shipping in the OSS repo.** `connectors/cdc.py`,
   `connectors/jdbc.py`, and `connectors/vector_db.py` are present and
   registered in OSS, but the edition matrix's Boundary Rule says "Plus
   features never live in the F-Pulse repo, even as stubs." Either the matrix
   is wrong (these are OSS) or the nodes shouldn't be here.

## Honest wins

The README already avoids the biggest traps: it does **not** claim streaming,
Spark, feature store, governed lakehouse, Iceberg, or ML — all verified absent
in code, and several are proactively disclaimed in
`docs/product_facts/25_anti_patterns.md`. The remaining exposure is narrow and
listed above.

---
*Generated from a code-level capability audit. Re-verify against the source
before publishing; treat the code, not this file, as the ground truth.*
