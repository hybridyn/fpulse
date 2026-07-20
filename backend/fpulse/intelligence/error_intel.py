"""
Error Intelligence Layer -- smart error analysis after execution failures.

Parses DuckDB errors, categorizes them, provides friendly messages,
suggests fixes using fuzzy matching, and offers auto-fix capability
for simple cases.
"""

from __future__ import annotations

import difflib
import os
import re
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ErrorSuggestion(BaseModel):
    """A single fix suggestion for an error."""
    suggestion: str
    confidence: float  # 0.0 - 1.0
    action_type: str  # rename_column, create_table, change_type, add_column, fix_path, fix_syntax, fix_extension
    action_params: dict[str, Any] = Field(default_factory=dict)


class ErrorAnalysis(BaseModel):
    """Complete analysis of a pipeline execution error."""
    original_error: str
    error_category: str  # missing_object, schema_mismatch, permission, syntax, connection, data_type, timeout, unknown
    human_message: str  # Clean, friendly error message
    suggestions: list[ErrorSuggestion] = Field(default_factory=list)
    auto_fix_available: bool = False
    auto_fix_action: str | None = None  # What the auto-fix would do


# ---------------------------------------------------------------------------
# Error pattern matchers
# ---------------------------------------------------------------------------

# DuckDB error patterns: (regex, category, human_message_template)
_ERROR_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    # File not found
    (
        re.compile(r"No files found that match the pattern[:\s]*['\"]?([^'\"]+)['\"]?", re.IGNORECASE),
        "missing_object",
        "File not found: {match}",
    ),
    (
        re.compile(r"Could not open file[:\s]*['\"]?([^'\"]+)['\"]?", re.IGNORECASE),
        "missing_object",
        "Cannot open file: {match}",
    ),
    (
        re.compile(r"File not found[:\s]*['\"]?([^'\"]+)['\"]?", re.IGNORECASE),
        "missing_object",
        "File not found: {match}",
    ),

    # Column not found
    (
        re.compile(r"Referenced column ['\"]?(\w+)['\"]? not found", re.IGNORECASE),
        "schema_mismatch",
        'Column "{match}" does not exist in the input data',
    ),
    (
        re.compile(r"Binder Error:.*column ['\"]?(\w+)['\"]?.*not found", re.IGNORECASE),
        "schema_mismatch",
        'Column "{match}" not found. Check your column name spelling.',
    ),
    (
        re.compile(r'column "(\w+)" must appear in the GROUP BY', re.IGNORECASE),
        "schema_mismatch",
        'Column "{match}" is used in SELECT but not in GROUP BY',
    ),
    (
        re.compile(r"could not find a column named ['\"]?(\w+)['\"]?", re.IGNORECASE),
        "schema_mismatch",
        'Column "{match}" not found in the dataset',
    ),

    # Table not found
    (
        re.compile(r"Table[:\s]+['\"]?(\w+)['\"]?[:\s]+does not exist", re.IGNORECASE),
        "missing_object",
        'Table "{match}" does not exist',
    ),
    (
        re.compile(r"Catalog Error:.*Table.*['\"]?(\w+)['\"]?.*not found", re.IGNORECASE),
        "missing_object",
        'Table "{match}" not found in the catalog',
    ),

    # SQL syntax errors
    (
        re.compile(r"Parser Error: syntax error at or near ['\"]?(\S+)['\"]?", re.IGNORECASE),
        "syntax",
        'SQL syntax error near "{match}"',
    ),
    (
        re.compile(r"Parser Error:(.+?)(?:\n|$)", re.IGNORECASE),
        "syntax",
        "SQL syntax error: {match}",
    ),

    # Type casting errors
    (
        re.compile(r"Conversion Error:.*Cannot cast.*['\"]?(\S+)['\"]?.*to.*['\"]?(\S+)['\"]?", re.IGNORECASE),
        "data_type",
        "Cannot convert value to the expected type",
    ),
    (
        re.compile(r"Could not cast value ['\"]?(.+?)['\"]? to type ['\"]?(\w+)['\"]?", re.IGNORECASE),
        "data_type",
        'Cannot cast "{match}" to the target type',
    ),
    (
        re.compile(r"Unimplemented type for cast.*?(\w+).*?(\w+)", re.IGNORECASE),
        "data_type",
        "Type cast not supported between these types",
    ),

    # Permission errors
    (
        re.compile(r"Permission denied.*?['\"]?([^'\"]+)['\"]?", re.IGNORECASE),
        "permission",
        "Permission denied: {match}",
    ),
    (
        re.compile(r"Access denied|Unauthorized|Forbidden", re.IGNORECASE),
        "permission",
        "Access denied. Check file or database permissions.",
    ),

    # Connection errors
    (
        re.compile(r"Connection refused|Could not connect|Connection timed out", re.IGNORECASE),
        "connection",
        "Failed to connect to the data source. Check if the service is running.",
    ),

    # Timeout
    (
        re.compile(r"timed? ?out|execution timeout|query timeout", re.IGNORECASE),
        "timeout",
        "Operation timed out. Try reducing the data volume or increasing the timeout.",
    ),

    # Out of memory
    (
        re.compile(r"Out of Memory|memory limit|insufficient memory", re.IGNORECASE),
        "resource",
        "Out of memory. Try processing the data in smaller batches.",
    ),

    # Division by zero
    (
        re.compile(r"division by zero|divide by zero", re.IGNORECASE),
        "data_type",
        "Division by zero encountered. Add a NULLIF or CASE check.",
    ),

    # Ambiguous column
    (
        re.compile(r"Binder Error:.*ambiguous.*column.*['\"]?(\w+)['\"]?", re.IGNORECASE),
        "schema_mismatch",
        'Column "{match}" is ambiguous -- it exists in multiple inputs. Use a table prefix.',
    ),
]


