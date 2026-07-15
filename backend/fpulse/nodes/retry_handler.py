"""Retry / Error Handler node — visual error handling for pipelines.

Instead of hiding retry logic in backend config, this makes error handling
a visible, configurable node on the canvas.  When attached downstream of
a node, it catches failures from the upstream and retries with configurable
delay and backoff.

This node wraps the upstream execution — it doesn't transform data.
On success, it passes through the upstream result unchanged.
On exhausted retries, it either fails the pipeline or routes to a
fallback output (dead-letter pattern).
"""

from __future__ import annotations

import logging
import time
from typing import Any, TYPE_CHECKING

# Stage 2.5b: duckdb only used for the execute() return-type annotation.
if TYPE_CHECKING:
    import duckdb

from fpulse.ir.schema import StepType
from fpulse.nodes.base import BaseNode, ExecutionContext
from fpulse.nodes.registry import register

logger = logging.getLogger(__name__)


@register(StepType.RETRY_HANDLER)
class RetryHandlerNode(BaseNode):
    """Retry upstream execution on failure with configurable backoff.

    Configuration:
      - max_retries: How many times to retry (default 3)
      - delay_seconds: Initial delay between retries (default 2)
      - backoff_multiplier: Multiply delay after each retry (default 2.0)
      - on_exhausted: What to do when all retries fail
        - "fail" (default): raise the error, stop the pipeline
        - "skip": return empty result, continue the pipeline
        - "last_good": return the last successful upstream result if cached

    The node logs each retry attempt with the error message so operators
    can see the retry pattern in execution logs.
    """

    display_name = "Retry"
    category = "flow"
    description = "Retry the previous step if it fails (with smart waits between attempts)"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        """Pass-through node — retry logic is applied by the executor.

        The executor's _find_retry_targets() scans for retry_handler nodes
        downstream of other nodes. When found, it wraps the upstream node's
        execution with retry/backoff logic using this node's params.

        This architecture is correct: the retry_handler is a *config node*,
        not an *execution node*. It makes error handling visible on the canvas.
        """
        upstream = self.params.get("_input_step_ids") or []
        if upstream:
            rel = ctx.get_input(upstream[0])
            if rel is not None:
                return rel

        # No upstream result yet (normal during retry orchestration)
        return ctx.conn.sql("SELECT 'retry_handler' AS _node_type, 'configured' AS _status")

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "max_retries": 3,
            "delay_seconds": 2,
            "backoff_multiplier": 2.0,
            "on_exhausted": "fail",
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "max_retries", "type": "number", "label": "Max Retries",
             "default": 3,
             "description": "Number of retry attempts before giving up"},
            {"name": "delay_seconds", "type": "number", "label": "Initial Delay (seconds)",
             "default": 2,
             "description": "Seconds to wait before the first retry"},
            {"name": "backoff_multiplier", "type": "number", "label": "Backoff Multiplier",
             "default": 2.0,
             "description": "Multiply delay by this factor after each retry (e.g., 2.0 = exponential)"},
            {"name": "on_exhausted", "type": "select", "label": "On All Retries Exhausted",
             "options": ["fail", "skip"],
             "default": "fail",
             "description": "fail = stop pipeline, skip = continue with empty result"},
        ]
