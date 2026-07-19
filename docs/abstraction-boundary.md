# Execution architecture vs. user architecture

**One sentence:** Hide physical execution by default, expose logical results
and trust signals, and reveal advanced controls only when the user explicitly
asks for them.

F-Pulse runs a sophisticated engine (DuckDB lazy relations, fused query plans,
Parquet segments, partitioning, checkpoints, parallelism). Users must never be
*required* to understand any of it to build or trust a pipeline. The engine is
free to be clever underneath; the surface stays calm, task-based, and
semantically clear.

This file is a **binding design contract**, not aspiration. New nodes, config
panels, and Steward detectors are reviewed against the five rules below.

---

## The two architectures

| Execution architecture (engine) | User architecture (surface) |
|---|---|
| `DuckDBPyRelation`, temp views, fused plans | Logical nodes on the canvas |
| Parquet segments, partitions, manifests | "Freshservice Tickets", "Active assets" |
| Batch size, threads, `max_memory`, checkpoints | "What it does · what's in · what's out" |
| Cursor state, retry/backoff, idempotency keys | "Loaded 10,234 rows · 0 rejected" |

The line between these two is the **abstraction boundary**. Physical detail
lives below it and only surfaces in an explicit Advanced affordance.

---

## The five rules

### 1. Default simplicity (progressive disclosure)
Every node's default inspector shows only: **what the step does · what data
goes in · what comes out · whether anything was filtered/rejected/failed**.
Physical knobs (batch size, threads, partitioning, materialization) live in a
collapsed **Advanced** section, never in the default view.
- **F-Pulse today:** `ConfigPanel` already separates default vs. advanced
  config; the canvas shows logical node cards.
- **Avoid:** exposing raw partition pickers (`Hash / Range / Temporal`). Most
  users have no basis to choose, and a wrong choice erodes trust.

### 2. Safe optimization (the logical invariant)
> Runtime optimization must never change the logical dataset. It may change how
> data **moves, is stored, or is processed** — never **what the data means**.

If a source yields 10,234 rows, the destination has 10,234 rows unless the
workflow *explicitly* transforms them (filter, aggregate, dedup, join). An
"Optimization Mode" or storage-layout choice that silently drops or collapses
rows is a defect, not a feature.
- **Performance presets, not physics:** expose `Automatic · Fastest · Lowest
  Memory · Highest Reliability`. Internally these map to DuckDB `threads` /
  `PRAGMA max_memory` / batched generators / materialization frequency.
  `Automatic` is the default and equals today's behavior (zero-risk).
- **Status:** Optimization Mode is *proposed* (backlog), not yet shipped.

### 3. Logical vs. physical output
Every node has both a **logical output** (the dataset the user reasons about —
row count, schema) and a **physical output** (N Parquet files, M partitions).
Users see the logical output by default. Physical output is an Advanced /
diagnostics detail.
- **F-Pulse today:** per-node preview row counts exist (the preview row-count
  stats work). Logical row counts are the user-facing contract.

### 4. Steward trust signals
Turn hidden runtime intelligence into visible, business-readable proof. For
each step, Steward should be able to state:

```
Input rows:   10,234
Output rows:  10,234
Dropped:           0
Rejected:          0
```

or, when something happened:

```
Input rows:   10,234
Output rows:  10,102
Rejected:        132   (Validation failure)
```

This is the trust win: users learn whether data is **complete, changed, or
rejected** — without learning what a partition or manifest is.
- **F-Pulse today:** Steward (`backend/fpulse/steward/`) already snapshots
  schemas and detects drift from real runs.
- **Proposed (backlog):** the per-node **row-delta integrity check** — assert
  `input == output` on any node that is *not* a filter / router / aggregate /
  dedup / join, and raise a Steward finding when an unexpected delta appears.
  This is rule 2 made enforceable.

### 5. Materialization boundaries for integrity nodes
Because execution is lazy and the planner fuses/reorders steps, an integrity
node could in principle be reordered after a downstream filter, or have its rows
silently optimized away. Nodes whose *meaning* depends on seeing the full,
ordered upstream dataset — **Validation, SCD2, Deduplicate** — must act as
**hard materialization boundaries**: force the engine to compute and checkpoint
state before the next step, so optimization can't change logical sequencing.
- **F-Pulse today:** step outputs and pipeline checkpoints exist
  (`step_output_store`, the `pipeline_checkpoints` table). 
- **To verify/strengthen:** confirm these node types always materialize before
  downstream filtering, and add an explicit barrier where they don't.

---

## Enforcement checklist (apply to every new node / feature)

- [ ] Default inspector shows logical intent + I/O + reject/fail only.
- [ ] Any physical knob is in a collapsed Advanced section, off the default path.
- [ ] No performance/storage setting can change row count or aggregation meaning.
- [ ] The node reports a logical row count Steward can read.
- [ ] If the node is an integrity node (Validation/SCD2/Dedup), it materializes
      before downstream steps.
- [ ] Advanced controls are presets/intent ("Lowest Memory"), not raw engine
      internals ("hash partition fanout = 16").

---

## What this is **not**
Not a rewrite. F-Pulse already follows most of this (logical canvas, hidden
physical layer, advanced sections, per-node counts, checkpoints). The two
genuine additions are **Optimization Mode** (rule 2) and the **Steward
row-delta check** (rule 4) — both additive, both `Automatic`/observe-only by
default, neither touching the existing execution path.
