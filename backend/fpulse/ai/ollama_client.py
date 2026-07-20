"""
Ollama-backed implementation of AgentLLMClient.

Uses Ollama's /api/chat endpoint with tool-use support (Ollama v0.3+).
Reference: https://github.com/ollama/ollama/blob/main/docs/api.md#chat-request-with-tools

Translates between our internal Anthropic-shaped messages (text + tool_use +
tool_result content blocks) and Ollama's flatter shape (role + content +
optional tool_calls / role="tool").

Tested against `llama3`, `phi3`, `mistral`. Older Ollama builds without
tool-use return `tool_calls` empty — agent loop then surfaces the LLM's
text directly without tool invocation, which is the safest fallback.
"""

from __future__ import annotations

import json as _json
import logging
import os as _os
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from fpulse.ai.agent import LLMResponse, LLMToolUse
from fpulse.ai.json_repair import parse_tolerant


logger = logging.getLogger(__name__)


# ── Per-request Ollama options (2026-05-22) ──────────────────────────────
#
# Ollama defaults `num_ctx` to **2048 tokens** when the request doesn't
# set it explicitly. That's silently too small for the F-Pulse agent path:
# the system prompt is ~1.3 K tokens, the tool schemas add another ~1.5 K,
# and even a compact page-context block lands around 500 tokens — leaving
# zero room for the user's question or the conversation, and forcing
# Ollama to silently drop the head of the prompt. Many of the
# "model returns greetings instead of calling tools" reports were this.
#
# Defaults aim for "boring CPU 7B at the 2026-05-19 tool-use floor":
#   - num_ctx=8192 → fits prompt + tools + context with room for a tool
#     loop. 8K is the floor that keeps prompt-processing under ~10 s on
#     CPU; 16K is opt-in via env; 32K is for GPU / cloud-class hardware.
#   - temperature=0.2 → low enough to keep tool-arg JSON stable but not
#     so low that the model refuses to vary phrasing.
#   - top_p=0.9, repeat_penalty=1.05 → conservative, well-tested.
#
# Every default is overridable via env var so an operator can tune for
# their hardware without code changes.
def _resolve_ollama_options() -> dict[str, Any]:
    def _intenv(name: str, default: int) -> int:
        raw = _os.environ.get(name, "").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            logger.warning("Invalid int for %s: %r — using default %d", name, raw, default)
            return default

    def _floatenv(name: str, default: float) -> float:
        raw = _os.environ.get(name, "").strip()
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            logger.warning("Invalid float for %s: %r — using default %s", name, raw, default)
            return default

    return {
        "num_ctx":        _intenv("FPULSE_OLLAMA_NUM_CTX", 8192),
        "temperature":    _floatenv("FPULSE_OLLAMA_TEMPERATURE", 0.2),
        "top_p":          _floatenv("FPULSE_OLLAMA_TOP_P", 0.9),
        "repeat_penalty": _floatenv("FPULSE_OLLAMA_REPEAT_PENALTY", 1.05),
    }


