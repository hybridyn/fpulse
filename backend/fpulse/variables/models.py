"""Variable models — global and project-scoped key-value pairs."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


class Variable(BaseModel):
    """A variable stores a key-value pair, scoped globally or to a project.

    Note on "global": the scope literal "global" means "visible to
    every project WITHIN ONE workspace", NOT across workspaces. A
    variable called PROD_DB_URL in workspace A is invisible to
    workspace B even when both are marked scope=global. Legacy rows
    back-filled to 'default' by v11.
    """
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    key: str
    value: str
    type: Literal["string", "secret", "number", "boolean"] = "string"
    scope: Literal["global", "project"] = "global"
    project_id: str = ""
    workspace_id: str = "default"
    description: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class VariableCreate(BaseModel):
    key: str
    value: str
    type: Literal["string", "secret", "number", "boolean"] = "string"
    scope: Literal["global", "project"] = "global"
    project_id: str = ""
    description: str = ""


class VariableUpdate(BaseModel):
    key: str | None = None
    value: str | None = None
    type: Literal["string", "secret", "number", "boolean"] | None = None
    scope: Literal["global", "project"] | None = None
    project_id: str | None = None
    description: str | None = None
