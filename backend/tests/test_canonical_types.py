"""Canonical type system — contract tests.

Locks the invariants every connector (source AND sink) will depend on:
parameterized narrowing detection, nested path lookup, dict round-trip
(needed for workflow snapshots and drift comparison), provenance
preservation, and the 4-level cast safety taxonomy.
"""

from __future__ import annotations

import pytest

from fpulse.types import (
    CanonicalSchema,
    CastSafety,
    Evidence,
    FPField,
    FPType,
    Provenance,
    classify_cast,
)


# ── Schema construction + round-trip ──

class TestSchemaRoundTrip:
    def test_flat_schema_roundtrip(self):
        s = CanonicalSchema(fields=[
            FPField(name="id", type=FPType.INTEGER, nullable=False,
                    params={"bits": 64}, native_raw="BIGINT"),
            FPField(name="email", type=FPType.STRING, params={"length": 255},
                    native_raw="VARCHAR(255)"),
            FPField(name="price", type=FPType.DECIMAL,
                    params={"precision": 10, "scale": 2}, native_raw="NUMERIC(10,2)"),
        ])
        roundtripped = CanonicalSchema.from_dict(s.to_dict())
        assert roundtripped.names == ["id", "email", "price"]
        assert roundtripped.by_name("id").params == {"bits": 64}
        assert roundtripped.by_name("price").params == {"precision": 10, "scale": 2}

    def test_provenance_survives_roundtrip(self):
        # Reviewer 2's "why was this column cast to VARCHAR?" needs full provenance.
        f = FPField(
            name="user_id",
            type=FPType.STRING,
            evidence=Evidence.COERCED,
            confidence=0.7,
            provenance=[
                Provenance(source="JSON sample(10000)", confidence=0.6, sample_size=10000,
                           conflicts=[{"value": "uuid-string", "count": 23}]),
                Provenance(source="user override", confidence=1.0),
            ],
        )
        out = FPField.from_dict(f.to_dict())
        assert out.evidence == Evidence.COERCED
        assert len(out.provenance) == 2
        assert out.provenance[0].confidence == 0.6
        assert out.provenance[0].sample_size == 10000
        assert out.provenance[0].conflicts[0]["value"] == "uuid-string"

    def test_struct_field_roundtrip(self):
        f = FPField(
            name="customer", type=FPType.STRUCT,
            fields={
                "id": FPField(name="id", type=FPType.INTEGER, params={"bits": 64}),
                "address": FPField(
                    name="address", type=FPType.STRUCT,
                    fields={
                        "city": FPField(name="city", type=FPType.STRING),
                        "zip": FPField(name="zip", type=FPType.STRING),
                    },
                ),
            },
        )
        out = FPField.from_dict(f.to_dict())
        assert out.type == FPType.STRUCT
        assert "address" in out.fields
        assert out.fields["address"].fields["city"].type == FPType.STRING


# ── Path-addressable nested fields ──

class TestPathLookup:
    @pytest.fixture
    def deep_struct(self):
        return FPField(
            name="customer", type=FPType.STRUCT,
            fields={
                "id": FPField(name="id", type=FPType.INTEGER),
                "address": FPField(
                    name="address", type=FPType.STRUCT,
                    fields={
                        "city": FPField(name="city", type=FPType.STRING),
                        "zip": FPField(name="zip", type=FPType.STRING),
                    },
                ),
            },
        )

    def test_at_path_one_level(self, deep_struct):
        assert deep_struct.at_path("id").type == FPType.INTEGER

    def test_at_path_two_levels(self, deep_struct):
        leaf = deep_struct.at_path("address.city")
        assert leaf is not None
        assert leaf.type == FPType.STRING
        assert leaf.name == "city"

    def test_at_path_missing_segment_returns_none(self, deep_struct):
        assert deep_struct.at_path("address.country") is None
        assert deep_struct.at_path("nope") is None

    def test_at_path_through_non_struct_returns_none(self, deep_struct):
        # "id" is INTEGER, not STRUCT — descending past it returns None.
        assert deep_struct.at_path("id.something") is None

    def test_iter_paths_walks_struct(self, deep_struct):
        paths = [p for p, _ in deep_struct.iter_paths()]
        assert paths == [
            "customer",
            "customer.id",
            "customer.address",
            "customer.address.city",
            "customer.address.zip",
        ]


