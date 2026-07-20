"""Live ``{{ }}`` expression preview (C4, 2026-06-15).

Resolves a single expression-style ``{{ }}`` against a caller-supplied sample row
using the SAME resolver the executor runs per-step
(``fpulse.expression.resolve_expressions``). Reusing the runtime engine — rather
than re-implementing a subset in the browser — means the in-editor preview can
never drift from what the pipeline actually does at run time.

Security: the resolver evaluates expressions in a restricted AST environment
(safe built-ins + sample data only, no I/O, no attribute access to arbitrary
objects) — the same sandbox used for every pipeline run, so exposing it here is
no broader than running a pipeline. The endpoint never raises on a bad
expression: it returns ``{ok: false, error}`` so the editor can show a red hint.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from fpulse.auth.deps import require_auth
from fpulse.expression import ExpressionError, resolve_expressions
from fpulse.expression.resolver import DateHelper

router = APIRouter(
    prefix="/api/expression",
    tags=["expression"],
    dependencies=[Depends(require_auth)],
)


class ExpressionPreviewRequest(BaseModel):
    expression: str
    # $json — the current row. The editor sends the upstream node's first
    # sample row (which it already has from the per-node preview).
    sample_row: dict[str, Any] | None = None
    # $vars — runtime variables (Set Variable / Lookup output).
    vars: dict[str, Any] | None = None
    # $itemIndex
    item_index: int = 0
    # $('Label') refs → sample rows, keyed by node LABEL. Optional.
    node_samples: dict[str, list[dict[str, Any]]] | None = None


class ExpressionPreviewResponse(BaseModel):
    ok: bool
    result: str | None = None
    value_type: str | None = None
    error: str | None = None


def _stringify(val: Any) -> str:
    if isinstance(val, DateHelper):
        return str(val)
    if isinstance(val, (dict, list)):
        import json
        try:
            return json.dumps(val, default=str, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            return str(val)
    return str(val)


def _type_name(val: Any) -> str:
    if isinstance(val, DateHelper):
        return "datetime"
    if isinstance(val, bool):
        return "bool"
    return type(val).__name__


@router.post("/preview", response_model=ExpressionPreviewResponse)
def preview_expression(req: ExpressionPreviewRequest) -> ExpressionPreviewResponse:
    """Resolve one expression against the supplied sample context."""
    expr = req.expression or ""
    if "{{" not in expr:
        # Not an expression — nothing to resolve; echo it back as a literal.
        return ExpressionPreviewResponse(ok=True, result=expr, value_type="str")

    # Map node samples (keyed by label) into the resolver's ctx_results +
    # identity node_labels so `$('Label')` refs resolve.
    node_samples = req.node_samples or {}
    ctx_results = dict(node_samples)
    node_labels = {label: label for label in node_samples}

    try:
        val = resolve_expressions(
            expr,
            ctx_results=ctx_results,
            node_labels=node_labels,
            item=req.sample_row or {},
            item_index=req.item_index,
            vars_=req.vars or {},
        )
    except ExpressionError as e:
        return ExpressionPreviewResponse(ok=False, error=str(e))
    except Exception as e:  # noqa: BLE001 — a preview must never 500
        return ExpressionPreviewResponse(ok=False, error=str(e))

    return ExpressionPreviewResponse(
        ok=True, result=_stringify(val), value_type=_type_name(val),
    )