@dataclass
class OllamaAgentClient:
    """Ollama tool-use client with token streaming.

    When ``on_token`` is supplied to ``call``, the client uses Ollama's
    streaming /api/chat (NDJSON, one JSON object per line) and invokes
    the callback with each text delta as it arrives. The text feels
    "live" in the UI even on CPU-bound local inference. Tool-call args
    are streamed too but only emitted to the agent loop once complete.
    """

    user_id: str | None = None
    workspace_id: str | None = None
    timeout_seconds: int = 180  # local CPU inference is slow

    async def call(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_token: Callable[[str], None] | None = None,
    ) -> LLMResponse:
        from fpulse.planner.ai_client import resolve_provider

        provider, _, model, base_url = resolve_provider(
            user_id=self.user_id, workspace_id=self.workspace_id
        )
        if provider != "ollama":
            raise RuntimeError(
                f"OllamaAgentClient invoked but resolve_provider returned {provider!r}; "
                "endpoint must dispatch by provider before constructing the client"
            )
        if not base_url:
            # 2026-05-22: IPv4 default so Windows `localhost`→::1 resolution
            # doesn't silently fail when Ollama binds to 127.0.0.1 only.
            base_url = "http://127.0.0.1:11434"
        url = base_url.rstrip("/") + "/api/chat"

        ollama_messages = _translate_messages(system, messages)
        # Stream whenever the caller wants tokens. Ollama supports streaming
        # with tools (text deltas flow as the model generates rationale; the
        # final `done:true` chunk carries any tool_calls). We extract those
        # in `_stream_call` so the agent loop still gets structured tool_uses.
        want_stream = on_token is not None

        base_body: dict[str, Any] = {
            "model": model or "llama3",
            "messages": ollama_messages,
            "stream": want_stream,
            # Keep the model resident in memory + the KV cache warm for 24 h
            # so subsequent turns don't pay the cold-start cost (10-30 s on
            # CPU for qwen2.5:7b at the recommended tool-use floor). Ollama
            # re-uses the prompt-prefix KV cache
            # automatically when the leading messages are byte-identical —
            # our system prompt + tool schemas are stable across turns, so
            # this captures the cache hit on the ~3.5 k-token prefix.
            "keep_alive": "24h",
            # Explicit options (2026-05-22) — see `_resolve_ollama_options`.
            # Critically sets num_ctx ≥ 8192 because Ollama's default of
            # 2048 silently truncated the system prompt + tool schemas and
            # was a real source of the "model returns greetings instead of
            # calling tools" failures on local 7B.
            "options": _resolve_ollama_options(),
        }
        body: dict[str, Any] = dict(base_body)
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.get("name", ""),
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema") or {},
                    },
                }
                for t in tools
            ]

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            if want_stream:
                try:
                    return await self._stream_call(client, url, body, on_token)
                except httpx.HTTPStatusError as e:
                    # Model doesn't support tools — retry streaming without them.
                    if tools and e.response.status_code in (400, 422):
                        body_no_tools = dict(base_body)
                        return await self._stream_call(client, url, body_no_tools, on_token)
                    raise

            try:
                resp = await client.post(url, json=body)
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                if tools and e.response.status_code in (400, 422):
                    body_no_tools = dict(base_body)
                    resp = await client.post(url, json=body_no_tools)
                    resp.raise_for_status()
                    data = resp.json()
                    return _parse_ollama_response(data)
                raise
            data = resp.json()

        return _parse_ollama_response(data)

    async def _stream_call(
        self,
        client: httpx.AsyncClient,
        url: str,
        body: dict[str, Any],
        on_token: Callable[[str], None],
    ) -> LLMResponse:
        """Stream Ollama's NDJSON response, emitting each text delta.

        Each line is one JSON object: {message: {role, content, tool_calls?}, done, ...}
        Text content streams progressively. `tool_calls` typically arrive
        on the final chunk where `done=true`, along with token counts.
        """
        text_parts: list[str] = []
        tokens_in = 0
        tokens_out = 0
        last_tool_calls: list[dict[str, Any]] = []
        async with client.stream("POST", url, json=body) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                try:
                    obj = _json.loads(line)
                except Exception:
                    continue
                msg = obj.get("message") or {}
                delta = msg.get("content") or ""
                if delta:
                    text_parts.append(delta)
                    try:
                        on_token(delta)
                    except Exception:
                        pass
                # Tool calls may appear on intermediate chunks AND on the
                # final chunk. Keep the last non-empty list — Ollama emits
                # them once, fully formed.
                tcs = msg.get("tool_calls")
                if tcs:
                    last_tool_calls = tcs
                if obj.get("done"):
                    tokens_in = int(obj.get("prompt_eval_count", 0) or 0)
                    tokens_out = int(obj.get("eval_count", 0) or 0)

        tool_uses: list[LLMToolUse] = []
        for i, tc in enumerate(last_tool_calls):
            fn = tc.get("function") or {}
            # Local models at the recommended floor (qwen2.5:7b, llama3.1:8b)
            # still emit malformed JSON in tool args ~1-2% of the time —
            # trailing commas, unescaped newlines, Python ``True`` literals.
            # parse_tolerant repairs these in-process instead of returning
            # ``{}``, which the model would otherwise treat as "no args" and
            # re-ask. (Sub-floor models like qwen2.5:1.5b/3b fail the
            # tool-use loop entirely — see the OllamaRecommendationBanner.)
            args = parse_tolerant(fn.get("arguments")).value
            tool_uses.append(LLMToolUse(
                id=f"ollama-tu-{i}",
                name=fn.get("name", ""),
                input=args if isinstance(args, dict) else {},
            ))
        return LLMResponse(
            text="".join(text_parts),
            tool_uses=tool_uses,
            stop_reason="tool_use" if tool_uses else "end_turn",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )


def _translate_messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate Anthropic-shaped content blocks → Ollama flat messages.

    Anthropic shape we receive:
      assistant: [{type:text, text:...}, {type:tool_use, id, name, input}]
      user:      [{type:tool_result, tool_use_id, content}, ...]   OR plain string

    Ollama shape we emit:
      [{role:system, content:...},
       {role:user, content:...},
       {role:assistant, content:..., tool_calls:[{function:{name, arguments}}]},
       {role:tool, content:...},
       ...]
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
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": block.get("input") or {},
                        },
                    })
            msg: dict[str, Any] = {
                "role": "assistant",
                "content": "\n".join(p for p in text_parts if p),
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
                        # Anthropic allows nested content blocks here too —
                        # flatten for Ollama.
                        body_content = "\n".join(
                            (b.get("text", "") if isinstance(b, dict) else str(b))
                            for b in body_content
                        )
                    out.append({"role": "tool", "content": str(body_content)})
                elif btype == "text":
                    out.append({"role": "user", "content": block.get("text", "")})

    return out


def _parse_ollama_response(data: dict[str, Any]) -> LLMResponse:
    msg = data.get("message") or {}
    text = msg.get("content", "") or ""
    raw_tool_calls = msg.get("tool_calls") or []

    tool_uses: list[LLMToolUse] = []
    for i, tc in enumerate(raw_tool_calls):
        fn = tc.get("function") or {}
        # Tolerant parse — handles dicts (newer Ollama), JSON strings
        # (older builds), and the common small-model defects (trailing
        # commas, unescaped newlines, Python literals, code fences).
        args = parse_tolerant(fn.get("arguments")).value
        tool_uses.append(
            LLMToolUse(
                id=f"ollama-tu-{i}",
                name=fn.get("name", ""),
                input=args if isinstance(args, dict) else {},
            )
        )

    return LLMResponse(
        text=text,
        tool_uses=tool_uses,
        stop_reason="tool_use" if tool_uses else "end_turn",
        tokens_in=int(data.get("prompt_eval_count", 0)),
        tokens_out=int(data.get("eval_count", 0)),
    )
