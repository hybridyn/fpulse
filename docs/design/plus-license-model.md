# F-Pulse+ license + seat model (design spec)

Status: **spec only — not implemented**. Design captured here for the
seat-based license enforcement F-Pulse+ needs before commercial
launch. Implementation is a separate work package, expected to land
incrementally — start small, harden as customer count grows.

## Reviewer-driven design corrections (2026-06-02)

An earlier draft proposed `session = (user_id, browser_fingerprint, source_ip)`
as the seat identity. Three independent reviewers flagged this as the
wrong primitive:

| Problem | Why it bites |
|---|---|
| Privacy concerns | Browser fingerprinting fails enterprise security reviews and conflicts with the spirit of GDPR/CCPA. |
| VDI / shared jump-server breakage | Citrix, terminal services, Workspaces — same IP across many users by design. |
| Browser updates change fingerprints | Chrome auto-update on Tuesday → every user locked out on Wednesday. |
| Reliability | Fingerprint mismatch from a corporate proxy or upgraded browser = false-positive seat consumption. |
| Support cost | Every "I changed laptops and now I can't log in" ticket is a non-bug. |

**The corrected design uses named-user seats as the contractual
identity.** Fingerprint and IP are kept as inputs to abuse-detection
(anomaly logging only), not as the licensing primitive.

## Scope of the first paid SKU

F-Pulse+ 1.0 ships as a server install with a fixed seat count per
license. Reference SKU:

| | |
|---|---|
| Seats | **5** (1 Admin + 4 Developers) |
| Environments per workspace | **1 Dev + 1 Prod** (Prod created by Admin only) |
| Deployment | On-prem or customer cloud; Hybridyn-hosted SaaS is post-1.0 |
| Plus features (RBAC enforcement, audit log, prod approvals, CDC source, etc.) | Gated by the same license check |

Higher SKUs (10 / 25 / unlimited seats) reuse the same enforcement
mechanism with different seat-count values.

## Seat = named user (the primitive)

A **seat** is one named-user account. The Admin assigns each license
seat to a specific user by email at user-creation time. That's the
entire seat identity:

```
license.seats = 5
licensed_users = [
  "alice@acme.com",   # Admin
  "bob@acme.com",     # Developer
  "carol@acme.com",   # Developer
  "dave@acme.com",    # Developer
  "eve@acme.com",     # Developer
]
```

Creating a 6th user when the license is at 5 seats returns 402
Payment Required. The Admin must either remove an existing seat (the
user account goes inactive but its history is preserved) or upgrade
the license.

That's the whole contract. Simple to explain, simple to enforce,
simple to support.

### What we don't do (and why)

| Anti-pattern | Why we skip it |
|---|---|
| Browser fingerprint binding | Privacy / reliability / VDI breakage |
| Source-IP binding | NAT, VPN, mobile networks — false positives constant |
| Hardware MAC binding | Enterprise IT teams reject on principle |
| 24-hour rolling distinct-IDs | Defeated by credential sharing — Reviewer caught this |
| Phone-home activation | Air-gapped customers can't reach external endpoints |
| Floating / borrowed seats | Adds complexity for marginal value at this stage |

### Anti-sharing controls (soft, not contractual)

Credential sharing across many devs to bypass seat counting is what
the rejected fingerprint+IP model was trying to prevent. The named-
user model handles this through **observability**, not **enforcement**:

- **Concurrent-session cap per user** — default 3 active sessions per
  named user. The 4th login prompts "You have 3 active sessions on
  other devices. Terminate one to continue?" The user picks which
  to kill. Multi-tab on the same browser counts as one session.
- **Anomaly log** — every new session records IP + user-agent +
  rough fingerprint hash. Admin sees a "session activity" panel
  with unusual patterns flagged (10 distinct IPs in 24h for one
  user = obvious sharing).
- **No hard auto-revoke.** The Admin acts on the signal, the system
  doesn't lock users out unilaterally.

This trades some theoretical license-revenue protection for a
massively better user experience + zero VDI/proxy false-positives.
For mid-market customers (the F-Pulse+ target), Admin-driven
enforcement is the right level.

## Role mapping onto existing RBAC

F-Pulse already has the role taxonomy needed. Nothing new gets invented.

| License seat type | Existing RBAC role | Dev env access | Prod env access | Manage users | Approve Prod deploys |
|---|---|---|---|---|---|
| 1 × Admin | `workspace_admin` (rank 90) | R / W | R / W / Schedule / Promote | Yes | Self-approves |
| 4 × Developer | `data_engineer` (rank 70) | R / W | Read-only + Request promotion | No | No |

Single-Prod-environment constraint is enforced at the `/environments`
API layer: `POST /environments` with `kind="prod"` returns 402 Payment
Required when a Prod environment already exists for the workspace.

## License file format (v1 — minimal)

