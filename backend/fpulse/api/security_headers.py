"""
Security headers middleware (2026-05-06).

Adds the response headers vulnerability scanners (Nessus, ZAP, Qualys)
look for, and strips identifiers (Server, X-Powered-By) that fingerprint
the stack. Defaults are picked to pass an out-of-the-box Nessus web app
scan without breaking the bundled React UI.

Headers set:
  • Strict-Transport-Security  — only when request arrived over HTTPS or
                                 behind a TLS-terminating proxy that set
                                 X-Forwarded-Proto: https. Setting HSTS
                                 over plain HTTP is a scan finding in
                                 itself.
  • X-Content-Type-Options     — nosniff
  • X-Frame-Options            — DENY (clickjacking)
  • Referrer-Policy            — strict-origin-when-cross-origin
  • Permissions-Policy         — disables camera/mic/geo/payment APIs
  • Content-Security-Policy    — permissive baseline that still passes a
                                 "CSP missing" finding. Operators tightening
                                 for production set FPULSE_CSP to override.
  • Cross-Origin-Opener-Policy — same-origin
  • X-XSS-Protection           — 0 (modern guidance: disable, rely on CSP)

Headers stripped:
  • Server, X-Powered-By — fingerprint reduction.

Env overrides:
  FPULSE_CSP                  — full CSP string; empty disables.
  FPULSE_HSTS_MAX_AGE         — seconds; default 31536000 (1y).
  FPULSE_DISABLE_SECURITY_HEADERS=1 — bypass entirely (debugging only).
"""

from __future__ import annotations

import os


_DEFAULT_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "connect-src 'self' ws: wss:; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)

_DEFAULT_PERMISSIONS_POLICY = (
    "camera=(), microphone=(), geolocation=(), payment=(), usb=(), "
    "accelerometer=(), gyroscope=(), magnetometer=()"
)


class SecurityHeadersMiddleware:
    """ASGI middleware. Wraps the response.start message to inject the
    security headers and strip fingerprint headers."""

    def __init__(self, app):
        self.app = app
        self._enabled = os.environ.get("FPULSE_DISABLE_SECURITY_HEADERS") != "1"
        self._hsts_max_age = os.environ.get("FPULSE_HSTS_MAX_AGE", "31536000")
        self._csp = os.environ.get("FPULSE_CSP", _DEFAULT_CSP)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self._enabled:
            await self.app(scope, receive, send)
            return

        is_https = self._is_https(scope)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = _HeaderList(message.get("headers", []))
                # Strip fingerprinting headers
                headers.remove(b"server")
                headers.remove(b"x-powered-by")
                # Inject (only set if absent — never clobber a route that
                # deliberately sets its own value, e.g. embed iframes)
                headers.set_default(b"x-content-type-options", b"nosniff")
                headers.set_default(b"x-frame-options", b"DENY")
                headers.set_default(
                    b"referrer-policy", b"strict-origin-when-cross-origin"
                )
                headers.set_default(
                    b"permissions-policy",
                    _DEFAULT_PERMISSIONS_POLICY.encode("ascii"),
                )
                headers.set_default(
                    b"cross-origin-opener-policy", b"same-origin"
                )
                headers.set_default(b"x-xss-protection", b"0")
                if self._csp:
                    headers.set_default(
                        b"content-security-policy", self._csp.encode("ascii")
                    )
                if is_https:
                    headers.set_default(
                        b"strict-transport-security",
                        f"max-age={self._hsts_max_age}; includeSubDomains".encode(
                            "ascii"
                        ),
                    )
                # Replace a benign Server identifier so reverse proxies
                # that re-add one downstream still see something neutral.
                headers.set(b"server", b"fpulse")
                message["headers"] = headers.as_list()
            await send(message)

        await self.app(scope, receive, send_wrapper)

    @staticmethod
    def _is_https(scope: dict) -> bool:
        if scope.get("scheme") == "https":
            return True
        # Trust X-Forwarded-Proto from the reverse proxy. Operators
        # putting F-Pulse behind nginx/Caddy/Traefik need this — uvicorn
        # itself sees HTTP.
        for name, value in scope.get("headers", []):
            if name == b"x-forwarded-proto" and value.lower() == b"https":
                return True
        return False


class _HeaderList:
    """Tiny helper around the ASGI list-of-(bytes, bytes) header form."""

    def __init__(self, headers):
        self._items = [(k.lower(), v) for k, v in headers]

    def remove(self, name: bytes) -> None:
        self._items = [(k, v) for k, v in self._items if k != name]

    def set(self, name: bytes, value: bytes) -> None:
        self.remove(name)
        self._items.append((name, value))

    def set_default(self, name: bytes, value: bytes) -> None:
        for k, _ in self._items:
            if k == name:
                return
        self._items.append((name, value))

    def as_list(self):
        return self._items