# ---------------------------------------------------------------------------
# ErrorIntelligence
# ---------------------------------------------------------------------------

class ErrorIntelligence:
    """Provides smart error analysis with suggestions and auto-fix capabilities."""

    def __init__(self, data_dir: str = "."):
        self.data_dir = data_dir

    def analyze(
        self,
        error: str,
        step_id: str | None = None,
        step_type: str | None = None,
        step_params: dict[str, Any] | None = None,
        available_columns: list[str] | None = None,
    ) -> ErrorAnalysis:
        """Analyze an error and return categorized analysis with suggestions.

        Args:
            error: The raw error message string.
            step_id: Optional step ID for context.
            step_type: Optional step type for context.
            step_params: Optional step params for context-aware suggestions.
            available_columns: Optional list of actual available columns for fuzzy matching.
        """
        error_str = str(error).strip()
        category = "unknown"
        human_message = error_str
        suggestions: list[ErrorSuggestion] = []
        auto_fix_available = False
        auto_fix_action: str | None = None

        # Match against known patterns
        for pattern, cat, msg_template in _ERROR_PATTERNS:
            m = pattern.search(error_str)
            if m:
                category = cat
                match_val = m.group(1) if m.lastindex and m.lastindex >= 1 else ""
                human_message = msg_template.replace("{match}", match_val.strip())
                break

        # Generate suggestions based on category
        if category == "missing_object":
            suggestions.extend(
                self._suggest_missing_object(error_str, step_params)
            )
            # Check for auto-fix: wrong extension
            if suggestions and suggestions[0].action_type == "fix_extension":
                auto_fix_available = True
                auto_fix_action = suggestions[0].suggestion

        elif category == "schema_mismatch":
            suggestions.extend(
                self._suggest_schema_fix(error_str, available_columns, step_params)
            )
            if suggestions and suggestions[0].confidence >= 0.8:
                auto_fix_available = True
                auto_fix_action = suggestions[0].suggestion

        elif category == "syntax":
            suggestions.extend(self._suggest_syntax_fix(error_str, step_params))

        elif category == "data_type":
            suggestions.extend(self._suggest_type_fix(error_str))

        elif category == "permission":
            suggestions.append(ErrorSuggestion(
                suggestion="Check file permissions or run F-Pulse with appropriate access rights",
                confidence=0.7,
                action_type="fix_permission",
                action_params={},
            ))

        elif category == "connection":
            suggestions.append(ErrorSuggestion(
                suggestion="Verify the database/service is running and the connection string is correct",
                confidence=0.7,
                action_type="fix_connection",
                action_params={},
            ))

        elif category == "timeout":
            suggestions.extend([
                ErrorSuggestion(
                    suggestion="Reduce the dataset size with a LIMIT or SAMPLE node before this step",
                    confidence=0.6,
                    action_type="add_sample",
                    action_params={"sample_size": 100000},
                ),
                ErrorSuggestion(
                    suggestion="Increase the execution timeout in pipeline settings",
                    confidence=0.5,
                    action_type="increase_timeout",
                    action_params={"timeout_ms": 600000},
                ),
            ])

        elif category == "resource":
            suggestions.append(ErrorSuggestion(
                suggestion="Process data in smaller batches using the Sample node or increase system memory",
                confidence=0.7,
                action_type="add_sample",
                action_params={"sample_size": 50000},
            ))

        # If no suggestions generated, provide a generic one
        if not suggestions:
            suggestions.append(ErrorSuggestion(
                suggestion="Review the error message and check your node configuration",
                confidence=0.3,
                action_type="manual_review",
                action_params={},
            ))

        return ErrorAnalysis(
            original_error=error_str,
            error_category=category,
            human_message=human_message,
            suggestions=suggestions,
            auto_fix_available=auto_fix_available,
            auto_fix_action=auto_fix_action,
        )

    # ------------------------------------------------------------------
    # Missing object suggestions
    # ------------------------------------------------------------------

    def _suggest_missing_object(
        self, error: str, step_params: dict[str, Any] | None,
    ) -> list[ErrorSuggestion]:
        suggestions: list[ErrorSuggestion] = []

        # Extract the missing file path from the error
        path_match = re.search(
            r"(?:not found|open file|match the pattern)[:\s]*['\"]?([^\s'\"]+)['\"]?",
            error, re.IGNORECASE,
        )
        missing_path = path_match.group(1) if path_match else None

        if not missing_path and step_params:
            missing_path = step_params.get("file_path", "")

        if missing_path:
            # Fuzzy match files in data_dir
            file_suggestions = self._find_similar_files(missing_path)
            for fname, score in file_suggestions:
                suggestions.append(ErrorSuggestion(
                    suggestion=f'Use "{fname}" instead of "{os.path.basename(missing_path)}"',
                    confidence=score,
                    action_type="fix_path",
                    action_params={"original": missing_path, "suggested": fname},
                ))

            # Check for wrong extension
            basename = os.path.basename(missing_path)
            name_no_ext = os.path.splitext(basename)[0]
            current_ext = os.path.splitext(basename)[1].lower()

            if os.path.isdir(self.data_dir):
                for f in os.listdir(self.data_dir):
                    f_name, f_ext = os.path.splitext(f)
                    if f_name.lower() == name_no_ext.lower() and f_ext.lower() != current_ext:
                        suggestions.insert(0, ErrorSuggestion(
                            suggestion=f'File exists with different extension: "{f}" (change {current_ext} to {f_ext})',
                            confidence=0.95,
                            action_type="fix_extension",
                            action_params={"original": basename, "corrected": f},
                        ))
                        break

            # If no CSV found, check if file needs to be uploaded
            if not suggestions:
                suggestions.append(ErrorSuggestion(
                    suggestion=f'Upload the file "{os.path.basename(missing_path)}" to the data directory',
                    confidence=0.5,
                    action_type="upload_file",
                    action_params={"file_name": os.path.basename(missing_path)},
                ))

        return suggestions

    # ------------------------------------------------------------------
    # Schema mismatch suggestions
    # ------------------------------------------------------------------

    def _suggest_schema_fix(
        self,
        error: str,
        available_columns: list[str] | None,
        step_params: dict[str, Any] | None,
    ) -> list[ErrorSuggestion]:
        suggestions: list[ErrorSuggestion] = []

        # Extract the missing column name
        col_match = re.search(
            r'(?:column|Referenced column)\s+["\']?(\w+)["\']?',
            error, re.IGNORECASE,
        )
        missing_col = col_match.group(1) if col_match else None

        if missing_col and available_columns:
            # Fuzzy match against available columns
            matches = difflib.get_close_matches(missing_col, available_columns, n=3, cutoff=0.4)
            for i, match in enumerate(matches):
                # Compute a rough similarity score
                ratio = difflib.SequenceMatcher(None, missing_col.lower(), match.lower()).ratio()
                suggestions.append(ErrorSuggestion(
                    suggestion=f'Replace column "{missing_col}" with "{match}"',
                    confidence=round(ratio, 2),
                    action_type="rename_column",
                    action_params={
                        "original": missing_col,
                        "suggested": match,
                        "step_params": step_params or {},
                    },
                ))

            if not matches:
                suggestions.append(ErrorSuggestion(
                    suggestion=f'Column "{missing_col}" not found. Available columns: {", ".join(available_columns[:10])}',
                    confidence=0.4,
                    action_type="manual_review",
                    action_params={"missing_column": missing_col, "available": available_columns[:20]},
                ))

        elif missing_col:
            suggestions.append(ErrorSuggestion(
                suggestion=f'Column "{missing_col}" not found. Check the upstream node output for available columns.',
                confidence=0.5,
                action_type="manual_review",
                action_params={"missing_column": missing_col},
            ))

        # GROUP BY suggestion
        if "must appear in the GROUP BY" in error and missing_col:
            suggestions.insert(0, ErrorSuggestion(
                suggestion=f'Add "{missing_col}" to the GROUP BY clause, or use an aggregate function on it',
                confidence=0.9,
                action_type="add_group_by",
                action_params={"column": missing_col},
            ))

        # Ambiguous column
        if "ambiguous" in error.lower() and missing_col:
            suggestions.insert(0, ErrorSuggestion(
                suggestion=f'Column "{missing_col}" exists in multiple inputs. Prefix with table name (e.g., left.{missing_col})',
                confidence=0.85,
                action_type="qualify_column",
                action_params={"column": missing_col},
            ))

        return suggestions

    # ------------------------------------------------------------------
    # Syntax error suggestions
    # ------------------------------------------------------------------

    def _suggest_syntax_fix(
        self, error: str, step_params: dict[str, Any] | None,
    ) -> list[ErrorSuggestion]:
        suggestions: list[ErrorSuggestion] = []

        # Extract the problematic token
        token_match = re.search(r'near\s+["\']?(\S+)["\']?', error, re.IGNORECASE)
        token = token_match.group(1) if token_match else None

        if token:
            suggestions.append(ErrorSuggestion(
                suggestion=f'Check the SQL near "{token}" -- this is where the parser failed',
                confidence=0.7,
                action_type="fix_syntax",
                action_params={"near_token": token},
            ))

        # Common syntax mistakes
        if step_params:
            expression = step_params.get("condition", "") or step_params.get("expression", "")

            # Missing quotes around string literals
            if re.search(r"=\s*\w+[^'\"]", expression):
                suggestions.append(ErrorSuggestion(
                    suggestion="Wrap string values in single quotes (e.g., status = 'active' not status = active)",
                    confidence=0.6,
                    action_type="fix_syntax",
                    action_params={"hint": "quote_strings"},
                ))

            # Using == instead of =
            if "==" in expression:
                fixed = expression.replace("==", "=")
                suggestions.insert(0, ErrorSuggestion(
                    suggestion='Use single "=" for comparison in SQL (not "==")',
                    confidence=0.95,
                    action_type="fix_syntax",
                    action_params={"original": expression, "fixed": fixed},
                ))

            # Using != instead of <>
            if "!=" in expression:
                suggestions.append(ErrorSuggestion(
                    suggestion='SQL uses "<>" for not-equal (though "!=" works in DuckDB)',
                    confidence=0.4,
                    action_type="fix_syntax",
                    action_params={"hint": "not_equal_operator"},
                ))

        return suggestions

    # ------------------------------------------------------------------
    # Type error suggestions
    # ------------------------------------------------------------------

    def _suggest_type_fix(self, error: str) -> list[ErrorSuggestion]:
        suggestions: list[ErrorSuggestion] = []

        # Extract source and target types
        cast_match = re.search(
            r"cast.*?['\"]?(\w+)['\"]?.*?to.*?['\"]?(\w+)['\"]?",
            error, re.IGNORECASE,
        )

        if cast_match:
            source_val = cast_match.group(1)
            target_type = cast_match.group(2)
            suggestions.append(ErrorSuggestion(
                suggestion=f'Use TRY_CAST({source_val} AS {target_type}) to handle conversion errors gracefully',
                confidence=0.8,
                action_type="change_type",
                action_params={"use_try_cast": True, "target_type": target_type},
            ))

        # Division by zero
        if "division by zero" in error.lower():
            suggestions.append(ErrorSuggestion(
                suggestion="Wrap divisor with NULLIF(column, 0) to avoid division by zero",
                confidence=0.9,
                action_type="fix_syntax",
                action_params={"hint": "nullif_division"},
            ))

        # Generic type suggestion
        if not suggestions:
            suggestions.append(ErrorSuggestion(
                suggestion="Add a Typecast node before this step to convert data types, or use CAST() in your expression",
                confidence=0.5,
                action_type="add_typecast",
                action_params={},
            ))

        return suggestions

    # ------------------------------------------------------------------
    # Fuzzy file matching
    # ------------------------------------------------------------------

    def _find_similar_files(self, missing_path: str) -> list[tuple[str, float]]:
        """Find files in data_dir similar to the missing path.

        Returns list of (filename, confidence_score) tuples sorted by score descending.
        """
        if not os.path.isdir(self.data_dir):
            return []

        available_files: list[str] = []
        for f in os.listdir(self.data_dir):
            ext = os.path.splitext(f)[1].lower()
            if ext in {".csv", ".json", ".parquet", ".tsv", ".txt"}:
                available_files.append(f)

        if not available_files:
            return []

        basename = os.path.basename(missing_path)
        matches = difflib.get_close_matches(basename, available_files, n=5, cutoff=0.3)

        results: list[tuple[str, float]] = []
        for match in matches:
            ratio = difflib.SequenceMatcher(None, basename.lower(), match.lower()).ratio()
            results.append((match, round(ratio, 2)))

        return sorted(results, key=lambda x: x[1], reverse=True)
