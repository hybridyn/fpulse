"""Tests for the REST pagination helpers used by catalog providers.

Each helper is exercised against a fake `requests.Session`-shaped
object so we don't need a live HTTP server. The fake records every
request URL/params and returns scripted bodies — that way we verify
both the iteration logic AND that we send the right cursor/offset
back on subsequent requests.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from fpulse.connections.paginator import (
    _parse_link_header,
    paginate_link_header,
    paginate_cursor_in_body,
    paginate_offset_limit,
    paginate_page_token,
    PaginationBudgetExceeded,
)


# ────────────────────────────────────────────────────────────────────
#  Fake session — only what the helpers actually call
# ────────────────────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, body: Any, headers: dict[str, str] | None = None, status: int = 200):
        self._body = body
        self.headers = headers or {}
        self.status_code = status

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeSession:
    def __init__(self, responses: list[_FakeResp]):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"method": "GET", "url": url, "params": params or {}})
        return self._responses.pop(0)

    def request(self, method, url, params=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params or {}})
        return self._responses.pop(0)


# ────────────────────────────────────────────────────────────────────
#  Link-header parser
# ────────────────────────────────────────────────────────────────────

def test_parse_link_header_simple():
    raw = '<https://api.example.com/page2>; rel="next", <https://api.example.com/last>; rel="last"'
    rels = _parse_link_header(raw)
    assert rels["next"] == "https://api.example.com/page2"
    assert rels["last"] == "https://api.example.com/last"


def test_parse_link_header_empty():
    assert _parse_link_header("") == {}


def test_parse_link_header_no_rel():
    # malformed entries are skipped, not raised
    assert _parse_link_header("<https://x>; foo=bar") == {}


# ────────────────────────────────────────────────────────────────────
#  Link-header pagination
# ────────────────────────────────────────────────────────────────────

def test_link_header_follows_next_until_absent():
    s = _FakeSession([
        _FakeResp([1, 2], {"Link": '<https://api/page2>; rel="next"'}),
        _FakeResp([3, 4], {"Link": '<https://api/page3>; rel="next"'}),
        _FakeResp([5], {}),  # last page — no Link header
    ])
    out = list(paginate_link_header(s, "https://api/page1"))
    assert out == [1, 2, 3, 4, 5]
    assert [c["url"] for c in s.calls] == [
        "https://api/page1", "https://api/page2", "https://api/page3",
    ]


def test_link_header_respects_max_items():
    s = _FakeSession([
        _FakeResp([1, 2, 3], {"Link": '<https://api/p2>; rel="next"'}),
        _FakeResp([4, 5, 6], {"Link": '<https://api/p3>; rel="next"'}),
    ])
    out = list(paginate_link_header(s, "https://api/p1", max_items=4))
    assert out == [1, 2, 3, 4]


def test_link_header_budget_exceeded():
    # Server says "next" forever — we must stop on time budget, not run away.
    def make_resp():
        return _FakeResp([1], {"Link": '<https://api/n>; rel="next"'})
    s = _FakeSession([make_resp() for _ in range(50)])
    with pytest.raises(PaginationBudgetExceeded):
        # 0-second budget guarantees the second iteration trips it
        list(paginate_link_header(s, "https://api/n", timeout_s=0))


# ────────────────────────────────────────────────────────────────────
#  Cursor-in-body pagination
# ────────────────────────────────────────────────────────────────────

def test_cursor_in_body_basic():
    s = _FakeSession([
        _FakeResp({"results": [{"id": 1}, {"id": 2}], "paging": {"next": {"after": "abc"}}}),
        _FakeResp({"results": [{"id": 3}], "paging": {}}),  # no cursor → stop
    ])
    out = list(paginate_cursor_in_body(
        s, "https://api/things",
        cursor_path=["paging", "next", "after"],
        cursor_param="after",
        items_path=["results"],
    ))
    assert [x["id"] for x in out] == [1, 2, 3]
    # Second call must echo the cursor back via the configured param.
    assert s.calls[1]["params"].get("after") == "abc"


def test_cursor_in_body_stops_when_cursor_missing():
    s = _FakeSession([
        _FakeResp({"results": [1, 2]}),  # no `paging` at all → stop after first page
    ])
    out = list(paginate_cursor_in_body(
        s, "https://api/x",
        cursor_path=["paging", "next"],
        items_path=["results"],
    ))
    assert out == [1, 2]


# ────────────────────────────────────────────────────────────────────
#  Offset/limit pagination
# ────────────────────────────────────────────────────────────────────

def test_offset_limit_walks_until_short_page():
    s = _FakeSession([
        _FakeResp({"items": [1, 2, 3]}),  # full page
        _FakeResp({"items": [4, 5, 6]}),  # full page
        _FakeResp({"items": [7]}),         # short → terminal
    ])
    out = list(paginate_offset_limit(
        s, "https://api/list", items_path=["items"], page_size=3,
    ))
    assert out == [1, 2, 3, 4, 5, 6, 7]
    # Verify the offset was incremented correctly each call.
    assert [c["params"]["offset"] for c in s.calls] == [0, 3, 6]


def test_offset_limit_uses_explicit_has_more_flag_when_provided():
    s = _FakeSession([
        _FakeResp({"items": [1, 2, 3], "has_more": True}),
        _FakeResp({"items": [4, 5, 6], "has_more": True}),
        _FakeResp({"items": [7, 8, 9], "has_more": False}),  # explicit stop
    ])
    out = list(paginate_offset_limit(
        s, "https://api/list",
        items_path=["items"], page_size=3, has_more_path=["has_more"],
    ))
    assert out == [1, 2, 3, 4, 5, 6, 7, 8, 9]


# ────────────────────────────────────────────────────────────────────
#  Page-token pagination (Google-style)
# ────────────────────────────────────────────────────────────────────

def test_page_token_basic():
    s = _FakeSession([
        _FakeResp({"items": [1, 2], "nextPageToken": "tok-2"}),
        _FakeResp({"items": [3, 4], "nextPageToken": "tok-3"}),
        _FakeResp({"items": [5]}),  # no next token
    ])
    out = list(paginate_page_token(
        s, "https://api/list",
        token_path=["nextPageToken"], items_path=["items"],
    ))
    assert out == [1, 2, 3, 4, 5]
    # The token from page N must appear as `pageToken` on page N+1.
    assert s.calls[1]["params"]["pageToken"] == "tok-2"
    assert s.calls[2]["params"]["pageToken"] == "tok-3"
