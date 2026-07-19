"""
Tests for the F-Pulse AI Step 1 foundation layer.

Covers:
  fpulse.ai.foundation  — try_llm_then_fallback, get_provider_info
  fpulse.ai.sanitize    — sanitize_for_llm + denylist + redaction + size cap
  fpulse.ai.normalize   — register_output_schema + normalize_tool_output
  fpulse.ai.cache       — L1Cache TTL/LRU + tenant key enforcement
  fpulse.ai.handles     — HandleStore put/get/delete + tenant scoping
  fpulse.ai.budget      — enforce_budget tier ordering + truncation

These tests run without any LLM provider configured. They don't touch the
existing `planner.ai_client.ai_generate_pipeline` path — that wiring lands
in Step 2 onward.
"""

from __future__ import annotations

import asyncio

import pytest

from fpulse.ai.budget import (
    BudgetExceededError,
    BudgetSection,
    enforce_budget,
    estimate_tokens,
)
from fpulse.ai.cache import (
    L1Cache,
    TenantKeyError,
    make_key,
)
from fpulse.ai.foundation import (
    ProviderInfo,
    get_provider_info,
    try_llm_then_fallback,
)
from fpulse.ai.handles import HandleStore
from fpulse.ai.normalize import (
    SchemaError,
    clear_registry,
    normalize_tool_output,
    register_output_schema,
)
from fpulse.ai.sanitize import (
    DENY_FIELD_PATTERN,
    sanitize_for_llm,
)


# ---------------------------------------------------------------------------
# foundation.py
# ---------------------------------------------------------------------------

