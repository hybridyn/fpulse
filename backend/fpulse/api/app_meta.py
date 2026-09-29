"""In-app product/contact metadata + opt-in update check.

Powers the "Help & Feedback" hub so users reach the project from inside the
app (report an issue, request a connector, check for updates) instead of
hunting on the website.

Privacy stance (this is a local-first OSS tool):
  * No telemetry, nothing automatic. /update-check reads ONLY the project's
    public release metadata (PyPI JSON + the GitHub "latest release") — fixed
    URLs, no user data sent — and degrades gracefully when offline / air-gapped.
  * Reporting an issue / requesting a connector happens by opening a
    pre-filled GitHub issue in the user's browser — the user reviews and
    submits it themselves. The server never transmits user data.
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends

from fpulse import app_meta
from fpulse.auth.deps import require_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/app", tags=["app-meta"])


@router.get("/info")
async def app_info() -> dict:
    """Product identity + the canonical contact/links. Open (no secrets)."""
    return {
        "version": app_meta.VERSION,
        "homepage": app_meta.HOMEPAGE,
        "docs_url": app_meta.DOCS_URL,
        "repo_url": app_meta.repo_url(),
        "issues_url": app_meta.issues_url(),
        "new_issue_url": app_meta.new_issue_url(),
        "releases_url": app_meta.releases_url(),
        "discussions_url": app_meta.discussions_url(),
    }


def _parse_semver(v: str) -> tuple[int, ...]:
    """Lenient numeric-version tuple. 'v1.2.3' / '1.2' / '1.2.3-rc1' → tuple."""
    core = (v or "").strip().lstrip("vV").split("-")[0].split("+")[0]
    parts: list[int] = []
    for chunk in core.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def _is_newer(latest: str, current: str) -> bool:
    if not latest:
        return False
    a, b = _parse_semver(latest), _parse_semver(current)
    n = max(len(a), len(b))
    a += (0,) * (n - len(a))
    b += (0,) * (n - len(b))
    return a > b


def _install_channel() -> str:
    """Best-effort guess of how this instance was installed, so the upgrade
    hint matches how the user actually updates. Never raises."""
    try:
        if os.path.exists("/.dockerenv") or os.environ.get("FPULSE_IN_DOCKER") == "1":
            return "docker"
        import fpulse
        pkg = (getattr(fpulse, "__file__", "") or "").replace("\\", "/").lower()
        if "site-packages" in pkg or "dist-packages" in pkg:
            return "pip"
        return "source"
    except Exception:  # noqa: BLE001
        return "unknown"


_UPGRADE_HINTS = {
    "pip": "pip install --upgrade fpulse",
    "docker": "docker compose pull && docker compose up -d",
    "source": "git pull && pip install -e .",
    "unknown": "",
}


async def _pypi_latest(client) -> str | None:
    """Latest published version from the PyPI JSON API, or None on any failure."""
    try:
        resp = await client.get(app_meta.pypi_json_url(), headers={"Accept": "application/json"})
        if resp.status_code == 200:
            return ((resp.json().get("info") or {}).get("version") or "").strip() or None
    except Exception:  # noqa: BLE001
        pass
    return None


async def _github_latest(client) -> tuple[str | None, dict]:
    """(tag, release-json) for the latest GitHub release, or (None, {}) on failure."""
    try:
        resp = await client.get(
            app_meta.releases_api_url(),
            headers={"Accept": "application/vnd.github+json"},
        )
        if resp.status_code == 200:
            data = resp.json()
            return ((data.get("tag_name") or data.get("name") or "").strip() or None), data
    except Exception:  # noqa: BLE001
        pass
    return None, {}


@router.get("/update-check", dependencies=[Depends(require_auth)])
async def update_check() -> dict:
    """Compare the running version against the newest public release.

    Opt-in (called on a user click, and cached client-side). No user data
    leaves the box — it only GETs the project's public release metadata from
    PyPI and GitHub. Checking BOTH matters: ``pip install fpulse`` tracks PyPI
    while installers/source track GitHub releases, and the two can differ. Any
    network/parse failure degrades to ``checked: false`` so air-gapped installs
    see a clean "couldn't check" instead of an error.
    """
    import httpx

    channel = _install_channel()
    current = app_meta.VERSION
    pypi_ver: str | None = None
    gh_ver: str | None = None
    gh_data: dict = {}

    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            pypi_ver = await _pypi_latest(client)
            gh_ver, gh_data = await _github_latest(client)
    except Exception as exc:  # noqa: BLE001 — offline / DNS / timeout
        logger.debug("update-check unreachable: %s", exc)

    # Both sources unreachable → honest "couldn't check" (air-gapped path).
    if not pypi_ver and not gh_ver:
        return {"checked": False, "offline": True, "current": current,
                "channel": channel, "releases_url": app_meta.releases_url()}

    # Newest across whatever we could reach.
    best = max((v for v in (pypi_ver, gh_ver) if v), key=_parse_semver)
    from_github = bool(gh_ver) and _parse_semver(gh_ver) >= _parse_semver(best)

    # Prefer GitHub's rich release page + notes when it is (tied for) newest;
    # otherwise point at the PyPI project page for that version.
    if from_github and gh_data:
        url = gh_data.get("html_url") or app_meta.releases_url()
        notes = (gh_data.get("body") or "")[:2000]
        published_at = gh_data.get("published_at")
    else:
        url = app_meta.pypi_project_url(best)
        notes = ""
        published_at = None

    return {
        "checked": True,
        "available": _is_newer(best, current),
        "current": current,
        "latest": best.lstrip("vV") or None,
        "channel": channel,
        "upgrade_hint": _UPGRADE_HINTS.get(channel, ""),
        "url": url,
        "notes": notes,
        "published_at": published_at,
        "releases_url": app_meta.releases_url(),
    }
