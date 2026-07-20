"""Failure classification taxonomy (2026-06-08, E1 of executor-maturity-1.2).

The executor today records `error_message` (free text) on every failed
run. That's enough for a human reading logs, but it's not enough to
drive automated retry decisions: "should we retry?" depends on
WHETHER the failure is the kind that retry could plausibly fix.

This module ships:
  * ``FailureClass`` enum with the six categories the retry policy
    (E2 of executor-maturity-1.2) will consult
  * ``classify_error(exc_or_message, exception_type=None)`` - the
    central classifier that maps a raised exception (or its message)
    to a FailureClass
  * ``register_classifier(class_name, fn)`` - hook for per-connector
    overrides when the generic regex match isn't precise enough

The retry policy can then declare ``retry_on: ["transient",
"dependency"]`` to skip data-quality and user-input failures (which
won't change between attempts).

# Why a separate module from steward/connector_health.py

Connector-health's `classify_error` (in
``backend/fpulse/steward/connector_health.py``) classifies the holder
of the connection - "is this an auth problem or a network problem?".
That's a narrow connector-focused taxonomy (auth_error / rate_limit /
timeout / unreachable / unknown).

This module's classification is wider - "is this failure retryable in
principle?". Same shape (substring matching), different output set.
Composes cleanly: the executor's retry path consults this; the
Steward connector-health detector consults the narrower one. We do
NOT want to merge them - the questions are different.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Callable, Optional


class FailureClass(str, Enum):
    """Six categories the retry policy + monitoring rollups will
    branch on. The labels are stable - downstream code (and any
    persisted classification field) reads the string value."""

    TRANSIENT     = "transient"      # network blip, 5xx, lock timeout - retry likely fixes
    DEPENDENCY    = "dependency"     # external system unreachable / cred expired / auth - retry might fix if external recovers
    DATA_QUALITY  = "data_quality"   # null in non-null column, schema mismatch - retry won't fix
    USER_INPUT    = "user_input"     # invalid pipeline config - retry won't fix
    FATAL         = "fatal"          # OOM / disk full / code bug - retry won't fix; might make it worse
    UNKNOWN       = "unknown"        # unclassified - retry policy decides defaults


# ── Pattern rules ────────────────────────────────────────────────────


# Ordering is precedence-aware: first match wins. Specific patterns
# go before catch-all ones. Each entry: (compiled regex on lowercased
# message, target FailureClass).
_MESSAGE_RULES: list[tuple[re.Pattern[str], FailureClass]] = [
    # ── FATAL (these never retry) ─────────────────────────────────
    (re.compile(r"\b(out of memory|memoryerror|cannot allocate)\b", re.IGNORECASE), FailureClass.FATAL),
    (re.compile(r"\b(no space left|disk full|enospc)\b", re.IGNORECASE), FailureClass.FATAL),
    (re.compile(r"\b(segmentation fault|stack overflow|nullpointerexception)\b", re.IGNORECASE), FailureClass.FATAL),

    # ── DATA_QUALITY (won't change between attempts) ───────────────
    (re.compile(r"\b(null value in column|not.null violation|cannot insert.+null)\b", re.IGNORECASE), FailureClass.DATA_QUALITY),
    (re.compile(r"\b(unique violation|duplicate key|already exists)\b", re.IGNORECASE), FailureClass.DATA_QUALITY),
    (re.compile(r"\b(check constraint|integrity error|foreign key|fk constraint)\b", re.IGNORECASE), FailureClass.DATA_QUALITY),
    (re.compile(r"\b(invalid (?:input|literal|datetime)|value too long|numeric overflow|cannot cast)\b", re.IGNORECASE), FailureClass.DATA_QUALITY),
    (re.compile(r"\b(schema mismatch|column .* (does ?n[o']t exist|not found)|unknown column)\b", re.IGNORECASE), FailureClass.DATA_QUALITY),

    # ── USER_INPUT (config / spec problems, won't change between attempts) ─
    (re.compile(r"\b(invalid (?:pipeline|workflow|config|configuration|parameter|argument))\b", re.IGNORECASE), FailureClass.USER_INPUT),
    (re.compile(r"\b(missing required (?:field|parameter|argument)|required.+(?:missing|absent))\b", re.IGNORECASE), FailureClass.USER_INPUT),
    (re.compile(r"\b(typeerror|valueerror|keyerror|attributeerror)\b", re.IGNORECASE), FailureClass.USER_INPUT),
    (re.compile(r"\b(no such (?:file|directory)|file not found|filenotfounderror)\b", re.IGNORECASE), FailureClass.USER_INPUT),

    # ── DEPENDENCY (external system; retry might fix if external recovers) ─
    (re.compile(r"\b(connection (?:refused|reset|aborted)|could not connect|unreachable)\b", re.IGNORECASE), FailureClass.DEPENDENCY),
    (re.compile(r"\b(name or service not known|dns|getaddrinfo|host not found|no route to host)\b", re.IGNORECASE), FailureClass.DEPENDENCY),
    # NOTE: trailing \b dropped on 'credential' / 'token' / 'key' so the
    # plural variants ('credentials', 'tokens') still match — word
    # boundary on these is the same false-negative we hit in
    # steward/connector_health.py.
    (re.compile(r"\b(401|403|unauthorised|unauthorized|forbidden|access denied|invalid (?:credential|token)|expired (?:credential|token|key))", re.IGNORECASE), FailureClass.DEPENDENCY),
    (re.compile(r"\b(503|service unavailable|gateway timeout|502|504)\b", re.IGNORECASE), FailureClass.DEPENDENCY),

    # ── TRANSIENT (likely fixed by retry) ─────────────────────────
    (re.compile(r"\b(timeout|timed out|deadline exceeded|operation timed out)", re.IGNORECASE), FailureClass.TRANSIENT),
    # 'throttl' covers throttle / throttled / throttling — trailing \b
    # would reject all but bare 'throttl'.
    (re.compile(r"\b(rate.?limit|429|throttl|too many requests|quota exceeded)", re.IGNORECASE), FailureClass.TRANSIENT),
    (re.compile(r"\b(lock (?:timeout|acquired)|deadlock|lock wait)", re.IGNORECASE), FailureClass.TRANSIENT),
    (re.compile(r"\b(500|internal server error|temporarily unavailable)\b", re.IGNORECASE), FailureClass.TRANSIENT),
    (re.compile(r"\b(network (?:is )?unreachable|broken pipe|connection (?:lost|closed))\b", re.IGNORECASE), FailureClass.TRANSIENT),
]


# Exception-type → FailureClass shortcuts. Faster + more reliable
# than parsing the message when we know the type. Class NAMES (not
# isinstance) so callers can pass exception types from third-party
# libraries without importing them.
_EXCEPTION_TYPE_RULES: dict[str, FailureClass] = {
    "MemoryError":            FailureClass.FATAL,
    "SystemError":            FailureClass.FATAL,
    "RecursionError":         FailureClass.FATAL,

    "ValueError":             FailureClass.USER_INPUT,
    "TypeError":              FailureClass.USER_INPUT,
    "KeyError":               FailureClass.USER_INPUT,
    "AttributeError":         FailureClass.USER_INPUT,
    "FileNotFoundError":      FailureClass.USER_INPUT,
    "NotADirectoryError":     FailureClass.USER_INPUT,
    "PermissionError":        FailureClass.USER_INPUT,

    "ConnectionRefusedError": FailureClass.DEPENDENCY,
    "ConnectionAbortedError": FailureClass.DEPENDENCY,
    "ConnectionResetError":   FailureClass.TRANSIENT,
    "BrokenPipeError":        FailureClass.TRANSIENT,
    "TimeoutError":           FailureClass.TRANSIENT,
}


# Optional per-connector overrides. Connectors can register a more
# precise classifier for their own exception types (e.g. psycopg2's
# OperationalError subclasses, snowflake.connector errors, pyodbc's
# IntegrityError subclasses). Registered functions take the exception
# (or string) and return a FailureClass | None; None means "fall
# through to the generic classifier."
_CONNECTOR_CLASSIFIERS: dict[str, Callable[[object], Optional[FailureClass]]] = {}


def register_classifier(exception_type_name: str,
                          fn: Callable[[object], Optional[FailureClass]]) -> None:
    """Register a per-connector classifier for ``exception_type_name``.
    Called by connector modules at import time.

    Example::

        from psycopg2 import IntegrityError
        from fpulse.engine.failure_class import register_classifier, FailureClass

        def _psyco_classify(exc):
            if isinstance(exc, IntegrityError):
                return FailureClass.DATA_QUALITY
            return None

        register_classifier("IntegrityError", _psyco_classify)
    """
    _CONNECTOR_CLASSIFIERS[exception_type_name] = fn


def classify_error(
    exc_or_message: object,
    *,
    exception_type: str | None = None,
) -> FailureClass:
    """Map a raised exception (or its message text) to a FailureClass.

    Resolution order:
      1. Registered per-connector classifier matching the exception type
         (cheap when classes line up; lets connectors be precise)
      2. Built-in exception-type rule (e.g. MemoryError → FATAL)
      3. Substring rules against the error message
      4. UNKNOWN (default for genuinely-unrecognised failures)

    Arguments:
      exc_or_message - either an Exception instance or the message string
      exception_type - optional override for the type name (use when the
                        caller already has the class name as a string)
    """
    # Resolve the exception-type name + message text
    if isinstance(exc_or_message, BaseException):
        type_name = exception_type or type(exc_or_message).__name__
        message = str(exc_or_message)
    else:
        type_name = exception_type or ""
        message = str(exc_or_message or "")

    # (1) Per-connector classifier on the type name
    if type_name and type_name in _CONNECTOR_CLASSIFIERS:
        result = _CONNECTOR_CLASSIFIERS[type_name](exc_or_message)
        if result is not None:
            return result

    # (2) Built-in exception-type rules
    if type_name and type_name in _EXCEPTION_TYPE_RULES:
        return _EXCEPTION_TYPE_RULES[type_name]

    # (3) Substring rules on the message
    for pattern, cls in _MESSAGE_RULES:
        if pattern.search(message):
            return cls

    # (4) Default
    return FailureClass.UNKNOWN


# ── Rollup helpers (for the "78% of failures were transient" UI) ────


def summarise_failure_classes(failure_classes: list[str]) -> dict[str, int]:
    """Aggregate a list of FailureClass strings into a {class: count}
    dict. Unknown / blank entries roll into UNKNOWN."""
    counts: dict[str, int] = {fc.value: 0 for fc in FailureClass}
    for fc in failure_classes:
        if not fc:
            counts[FailureClass.UNKNOWN.value] += 1
            continue
        counts[fc] = counts.get(fc, 0) + 1
    return counts


def retry_advisable(failure_class: FailureClass | str) -> bool:
    """Default policy hint - "would retry plausibly fix this?". The
    actual retry policy (E2) may override based on user config, but
    this captures the conservative default."""
    fc = FailureClass(failure_class) if isinstance(failure_class, str) else failure_class
    return fc in (FailureClass.TRANSIENT, FailureClass.DEPENDENCY)
