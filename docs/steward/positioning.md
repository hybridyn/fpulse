# F-Pulse Steward — Positioning

A short summary of what the Steward is and why it matters, for anyone
asking "what's the actual differentiator here?" — separate from the
contributor-facing [`architecture.md`](architecture.md).

---

## 60-second pitch

F-Pulse Steward is a **read-only workspace observer** with a gated
Memory Layer. It watches across pipelines, never mutates them, and
stores team-approved lessons separately from raw events.

**Active detection today:** duplicate-source + duplicate-pipeline
(architecture); connector health (auth-failure / unreachable /
rate-limit / near-expiry); schema drift + the data-quality engine
(null-rate / freshness / volume-anomaly / partition / row-count);
node empty-output; warehouse-waste (cost); and governance
(env-crossing / unapproved-destination / PII-leak) — plus user-defined
rules. **Still contract-ready (detector deferred):** pipeline-level
SLA / partial-output / retry-storm, structural join/cast checks,
credential-sprawl, and cost-drift — the 7-level contract is shipped
(finding model, suppression / notification de-dup, lesson store), so
those detectors slot in without re-shaping storage, UI, or alerts.

Steward is built on three architectural bets:

1. **Pipeline *judgment* is more valuable than pipeline *generation*.**
   Detecting that two pipelines read the same source for different
   destinations is worth more than helping a user author either of
   them faster.
2. **A lesson store is only useful if it's gated.** Steward's Memory
   Layer holds typed lessons (source quirks, failure-fix pairs, retry
   rules, intentional duplicates) that are **inert until a human
   approves them**. Dismiss-with-reason and resolve are distinct flows
   that feed suppression / closure — they do **not** auto-create
   lessons, which keeps the lesson store free of exception text and
   2-AM operator scratch notes.
3. **Reliability observation is a layer, not the execution engine.**
   Steward observes, evaluates, recommends, escalates. Extraction,
   transformation, and scheduling stay in the F-Pulse core.

It ships in F-Pulse OSS — Apache 2.0, not paywalled. F-Pulse+ adds
team-scale capabilities around it (cross-workspace correlation,
shared memory across teams, RBAC-aware approval chains) without
gating the detection capability itself.

---

## Four pillars — what Steward is, honestly

Steward is built to grow along four pillars. **Observe has shipped active
detection across six levels** (architecture, connector, data, node, cost,
governance), and **Learn has shipped** (the gated Memory Layer of
human-approved lessons). Explain (richer narration) and Optimize (cost
recommendations) are framework-ready — data model, storage, surface, and
lesson plumbing all in place — with their specialists landing in later
releases. This split is the most important thing to internalise about
where Steward is today vs where it's going.

### Pillar 1 — Observe — **shipped in 1.1**

Architectural intelligence: catch structural problems across the workspace
that single-pipeline tools can't see.

- **Active detection (Archeologist):** duplicate-source and
  duplicate-pipeline patterns across all workspace pipelines.
- **Alert-fatigue prevention by construction:** findings escalate only
  after BOTH a count threshold AND a minimum-hours-since-first threshold
  (time-clamp). Resolved patterns that return are tagged `rebounded` with
  `previously_resolved_at`. Dismiss-with-reason resets the per-signature
  counter so a dismissed-then-recurring finding rebuilds from zero.
- **Notification de-dup invariant:** at-most-one notification per
  (user, finding, severity, rebound-state).

### Pillar 2 — Explain — **roadmap (Incident Analyst, 1.2)**

When a pipeline fails, surface probable root cause + similar past incidents
from the Memory Layer + downstream impact. **Not shipped in 1.1.** The lesson
search API that Incident Analyst will call (`POST /lessons/search`) is shipped;
the auto-invocation on failure is the 1.2 work.

### Pillar 3 — Learn — **scaffolding shipped in 1.1; growing**

A gated lesson store, separate from the operational journal and from
dismiss-suppression.

- **Shipped:** `POST /lessons` for explicit human-proposed lessons; status
  starts `PROPOSED` and stays inert until a human `approve`s it; YAML+JSON
  on disk for hand-review; 10 lesson categories.
- **Shipped 2026-06-07:** the resolve endpoint now accepts an optional
  `fix_note` — when supplied, it's sanitised through the same 5-regex
  sweep as dismiss reasons and filed as a `PROPOSED` lesson candidate.
  This closes the loop: operators capturing what worked become the organic
  feeder for the lesson store, without ever polluting it with exception
  text (dismiss reasons still flow to suppression only, not to lessons).