# ── Schema lookup helpers ──

class TestSchemaLookup:
    def test_by_name_finds_field(self):
        s = CanonicalSchema(fields=[
            FPField(name="a", type=FPType.INTEGER),
            FPField(name="b", type=FPType.STRING),
        ])
        assert s.by_name("a").type == FPType.INTEGER
        assert s.by_name("missing") is None

    def test_iter_paths_walks_top_level_and_structs(self):
        s = CanonicalSchema(fields=[
            FPField(name="id", type=FPType.INTEGER),
            FPField(
                name="payload", type=FPType.STRUCT,
                fields={"k": FPField(name="k", type=FPType.STRING)},
            ),
        ])
        paths = [p for p, _ in s.iter_paths()]
        assert paths == ["id", "payload", "payload.k"]


# ── Cast safety: same-kind narrowing ──

class TestSameKindCasts:
    def test_decimal_widening_is_safe(self):
        s = FPField(name="x", type=FPType.DECIMAL, params={"precision": 10, "scale": 2})
        t = FPField(name="x", type=FPType.DECIMAL, params={"precision": 18, "scale": 4})
        safety, _ = classify_cast(s, t)
        assert safety == CastSafety.SAFE

    def test_decimal_narrowing_precision_is_lossy(self):
        s = FPField(name="x", type=FPType.DECIMAL, params={"precision": 18, "scale": 4})
        t = FPField(name="x", type=FPType.DECIMAL, params={"precision": 10, "scale": 2})
        safety, reason = classify_cast(s, t)
        assert safety == CastSafety.LOSSY
        assert "decimal" in reason

    def test_string_widening_is_safe(self):
        s = FPField(name="x", type=FPType.STRING, params={"length": 50})
        t = FPField(name="x", type=FPType.STRING, params={"length": 255})
        safety, _ = classify_cast(s, t)
        assert safety == CastSafety.SAFE

    def test_string_narrowing_is_lossy(self):
        s = FPField(name="x", type=FPType.STRING, params={"length": 500})
        t = FPField(name="x", type=FPType.STRING, params={"length": 255})
        safety, reason = classify_cast(s, t)
        assert safety == CastSafety.LOSSY
        assert "string length" in reason

    def test_string_unbounded_target_is_safe(self):
        # No `length` on target = unbounded (TEXT / NVARCHAR(MAX)).
        s = FPField(name="x", type=FPType.STRING, params={"length": 500})
        t = FPField(name="x", type=FPType.STRING)
        safety, _ = classify_cast(s, t)
        assert safety == CastSafety.SAFE

    def test_integer_narrowing_is_lossy(self):
        s = FPField(name="x", type=FPType.INTEGER, params={"bits": 64})
        t = FPField(name="x", type=FPType.INTEGER, params={"bits": 32})
        safety, _ = classify_cast(s, t)
        assert safety == CastSafety.LOSSY


# ── Cast safety: temporal semantic-lossiness ──

class TestTemporalCasts:
    def test_timestamp_drops_timezone_is_semantic_lossy(self):
        # TIMESTAMP TZ → TIMESTAMP: bytes fit, meaning narrows. Reviewer 2's
        # "Semantic-safe" axis — F-Pulse marks it LOSSY for clarity.
        s = FPField(name="t", type=FPType.TIMESTAMP, params={"timezone": "UTC"})
        t = FPField(name="t", type=FPType.TIMESTAMP)
        safety, reason = classify_cast(s, t)
        assert safety == CastSafety.SEMANTIC_LOSSY
        assert "timezone" in reason

    def test_date_to_timestamp_is_safe(self):
        s = FPField(name="d", type=FPType.DATE)
        t = FPField(name="d", type=FPType.TIMESTAMP)
        safety, _ = classify_cast(s, t)
        assert safety == CastSafety.SAFE

    def test_timestamp_to_date_drops_time(self):
        s = FPField(name="d", type=FPType.TIMESTAMP)
        t = FPField(name="d", type=FPType.DATE)
        safety, reason = classify_cast(s, t)
        assert safety == CastSafety.LOSSY
        assert "time" in reason


# ── Cast safety: cross-kind ──

