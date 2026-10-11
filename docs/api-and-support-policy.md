# F-Pulse OSS — API, versioning & support policy

The single reference for how F-Pulse versions itself, what stability the HTTP
API promises, and which releases receive fixes.

## Versioning

F-Pulse follows [Semantic Versioning](https://semver.org): `MAJOR.MINOR.PATCH`.

- **PATCH** (`1.0.0 → 1.0.1`) — backward-compatible fixes.
- **MINOR** (`1.0 → 1.1`) — backward-compatible additions.
- **MAJOR** (`1.x → 2.0`) — breaking changes (see the API policy below).

There is **one source of truth** for the version — `fpulse.app_meta.VERSION`,
which equals the published `pyproject.toml` / PyPI version. It is surfaced
consistently in:

| Surface | How to read it |
|---|---|
| CLI | `fpulse --version` |
| HTTP | `GET /api/health` → `version`, and `GET /api/app/info` → `version` |
| In-app | Help & Feedback shows the running version; the update banner compares it to the latest release |
| Package | `pip show fpulse` |

A client learns the server version from `GET /api/health` before relying on any
version-specific behavior.

## HTTP API stability

The HTTP surface lives under `/api/*`. Within a **major** version it evolves
**additively only**:

- New endpoints, new optional request fields, and new response fields may be
  added at any time. Clients must ignore unknown response fields.
- Existing endpoints, required parameters, and response field meanings do **not**
  change or disappear within a major version.
- A **breaking** change requires a major-version bump **and** a deprecation
  window of **at least one minor release**, during which the old behavior keeps
  working and is documented as deprecated.

(This formalizes the policy stated in [`../CHANGELOG.md`](../CHANGELOG.md).)

The OpenAPI schema is served at `/openapi.json` (and the vendored Swagger UI at
`/docs` when `FPULSE_MODE=dev`), so clients can generate typed bindings against
the exact running version.

## Database schema & migrations

Forward migrations run automatically at startup. The schema version is tracked
separately from the app version. **Upgrades are one-way** unless a release notes
otherwise — always take a verified backup before upgrading (see
[`product_facts/13_backup_recovery.md`](product_facts/13_backup_recovery.md)),
and do not run migrations concurrently from multiple app instances (F-Pulse OSS
is single-node by design).

## Support lifecycle

F-Pulse OSS is at **1.0.x**. Honest current policy:

- **Supported release:** the latest published version. Fixes land as a new
  PATCH on the latest MINOR.
- **Security fixes:** go into a new patch on the latest release; report privately
  via [`../SECURITY.md`](../SECURITY.md) (do not open a public issue).
- **No separate LTS branch yet.** A longer support window with pinned EOL dates
  will be published if/when the release cadence and adoption warrant it; until
  then, run a currently-supported version.

When a version reaches end-of-life this document and the CHANGELOG will say so
explicitly — absence of an EOL note does not imply indefinite support.

## See also

- [`CHANGELOG.md`](../CHANGELOG.md) — per-release changes and the tested-with matrix
- [`SECURITY.md`](../SECURITY.md) — vulnerability reporting and response targets
- [`security-deployment.md`](security-deployment.md) — hardening for network use
