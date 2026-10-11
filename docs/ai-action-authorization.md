# AI action authorization — how the Copilot can't go rogue

The value of an agent stack is not the model; it's the constrained layer around
it. This documents F-Pulse's model for that layer — the reason the Copilot can be
allowed near real data — its current state (honestly: what's complete vs partial),
and the plan to close the gap so "the AI can't take an action it isn't allowed to"
is a guarantee, not an aspiration.

## The invariant

**The agent can only do what a typed tool exposes, and never more than the
invoking user is allowed to do.** There is no path from the LLM to raw SQL, the
filesystem, or a mutation that isn't a registered, tiered, authorized tool. Data
correctness and execution stay **deterministic** — the LLM explains and proposes;
it never *is* the execution or the authorization decision.

## The layers (today)

1. **Typed tools only** (`ai/tools/`). The agent acts through a fixed registry
   (`register_initial_tools`), each an explicit `ToolDefinition` with a JSON input
   schema. No tool for it → the agent can't do it. It never emits raw SQL.
2. **Tiers** (`ai/tools/base.py`): `READ` / `SAFE_WRITE` / `HIGH_IMPACT_WRITE`.
   Read = permissive + no confirmation. Safe-write = standard RBAC + inline
   preview. High-impact = strict RBAC + **required confirmation** + **dry-run by
   default** + elevated audit. Write tiers **must** carry an idempotency key
   (enforced in `ToolDefinition.__post_init__`).
3. **Authorization** (`ai/rbac.py`) — "may this user attempt this?" An env-aware
   role×tier matrix (viewer / developer / admin / super_admin × dev / prod).
   Plus enforces it closed-world; **OSS is single-bootstrap-user, open-world**
   (RBAC is a Plus feature) — the code is identical open-core, only the
   unknown-role default flips.
4. **Policy** (`governance.py`) — "is this allowed *in this context, now*?" A
   separate concern from authz, so tightening one doesn't entangle the other.
5. **Sanitization** (`ai/sanitize.py`) — PII/secret redaction before any prompt
   leaves; the model never sees credentials or (by default) row data.
6. **Audit** — every tool call is traceable (`ai/trace_store.py`; Plus adds the
   hash-chained signed audit).
7. **MCP parity** (`api/mcp.py`) — external assistants reach the *same* registry
   under the *same* tier + RBAC gate; only READ-tier is exposed unless an operator
   opts in.

## Current state — honest

- **Reads:** fully typed and schema-only — solid.
- **Mutations:** the pattern is **draft-then-apply**. The `SAFE_WRITE` tools
  (`draft_pipeline_from_intent`, `modify_pipeline_step`, `draft_alert_rule`,
  `draft_connector_from_openapi/_samples`, `compose_report`, `test_connection`)
  produce **inert drafts**; the **only** real mutation the agent can commit is
  `apply_pipeline_draft` (the single `HIGH_IMPACT_WRITE`, gated by RBAC +
  dry-run-by-default + confirmation).
- **Consequence:** the invariant *holds* today largely because the mutation
  surface is tiny (one apply tool) and everything else is a draft. The claim
  "the AI can't go rogue" is **true but narrow** — it hasn't been stress-tested
  across a broad mutation surface because that surface doesn't exist yet.

## Gap to close (so the guarantee scales with the surface)

1. **Comprehensive typed mutation coverage.** As mutating capabilities are added
   (create schedule, send to destination, rotate credential, run backfill…),
   each ships as a tiered tool — never a bare endpoint the agent can reach. The
   registry stays the *complete* list of what the agent can do.
2. **Confirmation-card flow, complete.** Every `HIGH_IMPACT_WRITE` renders a
   confirmation card with the dry-run diff before commit; no silent commits.
3. **Policy engine, complete** (`governance.py`): env-crossing, destination
   allowlists, PII-movement, per-user rate/quota — evaluated on every write, in
   addition to authz.
4. **Caller's grants, end-to-end.** The tool's authorization always uses the
   *invoking user's* role + environment (no privilege escalation via the agent);
   a scheduled/agentic run carries the schedule owner's grants, nothing more.
5. **Kill switch / global dry-run.** An operator toggle that forces every write
   tool to dry-run, for incident response.

## Regression tests the guarantee needs

Prompt-injection attempts to invoke an out-of-tier tool → `policy_block`; a
`viewer` cannot reach a write tier; an unknown role is denied on Plus (allowed on
OSS single-user); a `HIGH_IMPACT_WRITE` without confirmation does not commit;
cancellation and provider timeout leave no partial mutation. These belong in CI
alongside the eval harness so every prompt/tool change re-verifies the boundary.

*Grounded in `ai/tools/base.py`, `ai/rbac.py`, `api/mcp.py`, and the tool
registry as of this writing. If a behavior here disagrees with the code, the
code wins — file an issue so this doc is corrected.*
