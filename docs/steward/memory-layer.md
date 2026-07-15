# F-Pulse Memory Layer

The Memory Layer is the part of F-Pulse Steward that turns one-off
operator decisions into durable institutional knowledge. It is the
**why** that survives team turnover — the runbook the Steward writes
*with you*, not for you.

This page is the user-facing description. For the storage contract,
file layout, and integration points, see
[`backend/fpulse/steward/lessons.py`](../../backend/fpulse/steward/lessons.py)
and [`architecture.md`](architecture.md).

---

## What it stores

Ten lesson categories, each a typed knowledge entry:

| Category              | Example                                                             |
|-----------------------|---------------------------------------------------------------------|
| `source_quirk`        | Salesforce REST returns 0 rows on a fresh OAuth token until 30s elapse |
| `schema_drift`        | Snowflake account_id changed from INT to VARCHAR on 2026-04-12      |
| `failure_pattern`     | `ORA-12154` alias failure → check gateway TNS_ADMIN + Oracle client |
| `transformation_rule` | Always strip trailing CRLF when reading our HR system's emails      |
| `retry_rule`          | Pool exhaustion: bounded retry helps; auth failures: never retry    |
| `cost_anomaly`        | Snowflake warehouse XL stayed warm overnight → ~$140 wasted         |
| `duplicate_warning`   | `audit_log` IS duplicated by design (DR replication) — do not flag   |
| `sla_pattern`         | Daily batch always finishes after 02:00 UTC, not 01:30              |
| `user_fix`            | Engineer A's regex for the malformed timestamp column               |
| `security_finding`    | The `notes` column contains PII — must redact downstream            |

---

## What a lesson looks like

Stored as a human-readable YAML file (one per lesson) at
`<data_dir>/steward/<workspace>/lessons/<id>.yaml`:

```yaml
id: ora12154
workspace_id: default
source: Oracle_FIN_PROD
pipeline: Load_AP_Invoices
lesson_type: failure_pattern
status: approved
confidence: high
issue: "ORA-12154 alias failure"
symptom: Cannot find alias in TNS/EZConnect
root_cause: Gateway TNS_ADMIN env points at the wrong oracle home
approved_fix: Check gateway TNS_ADMIN and Oracle client config
proposed_by: steward
approved_by: data-owner@hybridyn.com
validity_days: 180
occurrence_count: 7
created_at: 2026-03-14T09:21:00+00:00
last_validated: 2026-06-05T14:02:18+00:00
evidence:
  - kind: execution
    id: exec-2026-03-14-bf2b
    note: "first observation"
  - kind: execution
    id: exec-2026-05-19-ab17
    note: "recurred after env-var rollout"
```

YAML is the human side (PR review, hand-edit, grep). A companion
`<id>.json` is the machine side (API read path). Both are written
together inside a file lock — they cannot drift.

---

## How a lesson is created and trusted (the 8-step flow)

This is the workflow the Memory Layer is built around. In 1.1, steps
2 and 6 are reachable via `POST /api/steward/lessons/search` —
clients (the editor's failure-helper UI, the future Incident Analyst
sub-agent) call it explicitly. **Automatic invocation** when a pipeline
fails — the "Steward sees the error and proactively surfaces the
matching lesson without being asked" path — ships with the Incident
Analyst module in 1.2.

```
Pipeline fails
      |
      v
1. Steward reads the current error
      |
      v
2. Searches existing lessons by source + error substring
      |
      v
3. Checks source memory (matching source_quirk / schema_drift entries)
      |
      v
4. Compares against recent schema changes
      |
      v
5. Surfaces the highest-confidence matching lesson
      |
      v
6. Recommends the lesson's approved_fix verbatim
      |
      v
7. Asks the operator to confirm
      |
      v
8. On resolution, calls revalidate(): occurrence_count++,
   last_validated = now, confidence may promote LOW->MEDIUM->HIGH
```

If no lesson matches, Steward proposes a new one (status: PROPOSED).
A PROPOSED lesson does NOT influence future reasoning until a human
approves it. This is architectural Rule 3 — **learning is gated**.

---

## Lifecycle

```
                 propose()
                    |
                    v
              +-----------+
              | PROPOSED  | <-- Steward will NOT use this for matching
              +-----+-----+
                    |
       +------------+------------+
       | approve()       reject()|
       v                          v
   +----------+              +----------+
   | APPROVED |              | REJECTED |  (kept on disk for audit;
   +----+-----+              +----------+   suppresses re-propose)
        |
        | revalidate()
        | (occurrence_count++, push clock)
        v
   +----------+
   | APPROVED |
   +----+-----+
        |
        | validity_days elapsed without revalidate()
        v
   +----------+
   |  STALE   | <-- hidden from default queries; revalidate() revives
   +----------+
```

Confidence promotes deterministically:

