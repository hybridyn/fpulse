"""In-app product/contact metadata + opt-in update check.

Powers the "Help & Feedback" hub so users reach the project from inside the
app (report an issue, request a connector, check for updates) instead of
hunting on the website.

Privacy stance (this is a local-first OSS tool):
  * No telemetry, nothing automatic. /update-check runs only when the user
    clicks it, fetches ONLY the public GitHub "latest release" (a fixed URL,
    no user data sent), and degrades gracefully when offline / air-gapped.
  * Reporting an issue / requesting a connector happens by opening a
    pre-filled GitHub issue in the user's browser — the user reviews and
    submits it themselves. The server never transmits user data.
"""
from __future__ import annotations

import logging

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


@router.get("/update-check", dependencies=[Depends(require_auth)])
async def update_check() -> dict:
    """Compare the running version against the latest GitHub release.

    Opt-in (called on a user click). No user data leaves the box — it only
    GETs the project's public latest-release. Any network/parse failure
    returns ``checked: false`` so air-gapped installs see a clean "couldn't
    check" instead of an error.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(
                app_meta.releases_api_url(),
                headers={"Accept": "application/vnd.github+json"},
            )
    except Exception as exc:  # noqa: BLE001 — offline / DNS / timeout
        logger.debug("update-check unreachable: %s", exc)
        return {"checked": False, "offline": True, "current": app_meta.VERSION,
                "releases_url": app_meta.releases_url()}

    if resp.status_code == 404:
        # No releases published yet — not an error, just nothing to compare.
        return {"checked": True, "available": False, "current": app_meta.VERSION,
                "reason": "no_releases", "releases_url": app_meta.releases_url()}
    if resp.status_code != 200:
        return {"checked": False, "current": app_meta.VERSION,
                "reason": f"github_{resp.status_code}",
                "releases_url": app_meta.releases_url()}

    data = resp.json()
    latest = (data.get("tag_name") or data.get("name") or "").strip()
    available = _is_newer(latest, app_meta.VERSION)
    return {
        "checked": True,
        "available": available,
        "current": app_meta.VERSION,
        "latest": latest.lstrip("vV") or None,
        "url": data.get("html_url") or app_meta.releases_url(),
        "notes": (data.get("body") or "")[:2000],
        "published_at": data.get("published_at"),
    }
