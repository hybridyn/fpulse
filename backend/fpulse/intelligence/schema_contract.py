"""
SQLite-backed Schema Contract System — define, validate, and detect drift in data schemas.

Contracts pin down the expected schema at each step output. When the actual
schema drifts (columns added, removed, type changed), the system flags it
with severity levels: breaking, warning, or info.
"""

from __future__ import annotations

import json
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ExpectedColumn(BaseModel):
    """A single column expectation within a schema contract."""
    name: str
    type: str  # e.g. "VARCHAR", "INTEGER", "DOUBLE", "BOOLEAN", "TIMESTAMP"
    nullable: bool = True
    constraints: dict[str, Any] | None = None  # min, max, pattern, unique, etc.


class SchemaContract(BaseModel):
    """A schema contract pinned to a specific step output.

    Tenant-boundary note: a contract inherits its workspace from the
    parent workflow at create time. Contracts cannot be moved between
    workspaces — if a workflow is deleted, its contracts should be
    deleted with it.
    """
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:10])
    workflow_id: str
    workspace_id: str = "default"
    step_id: str
    expected_columns: list[ExpectedColumn]
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_validated: datetime | None = None
    status: str = "active"  # "active", "violated", "pending"
    description: str = ""


class SchemaDrift(BaseModel):
    """A single detected drift between expected and actual schema."""
    drift_type: str  # "column_added", "column_removed", "type_changed", "nullable_changed"
    column_name: str
    expected: str | None = None
    actual: str | None = None
    severity: str = "info"  # "breaking", "warning", "info"


