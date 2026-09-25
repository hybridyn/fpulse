"""OpenAI-schema probe param compatibility (fpulse.api.ai_config).

Covers the fix for `400 Unsupported parameter: 'max_tokens' ... Use
'max_completion_tokens' instead` on newer OpenAI models: send max_tokens, and
only on that specific 400 retry once with max_completion_tokens. (asyncio_mode
= auto, so plain `async def` tests run without a marker.)
"""

from __future__ import annotations

from fpulse.api import ai_config


class _FakeResp:
    def __init__(self, status: int, text: str = ""):
        self.status_code = status
        self.text = text


class _FakeClient:
    def __init__(self, first_400: bool = True):
        self.first_400 = first_400
        self.calls: list[dict] = []

    async def post(self, url, headers=None, json=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        if self.first_400 and "max_tokens" in json:
            return _FakeResp(400, "Unsupported parameter: 'max_tokens' is not supported "
                                  "with this model. Use 'max_completion_tokens' instead.")
        return _FakeResp(200, "ok")


async def test_retries_with_max_completion_tokens_on_400():
    c = _FakeClient(first_400=True)
    resp = await ai_config._post_openai_chat(c, "http://x", {"h": "v"}, "gpt-5.6-luna")
    assert resp.status_code == 200
    assert len(c.calls) == 2
    assert "max_tokens" in c.calls[0]["json"] and "max_completion_tokens" not in c.calls[0]["json"]
    assert "max_completion_tokens" in c.calls[1]["json"] and "max_tokens" not in c.calls[1]["json"]


async def test_old_model_makes_a_single_call():
    c = _FakeClient(first_400=False)
    resp = await ai_config._post_openai_chat(c, "http://x", {"h": "v"}, "gpt-4o-mini")
    assert resp.status_code == 200
    assert len(c.calls) == 1  # max_tokens accepted -> no retry


async def test_extra_headers_are_merged():
    c = _FakeClient(first_400=False)
    await ai_config._post_openai_chat(
        c, "http://x", {"Authorization": "Bearer k"}, "m",
        extra_headers={"X-Title": "F-Pulse", "HTTP-Referer": "https://ex"},
    )
    sent = c.calls[0]["headers"]
    assert sent["Authorization"] == "Bearer k"
    assert sent["X-Title"] == "F-Pulse"
    assert sent["HTTP-Referer"] == "https://ex"