def test_get_provider_info_returns_none_when_no_provider(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    # Disable the local-Ollama autoprobe — on dev boxes running ollama,
    # resolve_provider would otherwise detect localhost:11434 and report
    # has_provider=True, defeating the test.
    monkeypatch.setenv("FPULSE_DISABLE_OLLAMA_AUTOPROBE", "1")
    info = get_provider_info()
    assert isinstance(info, ProviderInfo)
    assert info.has_provider is False
    assert info.provider == "none"


def test_get_provider_info_detects_anthropic(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    info = get_provider_info()
    assert info.provider == "claude"
    assert info.has_provider is True


def test_try_llm_then_fallback_uses_fallback_when_no_provider(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_URL", raising=False)

    async def llm_fn(info):
        raise AssertionError("llm_fn should not be called when no provider")

    def fallback_fn():
        return {"ok": True, "from": "rules"}

    result, source = asyncio.run(
        try_llm_then_fallback(llm_fn=llm_fn, fallback_fn=fallback_fn)
    )
    assert source == "fallback"
    assert result["from"] == "rules"


def test_try_llm_then_fallback_uses_llm_when_provider_returns(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    async def llm_fn(info):
        assert info.has_provider
        return {"ok": True, "from": "llm"}

    def fallback_fn():
        raise AssertionError("fallback should not run when llm succeeds")

    result, source = asyncio.run(
        try_llm_then_fallback(llm_fn=llm_fn, fallback_fn=fallback_fn)
    )
    assert source == "llm"
    assert result["from"] == "llm"


def test_try_llm_then_fallback_falls_back_on_none(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    async def llm_fn(info):
        return None

    def fallback_fn():
        return "rules"

    result, source = asyncio.run(
        try_llm_then_fallback(llm_fn=llm_fn, fallback_fn=fallback_fn)
    )
    assert source == "fallback"
    assert result == "rules"


def test_try_llm_then_fallback_falls_back_on_exception(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    async def llm_fn(info):
        raise RuntimeError("provider 500")

    def fallback_fn():
        return "rules"

    result, source = asyncio.run(
        try_llm_then_fallback(llm_fn=llm_fn, fallback_fn=fallback_fn)
    )
    assert source == "fallback"
    assert result == "rules"


# ---------------------------------------------------------------------------
# sanitize.py
# ---------------------------------------------------------------------------

def test_sanitize_redacts_email():
    result = sanitize_for_llm({"note": "Contact bob@example.com please"})
    assert "bob@example.com" not in repr(result.payload)
    assert result.redactions.get("email") == 1


def test_sanitize_redacts_aadhaar_and_ssn():
    payload = {"k": "Aadhaar 1234 5678 9012 SSN 123-45-6789"}
    result = sanitize_for_llm(payload)
    text = result.payload["k"]
    assert "1234 5678 9012" not in text
    assert "123-45-6789" not in text
    assert result.redactions.get("aadhaar") == 1
    assert result.redactions.get("ssn") == 1


def test_sanitize_drops_denylisted_field_names():
    payload = {
        "host": "db.example.com",
        "password": "hunter2",
        "api_key": "abc",
        "nested": {"signing_secret": "x"},
    }
    result = sanitize_for_llm(payload)
    assert "password" not in result.payload
    assert "api_key" not in result.payload
    # Nested denylist applies too
    assert "signing_secret" not in result.payload["nested"]
    assert "password" in result.dropped_fields


def test_sanitize_allowlist_drops_unlisted_top_level():
    payload = {"keep": 1, "drop": 2}
    result = sanitize_for_llm(payload, allowed_fields={"keep"})
    assert "keep" in result.payload
    assert "drop" not in result.payload
    assert "drop" in result.dropped_fields


def test_sanitize_truncates_oversized_string():
    # Spaces every few chars so the API-key heuristic (32+ contiguous
    # alphanumeric run) does not match and replace the whole input.
    payload = "hello world " * 5_000  # ~60K chars; longest run is "hello"=5
    result = sanitize_for_llm(payload, max_chars=1000)
    assert result.truncated is True
    assert "[truncated" in result.payload


def test_deny_field_pattern_catches_common_secret_names():
    # Defensive — guard against regression if someone "tightens" the regex
    for name in ["password", "PASSWORD", "Api_Key", "secret", "private_key", "client_secret"]:
        assert DENY_FIELD_PATTERN.search(name), name


# ---------------------------------------------------------------------------
# normalize.py
# ---------------------------------------------------------------------------

def test_normalize_unregistered_tool_passes_through():
    clear_registry()
    assert normalize_tool_output("unknown_tool", {"x": 1}) == {"x": 1}


def test_normalize_dict_schema_drops_extras_and_validates_types():
    clear_registry()
    register_output_schema(
        "summarize_pipeline",
        {"node_count": "int", "name": "str"},
    )
    out = normalize_tool_output(
        "summarize_pipeline",
        {"node_count": 12, "name": "etl-1", "extra": "ignored"},
    )
    assert out == {"node_count": 12, "name": "etl-1"}


def test_normalize_raises_on_missing_key():
    clear_registry()
    register_output_schema("t", {"a": "int", "b": "str"})
    with pytest.raises(SchemaError, match="missing required key 'b'"):
        normalize_tool_output("t", {"a": 1})


def test_normalize_raises_on_wrong_type():
    clear_registry()
    register_output_schema("t", {"a": "int"})
    with pytest.raises(SchemaError, match="expected int"):
        normalize_tool_output("t", {"a": "not-int"})


def test_normalize_leaf_type_schema():
    clear_registry()
    register_output_schema("count", "int")
    assert normalize_tool_output("count", 42) == 42
    with pytest.raises(SchemaError):
        normalize_tool_output("count", "not int")


# ---------------------------------------------------------------------------
# cache.py
# ---------------------------------------------------------------------------

def test_cache_rejects_unprefixed_key():
    cache = L1Cache()
    with pytest.raises(TenantKeyError):
        cache.set("nope", "v", ttl_seconds=60)
    with pytest.raises(TenantKeyError):
        cache.get("nope")


def test_cache_accepts_prefixed_key_and_roundtrips():
    cache = L1Cache()
    key = make_key("tenant-a", "schema_introspection", "conn-1")
    cache.set(key, {"cols": 5}, ttl_seconds=60)
    assert cache.get(key) == {"cols": 5}


def test_cache_isolates_tenants_via_key_prefix():
    cache = L1Cache()
    a_key = make_key("tenant-a", "schema_introspection", "conn-1")
    b_key = make_key("tenant-b", "schema_introspection", "conn-1")
    cache.set(a_key, "A", ttl_seconds=60)
    cache.set(b_key, "B", ttl_seconds=60)
    assert cache.get(a_key) == "A"
    assert cache.get(b_key) == "B"


def test_cache_evicts_lru_when_max_entries_exceeded():
    cache = L1Cache(max_entries=2, max_bytes=10_000)
    k1 = make_key("t", "x", "1")
    k2 = make_key("t", "x", "2")
    k3 = make_key("t", "x", "3")
    cache.set(k1, 1, ttl_seconds=60)
    cache.set(k2, 2, ttl_seconds=60)
    cache.set(k3, 3, ttl_seconds=60)
    assert cache.get(k1) is None  # evicted
    assert cache.get(k2) == 2
    assert cache.get(k3) == 3


def test_make_key_rejects_colon_in_parts():
    with pytest.raises(ValueError, match="must not contain ':'"):
        make_key("tenant:a", "x", "k")
    with pytest.raises(ValueError, match="must not contain ':'"):
        make_key("t", "ca:che", "k")


# ---------------------------------------------------------------------------
# handles.py
# ---------------------------------------------------------------------------

def test_handles_put_get_roundtrip():
    store = HandleStore()
    h = store.put("tenant-a", {"rows": [1, 2, 3]})
    assert store.get("tenant-a", h) == {"rows": [1, 2, 3]}


def test_handles_cross_tenant_returns_none():
    store = HandleStore()
    h = store.put("tenant-a", "secret")
    # Tenant-b sees nothing — same return as missing
    assert store.get("tenant-b", h) is None


def test_handles_delete_only_owner():
    store = HandleStore()
    h = store.put("tenant-a", "v")
    assert store.delete("tenant-b", h) is False
    assert store.get("tenant-a", h) == "v"  # still there
    assert store.delete("tenant-a", h) is True
    assert store.get("tenant-a", h) is None


def test_handles_rejects_empty_tenant():
    store = HandleStore()
    with pytest.raises(ValueError):
        store.put("", "v")


def test_handles_evicts_when_over_max():
    store = HandleStore(max_entries=2)
    h1 = store.put("t", "a", ttl_seconds=600)
    h2 = store.put("t", "b", ttl_seconds=601)
    h3 = store.put("t", "c", ttl_seconds=602)
    assert len(store) == 2  # h1 evicted (oldest expiry)
    # h1 should be gone; h2 and h3 present
    present = sum(1 for h in (h1, h2, h3) if store.get("t", h) is not None)
    assert present == 2


# ---------------------------------------------------------------------------
# budget.py
# ---------------------------------------------------------------------------

def test_estimate_tokens_rough():
    assert estimate_tokens("a" * 4) == 1
    assert estimate_tokens("a" * 400) == 100


def test_budget_keeps_tier1_intact():
    sections = [
        BudgetSection("intent", "user wants X", tier=1),
        BudgetSection("summary", "x" * 4000, tier=2),
        BudgetSection("details", "x" * 4000, tier=3),
    ]
    result = enforce_budget(sections, max_tokens=200)
    # Tier 1 always kept; Tier 2/3 should be truncated/dropped
    names = [s.name for s in result.sections]
    assert "intent" in names
    assert result.total_tokens <= 200 + 50  # small tolerance for truncate marker


def test_budget_raises_when_tier1_overflows():
    sections = [BudgetSection("oversized_system", "x" * 100_000, tier=1)]
    with pytest.raises(BudgetExceededError):
        enforce_budget(sections, max_tokens=100)


def test_budget_drops_tier3_before_tier2():
    sections = [
        BudgetSection("intent", "ok", tier=1),
        BudgetSection("summary", "x" * 800, tier=2),
        BudgetSection("details", "x" * 800, tier=3),
    ]
    # ~200 tokens each for summary/details; budget 250 → keep summary, drop details
    result = enforce_budget(sections, max_tokens=250)
    names = [s.name for s in result.sections]
    assert "intent" in names
    assert "summary" in names
    # details either dropped or heavily truncated
    if "details" in names:
        assert "details" in result.truncated_sections
    else:
        assert "details" in result.dropped_sections


def test_budget_preserves_input_order_in_render():
    sections = [
        BudgetSection("a", "alpha", tier=1),
        BudgetSection("b", "beta", tier=2),
        BudgetSection("c", "gamma", tier=3),
    ]
    result = enforce_budget(sections, max_tokens=1000)
    rendered = result.render(separator="|")
    assert rendered.startswith("alpha|beta")
