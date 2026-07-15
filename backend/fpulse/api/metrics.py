"""
Stage 6 — Prometheus /api/metrics endpoint (2026-04-19).

Exposes a Prometheus text-format exposition of:
  • HTTP request counters / latency histogram (via middleware)
  • Process RSS / VMS / threads (via psutil, refreshed each scrape)
  • Warmup state (gauge: 0=not_applicable, 1=pending, 2=ok, 3=failed)
  • Feature flag states (one gauge per flag)
  • SQLite WAL pages (gauge)
  • Postgres pool checked-in / checked-out (gauges, when configured)

Design choices:
  • prometheus_client is in the [metrics] OPTIONAL dependency group.
    If it isn't installed we return 501 with a clear remediation
    message — the rest of the API keeps working untouched.
  • We use a single shared registry, not the default global one. This
    avoids polluting `prometheus_client.REGISTRY` with metrics from
    the test suite or repeated module reloads.
  • Process-level metrics (RSS, threads) are computed lazily on each
    /api/metrics call rather than via a background updater. Scrape
    frequency is 30s — the cost of a psutil read is microseconds.
  • Flag and warmup metrics read app_state at scrape time so they
    reflect current state, not a stale snapshot from startup.

Usage from main.py:

    from fpulse.api.metrics import router as metrics_router, METRICS_MIDDLEWARE
    app.add_middleware(METRICS_MIDDLEWARE)   # records request stats
    app.include_router(metrics_router)        # exposes /api/metrics
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response, JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["metrics"])


# ── Lazy dep detection ────────────────────────────────────────────────
def _prometheus_available() -> bool:
    from importlib.util import find_spec
    return find_spec("prometheus_client") is not None


# ── Shared registry + metric objects ───────────────────────────────────
# Construct these at first /api/metrics call (not at import time) so an
# OSS install without prometheus_client never pays the import cost.
_REGISTRY: Any = None
_HTTP_REQUESTS_TOTAL: Any = None
_HTTP_REQUEST_DURATION: Any = None
_PROC_RSS_BYTES: Any = None
_PROC_VMS_BYTES: Any = None
_PROC_THREADS: Any = None
_PROC_UPTIME_SECONDS: Any = None
_WARMUP_STATE: Any = None
_FLAG_STATE: Any = None
_WAL_PAGES: Any = None
_PG_CHECKED_IN: Any = None
_PG_CHECKED_OUT: Any = None
_AUDIT_DUAL_WRITE: Any = None
_LIFECYCLE_DUAL_WRITE: Any = None
_ALERT_LOG_DUAL_WRITE: Any = None
_LIFECYCLE_SHADOW_READ: Any = None
_AUDIT_SHADOW_READ: Any = None
_ALERT_LOG_SHADOW_READ: Any = None
_INITIALISED = False


def _init_metrics() -> bool:
    """Construct the registry + metric objects on first use.

    Returns True on success, False if prometheus_client isn't installed.
    Idempotent — subsequent calls are no-ops.
    """
    global _REGISTRY, _HTTP_REQUESTS_TOTAL, _HTTP_REQUEST_DURATION
    global _PROC_RSS_BYTES, _PROC_VMS_BYTES, _PROC_THREADS, _PROC_UPTIME_SECONDS
    global _WARMUP_STATE, _FLAG_STATE, _WAL_PAGES
    global _PG_CHECKED_IN, _PG_CHECKED_OUT, _AUDIT_DUAL_WRITE
    global _LIFECYCLE_DUAL_WRITE, _ALERT_LOG_DUAL_WRITE
    global _LIFECYCLE_SHADOW_READ, _AUDIT_SHADOW_READ
    global _ALERT_LOG_SHADOW_READ, _INITIALISED

    if _INITIALISED:
        return True
    if not _prometheus_available():
        return False

    from prometheus_client import (
        CollectorRegistry, Counter, Gauge, Histogram,
    )

    _REGISTRY = CollectorRegistry()

    _HTTP_REQUESTS_TOTAL = Counter(
        "fpulse_http_requests_total",
        "Total HTTP requests, labelled by method, route, and status class",
        ["method", "route", "status_class"],
        registry=_REGISTRY,
    )
    _HTTP_REQUEST_DURATION = Histogram(
        "fpulse_http_request_duration_seconds",
        "HTTP request duration in seconds, labelled by method and route",
        ["method", "route"],
        # Buckets tuned for an orchestrator: most requests <100ms,
        # pipeline-related ones can be seconds.
        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
        registry=_REGISTRY,
    )
    _PROC_RSS_BYTES = Gauge(
        "fpulse_process_rss_bytes",
        "Resident set size of the F-Pulse process in bytes",
        registry=_REGISTRY,
    )
    _PROC_VMS_BYTES = Gauge(
        "fpulse_process_vms_bytes",
        "Virtual memory size of the F-Pulse process in bytes",
        registry=_REGISTRY,
    )
    _PROC_THREADS = Gauge(
        "fpulse_process_threads",
        "Live thread count of the F-Pulse process",
        registry=_REGISTRY,
    )
    _PROC_UPTIME_SECONDS = Gauge(
        "fpulse_process_uptime_seconds",
        "Wall-clock seconds since the F-Pulse process started",
        registry=_REGISTRY,
    )
    _WARMUP_STATE = Gauge(
        "fpulse_warmup_state",
        "Background warmup task state: 0=not_applicable, 1=pending, 2=ok, 3=failed",
        registry=_REGISTRY,
    )
    _FLAG_STATE = Gauge(
        "fpulse_feature_flag_enabled",
        "1 if a feature flag is enabled, 0 if disabled",
        ["flag"],
        registry=_REGISTRY,
    )
    _WAL_PAGES = Gauge(
        "fpulse_sqlite_wal_pages",
        "SQLite WAL pages currently waiting for checkpoint",
        registry=_REGISTRY,
    )
    _PG_CHECKED_IN = Gauge(
        "fpulse_pg_pool_checked_in",
        "Postgres pool connections currently idle (checked in)",
        registry=_REGISTRY,
    )
    _PG_CHECKED_OUT = Gauge(
        "fpulse_pg_pool_checked_out",
        "Postgres pool connections currently in use (checked out)",
        registry=_REGISTRY,
    )
    _AUDIT_DUAL_WRITE = Gauge(
        "fpulse_audit_dual_write_total",
        "Cumulative audit log dual-write outcomes by backend and result. "
        "During the Stage 3b validation period an operator should see "
        "sqlite_ok and pg_ok climbing together with similar slopes.",
        ["outcome"],
        registry=_REGISTRY,
    )
    _LIFECYCLE_DUAL_WRITE = Gauge(
        "fpulse_lifecycle_dual_write_total",
        "Cumulative lifecycle_events dual-write outcomes by backend and result. "
        "Same meaning as the audit counter — sqlite_ok and pg_ok should "
        "climb in lockstep during the Stage 3b validation period.",
        ["outcome"],
        registry=_REGISTRY,
    )
    _ALERT_LOG_DUAL_WRITE = Gauge(
        "fpulse_alert_logs_dual_write_total",
        "Cumulative alert_logs dual-write outcomes by backend and result. "
        "Third Stage 3b store — same 5-outcome shape as audit and "
        "lifecycle so operators can compare all three side-by-side.",
        ["outcome"],
        registry=_REGISTRY,
    )
    _LIFECYCLE_SHADOW_READ = Gauge(
        "fpulse_lifecycle_shadow_read_total",
        "Cumulative lifecycle_events shadow-read outcomes. 'match' and "
        "'mismatch' indicate whether PG agrees with SQLite on the same "
        "query. pg_failed/pg_skipped_* are operational signals. During "
        "a healthy PR6 shadow-read period, match should climb and "
        "mismatch should stay at zero.",
        ["outcome"],
        registry=_REGISTRY,
    )
    _AUDIT_SHADOW_READ = Gauge(
        "fpulse_audit_shadow_read_total",
        "Cumulative audit_log shadow-read outcomes. Same 5-outcome shape "
        "as lifecycle so operators can compare stores side-by-side. "
        "Enabled via FPULSE_AUDIT_SHADOW_READS=1.",
        ["outcome"],
        registry=_REGISTRY,
    )
    _ALERT_LOG_SHADOW_READ = Gauge(
        "fpulse_alert_logs_shadow_read_total",
        "Cumulative alert_logs shadow-read outcomes. Third Stage 3b "
        "store — same 5-outcome shape as audit and lifecycle. Enabled "
        "via FPULSE_ALERT_LOGS_SHADOW_READS=1.",
        ["outcome"],
        registry=_REGISTRY,
    )

    _INITIALISED = True
    logger.info("Prometheus metrics registry initialised")
    return True


# ── Middleware that records every HTTP request ─────────────────────────
class PrometheusMetricsMiddleware:
    """ASGI middleware that increments fpulse_http_requests_total and
    observes fpulse_http_request_duration_seconds for every request.

    No-op when prometheus_client isn't installed — the metrics objects
    won't exist, so we just pass through.

    Route label uses request.scope.get("route").path when available
    (template like /api/workflows/{id}) so cardinality stays bounded.
    Falls back to the raw path if no route is matched (404s, etc.).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not _init_metrics():
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        method = scope.get("method", "UNKNOWN")
        # status defaults to 0 in case the response never sends (client
        # disconnect mid-stream). 0 is intentionally outside 2xx-5xx.
        status_holder = {"code": 0}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["code"] = message.get("status", 0)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - start
            route = _extract_route(scope)
            status_class = _status_class(status_holder["code"])
            try:
                _HTTP_REQUESTS_TOTAL.labels(
                    method=method, route=route, status_class=status_class,
                ).inc()
                _HTTP_REQUEST_DURATION.labels(
                    method=method, route=route,
                ).observe(elapsed)
            except Exception as exc:
                # Never let metrics collection take down a request.
                logger.debug("Metrics record failed: %s", exc)