Per Reviewer 1's "don't over-engineer before first customer" point,
the v1 license format is **deliberately minimal**:

```json
{
  "license_id": "FP-PLUS-2026-000123",
  "customer": "Acme Corp",
  "edition": "plus",
  "seats": 5,
  "expires_at": "2027-06-01T00:00:00Z",
  "issued_at": "2026-06-01T00:00:00Z",
  "signature": "<base64(Ed25519 signature of the above)>"
}
```

That's it for v1. No nested feature flags, no per-feature pricing
tiers, no max-workspaces cap. Plus features are gated by the boolean
"is the license valid" — features-as-a-list comes in v2 if/when a
paying customer asks for a non-standard bundle.

**Why Ed25519 specifically:**
- 64-byte signatures (vs 512 bytes for RSA-4096)
- No PKI infrastructure required — single public key embedded in the
  Plus binary at build time
- No signature-malleability footguns (unlike ECDSA)
- Standard-library support in Python's `cryptography` package

**License file location:** `$FPULSE_DATA_DIR/license/fpulse.lic`,
mode 0600. Verified at server startup; rejected if signature fails,
expiry past, or `seats < 1`. Admin uploads via `POST /api/plus/license`
(multipart, admin-only).

## Implementation work breakdown — staged by customer count

Per reviewer feedback, **don't build everything before the first
sale**. Stage the implementation:

### Stage 1 — Pre-first-customer (~3 sprints)

The minimum to credibly sell Plus to the first paying customer.

| PR | Effort | What it does |
|---|---|---|
| `plus/license.py` — file loader + Ed25519 verify | ~1 sprint | Loads `fpulse.lic`, verifies signature, parses fields, in-memory cache with file-stat invalidation. `POST /api/plus/license` admin upload. `GET /api/plus/license` admin status. |
| Plus-route gating middleware | ~half sprint | Wraps `/api/plus/**` routes. Returns 402 Payment Required with structured body if license missing/expired/invalid. |
| Seat-cap on user creation | ~half sprint | `POST /api/users` checks `count(active users) < license.seats` before creating. Returns 402 with a clear "seat limit reached, deactivate a user first" message. |
| Single-Prod-env enforcement | ~half sprint | `POST /api/environments` blocks a second `kind="prod"` row. UI surfaces the limit. |
| License-gen tooling for ops | ~1 day | `scripts/issue-license.py` — takes customer name, seats, expiry, signs with private key, emits `fpulse.lic`. Private key never ships in the product. |

**Total: ~3 sprints (~6 weeks) for one engineer to first-customer-ready.**

### Stage 2 — Post-first-customer (build when you actually need it)

Add only when a real customer asks or you see real abuse:

- Concurrent-session cap per user (default 3 sessions)
- Session anomaly logging + Admin "session activity" panel
- License grace-period handling on expiry (14 days soft, 14 days hard)
- License renewal email automation
- Per-feature flags in the license format (`features: [...]`)
- Admin License page polish (drag-drop upload, expiry countdown)

### What gets built later, never preemptively

- Floating/borrowed seats (only if asked)
- Multi-workspace per license (only if asked)
- Hosted SaaS billing integration (only when SaaS launches)

This staging is the explicit answer to Reviewer 1's "License
Enforcement Too Early" concern: ship Stage 1, sell to a customer,
let real usage tell us what Stage 2 actually needs.

## What happens when the license expires

(Stage 2 detail — not blocking Stage 1 ship)

- 30 days before expiry: dashboard banner + email to Admin
- 7 days before: stronger banner
- On expiry: Plus features (Prod env, RBAC enforcement, audit log,
  CDC source) become read-only with a renewal banner. Existing
  pipelines continue to run for **14 days** soft grace; manual runs
  work for another 14 days; after 28 days total, Plus features lock
  entirely. Re-uploading a valid license resumes operation instantly.

Fail-soft, never destructive. Matches mature vendor practice (GitLab
Ultimate, Confluent Platform, Tableau Server).

## What this spec deliberately does NOT include

- **Hardware-locked licenses** — user-hostile, enterprise IT rejection guaranteed
- **Phone-home telemetry / activation servers** — fails in air-gapped enterprise networks
- **Per-feature pricing tiers** — single SKU at launch; multi-SKU when warranted
- **Floating / borrowed seats** — named-user model is simpler and covers the same use case
- **Browser fingerprint or IP-based seat enforcement** — privacy / reliability / VDI breakage; only used as anomaly signals if at all

## What needs sign-off before Stage 1 starts

- [ ] Public-private Ed25519 keypair generation + secure storage of the private key (Hybridyn ops responsibility)
- [ ] Confirmed SKU price point + 5-seat bundle definition (sales/marketing)
- [ ] Legal review of the license JSON wording (terms of use, warranty disclaimers)

Stage 2 sign-off can wait until Stage 1 is in customer hands.
