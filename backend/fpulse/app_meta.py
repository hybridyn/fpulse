"""Product identity + where to reach the project — single source of truth.

Every in-app link (report an issue, request a connector, check for updates,
docs, discussions) is derived from the constants below. Moving GitHub orgs or
renaming the repo later is a ONE-LINE change here — nothing else needs editing.

2026-06-18: rebranded owner avisdreamer → hybridyn.
"""
from __future__ import annotations

VERSION = "1.0.0"

# GitHub coordinates. Change REPO_OWNER (and REPO_NAME if the repo is renamed)
# to re-point every in-app link at once.
REPO_OWNER = "hybridyn"
REPO_NAME = "hybridyn-f-pulse-oss"

HOMEPAGE = "https://hybridyn.com"
DOCS_URL = "https://docs.hybridyn.com/f-pulse"


def repo_url() -> str:
    return f"https://github.com/{REPO_OWNER}/{REPO_NAME}"


def issues_url() -> str:
    return f"{repo_url()}/issues"


def new_issue_url() -> str:
    """Issue-template chooser (bug / connector-request / node-request, …)."""
    return f"{repo_url()}/issues/new/choose"


def releases_url() -> str:
    return f"{repo_url()}/releases"


def discussions_url() -> str:
    return f"{repo_url()}/discussions"


def releases_api_url() -> str:
    """GitHub REST endpoint for the latest published release."""
    return f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/releases/latest"


__all__ = [
    "VERSION", "REPO_OWNER", "REPO_NAME", "HOMEPAGE", "DOCS_URL",
    "repo_url", "issues_url", "new_issue_url", "releases_url",
    "discussions_url", "releases_api_url",
]
