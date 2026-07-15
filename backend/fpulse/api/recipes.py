"""Recipe API — V2 round 1 (2026-05-26).

CRUD over the new `Recipe` IR model. A Recipe is a named, reusable
transform sequence — the standalone form of the steps a data_wrangler
node carries inline. Saving a wrangler's steps as a recipe lets the
same sequence be reused across multiple pipelines (and by File Data
Prep one-shot loads) without copy-pasting.

Round 1 (this commit) ships:
  - In-memory store at module level (similar to deployments.py)
  - GET    /api/recipes                  list (workspace-scoped)
  - POST   /api/recipes                  create
  - GET    /api/recipes/{id}             fetch one
  - PUT    /api/recipes/{id}             update fields
  - DELETE /api/recipes/{id}             drop
  - POST   /api/recipes/{id}/clone       fork a copy (name suffix " (copy)")
  - GET    /api/recipes/{id}/used-by     list pipelines that reference this recipe

Persistence (SQLite-backed store) + frontend Recipe picker land in
follow-up commits.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from fpulse.auth.deps import current_workspace_id
from fpulse.ir.schema import Recipe, RecipeStep

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/recipes", tags=["recipes"])


def _safe_workspace_id(request: Request) -> str:
    try:
        return current_workspace_id(request)
    except HTTPException:
        raise
    except Exception:
        return "default"


# ── Persistent store (mirrors deployments.py pattern) ────────────────


_RECIPES: dict[str, Recipe] = {}
_LOADED_FROM_DISK = False


def _data_dir() -> Path:
    try:
        from fpulse.main import app_state
        return Path(app_state.get("data_dir") or os.environ.get("FPULSE_DATA_DIR") or ".")
    except Exception:
        return Path(os.environ.get("FPULSE_DATA_DIR") or ".")


def _store_path() -> Path:
    return _data_dir() / "recipes.json"


def _load_from_disk() -> None:
    global _LOADED_FROM_DISK
    if _LOADED_FROM_DISK:
        return
    path = _store_path()
    if path.is_file():
        try:
            with path.open("r", encoding="utf-8") as f:
                blob = json.load(f)
            for raw in blob.get("recipes", []):
                try:
                    r = Recipe(**raw)
                    _RECIPES[r.id] = r
                except Exception as exc:  # noqa: BLE001
                    logger.warning("recipes.json: skipped malformed row: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("recipes.json: load failed (%s) — starting empty", exc)
    _LOADED_FROM_DISK = True


def _persist_to_disk() -> None:
    path = _store_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(
                {"version": 1, "recipes": [r.model_dump(mode="json") for r in _RECIPES.values()]},
                f,
                indent=2,
                default=str,
            )
        tmp.replace(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("recipes.json: persist failed (%s) — in-memory state diverged", exc)


def _filter_by_workspace(workspace_id: str) -> list[Recipe]:
    _load_from_disk()
    return [r for r in _RECIPES.values() if r.workspace_id == workspace_id]


def _name_in_use(workspace_id: str, name: str, exclude_id: str | None = None) -> bool:
    for r in _filter_by_workspace(workspace_id):
        if r.name == name and r.id != exclude_id:
            return True
    return False


# ── Request bodies ────────────────────────────────────────────────────


class CreateRecipeRequest(BaseModel):
    name: str
    description: str = ""
    steps: list[RecipeStep] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class UpdateRecipeRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    steps: list[RecipeStep] | None = None
    tags: list[str] | None = None


# ── Endpoints ─────────────────────────────────────────────────────────


@router.get("")
def list_recipes(workspace_id: str = Depends(_safe_workspace_id)):
    """List recipes in this workspace, newest first."""
    items = _filter_by_workspace(workspace_id)
    items.sort(key=lambda r: r.created_at, reverse=True)
    return {
        "recipes": [r.model_dump(mode="json") for r in items],
        "count": len(items),
    }


@router.post("")
def create_recipe(
    body: CreateRecipeRequest,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Create a new recipe. Name must be unique per workspace."""
    if not body.name.strip():
        raise HTTPException(400, "name is required")
    if _name_in_use(workspace_id, body.name):
        raise HTTPException(
            409,
            f"A recipe named '{body.name}' already exists in this workspace. "
            "Rename, or use the clone endpoint to fork.",
        )
    recipe = Recipe(
        workspace_id=workspace_id,
        name=body.name,
        description=body.description,
        steps=body.steps,
        tags=body.tags,
    )
    _RECIPES[recipe.id] = recipe
    _persist_to_disk()
    logger.info("recipe created id=%s name=%s steps=%d", recipe.id, recipe.name, len(recipe.steps))
    return recipe.model_dump(mode="json")


