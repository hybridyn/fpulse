# Connections — User Guide

**Audience:** All users
**Prerequisites:** Optionally a project you can access

A **Connection** is a saved, reusable configuration for a data source or sink — database URL, S3 bucket, API endpoint, etc. Instead of typing credentials into every pipeline node, you create one connection and reference it by ID everywhere.

---

## 1. Key properties

| Field | Meaning |
|---|---|
| `id` | System-assigned |
| `name` | Human-readable (e.g. `orders-pg`) |
| `type` | One of 45 supported types (postgres, mysql, s3, snowflake, slack, ...) |
| `config` | Connection params: host, port, database, region, etc. |
| `credentials` | Username / password / API key — encrypted at rest |
| `capabilities` | `["read"]`, `["write"]`, or `["read","write"]` — **enforced** at save-time |
| `project_id` | If set, connection is project-scoped; otherwise global |
| `tags` | Free-form labels |

> **F-Pulse+** adds a separate Secrets Manager that stores credentials in a vault with rotation, an `environment` field that scopes connections to DEV vs PROD, and tier-based access control. See [section 9](#9-f-pulse-secrets-manager--environment-scoping).

---

## 2. Creating a connection

### 2.1 Via UI

1. Go to the **Connections** page.
2. Click **+ New Connection**.
3. Pick a connector type. The picker shows status badges:
   - **Certified** — production-grade, no badge
   - **Beta** — usable with gaps; first-use shows a one-time confirmation dialog
   - Hidden: Coming-soon (UI-only stubs) and F-Pulse+ enterprise connectors
4. Fill in config + credentials.
5. Click **Test** to verify. Click **Save**.

### 2.2 Via API

```bash
curl -X POST http://localhost:8001/api/connections/ \
  -H "Content-Type: application/json" \
  -d '{
    "name": "orders-pg",
    "type": "postgresql",
    "config": {"host": "localhost", "port": 5432, "database": "orders"},
    "credentials": {"username": "fpulse", "password": "..."},
    "capabilities": ["read"],
    "project_id": "proj_sales"
  }'
```

---

## 3. The read/write capability split

Every connection is tagged with what it can do:

| Capability | Symbol | Used by |
|---|---|---|
| `read` | ⬆️ | Source nodes |
| `write` | ⬇️ | Sink nodes |
| `read+write` | ⬆️⬇️ | Both (default for databases) |

Notification connectors (Slack, email, webhook) default to `["write"]` only. The validator **refuses to save** a pipeline that wires a source node to a write-only connection — this is the capability split.

In the connection picker, filters automatically narrow to the right capability. A source node only shows read-capable connections.

---

## 4. Testing a connection

Before saving or on demand:

```bash
curl -X POST http://localhost:8001/api/connections/{id}/test
```

Returns `{"ok": true, "duration_ms": 124}` or `{"ok": false, "error": "..."}`. The UI shows a green check or red error inline.

---

## 5. Project-scoped vs global connections

- **Global** (`project_id = null`) — every project can use it. Appropriate for shared warehouses, shared Slack endpoints.
- **Project-scoped** (`project_id = proj_xxx`) — only visible inside that project. Useful when different pipelines need different DB users for the same host.

---

## 6. Connector status & Beta connectors

The connector picker shows status badges so you know what to expect:

| Status | What works | What might not |
|---|---|---|
| **Certified** | Auth + reads + pagination + sinks + error handling — production-grade | — |
| **Beta** | Auth + reads work; pagination may be partial; sinks may use basic INSERT (no bulk-load) | Schema drift logged but not enforced; test fixtures incomplete |

The first time you click a Beta connector, F-Pulse shows a one-time dialog listing typical limitations so you're not surprised. Acknowledged once per browser via localStorage.

See the [connector catalog](../connectors.md) for the full list.

---

## 7. Credentials at rest

F-Pulse OSS encrypts saved credentials using **Fernet (AES-128-CBC + HMAC-SHA256)**. The master key lives in `<FPULSE_DATA_DIR>/secret.key` (Docker default: `/data/secret.key`; or override the path via `FPULSE_MASTER_KEY_FILE`). POSIX permissions are enforced to `0600` on startup — the server fails closed if the perms are wider.

The plaintext password / API key is **never** returned by any API endpoint after save — neither in `GET /api/connections/{id}` nor in exports. To rotate, edit the connection and provide a new credential.

> F-Pulse+ is a paid extension that adds team-oriented governance on top of these primitives.

---

## 8. API reference (essentials)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/connections/` | List connections (filter by `type`, `project_id`, `scope`) |
| `GET` | `/api/connections/types` | Supported connection types |
| `GET` | `/api/connections/metadata` | Types + categories + storage formats |
| `POST` | `/api/connections/` | Create |
| `GET` | `/api/connections/{id}` | Fetch one (credentials redacted) |
| `PUT` | `/api/connections/{id}` | Update |
| `DELETE` | `/api/connections/{id}` | Delete |
| `POST` | `/api/connections/{id}/test` | Live test |

---

## 9. F-Pulse+: team-oriented governance for connections

F-Pulse+ is a paid extension that adds team-oriented governance on top of OSS connections — see [hybridyn.com/f-pulse](https://hybridyn.com/f-pulse).

---

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "capability mismatch: source uses write-only connection" | Sink connection wired to source node | Pick a read-capable connection, or change capabilities |
| Test fails "password authentication failed" | Credential is wrong | Edit connection, re-enter credential |
| Connection saved but pipeline can't see it | Project-scoped connection in wrong project | Edit connection, change `project_id` or set to global |
| "Connector not found" in picker | Connector is hidden (Coming soon, F-Pulse+, or unsupported) | Use Generic Source/Destination + REST API, or upgrade to F-Pulse+ |
| Master key file refuses startup | `~/.fpulse/secret.key` has wrong permissions (must be `0600` on POSIX) | `chmod 600 ~/.fpulse/secret.key` and restart, or set `FPULSE_ALLOW_INSECURE_KEY_PERMS=1` (dev only) |

---

**Change log**

| Date | Change |
|---|---|
| 2026-05-03 | Rewritten for F-Pulse OSS audience. Vault references + DEV/PROD scoping consolidated into section 9 (F-Pulse+ section). Connector status badges section added. |
| 2026-04-22 | Initial publication. |
