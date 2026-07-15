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
4. **Tag and push.**
   ```bash
   git tag v1.0.1
   git push origin v1.0.1
   ```
5. **Watch the Actions tab.** The `Release` workflow has three jobs:
   `docker`, `github-release`, `pypi` (gated). Each takes 3-8 minutes.
6. **Verify.** Pull the new image and smoke-test:
   ```bash
   docker pull hybridyn/fpulse:1.0.1
   docker run --rm -p 5174:8001 hybridyn/fpulse:1.0.1
   curl http://localhost:5174/api/health
   ```

## One-time setup (Docker Hub)

Already configured. For reference:

- `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` repository secrets are set.
- The token is a Docker Hub Personal Access Token scoped to **Read &
  Write** on `hybridyn/fpulse`.
- Rotate annually or on staff changes.

## Enabling PyPI publishing

The `pypi` job is gated behind `if: false`. To turn it on:

1. **Configure trusted publishing on pypi.org**
   ([docs](https://docs.pypi.org/trusted-publishers/)).
   - Project: `fpulse`
   - Owner: `hybridyn`
   - Repository: `f-pulse`
   - Workflow filename: `release.yml`
   - Environment name: leave blank
2. **Flip the gate.** In `.github/workflows/release.yml`, change
   `if: false` to `if: true` on the `pypi` job.
3. **Cut a patch release** (e.g. `v1.0.1`) to validate the flow before
   the next major.

OIDC trusted publishing is preferred over the legacy `PYPI_API_TOKEN`
secret — no token rotation, scope is implicit from the workflow.

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
