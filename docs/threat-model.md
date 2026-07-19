# F-Pulse Threat Model

> **Status:** Draft 1 — 2026-06-03. Authored as part of pre-v1.0 launch
> security self-audit. Pairs with [security.md](../SECURITY.md) (disclosure
> policy) and [security/audit-2026-06-03.md](security/audit-2026-06-03.md)
> (the audit findings this model is built on).
>
> **Scope:** F-Pulse OSS v1.0. F-Pulse+ delta is called out where it
> differs. Updated each release; significant changes recorded in
> [changelog.md](../CHANGELOG.md).

---

## 1. What we're protecting

F-Pulse OSS is a **single-host, single-tenant** data pipeline engine that
holds three categories of asset:

| Asset | Examples | Sensitivity | Where it lives |
|---|---|---|---|
| **Credentials** | DB passwords, API tokens, OAuth refresh tokens, SMTP passwords | High | `data/credentials.db` — Fernet-encrypted (AES-128-CBC + HMAC-SHA256) at rest, key at `~/.fpulse/secret.key` (mode 600) |
| **Pipeline definitions** | Workflow JSON, SQL transforms, connector manifests | Medium | `data/workflows/` — plaintext; the SQL these contain may reference sensitive table names but the definitions themselves aren't secrets |
| **Sample data + run artifacts** | DuckDB tables, pipeline outputs, parquet spill files | Variable — whatever the user feeds in | `data/samples/`, `data/duckdb_spill/` |
| **Master encryption key** | The Fernet symmetric key that unlocks every credential blob | **Critical** | `~/.fpulse/secret.key` (mode 600, refuses to start if group-readable on POSIX) |
| **User accounts** | Email + password hash for the local auth store | High | `data/auth.db` — password hashes only; see audit finding H1 for current hashing weakness |

---

## 2. Trust boundaries

```
┌───────────────────────── Untrusted ──────────────────────────┐
│  Public internet           Direct DB clients (psql, mongosh) │
│  Webhook callers           Files on disk (any FS user)       │
└──────────────────────────────────────────────────────────────┘
                              │
                  ┌───────────▼───────────┐
                  │  Loopback (127.0.0.1) │  ← default bind
                  │  FastAPI HTTP edge    │
                  └───────────┬───────────┘
                              │   X-FPulse-AI-Safety header
                              │   bearer token (session)
                  ┌───────────▼───────────┐
                  │  FastAPI router layer │
                  │  - auth required      │  ← anonymous /api/health, /docs
                  │  - rate-limited       │
                  │  - security headers   │
                  └───────────┬───────────┘
                              │   sanitized request models
                  ┌───────────▼───────────┐
                  │  Backend services     │
                  │  - workflow store     │
                  │  - executor           │
                  │  - encryptor          │
                  └───────────┬───────────┘
                              │   parameterised SQL only
                  ┌───────────▼───────────┐
                  │  DuckDB engine        │
                  └───────────────────────┘
```

**Boundaries enforced:**

1. **Loopback by default.** The bundled launcher binds `127.0.0.1`.
   Operators who bind `0.0.0.0` get a startup warning + the docs make
   the network-exposure escalation explicit.
2. **Auth required on every write route.** The "anonymous-access
   regression" CI job (`security-scan.yml`) re-runs the route-coverage
   check on every PR — fail-loud if a route is added that doesn't require
   auth.
3. **Rate limiting.** Per-IP sliding window on auth + execute endpoints.
   Tunable via `FPULSE_RATE_LIMIT_*` env vars.
4. **Security headers.** `SecurityHeadersMiddleware` sets
   X-Frame-Options, CSP, Referrer-Policy, HSTS. Pinned in
   `tests/test_security_headers.py` so a future middleware change can't
   silently drop them.

**Boundaries NOT enforced** (deliberately, with reasoning):

