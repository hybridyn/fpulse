"""Folder models — nested grouping inside a project."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field


class Folder(BaseModel):
    """A folder nests pipelines (and other folders) inside a project.

    Tree shape: a folder has exactly one project_id and an optional
    parent_folder_id. parent_folder_id=None means the folder sits at
    the project root. Depth is unbounded; cycle prevention is enforced
    by the API on create/move.
    """
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str
    description: str = ""
    project_id: str
    parent_folder_id: str | None = None
    workspace_id: str = "default"
    color: str = "#94a3b8"
    icon: str = "folder"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class FolderCreate(BaseModel):
    name: str
    project_id: str
    parent_folder_id: str | None = None
    description: str = ""
    color: str = "#94a3b8"
    icon: str = "folder"


class FolderUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    parent_folder_id: str | None = None
    color: str | None = None
    icon: str | None = None
