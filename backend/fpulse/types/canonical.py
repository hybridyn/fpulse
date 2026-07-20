"""Canonical schema contract — the platform-wide type system above DuckDB.

Every source connector emits a ``CanonicalSchema`` alongside its DuckDB
relation; every sink connector consumes one to plan its writes. Drift
detection, cast safety, mapping-tab glyphs, and (later) semantic /
governance layers all read from this single record.

The split between ``FPType``, ``params``, and ``Provenance`` is the
key design choice:

  - ``FPType``      is the LOGICAL kind (integer, decimal, …).
                    Pipelines reason about kinds, not engine-specific
                    physical types. Portable across execution engines.
  - ``params``      hold the PARAMETERIZED metadata (precision/scale
                    for decimal, length for string, timezone for
                    timestamp, element_type for list, key_type +
                    value_type for map). Drives DDL emission on the
                    sink side and narrowing-detection on cast.
  - ``Provenance``  records HOW this type was resolved — DB
                    advertised, JSON sample inferred, user manual,
                    or upstream coerced. Surfaces in the
                    "why was this column cast to VARCHAR?" answer
                    F-Pulse will need for explainability.

Nested types (``STRUCT``, ``LIST``, ``MAP``) keep their child schema
in ``params``. Recursion is bounded by user-imposed config; the
helpers below treat any depth uniformly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class FPType(Enum):
    """Logical type kind — engine-independent.

    Parameterized metadata (precision, length, timezone, etc.) lives
    in ``FPField.params`` so the enum stays small and stable. New
    kinds get added here only when no existing kind fits semantically.
    """

    INTEGER = "integer"
    DECIMAL = "decimal"
    FLOAT = "float"
    STRING = "string"
    BOOLEAN = "boolean"
    DATE = "date"
    TIME = "time"
    TIMESTAMP = "timestamp"
    BINARY = "binary"
    JSON = "json"          # opaque JSON document (shape unspecified)
    STRUCT = "struct"      # nested record with named child fields
    LIST = "list"          # array of `element_type` (in params)
    MAP = "map"             # key_type → value_type (in params)
    UNKNOWN = "unknown"    # honest fallback when type can't be resolved


class Evidence(Enum):
    """How a type assignment was reached.

    Drives the Mapping-tab confidence chip and the eventual
    "why this type?" answer in the explainability surface.
    """

    ADVERTISED = "advertised"    # source schema told us (DB, Parquet, Avro)
    INFERRED = "inferred"        # sampled (JSON, CSV, REST)
    MANUAL = "manual"            # user-declared override
    COERCED = "coerced"          # cast from a different upstream type


@dataclass
class Provenance:
    """One record of how a type was resolved.

    A column can have multiple provenance entries when the type was
    re-resolved across pipeline stages (e.g. JSON inferred → user
    override → downstream coerce). The list is append-only so the
    UI can render the resolution timeline for debugging.
    """

    source: str                              # "Oracle NUMBER(18,2)" / "CSV sample(10000)"
    confidence: float = 1.0
    sample_size: int = 0
    conflicts: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class FPField:
    """One named column in a ``CanonicalSchema``.

    Composes the logical kind (``type``), parameterized metadata
    (``params``), nullability, evidence + provenance, and (for
    nested types) the child schema.
    """

    name: str
    type: FPType
    nullable: bool = True
    params: dict[str, Any] = field(default_factory=dict)
    # Child schema for STRUCT (dict[name, FPField]).
    # For LIST / MAP the child shape lives in `params` (`element_type`,
    # `key_type`, `value_type`) so STRUCT is the only kind that uses
    # this slot. Kept named `fields` for clarity at the call site.
    fields: dict[str, "FPField"] | None = None
    evidence: Evidence = Evidence.ADVERTISED
    confidence: float = 1.0
    provenance: list[Provenance] = field(default_factory=list)
    native_raw: str | None = None             # "VARCHAR2(255)" / "TIMESTAMP WITH TIME ZONE"

    # ── Path traversal for nested STRUCT shapes ──
    # Reviewers asked for path-addressable nested schemas
    # (`customer.address.city`) — both for diffing and for the
    # mapping UI rendering subordinate rows.

    def at_path(self, path: str) -> "FPField | None":
        """Resolve a dotted path against nested STRUCT fields.

        Returns the leaf ``FPField`` or ``None`` if any segment of the
        path is missing or hits a non-STRUCT node.
        """
        if not path:
            return self
        parts = path.split(".")
        node: FPField | None = self
        for part in parts:
            if node is None or node.type != FPType.STRUCT or node.fields is None:
                return None
            node = node.fields.get(part)
        return node

    def iter_paths(self, prefix: str = "") -> "list[tuple[str, FPField]]":
        """Flat (path, field) listing for diff / mapping rendering.

        Recurses into STRUCT fields; leaves LIST / MAP element schemas
        addressed by ``params`` so iteration stays cycle-free.
        """
        here = f"{prefix}.{self.name}" if prefix else self.name
        out: list[tuple[str, FPField]] = [(here, self)]
        if self.type == FPType.STRUCT and self.fields:
            for child in self.fields.values():
                out.extend(child.iter_paths(here))
        return out

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe representation for snapshots + drift comparison."""
        return {
            "name": self.name,
            "type": self.type.value,
            "nullable": self.nullable,
            "params": _serialise_params(self.params),
            "fields": (
                {n: f.to_dict() for n, f in self.fields.items()}
                if self.fields else None
            ),
            "evidence": self.evidence.value,
            "confidence": self.confidence,
            "provenance": [
                {
                    "source": p.source, "confidence": p.confidence,
                    "sample_size": p.sample_size, "conflicts": p.conflicts,
                }
                for p in self.provenance
            ],
            "native_raw": self.native_raw,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FPField":
        return cls(
            name=data["name"],
            type=FPType(data["type"]),
            nullable=data.get("nullable", True),
            params=_deserialise_params(data.get("params") or {}),
            fields=(
                {n: cls.from_dict(v) for n, v in data["fields"].items()}
                if data.get("fields") else None
            ),
            evidence=Evidence(data.get("evidence", "advertised")),
            confidence=data.get("confidence", 1.0),
            provenance=[
                Provenance(
                    source=p["source"],
                    confidence=p.get("confidence", 1.0),
                    sample_size=p.get("sample_size", 0),
                    conflicts=p.get("conflicts", []) or [],
                )
                for p in (data.get("provenance") or [])
            ],
            native_raw=data.get("native_raw"),
        )


@dataclass
class CanonicalSchema:
    """The contract every source emits and every sink consumes.

    Ordered list of fields (column order matters for tabular DDL).
    Lookup by name is O(N) but N is small in practice; if it becomes
    a hot path we'll add a cached name→index map.
    """

    fields: list[FPField] = field(default_factory=list)

    @property
    def names(self) -> list[str]:
        return [f.name for f in self.fields]

    def by_name(self, name: str) -> FPField | None:
        for f in self.fields:
            if f.name == name:
                return f
        return None

    def iter_paths(self) -> list[tuple[str, FPField]]:
        """All (path, field) pairs across all columns, recursing into STRUCTs."""
        out: list[tuple[str, FPField]] = []
        for f in self.fields:
            out.extend(f.iter_paths())
        return out

    def to_dict(self) -> dict[str, Any]:
        return {"fields": [f.to_dict() for f in self.fields]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CanonicalSchema":
        return cls(fields=[FPField.from_dict(f) for f in data.get("fields", [])])


# ── Internal helpers ──

def _serialise_params(p: dict[str, Any]) -> dict[str, Any]:
    """Convert FPField values inside params (element_type, key_type, value_type)
    to their dict form so the whole params blob is JSON-safe."""
    out: dict[str, Any] = {}
    for k, v in p.items():
        if isinstance(v, FPField):
            out[k] = v.to_dict()
        else:
            out[k] = v
    return out


def _deserialise_params(p: dict[str, Any]) -> dict[str, Any]:
    """Inverse of ``_serialise_params``. Recognises ``element_type``,
    ``key_type``, ``value_type`` slots that carry nested FPFields."""
    out: dict[str, Any] = {}
    nested_slots = {"element_type", "key_type", "value_type"}
    for k, v in p.items():
        if k in nested_slots and isinstance(v, dict) and "type" in v:
            out[k] = FPField.from_dict(v)
        else:
            out[k] = v
    return out
