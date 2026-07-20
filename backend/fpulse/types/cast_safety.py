"""Source → sink cast classifier.

Drives the per-row ``✓ / ⚠ / ✕`` glyph on the Mapping tab and the
preflight compatibility check on the write path.

Four-level taxonomy (from reviewer 2's expanded model):

  - ``SAFE``            lossless; original value recoverable from the target.
                         INT → BIGINT, VARCHAR(50) → VARCHAR(255), DECIMAL → DECIMAL
                         (target ≥ source precision+scale).
  - ``SEMANTIC_LOSSY``  bytes fit but business meaning narrows.
                         TIMESTAMP WITH TZ → TIMESTAMP (timezone dropped).
                         JSON → STRING (parseability lost).
  - ``LOSSY``           potential value-level loss / truncation.
                         DECIMAL(18,4) → DECIMAL(10,2), BIGINT → INT,
                         VARCHAR(500) → VARCHAR(255).
  - ``IMPOSSIBLE``      no valid cast even in principle.
                         BLOB → DATE, STRUCT → INT.

Used both by the Mapping tab (glyph rendering) and by the sink-side
preflight (which raises when ``cast_policy="safe"`` and any
classification > SAFE).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from fpulse.types.canonical import FPField, FPType


class CastSafety(Enum):
    SAFE = "safe"
    SEMANTIC_LOSSY = "semantic_lossy"
    LOSSY = "lossy"
    IMPOSSIBLE = "impossible"


@dataclass
class CastPlanElement:
    """One row of the sink's write plan, built from a ``CanonicalSchema``."""

    source_column: str
    target_column: str
    target_native_type: str         # DDL fragment ("DECIMAL(10,2)", "NVARCHAR(MAX)")
    safety: CastSafety
    reason: str | None = None       # human-readable explanation when not SAFE


# ── Classifier ──

def classify_cast(source: FPField, target: FPField) -> tuple[CastSafety, str | None]:
    """Classify a source→target cast.

    Returns ``(safety, reason)`` where ``reason`` is a one-line
    explanation when safety is not SAFE. Examines parameterized
    metadata (precision/scale/length/timezone) to detect narrowing.
    """
    # Identity → SAFE (params still need a narrowing check below).
    if source.type == target.type:
        return _classify_same_kind(source, target)

    # Numeric widening/narrowing matrix.
    if source.type == FPType.INTEGER and target.type == FPType.DECIMAL:
        return CastSafety.SAFE, None
    if source.type == FPType.DECIMAL and target.type == FPType.INTEGER:
        return CastSafety.LOSSY, "fractional part truncated"
    if source.type == FPType.INTEGER and target.type == FPType.FLOAT:
        return CastSafety.LOSSY, "precision loss for integers > 2^53"
    if source.type == FPType.FLOAT and target.type == FPType.INTEGER:
        return CastSafety.LOSSY, "fractional part truncated"
    if source.type == FPType.FLOAT and target.type == FPType.DECIMAL:
        return CastSafety.LOSSY, "float→decimal can drift in low-order digits"
    if source.type == FPType.DECIMAL and target.type == FPType.FLOAT:
        return CastSafety.LOSSY, "decimal precision narrows to float mantissa"

    # Date/time pairings.
    if source.type == FPType.DATE and target.type == FPType.TIMESTAMP:
        return CastSafety.SAFE, None
    if source.type == FPType.TIMESTAMP and target.type == FPType.DATE:
        return CastSafety.LOSSY, "time component dropped"
    if source.type == FPType.TIME and target.type == FPType.TIMESTAMP:
        return CastSafety.LOSSY, "date component must be synthesized"
    if source.type == FPType.TIMESTAMP and target.type == FPType.TIME:
        return CastSafety.LOSSY, "date component dropped"

    # Everything-to-string is structurally compatible but semantically thin.
    if target.type == FPType.STRING:
        if source.type in (FPType.JSON, FPType.STRUCT, FPType.LIST, FPType.MAP):
            return CastSafety.SEMANTIC_LOSSY, (
                "serialized as string — parseability + nested addressing lost"
            )
        if source.type == FPType.BINARY:
            return CastSafety.LOSSY, "binary→string is encoding-dependent"
        return CastSafety.SEMANTIC_LOSSY, (
            "downcast to string discards type-level constraints"
        )

    # String → typed: always lossy (parse may fail at runtime).
    if source.type == FPType.STRING:
        if target.type in (FPType.INTEGER, FPType.FLOAT, FPType.DECIMAL):
            return CastSafety.LOSSY, "string→numeric parses at runtime; invalid values null"
        if target.type in (FPType.DATE, FPType.TIME, FPType.TIMESTAMP):
            return CastSafety.LOSSY, "string→temporal parses at runtime; format-dependent"
        if target.type == FPType.BOOLEAN:
            return CastSafety.LOSSY, "string→boolean depends on accepted truthy/falsy values"

    # JSON ↔ structured (STRUCT/LIST/MAP). Compatible storage but the
    # canonical shape lookup at the receiver depends on the engine.
    if source.type == FPType.JSON and target.type in (FPType.STRUCT, FPType.LIST, FPType.MAP):
        return CastSafety.SEMANTIC_LOSSY, "engine-specific JSON→typed mapping"
    if source.type in (FPType.STRUCT, FPType.LIST, FPType.MAP) and target.type == FPType.JSON:
        return CastSafety.SEMANTIC_LOSSY, "typed structure flattens to opaque JSON"

    # UNKNOWN ↔ anything: defer to coerce-at-runtime.
    if source.type == FPType.UNKNOWN or target.type == FPType.UNKNOWN:
        return CastSafety.LOSSY, "type unresolved at planning time"

    return CastSafety.IMPOSSIBLE, f"no defined cast from {source.type.value} to {target.type.value}"