class ContractValidation(BaseModel):
    """Result of validating actual data against a schema contract."""
    contract_id: str
    valid: bool
    drifts: list[SchemaDrift]
    validated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    actual_columns: list[dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Type compatibility matrix
# ---------------------------------------------------------------------------

_COMPATIBLE_TYPES: dict[str, set[str]] = {
    "VARCHAR": {"VARCHAR", "TEXT", "STRING", "CHAR"},
    "TEXT": {"VARCHAR", "TEXT", "STRING", "CHAR"},
    "STRING": {"VARCHAR", "TEXT", "STRING", "CHAR"},
    "INTEGER": {"INTEGER", "INT", "BIGINT", "SMALLINT", "TINYINT", "INT64", "INT32"},
    "INT": {"INTEGER", "INT", "BIGINT", "SMALLINT", "TINYINT", "INT64", "INT32"},
    "BIGINT": {"BIGINT", "INT64"},
    "DOUBLE": {"DOUBLE", "FLOAT", "REAL", "DECIMAL", "NUMERIC"},
    "FLOAT": {"DOUBLE", "FLOAT", "REAL"},
    "BOOLEAN": {"BOOLEAN", "BOOL"},
    "TIMESTAMP": {"TIMESTAMP", "DATETIME", "TIMESTAMP WITH TIME ZONE"},
    "DATE": {"DATE"},
    "BLOB": {"BLOB", "BYTEA"},
}


def _types_compatible(expected: str, actual: str) -> bool:
    e = expected.upper().strip()
    a = actual.upper().strip()
    if e == a:
        return True
    compatible = _COMPATIBLE_TYPES.get(e, set())
    return a in compatible


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class SchemaContractStore:
    """Schema contract store backed by SQLite."""

    def __init__(self, db=None):
        self._db = db

    def set_db(self, db):
        self._db = db

    def _save(self, contract: SchemaContract):
        data = contract.model_dump(mode="json")
        self._db.insert_json(
            "schema_contracts", contract.id, data,
            workflow_id=contract.workflow_id,
            workspace_id=contract.workspace_id or "default",
            step_id=contract.step_id,
            created_at=contract.created_at.isoformat(),
            updated_at=contract.updated_at.isoformat(),
        )

    def create_contract(
        self,
        workflow_id: str,
        step_id: str,
        columns: list[dict[str, Any]],
        description: str = "",
        workspace_id: str = "default",
    ) -> SchemaContract:
        expected_columns = []
        for col in columns:
            expected_columns.append(ExpectedColumn(
                name=col["name"],
                type=col.get("type", "VARCHAR"),
                nullable=col.get("nullable", True),
                constraints=col.get("constraints"),
            ))

        contract = SchemaContract(
            workflow_id=workflow_id,
            workspace_id=workspace_id or "default",
            step_id=step_id,
            expected_columns=expected_columns,
            description=description,
        )
        self._save(contract)
        return contract

    def get_contract(
        self,
        contract_id: str,
        workspace_id: str | None = None,
    ) -> SchemaContract | None:
        data = self._db.get_json("schema_contracts", contract_id)
        if data is None:
            return None
        if workspace_id is not None:
            if (data.get("workspace_id") or "default") != workspace_id:
                return None
        return SchemaContract(**data)

    def list_contracts(
        self,
        workflow_id: str,
        workspace_id: str | None = None,
    ) -> list[SchemaContract]:
        if workspace_id is not None:
            items = self._db.list_json(
                "schema_contracts",
                "workflow_id = ? AND workspace_id = ?",
                (workflow_id, workspace_id),
            )
        else:
            items = self._db.list_json(
                "schema_contracts", "workflow_id = ?", (workflow_id,)
            )
        return [SchemaContract(**d) for d in items]

    def list_contracts_for_step(
        self,
        workflow_id: str,
        step_id: str,
        workspace_id: str | None = None,
    ) -> list[SchemaContract]:
        if workspace_id is not None:
            items = self._db.list_json(
                "schema_contracts",
                "workflow_id = ? AND step_id = ? AND workspace_id = ?",
                (workflow_id, step_id, workspace_id),
            )
        else:
            items = self._db.list_json(
                "schema_contracts",
                "workflow_id = ? AND step_id = ?",
                (workflow_id, step_id),
            )
        return [SchemaContract(**d) for d in items]

    def update_contract(
        self,
        contract_id: str,
        columns: list[dict[str, Any]] | None = None,
        description: str | None = None,
        status: str | None = None,
        workspace_id: str | None = None,
    ) -> SchemaContract | None:
        contract = self.get_contract(contract_id, workspace_id=workspace_id)
        if not contract:
            return None

        if columns is not None:
            contract.expected_columns = [
                ExpectedColumn(
                    name=col["name"],
                    type=col.get("type", "VARCHAR"),
                    nullable=col.get("nullable", True),
                    constraints=col.get("constraints"),
                )
                for col in columns
            ]
        if description is not None:
            contract.description = description
        if status is not None:
            contract.status = status

        contract.updated_at = datetime.now(timezone.utc)
        self._save(contract)
        return contract

    def delete_contract(
        self,
        contract_id: str,
        workspace_id: str | None = None,
    ) -> bool:
        if workspace_id is not None:
            if not self.get_contract(contract_id, workspace_id=workspace_id):
                return False
        return self._db.delete_row("schema_contracts", contract_id)

    def validate_against(
        self,
        contract_id: str,
        actual_schema: list[dict[str, Any]],
        workspace_id: str | None = None,
    ) -> ContractValidation:
        contract = self.get_contract(contract_id, workspace_id=workspace_id)
        if not contract:
            return ContractValidation(
                contract_id=contract_id,
                valid=False,
                drifts=[SchemaDrift(
                    drift_type="contract_missing",
                    column_name="*",
                    severity="breaking",
                    expected="contract exists",
                    actual="contract not found",
                )],
            )

        drifts = self._detect_drifts(contract.expected_columns, actual_schema)
        valid = not any(d.severity == "breaking" for d in drifts)

        # Update contract state
        contract.last_validated = datetime.now(timezone.utc)
        contract.status = "active" if valid else "violated"
        self._save(contract)

        return ContractValidation(
            contract_id=contract_id,
            valid=valid,
            drifts=drifts,
            actual_columns=actual_schema,
        )

    def detect_drift(
        self,
        contract_id: str,
        actual_schema: list[dict[str, Any]],
        workspace_id: str | None = None,
    ) -> list[SchemaDrift]:
        contract = self.get_contract(contract_id, workspace_id=workspace_id)
        if not contract:
            return [SchemaDrift(
                drift_type="contract_missing",
                column_name="*",
                severity="breaking",
                expected="contract exists",
                actual="contract not found",
            )]
        return self._detect_drifts(contract.expected_columns, actual_schema)

    def auto_create_from_schema(
        self,
        workflow_id: str,
        step_id: str,
        actual_schema: list[dict[str, Any]],
        description: str = "",
        workspace_id: str = "default",
    ) -> SchemaContract:
        columns = []
        for col in actual_schema:
            columns.append({
                "name": col.get("name", "unknown"),
                "type": col.get("type", "VARCHAR"),
                "nullable": col.get("nullable", True),
                "constraints": col.get("constraints"),
            })

        return self.create_contract(
            workflow_id=workflow_id,
            step_id=step_id,
            columns=columns,
            description=description or f"Auto-created from step {step_id} output",
            workspace_id=workspace_id,
        )

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _detect_drifts(
        self,
        expected: list[ExpectedColumn],
        actual_schema: list[dict[str, Any]],
    ) -> list[SchemaDrift]:
        drifts: list[SchemaDrift] = []

        expected_map = {col.name.lower(): col for col in expected}
        actual_map = {
            col.get("name", "").lower(): col
            for col in actual_schema
        }

        for name, exp_col in expected_map.items():
            if name not in actual_map:
                drifts.append(SchemaDrift(
                    drift_type="column_removed",
                    column_name=exp_col.name,
                    expected=exp_col.type,
                    actual=None,
                    severity="breaking",
                ))
                continue

            actual_col = actual_map[name]

            actual_type = actual_col.get("type", "VARCHAR")
            if not _types_compatible(exp_col.type, actual_type):
                drifts.append(SchemaDrift(
                    drift_type="type_changed",
                    column_name=exp_col.name,
                    expected=exp_col.type,
                    actual=actual_type,
                    severity="breaking",
                ))

            actual_nullable = actual_col.get("nullable", True)
            if exp_col.nullable != actual_nullable:
                severity = "warning" if actual_nullable and not exp_col.nullable else "info"
                drifts.append(SchemaDrift(
                    drift_type="nullable_changed",
                    column_name=exp_col.name,
                    expected=str(exp_col.nullable),
                    actual=str(actual_nullable),
                    severity=severity,
                ))

        for name, actual_col in actual_map.items():
            if name not in expected_map:
                drifts.append(SchemaDrift(
                    drift_type="column_added",
                    column_name=actual_col.get("name", name),
                    expected=None,
                    actual=actual_col.get("type", "VARCHAR"),
                    severity="info",
                ))

        return drifts
