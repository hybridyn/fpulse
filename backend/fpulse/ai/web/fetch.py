"""SSRF-hardened URL fetch for the Copilot ``web_fetch`` tool.

Mirrors the redirect-revalidation discipline of
``connectors.ai_authoring.fetch_openapi_spec`` but is content-agnostic: it
returns the decoded body text plus metadata, letting the Copilot read a public
docs page or pull an OpenAPI/JSON document. Every URL — the initial one and
each redirect ``Location`` — is validated through
:func:`fpulse.security.ssrf.check_url` before a socket is opened.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from urllib.parse import urljoin

from fpulse.security.ssrf import (
    SsrfBlockedError,
    WEB_TOOLS_ALLOW_PRIVATE_ENV,
    check_url,
)

# Public docs pages can be large; cap hard so a huge page can't blow up the
# LLM context or memory. 1 MB is generous for a docs page or an OpenAPI JSON.
MAX_FETCH_BYTES = 1 * 1024 * 1024
MAX_REDIRECTS = 5
DEFAULT_TIMEOUT = 12.0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface 3xx responses un-followed so each hop can be re-validated."""

    def http_error_301(self, req, fp, code, msg, headers):  # noqa: D401
        return fp

    http_error_302 = http_error_301
    http_error_303 = http_error_301
    http_error_307 = http_error_301
    http_error_308 = http_error_301


def fetch_url_text(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Fetch ``url`` and return ``{url, final_url, status, content_type, text,
    bytes, truncated}``.

    SSRF-hardened: the initial URL and every redirect are checked against the
    web-tools SSRF policy (private/loopback/metadata blocked unless
    ``FPULSE_AI_WEB_ALLOW_PRIVATE=1``). Raises
    :class:`fpulse.security.ssrf.SsrfBlockedError` on a blocked target and
    ``RuntimeError`` on a network/HTTP error.
    """
    current_url = url
    final_url = url
    status = 0
    content_type = ""
    body = b""

    for _hop in range(MAX_REDIRECTS):
        check_url(current_url, allow_private_env=WEB_TOOLS_ALLOW_PRIVATE_ENV)
        req = urllib.request.Request(
            current_url,
            headers={
                "User-Agent": "F-Pulse/1.0 Copilot-web-fetch",
                "Accept": "application/json, application/yaml, text/yaml, text/html, text/plain, */*;q=0.1",
            },
        )
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(req, timeout=timeout) as resp:
                status = resp.getcode()
                if status in (301, 302, 303, 307, 308):
                    nxt = resp.headers.get("Location")
                    if not nxt:
                        raise SsrfBlockedError("Redirect with no Location header")
                    current_url = urljoin(current_url, nxt)
                    continue
                final_url = current_url
                content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
                body = resp.read(MAX_FETCH_BYTES + 1)
                break
        except urllib.error.HTTPError as exc:
            # A 4xx/5xx is a real answer the Copilot can use — surface the
            # status + a short body snippet rather than a bare exception.
            final_url = current_url
            status = exc.code
            content_type = (exc.headers.get("Content-Type") or "").split(";")[0].strip() if exc.headers else ""
            try:
                body = exc.read(MAX_FETCH_BYTES + 1)
            except Exception:  # noqa: BLE001
                body = b""
            break
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Fetch failed: {exc}") from exc
    else:
        raise SsrfBlockedError(f"Too many redirects (>{MAX_REDIRECTS}); possible loop")

    truncated = len(body) > MAX_FETCH_BYTES
    if truncated:
        body = body[:MAX_FETCH_BYTES]
    text = body.decode("utf-8", errors="replace")

    return {
        "url": url,
        "final_url": final_url,
        "status": status,
        "content_type": content_type,
        "text": text,
        "bytes": len(body),
        "truncated": truncated,
    }