- **OS-level isolation between users on the same host.** F-Pulse runs
  as one POSIX user. Anyone with that UID — or root — sees everything.
  This is by design for OSS: it's the same trust model as `git`,
  `postgres`, or any user-installed daemon. Multi-user separation is an
  F-Pulse+ feature (workspace RBAC + per-user data dirs).
- **In-process sandboxing of Python/SQL transforms.** The Python and
  SQL transform nodes execute with the same privileges as the F-Pulse
  process itself. A malicious pipeline author can read/write everything
  F-Pulse can. We rely on the operator to gate authoring rights via the
  built-in RBAC; we do NOT sandbox the executor.

---

## 3. Threats by asset

For each asset we walk **STRIDE** (Spoofing / Tampering / Repudiation /
Info-disclosure / DoS / Elevation) and record the mitigation.

### 3.1 Credentials store

| Threat | Mitigation today | Residual risk |
|---|---|---|
| Disk-image theft → decrypt credentials | Fernet AES-128-CBC + HMAC-SHA256, key in separate file with mode 600 | Master key must travel with backups — if both are stolen, credentials are recoverable. Documented in `docs/deployment.md` backup section. |
| Compromised process → read decrypted credentials in RAM | None (in-process secrets) | Out of scope for single-host OSS. F-Pulse+ on-prem operators wanting HSM/vault integration are routed to the commercial track. |
| Credential injection via API | Pydantic request validation + encrypt-on-write via `Encryptor.encrypt_config()` (sensitive-field allowlist) | A new sensitive field name not in `_SENSITIVE_FIELD_NAMES` stays plaintext. Mitigation: PR template asks "did you add a new credential field?" |
| Side-channel via logs | Centralised log sanitisation in `ai/sanitize.py`; credential fields scrubbed before any log line written | Spot-check that every new endpoint uses the sanitised logger. |

### 3.2 Pipeline definitions

| Threat | Mitigation today | Residual risk |
|---|---|---|
| Unauthorised modification of a published pipeline | Edit access gated on RBAC `developer` role + DEV/PROD environment split + (Plus) approval gate on PROD deploys | Single-user OSS install: any logged-in admin can modify any pipeline. By design. |
| Malicious SQL in a transform → arbitrary DuckDB call | DuckDB itself is the trust boundary; we run it in the F-Pulse process | A malicious pipeline author can read/write any file the F-Pulse process can. RBAC the authoring role. |
| Arbitrary code via the legacy Code Script node | **Disabled by default** and not in the node palette. It runs only when an operator sets `FPULSE_ENABLE_CODE_SCRIPT=1` on a trusted single-tenant install; even then it is in-process with restricted builtins + import allowlist, but NOT a sandbox. `FPULSE_DISABLE_CODE_SCRIPT=1` force-disables it. | With the default (opt-out) posture there is no user-code execution path. There is no run-your-own-Python node offered in either edition. |

### 3.3 Run artifacts + sample data

| Threat | Mitigation today | Residual risk |
|---|---|---|
| Cross-pipeline data leak via shared DuckDB | Each run creates an isolated DuckDB connection; relations are not persisted across runs unless explicitly written to a Storage table | Spill files in `duckdb_spill/` are co-located. Different pipelines spilling at the same time could see each other's intermediate files until purge. Acceptable for single-tenant; on a busy multi-tenant box, set per-pipeline spill dirs. |
| PII in run history | Run-history rows store row counts + status, NOT data. Failed-step error messages may include sample row values for debugging | Operators handling regulated data should disable error-row capture (`FPULSE_CAPTURE_ERROR_ROWS=0`). Documented in `docs/install/security-hardening.md`. |

### 3.4 Master encryption key

This is the highest-stakes asset. Loss = unrecoverable credential store.
Theft = full credential compromise.