- **Roadmap (Curator, 1.4):** automatic distillation across approved
  lessons into a single `EPULSE_RUNBOOK.md` per workspace.

### Pillar 4 — Optimize — **roadmap (Optimizer, 2.0)**

Cost + performance recommendations: redundant transfer, warehouse waste,
consolidation suggestions with measurable savings. **Not shipped.** The
`COST_DRIFT` / `WAREHOUSE_WASTE` / `COST_RECOMMENDATION` finding kinds
exist in the enum so the UI + notification bridge already handle them;
detectors land in 2.0.

## The architectural bet that makes all four pillars possible

Detection is plain code — signatures, lineage matching, statistical anomaly
analysis — never gated on an LLM. The LLM (when present) only phrases findings
in natural language; correctness never depends on it. That deterministic core
is what lets the contract stay stable across releases — adding a new detector
in 1.2 doesn't reshape the finding model, the lesson store, the notification
de-dup, or the UI.

---

## Where the Steward sits in the F-Pulse stack

```
              +-------------------------------+
              |        F-Pulse Steward        |
              |  Memory Layer + sub-modules   |
              |     (read-only, gated)        |
              +---------------+---------------+
                              |
                              v
                  +-----------+-----------+
                  |     F-Pulse Core      |
                  |  executor + scheduler |
                  |  + storage + UI       |
                  +-----------------------+
                              |
                              v
                +-------------+--------------+
                |  Workflow store, audit log,|
                |  execution history, etc.   |
                +----------------------------+
```

The Steward consumes snapshots from the layers below. It never
mutates them on its own.

---

## What it explicitly is NOT

- It is not a full SIEM.
- It is not a replacement for the scheduler.
- It is not a generic AI chatbot.
- It is not a business-logic authoring engine.
- It is not a universal root-cause oracle.
- It is not a cross-tenant intelligence network by default.

Naming the non-goals prevents scope drift and lets contributors
preserve the product identity.

---

## Sub-modules (today and roadmap)

Each module produces typed `StewardFinding` records that flow through
the same surface (eye-icon dropdown + notification bell) and the same
durable `LessonStore` (Memory Layer).

| Status  | Module             | What it does                                                     |
|---------|--------------------|------------------------------------------------------------------|
| Shipped | **Archeologist**   | Duplicate-source + duplicate-pipeline detection                  |
| Planned | **Autopsy**        | Failure RCA — uses the Memory Layer to match past incidents      |
| Planned | **Foreseer**       | Volume + schema-drift anomaly detection                          |
| Planned | **Curator**        | Distills a runbook from approved lessons                         |
| Planned | **Optimizer**      | Cost + performance recommendations                               |

Only Archeologist ships today. The rest are intent, not commitments —
no dates, no version numbers, because we won't promise a release we
haven't built.

These are **deterministic specialist modules**, not autonomous LLM
agents. The "agent" word in the broader market suggests "an LLM
decides things" — that's not what F-Pulse Steward modules are. They
are narrow, testable, code-driven analyzers. The LLM, when present,
is used only as a narration shell for the bodies of findings and
proposed lesson drafts.

---

## OSS vs Plus — horizontal split, not vertical

| | **OSS (this build)** | **Plus (later)** |
|---|---|---|
| Detection capability | Full Archeologist | Same — never gated |
| Memory Layer storage | Per-workspace lessons + journal | Multi-workspace federated storage |
| Notifications | Eye-icon dropdown + in-app bell + email/Slack/webhook | RBAC-aware approval routing on proposed lessons |
| Lessons scope | This workspace | Cross-workspace correlation |
| Retention | Local files, user-managed | Enterprise retention SLAs |

The split is **horizontal**: OSS gets the complete capability for a
single team. Plus adds scale — team-of-teams, multi-workspace,
governance. Plus is never "OSS minus the good parts."

---

## TL;DR for buyers

> Most pipeline tools tell you when a job failed. In 1.1, F-Pulse
> Steward also tells you which jobs your team built twice by accident,
> with alert-fatigue guards (time-clamped escalation, dismiss-resets,
> rebound tagging) so you don't drown in repeats. The Memory Layer is
> a gated lesson store — humans approve every entry before it
> influences Steward reasoning. From 1.2, the same surface starts
> answering "which failures have we already solved before" — that
> auto-recall lives in the Incident Analyst module, not in 1.1.

---

## See also

- [`architecture.md`](architecture.md) — full design rationale, code
  walkthrough, file map, performance characteristics
- [`memory-layer.md`](memory-layer.md) — the durable lesson surface
  in depth
- [`overview.md`](overview.md) — user-facing how-to-use guide
