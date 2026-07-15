"""Schema drift engine — diff two CanonicalSchemas, return a typed change list.

Same engine drives both arrows of the canonical contract:

  - **Read-side drift**:   runtime source schema  vs  locked canonical
                           (the source advertised a different shape than
                           last time — typical for JSON APIs and CSV drops)
  - **Write-side drift**:  existing destination schema  vs  canonical
                           (someone ALTERed the table out-of-band)

Each diff entry is classified by both **category** (added / removed /
type_changed / params_narrowed / params_widened / nullability_changed)
and **severity** (info / warning / critical). Severity is computed
from the cast-safety classifier so the rules stay consistent with the
Mapping tab's ✓ / ⚠ / ✕ glyphs:

    SAFE          → info or warning (depending on category)
    SEMANTIC_LOSSY → warning
    LOSSY         → critical
    IMPOSSIBLE    → critical

Consumers (drift detector at runtime, `cast_policy` enforcement, the
"What changed?" UI on the Executions page) read the same list.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from fpulse.types.canonical import CanonicalSchema, FPField, FPType
from fpulse.types.cast_safety import CastSafety, classify_cast


class DriftCategory(Enum):
    ADDED = "added"                          # new column appeared
    REMOVED = "removed"                      # column disappeared
    TYPE_CHANGED = "type_changed"            # FPType.X → FPType.Y
    PARAMS_WIDENED = "params_widened"        # decimal/string/integer got bigger
    PARAMS_NARROWED = "params_narrowed"      # decimal/string/integer got smaller
    NULLABILITY_CHANGED = "nullability_changed"  # nullable ↔ not null flip
    NESTED_CHANGED = "nested_changed"        # STRUCT/LIST/MAP child schema changed
    EVIDENCE_CHANGED = "evidence_changed"    # ADVERTISED → INFERRED, etc.


class DriftSeverity(Enum):
    INFO = "info"           # additive / lossless change; pipeline keeps working
    WARNING = "warning"     # semantic-lossy or non-destructive narrowing
    CRITICAL = "critical"   # silent data loss risk; needs operator attention


@dataclass
class SchemaDiff:
    """One classified change between two CanonicalSchemas."""
    path: str                         # column path ("col" or "customer.address.city")
    category: DriftCategory
    severity: DriftSeverity
    message: str                      # one-line human-readable description
    old: FPField | None = None        # field as it was (None for ADDED)
    new: FPField | None = None        # field as it is now (None for REMOVED)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "category": self.category.value,
            "severity": self.severity.value,
            "message": self.message,
            "old": self.old.to_dict() if self.old else None,
            "new": self.new.to_dict() if self.new else None,
        }


# ── Engine ──

def diff_schemas(old: CanonicalSchema, new: CanonicalSchema) -> list[SchemaDiff]:
    """Compute the drift between two canonical schemas.

    Returns one ``SchemaDiff`` entry per detected change, in
    column-order. Stable: the same input pair always returns the same
    list, so snapshots + cache keys are predictable.
    """
    old_by_name: dict[str, FPField] = {f.name: f for f in old.fields}
    new_by_name: dict[str, FPField] = {f.name: f for f in new.fields}

    diffs: list[SchemaDiff] = []

    # Pass 1: columns in OLD — present in NEW (compare) or gone (REMOVED).
    for name, old_f in old_by_name.items():
        if name not in new_by_name:
            diffs.append(SchemaDiff(
                path=name,
                category=DriftCategory.REMOVED,
                severity=DriftSeverity.CRITICAL,
                message=f"column '{name}' removed",
                old=old_f,
                new=None,
            ))
            continue
        new_f = new_by_name[name]
        diffs.extend(_compare_field(name, old_f, new_f))

    # Pass 2: NEW columns missing from OLD (ADDED).
    for name, new_f in new_by_name.items():
        if name in old_by_name:
            continue
        severity = DriftSeverity.INFO if new_f.nullable else DriftSeverity.WARNING
        msg = f"column '{name}' added ({new_f.type.value})"
        if not new_f.nullable:
            msg += " — NOT NULL columns added to existing tables can fail backfill"
        diffs.append(SchemaDiff(
            path=name,
            category=DriftCategory.ADDED,
            severity=severity,
            message=msg,
            old=None,
            new=new_f,
        ))

    return diffs


def _compare_field(path: str, old: FPField, new: FPField) -> list[SchemaDiff]:
    """Compare one field pair; recurse into STRUCT children."""
    out: list[SchemaDiff] = []

    # Type kind changed (e.g. INTEGER → STRING). Severity = cast safety.
    if old.type != new.type:
        safety, reason = classify_cast(old, new)
        out.append(SchemaDiff(
            path=path,
            category=DriftCategory.TYPE_CHANGED,
            severity=_severity_for(safety),
            message=f"type {old.type.value} → {new.type.value}"
                    + (f" ({reason})" if reason else ""),
            old=old, new=new,
        ))
        return out  # No point comparing params on a different kind.

    # Same kind: behavior splits on container vs scalar.
    if old.type == FPType.STRUCT:
        # Container: skip parent-level cast-safety diff and let recursion
        # produce specific child diffs at their full dotted path. A parent-level
        # "struct narrowed" entry would just double-count what recursion finds.
        if old.fields and new.fields:
            out.extend(_compare_struct_fields(path, old.fields, new.fields))
    elif old.type in (FPType.LIST, FPType.MAP):
        # LIST/MAP recursion isn't path-addressed yet — emit one parent-level
        # NESTED_CHANGED summary if the classifier flags any difference. Keep
        # this coarse until element/key/value path diffing lands.
        safety, reason = classify_cast(old, new)
        if safety != CastSafety.SAFE:
            out.append(SchemaDiff(
                path=path,
                category=DriftCategory.NESTED_CHANGED,
                severity=_severity_for(safety),
                message=f"{old.type.value} children changed"
                        + (f" — {reason}" if reason else ""),
                old=old, new=new,
            ))
    else:
        # Scalar kinds: parameterized narrowing/widening via cast classifier.
        safety, reason = classify_cast(old, new)
        if safety != CastSafety.SAFE:
            out.append(SchemaDiff(
                path=path,
                category=DriftCategory.PARAMS_NARROWED,
                severity=_severity_for(safety),
                message=f"{old.type.value} narrowed"
                        + (f" — {reason}" if reason else ""),
                old=old, new=new,
            ))
        elif _params_widened(old, new):
            # Same kind, classifier-SAFE — but params still might have widened
            # (which is fine but worth flagging at info level for snapshots).
            out.append(SchemaDiff(
                path=path,
                category=DriftCategory.PARAMS_WIDENED,
                severity=DriftSeverity.INFO,
                message=f"{old.type.value} widened — {_describe_widening(old, new)}",
                old=old, new=new,
            ))

    # Nullability flip (orthogonal to type narrowing).
    if old.nullable != new.nullable:
        # NULL → NOT NULL is the dangerous direction (writes may fail).
        sev = (
            DriftSeverity.CRITICAL if old.nullable and not new.nullable
            else DriftSeverity.INFO
        )
        out.append(SchemaDiff(
            path=path,
            category=DriftCategory.NULLABILITY_CHANGED,
            severity=sev,
            message=(
                "nullable → NOT NULL (existing NULLs will fail writes)"
                if old.nullable and not new.nullable
                else "NOT NULL → nullable"
            ),
            old=old, new=new,
        ))

    # Evidence change is informational — surfaces in explainability surfaces
    # but doesn't break pipelines. Quiet by default; INFO severity.
    if old.evidence != new.evidence:
        out.append(SchemaDiff(
            path=path,
            category=DriftCategory.EVIDENCE_CHANGED,
            severity=DriftSeverity.INFO,
            message=f"evidence {old.evidence.value} → {new.evidence.value}",
            old=old, new=new,
        ))

    return out


def _compare_struct_fields(
    parent_path: str,
    old_children: dict[str, FPField],
    new_children: dict[str, FPField],
) -> list[SchemaDiff]:
    """Recurse the same compare logic over nested STRUCT children."""
    out: list[SchemaDiff] = []
    for name, child_old in old_children.items():
        child_path = f"{parent_path}.{name}"
        if name not in new_children:
            out.append(SchemaDiff(
                path=child_path,
                category=DriftCategory.REMOVED,
                severity=DriftSeverity.CRITICAL,
                message=f"nested field '{child_path}' removed",
                old=child_old, new=None,
            ))
            continue
        out.extend(_compare_field(child_path, child_old, new_children[name]))
    for name, child_new in new_children.items():
        if name in old_children:
            continue
        out.append(SchemaDiff(
            path=f"{parent_path}.{name}",
            category=DriftCategory.ADDED,
            severity=DriftSeverity.INFO,
            message=f"nested field '{parent_path}.{name}' added ({child_new.type.value})",
            old=None, new=child_new,
        ))
    return out


# ── Severity mapping ──

def _severity_for(safety: CastSafety) -> DriftSeverity:
    """Map cast-safety → drift-severity. Single source of truth so the
    Mapping-tab glyph and the drift report stay in lockstep."""
    if safety == CastSafety.SAFE:
        return DriftSeverity.INFO
    if safety == CastSafety.SEMANTIC_LOSSY:
        return DriftSeverity.WARNING
    return DriftSeverity.CRITICAL  # LOSSY or IMPOSSIBLE


def _params_widened(old: FPField, new: FPField) -> bool:
    """Detect harmless widening within the same kind (info-level drift)."""
    if old.type == FPType.DECIMAL:
        sp = int(old.params.get("precision", 38))
        tp = int(new.params.get("precision", 38))
        return tp > sp
    if old.type == FPType.STRING:
        sl = old.params.get("length")
        tl = new.params.get("length")
        if sl is None or tl is None:
            return tl is None and sl is not None  # bounded → unbounded
        return int(tl) > int(sl)
    if old.type == FPType.INTEGER:
        sb = old.params.get("bits")
        tb = new.params.get("bits")
        if sb is None or tb is None:
            return False
        return int(tb) > int(sb)
    return False


def _describe_widening(old: FPField, new: FPField) -> str:
    if old.type == FPType.DECIMAL:
        sp, ss = old.params.get("precision", 38), old.params.get("scale", 0)
        tp, ts = new.params.get("precision", 38), new.params.get("scale", 0)
        return f"precision {sp},{ss} → {tp},{ts}"
    if old.type == FPType.STRING:
        sl = old.params.get("length", "∞")
        tl = new.params.get("length", "∞")
        return f"length {sl} → {tl}"
    if old.type == FPType.INTEGER:
        return f"bits {old.params.get('bits')} → {new.params.get('bits')}"
    return ""


# ── Summary ──

def summarize_drift(diffs: list[SchemaDiff]) -> dict:
    """Roll a diff list up to per-severity + per-category counts.

    Used by the runtime to decide whether to gate execution (any
    CRITICAL? fail if cast_policy=strict) and by the UI to render a
    drift summary badge without iterating the full list."""
    by_sev = {s.value: 0 for s in DriftSeverity}
    by_cat = {c.value: 0 for c in DriftCategory}
    for d in diffs:
        by_sev[d.severity.value] += 1
        by_cat[d.category.value] += 1
    return {
        "total": len(diffs),
        "by_severity": by_sev,
        "by_category": by_cat,
        "has_critical": by_sev[DriftSeverity.CRITICAL.value] > 0,
    }