@router.get("/{recipe_id}")
def get_recipe(
    recipe_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    _load_from_disk()
    r = _RECIPES.get(recipe_id)
    if not r or r.workspace_id != workspace_id:
        raise HTTPException(404, "recipe not found")
    return r.model_dump(mode="json")


@router.put("/{recipe_id}")
def update_recipe(
    recipe_id: str,
    body: UpdateRecipeRequest,
    workspace_id: str = Depends(_safe_workspace_id),
):
    r = _RECIPES.get(recipe_id)
    if not r or r.workspace_id != workspace_id:
        raise HTTPException(404, "recipe not found")
    updates = body.model_dump(exclude_none=True)
    if "name" in updates:
        if not updates["name"].strip():
            raise HTTPException(400, "name cannot be empty")
        if _name_in_use(workspace_id, updates["name"], exclude_id=recipe_id):
            raise HTTPException(
                409,
                f"A recipe named '{updates['name']}' already exists in this workspace.",
            )
    for k, v in updates.items():
        setattr(r, k, v)
    r.updated_at = datetime.now(timezone.utc)
    _persist_to_disk()
    return r.model_dump(mode="json")


@router.delete("/{recipe_id}")
def delete_recipe(
    recipe_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    r = _RECIPES.get(recipe_id)
    if not r or r.workspace_id != workspace_id:
        raise HTTPException(404, "recipe not found")
    # Refuse delete if pipelines reference this recipe — the same
    # protection pattern managed-table drop uses. Forces operator to
    # detach first or clone-and-edit elsewhere.
    used_by = _find_used_by(workspace_id, recipe_id)
    if used_by:
        raise HTTPException(
            409,
            f"{len(used_by)} pipeline{'s' if len(used_by) != 1 else ''} reference this recipe — detach first.",
        )
    del _RECIPES[recipe_id]
    _persist_to_disk()
    return {"deleted": True, "id": recipe_id}


@router.post("/{recipe_id}/clone")
def clone_recipe(
    recipe_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Fork a recipe under a new id with name '<original> (copy)'."""
    src = _RECIPES.get(recipe_id)
    if not src or src.workspace_id != workspace_id:
        raise HTTPException(404, "recipe not found")
    base_name = f"{src.name} (copy)"
    name = base_name
    n = 2
    while _name_in_use(workspace_id, name):
        name = f"{base_name} {n}"
        n += 1
    clone = Recipe(
        workspace_id=workspace_id,
        name=name,
        description=src.description,
        steps=[RecipeStep(**s.model_dump()) for s in src.steps],
        tags=list(src.tags),
    )
    _RECIPES[clone.id] = clone
    _persist_to_disk()
    return clone.model_dump(mode="json")


def _find_used_by(workspace_id: str, recipe_id: str) -> list[dict[str, Any]]:
    """Return pipelines that reference this recipe id.

    Looks for any step.params.recipe_id == recipe_id across the
    workspace's workflows. Computed on demand — no separate index.
    Cheap up to a few hundred workflows; larger installs will want a
    materialised reverse-index in a follow-up.
    """
    try:
        from fpulse.main import app_state
    except Exception:
        return []
    store = app_state.get("store")
    if store is None:
        return []
    try:
        wfs = store.list_all(workspace_id=workspace_id)
    except Exception:
        return []
    matches: list[dict[str, Any]] = []
    for wf in wfs or []:
        wf_id = wf.get("id") if isinstance(wf, dict) else getattr(wf, "id", None)
        wf_name = wf.get("name") if isinstance(wf, dict) else getattr(wf, "name", None)
        steps = wf.get("steps") if isinstance(wf, dict) else getattr(wf, "steps", [])
        for st in steps or []:
            params = st.get("params") if isinstance(st, dict) else getattr(st, "params", {}) or {}
            if isinstance(params, dict) and params.get("recipe_id") == recipe_id:
                matches.append({"id": wf_id, "name": wf_name})
                break
    return matches


@router.get("/{recipe_id}/used-by")
def used_by(
    recipe_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    r = _RECIPES.get(recipe_id)
    if not r or r.workspace_id != workspace_id:
        raise HTTPException(404, "recipe not found")
    return {
        "recipe_id": recipe_id,
        "pipelines": _find_used_by(workspace_id, recipe_id),
    }
