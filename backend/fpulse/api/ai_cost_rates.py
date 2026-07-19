"""Workspace-scoped AI cost-rate table — read/write API.

GET  /api/v1/ai/cost-rates              — effective rates (defaults + overrides)
PUT  /api/v1/ai/cost-rates              — patch the override map
DELETE /api/v1/ai/cost-rates            — reset to defaults

Gated by ``require_auth`` only — OSS Free is a single-user install so any
authenticated user owns the install. Plus middleware layers an admin-role
check on top of this.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

from fpulse.ai.cost_rates import DEFAULT_RATES, get_rates, reset_rates, set_rates
from fpulse.auth.deps import current_workspace_id, require_auth


logger = logging.getLogger("fpulse.ai_cost_rates")
router = APIRouter(prefix="/api/v1/ai/cost-rates", tags=["ai-cost-rates"])


class CostRatesResponse(BaseModel):
    workspace_id: str
    rates: dict[str, Any]
    defaults: dict[str, Any]


class UpdateRatesBody(BaseModel):
    # Patch shape mirrors the rate table itself:
    #   { "providers": { "anthropic": { "input_per_mtok": 3.0, "output_per_mtok": 15.0 } },
    #     "models":    { "claude-haiku-4-5": { ... } },
    #     "fallback":  { ... } }
    # Only keys present here are updated; everything else keeps current state.
    providers: dict[str, Any] | None = Field(default=None)
    models: dict[str, Any] | None = Field(default=None)
    fallback: dict[str, Any] | None = Field(default=None)


@router.get("", response_model=CostRatesResponse)
def get_cost_rates(
    _user=Depends(require_auth),
    workspace_id: str = Depends(current_workspace_id),
):
    return CostRatesResponse(
        workspace_id=workspace_id,
        rates=get_rates(workspace_id),
        defaults=DEFAULT_RATES,
    )


@router.put("", response_model=CostRatesResponse)
def put_cost_rates(
    body: UpdateRatesBody = Body(...),
    user=Depends(require_auth),
    workspace_id: str = Depends(current_workspace_id),
):
    patch: dict[str, Any] = {}
    if body.providers is not None:
        patch["providers"] = body.providers
    if body.models is not None:
        patch["models"] = body.models
    if body.fallback is not None:
        patch["fallback"] = body.fallback
    if not patch:
        raise HTTPException(400, "No rate keys to update")
    updated_by = getattr(user, "email", None) or getattr(user, "id", None) or "user"
    merged = set_rates(workspace_id, patch, updated_by=updated_by)
    return CostRatesResponse(workspace_id=workspace_id, rates=merged, defaults=DEFAULT_RATES)


@router.delete("", response_model=CostRatesResponse)
def delete_cost_rates(
    user=Depends(require_auth),
    workspace_id: str = Depends(current_workspace_id),
):
    updated_by = getattr(user, "email", None) or getattr(user, "id", None) or "user"
    rates = reset_rates(workspace_id, updated_by=updated_by)
    return CostRatesResponse(workspace_id=workspace_id, rates=rates, defaults=DEFAULT_RATES)