class TestCrossKindCasts:
    def test_int_to_decimal_is_safe(self):
        safety, _ = classify_cast(
            FPField(name="x", type=FPType.INTEGER),
            FPField(name="x", type=FPType.DECIMAL),
        )
        assert safety == CastSafety.SAFE

    def test_decimal_to_int_is_lossy(self):
        safety, reason = classify_cast(
            FPField(name="x", type=FPType.DECIMAL),
            FPField(name="x", type=FPType.INTEGER),
        )
        assert safety == CastSafety.LOSSY
        assert "fractional" in reason

    def test_int_to_float_is_lossy_high_magnitude(self):
        # Documented: integers > 2^53 lose precision in float64.
        safety, _ = classify_cast(
            FPField(name="x", type=FPType.INTEGER),
            FPField(name="x", type=FPType.FLOAT),
        )
        assert safety == CastSafety.LOSSY

    def test_json_to_string_is_semantic_lossy(self):
        safety, reason = classify_cast(
            FPField(name="x", type=FPType.JSON),
            FPField(name="x", type=FPType.STRING),
        )
        assert safety == CastSafety.SEMANTIC_LOSSY
        assert "parseab" in reason or "addressing" in reason

    def test_struct_to_string_is_semantic_lossy(self):
        safety, _ = classify_cast(
            FPField(name="x", type=FPType.STRUCT,
                    fields={"a": FPField(name="a", type=FPType.INTEGER)}),
            FPField(name="x", type=FPType.STRING),
        )
        assert safety == CastSafety.SEMANTIC_LOSSY

    def test_string_to_int_is_lossy(self):
        safety, _ = classify_cast(
            FPField(name="x", type=FPType.STRING),
            FPField(name="x", type=FPType.INTEGER),
        )
        assert safety == CastSafety.LOSSY

    def test_blob_to_date_is_impossible(self):
        safety, _ = classify_cast(
            FPField(name="x", type=FPType.BINARY),
            FPField(name="x", type=FPType.DATE),
        )
        assert safety == CastSafety.IMPOSSIBLE


# ── Cast safety: nested types ──

class TestNestedCasts:
    def test_list_propagates_element_safety(self):
        # LIST<INTEGER> → LIST<STRING> = SEMANTIC_LOSSY (element narrows).
        s = FPField(
            name="ids", type=FPType.LIST,
            params={"element_type": FPField(name="_", type=FPType.INTEGER)},
        )
        t = FPField(
            name="ids", type=FPType.LIST,
            params={"element_type": FPField(name="_", type=FPType.STRING)},
        )
        safety, _ = classify_cast(s, t)
        assert safety == CastSafety.SEMANTIC_LOSSY

    def test_map_worst_of_key_and_value(self):
        # MAP<STRING,DECIMAL(10,2)> → MAP<STRING,INTEGER> is LOSSY (value cast).
        s = FPField(
            name="m", type=FPType.MAP,
            params={
                "key_type": FPField(name="k", type=FPType.STRING),
                "value_type": FPField(name="v", type=FPType.DECIMAL,
                                       params={"precision": 10, "scale": 2}),
            },
        )
        t = FPField(
            name="m", type=FPType.MAP,
            params={
                "key_type": FPField(name="k", type=FPType.STRING),
                "value_type": FPField(name="v", type=FPType.INTEGER),
            },
        )
        safety, reason = classify_cast(s, t)
        assert safety == CastSafety.LOSSY
        assert "value" in reason

    def test_struct_missing_target_field_is_lossy(self):
        s = FPField(
            name="row", type=FPType.STRUCT,
            fields={
                "a": FPField(name="a", type=FPType.INTEGER),
                "b": FPField(name="b", type=FPType.STRING),
            },
        )
        t = FPField(
            name="row", type=FPType.STRUCT,
            fields={"a": FPField(name="a", type=FPType.INTEGER)},
        )
        safety, reason = classify_cast(s, t)
        assert safety == CastSafety.LOSSY
        assert "'b'" in reason and "dropped" in reason


# ── Unknown kind handling ──

class TestUnknownKind:
    def test_unknown_source_is_lossy(self):
        safety, _ = classify_cast(
            FPField(name="x", type=FPType.UNKNOWN),
            FPField(name="x", type=FPType.INTEGER),
        )
        assert safety == CastSafety.LOSSY

    def test_unknown_target_is_lossy(self):
        safety, _ = classify_cast(
            FPField(name="x", type=FPType.STRING),
            FPField(name="x", type=FPType.UNKNOWN),
        )
        assert safety == CastSafety.LOSSY
