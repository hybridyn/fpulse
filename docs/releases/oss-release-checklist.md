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

F-Pulse does NOT auto-create an admin by default — the operator creates one
on first launch. For CI/E2E, start the backend with **`FPULSE_BOOTSTRAP_ADMIN=1`**;
F-Pulse then seeds a bootstrap admin (`admin@fpulse.local`) and writes its
generated password to `INITIAL_ADMIN_PASSWORD.txt` in the data dir. The
Playwright E2E workflow sets this flag, and a fresh data dir per run makes it
self-seeding.

For **local** authenticated E2E, start the server with `FPULSE_BOOTSTRAP_ADMIN=1`
(or use an admin you created yourself), then point the tools at it and supply
the password via env:

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

## 4. Publication gates (the docs promise these — verify they are true)

Tagging a release does **not** publish the artifacts. Each is gated off
until configured, so a tag can go out with every CI check green while the
install commands in the README still fail. Verify each one *before*
announcing, not after.

| Promise in the docs | Verify | Enable |
|---|---|---|
| `pip install fpulse` (README headline; ~60 refs incl. `fpulse[postgres]` etc.) | `curl -sI https://pypi.org/pypi/fpulse/json` → 200, not 404 | Create the `fpulse` project on pypi.org + add a Trusted Publisher (owner=hybridyn, repo=fpulse, workflow=release.yml), then set repo variable `PUBLISH_PYPI=true`. No API token — OIDC only. |
| `docker pull hybridyn/fpulse:<ver>` | `curl -sI https://hub.docker.com/v2/repositories/hybridyn/fpulse/` → 200 | Add `DOCKERHUB_USERNAME` + `DOCKERHUB_TOKEN` secrets, then set repo variable `PUBLISH_DOCKER=true`. Remove the README "builds locally on first run" heads-up in the same change. |
| Repo is reachable at `github.com/hybridyn/fpulse` | open it while signed out | Flip visibility to public. **Do this early, not at announce time** — CodeQL self-skips on private repos, so its first-ever run happens on the flip, in public. |

> Secrets and variables do **not** travel with code. Anything configured on
> a previous repo is gone after a move — this is how the Docker job came to
> fail on every tag push with "Username and password required".

## 5. Operational polish

- Stale `.fpulse/runtime/instance.json` after an unclean shutdown →
  `fpulse doctor --repair` clears it so the launcher / `fpulse stop`
  ownership checks are clean.
- Confirm `LICENSE` is **Apache 2.0** (CI's `license-and-changelog` job
  enforces this) and `THIRD_PARTY_LICENSES` / SBOM are current. F-Pulse OSS
  is Apache 2.0; only the F-Pulse+ monorepo is proprietary — do not carry
  that rule across.
- Confirm the connector-count claim in `README` matches reality (33
  first-party visible by default — see
  [`../product_facts/08_connectors.md`](../product_facts/08_connectors.md)
  for the definition and why it is not 37 or 43).
