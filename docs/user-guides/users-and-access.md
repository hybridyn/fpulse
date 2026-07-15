# Users & Access Control — User Guide

**Audience:** Administrators (primary), all users (informational)

Everything about *who* can do *what* in F-Pulse.

---

## 1. The 5-tier role model

| Role | Scope | Typical duties |
|---|---|---|
| `super_admin` | Instance-wide | Install, license, cross-workspace admin, emergency access |
| `admin` | Workspace | Manage users, projects, connections, approve PROD deploys |
| `lead` | Workspace | Approve PROD deploys, mentor engineers; does **not** manage users |
| `data_engineer` | Project (scoped by `user.projects` / `project.members`) | Build and test pipelines in DEV, submit for review |
| `analyst` | Project | Read pipeline output, build read-only reports |
| `viewer` | Project | Read-only — dashboards and execution history, no editing |

**Rule of thumb:** grant the least role that gets the job done. Ten analysts are safer than ten admins.

---

## 2. Adding a user

**Who:** admin+.

**Via UI:** **Admin** page → **Users** → **+ Invite User** → fill email, role, projects → **Send Invite**. The invitee receives an email with a one-time link to set their password.

**Via API:**

```bash
curl -X POST http://localhost:8001/api/auth/invite \
  -H "Cookie: session=..." \
  -d '{
    "email": "new@corp",
    "name": "New Engineer",
    "role": "data_engineer",
    "projects": ["proj_sales"]
  }'
```

The invited user's record is created with `is_active = true` and `password_hash = null`; the invite link carries a signed token that lets them set their password on first login.

---

## 3. Changing a role

Only admins can change roles. A user cannot grant themselves a higher role.

```bash
curl -X PUT http://localhost:8001/api/plus/users/{user_id}/role \
  -H "Cookie: session=..." \
  -d '{"role": "lead"}'
```

**Guardrails:**
- Cannot demote the last `super_admin`. The system always keeps at least one.
- Role changes are written to the audit log (see [Security Guide](../security/security-compliance.md)).

---

## 4. Deactivating / reactivating a user

Deactivation blocks login but preserves the user record (and their pipeline ownership).

```bash
curl -X POST http://localhost:8001/api/plus/users/{user_id}/deactivate -H "Cookie: session=..."
curl -X POST http://localhost:8001/api/plus/users/{user_id}/activate   -H "Cookie: session=..."
```

**Never** hard-delete a user who owns pipelines — you'll orphan the ownership. Deactivate instead, then reassign pipelines to a new owner before deletion.

---

## 5. Password policy

Every password is validated against the same policy regardless of endpoint (register, invite, reset, self-change). Policy:

- Minimum length enforced centrally.
- No reuse of email, name, or common dictionary words.
- Strength score shown to the user as a live checklist.

API response on weak password:

```json
{
  "code": "weak_password",
  "message": "Password does not meet the strength policy.",
  "failures": [...],
  "suggestions": [...],
  "score": 37,
  "label": "fair"
}
```

---

## 6. Session concurrency

Admins choose how many simultaneous sessions a user can hold (Admin → Settings → Session Policy):

| Mode | Behavior |
|---|---|
| `unlimited` | No limit (Free default) |
| `single` | One session; new login kills old (Plus default) |
| `capped` | Up to N concurrent sessions |

Active sessions are visible to admins:

```bash
curl -X GET http://localhost:8001/api/plus/sessions/active -H "Cookie: session=..."
```

Admins can force-revoke:

```bash
curl -X POST http://localhost:8001/api/plus/sessions/revoke/{user_id} -H "Cookie: session=..."
```

---

## 7. Environment-scoped access (DEV vs PROD)

F-Pulse separates DEV and PROD. **Developers default to DEV-only everywhere** — dashboards, pipelines, executions, **and System Reports**. Admin-equivalent roles (super_admin, admin, lead) see PROD without configuration. An admin grants **prod-permissions** per user when a developer needs PROD visibility:

```bash
curl -X PUT http://localhost:8001/api/plus/users/{user_id}/prod-permissions \
  -H "Cookie: session=..." \
  -d '{
    "can_view": true,
    "can_trigger": false,
    "can_deploy": false
  }'
```

| Permission | What it allows |
|---|---|
| `can_view` | See PROD dashboards, executions, metrics, **and PROD-scoped System Reports** |
| `can_trigger` | Manually run a deployed pipeline in PROD |
| `can_deploy` | Run `/deploy` to pin a new version |

