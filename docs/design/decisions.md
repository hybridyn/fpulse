# F-Pulse OSS — Design Decisions Log

> Locked decisions referenced from PAGE_DESIGN_AUDIT.md and the implementation plan. Captured 2026-05-05.
>
> Every entry: the question, the decision, the rationale, and the supersedence rule.

---

## D-001 — Lineage page placement
**Question:** Lineage is routable but has no nav entry. Top-level menu, embedded in Pipelines master-detail, or hybrid?

**Decision:** **Embedded** (Option B). Add a "Lineage" tab to the Pipelines master-detail panel for per-pipeline lineage. The standalone `#lineage` route still works for deep links and shows workspace-wide lineage when called directly, but is not promoted to the main menu.

**Rationale:** Lineage is most useful in context of a specific pipeline. Promoting to top-level menu adds clutter; users typically arrive at lineage *from* a pipeline, not the other way around.

**Supersede when:** workspace-wide lineage becomes a primary use case (e.g., a customer specifically asks "show me everything that depends on table X" without first picking a pipeline).

---

## D-002 — Connection pool sizing
**Question:** When connection pooling lands (Critical #5), what's the per-connection concurrent cap?

**Decision:** **5 concurrent connections per `connection_id`**, configurable via env var `FPULSE_CONNECTION_POOL_SIZE` (defaults 5). Pool is keyed by `(connection_id, run_id)` so each run gets its own slice; pool entries are returned at run-end (not held for the next run, to keep credential rotation working).

**Rationale:** 5 is enough headroom for a typical solo-dev workflow with parallel steps; small enough that a single bad pipeline can't exhaust a downstream Postgres's `max_connections=100`. Lower bound chosen to fail fast under DB pressure rather than mask the problem.

**Supersede when:** users routinely report "too few connections" (raise default) or "DB exhaustion" (lower default + add per-DB advice).

---

## D-003 — Default table density
**Question:** Compact / Comfortable / Spacious — which is the default when the user hasn't set a preference?

**Decision:** **Comfortable** (current row height — `py-3`). Compact and Spacious are opt-in via the per-table density toggle, persisted in localStorage as `fpulse_table_density`.

**Rationale:** Comfortable matches the existing experience so the default-out-of-the-box reads consistent with what users have today. Power users get density via the toggle; new users aren't penalized with a too-tight or too-airy first impression.

**Supersede when:** telemetry (post-launch) shows >40% of users actively switch to Compact or Spacious — promote the popular choice to the default.

---

## D-004 — Skeleton vs spinner cutoff
**Question:** When a fetch is in flight, when do we show a skeleton vs a spinner vs nothing?

**Decision:**
- **0–200 ms**: nothing (don't flicker a loader for a fast response)
- **200 ms+**: skeleton placeholder (preserves layout; content fades in when arrives)
- **Spinners only inside buttons** during action invocations (Save, Run, Test) — not for page-level loading

**Rationale:** Sub-200ms responses are perceptually instant; showing any loader for them is jitter. Beyond 200ms, the user notices the wait — a skeleton tells them "content is coming and will land here". Spinners on top of skeletons read as "broken / double-loading" so we use them only for explicit user-initiated actions.

**Supersede when:** real-world telemetry shows >5% of fetches sit in the 100-300ms window — bring the cutoff down to 100ms.

---

## D-005 — Auto-clear chat threshold
**Question:** Should the Copilot chat auto-clear to keep the KV cache from bloating? If so, when?

**Decision:** **30 minutes idle** triggers an auto-clear *prompt* (not a silent wipe). The user sees a banner: *"Your chat has been idle 30 min. Clear to keep responses fast?"* with [Clear] and [Keep] buttons. **No turn-count cap** — long deliberate sessions stay intact.

**Rationale:** Silent wipes are user-hostile (lost context). A prompt respects the user's deliberate sessions while still giving the speed-recovery option. 30 minutes is long enough that mid-task tab-switches don't trigger it; short enough that overnight tabs catch it on next focus.

**Supersede when:** the cache-pressure problem becomes a real user complaint — switch to silent auto-clear at 60 min idle and add a "session pinned" toggle for users who want to opt out.

---

## D-006 — Eval harness cadence
**Question:** Auto-run on a schedule, or on-demand only?

**Decision:** **On-demand only**. The Eval Harness sub-tab in AI-Hub has a [Re-run all] button + [Re-run category] buttons. No automated runs — each invocation is a deliberate user click.

**Rationale:** OSS solo-dev installs don't have a CI pipeline; auto-running burns CPU on a laptop the user might be doing real work on. The audit story is *"the eval ran on demand and these are the results"* — not *"the eval runs every night without my knowing"*.

**Supersede when:** the project ships a self-hosted CI runner profile (out of scope for OSS Free) — that audience expects automated runs.

---

## D-007 — Settings tab layout (horizontal stays)
**Question:** Should Settings switch to a vertical left-rail tab layout (modern SaaS pattern) or keep horizontal tabs in the header?

**Decision:** **Keep horizontal tabs in the header.** Tried the vertical rail in one session; user explicitly preferred the original layout.

**Rationale:** F-Pulse's top horizontal nav is a deliberate identity choice (per D-NN around top-nav preservation). Settings tabs at the top match that visual rhythm — vertical rails introduce a second axis of navigation that doesn't compose with the existing top nav. The tab count (4) is small enough that horizontal scales fine. The 1100px reading-column cap on the content pane already addresses the "stretched on wide screen" complaint independently.

**Supersede when:** the Settings tab count grows past 6-7 entries — at that point horizontal starts to crowd and the vertical rail becomes the better trade-off.

---

## D-008 — Executions detail stays full-screen (no drawer migration)
**Question:** Should the Executions detail experience migrate to `<DetailDrawer>` for consistency with Pipelines / Connections / Credentials master-detail?

**Decision:** **Keep the full-screen overlay.** No drawer migration.

**Rationale:** Execution detail carries fundamentally more visual content than other entity detail panels — DAG canvas, multi-step timeline, full logs viewer, metrics breakdown, input/output sample rows. These need wide horizontal real estate to be useful. A 720px drawer would either crowd them or hide them behind sub-tabs, both regressions. Master-detail (drawer alongside list) makes sense when the detail content is naturally narrow (a credential's metadata, a pipeline's summary). When the detail is its own workspace (an execution being diagnosed), full-screen is correct.

**Supersede when:** F-Pulse adds a "compare two runs side-by-side" feature — at that point keeping the list visible alongside might become useful, and a 50/50 split-view (not a 720px drawer) becomes the right shape.

---

## Decision-making protocol

When a future PR / design discussion needs a decision and no entry exists here:

1. Pick the answer that's safest for the **solo dev on their laptop**
2. Pick the answer that **doesn't lock out a future Plus differentiation** (e.g., "team approvals" stays Plus, OSS gets the simpler path)
3. Pick the answer that **degrades gracefully when data is missing** (empty workspace = empty state, not error)
4. Document the new decision here with a `D-XXX` ID and the same four sections (Question / Decision / Rationale / Supersede when)

The bar to add an entry: any choice that touches >1 page or has user-visible consequences.
