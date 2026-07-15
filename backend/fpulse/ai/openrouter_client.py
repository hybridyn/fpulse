"""
OpenRouter-backed implementation of AgentLLMClient.

OpenRouter exposes an OpenAI-compatible Chat Completions API at
https://openrouter.ai/api/v1/chat/completions, but routes requests across
100+ underlying models (Anthropic, OpenAI, Google, Meta, Mistral, ...) so
a single key gives F-Pulse users access to many providers without
configuring each separately.

Differences from the direct OpenAI client:
  - Base URL: openrouter.ai (not openai.com)
  - Required attribution headers: HTTP-Referer + X-Title (per OpenRouter docs)
  - Model id is namespaced: "anthropic/claude-sonnet-4", "openai/gpt-4o-mini",
    "google/gemini-pro-1.5", "meta-llama/llama-3.1-70b-instruct", etc.
  - Tool-use support depends on the chosen model — same as direct OpenAI.
    OpenRouter passes the tools[] payload through unchanged; tool_calls
    come back in the same shape we already parse.
  - Privacy: OpenRouter is an additional middleman vs direct OpenAI, but
    we still send `route: "fallback"` and don't store the prompt anywhere
    on our side. Users sensitive to third-party transit should use direct
    Anthropic / OpenAI keys instead.

Reference: https://openrouter.ai/docs/quickstart
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from fpulse.ai.agent import LLMResponse, LLMToolUse


# Attribution headers — OpenRouter uses these for ranking pages on their
# leaderboards. Override in production via env if you want your domain
# attributed; default is conservative.
import os as _os

_DEFAULT_REFERER = _os.environ.get("FPULSE_OPENROUTER_REFERER", "https://hybridyn.example/fpulse")
_DEFAULT_TITLE = _os.environ.get("FPULSE_OPENROUTER_TITLE", "F-Pulse")


@dataclass
class OpenRouterAgentClient:
    """OpenRouter Chat Completions client. OpenAI-API compatible.

    Provider/model resolved per-call via fpulse.planner.ai_client.resolve_provider.
    The resolver returns ``provider == "openrouter"`` and the model id
    pre-namespaced (e.g. "openai/gpt-4o-mini") so we can pass it through
    unchanged.
    """

    user_id: str | None = None
    workspace_id: str | None = None
    max_tokens_per_turn: int = 2048
    api_url: str = "https://openrouter.ai/api/v1/chat/completions"
    timeout_seconds: int = 60

    async def call(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_token=None,
    ) -> LLMResponse:
        from fpulse.planner.ai_client import resolve_provider

        provider, api_key, model, _base = resolve_provider(
            user_id=self.user_id, workspace_id=self.workspace_id
        )
        if provider != "openrouter":
            raise RuntimeError(
                f"OpenRouterAgentClient invoked but resolve_provider returned {provider!r}; "
                "endpoint must dispatch by provider before constructing the client"
            )
        if not api_key:
            raise RuntimeError("OpenRouter provider resolved but api_key is empty")

        oai_messages = _translate_messages(system, messages)
        body: dict[str, Any] = {
            # Default to a cheap, fast tool-capable model when the user hasn't
            # picked one yet. They can override via Insights → AI Provider.
            "model": model or "openai/gpt-4o-mini",
            "messages": oai_messages,
            "max_tokens": self.max_tokens_per_turn,
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.get("name", ""),
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
                    },
                }
                for t in tools
            ]

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            resp = await client.post(
                self.api_url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    # OpenRouter attribution — used for their leaderboards.
                    # Custom override via env var; defaults to conservative values.
                    "HTTP-Referer": _DEFAULT_REFERER,
                    "X-Title": _DEFAULT_TITLE,
                },
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

        return _parse_openrouter_response(data)


def _translate_messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate Anthropic-shaped content blocks → OpenAI flat messages.

    Identical shape to the direct OpenAI client — OpenRouter forwards the
    payload to the underlying provider unchanged, so tool_calls round-trip
    the same way regardless of whether the chosen model is gpt-4o or
    claude-sonnet-4 or llama-3.1-70b-instruct.
    """
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in messages:
        role = m.get("role", "")
        content = m.get("content")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            continue

        if role == "assistant":
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for block in content:
                btype = block.get("type")
                if btype == "text":
                    text_parts.append(block.get("text", ""))
                elif btype == "tool_use":
                    tool_calls.append({
                        "id": block.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(block.get("input") or {}),
                        },
                    })
            msg: dict[str, Any] = {
                "role": "assistant",
                "content": "\n".join(p for p in text_parts if p) or None,
            }
            if tool_calls:
                msg["tool_calls"] = tool_calls
            out.append(msg)
            continue

        if role == "user":
            for block in content:
                btype = block.get("type")
                if btype == "tool_result":
                    body_content = block.get("content", "")
                    if isinstance(body_content, list):
                        body_content = "\n".join(
                            (b.get("text", "") if isinstance(b, dict) else str(b))
                            for b in body_content
                        )
                    out.append({
                        "role": "tool",
                        "tool_call_id": block.get("tool_use_id", ""),
                        "content": str(body_content),
                    })
                elif btype == "text":
                    out.append({"role": "user", "content": block.get("text", "")})

    return out


def _parse_openrouter_response(data: dict[str, Any]) -> LLMResponse:
    """OpenRouter normalizes responses to OpenAI's chat.completions shape.

    Same structure as `_parse_openai_response` in openai_client.py. Kept
    separate so any OpenRouter-specific quirks (e.g. their `usage.cost_usd`
    extension, or model-routing notices in `provider` field) can be handled
    here without touching the direct-OpenAI path.
    """
    choices = data.get("choices") or []
    if not choices:
        return LLMResponse(text="", tool_uses=[], stop_reason="end_turn")

    msg = choices[0].get("message") or {}
    text = msg.get("content") or ""
    raw_tool_calls = msg.get("tool_calls") or []
    finish_reason = choices[0].get("finish_reason", "stop")

    tool_uses: list[LLMToolUse] = []
    for tc in raw_tool_calls:
        fn = tc.get("function") or {}
        # Tolerant parse — OpenRouter routes to many underlying models
        # (claude, gpt, llama-3.1 variants, etc.). Argument quality varies;
        # parse_tolerant repairs the common small-model defects.
        from fpulse.ai.json_repair import parse_tolerant
        args = parse_tolerant(fn.get("arguments", "{}")).value
        tool_uses.append(
            LLMToolUse(
                id=tc.get("id", ""),
                name=fn.get("name", ""),
                input=args if isinstance(args, dict) else {},
            )
        )

    usage = data.get("usage") or {}
    return LLMResponse(
        text=text or "",
        tool_uses=tool_uses,
        stop_reason="tool_use" if tool_uses else (
            "end_turn" if finish_reason == "stop" else finish_reason
        ),
        tokens_in=int(usage.get("prompt_tokens", 0)),
        tokens_out=int(usage.get("completion_tokens", 0)),
    )
