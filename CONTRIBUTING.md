# Contributing to F-Pulse

Thanks for your interest in contributing. This guide covers how to get set
up, our conventions, and the legal step every contribution requires.

## Contributor License Agreement (required)

**Every contributor must sign the F-Pulse [CLA](CLA.md) before their first
PR can be merged.** The CLA-Assistant bot will comment on your first PR
with a one-click sign-off link — no manual paperwork.

Why we require this: the CLA grants Hybridyn the right to relicense your
contribution and to use it in commercial F-Pulse+ features. Without it,
we cannot accept your code.

## Reporting bugs

Open an issue on GitHub with:

- F-Pulse version (`fpulse --version`)
- OS and Python version
- Minimal reproduction steps
- Actual vs expected behavior

Security vulnerabilities go through [security.md](SECURITY.md), not public issues.

## Proposing features

Open a GitHub Discussion first for anything non-trivial — we'd rather agree
on the approach before you spend hours on a PR. For small fixes, a PR is fine.

## Setting up the dev environment

```bash
TODO: dev setup instructions land here in Phase 2 of the repo split
# git clone, python venv, install deps, run tests
```

## Conventions

- Python 3.11+, type hints required on public functions
- `ruff` for lint, `pytest` for tests
- One feature per PR; rebase on `main` before requesting review
- Tests required for all behavior changes
- No new dependencies without discussion

## What goes in F-Pulse vs F-Pulse+

This repository contains the open-source core. Commercial features
(team workspaces, RBAC enforcement, approval gates, audit, vault,
sandbox, SLA support) live in a separate proprietary repository and
are NOT accepted here.

If you're unsure where a feature belongs, ask in a GitHub Discussion first.
The current free / commercial split is documented in [edition-matrix.md](edition-matrix.md).

## Contributing a connector

Connectors are JSON manifests, not code. The contribution path:

1. Read [community-connectors/readme.md](community-connectors/readme.md).
2. Drop your manifest in `community-connectors/`.
3. Run `python -m fpulse.connectors.certify <connector_id>` locally — the
   PR cannot merge until it passes.
4. Open a PR using the connector template:
   add `?template=connector.md` to the compare URL.

Manifests live in `community-connectors/` until a maintainer promotes them
into the built-in catalog at `backend/fpulse/connectors/manifests/`.
Custom transform code is not yet accepted via this path — open a
Discussion first if your connector needs more than declarative config.

## Code of conduct

All participation is governed by [code-of-conduct.md](CODE_OF_CONDUCT.md).