def _extract_route(scope: dict) -> str:
    """Prefer the FastAPI route template (/api/workflows/{id}) over the
    raw path (/api/workflows/abc123) so label cardinality stays bounded.
    Falls back to raw path when no route matched (e.g. 404)."""
    route = scope.get("route")
    if route is not None and hasattr(route, "path"):
        return route.path
    return scope.get("path", "unknown")


def _status_class(code: int) -> str:
    """Group 2xx/3xx/4xx/5xx — keeps label cardinality at 5 instead of
    one label per status code."""
    if code == 0:
        return "client_disconnect"
    return f"{code // 100}xx"


# ── /api/metrics/ai — AI loop counters as JSON ─────────────────────────
@router.get("/metrics/ai")
async def ai_metrics_endpoint(request: Request) -> dict:
    """Per-day AI loop metrics — counters + averages.

    JSON shape so the UI's cost indicator can render directly without
    parsing Prometheus exposition format. The Prometheus side
    (``/api/metrics``) stays focused on HTTP / process gauges; this
    endpoint is the dedicated AI surface (Review #2 tweak, May 17 2026).

    Anonymous reads are allowed — the data is operator-aggregate, not
    per-user, and Free/OSS users typically run this dashboard against
    their own install. Auth-gating would gate them out of their own
    metrics.

    Counters reset at midnight UTC. To persist across resets, export
    this endpoint to a daily JSON cron under
    ``$FPULSE_DATA_DIR/ai_metrics/<date>.json``.
    """
    from fpulse.ai.ai_metrics import get_store
    snap = get_store().get_snapshot()
    return {
        "period_start_utc": snap.period_start_utc,
        "total_requests": snap.total_requests,
        "fallback_hits": snap.fallback_hits,
        "escalations": snap.escalations,
        "per_lane": snap.per_lane,
    }


