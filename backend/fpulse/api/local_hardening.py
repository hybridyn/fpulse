"""Local-mode hardening for F-Pulse OSS (2026-06-02).

This module groups three defenses that matter only when the backend is
running on a user's laptop as the "local-first" OSS shape:

  1. **Origin/Referer pinning** — when the backend is bound to loopback,
     refuse cross-origin requests whose `Origin` or `Referer` isn't a
     loopback URL. Defends against DNS-rebinding attacks where a
     malicious page on the public internet resolves a host it controls
     to ``127.0.0.1`` and then makes requests against the local API.

  2. **Loopback-only auth-bypass guard** — any developer convenience
     auth-bypass (FPULSE_DEV_NO_AUTH, etc.) must refuse to engage
     unless the request actually arrives via loopback. Closes the
     "I set dev mode on my laptop, then deployed to a server with the
     same env vars" foot-gun.

  3. **Bind-info hook for the UI banner** — `/api/health/bind-info`
     returns whether the backend is loopback-only or LAN-exposed.
     The frontend renders a sticky warning banner in the latter case
     so the operator can't miss it.

Everything in this module is no-op when not on the OSS-local path,
so importing it has zero cost on a Plus server install.
"""
from __future__ import annotations

import os
import urllib.parse
from typing import Iterable

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp


# Origin patterns that are always treated as local. Browser sends
# ``http://localhost:PORT`` (or 127.0.0.1) on every same-origin XHR; we
# trust both spellings interchangeably.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})


def _origin_host(value: str) -> str | None:
    """Pull the hostname out of an Origin / Referer header value.

    Returns None when the header is missing, malformed, or schemeless.
    Treats both `http://localhost` and `http://localhost:5173` the
    same way — port doesn't matter for the loopback check.
    """
    if not value:
        return None
    try:
        parsed = urllib.parse.urlparse(value)
    except ValueError:
        return None
    return (parsed.hostname or "").lower() or None


def _is_local_request(request: Request) -> bool:
    """True iff the TCP peer for this request is a loopback address.

    Honours forwarded-for headers only when the immediate peer is
    loopback (i.e. behind a trusted local reverse proxy). On a Plus
    server install behind a real LB, `_is_local_request` returning
    False is correct — these defenses are intentionally OSS-only.
    """
    client = request.client
    if client is None:
        return False
    return client.host in {"127.0.0.1", "::1"}


def _backend_bound_loopback_only() -> bool:
    """Inspect the resolved bind host the launcher chose.

    The launcher (`fpulse.main._resolve_bind_host`) writes the final
    bind into ``FPULSE_RESOLVED_BIND_HOST`` so middleware can read
    it without re-doing the env-var dance. Falls back to checking the
    raw env vars when the launcher wasn't used (e.g. uvicorn invoked
    directly with `--host 0.0.0.0`).
    """
    resolved = os.environ.get("FPULSE_RESOLVED_BIND_HOST", "").strip()
    if resolved:
        return resolved == "127.0.0.1" or resolved == "::1"
    # Fallback inference: if FPULSE_ALLOW_LAN is set or FPULSE_BIND_HOST
    # is anything other than loopback, assume LAN-exposed.
    if os.environ.get("FPULSE_ALLOW_LAN", "").strip() in {"1", "true", "yes", "on"}:
        return False
    declared = os.environ.get("FPULSE_BIND_HOST", "127.0.0.1").strip()
    return declared in {"127.0.0.1", "::1", "localhost"}


# ─────────────────────────── Origin pinning ────────────────────────────


