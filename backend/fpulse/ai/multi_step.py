"""
Multi-step prompt detector — May 5 2026.

A prompt is "multi-step" when the user is chaining two or more imperative
clauses with sequencing words ("first ... then ...", "X and then Y",
"after that"). These need the full agent loop because no single
fast-lane / direct handler can satisfy the whole request.

The detector is intentionally conservative — false positives push a
prompt to the slow agent, false negatives let fast-lane mis-route a
multi-step request to a single intent (much worse). Keep markers tight.
"""

from __future__ import annotations

import re

# Sequencing markers — strong signal of two-step intent.
_SEQ_MARKERS = (
    " then ",
    " and then ",
    " followed by ",
    " after that ",
    " after which ",
    " before that ",
    # May 6 2026 review additions
    " along with ",
    " while also ",
    " and also ",
    " plus then ",
    " next ",
    " step by step ",
    " step-by-step ",
)

# "First X" without a "then Y" is fine — just emphasis. We require BOTH
# "first ..." AND "then ..." in the same sentence to consider it sequential.
_FIRST_THEN_RE = re.compile(r"\bfirst\b.+\bthen\b", re.I)


def is_multi_step(prompt: str) -> bool:
    """Return True when the prompt chains two or more imperative steps."""
    if not prompt:
        return False
    p = " " + prompt.lower().strip() + " "
    if any(m in p for m in _SEQ_MARKERS):
        return True
    if _FIRST_THEN_RE.search(prompt):
        return True
    return False
