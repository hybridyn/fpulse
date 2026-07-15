# F-Pulse RBAC — two systems, easy to confuse

Per `edition-matrix.md` line 122-127, F-Pulse has **TWO distinct RBAC systems**. They serve different purposes; they can coexist; OSS Free has only the first; Plus has both.

## System 1: Agent-tool RBAC (OSS + Plus, always-on)

This RBAC gates **what the AI Copilot can call** on behalf of the user.

**Dimensions:**
- **4 user roles** — `viewer`, `developer`, `admin`, `super_admin`
- **2 environments** — DEV, PROD
- **3 tool tiers** — READ, SAFE_WRITE, HIGH_IMPACT_WRITE

**Resolution matrix** (excerpt, see `backend/fpulse/ai/rbac.py` for the canonical version):

| Role | DEV tiers | PROD tiers |
| --- | --- | --- |
| `viewer` | READ | READ |
| `developer` | READ + SAFE_WRITE | READ |
| `admin` | READ + SAFE_WRITE + HIGH_IMPACT_WRITE | READ + SAFE_WRITE |
| `super_admin` | all tiers | all tiers |

**Where it's enforced:** every tool invocation from `AgentRunner._execute_tool` checks the role × env × tier intersection. A mismatch returns a `policy_block` outcome with a `rbac:role_X_no_Y_access_in_Z` reason.

**Where the role comes from:**
- OSS Free: the operator's user record. Defaults to `developer` for the seeded admin.
- Plus: workspace-RBAC role mapped down to one of the four agent-RBAC roles.

**This RBAC is in OSS Free.** It is not a Plus feature.

## System 2: Workspace RBAC (F-Pulse+ only)

This RBAC gates **who can edit what in the workspace**. Five tiers, per-environment permissions, approval rights, seat limits.

**The 5 tiers** (per `edition-matrix.md` line 127):

1. **Super Admin** — install-wide. Manages all workspaces, billing, license. Can do anything.
2. **Workspace Admin** — owns one workspace. Add/remove members, configure approvers, manage credentials, deploy to PROD.
3. **Data Engineer** — write in DEV; PROD writes need approval. Can build pipelines, run them in DEV.
4. **Analyst** — read everywhere; can approve PROD changes; cannot edit pipelines.
5. **Viewer** — read-only. Sees pipelines, executions, dashboards. Cannot save or run.

**Distinct from agent RBAC:** workspace-RBAC sets who can hit `POST /api/workflows` (edit a pipeline). Agent-RBAC sets what the LLM can call on the user's behalf via `POST /api/ai/agent`. Both checks fire on every action; both must pass.

**OSS Free does NOT have workspace RBAC.** The single operator is effectively the workspace admin and is treated as such by the API.

## How the two systems interact (Plus only)

In Plus, when an Analyst (workspace-RBAC) opens the chat:
- Workspace RBAC says: read everywhere, no PROD edit.
- Agent RBAC: the user's role maps to either `viewer` or a custom mapping. By default, Analyst → `developer` agent role in DEV, `viewer` in PROD.
- The Analyst can ask the LLM to draft a pipeline (SAFE_WRITE) but cannot apply it to PROD without approval.

## Anti-patterns

- ❌ Telling a Free user "your role doesn't have permission to do X" when X is just blocked by agent-RBAC. The fix is to surface that this is the AGENT layer, not the workspace layer (which doesn't exist in Free).
- ❌ Conflating the two systems in error messages. The error "your role has no allowed tiers in env=prod" is agent-RBAC, not workspace-RBAC.
- ❌ Telling a Free user "ask your workspace admin to add you to the developers group" — there is no group concept in Free. The single user is the admin.
- ❌ "Use SAML SSO to manage roles" — SSO is Plus-only, and even there it federates identity not roles. Roles still live in F-Pulse's user store.

## Quick FAQ

**Q: I'm getting "your role has no allowed tiers" — what role am I?**
A: Use the chat fast lane phrase "what's my role" — answers instantly from the session context with your role + environment + edition.

**Q: How do I change my role in OSS Free?**
A: There's a single user — the admin. There's nothing to change. (If you're seeing a non-admin role you may be running with a custom env var; check `Settings → Account`.)

**Q: How do I add a teammate to my workspace?**
A: F-Pulse+. OSS Free is single-user.

**Q: Where's the audit log of role changes?**
A: F-Pulse+ has the persistent retention-policy audit log. OSS has a basic in-process audit table that captures recent actions but doesn't enforce retention.
