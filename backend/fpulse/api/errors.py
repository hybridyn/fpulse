"""Standardized API error helpers — V8/V9 of the F-Pulse product vision.

Adds a single way to build error responses with a consistent shape:

    {
        "detail": "human-readable message",
        "code": "stable_machine_code",   # optional
        "field": "specific_field",       # optional (validation errors)
        "trace_id": "uuid"               # optional (support reference)
    }

Wraps FastAPI's HTTPException so existing exception handlers and OpenAPI
schema generation keep working. Endpoints that haven't migrated yet
continue to use `raise HTTPException(status_code=..., detail="...")`
and that bare `{detail: "..."}` shape is still valid — `api_error` is
additive, not replacing.

Usage:

    from fpulse.api.errors import api_error, ErrorCode

    if not pipeline:
        raise api_error(
            "Pipeline not found.",
            code=ErrorCode.NOT_FOUND,
            status=404,
        )

    if not body.name:
        raise api_error(
            "Pipeline name is required.",
            code=ErrorCode.INVALID_INPUT,
            status=400,
            field="name",
        )

This module is import-safe (no FastAPI app dependency) so it can be
imported by routers, services, and tools alike.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException


class ErrorCode:
    """Stable machine-readable error codes.

    Add new codes here so the catalogue stays discoverable; never
    rename or drop one in a non-breaking release — callers may
    branch on these strings.
    """

    # Generic
    INVALID_INPUT = "invalid_input"
    NOT_FOUND = "not_found"
    PERMISSION_DENIED = "permission_denied"
    CONFLICT = "conflict"
    UNPROCESSABLE = "unprocessable"
    INTERNAL_ERROR = "internal_error"
    UNAVAILABLE = "unavailable"
    RATE_LIMITED = "rate_limited"

    # Auth
    UNAUTHENTICATED = "unauthenticated"
    SESSION_EXPIRED = "session_expired"

    # Plus / licensing
    PLUS_REQUIRED = "plus_required"
    LICENSE_INVALID = "license_invalid"

    # Workspace / RBAC
    WORKSPACE_NOT_FOUND = "workspace_not_found"
    WORKSPACE_REQUIRED = "workspace_required"

    # Pipeline / execution
    PIPELINE_NOT_FOUND = "pipeline_not_found"
    PIPELINE_INVALID = "pipeline_invalid"
    EXECUTION_FAILED = "execution_failed"
    PREFLIGHT_FAILED = "preflight_failed"

    # Connector / credential
    CONNECTOR_NOT_FOUND = "connector_not_found"
    CONNECTION_NOT_FOUND = "connection_not_found"
    CREDENTIAL_NOT_FOUND = "credential_not_found"

    # AI provider
    AI_PROVIDER_UNCONFIGURED = "ai_provider_unconfigured"
    AI_PROVIDER_UNREACHABLE = "ai_provider_unreachable"


def api_error(
    detail: str,
    *,
    status: int = 400,
    code: str | None = None,
    field: str | None = None,
    trace_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> HTTPException:
    """Build an HTTPException with the standardized payload shape.

    Returns an HTTPException so callers raise it as usual:

        raise api_error("Pipeline not found.", status=404, code=ErrorCode.NOT_FOUND)

    The response body becomes:

        {"detail": "Pipeline not found.", "code": "not_found"}

    Fields are only included when non-None / non-empty, so a minimal
    call (`api_error("oops")`) still serializes to plain
    `{"detail": "oops"}` — backward-compatible with the bare-detail
    pattern used by 200+ existing call sites today.

    Args:
        detail: Human-readable message shown to the user. Keep it short
            and actionable; do not leak stack traces or internal IDs.
        status: HTTP status code. Defaults to 400.
        code: Stable machine-readable code from ErrorCode. Recommended
            for any new endpoint so frontend `catch` blocks can branch.
        field: For validation errors, the specific field that failed.
        trace_id: A UUID for support reference; lets users quote a
            short identifier and operators find the matching log line.
        extra: Additional fields to merge into the payload. Use
            sparingly — most cases should be covered by the named args.

    Returns:
        HTTPException ready to be `raise`d.
    """
    payload: dict[str, Any] = {"detail": detail}
    if code:
        payload["code"] = code
    if field:
        payload["field"] = field
    if trace_id:
        payload["trace_id"] = trace_id
    if extra:
        # Caller-supplied extras never overwrite the named fields above.
        for k, v in extra.items():
            if k not in payload:
                payload[k] = v
    return HTTPException(status_code=status, detail=payload)


# ── Convenience constructors for common cases ───────────────────────────────


def not_found(detail: str, *, code: str = ErrorCode.NOT_FOUND, **kw: Any) -> HTTPException:
    """404 with the NOT_FOUND code by default."""
    return api_error(detail, status=404, code=code, **kw)


def invalid_input(
    detail: str,
    *,
    field: str | None = None,
    code: str = ErrorCode.INVALID_INPUT,
    **kw: Any,
) -> HTTPException:
    """400 with the INVALID_INPUT code by default."""
    return api_error(detail, status=400, code=code, field=field, **kw)


def permission_denied(
    detail: str = "You don't have permission to perform this action.",
    *,
    code: str = ErrorCode.PERMISSION_DENIED,
    **kw: Any,
) -> HTTPException:
    """403 with the PERMISSION_DENIED code by default."""
    return api_error(detail, status=403, code=code, **kw)


def plus_required(
    detail: str = "This feature requires F-Pulse+.",
    *,
    code: str = ErrorCode.PLUS_REQUIRED,
    **kw: Any,
) -> HTTPException:
    """402 (Payment Required) — license gating signal."""
    return api_error(detail, status=402, code=code, **kw)


def conflict(detail: str, *, code: str = ErrorCode.CONFLICT, **kw: Any) -> HTTPException:
    """409 with the CONFLICT code by default."""
    return api_error(detail, status=409, code=code, **kw)


__all__ = [
    "ErrorCode",
    "api_error",
    "not_found",
    "invalid_input",
    "permission_denied",
    "plus_required",
    "conflict",
]
