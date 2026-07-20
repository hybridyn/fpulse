"""
F-Pulse Data Intelligence — Schema Detection, Flattening,
Execution Intelligence, Schema Contracts, Pre-Validation, and Error Intelligence.
"""

from .schema_detector import SchemaDetector, DetectedSchema, ColumnInfo, RepeatingGroup
from .flatten_engine import FlattenEngine, FlattenResult, FlattenedTable
from .execution_intel import ExecutionIntelligence, ExecutionConfig, ExecutionPlan, RetryStrategy
from .schema_contract import SchemaContractStore, SchemaContract, SchemaDrift, ContractValidation
from .pre_validator import PreValidator, PreValidationResult, ValidationCheck
from .error_intel import ErrorIntelligence, ErrorAnalysis, ErrorSuggestion

__all__ = [
    "SchemaDetector",
    "DetectedSchema",
    "ColumnInfo",
    "RepeatingGroup",
    "FlattenEngine",
    "FlattenResult",
    "FlattenedTable",
    "ExecutionIntelligence",
    "ExecutionConfig",
    "ExecutionPlan",
    "RetryStrategy",
    "SchemaContractStore",
    "SchemaContract",
    "SchemaDrift",
    "ContractValidation",
    "PreValidator",
    "PreValidationResult",
    "ValidationCheck",
    "ErrorIntelligence",
    "ErrorAnalysis",
    "ErrorSuggestion",
]
