# F-Pulse compliance one-pager

**Version:** 1.0
**Last reviewed:** 2026-05-04
**Audience:** compliance, procurement, security review teams

This one-pager answers the questions that come up in every regulated
buyer's review. It is intentionally short. Each row links to the live
posture endpoint or source artifact so reviewers can verify rather than
trust.

## Architecture in one sentence

> Self-hosted single-tenant data workflow platform with deterministic
> execution, AI-assisted logic, and full audit lineage — designed for
> teams whose data cannot leave their infrastructure.

## Data residency

| Question | Answer | Verify |
| --- | --- | --- |
| Where is operator data stored? | On the host where F-Pulse is installed. | `data/` directory on disk. |
| Does data ever leave the host? | Not by default. | `GET /api/trust/posture` → `sovereignty.data_stays_local_by_default = true`. |
| What about the AI provider? | Default is Ollama (local). Cloud providers are off-by-default opt-in. | `GET /api/trust/posture` → `sovereignty.active_provider_is_local`. |
| Telemetry? | Off by default; opt-in only. | `GET /api/trust/posture` → `sovereignty.telemetry_currently_enabled`. |

## Authentication & authorisation

| Layer | OSS Free | F-Pulse+ |
| --- | --- | --- |
| User accounts | Yes | Yes |
| 2FA | — | Yes (TOTP + recovery codes) |
| SSO (SAML/OIDC) | — | Yes |
| RBAC (4-role agent + 5-tier workspace) | — | Yes |
| Audit log | Best-effort, in-process | Persistent with retention policy |
| Approval gates (DEV → PROD) | — | Two-gate with optional two-person rule |

OSS is intended for the solo developer; multi-user controls are F-Pulse+.

## Cryptography

| Use | Algorithm | Where |
| --- | --- | --- |
| Stored credentials + AI provider API keys (Free + Plus) | Fernet (AES-128-CBC + HMAC-SHA256) | `backend/fpulse/security/encryptor.py` |
| Master key file | 32-byte symmetric, chmod 600, fail-closed on POSIX | `~/.fpulse/secret.key` (override via `FPULSE_MASTER_KEY_FILE`) |
| TLS in transit | Operator's reverse proxy (nginx / Caddy) | Outside F-Pulse — F-Pulse listens on plain HTTP and expects to sit behind a TLS terminator. |
| External vault (Plus only) | HashiCorp Vault / AWS Secrets Manager / Azure Key Vault / GCP Secret Manager | F-Pulse+ feature — see hybridyn.com/f-pulse |
| Workflow content hash | SHA-256 over canonical JSON | `workflow_versions.content_hash` |
| Tamper-detection | Re-hash on rollback; mismatch = HTTP 409 Conflict | Schema v15. |

## Network posture

| Item | Default | Operator override |
| --- | --- | --- |
| CORS allowlist | Empty (same-origin only) | `FPULSE_CORS_ORIGINS` env var |
| IP allowlist | Off | `FPULSE_PLUS_IP_ALLOWLIST` (Plus only) |
| Trusted proxies | Off — `X-Forwarded-For` ignored | `FPULSE_TRUSTED_PROXIES` env var |
| Rate limiter | On — per-IP sliding window | Tunable in code; not exposed to admins. |

These are surfaced read-only in **Settings → Security → Operator Config**
because they are env-var driven at startup. See
[`docs/deployment.md`](deployment.md) for the operator runbook.

## Reviewable surfaces

| Surface | Endpoint or path |
| --- | --- |
| Live trust posture | `GET /api/trust/posture` |
| Supported-models policy | `GET /api/trust/supported-models` |
| Eval harness pass rate | `GET /api/trust/eval-summary` |
| Connector certification matrix | `GET /api/connectors/cert-matrix` |
| Schema migrations changelog | `backend/fpulse/storage/database.py` (in-code comments above `SCHEMA_VERSION`) |
| Tested-with version matrix | [Changelog](../CHANGELOG.md) |
| Operator runbook | [Deployment guide](deployment.md) |
| AI boundary contract | [AI boundary contract](ai-boundary-contract.md) |
| Source code | Apache-2.0 — full repository |

## What F-Pulse does NOT claim

We are deliberate about scope. F-Pulse does not currently provide:

- SOC 2 / ISO 27001 attestation (the operator's deployment may be in
  scope of their own attestation; F-Pulse the software is not
  separately certified).
- HIPAA / PCI scope reduction guarantees — those are deployment
  decisions made by the operator.
- Distributed crash-resilient queue coordination — single-node only in
  OSS. F-Pulse+ is a paid extension that adds queue-side coordination
  for team deployments (see hybridyn.com/f-pulse).
- Any guarantee about cloud LLM providers' privacy practices when an
  operator opts in to one. That is the vendor's contract with the
  operator, not F-Pulse's.

## How to validate this document

Every "verify" link in this one-pager points at an artifact that can be
checked independently. A reviewer who runs the live install and hits
the endpoints listed above can confirm every claim without trusting the
F-Pulse team.

For questions on specific controls, see the linked source files or open
an issue on the project repository.
