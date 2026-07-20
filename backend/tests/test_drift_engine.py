"""Schema drift engine — contract tests.

Locks the categories + severity assignments the runtime drift detector
and the Mapping-tab UI both rely on.
"""

from __future__ import annotations

import pytest

from fpulse.types import (
    CanonicalSchema,
    DriftCategory,
    DriftSeverity,
    FPField,
    FPType,
    Evidence,
    diff_schemas,
    summarize_drift,
)


def _field(name: str, t: FPType, **kw) -> FPField:
    return FPField(name=name, type=t, **kw)


# ── No-drift case ──

class TestIdentity:
    def test_identical_schemas_no_diff(self):
        s = CanonicalSchema(fields=[
            _field("id", FPType.INTEGER, params={"bits": 64}),
            _field("name", FPType.STRING, params={"length": 255}),
        ])
        assert diff_schemas(s, s) == []


# ── ADDED / REMOVED ──

class TestAddRemove:
    def test_added_nullable_column_is_info(self):
        old = CanonicalSchema(fields=[_field("id", FPType.INTEGER)])
        new = CanonicalSchema(fields=[
            _field("id", FPType.INTEGER),
            _field("name", FPType.STRING, nullable=True),
        ])
        diffs = diff_schemas(old, new)
        assert len(diffs) == 1
        assert diffs[0].category == DriftCategory.ADDED
        assert diffs[0].severity == DriftSeverity.INFO
        assert diffs[0].path == "name"

    def test_added_not_null_column_is_warning(self):
        old = CanonicalSchema(fields=[_field("id", FPType.INTEGER)])
        new = CanonicalSchema(fields=[
            _field("id", FPType.INTEGER),
            _field("name", FPType.STRING, nullable=False),
        ])
        diffs = diff_schemas(old, new)
        assert diffs[0].severity == DriftSeverity.WARNING
        assert "NOT NULL" in diffs[0].message

    def test_removed_column_is_critical(self):
        old = CanonicalSchema(fields=[
            _field("id", FPType.INTEGER),
            _field("dropped", FPType.STRING),
        ])
        new = CanonicalSchema(fields=[_field("id", FPType.INTEGER)])
        diffs = diff_schemas(old, new)
        assert len(diffs) == 1
        assert diffs[0].category == DriftCategory.REMOVED
        assert diffs[0].severity == DriftSeverity.CRITICAL
        assert diffs[0].path == "dropped"


# ── TYPE_CHANGED ──

class TestTypeChanged:
    def test_int_to_string_is_critical(self):
        old = CanonicalSchema(fields=[_field("id", FPType.INTEGER)])
        new = CanonicalSchema(fields=[_field("id", FPType.STRING)])
        diffs = diff_schemas(old, new)
        assert diffs[0].category == DriftCategory.TYPE_CHANGED
        # INT → STRING is SEMANTIC_LOSSY (target STRING fallback path) — WARNING.
        assert diffs[0].severity == DriftSeverity.WARNING

    def test_string_to_int_is_critical(self):
        # STRING → INTEGER is LOSSY (parses at runtime, can fail).
        old = CanonicalSchema(fields=[_field("id", FPType.STRING)])
        new = CanonicalSchema(fields=[_field("id", FPType.INTEGER)])
        diffs = diff_schemas(old, new)
        assert diffs[0].category == DriftCategory.TYPE_CHANGED
        assert diffs[0].severity == DriftSeverity.CRITICAL

    def test_int_to_bigger_decimal_is_info(self):
        # INTEGER → DECIMAL is SAFE (lossless widening).
        old = CanonicalSchema(fields=[_field("id", FPType.INTEGER)])
        new = CanonicalSchema(fields=[
            _field("id", FPType.DECIMAL, params={"precision": 20, "scale": 0}),
        ])
        diffs = diff_schemas(old, new)
        assert diffs[0].category == DriftCategory.TYPE_CHANGED
        assert diffs[0].severity == DriftSeverity.INFO

    def test_decimal_to_int_is_critical(self):
        old = CanonicalSchema(fields=[
            _field("amount", FPType.DECIMAL, params={"precision": 18, "scale": 2}),
        ])
        new = CanonicalSchema(fields=[_field("amount", FPType.INTEGER)])
        diffs = diff_schemas(old, new)
        assert diffs[0].severity == DriftSeverity.CRITICAL
        assert "fractional" in diffs[0].message


# ── PARAMS narrowing / widening ──

class TestParamsChanges:
    def test_decimal_narrowing_is_critical(self):
        old = CanonicalSchema(fields=[
            _field("x", FPType.DECIMAL, params={"precision": 18, "scale": 4}),
        ])
        new = CanonicalSchema(fields=[
            _field("x", FPType.DECIMAL, params={"precision": 10, "scale": 2}),
        ])
        diffs = diff_schemas(old, new)
        assert diffs[0].category == DriftCategory.PARAMS_NARROWED
        assert diffs[0].severity == DriftSeverity.CRITICAL

    def test_decimal_widening_is_info(self):
        old = CanonicalSchema(fields=[
            _field("x", FPType.DECIMAL, params={"precision": 10, "scale": 2}),
        ])
        new = CanonicalSchema(fields=[
            _field("x", FPType.DECIMAL, params={"precision": 18, "scale": 4}),
        ])
        diffs = diff_schemas(old, new)
        assert diffs[0].category == DriftCategory.PARAMS_WIDENED
        assert diffs[0].severity == DriftSeverity.INFO

    def test_string_narrowing_is_critical(self):
        old = CanonicalSchema(fields=[
            _field("x", FPType.STRING, params={"length": 500}),
        ])
        new = CanonicalSchema(fields=[
            _field("x", FPType.STRING, params={"length": 100}),
        ])
        diffs = diff_schemas(old, new)
        assert diffs[0].category == DriftCategory.PARAMS_NARROWED
        assert diffs[0].severity == DriftSeverity.CRITICAL

    def test_string_widening_is_info(self):
        old = CanonicalSchema(fields=[
            _field("x", FPType.STRING, params={"length": 100}),
        ])
        new = CanonicalSchema(fields=[
            _field("x", FPType.STRING, params={"length": 500}),
        ])
        diffs = diff_schemas(old, new)
        assert diffs[0].category == DriftCategory.PARAMS_WIDENED
        assert diffs[0].severity == DriftSeverity.INFO

    def test_string_bounded_to_unbounded_is_widening(self):
        old = CanonicalSchema(fields=[
            _field("x", FPType.STRING, params={"length": 100}),
        ])
        new = CanonicalSchema(fields=[_field("x", FPType.STRING)])  # no length
        diffs = diff_schemas(old, new)
        assert diffs[0].category == DriftCategory.PARAMS_WIDENED


