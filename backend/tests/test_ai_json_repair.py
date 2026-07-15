"""Tests for fpulse.ai.json_repair.parse_tolerant.

Each case is one shape the helper is expected to handle. New small-model
defects discovered in production should be added here so the fix is
regression-locked.
"""

from __future__ import annotations

from fpulse.ai.json_repair import parse_tolerant


# ── Strict / fast-path ────────────────────────────────────────────────────

def test_valid_object_unchanged():
    r = parse_tolerant('{"a": 1, "b": "hi"}')
    assert r.value == {"a": 1, "b": "hi"}
    assert r.repaired is False
    assert r.error is None


def test_valid_array_unchanged():
    r = parse_tolerant('[1, 2, "three"]')
    assert r.value == [1, 2, "three"]
    assert r.repaired is False


def test_dict_passthrough():
    src = {"foo": "bar"}
    r = parse_tolerant(src)
    assert r.value is src  # exact same object — no copy
    assert r.repaired is False


def test_list_passthrough():
    src = [1, 2, 3]
    r = parse_tolerant(src)
    assert r.value is src
    assert r.repaired is False


def test_bytes_decoded():
    r = parse_tolerant(b'{"a": 1}')
    assert r.value == {"a": 1}
    assert r.repaired is False


def test_empty_string():
    r = parse_tolerant("")
    assert r.value == {}
    assert r.repaired is False
    assert r.error == "empty"


def test_whitespace_only():
    r = parse_tolerant("   \n  ")
    assert r.value == {}
    assert r.error == "empty"


# ── Repair: trailing comma ────────────────────────────────────────────────

def test_trailing_comma_object():
    r = parse_tolerant('{"a": 1, "b": 2,}')
    assert r.value == {"a": 1, "b": 2}
    assert r.repaired is True


def test_trailing_comma_array():
    r = parse_tolerant('[1, 2, 3,]')
    assert r.value == [1, 2, 3]
    assert r.repaired is True


def test_multiple_trailing_commas_nested():
    r = parse_tolerant('{"a": [1, 2,], "b": {"c": 3,},}')
    assert r.value == {"a": [1, 2], "b": {"c": 3}}
    assert r.repaired is True


# ── Repair: Python literals ───────────────────────────────────────────────

def test_python_true_false_none():
    r = parse_tolerant('{"a": True, "b": False, "c": None}')
    assert r.value == {"a": True, "b": False, "c": None}
    assert r.repaired is True


def test_python_literals_in_string_left_alone():
    # The word "None" appears inside a string — must not be rewritten.
    r = parse_tolerant('{"label": "None of the above"}')
    assert r.value == {"label": "None of the above"}


def test_word_boundary_protects_identifiers():
    # "Truthy" contains "True" but isn't the literal — leave alone.
    # We can only test indirectly: a payload that becomes valid only if
    # we DON'T rewrite the substring.
    r = parse_tolerant('{"x": "Truthy"}')
    assert r.value == {"x": "Truthy"}


# ── Repair: markdown code fences ──────────────────────────────────────────

def test_fenced_json_block():
    r = parse_tolerant('```json\n{"a": 1}\n```')
    assert r.value == {"a": 1}
    assert r.repaired is True


def test_fenced_no_language_tag():
    r = parse_tolerant('```\n{"a": 1}\n```')
    assert r.value == {"a": 1}
    assert r.repaired is True


# ── Repair: control characters inside strings ─────────────────────────────

def test_raw_newline_in_string():
    payload = '{"msg": "hello\nworld"}'
    r = parse_tolerant(payload)
    assert r.value == {"msg": "hello\nworld"}
    assert r.repaired is True


def test_raw_tab_in_string():
    payload = '{"msg": "col1\tcol2"}'
    r = parse_tolerant(payload)
    assert r.value == {"msg": "col1\tcol2"}
    assert r.repaired is True


# ── Repair: single quotes ─────────────────────────────────────────────────

def test_all_single_quotes():
    r = parse_tolerant("{'name': 'pipeline_a', 'count': 3}")
    assert r.value == {"name": "pipeline_a", "count": 3}
    assert r.repaired is True


def test_mixed_quotes_not_repaired():
    # We refuse to touch payloads that already have any double quotes —
    # too dangerous. Strict parse will succeed on this one anyway.
    r = parse_tolerant('{"a": "b", "c": "d"}')
    assert r.value == {"a": "b", "c": "d"}
    assert r.repaired is False


# ── Repair: trailing chatter ──────────────────────────────────────────────

def test_trailing_chatter_after_object():
    r = parse_tolerant('{"a": 1}\n\nNote: this is my answer.')
    assert r.value == {"a": 1}
    assert r.repaired is True


def test_trailing_chatter_after_array():
    r = parse_tolerant('[1, 2, 3]  // here is the list')
    assert r.value == [1, 2, 3]
    assert r.repaired is True


# ── Failure cases ─────────────────────────────────────────────────────────

def test_garbage_returns_empty_with_error():
    r = parse_tolerant("not json at all !@#$")
    assert r.value == {}
    assert r.repaired is False
    assert r.error is not None and "json-decode" in r.error


def test_unsupported_type():
    r = parse_tolerant(12345)  # type: ignore[arg-type]
    assert r.value == {}
    assert r.repaired is False
    assert r.error and "unsupported-type" in r.error


# ── Combined small-model defects ──────────────────────────────────────────

def test_combined_fence_trailing_comma_python_literal():
    payload = '```json\n{"draft_id": "d-123", "dry_run": True, "tags": ["a", "b",],}\n```'
    r = parse_tolerant(payload)
    assert r.value == {"draft_id": "d-123", "dry_run": True, "tags": ["a", "b"]}
    assert r.repaired is True


def test_combined_newline_and_trailing_comma():
    payload = '{"msg": "line1\nline2", "ok": True,}'
    r = parse_tolerant(payload)
    assert r.value == {"msg": "line1\nline2", "ok": True}
    assert r.repaired is True
