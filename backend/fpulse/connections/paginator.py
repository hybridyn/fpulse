"""Generic REST pagination helper for catalog providers.

Most SaaS APIs paginate. Each one is gratuitously different — a small
number of patterns covers >95% of the world: cursor-in-body,
Link-header (RFC 5988), offset/limit, and opaque page-token. This
module centralises them so per-connector providers stay thin.

Hard rules:
- Total wall-clock budget capped (default 30s) — catalog browsing
  must never block the UI for longer than this regardless of how
  big the underlying account is.
- Per-page item cap also enforced — protects against runaway loops
  on misconfigured cursors.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Iterator
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 30
DEFAULT_MAX_ITEMS = 5000


class PaginationBudgetExceeded(RuntimeError):
    pass


def _parse_link_header(value: str) -> dict[str, str]:
    """RFC 5988 Link header parser. Returns {rel: url}.

    Example input:
      <https://api.example.com/v1/items?page=2>; rel="next", <...>; rel="last"
    """
    out: dict[str, str] = {}
    if not value:
        return out
    for part in value.split(","):
        part = part.strip()
        if "; rel=" not in part and "; rel=" not in part.lower():
            continue
        url_part, rel_part = part.split(";", 1)
        url = url_part.strip().strip("<>").strip()
        rel = rel_part.split("=", 1)[1].strip().strip('"').strip("'")
        out[rel] = url
    return out


def paginate_link_header(
    session,  # requests.Session-like
    first_url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    max_items: int = DEFAULT_MAX_ITEMS,
    extract_items: Callable[[Any], list[Any]] = lambda body: body if isinstance(body, list) else [],
) -> Iterator[Any]:
    """GitHub / Atlassian / Mailchimp pattern — follow `Link: rel="next"`."""
    deadline = time.time() + timeout_s
    url: str | None = first_url
    yielded = 0
    while url:
        # Use >= so a timeout_s of 0 trips immediately rather than relying
        # on sub-millisecond clock jitter to push past the deadline on a
        # fast machine. Callers who want no enforcement should leave
        # timeout_s at its default.
        if time.time() >= deadline:
            raise PaginationBudgetExceeded(f"Pagination budget {timeout_s}s exceeded")
        r = session.get(url, headers=headers or {}, timeout=min(10, timeout_s))
        r.raise_for_status()
        body = r.json()
        for item in extract_items(body):
            if yielded >= max_items:
                return
            yield item
            yielded += 1
        next_url = _parse_link_header(r.headers.get("Link") or r.headers.get("link") or "").get("next")
        url = next_url


def paginate_cursor_in_body(
    session,
    first_url: str,
    *,
    cursor_path: list[str],            # e.g. ["nextCursor"] or ["paging","next","after"]
    cursor_param: str = "after",        # query-string key the cursor is sent back as
    items_path: list[str],              # e.g. ["results"]
    headers: dict[str, str] | None = None,
    method: str = "GET",
    extra_params: dict[str, Any] | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> Iterator[Any]:
    """HubSpot / Slack / many cursor-paginated APIs."""
    deadline = time.time() + timeout_s
    cursor: Any = None
    yielded = 0
    while True:
        if time.time() > deadline:
            raise PaginationBudgetExceeded(f"Pagination budget {timeout_s}s exceeded")
        params = dict(extra_params or {})
        if cursor:
            params[cursor_param] = cursor
        r = session.request(method, first_url, params=params, headers=headers or {},
                            timeout=min(10, timeout_s))
        r.raise_for_status()
        body = r.json()
        items = body
        for key in items_path:
            items = items.get(key, []) if isinstance(items, dict) else []
        for item in items or []:
            if yielded >= max_items:
                return
            yield item
            yielded += 1
        nxt: Any = body
        for key in cursor_path:
            nxt = nxt.get(key) if isinstance(nxt, dict) else None
            if nxt is None:
                break
        if not nxt:
            return
        cursor = nxt


def paginate_offset_limit(
    session,
    url: str,
    *,
    items_path: list[str],
    page_size: int = 100,
    offset_param: str = "offset",
    limit_param: str = "limit",
    headers: dict[str, str] | None = None,
    extra_params: dict[str, Any] | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    max_items: int = DEFAULT_MAX_ITEMS,
    has_more_path: list[str] | None = None,
) -> Iterator[Any]:
    """Jira / ServiceNow / many enterprise REST patterns."""
    deadline = time.time() + timeout_s
    offset = 0
    yielded = 0
    while True:
        if time.time() > deadline:
            raise PaginationBudgetExceeded(f"Pagination budget {timeout_s}s exceeded")
        params = dict(extra_params or {})
        params[offset_param] = offset
        params[limit_param] = page_size
        r = session.get(url, params=params, headers=headers or {}, timeout=min(10, timeout_s))
        r.raise_for_status()
        body = r.json()
        items = body
        for key in items_path:
            items = items.get(key, []) if isinstance(items, dict) else []
        items = items or []
        for item in items:
            if yielded >= max_items:
                return
            yield item
            yielded += 1
        # Stop conditions: explicit has_more flag OR short page.
        if has_more_path:
            more: Any = body
            for key in has_more_path:
                more = more.get(key) if isinstance(more, dict) else None
            if not more:
                return
        elif len(items) < page_size:
            return
        offset += page_size


def paginate_page_token(
    session,
    url: str,
    *,
    token_path: list[str],
    items_path: list[str],
    token_param: str = "pageToken",
    headers: dict[str, str] | None = None,
    extra_params: dict[str, Any] | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> Iterator[Any]:
    """Google APIs / many GCP services use opaque pageToken."""
    deadline = time.time() + timeout_s
    token: Any = None
    yielded = 0
    while True:
        if time.time() > deadline:
            raise PaginationBudgetExceeded(f"Pagination budget {timeout_s}s exceeded")
        params = dict(extra_params or {})
        if token:
            params[token_param] = token
        r = session.get(url, params=params, headers=headers or {}, timeout=min(10, timeout_s))
        r.raise_for_status()
        body = r.json()
        items = body
        for key in items_path:
            items = items.get(key, []) if isinstance(items, dict) else []
        for item in items or []:
            if yielded >= max_items:
                return
            yield item
            yielded += 1
        nxt: Any = body
        for key in token_path:
            nxt = nxt.get(key) if isinstance(nxt, dict) else None
            if nxt is None:
                break
        if not nxt:
            return
        token = nxt
