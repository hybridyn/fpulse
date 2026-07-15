"""AI primitives — Sprint C.

Three building-block nodes that turn F-Pulse into a usable RAG / agent
substrate without coupling to a single LLM vendor:

- EmbedderNode: take a text column, emit a fixed-dimension vector column.
  Providers: openai, cohere, sentence_transformers, and a deterministic
  hash fallback so the node always works offline (great for tests/CI).

- LlmGuardrailNode: cheap, deterministic content checks for PII, profanity
  and prompt-injection patterns. Routes rows: block / mask / tag.

- SemanticRouterNode: classify rows into one of N labels by cosine-comparing
  the row's embedding against per-label prototype embeddings. No LLM call
  required at runtime; everything runs in-process.

All three are pure pandas/duckdb on the row-level — no async, no cloud
dependency at import time. Optional providers are imported lazily.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Any, Iterable, TYPE_CHECKING

# Stage 2.5b: duckdb only used for the execute() return-type annotation.
if TYPE_CHECKING:
    import duckdb

from fpulse.ir.schema import StepType
from fpulse.nodes.base import BaseNode, ExecutionContext
from fpulse.nodes.registry import register


# ─────────────────────────────────────────────────────────────────────────────
# Embedding providers
# ─────────────────────────────────────────────────────────────────────────────

def _hash_embed(text: str, dim: int = 384) -> list[float]:
    """Deterministic hash-based embedding. Not semantic, but stable + free.

    Used as the fallback when no API key is configured. Good enough for
    smoke-tests, demos, and pipelines where rows just need a unique vector.
    """
    text = text or ""
    # Repeat the digest until we have `dim` floats.
    out: list[float] = []
    counter = 0
    while len(out) < dim:
        h = hashlib.sha256(f"{counter}:{text}".encode("utf-8")).digest()
        for i in range(0, len(h), 4):
            if len(out) >= dim:
                break
            chunk = h[i:i + 4]
            val = int.from_bytes(chunk, "big") / 0xFFFFFFFF  # 0..1
            out.append(val * 2 - 1)  # -1..1
        counter += 1
    # L2 normalise so cosine similarity behaves.
    norm = math.sqrt(sum(x * x for x in out)) or 1.0
    return [x / norm for x in out]


def _embed_batch(provider: str, model: str, texts: list[str], dim: int) -> list[list[float]]:
    """Dispatch a batch of texts to the configured provider.

    Each provider import is wrapped in try/except so missing SDKs degrade to
    the hash fallback rather than crashing the pipeline.
    """
    if provider == "openai":
        try:
            from openai import OpenAI  # type: ignore
            client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
            resp = client.embeddings.create(model=model or "text-embedding-3-small", input=texts)
            return [d.embedding for d in resp.data]
        except Exception as e:
            print(f"[embedder] openai unavailable, falling back to hash: {e}")
            return [_hash_embed(t, dim) for t in texts]

    if provider == "cohere":
        try:
            import cohere  # type: ignore
            client = cohere.Client(os.environ.get("COHERE_API_KEY"))
            resp = client.embed(texts=texts, model=model or "embed-english-v3.0", input_type="search_document")
            return [list(v) for v in resp.embeddings]
        except Exception as e:
            print(f"[embedder] cohere unavailable, falling back to hash: {e}")
            return [_hash_embed(t, dim) for t in texts]

    if provider == "sentence_transformers":
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
            st_model = SentenceTransformer(model or "all-MiniLM-L6-v2")
            return [list(v) for v in st_model.encode(texts, normalize_embeddings=True)]
        except Exception as e:
            print(f"[embedder] sentence_transformers unavailable, falling back to hash: {e}")
            return [_hash_embed(t, dim) for t in texts]

    # Default: hash
    return [_hash_embed(t, dim) for t in texts]


# ─────────────────────────────────────────────────────────────────────────────
# Embedder node
# ─────────────────────────────────────────────────────────────────────────────

@register(StepType.EMBEDDER)
class EmbedderNode(BaseNode):
    display_name = "Embedder"
    category = "transform"
    description = "Convert text into numeric vectors so AI can find similar items"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        inputs = ctx.get_inputs(self.params.get("_input_step_ids", []))
        if not inputs:
            raise ValueError("Embedder node has no input data")

        source = inputs[0]
        text_col = self.params.get("text_column") or ""
        if not text_col:
            raise ValueError("Embedder requires a text_column")

        provider = self.params.get("provider", "hash")
        model = self.params.get("model", "")
        dim = int(self.params.get("dim", 384))
        out_col = self.params.get("output_column", "embedding")
        batch_size = int(self.params.get("batch_size", 64))

        df = source.fetchdf()
        if text_col not in df.columns:
            raise ValueError(f"Embedder: column '{text_col}' not in upstream {list(df.columns)}")

        texts = [str(v) if v is not None else "" for v in df[text_col].tolist()]
        vectors: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            vectors.extend(_embed_batch(provider, model, texts[i:i + batch_size], dim))

        df[out_col] = vectors
        return ctx.conn.from_df(df)

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "text_column": "",
            "provider": "hash",
            "model": "",
            "dim": 384,
            "output_column": "embedding",
            "batch_size": 64,
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "text_column", "type": "column", "label": "Text Column", "required": True},
            {"name": "provider", "type": "select", "label": "Provider",
             "options": ["hash", "openai", "cohere", "sentence_transformers"], "default": "hash"},
            {"name": "model", "type": "text", "label": "Model (optional)",
             "placeholder": "text-embedding-3-small"},
            {"name": "dim", "type": "number", "label": "Hash Dimension", "default": 384},
            {"name": "output_column", "type": "text", "label": "Output Column", "default": "embedding"},
            {"name": "batch_size", "type": "number", "label": "Batch Size", "default": 64},
        ]


# ─────────────────────────────────────────────────────────────────────────────
# Guardrail patterns
# ─────────────────────────────────────────────────────────────────────────────

# Conservative defaults — designed to catch the obvious cases without
# false-positive storms. Users can extend via the `extra_patterns` param.
_PII_PATTERNS = {
    "email":      re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I),
    "phone":      re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
    "ssn":        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "credit_card": re.compile(r"\b(?:\d[ -]*?){13,16}\b"),
    "ip_address": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
}

_PROMPT_INJECTION_PATTERNS = [
    re.compile(r"ignore (all )?previous instructions", re.I),
    re.compile(r"disregard (the )?(above|prior|previous)", re.I),
    re.compile(r"system prompt", re.I),
    re.compile(r"you are now (a |an )?", re.I),
    re.compile(r"act as if", re.I),
    re.compile(r"</?(system|user|assistant)>", re.I),
]

_PROFANITY_DEFAULT = {"damn", "hell", "shit", "fuck", "bitch", "asshole"}


def _scan_text(text: str, checks: Iterable[str], extra_patterns: list[str], profanity_words: set[str]) -> list[str]:
    """Return list of triggered check names for one text value."""
    if not text:
        return []
    hits: list[str] = []

    if "pii" in checks:
        for name, pat in _PII_PATTERNS.items():
            if pat.search(text):
                hits.append(f"pii:{name}")

    if "prompt_injection" in checks:
        for pat in _PROMPT_INJECTION_PATTERNS:
            if pat.search(text):
                hits.append("prompt_injection")
                break

    if "profanity" in checks and profanity_words:
        lower = text.lower()
        if any(w in lower for w in profanity_words):
            hits.append("profanity")

    for raw in extra_patterns:
        try:
            if re.search(raw, text, re.I):
                hits.append(f"custom:{raw[:20]}")
        except re.error:
            continue

    return hits


def _mask_text(text: str) -> str:
    """Replace PII matches with ***."""
    if not text:
        return text
    for pat in _PII_PATTERNS.values():
        text = pat.sub("***", text)
    return text


@register(StepType.LLM_GUARDRAIL)
class LlmGuardrailNode(BaseNode):
    display_name = "LLM Guardrail"
    category = "transform"
    description = "Catch sensitive info, unsafe prompts, or inappropriate content — route the bad ones aside"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        inputs = ctx.get_inputs(self.params.get("_input_step_ids", []))
        if not inputs:
            raise ValueError("Guardrail node has no input data")

        source = inputs[0]
        text_col = self.params.get("text_column", "")
        if not text_col:
            raise ValueError("Guardrail requires a text_column")

        checks = self.params.get("checks") or ["pii", "prompt_injection"]
        if isinstance(checks, str):
            checks = [c.strip() for c in checks.split(",") if c.strip()]
        mode = self.params.get("mode", "tag")  # tag | block | mask
        extra_patterns = self.params.get("extra_patterns") or []
        if isinstance(extra_patterns, str):
            extra_patterns = [p.strip() for p in extra_patterns.split(",") if p.strip()]
        profanity_words = set((self.params.get("profanity_words") or list(_PROFANITY_DEFAULT)))

        df = source.fetchdf()
        if text_col not in df.columns:
            raise ValueError(f"Guardrail: column '{text_col}' not in upstream {list(df.columns)}")

        flags: list[str] = []
        masked: list[str] = []
        for v in df[text_col].tolist():
            text = str(v) if v is not None else ""
            hits = _scan_text(text, checks, extra_patterns, profanity_words)
            flags.append(",".join(hits))
            masked.append(_mask_text(text) if mode == "mask" and hits else text)

        df["__guardrail_flags"] = flags
        if mode == "mask":
            df[text_col] = masked

        if mode == "block":
            df = df[[not bool(f) for f in flags]].reset_index(drop=True)

        return ctx.conn.from_df(df)

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "text_column": "",
            "checks": ["pii", "prompt_injection"],
            "mode": "tag",
            "extra_patterns": [],
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "text_column", "type": "column", "label": "Text Column", "required": True},
            {"name": "checks", "type": "multi_select", "label": "Checks",
             "options": ["pii", "prompt_injection", "profanity"]},
            {"name": "mode", "type": "select", "label": "Mode",
             "options": ["tag", "block", "mask"], "default": "tag"},
            {"name": "extra_patterns", "type": "string_list", "label": "Extra Regex Patterns"},
            # 2026-06-15: profanity_words was read by execute() but missing
            # from the schema, so users couldn't override the built-in list.
            {"name": "profanity_words", "type": "string_list", "label": "Custom Profanity List",
             "description": "Override the built-in profanity words (used only when the "
                            "'profanity' check is on). Empty = built-in defaults."},
        ]


# ─────────────────────────────────────────────────────────────────────────────
# Semantic router
# ─────────────────────────────────────────────────────────────────────────────

def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b))
    return dot  # already normalised by hash/sentence-transformers


@register(StepType.SEMANTIC_ROUTER)
class SemanticRouterNode(BaseNode):
    display_name = "Semantic Router"
    category = "transform"
    description = "Sort rows into categories using AI — finds the closest matching label for each row"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        inputs = ctx.get_inputs(self.params.get("_input_step_ids", []))
        if not inputs:
            raise ValueError("Semantic Router has no input data")

        source = inputs[0]
        text_col = self.params.get("text_column", "")
        if not text_col:
            raise ValueError("Semantic Router requires a text_column")

        labels: list[dict] = self.params.get("labels") or []
        if not labels:
            raise ValueError("Semantic Router requires at least one label")

        provider = self.params.get("provider", "hash")
        model = self.params.get("model", "")
        dim = int(self.params.get("dim", 384))
        out_col = self.params.get("output_column", "__route")
        threshold = float(self.params.get("threshold", 0.0))
        default_label = self.params.get("default_label", "other")

        # Build prototype embeddings (one per label, from concatenated examples).
        prototypes: list[tuple[str, list[float]]] = []
        for lbl in labels:
            name = lbl.get("name", "")
            examples = lbl.get("examples") or []
            if isinstance(examples, str):
                examples = [examples]
            if not name or not examples:
                continue
            joined = " \n ".join(str(e) for e in examples)
            vec = _embed_batch(provider, model, [joined], dim)[0]
            prototypes.append((name, vec))

        if not prototypes:
            raise ValueError("Semantic Router: every label must have a name + examples")

        df = source.fetchdf()
        if text_col not in df.columns:
            raise ValueError(f"Semantic Router: column '{text_col}' not in upstream {list(df.columns)}")

        texts = [str(v) if v is not None else "" for v in df[text_col].tolist()]
        row_vecs = _embed_batch(provider, model, texts, dim)

        routes: list[str] = []
        scores: list[float] = []
        for vec in row_vecs:
            best_label = default_label
            best_score = -1.0
            for name, proto in prototypes:
                s = _cosine(vec, proto)
                if s > best_score:
                    best_score = s
                    best_label = name
            if best_score < threshold:
                best_label = default_label
            routes.append(best_label)
            scores.append(round(best_score, 4))

        df[out_col] = routes
        df[f"{out_col}_score"] = scores
        # B4 (2026-06-15): opt-in branching. When route_outputs is on, tag each
        # row with _split_output = its matched label so the executor routes
        # rows to per-label output ports (one per label + the default). Off by
        # default → single output, existing pipelines behave unchanged.
        if self.params.get("route_outputs"):
            df["_split_output"] = routes
        return ctx.conn.from_df(df)

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "text_column": "",
            "labels": [],
            "provider": "hash",
            "model": "",
            "dim": 384,
            "output_column": "__route",
            "threshold": 0.0,
            "default_label": "other",
            "route_outputs": False,
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "text_column", "type": "column", "label": "Text Column", "required": True},
            {"name": "labels", "type": "label_list", "label": "Labels (name + examples)", "required": True},
            {"name": "provider", "type": "select", "label": "Provider",
             "options": ["hash", "openai", "cohere", "sentence_transformers"], "default": "hash"},
            {"name": "threshold", "type": "number", "label": "Min Confidence", "default": 0.0},
            {"name": "default_label", "type": "text", "label": "Default Label", "default": "other"},
            # B4 (2026-06-15): opt-in multi-output. On → one output handle per
            # label (+ default), routing rows by matched category.
            {"name": "route_outputs", "type": "boolean", "label": "Route to per-label outputs",
             "default": False,
             "description": "Off: single output with a label column. On: branch rows to one output per label."},
            # 2026-06-15: model / dim / output_column were read by execute()
            # but missing from the schema (Embedder exposed them; this didn't).
            {"name": "model", "type": "text", "label": "Model (optional)",
             "description": "Embedding model for the active provider; blank = provider default."},
            {"name": "output_column", "type": "text", "label": "Output Column", "default": "__route",
             "description": "Column for the matched label (a <output_column>_score is added too)."},
            {"name": "dim", "type": "number", "label": "Hash Dimension", "default": 384},
        ]
