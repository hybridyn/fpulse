"""Generic OpenAI-compatible implementation of AgentLLMClient.

The catch-all Steward client for any provider that speaks OpenAI's Chat
Completions wire format but has no dedicated client of its own — DeepSeek,
Groq, Mistral, Moonshot/Kimi, Together, xAI, Fireworks, Perplexity, NVIDIA —
or a self-hosted OpenAI-compatible server (vLLM, LM Studio, llama.cpp,
LocalAI).

This is what makes the Steward honor ANY model the user picks instead of only
the four providers (claude / openai / ollama / openrouter) with bespoke
clients. Endpoint + model come from ``resolve_provider`` (workspace/user config
or env vars); an explicit ``base_url`` wins, else a known public default per
provider (see ``openai_compatible_base``). Message translation + response
parsing are shared with the direct OpenAI client so tool-use round-trips
identically regardless of which model answers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from fpulse.ai.agent import LLMResponse
from fpulse.ai.openai_client import _parse_openai_response, _translate_messages
from fpulse.planner.ai_client import _chat_completions_url, openai_compatible_base


@dataclass
class OpenAICompatibleAgentClient:
    """OpenAI-wire-format client for the long tail of providers + local servers.

    Unlike the provider-specific clients, this does NOT assert a particular
    ``resolve_provider`` result — it accepts whatever provider the caller
    dispatched to it, and resolves the endpoint from base_url / known defaults.
    """

    user_id: str | None = None
    workspace_id: str | None = None
    max_tokens_per_turn: int = 2048
    timeout_seconds: int = 60

    async def call(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_token=None,  # accepted but unused; responses are non-streamed
    ) -> LLMResponse:
        from fpulse.planner.ai_client import resolve_provider

        provider, api_key, model, base_url = resolve_provider(
            user_id=self.user_id, workspace_id=self.workspace_id
        )
        api_base = openai_compatible_base(provider, base_url)
        if not api_base:
            raise RuntimeError(
                f"provider {provider!r} needs an OpenAI-compatible base_url; "
                "none configured and no known default for this provider"
            )
        if not model:
            raise RuntimeError(
                f"provider {provider!r} resolved but no model was chosen; "
                "pick a model in Insights → AI Provider"
            )

        url = _chat_completions_url(api_base)
        oai_messages = _translate_messages(system, messages)
        body: dict[str, Any] = {
            "model": model,
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

        headers = {"Content-Type": "application/json"}
        # Local servers (vLLM / LM Studio) commonly accept no key; only send
        # Authorization when the user actually configured one.
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()

        return _parse_openai_response(data)