| Threat | Mitigation today | Residual risk |
|---|---|---|
| Accidental commit to source control | `.gitignore` excludes `data/`, `~/.fpulse/`; gitleaks CI job scans every commit | If the operator overrides `FPULSE_MASTER_KEY_FILE` to a path inside the repo, gitleaks won't catch it on the first commit. Documented as anti-pattern in `docs/anti-patterns.md`. |
| Backup includes the key file | Documented — backup intentionally INCLUDES the key (otherwise the credential store is useless after restore). Operators must protect backups accordingly. | Operator must encrypt backups. We don't enforce. |
| Insufficient file permissions | `_verify_perms()` refuses to start if mode is group/world readable | Windows / NTFS not checked — POSIX-only. Acceptable since enterprise Windows deployments typically use NTFS ACLs configured by the operator. |

### 3.5 User accounts (auth store)

| Threat | Mitigation today | Residual risk |
|---|---|---|
| Brute-force login | Per-IP rate limit + per-account exponential backoff (`tests/test_security_hardening.py`); **bcrypt cost 12** for password hashing (`auth/models.py`, since 2026-06-03 — H1 closed). | Legacy `salt:sha256` hashes from installs created before 2026-06-03 are transparently re-hashed to bcrypt on the next successful login. Until every legacy user has logged in once, a leaked auth-store file could contain a mix of bcrypt + legacy hashes; the legacy ones would still be GPU-crackable. Operator action: force a one-time password rotation if the database file ever left the host. |
| Session theft | Sessions are 32-byte URL-safe tokens, server-side state (revocable), 8-hour timeout, machine-fingerprint bound | Token in transit relies on operator's TLS choice (reverse-proxy guidance in `docs/deployment.md`). |
| Privilege escalation via API | Every write route checks `current_user.role` via the RBAC dependency. Anonymous-access regression test enforces no bypass | A new route forgetting the dependency would be caught by the CI regression test (`run_all_tests.py --security`). |

---

## 4. Top external-facing risks

Beyond the per-asset table, three external surfaces deserve named
attention because they touch user-supplied URLs / file paths / SQL:

### 4.1 Server-Side Request Forgery (SSRF)

| Surface | Status |
|---|---|
| **OpenAPI Author Connector fetch** (`ai_authoring.py:_ssrf_check_url`) | **Strong** — scheme allowlist, DNS resolution check, blocks 169.254/16 + RFC1918 + loopback, resolved-IP fetch to defeat DNS rebinding, redirect re-validation, 2MB response cap. Opt-in escape via `FPULSE_OPENAPI_FETCH_ALLOW_PRIVATE=1` for trusted internal networks. |
| **`api_source` node HTTP fetch** (`nodes/activities.py`) | **Strong** (since 2026-06-03 — H2 closed) — every `urllib.request.urlopen()` site (initial + pagination) wraps `fpulse.security.ssrf.check_url()` first. Same defence as OpenAPI fetch. Operators with internal API catalogs can opt in via `FPULSE_API_SOURCE_ALLOW_PRIVATE=1`. |
| **`http_request` action node** | Strong — uses the same `_do_request` path as `api_source`, inherits the SSRF guard. |
| **Webhook trigger** | Inbound, not outbound — receives signed POSTs. HMAC verification per-trigger using `signing_secret`. |

### 4.2 Local file-system access

| Surface | Status |
|---|---|
| **File source READ paths** (`_path_utils.resolve_input_path`) | Resolves user paths against `data_dir` first, then CWD. **Accepts absolute paths as-is.** A pipeline-author role can therefore reference any path the F-Pulse process can read. Acceptable for single-tenant local install; **becomes a real issue when the authoring role is RBAC-restricted but pipelines run as a privileged operator.** Mitigation today: gate authoring. |
| **File sink WRITE paths** | Same `_path_utils` but writes are always rooted at `data_dir` (read fallback to CWD does NOT apply). |
| **Spill directory** | Operator-configured via `FPULSE_DUCKDB_SPILL_DIR`. Defaults to a subdir of `data_dir`. |

### 4.3 SQL injection in transform nodes

