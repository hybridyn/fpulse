# Security Policy

## Scope

This policy covers **F-Pulse** (the open-source pipeline orchestrator
distributed under the Apache 2.0 license from this repository).

For vulnerabilities in **F-Pulse+** (the commercial edition), please
email `info@hybridyn.com` with the subject prefix `[F-Pulse+ security]`
and **do not** file a public report. F-Pulse+ issues are handled under
a separate private disclosure process and are not covered by this
document.

## Supported versions

Security fixes are published for the versions below. Older lines may
still build but will not receive security backports.

| Version       | Status                |
| ------------- | --------------------- |
| 1.x (latest)  | Supported             |
| 1.x (n-1)     | Security fixes only, for 6 months after the next minor release |
| < 1.0         | Not supported         |

## Reporting a vulnerability

**Please do not open a public GitHub issue, discussion, or pull
request for security reports.** Public disclosure before a fix is
available puts existing users at risk.

Use one of the channels below, in order of preference:

1. **GitHub Private Vulnerability Reporting** (preferred) — open the
   repository's **Security** tab and choose *Report a vulnerability*.
   This creates a private advisory visible only to the maintainers and
   the reporter.
2. **Email** — `info@hybridyn.com` with the subject prefix
   `[F-Pulse security]`.

Please include:

- A clear description of the issue and the affected component
- Steps to reproduce, ideally with a minimal test case
- Affected version(s) and configuration
- Your assessment of impact and severity
- Any suggested mitigation, if you have one

## Response SLA

We aim to meet the following timelines, measured in business days from
the first report:

| Stage                        | Target  |
| ---------------------------- | ------- |
| Acknowledgement              | 3 days  |
| Initial assessment           | 7 days  |
| Fix or mitigation timeline   | 14 days |

If a report is incomplete or out of scope, we will say so during the
initial assessment rather than going silent.

## Severity classification

We classify reports along the lines below. Final severity is set by
the maintainers after assessment and may differ from the reporter's
estimate.

| Severity | Examples                                                                  |
| -------- | ------------------------------------------------------------------------- |
| Critical | Remote code execution, authentication bypass, unauthenticated data loss   |
| High     | Privilege escalation, sensitive-data exposure, persistent stored XSS      |
| Medium   | Limited-impact information disclosure, vulnerabilities requiring local access or unusual configuration |
| Low      | Minor issues, defense-in-depth gaps, hardening recommendations            |

## Coordinated disclosure

We follow coordinated disclosure. We ask that you give us a reasonable
window to ship a fix before publishing details, and we commit to
agreeing a public disclosure date with you. Once a fix is released,
we will credit reporters in the release notes and security advisory
unless you ask to remain anonymous.

## Out of scope

The following are generally not treated as security vulnerabilities
under this policy:

- Issues in third-party dependencies — please report those upstream;
  we will bump the dependency once a fix lands and issue an advisory
  if F-Pulse users are materially affected.
- Vulnerabilities that require physical access to a host running
  F-Pulse, or that require an attacker to already hold administrator
  credentials.
- Self-XSS, clickjacking on pages without sensitive actions, and
  social-engineering scenarios.
- Reports generated solely by automated scanners with no demonstrated
  exploit path.

## Security advisories

Once a fix is released, we publish a GitHub Security Advisory on this
repository describing the issue, affected versions, and the upgrade
path. Subscribe to the repository's *Security advisories* feed to be
notified.

## AI data handling

F-Pulse AI features operate under a strict data-handling contract
documented in [`docs/ai-boundary-contract.md`](docs/ai-boundary-contract.md).
Summary:

### What is sent to the LLM
- Only fields declared in each tool's input schema (per-tool table in
  the boundary contract).
- After PII redaction (email / phone / credit card / Aadhaar / SSN /
  IP / API-key patterns).
- After workspace-configured PII regex redaction.
- Within token budget caps (8K per request default, 16K configurable
  on F-Pulse+).

### What is **never** sent
- Plaintext credentials, Vault values, API keys, signing secrets.
- Sample row data — only schema (column names + types).
- Full execution logs — only first 500 chars per error,
  PII-redacted.
- Any field name matching
  `(?i)(password|secret|token|api_key|private_key|signing_secret)`.
- Customer data classified L3 or L4.

### No-train guarantee
- Anthropic: zero-retention API tier; `disable_training` metadata
  where supported.
- OpenAI: account-level data-sharing opt-out + per-request
  `store: false`.
- Ollama: local — never leaves the host.
- Azure OpenAI: tenant-bound; default no-training.

### Architecture invariants
Ten non-negotiable rules in
[`backend/tests/architecture/test_invariants.py`](backend/tests/architecture/test_invariants.py),
enforced in CI. Of particular relevance to data handling:
- Rule 4 — no unbounded prompt assembly (size caps enforced)
- Rule 8 — no shared mutable execution state across users
  (cache key tenant isolation)
- Rule 9 — no tool call without typed schema validation +
  audit event
- Rule 10 — no agent write action without idempotency key +
  confirmation artifact

### Trust boundary
**Signed system prompts do not make tool / RAG / external content
trusted.** All tool outputs, retrieved documents, user content,
logs, connection metadata, and external documents are wrapped and
labelled as untrusted data, never instructions. Per OWASP LLM
Prompt Injection Prevention guidance.

### Customer evidence
Every agent run produces a replay-safe trace (`tool_name`,
`input_hash`, `output_hash`, `decision_reason`, `redactions_applied`
(counts and categories only — never raw values), `outcome`).
Traces are durable for 90 days minimum and exportable as
SOC2 / DORA evidence.

### BYO key vs managed
F-Pulse OSS supports a "bring your own key" model where you supply
your own LLM provider credentials. F-Pulse cannot police what your
own provider does with your own key — review your provider's TOS
independently. F-Pulse+ adds a managed-provider option with workspace
budget enforcement and an on-prem Ollama path that keeps all AI
traffic inside your network.