### 7.1 DEV-only rule — who it applies to

| Role | Default env visibility | Can be granted PROD view? |
|---|---|---|
| `super_admin` | All | — (already all) |
| `admin` | All | — (already all) |
| `lead` | All | — (already all) |
| `data_engineer` | **DEV only** | Yes, via `prod_permissions.can_view` |
| `analyst` | **DEV only** | Yes, via `prod_permissions.can_view` |
| `viewer` | **DEV only** | Yes, via `prod_permissions.can_view` |

### 7.2 How System Reports respect the rule

When a developer opens **Insights → Reports**:

- The **Environment** selector's `PROD only` button is **disabled** with a lock icon and tooltip.
- The **All** option is labelled `"DEV only (your role)"` — because for a DEV-locked user, "all" *is* "DEV".
- The backend additionally enforces the rule: hitting the `/api/reports/inventory` endpoint directly with `?env=prod` returns a DEV-only report, not an error. This is intentional — fail safe, not loud.
- The downloaded report cover page reflects what was actually served (e.g. `Environment: DEV only`).

A developer who should "see PROD dashboards but not edit" gets `can_view = true` — they can then generate PROD-scoped reports in read mode. Others stay DEV-locked.

### 7.3 Why DEV-only for developers

The governance story: developers build and test in DEV. PROD is the approved, deployed, scheduled surface maintained by leads and admins. Letting a developer pull a full PROD inventory — including deployed versions, live connection usage, PROD schedules, and PROD alerts — would side-channel information that the rest of the product (pipeline page, executions, dashboards) already DEV-scopes. The Reports endpoint honours the same boundary.

---

## 8. Approval gates

Pipelines going DEV → PROD must pass an approval gate. Gates live at three scopes; most specific wins:

| Scope | Use |
|---|---|
| `pipeline` | Override for one specific pipeline |
| `project` | One gate for every pipeline in the project |
| `global` | Workspace default |

Configure in Admin → **Approval Gates**. Each gate specifies:

- `approvers: [user_id, ...]` — who can approve
- `min_approvals: N` — how many approvals needed (default 1)
- `notify_channels: [in_app, email, slack]`

If no gate is defined, the notification falls back to all admins/leads in the workspace.

---

## 9. IP restriction

Plus-tier admins can whitelist IP ranges for the whole workspace. Requests from other IPs are rejected at the middleware before hitting any endpoint.

Admin → **Settings** → **IP Allow List** → enter CIDRs. Leave empty to disable.

---

## 10. Audit log

Every state-changing API call is appended to the audit log. Visible to admins:

```bash
curl -X GET "http://localhost:8001/api/plus/audit?limit=100" -H "Cookie: session=..."
```

Retention is configurable (default 90 days). See [Administrator Guide](../admin/administrator-guide.md).

---

## 11. API reference (essentials)

| Method | Path | Auth |
|---|---|---|
| `POST` | `/api/auth/login` | public |
| `POST` | `/api/auth/logout` | auth |
| `POST` | `/api/auth/invite` | admin |
| `POST` | `/api/auth/register` | (from invite token) |
| `POST` | `/api/auth/change-password` | auth |
| `GET` | `/api/plus/users` | admin |
| `PUT` | `/api/plus/users/{id}/role` | admin |
| `POST` | `/api/plus/users/{id}/deactivate` | admin |
| `POST` | `/api/plus/users/{id}/activate` | admin |
| `GET` | `/api/plus/users/{id}/prod-permissions` | admin |
| `PUT` | `/api/plus/users/{id}/prod-permissions` | admin |
| `GET` | `/api/plus/sessions/active` | admin |
| `POST` | `/api/plus/sessions/revoke/{user_id}` | admin |
| `GET` | `/api/plus/audit` | admin |

---

## 12. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "Invalid email or password" | Wrong password or user deactivated | Check active status; use password reset |
| "Account is deactivated" | `is_active = false` | Admin reactivates |
| Session kicked out after login | `session_mode = single` kicked prior session | Expected behavior. Log in again. |
| "Permission denied" on a PROD action | Missing prod-permission | Admin grants via `PUT /users/{id}/prod-permissions` |
| "Cannot change role of last super_admin" | System refuses to leave the instance admin-less | Assign another super_admin first |
| User not receiving invites | SMTP not configured | Admin → Settings → Email |

---

**Change log**

| Date | Change |
|---|---|
| 2026-04-22 | Initial publication; 5-tier roles + environment-scoped permissions + approval gates |