| Surface | Status |
|---|---|
| `transform` node | User-provided SQL is run verbatim through DuckDB. **This is by design** — the node IS a SQL editor. The threat is not "injection" but "a user with authoring rights can run arbitrary SQL against any registered table." Same trust model as a DBA console. |
| `execute_sql_task` action node | Same — runs user SQL against a configured connection. Gate the role. |
| `db_source` parameter expansion | All user-supplied filter values are parameterised via `?` placeholders (not string-formatted). |

---

## 5. Out of scope (and why)

These are real concerns we deliberately do NOT defend against in v1.0
OSS. Each is either out-of-product-shape, or routed to the operator's
deployment posture, or only meaningful in F-Pulse+ enterprise.

| Out-of-scope item | Why |
|---|---|
| Malicious admin on the same host | F-Pulse runs as one OS user. We can't protect against root or the user we run as. Same trust model as every other user-installed daemon. |
| Side-channel timing attacks on the auth path | Out of scope at OSS scale. Bcrypt fix from H1 will incidentally make this less exploitable. |
| HSM / vault-backed master key | F-Pulse+ feature. OSS uses local file with mode 600. |
| Network-layer isolation (firewalls, mTLS between services) | Operator's responsibility. We document the reverse-proxy pattern in `docs/deployment.md`. |
| Code-signing of binary releases | Planned for v1.1; today's releases are unsigned. Verify by SHA-256 from changelog. |
| Anti-tampering for compiled wheels | Not feasible for pure-Python install; trust comes from `pip install` over TLS + pypi-side typo-squat scanning. |
| Sandboxed user-code execution | There is no run-your-own-Python node in either edition, and the legacy Code Script executor is **off by default** (opt in with `FPULSE_ENABLE_CODE_SCRIPT=1`). When enabled it is in-process only, not sandboxed — a sandboxed code node is not offered. |
| Multi-tenant data isolation | F-Pulse+ feature. OSS is single-tenant by design. |

---

## 6. How to verify these claims yourself

Every claim above can be checked against running code:

| Claim | Verification command |
|---|---|
| "Fernet encryption at rest" | `python -c "from fpulse.security.encryptor import Encryptor; e = Encryptor.from_master_key(); print(e.encrypt_value('hello'))"` — starts with `ENC:v1:` |
| "Master key file is mode 600" | `stat -c '%a' ~/.fpulse/secret.key` → `600` |
| "Anonymous access denied on writes" | `curl -X POST http://localhost:8001/api/workflows -d '{}'` → 401 |
| "Security headers present" | `curl -I http://localhost:8001/` shows `X-Frame-Options`, `Content-Security-Policy`, `Referrer-Policy`, `Strict-Transport-Security` |
| "Rate limit active" | Hit `/api/auth/login` 30× in 10 seconds → 429 |
| "SSRF block on OpenAPI fetch" | POST `/api/connectors/author/from-openapi` with `url=http://169.254.169.254/` → 400 SsrfBlockedError |
| "Connector tier matrix" | `curl /api/connectors/cert-matrix` → JSON with depth scores |
| "No telemetry by default" | `curl /api/trust/posture` → `telemetry_currently_enabled: false` |

The Trust page in the running app surfaces these claims as live signals
with the same endpoint links — see `frontend/src/components/pages/TrustPage.tsx`.

---

## 7. Disclosure + audit history

- **2026-06-03** — Initial threat model published. Paired self-audit at
  `docs/security/audit-2026-06-03.md` recorded findings H1 (password
  hashing weakness), H2 (api_source SSRF gap), L1 (UI text inaccuracy
  about crypto algorithms). All three closed in the same cycle:
  H1 migrated to bcrypt cost 12 with transparent legacy rehash, H2 wired
  the shared `security.ssrf.check_url` into both `_do_request` and the
  pagination loop, L1 corrected the UI label to name Fernet honestly.
  M1 (file-source absolute paths) documented as a known boundary; will
  be addressed when F-Pulse+ multi-tenant ships.
- Disclosure policy: see [security.md](../SECURITY.md). Reports to
  `security@hybridyn.com`.