class LocalOriginGuardMiddleware(BaseHTTPMiddleware):
    """DNS-rebinding defense for loopback-bound installs.

    Engages only when the backend resolved to a loopback bind. On a
    Plus server install (LAN/public bind), this middleware is a no-op
    — the existing CORS middleware handles legitimate cross-origin
    flows and the hosting environment handles network isolation.

    DNS-rebinding attack (the threat this defends against):
      A malicious page on the public internet points an
      attacker-controlled domain at ``127.0.0.1`` via short-TTL DNS
      manipulation. The user's browser sends a request with
      ``Host: attacker.com`` (not a loopback name) to the IP that
      now resolves to localhost — reaching the local API while the
      browser believes it's same-origin with the attacker's page.

    Two layered defenses:

      1. **Host header allowlist (primary).** Reject any request whose
         ``Host`` header isn't one of the expected loopback hostnames
         or addresses. This is the STRONGER control — browsers always
         send Host, and the attacker can't forge a loopback Host from
         a rebinding-controlled domain. Recommended by every major
         DNS-rebinding write-up (GitHub Security blog 2025, NCC
         Group's Singularity guide, MCP Security advisory series).

      2. **Origin/Referer pinning (secondary).** Reject cross-origin
         requests whose Origin/Referer points outside loopback. This
         catches CSRF-style cross-origin XHRs even when Host happens
         to be loopback (e.g. an attacker page that fetched our HTML
         and then issued requests with the right Host).

    Bypass paths (always allowed even from non-local Origin/Host):
      * `/api/health*` — uptime probes from monitoring tools
      * `/api/metrics` — Prometheus scrape
      * Static asset GETs (no Origin/Host scrutiny needed for direct nav)
    """

    _SAFE_PREFIXES = ("/api/health", "/api/metrics")

    def __init__(self, app: ASGIApp, *, additional_safe_prefixes: Iterable[str] = ()):
        super().__init__(app)
        self._safe_prefixes = tuple(self._SAFE_PREFIXES) + tuple(additional_safe_prefixes)

    @staticmethod
    def _host_header_value(raw: str) -> str:
        """Strip port from a `Host` header value. `localhost:8001` → `localhost`."""
        if not raw:
            return ""
        # Bracketed IPv6: `[::1]:8001`
        if raw.startswith("["):
            close = raw.find("]")
            return raw[1:close].lower() if close > 1 else raw.lower()
        # Plain `host:port` → take the part before colon
        return raw.split(":", 1)[0].lower()

    async def dispatch(self, request: Request, call_next):
        if not _backend_bound_loopback_only():
            return await call_next(request)
        path = request.url.path
        if any(path.startswith(p) for p in self._safe_prefixes):
            return await call_next(request)
        # Static / non-API GETs have no Origin and aren't a CSRF vector
        # against our state-changing API surface.
        method = request.method.upper()
        if method in {"GET", "HEAD", "OPTIONS"} and not path.startswith("/api/"):
            return await call_next(request)

        # ── Primary: Host header allowlist ──
        host_value = self._host_header_value(request.headers.get("host", ""))
        if host_value and host_value not in _LOCAL_HOSTS:
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={
                    "error": "non_loopback_host_blocked",
                    "detail": (
                        f"Host header {host_value!r} is not a loopback "
                        "hostname. F-Pulse is in loopback-only mode and "
                        "rejects requests that pass a non-loopback Host "
                        "header (DNS-rebinding defense). If you intended "
                        "to reach F-Pulse from another machine, set "
                        "FPULSE_BIND_HOST=0.0.0.0 and access via the "
                        "machine's real address."
                    ),
                },
            )

        # ── Secondary: Origin / Referer pinning ──
        origin = request.headers.get("origin") or ""
        referer = request.headers.get("referer") or ""
        origin_host = _origin_host(origin)
        referer_host = _origin_host(referer)

        # Allow same-origin / no-origin (server-to-self, curl tests
        # without Origin) — Host check above is the outer guard.
        if origin_host is None and referer_host is None:
            return await call_next(request)
        if origin_host and origin_host not in _LOCAL_HOSTS:
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={
                    "error": "cross_origin_blocked",
                    "detail": (
                        f"Origin {origin!r} is not allowed when F-Pulse is "
                        "running in loopback-only mode."
                    ),
                },
            )
        if referer_host and referer_host not in _LOCAL_HOSTS:
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={
                    "error": "cross_origin_blocked",
                    "detail": f"Referer {referer!r} is not a loopback URL.",
                },
            )
        return await call_next(request)


# ─────────────────────────── Bind-info endpoint ────────────────────────


router = APIRouter()


@router.get("/health/bind-info")
def bind_info() -> dict:
    """Surface the bind / hardening state to the frontend.

    The UI reads this on first render. If `loopback_only` is False,
    a sticky warning banner appears at the top of every page reading
    "F-Pulse is exposed on your local network — anyone on this WiFi
    can hit the API. Set FPULSE_BIND_HOST=127.0.0.1 to fix."
    """
    bound = _backend_bound_loopback_only()
    return {
        "bind_host": os.environ.get(
            "FPULSE_RESOLVED_BIND_HOST",
            os.environ.get("FPULSE_BIND_HOST", "127.0.0.1"),
        ),
        "loopback_only": bound,
        "allow_lan_flag": bool(
            os.environ.get("FPULSE_ALLOW_LAN", "").strip() in {"1", "true", "yes", "on"}
        ),
        "warning": None if bound else (
            "F-Pulse is bound to a non-loopback interface — the API is "
            "reachable from your local network. Set FPULSE_BIND_HOST="
            "127.0.0.1 (or unset FPULSE_ALLOW_LAN) to restrict to "
            "loopback only."
        ),
    }


