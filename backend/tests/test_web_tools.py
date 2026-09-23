"""Tests for the opt-in Copilot web tools (web_fetch / web_search).

Covers: the master-switch gating (default OFF → not registered), READ tier,
web_fetch SSRF-block + clean error result + success via a stubbed fetcher,
and web_search not-configured message + provider parsing via a fake transport.
No network access.
"""
from __future__ import annotations

import asyncio

import pytest

from fpulse.ai.tools import register_initial_tools
from fpulse.ai.tools.base import ToolContext, ToolTier
from fpulse.ai.tools.registry import ToolRegistry


def _ctx() -> ToolContext:
    return ToolContext(tenant_id="t", user_id="u", workspace_id="default", environment="dev")


# ── Gating on the master switch ───────────────────────────────────────

def test_web_tools_absent_by_default(monkeypatch):
    monkeypatch.delenv("FPULSE_AI_WEB_ACCESS", raising=False)
    reg = register_initial_tools(ToolRegistry())
    assert len(reg) == 29
    assert "web_fetch" not in reg
    assert "web_search" not in reg


def test_web_tools_present_when_enabled(monkeypatch):
    monkeypatch.setenv("FPULSE_AI_WEB_ACCESS", "1")
    reg = register_initial_tools(ToolRegistry())
    assert len(reg) == 31
    assert "web_fetch" in reg
    assert "web_search" in reg
    # Both are READ tier — no idempotency key, no confirmation gate.
    assert reg.get("web_fetch").tier == ToolTier.READ
    assert reg.get("web_search").tier == ToolTier.READ
    assert reg.get("web_fetch").requires_idempotency_key is False
    assert reg.get("web_search").requires_idempotency_key is False


# ── web_fetch ─────────────────────────────────────────────────────────

def test_web_fetch_blocks_loopback():
    from fpulse.ai.tools.web_fetch import _handler
    out = asyncio.run(_handler({"url": "http://127.0.0.1:1/x"}, _ctx()))
    assert out["ok"] is False
    assert "blocked" in out["error"].lower()


def test_web_fetch_blocks_bad_scheme():
    from fpulse.ai.tools.web_fetch import _handler
    out = asyncio.run(_handler({"url": "file:///etc/passwd"}, _ctx()))
    assert out["ok"] is False
    assert out["status"] == 0


def test_web_fetch_requires_url():
    from fpulse.ai.tools.web_fetch import _handler
    with pytest.raises(ValueError):
        asyncio.run(_handler({"url": "  "}, _ctx()))


def test_web_fetch_success_via_stub(monkeypatch):
    import fpulse.ai.web.fetch as fetch_mod
    from fpulse.ai.tools import web_fetch as tool_mod

    def fake_fetch(url, *, timeout=12.0):
        return {
            "url": url, "final_url": url, "status": 200,
            "content_type": "application/json",
            "text": '{"openapi":"3.0.0"}', "bytes": 19, "truncated": False,
        }

    monkeypatch.setattr(fetch_mod, "fetch_url_text", fake_fetch)
    out = asyncio.run(tool_mod._handler({"url": "https://acme.dev/openapi.json"}, _ctx()))
    assert out["ok"] is True
    assert out["status"] == 200
    assert "openapi" in out["text"]


# ── web_search ────────────────────────────────────────────────────────