| Condition                                | Confidence |
|------------------------------------------|------------|
| `REJECTED`                               | LOW        |
| `APPROVED` + `occurrence_count >= 5`     | HIGH       |
| `APPROVED` OR `occurrence_count >= 2`    | MEDIUM     |
| Otherwise                                | LOW        |

---

## Storage layout

```
<data_dir>/steward/<workspace_id>/
  ├── settings.json
  ├── suppressions.json
  ├── memory.jsonl          <-- operational event journal (high-volume,
  |                              ephemeral, see memory.py docstring)
  └── lessons/              <-- the Memory Layer (low-volume, durable)
      ├── ora12154.yaml
      ├── ora12154.json
      ├── salesforce-empty-window.yaml
      ├── salesforce-empty-window.json
      ├── snowflake-warehouse-cost.yaml
      └── snowflake-warehouse-cost.json
```

**Two stores, one Memory Layer.** The event journal (`memory.jsonl`)
answers "what has the Steward seen recently?" — high-volume, append-only,
rotatable. The lesson store (`lessons/`) answers "what has this team
**learned**?" — low-volume, durable, repo-friendly, version-controllable.

---

## HTTP API

| Method | Endpoint                                          | Purpose                                                                              |
|--------|---------------------------------------------------|--------------------------------------------------------------------------------------|
| GET    | `/api/steward/lessons`                            | List with optional `status`, `lesson_type`, `source`, `pipeline` filters             |
| GET    | `/api/steward/lessons/stats`                      | Counters: by_status, by_type, total                                                  |
| GET    | `/api/steward/lessons/{id}`                       | One lesson by ID                                                                     |
| POST   | `/api/steward/lessons`                            | Propose a new lesson (status starts PROPOSED)                                        |
| POST   | `/api/steward/lessons/{id}/approve`               | Body `{"approver": "..."}` → PROPOSED to APPROVED                                    |
| POST   | `/api/steward/lessons/{id}/reject`                | Body `{"reviewer": "...", "reason": "..."}` → mark incorrect, reason kept in evidence |
| POST   | `/api/steward/lessons/{id}/revalidate`            | Body `{"reviewer": "..."}` → occurrence_count++, refresh clock, may promote confidence |
| POST   | `/api/steward/lessons/search`                     | Body `{"source": "...", "error": "..."}` → ranked APPROVED matches (step 2 of the 8-step flow) |
| DELETE | `/api/steward/lessons/{id}`                       | Hard-delete (only suitable for REJECTED entries)                                     |

All endpoints are workspace-scoped (default `"default"`) and auth-gated.

---

## How this is different from generic "AI memory"

A few things ChatGPT-style "memory" features get wrong that the
Memory Layer is built to avoid:

| Generic AI memory             | F-Pulse Memory Layer                                         |
|-------------------------------|--------------------------------------------------------------|
| Stores raw chat transcripts   | Stores typed, structured lessons with explicit fields        |
| Silent auto-promotion         | Explicit `propose() -> approve()` step; PROPOSED is inert    |
| Vector-DB fuzzy retrieval     | Deterministic source + substring match; ranked by confidence |
| Opaque "the AI remembered X"  | Every lesson has YAML on disk with `evidence` provenance     |
| Drifts as models change       | YAML format is model-agnostic; LLM only renders the body     |

The bet: **the most useful memory in a data team is the one a human
already vouched for.** Steward's role is to *propose* — humans
*approve*. That's what makes the recommendations safe to act on.

---

## What the LLM does and does not do in the Memory Layer

| Step                                            | Code? | LLM? |
|-------------------------------------------------|-------|------|
| Detect a recurring failure pattern              | Yes   | No   |
| Compute the search-ranking score                | Yes   | No   |
| Match an incoming error to existing lessons     | Yes   | No   |
| Promote PROPOSED to APPROVED                    | Yes   | No   |
| Phrase the proposed_fix in friendly language    | Yes (operator-written, currently) | Later: a narration shell that drafts the prose; user still edits + approves |
| Translate `approved_fix` into a runtime patch   | NEVER | NEVER (read-only rule) |

The Memory Layer works fully with the LLM disabled. With an LLM
configured, the only thing that changes is that proposed lessons can
come with a *draft* `approved_fix` body the operator polishes before
clicking Approve. The decision-making path stays in code.

---

## Roadmap dependencies

The Memory Layer unlocks several follow-on sub-agents:

- **Autopsy (1.2)** — uses `search_for_failure()` to find matching
  past incidents when a new failure lands.
- **Foreseer (1.3)** — proposes `schema_drift` and `volume_anomaly`
  lessons from statistical signals; operator approves to make them
  durable.
- **Curator (1.4)** — distills `EPULSE_RUNBOOK.md` from the lesson
  set, grouped by source and lesson_type, so onboarding a new
  engineer is "read the runbook" instead of "shadow the senior."
- **Optimizer (2.0)** — consumes `cost_anomaly` lessons to propose
  warehouse / instance changes.

Every sub-agent reads + writes through the same `LessonStore`. None
of them invent a parallel knowledge layer.
