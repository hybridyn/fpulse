# Supported AI Models — F-Pulse policy

**Version:** 1.1
**Last reviewed:** 2026-05-19
**Audience:** operators, security reviewers, compliance teams

This document is the authoritative supported-models policy for F-Pulse.
The `/api/trust/supported-models` endpoint returns the same content as
JSON — both are derived from the same source of truth.

## Default posture

F-Pulse is a **local-first** system. The default AI provider is a model
that runs on the operator's own host:

| Setting | Default value |
| --- | --- |
| Provider | Ollama (local) |
| CPU model | `qwen2.5:7b` (4.7 GB on disk; ~6 GB RAM at Q4_K_M) — the 2026-05-19 tool-use floor |
| Tool support | Yes |
| Data leaves the host? | No |
| Telemetry | Off (opt-in) |

Operators using cloud LLM providers must explicitly opt in via
**Insights → AI Provider**.

## Hardware tiers

The trust page surfaces a recommended model per hardware class. These are
guidelines, not hard requirements — pick the largest model whose
tool-call latency is acceptable for your workload.

| Tier | Hardware | Recommended model | Min RAM | Min VRAM | Typical tool-call latency |
| --- | --- | --- | --- | --- | --- |
| CPU laptop *(default)* | 8 GB+ RAM laptop | `qwen2.5:7b` | 8 GB | — | 30–60 s |
| Workstation / consumer GPU | RTX 4060 Ti+ | `qwen2.5:14b` | 16 GB | 12 GB | 1–3 s |
| GPU server | RTX 4090 / Ada / H100 | `llama3.1:70b-q4` | 32 GB | 48 GB | < 1 s |

The 2026-05-19 floor revision raised the CPU-laptop recommendation from
`qwen2.5:3b` to `qwen2.5:7b`. Three independent reviews converged that
sub-7B local models advertise tool schemas but fail to drive the agent's
tool-use loop reliably — they silently reply with greetings or empty
text instead of calling tools. `qwen2.5:7b`, `llama3.1:8b`, and `phi-4`
are equally-supported picks at the floor.

Models outside this list still work — F-Pulse delegates resolution to
Ollama and supports anything the local Ollama daemon can serve. The
tiers above are simply what we test and certify.

## Cloud provider escape hatch

F-Pulse keeps cloud providers in code as an explicit opt-in escape
hatch. They are **disabled by default**. Choosing one means the
operator has reviewed the privacy implications and accepts that
**prompts and tool inputs leave the host**.

Supported cloud providers:

- Anthropic Claude
- OpenAI
- OpenRouter (multi-model gateway)
- Google Gemini
- DeepSeek
- Groq
- Mistral
- Azure OpenAI

These are listed in case the operator's organisation has already
contracted privacy and data-handling terms with that vendor. F-Pulse
itself does not transmit any data to the vendor — the operator's
configuration controls the destination.

## Tool-capability requirement

The agent surface (Insights → AI Provider) only marks a model as
"agentic" when its prefix matches one of the curated tool-capable
families. The list lives in two places that must stay in sync:

- `frontend/src/util/aiModels.ts` → `OLLAMA_TOOL_CAPABLE_PREFIXES`
- `backend/fpulse/planner/ai_client.py` → `_TOOL_CAPABLE_PREFIXES`

Adding a new model to one but not the other is a UX bug — the
self-test `tests/test_supported_models_consistency.py` catches it
in CI.

## Deprecated recommendations

We keep a public record of older defaults so reviewers can audit how
the policy has evolved.

| Model | Deprecated on | Reason |
| --- | --- | --- |
| `qwen2.5:3b` | 2026-05-19 | Sub-floor tool-use reliability — advertises tool schemas but returns greetings or empty responses instead of calling tools. Replaced by `qwen2.5:7b`. |
| `qwen2.5:1.5b` | 2026-05-19 | Same failure mode as `qwen2.5:3b` (silent greetings instead of tool calls). |
| `llama3.1:8b` | 2026-05-03 | 30–60 s/turn on CPU-only laptops — at the time considered unusable for interactive tool-use. Re-instated as an equally-supported alternative at the 2026-05-19 floor revision since the floor itself is now at that latency. |

## Updating this policy

This file is reviewed at every minor release. To propose a change:

1. Open a PR that edits this file AND the `_SUPPORTED_MODELS` struct in
   `backend/fpulse/api/trust.py` together. The diff is reviewed by a
   security reviewer who is not the change author.
2. Bump `policy_version` if the change is backwards-incompatible for
   compliance scrapers reading `/api/trust/supported-models`.
3. Note the deprecation in this document if you remove a previously
   recommended model.