def test_web_search_not_configured(monkeypatch):
    monkeypatch.delenv("FPULSE_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("FPULSE_WEB_SEARCH_API_KEY", raising=False)
    from fpulse.ai.tools.web_search import _handler
    out = asyncio.run(_handler({"query": "acme api openapi"}, _ctx()))
    assert out["ok"] is False
    assert out["configured"] is False
    assert "provider" in out["error"].lower()


def test_web_search_requires_query():
    from fpulse.ai.tools.web_search import _handler
    with pytest.raises(ValueError):
        asyncio.run(_handler({"query": ""}, _ctx()))


def test_web_search_brave_parse(monkeypatch):
    monkeypatch.setenv("FPULSE_WEB_SEARCH_PROVIDER", "brave")
    monkeypatch.setenv("FPULSE_WEB_SEARCH_API_KEY", "k")

    from fpulse.ai.web.search import search_web

    def fake(method, url, headers, body, timeout):
        assert headers.get("X-Subscription-Token") == "k"
        return {"web": {"results": [
            {"title": "Acme API", "url": "https://acme.dev/openapi.json", "description": "spec"},
        ]}}

    results = search_web("acme api", count=3, _transport=fake)
    assert results[0]["url"] == "https://acme.dev/openapi.json"
    assert results[0]["snippet"] == "spec"


def test_web_search_tavily_parse(monkeypatch):
    monkeypatch.setenv("FPULSE_WEB_SEARCH_PROVIDER", "tavily")
    monkeypatch.setenv("FPULSE_WEB_SEARCH_API_KEY", "k")

    from fpulse.ai.web.search import search_web

    def fake(method, url, headers, body, timeout):
        assert body and body.get("api_key") == "k"
        return {"results": [{"title": "T", "url": "https://t.dev", "content": "snip"}]}

    results = search_web("x", _transport=fake)
    assert results[0]["snippet"] == "snip"


def test_web_search_unknown_provider(monkeypatch):
    monkeypatch.setenv("FPULSE_WEB_SEARCH_PROVIDER", "altavista")
    monkeypatch.setenv("FPULSE_WEB_SEARCH_API_KEY", "k")
    from fpulse.ai.web.search import search_web, WebSearchNotConfigured
    with pytest.raises(WebSearchNotConfigured):
        search_web("x")


# ── Runtime admin-setting toggle (no env, no restart) ─────────────────

def test_admin_setting_enables_web_access(monkeypatch):
    """The admin Settings toggle enables web access without the env var, and
    register_initial_tools reconciles the tools live."""
    import fpulse.ai.web as web
    monkeypatch.delenv("FPULSE_AI_WEB_ACCESS", raising=False)

    settings = {"ai_web_access": False}
    monkeypatch.setattr(web, "read_admin_web_settings", lambda: settings)

    reg = register_initial_tools(ToolRegistry())
    assert "web_fetch" not in reg and len(reg) == 29

    # Flip the admin setting ON → same registry reconciles to include them.
    settings["ai_web_access"] = True
    assert web.web_access_enabled() is True
    register_initial_tools(reg)
    assert "web_fetch" in reg and "web_search" in reg and len(reg) == 31

    # Flip OFF → tools are removed again (live).
    settings["ai_web_access"] = False
    register_initial_tools(reg)
    assert "web_fetch" not in reg and "web_search" not in reg and len(reg) == 29


def test_web_search_searxng_parse(monkeypatch):
    """Self-hosted SearXNG (keyless) — provider + endpoint, no API key."""
    monkeypatch.setenv("FPULSE_WEB_SEARCH_PROVIDER", "searxng")
    monkeypatch.setenv("FPULSE_WEB_SEARCH_ENDPOINT", "http://searxng.internal:8080")
    monkeypatch.delenv("FPULSE_WEB_SEARCH_API_KEY", raising=False)

    from fpulse.ai.web.search import search_web

    def fake(method, url, headers, body, timeout):
        assert method == "GET" and "format=json" in url and "searxng.internal" in url
        return {"results": [
            {"title": "Acme API", "url": "https://acme.dev/openapi.json", "content": "spec"},
            {"title": "Docs", "url": "https://acme.dev/docs", "content": "guide"},
        ]}

    results = search_web("acme api", count=5, _transport=fake)
    assert results[0]["url"] == "https://acme.dev/openapi.json"
    assert results[0]["snippet"] == "spec"


def test_web_search_searxng_requires_endpoint(monkeypatch):
    monkeypatch.setenv("FPULSE_WEB_SEARCH_PROVIDER", "searxng")
    monkeypatch.delenv("FPULSE_WEB_SEARCH_ENDPOINT", raising=False)
    from fpulse.ai.web.search import search_web, WebSearchNotConfigured
    with pytest.raises(WebSearchNotConfigured):
        search_web("x")


def test_web_search_hybridyn_gateway(monkeypatch):
    """Hybridyn managed gateway — endpoint + license token, no user signup."""
    monkeypatch.setenv("FPULSE_WEB_SEARCH_PROVIDER", "hybridyn")
    monkeypatch.setenv("FPULSE_WEB_SEARCH_ENDPOINT", "https://gateway.hybridyn.com")
    monkeypatch.setenv("FPULSE_WEB_SEARCH_API_KEY", "license-token")

    from fpulse.ai.web.search import search_web

    def fake(method, url, headers, body, timeout):
        assert method == "POST" and url.endswith("/search")
        assert headers.get("Authorization") == "Bearer license-token"
        assert body and body.get("query") == "acme"
        return {"results": [{"title": "T", "url": "https://t.dev", "snippet": "snip"}]}

    results = search_web("acme", _transport=fake)
    assert results[0]["snippet"] == "snip"


def test_tool_selector_surfaces_web_tools_for_web_prompt(monkeypatch):
    """The selector must keep web_search/web_fetch in the candidate set for a
    web prompt — otherwise the model denies web access even when it's on."""
    monkeypatch.setenv("FPULSE_AI_WEB_ACCESS", "1")
    from fpulse.ai.tools import register_initial_tools
    from fpulse.ai.tools.base import ToolTier
    from fpulse.ai.tool_selector import select_tools

    reg = register_initial_tools(ToolRegistry())
    read_tools = reg.filter_by_tiers([ToolTier.READ])
    assert len(read_tools) > 14  # cap must actually bite for this to matter

    for prompt in (
        "search the web for the FactoHR API documentation",
        "I need to get the FactoHR API",          # natural phrasing (no 'web')
        "integrate Stripe into a pipeline",
        "connect to Salesforce",
    ):
        picked = {t.name for t in select_tools(
            available_tools=read_tools, page="pipelines",
            prompt=prompt, provider_hint="openai", max_tools=14,
        )}
        assert "web_search" in picked, f"web_search missing for: {prompt!r}"
        assert "web_fetch" in picked, f"web_fetch missing for: {prompt!r}"


def test_tool_selector_web_tools_are_conditional_floor(monkeypatch):
    """When web access is on, the web tools stay available EVERY turn — even a
    prompt with no web keyword — so 'add Aconex as a source' can still search."""
    monkeypatch.setenv("FPULSE_AI_WEB_ACCESS", "1")
    from fpulse.ai.tools import register_initial_tools
    from fpulse.ai.tools.base import ToolTier
    from fpulse.ai.tool_selector import select_tools

    reg = register_initial_tools(ToolRegistry())
    read_tools = reg.filter_by_tiers([ToolTier.READ])
    for prompt in ("list my active pipelines", "add Aconex as a source", "show workspace health"):
        picked = {t.name for t in select_tools(
            available_tools=read_tools, page="pipelines",
            prompt=prompt, provider_hint="openai", max_tools=14,
        )}
        assert "web_search" in picked, f"web_search missing for: {prompt!r}"
        assert "web_fetch" in picked, f"web_fetch missing for: {prompt!r}"


def test_tool_selector_web_boost_inert_when_disabled(monkeypatch):
    """With web access off the web tools aren't registered, so the web boost
    is a no-op — it can't pull in a tool that isn't there."""
    monkeypatch.delenv("FPULSE_AI_WEB_ACCESS", raising=False)
    import fpulse.ai.web as web
    monkeypatch.setattr(web, "read_admin_web_settings", lambda: {})
    from fpulse.ai.tools import register_initial_tools
    from fpulse.ai.tools.base import ToolTier
    from fpulse.ai.tool_selector import select_tools

    reg = register_initial_tools(ToolRegistry())
    read_tools = reg.filter_by_tiers([ToolTier.READ])
    picked = {t.name for t in select_tools(
        available_tools=read_tools, page="pipelines",
        prompt="search the web for anything", provider_hint="openai", max_tools=14,
    )}
    assert "web_search" not in picked and "web_fetch" not in picked


def test_sanitizer_keeps_web_search_urls_but_still_redacts_secrets():
    """web_search URLs (public) must survive the api_key heuristic, while real
    secrets in the payload are still redacted — and other tools are untouched."""
    from fpulse.ai.sanitize import sanitize_for_llm

    long_slug = "how-to-use-oracle-aconex-apis-to-extract-documents-for-ai"
    payload = {"results": [{
        "title": "Aconex docs",
        "url": f"https://community.oracle.com/discussion/918914/{long_slug}",
        "snippet": "guide",
    }]}

    # web_search: the public URL slug is preserved (not [REDACTED:API_KEY]).
    out = sanitize_for_llm(payload, tool_name="web_search").payload
    assert long_slug in out["results"][0]["url"]
    assert "REDACTED" not in out["results"][0]["url"]

    # Non-web tool: the heuristic still fires (unchanged behaviour).
    out2 = sanitize_for_llm(payload).payload
    assert "[REDACTED:API_KEY]" in out2["results"][0]["url"]

    # Even for web_search, a genuine key in a query string is still redacted
    # (kv_secret is NOT skipped — only the length heuristic is).
    leaky = {"url": "https://api.acme.dev/v1?api_key=SUPERSECRETKEYvalue123456"}
    out3 = sanitize_for_llm(leaky, tool_name="web_search").payload
    assert "SUPERSECRETKEYvalue123456" not in out3["url"]


def test_get_search_config_prefers_admin_setting(monkeypatch):
    import fpulse.ai.web as web
    monkeypatch.setenv("FPULSE_WEB_SEARCH_PROVIDER", "brave")
    monkeypatch.setenv("FPULSE_WEB_SEARCH_API_KEY", "env-key")
    monkeypatch.setattr(web, "read_admin_web_settings",
                        lambda: {"web_search_provider": "tavily", "web_search_api_key": "ui-key"})
    provider, api_key, _ = web.get_search_config()
    assert provider == "tavily" and api_key == "ui-key"