# ── /api/metrics endpoint ──────────────────────────────────────────────
@router.get("/metrics")
async def metrics_endpoint(request: Request):
    """Prometheus exposition. Returns text/plain on success, JSON 501 if
    prometheus_client isn't installed.

    Refreshes process / warmup / flag / wal / pg gauges on each call
    so the scraped value reflects the moment of the scrape, not a
    background snapshot. psutil reads are microsecond-cheap.
    """
    if not _init_metrics():
        return JSONResponse(
            status_code=501,
            content={
                "detail": "metrics_unavailable",
                "message": "Install with: pip install -e '.[metrics]' (or "
                           "add 'prometheus-client' to requirements.txt) "
                           "and restart.",
            },
        )

    # Refresh dynamic gauges. Read app_state via app.state so we don't
    # depend on import order.
    app_state: dict = getattr(request.app.state, "fpulse_state", None) or {}
    _refresh_gauges(app_state)

    from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
    payload = generate_latest(_REGISTRY)
    return Response(content=payload, media_type=CONTENT_TYPE_LATEST)


def _refresh_gauges(app_state: dict) -> None:
    """Refresh the gauges that depend on live state. Wrapped so a
    failure in one refresh doesn't block the others."""
    # Process
    try:
        import psutil
        proc = psutil.Process()
        mem = proc.memory_info()
        _PROC_RSS_BYTES.set(mem.rss)
        _PROC_VMS_BYTES.set(mem.vms)
        _PROC_THREADS.set(proc.num_threads())
        _PROC_UPTIME_SECONDS.set(time.time() - proc.create_time())
    except Exception as exc:
        logger.debug("Process gauge refresh failed: %s", exc)

    # Warmup
    try:
        status_map = {
            "not_applicable": 0,
            "pending": 1,
            "ok": 2,
            "failed": 3,
        }
        status = app_state.get("warmup_status", "not_applicable")
        _WARMUP_STATE.set(status_map.get(status, 0))
    except Exception as exc:
        logger.debug("Warmup gauge refresh failed: %s", exc)

    # Feature flags
    try:
        from fpulse.feature_flags import snapshot
        for flag, enabled in snapshot().items():
            _FLAG_STATE.labels(flag=flag).set(1 if enabled else 0)
    except Exception as exc:
        logger.debug("Flag gauge refresh failed: %s", exc)

    # SQLite WAL
    try:
        db = app_state.get("db")
        if db is not None and hasattr(db, "wal_stats"):
            stats = db.wal_stats()
            wal_pages = stats.get("wal_pages")
            if wal_pages is not None:
                _WAL_PAGES.set(int(wal_pages))
    except Exception as exc:
        logger.debug("WAL gauge refresh failed: %s", exc)

    # Postgres pool (only when configured)
    try:
        pg = app_state.get("pg")
        if pg is not None and getattr(pg, "_initialised", False):
            engine = getattr(pg, "_engine", None)
            if engine is not None:
                pool = engine.pool
                if hasattr(pool, "checkedin"):
                    _PG_CHECKED_IN.set(pool.checkedin())
                if hasattr(pool, "checkedout"):
                    _PG_CHECKED_OUT.set(pool.checkedout())
    except Exception as exc:
        logger.debug("PG gauge refresh failed: %s", exc)

    # lifecycle_events dual-write counter (only populated when an
    # Same 5-outcome shape as audit so operators can compare the two
    # stores side-by-side in Grafana.
    try:
        from fpulse.ir.lifecycle import get_dual_write_stats as _lc_stats
        for outcome, count in _lc_stats().items():
            _LIFECYCLE_DUAL_WRITE.labels(outcome=outcome).set(count)
    except Exception as exc:
        logger.debug("Lifecycle dual-write gauge refresh failed: %s", exc)

    # Stage 3b third store: alert_logs dual-write counter. Same shape.
    try:
        from fpulse.alerts.store import get_dual_write_stats as _al_stats
        for outcome, count in _al_stats().items():
            _ALERT_LOG_DUAL_WRITE.labels(outcome=outcome).set(count)
    except Exception as exc:
        logger.debug("Alert-logs dual-write gauge refresh failed: %s", exc)

    # PR6: lifecycle_events shadow-read counter. Only populated while
    # FPULSE_LIFECYCLE_SHADOW_READS is enabled; stays at zero otherwise.
    try:
        from fpulse.ir.lifecycle import get_shadow_read_stats
        for outcome, count in get_shadow_read_stats().items():
            _LIFECYCLE_SHADOW_READ.labels(outcome=outcome).set(count)
    except Exception as exc:
        logger.debug("Lifecycle shadow-read gauge refresh failed: %s", exc)

    # alert_logs shadow-read counter (enabled via env var).
    try:
        from fpulse.alerts.store import get_shadow_read_stats as _alert_sr_stats
        for outcome, count in _alert_sr_stats().items():
            _ALERT_LOG_SHADOW_READ.labels(outcome=outcome).set(count)
    except Exception as exc:
        logger.debug("Alert-logs shadow-read gauge refresh failed: %s", exc)
