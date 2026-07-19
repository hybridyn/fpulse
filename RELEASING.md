# Releasing F-Pulse

The release pipeline is fully automated. Pushing a `v*.*.*` git tag
fires `.github/workflows/release.yml`, which publishes:

| Artifact          | Where                                | Status            |
|-------------------|--------------------------------------|-------------------|
| Docker image      | `hybridyn/fpulse:<version>` + `:latest` on Docker Hub | ✅ enabled |
| GitHub release    | release page with notes from CHANGELOG | ✅ enabled |
| PyPI sdist+wheel  | `fpulse` on pypi.org                 | ⏸️  gated — see below |

## Cutting a release

1. **Land everything on `main`.** The tag must point at a green commit.
2. **Update [`changelog.md`](CHANGELOG.md).** Add a `## [x.y.z] - YYYY-MM-DD`
   section above the previous release. The release workflow extracts
   this section verbatim into the GitHub release notes — if it can't
   find a matching section, it falls back to a minimal placeholder.
3. **Bump the version in [`pyproject.toml`](pyproject.toml).** Must
   match the tag (without the `v` prefix). The Docker image tag
   is derived from the git tag, so this only matters once the PyPI
   job is enabled.
4. **Tag and push.** (`vX.Y.Z` below is a placeholder — use the real
   version; it must match `pyproject.toml`.)
   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```
5. **Watch the Actions tab.** The `Release` workflow has three jobs:
   `github-release` (always runs), plus `docker` and `pypi` — both
   gated off until configured, so a tag push publishes a GitHub release
   only. See "One-time setup" below to enable them.
6. **Verify** (once the `docker` job is enabled — until then the image
   is built locally on first `docker compose up`):
   ```bash
   docker pull hybridyn/fpulse:X.Y.Z
   docker run --rm -p 5174:8001 hybridyn/fpulse:X.Y.Z
   curl http://localhost:5174/api/health
   ```

## One-time setup (Docker Hub)

**Not configured yet — the `docker` job is gated off.** Repository secrets
do not travel with code, so the credentials from the old repo did not come
across; without this setup every tag push failed at the login step with
`Username and password required`, publishing nothing.

To enable:

1. Add `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` repository secrets. The
   token is a Docker Hub Personal Access Token scoped to **Read & Write**
   on `hybridyn/fpulse`.
2. Set repository **variable** `PUBLISH_DOCKER=true` (Settings → Secrets
   and variables → Actions → Variables). The job stays skipped until this
   is set — no code change needed.
3. Drop the "builds locally on first run" heads-up from `README.md`, which
   is only true while the image is unpublished.

Rotate the token annually or on staff changes.

## Enabling PyPI publishing

**`fpulse` is not on PyPI yet.** Until it is, the README's headline
`pip install fpulse` fails for every reader, as does every
`pip install fpulse[postgres]`-style command in the docs. The name is
unclaimed — nobody can take it from us — but nothing installs until this
is done.

1. **Add a PENDING publisher on pypi.org**
   ([docs](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)).

   `fpulse` does not exist on PyPI yet, and **you cannot create an empty
   project** — there is no "new project" button. Instead you pre-register
   who is allowed to create it, and the project springs into existence on
   the first successful upload. Go to **Your account → Publishing → Add a
   new pending publisher** (NOT the per-project "Publishing" tab — that
   only exists once the project does).

   - PyPI Project Name: `fpulse`
   - Owner: `hybridyn`
   - Repository name: `fpulse`  ← the repo name, exactly. `f-pulse` or
     `hybridyn-f-pulse-oss` will fail.
   - Workflow name: `release.yml`  ← the filename of the workflow doing the
     upload. Each workflow needs its own publisher: `publish-testpypi.yml`
     on TestPyPI is a **separate** registration from `release.yml` here.
   - Environment name: leave blank (the job declares no `environment:`)

   Every one of these is matched against the OIDC token's claims. Any
   mismatch fails with `invalid-publisher: valid token, but no
   corresponding publisher`, which tells you nothing about *which* field is
   wrong. The job log prints the claims it actually sent — compare them
   field by field against the form.
2. **Set the gate.** Repository variable `PUBLISH_PYPI=true`
   (Settings → Secrets and variables → Actions → Variables). The job stays
   skipped until this is set — no code change needed.
3. **Push the version tag.** PyPI refuses re-uploads of an existing
   version, so a tag publishes exactly once. A bad wheel cannot be
   replaced in place — only yanked and re-released under a new version.
   That is why the job verifies the wheel's contents before publishing.

OIDC trusted publishing is preferred over the legacy `PYPI_API_TOKEN`
secret — no token rotation, scope is implicit from the workflow. Do not
add a token secret instead.

### The wheel must carry the UI

`fpulse` is one pip package that serves its own React app. The app builds
to `frontend/dist`, **outside** the Python package, so setuptools cannot
see it. The `pypi` job therefore builds the frontend, runs
`scripts/stage_frontend.py` to copy it to `backend/fpulse/frontend_dist`
(which `[tool.setuptools.package-data]` packages), and only then builds
the wheel.

Building a wheel without staging first produces a package that installs
fine and serves a 404 at `/` — no error, no warning. That is what the
first 1.0.0 wheel did: 445 `.py` files, no UI, no Swagger assets, no
connector manifests. It survived review because every path the team
exercised (source checkout, `pip install -e .`, Docker, the desktop
installers) reads `frontend/dist` directly and works. Only a PyPI install
hits the gap.

To build a release wheel by hand:

```bash
cd frontend && npm ci && npm run build && cd ..
python scripts/stage_frontend.py     # exits 1 if the frontend isn't built
python -m build
pytest backend/tests/test_packaging.py -m slow   # asserts the wheel's contents
```

## Hotfix flow

For a security or correctness fix that can't wait:

1. Branch from the previous release tag, not `main`.
2. Cherry-pick or write the fix.
3. Bump the patch version, update `changelog.md`, tag, push.
4. After the release lands, forward-merge the hotfix branch into `main`
   so the fix doesn't get reverted on the next minor.

## Rolling back

Docker images are immutable — to "roll back" you re-tag `:latest`:

```bash
docker buildx imagetools create -t hybridyn/fpulse:latest hybridyn/fpulse:<previous-version>
```

GitHub releases can be deleted from the Releases page. PyPI does not
allow re-using a version number — yank the bad version
(`pip install --user pkginfo` then via web UI) and ship `x.y.(z+1)`
with the fix.

## Tested-with matrix

Each release pins external versions in [`docker-compose.yml`](docker-compose.yml)
and records what was tested in [`changelog.md`](CHANGELOG.md) under
*Tested with*. Bump those when the runtime/Ollama version moves.
