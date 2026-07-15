# F-Pulse OSS — release checklist

A repeatable pre-release gate for F-Pulse OSS. Split into **verification**
(must be green before tagging) and **operator guidance** (how the two run
modes differ, so release notes and docs stay honest).

## 1. Verification gates (all must pass before a release tag)

Run locally when CI minutes are unavailable; these mirror the CI workflows.

| Gate | Command (from repo root unless noted) | Signal |
|---|---|---|
| Backend full suite | `python -m pytest backend/tests -q` | exit 0 |
| Backend security suite | `python -m pytest backend/tests/test_security_*.py backend/tests/test_route_posture.py backend/tests/test_ai_action_gate.py backend/tests/test_execution_codes.py -q` | all pass |
| Frontend unit/contract | `cd frontend && npm test` | all pass |
| Frontend **type-checked** build | `cd frontend && npm run build` (`tsc -b && vite build`) | 0 type errors |
| Authenticated E2E | see §3 | login + page-render smoke pass |
| Route-posture ratchet | part of the backend suite (`test_route_posture.py`) | no new *ungated* mutating route |

> `vite build` alone is a false green — it skips type-checking. The real
> gate is `tsc -b` = 0 errors, which `npm run build` runs first.

CI workflows that already enforce these (make them **required** checks in
branch protection before release): `ci.yml`, `unit-tests.yml`,
`e2e-playwright.yml` (runs the full Playwright spec set, including
`node-configs.spec.ts`), `security-scan.yml`, `codeql.yml`.

## 2. Choose the security mode for the release artifact

`FPULSE_SECURITY_MODE` (see
[`../install/security-hardening.md`](../install/security-hardening.md)):

- **`local`** (default) — single-user, loopback-only, anonymous access
  falls back to the `default` workspace. Correct for the `pip install
  fpulse && fpulse open` laptop experience. Nothing to configure.
- **`server`** — exposed / multi-user. Auth required (no anonymous
  fallback), one-time execution codes on, CORS same-origin only, AI
  execute-actions require a write role.

Guard rails to confirm in the shipped artifact:
- A non-loopback bind in `local` mode **refuses to start** (exposure guard).
- `FPULSE_ALLOW_LAN=1` implies `server` mode automatically.
- `curl http://127.0.0.1:8001/api/health/bind-info` → `loopback_only: true`
  in the default build.

## 3. Admin credential + authenticated E2E

On first backend start against an empty data dir, a bootstrap admin is
created (`admin@fpulse.local`) and its generated password is written to
`INITIAL_ADMIN_PASSWORD.txt` in the data dir. CI uses a fresh data dir per
run, so E2E there is self-seeding.

For **local** authenticated E2E, point the tools at a running server and
supply the admin password via env:

```powershell
$env:FPULSE_URL      = "http://127.0.0.1:8001"   # or the Vite dev server :5174
$env:FPULSE_E2E_EMAIL    = "admin@fpulse.local"
$env:FPULSE_E2E_PASSWORD = "<the admin password>"
cd frontend; npx playwright test
```

The E2E `global-setup.ts` locates the login fields by input **type**
(`input[type="email"]` / `input[type="password"]`) — not by placeholder
text — so a mojibake'd bullet placeholder can never silently break login.

If the local server's admin password is unknown / out of sync with the
`INITIAL_ADMIN_PASSWORD.txt` you have, reset it directly in that data
dir's `fpulse.db` with `UserStore.set_password(user_id,
User.hash_password(new_pw))`. Note the installed service uses its own data
dir (`%LOCALAPPDATA%\FPulse\data` on Windows), which is **not** any repo
`data/` copy — reset the DB the running server actually opened.

## 4. Operational polish

- Stale `.fpulse/runtime/instance.json` after an unclean shutdown →
  `fpulse doctor --repair` clears it so the launcher / `fpulse stop`
  ownership checks are clean.
- Confirm `LICENSE` is proprietary and `THIRD_PARTY_LICENSES` / SBOM are
  current.
- Confirm the connector-count claim in `README` matches reality (33
  first-party).
