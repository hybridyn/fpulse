"""F-Pulse canonical type system.

The single shared schema contract every connector produces (on read)
and consumes (on write). Sits one layer above DuckDB so the runtime
execution substrate (DuckDB today, Polars / DataFusion / Spark
tomorrow) can change without rewriting every connector or transform.

Three layers of meaning, kept separate by design:

  - ``FPType``      logical type kind (integer, decimal, string, …)
  - ``params``      parameterized metadata (precision, scale, length, timezone, …)
  - ``Provenance``  how this type was resolved (advertised / inferred / coerced)

See ``fpulse/types/canonical.py`` for the dataclasses and
``fpulse/types/cast_safety.py`` for the source→sink safety classifier
that drives the Mapping tab's ``✓ / ⚠ / ✕`` glyph.
"""

from fpulse.types.canonical import (
    CanonicalSchema,
    Evidence,
    FPField,
    FPType,
    Provenance,
)
from fpulse.types.cast_safety import CastPlanElement, CastSafety, classify_cast
from fpulse.types.drift import (
    DriftCategory,
    DriftSeverity,
    SchemaDiff,
    diff_schemas,
    summarize_drift,
)
from fpulse.types.from_postgres import (
    CANONICAL_COLUMN_QUERY,
    postgres_columns_to_canonical,
)
# 2026-05-22: cross-dialect mappers.
from fpulse.types.from_mssql import (
    CANONICAL_COLUMN_QUERY_MSSQL,
    mssql_columns_to_canonical,
)
from fpulse.types.from_mysql import (
    CANONICAL_COLUMN_QUERY_MYSQL,
    mysql_columns_to_canonical,
)
from fpulse.types.from_oracle import (
    CANONICAL_COLUMN_QUERY_ORACLE,
    oracle_columns_to_canonical,
)
from fpulse.types.policy import (
    CastPolicy,
    CastPlanVerdict,
    InferencePolicy,
    InferenceVerdict,
    gate_cast_plan,
    gate_inferred_schema,
)
from fpulse.types.to_postgres import (
    canonical_to_postgres_alter,
    canonical_to_postgres_ddl,
    plan_cast,
)
from fpulse.types.to_mssql import (
    canonical_to_mssql_alter,
    canonical_to_mssql_ddl,
)
from fpulse.types.to_mysql import (
    canonical_to_mysql_alter,
    canonical_to_mysql_ddl,
)
from fpulse.types.to_oracle import (
    canonical_to_oracle_alter,
    canonical_to_oracle_ddl,
)

__all__ = [
    "CANONICAL_COLUMN_QUERY",
    "CANONICAL_COLUMN_QUERY_MSSQL",
    "CANONICAL_COLUMN_QUERY_MYSQL",
    "CANONICAL_COLUMN_QUERY_ORACLE",
    "CanonicalSchema",
    "CastPlanElement",
    "CastPlanVerdict",
    "CastPolicy",
    "CastSafety",
    "DriftCategory",
    "DriftSeverity",
    "Evidence",
    "FPField",
    "FPType",
    "InferencePolicy",
    "InferenceVerdict",
    "Provenance",
    "SchemaDiff",
    "canonical_to_mssql_alter",
    "canonical_to_mssql_ddl",
    "canonical_to_mysql_alter",
    "canonical_to_mysql_ddl",
    "canonical_to_oracle_alter",
    "canonical_to_oracle_ddl",
    "canonical_to_postgres_alter",
    "canonical_to_postgres_ddl",
    "classify_cast",
    "diff_schemas",
    "gate_cast_plan",
    "gate_inferred_schema",
    "mssql_columns_to_canonical",
    "mysql_columns_to_canonical",
    "oracle_columns_to_canonical",
    "plan_cast",
    "postgres_columns_to_canonical",
    "summarize_drift",
]
