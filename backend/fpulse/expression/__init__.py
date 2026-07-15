"""F-Pulse expression engine — Mustache-style {{ ... }} resolution."""

from .resolver import resolve_expressions, ExpressionError

__all__ = ["resolve_expressions", "ExpressionError"]
