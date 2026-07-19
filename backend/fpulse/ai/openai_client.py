"""
OpenAI-backed implementation of AgentLLMClient.

Uses OpenAI's Chat Completions endpoint with function-calling. Translates
between our internal Anthropic-shaped content blocks (text + tool_use +
tool_result) and OpenAI's flatter shape (system + user/assistant messages
with optional tool_calls + role="tool" reply messages).

Reference: https://platform.openai.com/docs/guides/function-calling

Tested shape against gpt-4o, gpt-4o-mini, o1-mini. Response carries
`choices[0].message.tool_calls` as a list of `{id, type:"function",
function:{name, arguments}}` where `arguments` is a JSON-encoded string.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from fpulse.ai.agent import LLMResponse, LLMToolUse


@dataclass
class OpenAIAgentClient:
    """OpenAI Chat Completions client with function-calling tool-use.

    Provider/model resolved per-call via fpulse.planner.ai_client.resolve_provider.
    Caller threads user_id / workspace_id via constructor — same shape as
    AnthropicAgentClient + OllamaAgentClient.
    """

    user_id: str | None = None
    workspace_id: str | None = None
    max_tokens_per_turn: int = 2048
    api_url: str = "https://api.openai.com/v1/chat/completions"
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
        if provider != "openai":
            raise RuntimeError(
                f"OpenAIAgentClient invoked but resolve_provider returned {provider!r}; "
                "endpoint must dispatch by provider before constructing the client"
            )
        if not api_key:
            raise RuntimeError("OpenAI provider resolved but api_key is empty")

        oai_messages = _translate_messages(system, messages)
        body: dict[str, Any] = {
            "model": model or "gpt-4o-mini",
            "messages": oai_messages,
            "max_tokens": self.max_tokens_per_turn,
            # Per ai-boundary-contract.md §4 — opt out of training where
            # the API supports the flag.
            "store": False,
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
            # Default tool_choice="auto" — model decides when to call tools.

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            resp = await client.post(
                self.api_url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

        return _parse_openai_response(data)


def _translate_messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate Anthropic-shaped content blocks → OpenAI flat messages.

    Anthropic shape we receive:
      assistant: [{type:text, text:...}, {type:tool_use, id, name, input}]
      user:      [{type:tool_result, tool_use_id, content}]   OR plain string

    OpenAI shape we emit:
      [{role:system, content:...},
       {role:user, content:...},
       {role:assistant, content:..., tool_calls:[{id, type:"function", function:{name, arguments}}]},
       {role:tool, tool_call_id:..., content:...},
       ...]

    Note: OpenAI requires `tool_call_id` on tool reply messages to link them
    back to the matching tool_call in the prior assistant turn. We propagate
    it from the Anthropic `tool_use_id` field.
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
                            # OpenAI expects arguments as a JSON STRING, not dict.
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


def _parse_openai_response(data: dict[str, Any]) -> LLMResponse:
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
        # Tolerant parse — repairs common defects (trailing commas,
        # unescaped control chars, code fences). GPT-4o-mini is mostly
        # well-formed; the repair path covers the long tail.
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