# ─────────────────────────── Graceful shutdown ─────────────────────────


@router.post("/system/shutdown")
def graceful_shutdown(request: Request) -> dict:
    """Signal the backend to terminate cleanly.

    2026-06-02: addresses the launcher-orphan-process gotcha. Without
    this endpoint, closing the browser tab leaves the backend running
    indefinitely; the next ``fpulse open`` then crashes with
    EADDRINUSE on port 8001.

    The frontend's BindWarningBanner / App-level `beforeunload` hook
    fires this as a sendBeacon when the last F-Pulse tab closes.

    Safety:
      - Loopback-only: refuses non-127.0.0.1 callers. A LAN-exposed
        server should NOT be killed by anyone who can hit the API;
        the server operator is responsible for lifecycle there.
      - LAN-bound installs (Plus, on-prem): this endpoint is a no-op
        that returns 403. Operators kill via systemd / launchd /
        Windows Service Manager, not via HTTP.

    Mechanism: schedule a SIGTERM-equivalent that fires AFTER the
    current request finishes (so the 200 OK reaches the browser).
    Uvicorn's signal handler runs the usual shutdown lifecycle:
    in-flight requests complete (with a short timeout), connections
    drain, database handles close cleanly.
    """
    # ── Security boundaries FIRST ──
    # These are hard limits that must hold regardless of the tab-close gate
    # below: a non-loopback caller, or a LAN-bound install, can never trigger
    # an HTTP shutdown. (Previously the opt-in gate ran first and returned a
    # 200 no-op before these checks — so a LAN-bound install answered 200
    # instead of 403, depending on env state.)
    if not _is_local_request(request):
        from fastapi import HTTPException
        raise HTTPException(
            status_code=403,
            detail="shutdown refused — endpoint is loopback-only",
        )
    if not _backend_bound_loopback_only():
        from fastapi import HTTPException
        raise HTTPException(
            status_code=403,
            detail="shutdown refused — backend is not in loopback-only mode "
                   "(LAN-bound installs must be stopped via service manager)",
        )

    # ── Opt-in gate (after the security checks) ──
    # The tab-close auto-shutdown is a convenience for the ephemeral
    # `fpulse open` dev launcher (its process IS the server and should die with
    # the window). The always-on SERVICE must NEVER be killable this way —
    # otherwise closing the app window would stop 24/7 scheduling and any
    # in-flight runs. `fpulse open` sets FPULSE_ALLOW_TAB_SHUTDOWN=1; the
    # service does not, so here it's a deliberate no-op.
    import os as _os
    if _os.environ.get("FPULSE_ALLOW_TAB_SHUTDOWN", "").strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        return {
            "ok": True,
            "shutting_down": False,
            "reason": "tab-close shutdown disabled in service mode; "
                      "stop via the service manager or `fpulse uninstall-service`",
        }

    # Schedule SIGINT (Ctrl+C equivalent) on the main thread shortly
    # after this response goes out. Using a background thread + small
    # delay so the JSON response can flush before the loop exits.
    import threading
    import signal
    import os as _os
    import time as _time

    def _delayed_shutdown():
        # Brief delay lets uvicorn finish writing the 200 OK + lets
        # any beforeunload beacon batch land before we tear down.
        _time.sleep(0.25)
        try:
            _os.kill(_os.getpid(), signal.SIGINT)
        except Exception:
            # SIGINT on Windows in some configurations falls back to
            # os._exit. Last-resort hard exit is acceptable — the
            # operator asked us to shut down.
            _os._exit(0)

    threading.Thread(target=_delayed_shutdown, daemon=True).start()
    return {"ok": True, "shutting_down": True}


# ─────────────────────────── Dev-auth bypass guard ─────────────────────


def assert_dev_auth_local_only(request: Request) -> None:
    """Refuse to honour an auth-bypass unless the request is loopback.

    Call this from the auth dependency BEFORE accepting any dev-mode
    convenience header / env-var bypass. If the bypass code is shipped
    accidentally and someone deploys F-Pulse to a server with
    FPULSE_DEV_NO_AUTH=1, this guard turns the bypass into a hard
    failure rather than a silent open door.
    """
    if not _is_local_request(request):
        # Fail closed — a misconfigured server should not silently allow
        # anonymous access. The error message is intentionally vague to
        # avoid revealing the bypass mechanism's name in a 403.
        from fastapi import HTTPException
        raise HTTPException(
            status_code=403,
            detail="dev auth bypass refused for non-loopback caller",
        )
