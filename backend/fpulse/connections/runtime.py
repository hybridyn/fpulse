"""Runtime helpers for local-network / air-gapped deployments.

Three concerns this module exists to solve:

  1. Per-connection TLS verification toggle — internal services with
     self-signed certs can't be browsed/extracted unless the operator
     opts in to disabling verification for that specific connection.

  2. Air-gapped mode — when `FPULSE_AIR_GAPPED=1`, every outbound
     fallback path (telemetry, license check, OpenRouter price feed,
     etc.) short-circuits silently. The UI surfaces a badge so
     operators know they're running offline-safe.

  3. Reachability preflight — a small synchronous probe the engine
     can call before kicking off a long-running run, so unreachable
     sources fail in seconds instead of consuming 5+ minutes of AIMD
     failure budget.

Each helper is intentionally tiny so callers can use it without
pulling in extra deps.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from urllib.parse import urlparse


# ── TLS verification ────────────────────────────────────────────────

def _bool_env(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return default


def resolve_verify_ssl(config: dict | None) -> bool:
    """Return whether to verify TLS for this connection.

    Precedence:
      1. Per-connection `config["verify_ssl"]` if explicitly set
      2. `FPULSE_VERIFY_SSL` env var (defaults true)
      3. Default true

    Use the per-connection override only for self-signed internal
    services. Disabling globally via env is a development-only escape
    hatch — production deployments should fix the trust store instead.
    """
    if config is not None and "verify_ssl" in config:
        return bool(config["verify_ssl"])
    return _bool_env("FPULSE_VERIFY_SSL", default=True)


# ── Air-gapped mode ─────────────────────────────────────────────────

def is_air_gapped() -> bool:
    """True when `FPULSE_AIR_GAPPED=1` (or true/yes/on) is set.

    When true, callers MUST short-circuit any outbound HTTP they're
    about to make to a non-source host (telemetry, license check,
    public price feeds, etc.). Source-data calls — what the user
    actually configured connections for — still go out; that's the
    whole point of the deployment.
    """
    return _bool_env("FPULSE_AIR_GAPPED", default=False)


# ── Reachability preflight ──────────────────────────────────────────

@dataclass
class ReachabilityResult:
    reachable: bool
    target: str
    detail: str
    latency_ms: float | None


def check_reachability(url: str, *, timeout_s: float = 5.0) -> ReachabilityResult:
    """Quick TCP-level probe of the host:port a URL points to.

    Doesn't speak HTTP — just opens a socket. That's enough to detect
    'connection refused', 'host not found', 'firewall dropping
    packets', and 'wrong port' before we burn into a long-running
    extraction job. Returns within `timeout_s` either way.
    """
    import time
    parsed = urlparse(url if "://" in url else "http://" + url)
    host = parsed.hostname
    if not host:
        return ReachabilityResult(False, url, "no host in url", None)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    target = f"{host}:{port}"
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            latency = round((time.monotonic() - start) * 1000, 1)
            return ReachabilityResult(True, target, "tcp connect ok", latency)
    except socket.gaierror as exc:
        return ReachabilityResult(False, target, f"dns: {exc}", None)
    except (TimeoutError, socket.timeout):
        return ReachabilityResult(False, target,
                                     f"timeout after {timeout_s}s", None)
    except OSError as exc:
        return ReachabilityResult(False, target, f"{type(exc).__name__}: {exc}", None)


# ── Combined runtime status (for /api/system/runtime) ──────────────

def runtime_status() -> dict:
    return {
        "air_gapped": is_air_gapped(),
        "verify_ssl_default": _bool_env("FPULSE_VERIFY_SSL", default=True),
    }
