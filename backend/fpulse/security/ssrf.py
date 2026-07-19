"""SSRF defence for any node / API that fetches a user-supplied URL.

Extracted 2026-06-03 from ``connectors/ai_authoring.py`` so the same
hardening can guard every user-controlled URL fetch in F-Pulse — not
just the OpenAPI Author Connector flow.

# Threat

A user with pipeline-authoring rights can paste an arbitrary URL into
an ``api_source`` / ``http_request`` / OpenAPI fetch / etc. Without a
guard, the F-Pulse process happily fetches it. Classic SSRF abuse:

  * ``http://169.254.169.254/latest/meta-data/iam/security-credentials/``
    — AWS / GCP / Azure instance metadata
  * ``http://localhost:6379/`` — probe internal Redis
  * ``http://10.0.0.1/admin`` — pivot into private network
  * ``file:///etc/passwd`` — local file read via urllib

# Defence in depth

The :func:`check_url` function refuses any URL that:

1. Uses a scheme other than ``http`` or ``https`` (rules out
   ``file://``, ``gopher://``, ``ftp://``, ``data:``, etc).
2. Has no hostname (a bare scheme, or a URL like ``http://:8080``).
3. Embeds credentials in the URL (``http://user:pass@host`` — both an
   info-disclosure smell and a SSRF-via-parsing-quirk vector).
4. Resolves (any of its DNS A records) to:
     * loopback     — 127/8, ::1
     * link-local   — 169.254/16 (covers cloud metadata endpoints)
     * private      — 10/8, 172.16/12, 192.168/16, fc00::/7
     * multicast / reserved / unspecified

Callers fetching the URL MUST validate first and then connect to the
resolved IP (with the original Host header), so a DNS-rebinding attack
can't flip the address between the check and the fetch.

# Per-feature escape hatches

Some operators (on-prem with internal API catalogs, dev with local
mocks) legitimately need to fetch private-network hosts. Each caller
passes its own env-var name so the operator can enable the escape for
one feature without weakening the others:

  * OpenAPI authoring  → ``FPULSE_OPENAPI_FETCH_ALLOW_PRIVATE=1``
  * API source / HTTP  → ``FPULSE_API_SOURCE_ALLOW_PRIVATE=1``

Even with the escape on, the obviously-dangerous categories
(multicast / reserved / unspecified) stay blocked — there is no
legitimate reason to fetch those.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from typing import Tuple
from urllib.parse import urlparse


__all__ = ["SsrfBlockedError", "check_url", "OPENAPI_ALLOW_PRIVATE_ENV", "API_SOURCE_ALLOW_PRIVATE_ENV"]


# Canonical env-var names for the two escape hatches. Imported by both
# callers so we have one place to grep when an operator asks "what env
# var lets me allow internal hosts?".
OPENAPI_ALLOW_PRIVATE_ENV = "FPULSE_OPENAPI_FETCH_ALLOW_PRIVATE"
API_SOURCE_ALLOW_PRIVATE_ENV = "FPULSE_API_SOURCE_ALLOW_PRIVATE"


class SsrfBlockedError(ValueError):
    """URL rejected by SSRF policy. Message body is plain text safe to
    surface to the user (no internal state leak)."""


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def check_url(url: str, *, allow_private_env: str | None = None) -> Tuple[str, str, int]:
    """Validate ``url`` against the SSRF policy.

    Returns ``(scheme, hostname, port)`` on success. Raises
    :class:`SsrfBlockedError` with a user-safe message on any policy
    violation.

    Parameters
    ----------
    url
        The user-supplied URL. Must be a string of length ≤ 2048.
    allow_private_env
        Env var name that, when truthy, permits private/loopback hosts.
        Pass ``OPENAPI_ALLOW_PRIVATE_ENV`` or
        ``API_SOURCE_ALLOW_PRIVATE_ENV``. ``None`` (default) = no escape.

    Notes
    -----
    Does NOT fetch. Inspection only. The caller's fetch should bind to
    the resolved IP (with Host header set to the original hostname) so
    a DNS-rebinding attack can't flip the address between the check and
    the fetch.
    """
    if not isinstance(url, str) or len(url) > 2048:
        raise SsrfBlockedError("URL too long or not a string")

    parsed = urlparse(url.strip())

    if parsed.scheme not in ("http", "https"):
        raise SsrfBlockedError(
            f"Scheme {parsed.scheme!r} not allowed — only http/https"
        )
    if not parsed.hostname:
        raise SsrfBlockedError("URL has no hostname")
    if parsed.username or parsed.password:
        raise SsrfBlockedError("URL must not embed credentials")

    hostname = parsed.hostname.lower()
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    allow_private = bool(allow_private_env) and _env_truthy(allow_private_env)

    # Resolve all addresses the hostname returns. If ANY resolves to a
    # disallowed range, refuse. Stricter than "the first one" because
    # some attacker setups round-robin across a legit + a localhost record.
    try:
        addrinfo = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SsrfBlockedError(f"DNS resolution failed for {hostname}") from exc

    for entry in addrinfo:
        addr = entry[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            raise SsrfBlockedError(f"Resolved address {addr!r} not parseable")
        if allow_private:
            # Even with private allowed, obviously-dangerous categories
            # (multicast / reserved / unspecified) stay blocked — no
            # legitimate use case to fetch them.
            if ip.is_multicast or ip.is_unspecified or ip.is_reserved:
                raise SsrfBlockedError(
                    f"Host {hostname} resolves to multicast/reserved {ip}"
                )
        else:
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
                or ip.is_unspecified
            ):
                escape = f" Set {allow_private_env}=1 to allow internal-network targets in trusted deployments." if allow_private_env else ""
                raise SsrfBlockedError(
                    f"Host {hostname} resolves to {ip} which is blocked "
                    f"(private/loopback/link-local/multicast/reserved).{escape}"
                )

    return parsed.scheme, hostname, port


def is_internal_host(url: str) -> bool:
    """True if ``url``'s host resolves to a private / link-local (incl. cloud
    metadata 169.254) / reserved / multicast address — the categories a
    non-admin egress probe must not reach.

    LOOPBACK is deliberately EXCLUDED (returns False): local services such as
    Ollama live on 127.0.0.1 and are a legitimate target. Public hosts return
    False. Unresolvable / malformed input returns False (fail-open — the real
    fetch will then fail on its own). Callers pair this with an admin check:
    ``if is_internal_host(u) and not is_admin: 403``.

    This is the SOFT, admin-aware companion to ``guard_url`` above: guard_url
    hard-blocks every private/loopback target for node fetches; is_internal_host
    lets an admin reach an internal host on purpose (test a local model, an
    on-box service) while stopping a non-admin from turning a probe endpoint
    into an internal-network reach.
    """
    try:
        parsed = urlparse((url or "").strip())
        host = (parsed.hostname or "").lower()
        if not host or host in ("localhost", "127.0.0.1", "::1"):
            return False
        addrinfo = socket.getaddrinfo(host, None)
    except Exception:
        return False
    for entry in addrinfo:
        try:
            ip = ipaddress.ip_address(entry[4][0])
        except ValueError:
            continue
        if ip.is_loopback:
            continue
        if (ip.is_private or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return True
    return False