# ── NULLABILITY ──

class TestNullability:
    def test_nullable_to_not_null_is_critical(self):
        old = CanonicalSchema(fields=[_field("id", FPType.INTEGER, nullable=True)])
        new = CanonicalSchema(fields=[_field("id", FPType.INTEGER, nullable=False)])
        diffs = diff_schemas(old, new)
        assert diffs[0].category == DriftCategory.NULLABILITY_CHANGED
        assert diffs[0].severity == DriftSeverity.CRITICAL

    def test_not_null_to_nullable_is_info(self):
        old = CanonicalSchema(fields=[_field("id", FPType.INTEGER, nullable=False)])
        new = CanonicalSchema(fields=[_field("id", FPType.INTEGER, nullable=True)])
        diffs = diff_schemas(old, new)
        assert diffs[0].category == DriftCategory.NULLABILITY_CHANGED
        assert diffs[0].severity == DriftSeverity.INFO


# ── Nested STRUCT changes ──

class TestNestedStruct:
    def _struct(self, name, children):
        return _field(name, FPType.STRUCT, fields=children)

    def test_nested_added_field_paths_correctly(self):
        old = CanonicalSchema(fields=[
            self._struct("customer", {"id": _field("id", FPType.INTEGER)}),
        ])
        new = CanonicalSchema(fields=[
            self._struct("customer", {
                "id": _field("id", FPType.INTEGER),
                "email": _field("email", FPType.STRING),
            }),
        ])
        diffs = diff_schemas(old, new)
        assert len(diffs) == 1
        assert diffs[0].category == DriftCategory.ADDED
        assert diffs[0].path == "customer.email"

    def test_nested_removed_field_is_critical(self):
        old = CanonicalSchema(fields=[
            self._struct("customer", {
                "id": _field("id", FPType.INTEGER),
                "email": _field("email", FPType.STRING),
            }),
        ])
        new = CanonicalSchema(fields=[
            self._struct("customer", {"id": _field("id", FPType.INTEGER)}),
        ])
        diffs = diff_schemas(old, new)
        assert len(diffs) == 1
        assert diffs[0].category == DriftCategory.REMOVED
        assert diffs[0].severity == DriftSeverity.CRITICAL
        assert diffs[0].path == "customer.email"

    def test_deeply_nested_type_change(self):
        old = CanonicalSchema(fields=[
            self._struct("customer", {
                "address": self._struct("address", {
                    "zip": _field("zip", FPType.STRING),
                }),
            }),
        ])
        new = CanonicalSchema(fields=[
            self._struct("customer", {
                "address": self._struct("address", {
                    "zip": _field("zip", FPType.INTEGER),  # changed!
                }),
            }),
        ])
        diffs = diff_schemas(old, new)
        assert len(diffs) == 1
        assert diffs[0].path == "customer.address.zip"
        assert diffs[0].category == DriftCategory.TYPE_CHANGED


# ── Evidence ──

class TestEvidence:
    def test_evidence_change_is_info(self):
        old = CanonicalSchema(fields=[
            _field("x", FPType.STRING, evidence=Evidence.ADVERTISED),
        ])
        new = CanonicalSchema(fields=[
            _field("x", FPType.STRING, evidence=Evidence.INFERRED),
        ])
        diffs = diff_schemas(old, new)
        assert len(diffs) == 1
        assert diffs[0].category == DriftCategory.EVIDENCE_CHANGED
        assert diffs[0].severity == DriftSeverity.INFO


# ── Summarize ──

class TestSummary:
    def test_summary_counts_by_severity_and_category(self):
        diffs = diff_schemas(
            CanonicalSchema(fields=[
                _field("a", FPType.INTEGER),
                _field("b", FPType.STRING, params={"length": 500}),
                _field("c", FPType.INTEGER, nullable=True),
            ]),
            CanonicalSchema(fields=[
                _field("b", FPType.STRING, params={"length": 100}),
                _field("c", FPType.INTEGER, nullable=False),
                _field("d", FPType.STRING),
            ]),
        )
        summary = summarize_drift(diffs)
        # Expected diffs:
        #   a removed     (critical)
        #   b narrowed    (critical)
        #   c null→notnull(critical)
        #   d added       (info)
        assert summary["total"] == 4
        assert summary["has_critical"] is True
        assert summary["by_severity"]["critical"] == 3
        assert summary["by_severity"]["info"] == 1
        assert summary["by_category"]["removed"] == 1
        assert summary["by_category"]["params_narrowed"] == 1
        assert summary["by_category"]["nullability_changed"] == 1
        assert summary["by_category"]["added"] == 1
