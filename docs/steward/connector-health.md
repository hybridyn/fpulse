# F-Pulse Steward — Connector health

The first **connector-level** Steward detector. Activates four
FindingKinds that had been contract-only in 1.1:

| FindingKind | When it fires |
|---|---|
| `connector_auth_failure` | Credentials rejected (401 / 403 / "permission denied" / etc.) |
| `connector_unreachable` | Network refused / DNS failed / timeout / SSL handshake |
| `connector_rate_limit` | 429 / "throttled" / "too many requests" / "quota exceeded" |
| `credential_near_expiry` | Recorded credential expiry is within 7 days from now |

Goal: when a connection has been failing for a sustained period — not
a momentary flap — Steward turns the eye icon red with the right
diagnosis, not just "something broke."

## How health gets recorded

Three ways:

| Source | When |
|---|---|
| **The built-in Test Connection button** (`POST /api/connections/{id}/test`) | Every time a user clicks Test, the result is automatically piped into Steward's health-state sidecar. Zero user effort — Steward learns from existing workflow. |
| **`POST /api/steward/connector-health`** | External CI runners, monitoring tools, or scheduled health probes push results in directly. Body: `{"connection_id": "...", "ok": true/false, "error_message": "...", "latency_ms": 250, "credential_expires_at": "..."}` |
| **Future**: pipeline runs | The executor records connector outcomes on every read/write. Not shipped in 1.1.x — but the storage path will absorb run outcomes without schema change. |

## The state machine

Per-connection record:

```jsonc
{
  "connection_id": "conn-abc",
  "consecutive_failures": 5,            // streak length
  "first_failure_at": "2026-06-07T10:00:00Z",  // start of CURRENT streak
  "last_check_at":    "2026-06-07T12:00:00Z",
  "last_status":      "failing",        // healthy | failing | unknown
  "last_error_class": "auth_error",     // see classifier below
  "last_error_message": "401 Unauthorized — token rejected",
  "latency_ms": 250,
  "credential_expires_at": "2026-06-14T00:00:00Z"  // optional
}
```

Transitions:

- **First failure** → `consecutive_failures = 1`, `first_failure_at = now`
- **Failure after failure** → streak increments, `first_failure_at` UNCHANGED
- **Success after failure** → resets to `consecutive_failures = 0`, `first_failure_at = null`
- **Failure after recovery** → starts a fresh streak from 1 (NOT continuing the old count)

That last rule is what makes "fixed but broke again" NOT auto-escalate
to the previous severity peak. The operator gets a fresh P3, not an
inherited P1.

## Severity rules

A finding only fires when BOTH:
- `consecutive_failures >= 2` (single-flap suppression)
- `first_failure_at` was at least **5 minutes ago** (time-clamp — Rule 6)

Then severity scales with streak length:

| Streak | Severity |
|---|---|
| 2 – 3 | P3 (digest only) |
| 4 – 9 | P2 (in-app + bell) |
| 10+ | P1 (page) |

`credential_near_expiry` is independent:

| Days to expiry | Severity |
|---|---|
| ≤ 1 day | P1 |
| ≤ 3 days | P2 |
| ≤ 7 days | P3 |
| > 7 days | (no finding) |

## Error classification

Free-text test errors are mapped to one of `{auth_error, rate_limit, timeout, unreachable, unknown}` by ordered substring matching:

| Class | Triggered by (case-insensitive substring) |
|---|---|
| `auth_error` | `auth`, `credential`, `permission`, `password`, `unauthor`, `401`, `403`, `access denied`, `invalid token`, `forbidden` |
| `rate_limit` | `rate limit`, `429`, `throttl`, `too many requests`, `quota exceeded` |
| `timeout` | `timeout`, `time out`, `timed out`, `deadline exceeded` |
| `unreachable` | `connection refused`, `unreachable`, `could not connect`, `name or service not known`, `getaddrinfo`, `dns`, `host not found`, `no route to host`, `network is unreachable`, `ssl` |

Order matters — auth is checked before rate-limit so `401 Unauthorized` classifies as `auth_error` rather than being snared by a 4xx-prefix pattern.

`timeout` collapses to the same FindingKind as `unreachable` — they're operationally similar and users don't need two near-identical alert categories to triage.

## Suppression — per (connection, kind), not per connection

Source signature is `connhealth::<workspace>::<connection_id>::<kind>`. That means:

- Dismissing "this connection is intentionally rate-limited" silences ONLY `connector_rate_limit` for that connection
- Auth failures or unreachable events on the same connection STILL fire

This prevents the failure mode of "dismissed the rate-limit warning yesterday, missed the auth rotation today because I silenced the whole connection."

## API surface

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/steward/connector-health` | List every recorded state in this workspace |
| `POST` | `/api/steward/connector-health` | External record path (body: `connection_id`, `ok`, optional `error_message`, `latency_ms`, `credential_expires_at`) |
| `POST` | `/api/connections/{id}/test` | The existing test endpoint — now also stamps Steward's health-state sidecar |

The findings themselves appear at `GET /api/steward/findings` alongside Archeologist + user-rule findings, with `level: "connector"` so the UI groups them.

## What does NOT happen

- **Steward never tests a connection on its own.** It reads the results of tests that already happened. No outbound probes from the Steward scan loop — that would risk noisy false positives on healthy connections during temporary network blips, plus introduce timing+rate-limit concerns.
- **Steward never mutates the connection.** Read-only Rule 1 still holds. Dismiss / resolve / approve a fix-note all work; the connection record itself is untouched.

## What needs Plus

OSS gets the full detector + recording + API. Plus will add:

- A **connector-health dashboard** in the UI rendering per-connection latency / streak / last-error trends
- **Scheduled probes** the platform runs against connections on a cadence (not relying on user clicks of Test)
- **External alerting integrations** (PagerDuty / Opsgenie routing)

The detector and the storage format stay identical between OSS and Plus.

## See also

- [`overview.md`](overview.md) — the 7-level Steward contract this fits into
- [`custom-rules.md`](custom-rules.md) — admins can layer additional connector checks on top of these built-ins
- [`positioning.md`](positioning.md) — the 4-pillar product framing
