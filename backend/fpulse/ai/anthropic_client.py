"""
Real Anthropic-backed implementation of AgentLLMClient.

Implements the tool-use protocol per
https://docs.anthropic.com/en/docs/build-with-claude/tool-use

Reuses the provider-resolution and HTTP shape from
fpulse.planner.ai_client._call_claude. This client is the production binding
for AgentRunner; tests substitute FakeLLMClient via the AgentLLMClient Protocol.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

from fpulse.ai.agent import AgentLLMClient, LLMResponse, LLMToolUse


@dataclass
class AnthropicAgentClient:
    """Anthropic Messages API client with tool-use support.

    Provider/model resolved per-call via fpulse.planner.ai_client.resolve_provider.
    Caller threads user_id / workspace_id via constructor; the resolution
    rules are identical to ai_generate_pipeline.
    """

    user_id: str | None = None
    workspace_id: str | None = None
    max_tokens_per_turn: int = 2048
    api_url: str = "https://api.anthropic.com/v1/messages"
    timeout_seconds: int = 60

    async def call(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_token=None,  # accepted but unused; cloud responses are fast enough non-streamed
    ) -> LLMResponse:
        from fpulse.planner.ai_client import resolve_provider

        provider, api_key, model, _base = resolve_provider(
            user_id=self.user_id, workspace_id=self.workspace_id
        )

        # AgentRunner must NOT call this client when no provider — endpoint
        # gates that before constructing AgentRunner. If we ever get here
        # with provider="none" it's a wiring bug; raise so the agent loop
        # falls back via its outer try/except.
        if provider != "claude":
            raise RuntimeError(
                f"AnthropicAgentClient invoked but resolve_provider returned {provider!r}; "
                "endpoint must route non-Claude providers separately"
            )
        if not api_key:
            raise RuntimeError("Anthropic provider resolved but api_key is empty")

        body: dict[str, Any] = {
            "model": model,
            "max_tokens": self.max_tokens_per_turn,
            "system": system,
            "messages": messages,
        }
        if tools:
            body["tools"] = tools

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            resp = await client.post(
                self.api_url,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

        return _parse_anthropic_response(data)


def _parse_anthropic_response(data: dict[str, Any]) -> LLMResponse:
    """Map Anthropic Messages API response to LLMResponse.

    Anthropic content blocks come in two relevant flavors:
      {"type": "text", "text": "..."}
      {"type": "tool_use", "id": "...", "name": "...", "input": {...}}
    """
    content_blocks = data.get("content") or []
    text_parts: list[str] = []
    tool_uses: list[LLMToolUse] = []
    for block in content_blocks:
        btype = block.get("type")
        if btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype == "tool_use":
            tool_uses.append(
                LLMToolUse(
                    id=block.get("id", ""),
                    name=block.get("name", ""),
                    input=block.get("input") or {},
                )
            )

    usage = data.get("usage") or {}
    return LLMResponse(
        text="\n".join(p for p in text_parts if p),
        tool_uses=tool_uses,
        stop_reason=data.get("stop_reason", "end_turn"),
        tokens_in=int(usage.get("input_tokens", 0)),
        tokens_out=int(usage.get("output_tokens", 0)),
    )
