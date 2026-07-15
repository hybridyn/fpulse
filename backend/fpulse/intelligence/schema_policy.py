"""Schema-policy enforcement for sinks (2026-05-27).

A *schema policy* tells a sink what to do when the incoming schema doesn't
match the existing destination's schema. Without this layer, sinks fan
out to ad-hoc `auto_evolve` flags (warehouse_sink) or silent overwrites
(local_table_sink replace mode) — the operator has no single lever and
no preview of what's about to happen.

The four-value policy and decision model below are intentionally narrow:

  * ``strict``                    — fail the run on ANY schema change
  * ``add_columns``               — allow new nullable columns; reject the rest
  * ``compatible``                — adds + type widening; reject narrowing/drops
  * ``allow_all_with_warning``    — apply everything, emit warning event

This module is pure (no I/O, no DB, no DuckDB). Sinks call
``evaluate_policy(existing, incoming, policy)`` and act on the returned
``PolicyDecision``. That keeps the policy logic unit-testable without
spinning up the rest of the runtime, and lets the API surface the same
decision in a "pre-run drift preview" without re-executing the sink.

Type-widening rules use the same compatibility matrix as
``schema_contract._COMPATIBLE_TYPES`` (referenced via
``_TYPE_WIDENING`` here) so the Mapping tab's ✓/⚠/✕ glyphs, the
contract validator, and the policy evaluator never disagree about
whether ``INT → BIGINT`` is safe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from fpulse.intelligence.type_normalization import (
    CT_UNKNOWN,
    canonicalize_type,
    types_compatible,
)


# ── Policy enum ────────────────────────────────────────────────────────


class SchemaPolicy(str, Enum):
    """How a sink reacts to schema drift between existing + incoming.

    Inherits from ``str`` so it round-trips through Pydantic and JSON
    without manual ``.value`` calls — the params dict on a Step keeps
    plain strings, and ``SchemaPolicy(params["schema_policy"])`` works.
    """

    STRICT = "strict"
    ADD_COLUMNS = "add_columns"
    COMPATIBLE = "compatible"
    ALLOW_ALL_WITH_WARNING = "allow_all_with_warning"


# Default policy applied when a sink config omits ``schema_policy``.
# ``add_columns`` is the safest non-strict default: it preserves the
# existing shape, lets the pipeline grow naturally (a new ``email``
# column in the source becomes a new column on the table) but refuses
# to drop or narrow anything. A breaking change still trips the run —
# the operator just has to opt in to ``strict`` for tighter contracts
# or ``allow_all_with_warning`` for full evolution.
DEFAULT_POLICY = SchemaPolicy.ADD_COLUMNS


# ── Errors ─────────────────────────────────────────────────────────────


class SchemaDriftError(RuntimeError):
    """Raised when a sink's schema_policy refuses the incoming schema.

    The carried ``decision`` lets the caller (executor, API layer)
    surface the same drift report the UI rendered in the pre-run banner.
    """

    def __init__(self, message: str, decision: "PolicyDecision | None" = None) -> None:
        super().__init__(message)
        self.decision = decision


# ── Type widening matrix ───────────────────────────────────────────────
#
# A "compatible" type change is one the policy ``compatible`` will allow
# WITHOUT raising. We keep the matrix here rather than re-importing the
# contract store's matrix because:
#   * contract's matrix is symmetric (a→b OR b→a), but widening is
#     direction-sensitive — INT → BIGINT widens, BIGINT → INT narrows.
#   * The DDL emitted on the SQL path needs a "this is a strict
#     widening" answer, not "are these two types in the same family".
#
# Keys are the OLD type; the set is the NEW types that count as
# widening. Equality (old == new) is handled separately so we don't
# need to repeat each key in its own value set.
_TYPE_WIDENING: dict[str, set[str]] = {
    # Integer family — wider bit-width is fine, narrower is not.
    "TINYINT": {"SMALLINT", "INTEGER", "INT", "BIGINT", "INT64", "INT32"},
    "SMALLINT": {"INTEGER", "INT", "BIGINT", "INT64", "INT32"},
    "INTEGER": {"BIGINT", "INT64"},
    "INT": {"BIGINT", "INT64"},
    "INT32": {"BIGINT", "INT64"},
    # Float family — REAL/FLOAT (32) → DOUBLE (64) widens; the reverse narrows.
    "REAL": {"DOUBLE", "FLOAT"},
    "FLOAT": {"DOUBLE"},
    # String family — VARCHAR/CHAR are open-length on DuckDB so widening
    # is the move to a strictly unbounded representation.
    "CHAR": {"VARCHAR", "TEXT", "STRING"},
    "VARCHAR": {"TEXT", "STRING"},
    "STRING": {"TEXT", "VARCHAR"},
}

# Aliases that should be treated as the same type for ALL comparisons —
# the contract store already normalises these in ``_types_compatible``.
# Centralised here so the policy evaluator and the contract validator
# never disagree on "is VARCHAR == STRING".
_TYPE_ALIASES: dict[str, str] = {
    "STRING": "VARCHAR",
    "TEXT": "VARCHAR",
    "CHAR": "VARCHAR",
    "INT": "INTEGER",
    "INT32": "INTEGER",
    "INT64": "BIGINT",
    "BOOL": "BOOLEAN",
    "DATETIME": "TIMESTAMP",
    "BYTEA": "BLOB",
}


def _norm(type_str: str) -> str:
    """Normalise a type string: upper, strip, drop precision parens."""
    base = (type_str or "").upper().strip()
    if "(" in base:
        base = base.split("(", 1)[0].strip()
    return _TYPE_ALIASES.get(base, base)


def _is_strict_widening(old_type: str, new_type: str) -> bool:
    o, n = _norm(old_type), _norm(new_type)
    if o == n:
        return False  # Equal isn't widening — that's "no change".
    return n in _TYPE_WIDENING.get(o, set())


# ── Decision model ─────────────────────────────────────────────────────


@dataclass
class ColumnChange:
    """One detected difference between existing and incoming schemas."""

    kind: str           # "added" | "dropped" | "type_changed" | "nullable_changed"
    column: str         # column name
    from_type: str | None = None   # existing type (None for added)
    to_type: str | None = None     # incoming type (None for dropped)
    from_nullable: bool | None = None
    to_nullable: bool | None = None
    # ``policy_action`` is what the chosen policy will DO about this
    # change. Used by the API + UI to render "this run will: add
    # column email, widen id from INT to BIGINT" without re-implementing
    # the rules on the frontend.
    policy_action: str = "reject"   # "apply_add" | "apply_widen" | "apply_force" | "reject" | "ignore"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "column": self.column,
            "from_type": self.from_type,
            "to_type": self.to_type,
            "from_nullable": self.from_nullable,
            "to_nullable": self.to_nullable,
            "policy_action": self.policy_action,
        }


@dataclass
class PolicyDecision:
    """The result of evaluating a policy against a (existing, incoming) pair.

    ``ok`` is the load-bearing flag — sinks call ``decision.raise_if_rejected()``
    rather than re-checking the policy. The other fields exist so the API
    layer can render the same decision in the pre-run banner without
    running the sink.
    """

    policy: SchemaPolicy
    ok: bool                                # False ⇒ policy rejects this run
    changes: list[ColumnChange] = field(default_factory=list)
    rejection_reason: str | None = None     # populated when ok == False
    has_drift: bool = False                 # True if ANY change detected
    severity: str = "info"                  # "info" | "warning" | "critical"

    # Convenience accessors the sink reaches for at ALTER time.
    @property
    def adds(self) -> list[ColumnChange]:
        """Columns to ADD (action == apply_add)."""
        return [c for c in self.changes if c.policy_action == "apply_add"]

    @property
    def widens(self) -> list[ColumnChange]:
        """Columns to widen (apply_widen)."""
        return [c for c in self.changes if c.policy_action == "apply_widen"]

    @property
    def forced(self) -> list[ColumnChange]:
        """Changes the policy is forcing through (allow_all_with_warning).

        Includes drops and narrowing — anything the safer policies
        would reject. Sinks treat these as "best effort" — the SQL
        sink will issue a DROP COLUMN; the Parquet sink will let the
        new file land with whatever shape it has.
        """
        return [c for c in self.changes if c.policy_action == "apply_force"]

    def to_summary(self) -> dict[str, Any]:
        """Compact form for ``schema_history.change_summary`` and the bus event."""
        return {
            "policy": self.policy.value,
            "added": [c.column for c in self.changes if c.kind == "added"],
            "dropped": [c.column for c in self.changes if c.kind == "dropped"],
            "type_changed": [
                {"column": c.column, "from": c.from_type, "to": c.to_type}
                for c in self.changes if c.kind == "type_changed"
            ],
            "nullable_changed": [c.column for c in self.changes if c.kind == "nullable_changed"],
            "severity": self.severity,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy.value,
            "ok": self.ok,
            "has_drift": self.has_drift,
            "severity": self.severity,
            "rejection_reason": self.rejection_reason,
            "changes": [c.to_dict() for c in self.changes],
        }

    def raise_if_rejected(self) -> None:
        """Called by sinks after evaluate_policy — raises on a non-ok decision.

        Keeps the call-site one line at the cost of one method. The
        ``SchemaDriftError`` carries the full decision so callers
        upstream (executor → API → UI) can render the same diff that
        triggered the rejection.
        """
        if not self.ok:
            raise SchemaDriftError(
                self.rejection_reason or "schema drift rejected by policy",
                decision=self,
            )


# ── The evaluator ──────────────────────────────────────────────────────


def evaluate_policy(
    existing: list[dict[str, Any]] | None,
    incoming: list[dict[str, Any]],
    policy: SchemaPolicy | str | None = None,
    *,
    existing_dialect: str | None = None,
    incoming_dialect: str | None = None,
) -> PolicyDecision:
    """Apply a schema_policy to an (existing, incoming) column-list pair.

    Args:
        existing: list of dicts ``{name, type, nullable}`` for the
            destination's current shape. ``None`` (or empty list) means
            the destination doesn't exist yet — first write — and we
            short-circuit to ``ok=True`` with no changes.
        incoming: same shape as ``existing`` but for the upstream
            relation about to land in the sink.
        policy: a ``SchemaPolicy`` value or its string form. ``None``
            falls back to ``DEFAULT_POLICY``.
        existing_dialect: connector type of the destination (e.g.
            ``"duckdb"``, ``"postgresql"``). When both dialects are
            known, type comparison defers to
            ``type_normalization.types_compatible`` so a PostgreSQL
            ``numeric(18,2)`` arriving at a DuckDB ``decimal(18,2)``
            destination is treated as compatible instead of drift.
            Falls back to string equality when omitted.
        incoming_dialect: connector type of the source. Same semantics
            as ``existing_dialect``.

    Returns:
        A ``PolicyDecision``. Inspect ``.ok`` to gate the write,
        ``.adds`` / ``.widens`` / ``.forced`` to drive ALTER DDL, and
        ``.to_summary()`` to record in schema_history.

    The evaluator never raises by design — sinks call
    ``.raise_if_rejected()`` explicitly. That separation keeps the
    pre-run drift preview endpoint safe to call from the UI (no
    exception traffic just to render a banner).
    """
    # Normalise policy input. Accepting both enum and string lets the
    # IR carry plain strings (which it does) while internal callers
    # pass the enum.
    if policy is None:
        policy = DEFAULT_POLICY
    if isinstance(policy, str):
        try:
            policy = SchemaPolicy(policy.lower())
        except ValueError:
            # Unknown policy string → fall back to default rather than
            # crash. The pre-validator should have caught this upstream,
            # but a typo in a workflow JSON should not take a run down.
            policy = DEFAULT_POLICY

    # First-write short-circuit. No existing schema → nothing to drift
    # FROM, so every policy accepts. ``ok=True`` and an empty changes
    # list mean the sink writes without ALTER and without emitting a
    # SchemaDriftDetected event.
    if not existing:
        return PolicyDecision(policy=policy, ok=True, has_drift=False)

    existing_map = {(c.get("name") or "").lower(): c for c in existing}
    incoming_map = {(c.get("name") or "").lower(): c for c in incoming}

    changes: list[ColumnChange] = []

    # ── Pass 1: columns in EXISTING — present in INCOMING (compare) or dropped.
    for name_lc, exist_col in existing_map.items():
        exist_name = exist_col.get("name") or name_lc
        if name_lc not in incoming_map:
            changes.append(ColumnChange(
                kind="dropped",
                column=exist_name,
                from_type=exist_col.get("type"),
                to_type=None,
                from_nullable=exist_col.get("nullable"),
            ))
            continue

        inc_col = incoming_map[name_lc]
        exist_type = exist_col.get("type", "VARCHAR")
        inc_type = inc_col.get("type", "VARCHAR")

        # Type change — but normalise aliases first so VARCHAR == STRING
        # isn't reported as drift. The policy decision below splits
        # widening (apply_widen) from narrowing (reject under
        # compatible / accept under allow_all).
        #
        # Connector-aware gate (Roadmap Item 3, 2026-05-27): when both
        # dialects are known, prefer the canonical-type compatibility
        # check from ``type_normalization`` so e.g. PostgreSQL
        # ``numeric(18,2)`` arriving at a DuckDB ``decimal(18,2)``
        # destination is treated as same-type, not drift. We still fall
        # back to ``_norm`` string equality when dialects are unknown
        # so the pre-2026-05-27 behaviour is preserved end-to-end.
        if _norm(exist_type) != _norm(inc_type):
            cross_dialect_ok = False
            if existing_dialect and incoming_dialect:
                ca = canonicalize_type(existing_dialect, exist_type)
                cb = canonicalize_type(incoming_dialect, inc_type)
                if ca != CT_UNKNOWN and cb != CT_UNKNOWN and ca == cb:
                    # Same canonical form — pure re-encoding, no drift.
                    cross_dialect_ok = True
                elif types_compatible(
                    existing_dialect, exist_type,
                    incoming_dialect, inc_type,
                ):
                    # Incoming type already fits the existing column
                    # without coercion — no drift event needed.
                    cross_dialect_ok = True
            if not cross_dialect_ok:
                changes.append(ColumnChange(
                    kind="type_changed",
                    column=exist_name,
                    from_type=exist_type,
                    to_type=inc_type,
                ))

        # Nullability flip is tracked but is informational unless the
        # policy is strict — adding a NOT NULL constraint to an existing
        # nullable column is dangerous, but neither widening nor adding
        # a column. We surface it; the policy below decides whether to
        # gate on it.
        exist_n = exist_col.get("nullable", True)
        inc_n = inc_col.get("nullable", True)
        if exist_n != inc_n:
            changes.append(ColumnChange(
                kind="nullable_changed",
                column=exist_name,
                from_nullable=exist_n,
                to_nullable=inc_n,
            ))

    # ── Pass 2: columns in INCOMING that aren't in EXISTING (added).
    for name_lc, inc_col in incoming_map.items():
        if name_lc in existing_map:
            continue
        changes.append(ColumnChange(
            kind="added",
            column=inc_col.get("name") or name_lc,
            from_type=None,
            to_type=inc_col.get("type"),
            to_nullable=inc_col.get("nullable", True),
        ))

    # No changes detected → ok regardless of policy.
    if not changes:
        return PolicyDecision(policy=policy, ok=True, has_drift=False)

    # ── Annotate each change with the action the policy will take.
    severity = "info"
    rejection_reason: str | None = None
    ok = True

    for change in changes:
        action, sev = _classify_action(change, policy)
        change.policy_action = action
        if sev == "critical":
            severity = "critical"
        elif sev == "warning" and severity != "critical":
            severity = "warning"

    if policy is SchemaPolicy.STRICT:
        # ANY change fails strict — even an added nullable column.
        ok = False
        rejection_reason = _format_strict_reason(changes)
    elif policy is SchemaPolicy.ADD_COLUMNS:
        # ADD ok; everything else fails.
        rejected = [c for c in changes if c.policy_action == "reject"]
        if rejected:
            ok = False
            rejection_reason = _format_policy_reason("add_columns", rejected)
    elif policy is SchemaPolicy.COMPATIBLE:
        rejected = [c for c in changes if c.policy_action == "reject"]
        if rejected:
            ok = False
            rejection_reason = _format_policy_reason("compatible", rejected)
    # ALLOW_ALL_WITH_WARNING: ok stays True; every change either
    # applies normally or apply_force, and severity is already set
    # to at least "warning" by ``_classify_action``.

    return PolicyDecision(
        policy=policy,
        ok=ok,
        changes=changes,
        rejection_reason=rejection_reason,
        has_drift=True,
        severity=severity,
    )


def _classify_action(change: ColumnChange, policy: SchemaPolicy) -> tuple[str, str]:
    """Return (action, severity) for a single change under a policy.

    Pulled out of evaluate_policy so the 4-policy × 4-change-kind grid
    is the one thing the unit tests pin down. Adding a fifth policy or
    a fifth change kind means editing this function and the matrix
    documented in the module docstring — no other place.
    """
    kind = change.kind

    if kind == "added":
        # Adds are always safe under any non-strict policy.
        if policy is SchemaPolicy.STRICT:
            return "reject", "critical"
        # Nullable added column → info. NOT NULL added → warning (existing
        # rows have no value for it; the writer is responsible for
        # back-filling or the DDL will fail).
        sev = "info" if (change.to_nullable is None or change.to_nullable) else "warning"
        return "apply_add", sev

    if kind == "type_changed":
        widens = _is_strict_widening(change.from_type or "", change.to_type or "")
        if policy is SchemaPolicy.STRICT:
            return "reject", "critical"
        if policy is SchemaPolicy.ADD_COLUMNS:
            # Type change isn't an add — always rejected here.
            return "reject", "critical"
        if policy is SchemaPolicy.COMPATIBLE:
            return ("apply_widen", "info") if widens else ("reject", "critical")
        # ALLOW_ALL_WITH_WARNING: forcing through.
        return ("apply_widen" if widens else "apply_force"), "warning"

    if kind == "dropped":
        # Dropping a column is destructive. Only allow_all_with_warning
        # will swallow it; everyone else rejects.
        if policy is SchemaPolicy.ALLOW_ALL_WITH_WARNING:
            return "apply_force", "warning"
        return "reject", "critical"

    if kind == "nullable_changed":
        # Nullable → NOT NULL: critical, would break existing NULLs.
        # NOT NULL → nullable: harmless, info-level.
        was_nullable = bool(change.from_nullable)
        now_nullable = bool(change.to_nullable)
        if was_nullable and not now_nullable:
            # Tightening — the dangerous direction.
            if policy is SchemaPolicy.ALLOW_ALL_WITH_WARNING:
                return "apply_force", "warning"
            return "reject", "critical"
        # Loosening or unchanged direction — informational under every
        # non-strict policy.
        if policy is SchemaPolicy.STRICT:
            return "reject", "warning"
        return "ignore", "info"

    # Unknown kind — never raise from a classifier. Treat as a forced
    # change so the caller still sees it in the decision.
    return "apply_force", "warning"


def _format_strict_reason(changes: list[ColumnChange]) -> str:
    """Build the user-facing message for a strict-policy rejection."""
    parts: list[str] = []
    for c in changes:
        if c.kind == "added":
            parts.append(f"+{c.column} ({c.to_type})")
        elif c.kind == "dropped":
            parts.append(f"-{c.column}")
        elif c.kind == "type_changed":
            parts.append(f"{c.column}: {c.from_type}→{c.to_type}")
        elif c.kind == "nullable_changed":
            parts.append(f"{c.column} nullable {c.from_nullable}→{c.to_nullable}")
    return (
        "schema_policy=strict: refusing to write because the incoming schema "
        f"differs from the existing one ({', '.join(parts)}). Set schema_policy "
        f"to add_columns, compatible, or allow_all_with_warning to evolve."
    )


def _format_policy_reason(policy_name: str, rejected: list[ColumnChange]) -> str:
    parts: list[str] = []
    for c in rejected:
        if c.kind == "dropped":
            parts.append(f"-{c.column} (drop)")
        elif c.kind == "type_changed":
            parts.append(f"{c.column}: {c.from_type}→{c.to_type} (narrowing)")
        elif c.kind == "nullable_changed":
            parts.append(f"{c.column}: nullability tightened")
        else:
            parts.append(f"{c.column} ({c.kind})")
    return (
        f"schema_policy={policy_name}: refusing to apply unsafe changes "
        f"({', '.join(parts)}). Use allow_all_with_warning to force, or fix "
        f"the source to preserve the existing shape."
    )


# ── Param-schema helper for sink registries ────────────────────────────

# Sink registry entries call this so the option list + tooltips stay
# in ONE place. Adding a 5th policy means editing here and the enum —
# the frontend automatically picks up new options via DynamicConfig.
def schema_policy_param() -> dict[str, Any]:
    """Param-schema fragment a sink adds to its ``param_schema()`` list.

    Returns the shape DynamicConfig's ``select`` field renderer expects.
    Options are emitted as ``{value, label, description}`` so the
    frontend tooltip per option is driven by this single source.
    """
    return {
        "name": "schema_policy",
        "type": "select",
        "label": "Schema policy",
        "default": DEFAULT_POLICY.value,
        "options": [
            {
                "value": SchemaPolicy.STRICT.value,
                "label": "Strict — fail on any change",
                "description": (
                    "Refuse to write if the incoming schema differs from the "
                    "destination in any way. Best for pipelines feeding a "
                    "contract-locked dataset."
                ),
            },
            {
                "value": SchemaPolicy.ADD_COLUMNS.value,
                "label": "Add columns (default)",
                "description": (
                    "Allow new nullable columns; reject drops, type changes, "
                    "and nullability tightening. The safe everyday default."
                ),
            },
            {
                "value": SchemaPolicy.COMPATIBLE.value,
                "label": "Compatible — adds + widening",
                "description": (
                    "Allow new columns AND lossless type widening "
                    "(INT→BIGINT, VARCHAR→TEXT). Reject narrowing and drops."
                ),
            },
            {
                "value": SchemaPolicy.ALLOW_ALL_WITH_WARNING.value,
                "label": "Allow all (warning)",
                "description": (
                    "Apply every change, including drops and narrowing. "
                    "Emits a SchemaDriftDetected event with severity=warning. "
                    "Use when the destination is fully evolvable."
                ),
            },
        ],
        "description": (
            "Controls how this sink reacts when the incoming schema differs "
            "from the destination's existing shape. Drift is reviewed in the "
            "Run banner before the run starts."
        ),
        "tier": "smart-default",
        "tab": "Schema",
    }


__all__ = [
    "SchemaPolicy",
    "SchemaDriftError",
    "ColumnChange",
    "PolicyDecision",
    "DEFAULT_POLICY",
    "evaluate_policy",
    "schema_policy_param",
]