def _classify_same_kind(source: FPField, target: FPField) -> tuple[CastSafety, str | None]:
    """Same logical kind — params decide whether it's a narrowing."""
    if source.type == FPType.DECIMAL:
        sp, ss = int(source.params.get("precision", 38)), int(source.params.get("scale", 0))
        tp, ts = int(target.params.get("precision", 38)), int(target.params.get("scale", 0))
        if tp >= sp and ts >= ss:
            return CastSafety.SAFE, None
        return CastSafety.LOSSY, f"decimal narrows {sp},{ss} → {tp},{ts}"

    if source.type == FPType.STRING:
        sl = source.params.get("length")
        tl = target.params.get("length")
        if tl is None or sl is None or int(tl) >= int(sl):
            return CastSafety.SAFE, None
        return CastSafety.LOSSY, f"string length narrows {sl} → {tl}"

    if source.type == FPType.TIMESTAMP or source.type == FPType.TIME:
        # 2026-05-22 bug fix: this branch previously read params.get("timezone")
        # but every mapper (from_postgres, future from_mssql/oracle/mysql) writes
        # params["with_timezone"]. The result was a SILENT no-op — every
        # tz-bearing → tz-naked cast scored SAFE instead of SEMANTIC_LOSSY.
        # `timezone` is kept as a fallback alias so any callers that happened
        # to write the legacy key still work, but the canonical key is
        # `with_timezone`. See test_canonical_keys_contract for the pin.
        def _tz(field) -> bool:
            return bool(field.params.get("with_timezone") or field.params.get("timezone"))
        s_tz = _tz(source)
        t_tz = _tz(target)
        if s_tz and not t_tz:
            return CastSafety.SEMANTIC_LOSSY, "timezone dropped"
        if not s_tz and t_tz:
            return CastSafety.SEMANTIC_LOSSY, "timezone synthesized from local"
        return CastSafety.SAFE, None

    if source.type == FPType.INTEGER:
        # Integer "width" lives in params.bits when known.
        sb = source.params.get("bits")
        tb = target.params.get("bits")
        if sb is None or tb is None or int(tb) >= int(sb):
            return CastSafety.SAFE, None
        return CastSafety.LOSSY, f"integer narrows {sb}b → {tb}b"

    if source.type == FPType.LIST:
        # Recurse into element_type.
        s_el = source.params.get("element_type")
        t_el = target.params.get("element_type")
        if isinstance(s_el, FPField) and isinstance(t_el, FPField):
            return classify_cast(s_el, t_el)
        return CastSafety.SAFE, None

    if source.type == FPType.MAP:
        # Both key and value must be SAFE for the map to be SAFE.
        s_k, t_k = source.params.get("key_type"), target.params.get("key_type")
        s_v, t_v = source.params.get("value_type"), target.params.get("value_type")
        worst = CastSafety.SAFE
        reasons: list[str] = []
        for s_part, t_part, label in ((s_k, t_k, "key"), (s_v, t_v, "value")):
            if isinstance(s_part, FPField) and isinstance(t_part, FPField):
                part_safety, part_reason = classify_cast(s_part, t_part)
                worst = _worst(worst, part_safety)
                if part_reason:
                    reasons.append(f"{label}: {part_reason}")
        return worst, "; ".join(reasons) or None

    if source.type == FPType.STRUCT:
        # Field-by-field; missing target fields are dropped (LOSSY).
        if source.fields is None or target.fields is None:
            return CastSafety.SAFE, None
        worst = CastSafety.SAFE
        reasons: list[str] = []
        for name, s_field in source.fields.items():
            t_field = target.fields.get(name)
            if t_field is None:
                worst = _worst(worst, CastSafety.LOSSY)
                reasons.append(f"field '{name}' dropped")
                continue
            part_safety, part_reason = classify_cast(s_field, t_field)
            worst = _worst(worst, part_safety)
            if part_reason:
                reasons.append(f"{name}: {part_reason}")
        return worst, "; ".join(reasons) or None

    # Same kind, no parameterized narrowing checks needed.
    return CastSafety.SAFE, None


_SAFETY_ORDER = {
    CastSafety.SAFE: 0,
    CastSafety.SEMANTIC_LOSSY: 1,
    CastSafety.LOSSY: 2,
    CastSafety.IMPOSSIBLE: 3,
}


def _worst(a: CastSafety, b: CastSafety) -> CastSafety:
    """Return the more-severe of two safety classifications."""
    return a if _SAFETY_ORDER[a] >= _SAFETY_ORDER[b] else b
