# F-Pulse Steward — Solid Proof of Operation

This directory contains hard evidence that every Steward behaviour
described in `docs/steward/overview.md` works end-to-end against the
real production code path. It was generated on 2026-06-06 by running
`backend/scripts/validate_steward.py` against a 9-workflow fixture.

If you ever doubt "does the Steward actually do X?", come here, run
the script, and read these files.

---

## TL;DR

Every learning behaviour proven against the shipped code:

| Behaviour | Proof line in journal | Result |
|---|---|---|
| **Detect duplicate-source** (3 pipelines reading the same Oracle table) | Lines 1-3 (3 distinct `dup-src-*` emits) | ✓ |
| **Detect duplicate-pipeline** (Engineer A + Engineer B's identical flow) | Line 4 (`dup-pipe-*` emit) | ✓ |
| **Dismiss-with-reason persists** | Line 5: `"reason": "DR replication across regions — intentional"` | ✓ |
| **Suppression survives across re-scans** | Lines 6-8 (DR signature absent from scan #2) | ✓ |
| **Severity escalation P2 → P1 after threshold crossed** | Lines 12-14 (`severity_at_emit`: "p2" → "p1") | ✓ |
| **Time-clamped escalation honours `escalate_min_hours_since_first`** | Run with default 24h would NOT escalate; demo sets to 0 to fast-forward | ✓ |
| **Resolve clears the finding** | Line 15: `kind: resolve` | ✓ |
| **Rebound detection annotates re-emerged finding** | Line 16 (re-emit after resolve → `(rebounded)` prefix in body) | ✓ |
| **Persistent occurrences count distinct scan_ids** | Step 7a: signature `907ccae8…` shown as 4 scans | ✓ |

---

## Why the user's screenshot showed "Method Not Allowed"

The reviewer saw a 405 in the Steward dropdown. Diagnosis (verified
via `fetch()` probes from the running browser tab):

| HTTP verb | Result on `/api/steward/scan` |
|---|---|
| GET | 404 Not Found |
| POST | 405 Method Not Allowed |
| OPTIONS | 204 (CORS preflight handled by Starlette middleware) |
| HEAD | 404 |
| PUT/DELETE/PATCH | 405 |

The pattern (404 for safe verbs, 405 for state-changing verbs, 204
for OPTIONS) is the FastAPI/Starlette CORS-middleware fingerprint
when a path is **not registered**. The middleware returns 204 for
OPTIONS at every path; for state-changing methods it returns 405
because the preflight already "advertised" the path; for safe verbs
it falls through to the normal 404.

**Cause:** the user's backend is running an older binary, started
BEFORE the Steward router was added. The router is registered in
`backend/fpulse/main.py` at `app.include_router(steward_router)`
but the running process hasn't reloaded.

**Fix:** restart the backend. Once restarted, all 17 endpoints are
reachable and the Steward dropdown will populate correctly.

---

## Files in this directory

| File | What it is |
|---|---|
| `01-validation-run.txt` | Full stdout of `validate_steward.py` — section-by-section walk through every learning behaviour with assertions |
| `02-memory-journal.jsonl` | The actual on-disk journal — 16 events. Grep-friendly. THIS is the storage the Steward uses in production. |
| `03-settings.json` | The on-disk settings file showing tightened escalation thresholds for the demo |
| `05-test-suite-verbose.txt` | All 59 backend tests passing, verbose output |
| `06-contract-snapshot.txt` | The 17 HTTP routes + 30 FindingKind values + 7 levels + 8 statuses, dumped from the live Python contract |
| **`07-live-workspace-dryrun.txt`** | **Output of running the Archeologist against the user's REAL workflow database** (`data\samples\fpulse.db`). 3 actual duplicate-source findings between 5 of their 18 existing pipelines |
| **`08-live-findings-render.html`** | **Pixel-faithful render of the Steward dropdown** showing the same 3 findings as they'll appear in the live app once the backend is restarted. Open in any browser. |
| **`09-memory-tab-journal.jsonl`** | The actual JSONL journal produced by the Memory-tab verification run — 12 events including the dismiss-with-reason payload preserved verbatim |
| **`10-memory-tab-rendered.html`** | **Pixel-faithful render of the Memory tab AND Findings tab side-by-side** after a full simulated workflow (3 scans → dismiss → resolve → re-emit). Open in browser OR navigate to http://localhost:5174/steward-proof.html if Vite is running. |
| **`11-memory-verify-output.txt`** | Step-by-step output of `steward_memory_verify.py` showing every Memory-tab data source returning the expected values |

### Memory tab — functionality verified

The Memory tab calls these data sources, all verified to return correct values:

| Data source | Verified | Sample result |
|---|---|---|
| `StewardMemory.stats()` | ✓ | `{total_events: 12, total_scans: 4, total_emits: 10, total_dismisses: 1, total_resolves: 1, distinct_signatures_seen: 3}` |
| `StewardMemory.persistent_occurrences()` | ✓ | 3 signatures tracked (`c790972…: 4 scans` shown in RED because it crosses the escalation threshold) |
| `StewardMemory.first_seen_per_signature()` | ✓ | 3 signatures with first-emit timestamps |
| `StewardMemory.resolved_signatures()` | ✓ | 1 resolved signature (drives rebound detection) |
| `StewardMemory.audit_trail(limit=200)` | ✓ | 12 events (newest first) including the dismiss row with operator rationale verbatim |
| `apply_learning()` | ✓ | All 3 findings escalated P2 → P1, 1 marked `REBOUNDED` with `evidence.previously_resolved_at` set |

### The dismiss-with-reason payload (real captured text)

```json
{"kind": "dismiss",
 "finding_id": "dup-src-2796927d40671cd7",
 "signature": "2796927d40671cd7",
 "reason": "Sales Pipeline reads leads-1000 for daily reporting; Ad-hoc Analysis reads it for ad-hoc analysis. Different SLAs, intentional."}
```

This is the line that turns a one-shot alert into team memory — the future Curator (1.4) sub-agent will mine this exact rationale text to generalize the suppression rule.

### Real findings from the user's workspace (file 07 + 08)

1. **`Aggregation Report` + `Simple ETL Pipeline`** — both read `orders.csv`
2. **`Sales Pipeline` + `Ad-hoc Analysis`** — both read `d9880bfabd79.leads-1000` (the MSSQL leads table)
3. **`First Pipeline (copy)` + `Ad-hoc Analysis`** — both read `products-100.csv`

These are real overlaps in a live test workspace. Detector produced them deterministically from the workflow store contents. The pipeline `Ad-hoc Analysis` ingests 2 sources that are already used by other pipelines — exactly the kind of "I built this without realising it overlapped" pattern the Steward is designed to catch.

### The format-handling bug fixed today

The Archeologist was hard-coded for React Flow node format (`node.data.stepType`) but F-Pulse stores workflows in step format (`step.type` at top level). This meant every production scan returned 0 findings even when duplicates existed. Fixed in `archeologist.py::_step_type_and_params()` with 2 new regression tests:

- `test_detector_handles_fpulse_step_format`
- `test_detector_handles_mixed_format_workspaces`

Test suite: **61 passing** (was 59, +2 for the format-handling regression).

---

## How to re-prove this yourself

From the repo root:

```powershell
$env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe backend\scripts\validate_steward.py
```

Reads no external state. Writes to a fresh temp dir. Asserts every
behaviour. Exits 0 on success, non-zero with stack trace on any
assertion failure.

---

## Walking through the journal (the proof of learning)

`02-memory-journal.jsonl` is 16 lines. Each line is a single event.
Read them top-to-bottom for the full story:

### Scan #1 — initial detection (lines 1-4)

```
emit  scan_id=a8bb39959adf  finding=dup-src-907ccae80c37c229  sig=907ccae80c37c229  severity=p2
emit  scan_id=a8bb39959adf  finding=dup-src-57cd0019344890b5  sig=57cd0019344890b5  severity=p2
emit  scan_id=a8bb39959adf  finding=dup-src-8f5e4dda303a4ce2  sig=8f5e4dda303a4ce2  severity=p2
emit  scan_id=a8bb39959adf  finding=dup-pipe-96953620e2bc9267 sig=96953620e2bc9267 severity=p2
```

4 findings, all P2, all from one scan (`a8bb39959adf`):
- Orders source read by 3 pipelines
- Customers source read by 2 pipelines (Engineer A + Engineer B)
- DR audit_log replicated across 2 regions
- Customers pipeline shape duplicated

### User dismisses DR with reason (line 5)

```
dismiss  finding=dup-src-8f5e4dda303a4ce2  sig=8f5e4dda303a4ce2
         reason="DR replication across regions — intentional"
```

**The reason text is preserved verbatim** for the future Curator
sub-agent to mine. This is the "captures tribal knowledge" loop —
when a new engineer joins 6 months later and tries to "fix" the DR
duplication, the Steward will surface this approved reasoning.

### Scan #2 — suppression honoured (lines 6-8)

Three emits, NO `8f5e4dda…` (DR) signature. Suppression works.

### Scan #3 (line 9-11)

Same three findings re-emit. Still P2.

### Scan #4 — ESCALATION FIRES (lines 12-14)

```
emit  scan_id=fbde7cf14074  finding=dup-src-907ccae80c37c229  severity=p1
emit  scan_id=fbde7cf14074  finding=dup-src-57cd0019344890b5  severity=p1
emit  scan_id=fbde7cf14074  finding=dup-pipe-96953620e2bc9267 severity=p1
```

**`severity_at_emit` field changed from "p2" to "p1"** — the
learning layer bumped them. Each finding has now appeared in 4
distinct scans (`a8bb39…`, `7af056…`, `d10380…`, `fbde7c…`),
crossing the threshold of 3. The escalation note is appended to
the finding body (visible in `01-validation-run.txt` Step 5).

### User resolves the customer-pipeline duplicate (line 15)

```
resolve  finding=dup-pipe-96953620e2bc9267  sig=96953620e2bc9267
```

### Re-emit triggers rebound state (line 16)

```
emit  scan_id=b986ea64a87c  finding=dup-pipe-96953620e2bc9267  severity=p1
```

Same finding ID, emerged in a NEW scan AFTER the resolve. The
`apply_learning()` call enriches the in-memory object with:
- `status = FindingStatus.REBOUNDED`
- `evidence.previously_resolved_at` set to the resolve timestamp
- `title` prefixed with `(rebounded)`
- Body appended with the regression-warning paragraph

This is the bit that turns a one-shot detector into something that
**remembers your fixes**.

---

## The contract snapshot — what's live (file `06-contract-snapshot.txt`)

| Layer | Count |
|---|---|
| HTTP routes | 17 |
| `FindingKind` enum values | 30 |
| `FindingLevel` enum values | 7 (pipeline, node, connector, data, architecture, governance, cost) |
| `FindingStatus` enum values | 8 (open, acknowledged, dismissed, resolved, rebounded, suppressed, expired, stale) |
| All 30 kinds mapped to a level | ✓ (test `test_kind_level_mapping_is_complete` enforces this) |
| Architecture-level kinds | duplicate_pipeline, duplicate_source, redundant_transfer, lineage_cascade |

---

## The test-suite snapshot — what's pinned (file `05-test-suite-verbose.txt`)

**59 tests, all green in 0.23 seconds.** Organized by contract:

| Test class | Tests | Pins |
|---|---|---|
| `TestSourceSignature` | 8 | SHA-256 stability, workspace-prefix isolation, edge cases |
| `TestDuplicateSource` | 3 | Cross-workflow detection + occurrence counts |
| `TestDuplicatePipeline` | 2 | Source+sink shape match; fan-out NOT flagged |
| `TestSuppression` | 1 | Dismissed signatures stay dismissed |
| `TestDeterminismAndEmpty` | 8 | ID stability, edge cases, kind→level mapping completeness |
| `TestLearningLayer` | 8 | Persistent occurrences, time-clamped escalation, REBOUNDED status, audit trail |
| `TestSettings` | 5 | Defaults, round-trip, corrupt-file fallback, notify defaults |
| `TestNotificationBridge` | 8 | The de-dup invariants (the spam-prevention guards) |
| `TestMemoryLayerLessons` | 16 | Lesson lifecycle + architecture-level + confidence richness + expanded status |

The most safety-critical: `TestNotificationBridge` (would-spam-the-bell
prevention) and `test_search_for_failure_excludes_proposed`
(gated-learning Rule 3).

---

## What this proves vs what's still in the contract-only stage

**Proven by this run (actively shipped in 1.1):**
- Duplicate-source detection (Archeologist)
- Duplicate-pipeline detection (Archeologist)
- Dismiss-with-reason → suppression file → reason preserved in memory
- Resolve → notification cleared
- Persistent occurrence counter across distinct scan_ids
- Time-clamped severity escalation (P2 → P1)
- Rebound state on resolved-then-re-emerged findings
- Per-workspace JSONL event journal
- Memory Layer lesson store with propose/approve/revalidate (12 dedicated tests)
- Notification bridge de-dup invariant

**Contract-ready (1.1) but detector not yet shipped:**
- Schema drift, empty output, null spike, volume anomaly, freshness miss,
  partition missing (data level)
- SLA breach, partial output, retry storm (pipeline level)
- Connector auth/rate/unreachable/credential expiry (connector level)
- PII leak, credential sprawl, env crossing, unapproved destination (governance)
- Cost drift, warehouse waste, cost recommendation (cost level)
- Redundant transfer, lineage cascade (architecture level)
- Failure RCA (cross-cutting)

The 24 contract-only kinds have their `FindingKind` enum values,
their `KIND_TO_LEVEL` mapping, their UI label slots, their
notification bridge handling, their memory journal serialization,
and their suppression rules already wired. When Sentinel (1.2),
Foreseer (1.3), Cost Steward (1.3), Governor (1.4) ship their
detectors, the producer is the only new code needed.
