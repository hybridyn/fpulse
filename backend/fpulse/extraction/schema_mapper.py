"""JSON path navigation + deep→flat schema mapping with type coercion.

The extraction engine reads `SchemaProfile.field_paths` (output column
→ JSON path) and uses this module to project deeply nested API
responses into flat row dicts ready for bulk-loading.

Path syntax:

    "a.b.c"             — nested key access
    "a.b[0]"            — first list element
    "a.b[3]"            — fourth list element
    "a.b[*]"            — wildcard: returns list of values for all
                           elements (further path segments are applied
                           to each element)
    "a.b|default=null"  — fallback when any segment is missing
    "a.b|default=foo"   — fallback to literal string

Type coercions supported by `coerce_value`:

    "int" | "float" | "bool" | "str" | "iso_datetime" | "lower" | "upper"

The `iso_datetime` coercion accepts ISO 8601 strings and returns a
timezone-aware `datetime` (or the original value if unparseable —
the engine logs and continues; we don't drop rows on bad timestamps).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# ── Path navigation ─────────────────────────────────────────────────

_SEGMENT_RE = re.compile(r"""
    (?:
        (?P<key>[^.\[\]]+)            # bare key
    )
    (?:                               # optional indexers
        \[(?P<idx>\d+|\*)\]
    )*
""", re.VERBOSE)

_SENTINEL = object()


def _split_path(path: str) -> list[tuple[str, list[str]]]:
    """Tokenise 'a.b[0].c[*].d' → [('a',[]), ('b',['0']), ('c',['*']), ('d',[])].

    Each tuple is (key, list-of-indexers). The indexer list lets us
    handle 'a[0][*]' (rare but real — nested arrays).
    """
    segments: list[tuple[str, list[str]]] = []
    for raw in path.split("."):
        # peel indexers off the tail
        idxs: list[str] = []
        m = re.match(r"^([^\[]+)((?:\[[^\]]+\])*)$", raw)
        if not m:
            segments.append((raw, []))
            continue
        key = m.group(1)
        for idx in re.findall(r"\[([^\]]+)\]", m.group(2) or ""):
            idxs.append(idx)
        segments.append((key, idxs))
    return segments


def _apply_indexers(value: Any, indexers: list[str], remaining_segments: list[tuple[str, list[str]]]) -> Any:
    """Apply a list of [N] / [*] indexers in sequence. When [*] is
    encountered we fan out the rest of the path across each element."""
    cursor = value
    for i, idx in enumerate(indexers):
        if cursor is None:
            return None
        if not isinstance(cursor, list):
            return None
        if idx == "*":
            # Wildcard: apply remaining indexers + remaining segments
            # to every list element, return list of results.
            rest_idx = indexers[i + 1:]
            return [
                _navigate_after_wildcard(elem, rest_idx, remaining_segments)
                for elem in cursor
            ]
        try:
            n = int(idx)
        except ValueError:
            return None
        if n < 0 or n >= len(cursor):
            return None
        cursor = cursor[n]
    return cursor


def _navigate_after_wildcard(elem: Any, remaining_indexers: list[str],
                              remaining_segments: list[tuple[str, list[str]]]) -> Any:
    """Continue navigating from inside a wildcard expansion."""
    cursor = _apply_indexers(elem, remaining_indexers, remaining_segments)
    return _walk_segments(cursor, remaining_segments)


def _walk_segments(cursor: Any, segments: list[tuple[str, list[str]]]) -> Any:
    for i, (key, idxs) in enumerate(segments):
        if cursor is None:
            return None
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(key, _SENTINEL)
        if cursor is _SENTINEL:
            return None
        if idxs:
            cursor = _apply_indexers(cursor, idxs, segments[i + 1:])
            if isinstance(cursor, list) and "*" in idxs:
                # Wildcard already short-circuited the rest of the walk.
                return cursor
    return cursor


def get_json_path(record: Any, path: str) -> Any:
    """Navigate a JSON path. Returns None if any segment is missing.

    Supports dotted keys, [N] index, [*] wildcard, and the
    `|default=...` fallback suffix.
    """
    if "|default=" in path:
        path, default_part = path.split("|default=", 1)
        default = _parse_default(default_part)
    else:
        default = None
    segments = _split_path(path)
    value = _walk_segments(record, segments)
    return value if value is not None else default


def _parse_default(token: str) -> Any:
    if token == "null":
        return None
    if token in ("true", "false"):
        return token == "true"
    if token.lstrip("-").isdigit():
        return int(token)
    try:
        return float(token)
    except ValueError:
        return token  # treat as string literal


# ── Type coercions ──────────────────────────────────────────────────

def _coerce_bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "1", "yes", "y", "t"):
            return True
        if s in ("false", "0", "no", "n", "f", ""):
            return False
    return None


def _coerce_iso_datetime(v: Any) -> Any:
    if v is None or isinstance(v, datetime):
        return v
    if not isinstance(v, str):
        return v
    # Tolerate trailing Z (Python <3.11 fromisoformat doesn't accept it).
    s = v.replace("Z", "+00:00") if v.endswith("Z") else v
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return v  # leave as-is; engine logs once
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def coerce_value(value: Any, kind: str) -> Any:
    """Apply a type coercion. Returns the original value (or None) when
    the coercion can't be applied — never raises."""
    if value is None:
        return None
    try:
        if kind == "int":
            return int(value) if not isinstance(value, bool) else int(bool(value))
        if kind == "float":
            return float(value)
        if kind == "bool":
            return _coerce_bool(value)
        if kind == "str":
            return str(value)
        if kind == "iso_datetime":
            return _coerce_iso_datetime(value)
        if kind == "lower":
            return str(value).lower()
        if kind == "upper":
            return str(value).upper()
    except (TypeError, ValueError):
        return value
    return value


# ── Mapper ───────────────────────────────────────────────────────────

class SchemaMapper:
    """Project a deeply-nested record into a flat row using a SchemaProfile.

    Usage:
        mapper = SchemaMapper(profile.schema)
        flat = mapper.flatten(api_response_record)

    Returns a dict[str, Any] keyed by SchemaProfile.field_paths keys.
    """

    def __init__(self, profile) -> None:  # type: SchemaProfile
        self._field_paths = dict(profile.field_paths)
        self._coercions = dict(profile.coercions)

    def flatten(self, record: Any) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for column, path in self._field_paths.items():
            value = get_json_path(record, path)
            kind = self._coercions.get(column)
            if kind:
                value = coerce_value(value, kind)
            out[column] = value
        return out

    def flatten_many(self, records) -> list[dict[str, Any]]:
        return [self.flatten(r) for r in records]
