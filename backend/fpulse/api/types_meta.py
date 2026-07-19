"""Meta endpoints for the canonical type system.

Surfaces the policy enums + cast-safety taxonomy to the frontend so
the Mapping tab dropdown and the cast-safety glyph render from a
single source of truth. Adding a new policy in
``fpulse/types/policy.py`` shows up here automatically — no second
list to edit.
"""

from __future__ import annotations

from fastapi import APIRouter

from fpulse.types import (
    CastPolicy,
    CastSafety,
    InferencePolicy,
)


router = APIRouter(prefix="/api/types", tags=["types"])


# Friendly human labels — kept short so they fit the Mapping tab's
# dropdown without truncation.
_CAST_POLICY_LABELS = {
    CastPolicy.SAFE: "Safe (block any unsafe cast)",
    CastPolicy.COERCE: "Coerce (allow semantic-lossy with warning)",
    CastPolicy.TRUNCATE: "Truncate (allow lossy, warn)",
    CastPolicy.STRICT: "Strict (identity casts only)",
    CastPolicy.LEARN: "Learn (record mismatches, proceed)",
}

_INFERENCE_POLICY_LABELS = {
    InferencePolicy.AUTO: "Auto (sample + infer)",
    InferencePolicy.STRICT: "Strict (fail on unresolved types)",
    InferencePolicy.COERCE: "Coerce (unknowns become strings)",
    InferencePolicy.MANUAL: "Manual (operator-declared schema only)",
    InferencePolicy.LEARN: "Learn (refine over time)",
}

_CAST_SAFETY_LABELS = {
    CastSafety.SAFE: ("Safe", "ok"),
    CastSafety.SEMANTIC_LOSSY: ("Semantic-lossy", "warn"),
    CastSafety.LOSSY: ("Lossy", "warn"),
    CastSafety.IMPOSSIBLE: ("Impossible", "error"),
}


@router.get("/policies")
def get_policies() -> dict:
    """Return both policy enums in the shape the Mapping tab consumes.

    Response:
    ```
    {
      "cast_policy": {
        "default": "coerce",
        "options": [
          {"value": "safe",     "label": "Safe (...)"},
          {"value": "coerce",   "label": "Coerce (...)"},
          ...
        ]
      },
      "inference_policy": { ... }
    }
    ```
    """
    return {
        "cast_policy": {
            "default": CastPolicy.COERCE.value,
            "options": [
                {"value": p.value, "label": _CAST_POLICY_LABELS[p]}
                for p in CastPolicy
            ],
        },
        "inference_policy": {
            "default": InferencePolicy.AUTO.value,
            "options": [
                {"value": p.value, "label": _INFERENCE_POLICY_LABELS[p]}
                for p in InferencePolicy
            ],
        },
    }


@router.get("/cast-safety")
def get_cast_safety() -> dict:
    """Return the cast-safety taxonomy + UI tier hint.

    ``ui_tier`` ∈ ``{ok | warn | error}`` so the frontend can pick the
    right glyph (``✓ / ⚠ / ✕``) and color without re-implementing the
    classification.
    """
    return {
        "options": [
            {
                "value": s.value,
                "label": _CAST_SAFETY_LABELS[s][0],
                "ui_tier": _CAST_SAFETY_LABELS[s][1],
            }
            for s in CastSafety
        ],
    }
