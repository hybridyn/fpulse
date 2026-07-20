# F-Pulse Steward — Schema drift

The first **data-level** Steward detector and the first **event-driven**
one. Where Archeologist and user-rules derive findings from current
workspace state on every scan, schema drift only matters at the moment
a new schema appears — so the architecture is:

1. A pipeline run (or external recorder) posts the **current** schema
   for a source to `POST /api/steward/schema-snapshot`.
2. The recorder compares against the previous snapshot for that source.
3. If the diff is non-empty, a `SCHEMA_DRIFT` finding is appended to
   the per-workspace journal.
4. Every subsequent `/findings` call re-surfaces open drift findings
   from the journal (until dismissed or resolved).

This activates `FindingKind.SCHEMA_DRIFT` at `FindingLevel.DATA` — the
fifth of the seven Steward levels to ship an active detector in OSS
(after architecture / connector via Archeologist + connector-health).

## Three change classes

| Class | Severity | Why |
|---|---|---|
| `added` | **P3** | New column. Usually safe (additive change). Surfaced so the team notices, not to wake anyone. |
| `dropped` | **P1** | Column gone. Almost always breaks a downstream SELECT, JOIN, or transform. Page-worthy. |
| `type_changed` | **P1** | Same column name, different type. Downstream casts and arithmetic likely fail. |

**Worst-case wins.** A diff that bundles an `added` (P3) with a
`type_changed` (P1) escalates the **whole** finding to P1. We don't
want operators thinking "oh, just additions" while a type change
quietly breaks downstream casts.

## How to record a snapshot

```bash
POST /api/steward/schema-snapshot
{
  "source_signature": "abc123def456",     # required — same key Archeologist uses
  "source_label":     "orders.csv",       # optional, human-readable
  "columns": [                             # required
    {"name": "id",         "type": "int"},
    {"name": "amount",     "type": "decimal"},
    {"name": "created_at", "type": "timestamp"}
  ],
  "run_id": "exec-9876"                    # optional — links to a pipeline run
}
```

Response when no previous snapshot exists (baseline establishment):

```json
{ "recorded": true, "drift_detected": false }
```

Response when the diff is non-empty:

```json
{
  "recorded": true,
  "drift_detected": true,
  "finding_id": "sdrift-a1b2c3d4e5f60718",
  "changes": [
    {"kind": "type_changed", "column_name": "amount",
      "old_type": "int", "new_type": "decimal"},
    {"kind": "added",        "column_name": "email",
      "old_type": "",    "new_type": "text"}
  ]
}
```

## Recording paths

| Source | When |
|---|---|
| **External POST** | The primary path today. Pipeline runners, CI jobs, scheduled probes, or admin scripts all push snapshots here. |
| **Future: built-in pipeline executor** | Will record a snapshot automatically on every successful source read. Not shipped in 1.1.x — the storage format will absorb auto-recorded snapshots without schema change. |

## What "drift" actually compares

The diff is computed by column **name** (order-insensitive). For each
name in the new snapshot:

- Missing from old → `added`
- In old with same type (case-insensitive) → no change
- In old with different type → `type_changed`

For each name in the old snapshot but not the new → `dropped`.

Type comparison is case-insensitive — `text` vs `TEXT` across drivers
is NOT drift. Casing-only noise was the first false-positive class we
saw in design review.

## Storage layout

```
<data_dir>/steward/<workspace>/
  schemas/
    <sha256-of-source-signature>.json     # LATEST snapshot for that source
    ...
  schema_drift_findings.jsonl              # append-only journal of every drift
                                            # finding ever emitted in this workspace
```

The snapshots dir holds only the latest known schema per source — we
don't need history for drift detection (we always diff against the
immediately previous shape). The journal is what gives drift findings
persistence across rescans.

## Suppression

Source signature on every drift finding is the same source key
Archeologist uses, so the existing dismiss-with-reason flow works
unchanged:

- Dismiss a drift finding with `"Intentional — new email column requested by analytics"`
- That signature joins the workspace `suppressed_signatures` list
- Subsequent `/findings` scans skip this drift

The **next** drift on the same source (a different captured_at →
different finding id, different signature suffix) WILL produce a
fresh finding — dismiss is per-drift-event, not per-source.

## Lifecycle

| Stage | What happens |
|---|---|
| **Baseline** | First snapshot for a source establishes the shape. No finding emitted. |
| **Drift detected** | Subsequent snapshot differs → finding written to journal, immediately visible at `/findings`. |
| **Operator dismisses** | Signature added to suppressed list. The new shape becomes the implicit baseline for future drift. |
| **Operator resolves** | Marked resolved. If they typed a `fix_note`, becomes a `PROPOSED` lesson in the Memory Layer (resolve→lesson loop). |
| **Operator ignores** | Finding stays open. Time-clamped escalation rules from `apply_learning` apply on subsequent scans the same way they do for any other finding. |

## What does NOT happen

- **Steward never probes a source to read its schema.** Schemas are
  pushed in. Probing would be a hidden side effect on production
  sources, against Rule 1 (read-only).
- **Steward never auto-mutates downstream pipelines.** If a column was
  dropped and a downstream transform references it, the finding tells
  you — the fix is yours.
- **A single recording with no previous snapshot is never a "finding"
  of drift.** That would punish bootstrapping. The first snapshot is
  always the baseline.

## What needs Plus

OSS gets the full detector, journal, recording API. Plus will add:

- An **in-app schema-history viewer** rendering past snapshots + diffs
- **Scheduled snapshot probes** that the platform runs (not waiting
  on external POSTs)
- **Cross-workspace drift correlation** (the same source signature
  drifting across multiple Plus workspaces, surfaced as a single roll-up)

Storage format stays identical between OSS and Plus.

## See also

- [`overview.md`](overview.md) — the 7-level Steward contract this fits into
- [`connector-health.md`](connector-health.md) — the connector-level companion detector
- [`custom-rules.md`](custom-rules.md) — admins can layer additional data-level checks on top
- [`positioning.md`](positioning.md) — 4-pillar product framing
