"""Pluggable web search for the Copilot ``web_search`` tool.

F-Pulse ships no bundled search index or crawler — a real web search needs a
third-party search API. Rather than pretend, this module reads a provider +
key from the environment and calls that provider's HTTP API. With nothing
configured it raises :class:`WebSearchNotConfigured` carrying a message the
Copilot relays to the user verbatim.

Configure with::

    FPULSE_WEB_SEARCH_PROVIDER=brave      # or: tavily
    FPULSE_WEB_SEARCH_API_KEY=<key>
    FPULSE_WEB_SEARCH_ENDPOINT=<url>      # optional override

Providers implemented: Brave Search API, Tavily. Both normalise to a list of
``{title, url, snippet}``. The HTTP call is injectable (``_transport``) so the
plumbing is unit-tested without network access.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable

from fpulse.ai.web import (
    SEARCH_API_KEY_ENV,
    SEARCH_PROVIDER_ENV,
    get_search_config,
)

# A transport takes (method, url, headers, json_body|None, timeout) and returns
# the parsed JSON response as a dict. Injectable for tests.
Transport = Callable[[str, str, dict, "dict | None", float], dict]

DEFAULT_TIMEOUT = 12.0

# Providers fall into two families:
#   * KEY providers (brave, tavily) — a hosted API the user brings a key for.
#   * URL providers (searxng, hybridyn) — F-Pulse points at an endpoint the
#     ENTERPRISE controls (a SearXNG container inside their network) or that
#     Hybridyn hosts (the managed gateway). No per-user third-party signup.
KEY_PROVIDERS = ("brave", "tavily")
URL_PROVIDERS = ("searxng", "hybridyn")
SUPPORTED_PROVIDERS = KEY_PROVIDERS + URL_PROVIDERS

_BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
_TAVILY_ENDPOINT = "https://api.tavily.com/search"


class WebSearchNotConfigured(RuntimeError):
    """Web access is on but no usable search provider is configured. The
    message is safe to surface to the user."""


def _urllib_transport(method: str, url: str, headers: dict, body: dict | None, timeout: float) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed provider host
            raw = resp.read(2 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(2048).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"search provider returned HTTP {exc.code}: {detail[:200]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"search provider unreachable: {exc}") from exc
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("search provider returned non-JSON") from exc


def search_web(
    query: str,
    *,
    count: int = 5,
    timeout: float = DEFAULT_TIMEOUT,
    _transport: Transport | None = None,
) -> list[dict[str, str]]:
    """Run a web search via the configured provider.

    Returns a list of ``{title, url, snippet}`` (at most ``count``). Raises
    :class:`WebSearchNotConfigured` when no provider/key is set, and
    ``RuntimeError`` on a provider/transport error.
    """
    query = (query or "").strip()
    if not query:
        raise ValueError("query is required")
    count = max(1, min(int(count), 10))

    # Provider/key/endpoint resolve from the admin setting first, then env.
    provider, api_key, endpoint = get_search_config()

    if not provider:
        raise WebSearchNotConfigured(
            "Web search is enabled but no provider is configured. Set "
            f"{SEARCH_PROVIDER_ENV} (one of: {', '.join(SUPPORTED_PROVIDERS)}) and "
            f"{SEARCH_API_KEY_ENV} to a valid API key, then restart. Meanwhile you "
            "can paste an API's OpenAPI spec or a docs URL directly."
        )
    if provider not in SUPPORTED_PROVIDERS:
        raise WebSearchNotConfigured(
            f"Unknown search provider {provider!r}. Supported: {', '.join(SUPPORTED_PROVIDERS)}."
        )
    if provider in KEY_PROVIDERS and not api_key:
        raise WebSearchNotConfigured(
            f"Provider '{provider}' needs an API key — set one in Settings -> AI Provider -> Copilot web access."
        )
    if provider in URL_PROVIDERS and not endpoint:
        what = "your SearXNG instance URL" if provider == "searxng" else "the Hybridyn search gateway URL"
        raise WebSearchNotConfigured(
            f"Provider '{provider}' needs an endpoint — set {what} in Settings -> AI Provider -> Copilot web access."
        )

    transport = _transport or _urllib_transport

    if provider == "brave":
        return _search_brave(query, count, api_key, endpoint or _BRAVE_ENDPOINT, timeout, transport)
    if provider == "tavily":
        return _search_tavily(query, count, api_key, endpoint or _TAVILY_ENDPOINT, timeout, transport)
    if provider == "searxng":
        return _search_searxng(query, count, endpoint, timeout, transport)
    return _search_hybridyn(query, count, api_key, endpoint, timeout, transport)


def _search_brave(query, count, api_key, endpoint, timeout, transport) -> list[dict[str, str]]:
    from urllib.parse import urlencode

    url = f"{endpoint}?{urlencode({'q': query, 'count': count})}"
    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": api_key,
        "User-Agent": "F-Pulse/1.0 Copilot-web-search",
    }
    data = transport("GET", url, headers, None, timeout)
    results = ((data.get("web") or {}).get("results")) or []
    out: list[dict[str, str]] = []
    for r in results[:count]:
        if not isinstance(r, dict):
            continue
        out.append({
            "title": str(r.get("title") or "").strip(),
            "url": str(r.get("url") or "").strip(),
            "snippet": str(r.get("description") or "").strip(),
        })
    return out


def _search_tavily(query, count, api_key, endpoint, timeout, transport) -> list[dict[str, str]]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "F-Pulse/1.0 Copilot-web-search",
    }
    body: dict[str, Any] = {"api_key": api_key, "query": query, "max_results": count}
    data = transport("POST", endpoint, headers, body, timeout)
    results = data.get("results") or []
    out: list[dict[str, str]] = []
    for r in results[:count]:
        if not isinstance(r, dict):
            continue
        out.append({
            "title": str(r.get("title") or "").strip(),
            "url": str(r.get("url") or "").strip(),
            "snippet": str(r.get("content") or "").strip(),
        })
    return out


def _search_searxng(query, count, base_url, timeout, transport) -> list[dict[str, str]]:
    """Query a self-hosted SearXNG metasearch instance (keyless).

    The ENTERPRISE runs SearXNG inside their own network; F-Pulse just calls
    its JSON API, so no third-party account and no data leaving the perimeter.
    Requires the instance to allow the JSON format (``search.formats: [json]``
    in searxng settings.yml).
    """
    from urllib.parse import urlencode

    base = str(base_url).rstrip("/")
    url = f"{base}/search?{urlencode({'q': query, 'format': 'json', 'categories': 'general'})}"
    headers = {
        "Accept": "application/json",
        "User-Agent": "F-Pulse/1.0 Copilot-web-search",
    }
    data = transport("GET", url, headers, None, timeout)
    results = data.get("results") or []
    out: list[dict[str, str]] = []
    for r in results[:count]:
        if not isinstance(r, dict):
            continue
        out.append({
            "title": str(r.get("title") or "").strip(),
            "url": str(r.get("url") or "").strip(),
            "snippet": str(r.get("content") or "").strip(),
        })
    return out


def _search_hybridyn(query, count, token, endpoint, timeout, transport) -> list[dict[str, str]]:
    """Query the Hybridyn managed search gateway (F-Pulse+ / Enterprise).

    The gateway is a Hybridyn-hosted service that holds the upstream provider
    key and bears the cost, so the customer needs no third-party signup — they
    just point at the gateway URL and authenticate with their license/gateway
    token. The gateway normalises results to ``{title, url, snippet}``.
    """
    ep = str(endpoint).rstrip("/")
    url = ep if ep.endswith("/search") else f"{ep}/search"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "F-Pulse/1.0 Copilot-web-search",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = transport("POST", url, headers, {"query": query, "count": count}, timeout)
    results = data.get("results") or []
    out: list[dict[str, str]] = []
    for r in results[:count]:
        if not isinstance(r, dict):
            continue
        out.append({
            "title": str(r.get("title") or "").strip(),
            "url": str(r.get("url") or "").strip(),
            # accept either 'snippet' or 'content' from the gateway
            "snippet": str(r.get("snippet") or r.get("content") or "").strip(),
        })
    return out
