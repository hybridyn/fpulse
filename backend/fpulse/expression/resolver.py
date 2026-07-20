"""
F-Pulse Expression Engine — Mustache-style templated parameters.

Resolves `{{ ... }}` expressions inside step parameters before a node runs.

Supported syntax (kept intentionally small — evaluated through a restricted AST,
never `eval()`):

    {{ $json.field }}              current item, dotted access
    {{ $json["weird key"] }}       bracket access for non-ident keys
    {{ $itemIndex }}               0-based index of the current item
    {{ $vars.FOO }}                workspace variables (injected by executor)
    {{ $env.VAR }}                 whitelisted environment vars
    {{ $now }}                     current datetime (DateHelper)
    {{ $now.minus({days: 60}) }}   datetime arithmetic
    {{ $now.toFormat('yyyy-MM-dd')}} strftime-style formatting
    {{ $today }}                   midnight today (DateHelper)
    {{ $('Node Name').all() }}     every row from an upstream node (list of dicts)
    {{ $('Node Name').first() }}   first row (dict) — .field chains work
    {{ $('Node Name').item(3) }}   specific row
    {{ 'foo' + $json.bar }}        string concat / basic arithmetic

If a parameter value is a plain string wrapped entirely in `{{ ... }}` the
resolver returns the typed Python value (list/dict/int/datetime). Otherwise the
expression's `str()` is spliced back into the surrounding string.

Errors raise ExpressionError with the offending expression attached — the
executor surfaces that as the step error so users can fix templates quickly.
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


# ── Errors ──

class ExpressionError(Exception):
    def __init__(self, message: str, expression: str = ""):
        super().__init__(message)
        self.expression = expression


# ── Helper objects exposed inside expressions ──

_STRFTIME_MAP = [
    ("yyyy", "%Y"), ("yy", "%y"),
    ("MM", "%m"),
    ("dd", "%d"),
    ("HH", "%H"), ("mm", "%M"), ("ss", "%S"),
]


def _luxon_to_strftime(fmt: str) -> str:
    out = fmt
    for luxon, py in _STRFTIME_MAP:
        out = out.replace(luxon, py)
    return out


@dataclass
class DateHelper:
    """Subset of Luxon-like DateTime used in templates.

    2026-06-11 — added calendar-aware month/year arithmetic to
    plus/minus (previously {months}/{years} were silently ignored —
    only days/weeks/hours/min/sec worked) plus startOf/endOf for
    period boundaries, so expression-style ``{{ }}`` like
    ``$now.startOf('month').toFormat('yyyy-MM-dd')`` and
    ``$now.minus({ months: 1 })`` behave as users expect.
    """
    dt: datetime

    def minus(self, spec: dict[str, int]) -> "DateHelper":
        return DateHelper(_shift(self.dt, spec, -1))

    def plus(self, spec: dict[str, int]) -> "DateHelper":
        return DateHelper(_shift(self.dt, spec, +1))

    def startOf(self, unit: str) -> "DateHelper":
        return DateHelper(_start_of(self.dt, str(unit or "").lower()))

    def endOf(self, unit: str) -> "DateHelper":
        return DateHelper(_end_of(self.dt, str(unit or "").lower()))

    def toFormat(self, fmt: str) -> str:
        return self.dt.strftime(_luxon_to_strftime(fmt))

    def toISO(self) -> str:
        return self.dt.isoformat()

    def __str__(self) -> str:
        return self.dt.isoformat()


def _timedelta_from(spec: dict[str, int]) -> timedelta:
    days = spec.get("days", 0) + 7 * spec.get("weeks", 0)
    return timedelta(
        days=days,
        hours=spec.get("hours", 0),
        minutes=spec.get("minutes", 0),
        seconds=spec.get("seconds", 0),
    )


def _add_months(dt: datetime, months: int) -> datetime:
    """Add (or subtract) calendar months, clamping the day to month length."""
    import calendar
    m0 = dt.month - 1 + months
    year = dt.year + m0 // 12
    month = m0 % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def _shift(dt: datetime, spec: dict[str, int], sign: int) -> datetime:
    """Apply a Luxon-style duration: calendar months/years first, then the
    fixed-length parts (days/weeks/hours/minutes/seconds)."""
    months = int(spec.get("months", 0)) + 12 * int(spec.get("years", 0))
    if months:
        dt = _add_months(dt, sign * months)
    td = _timedelta_from(spec)
    return dt + td if sign > 0 else dt - td


_MIN_DAY = {"hour": 0, "minute": 0, "second": 0, "microsecond": 0}


def _start_of(dt: datetime, unit: str) -> datetime:
    if unit == "year":
        return dt.replace(month=1, day=1, **_MIN_DAY)
    if unit == "quarter":
        return dt.replace(month=3 * ((dt.month - 1) // 3) + 1, day=1, **_MIN_DAY)
    if unit == "month":
        return dt.replace(day=1, **_MIN_DAY)
    if unit == "week":  # ISO week — Monday start
        return (dt - timedelta(days=dt.weekday())).replace(**_MIN_DAY)
    if unit == "day":
        return dt.replace(**_MIN_DAY)
    raise ExpressionError(f"startOf: unknown unit '{unit}' (use year/quarter/month/week/day)")


def _end_of(dt: datetime, unit: str) -> datetime:
    if unit == "day":
        return dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    one_us = timedelta(microseconds=1)
    if unit == "year":
        return dt.replace(year=dt.year + 1, month=1, day=1, **_MIN_DAY) - one_us
    if unit == "month":
        return _add_months(dt.replace(day=1, **_MIN_DAY), 1) - one_us
    if unit == "quarter":
        start = dt.replace(month=3 * ((dt.month - 1) // 3) + 1, day=1, **_MIN_DAY)
        return _add_months(start, 3) - one_us
    if unit == "week":
        start = (dt - timedelta(days=dt.weekday())).replace(**_MIN_DAY)
        return start + timedelta(days=7) - one_us
    raise ExpressionError(f"endOf: unknown unit '{unit}' (use year/quarter/month/week/day)")


class NodeRef:
    """Handle for `$('Node Name')` — exposes .all(), .first(), .item(i), .field."""

    def __init__(self, rows: list[dict[str, Any]], label: str):
        self._rows = rows
        self._label = label

    def all(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def first(self) -> dict[str, Any]:
        return self._rows[0] if self._rows else {}

    def last(self) -> dict[str, Any]:
        return self._rows[-1] if self._rows else {}

    def item(self, i: int) -> dict[str, Any]:
        if 0 <= i < len(self._rows):
            return self._rows[i]
        raise ExpressionError(f"$('{self._label}').item({i}) out of range (len={len(self._rows)})")

    def __len__(self) -> int:
        return len(self._rows)


# ── Safe AST evaluator ──

_ALLOWED_NODES = (
    ast.Expression, ast.Constant, ast.Name, ast.Load,
    ast.Attribute, ast.Subscript, ast.Index,
    ast.Call, ast.keyword,
    ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod,
    ast.UnaryOp, ast.USub, ast.UAdd, ast.Not,
    ast.BoolOp, ast.And, ast.Or,
    ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.IfExp,
    ast.List, ast.Tuple, ast.Dict, ast.Set,
    ast.Slice,
)


def _eval(node: ast.AST, env: dict[str, Any]) -> Any:
    if not isinstance(node, _ALLOWED_NODES):
        raise ExpressionError(f"disallowed syntax: {type(node).__name__}")

    if isinstance(node, ast.Expression):
        return _eval(node.body, env)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in env:
            raise ExpressionError(f"unknown name: ${node.id}" if node.id.startswith("_") else f"unknown name: {node.id}")
        return env[node.id]
    if isinstance(node, ast.Attribute):
        obj = _eval(node.value, env)
        if isinstance(obj, dict):
            if node.attr not in obj:
                raise ExpressionError(f"no field '{node.attr}' on item")
            return obj[node.attr]
        if not hasattr(obj, node.attr):
            raise ExpressionError(f"no attribute '{node.attr}' on {type(obj).__name__}")
        attr = getattr(obj, node.attr)
        # block dunder access
        if node.attr.startswith("_"):
            raise ExpressionError(f"attribute '{node.attr}' is not accessible")
        return attr
    if isinstance(node, ast.Subscript):
        obj = _eval(node.value, env)
        key = _eval(node.slice, env) if not isinstance(node.slice, ast.Slice) else slice(
            _eval(node.slice.lower, env) if node.slice.lower else None,
            _eval(node.slice.upper, env) if node.slice.upper else None,
            _eval(node.slice.step, env) if node.slice.step else None,
        )
        return obj[key]
    if isinstance(node, ast.Call):
        func = _eval(node.func, env)
        args = [_eval(a, env) for a in node.args]
        kwargs = {kw.arg: _eval(kw.value, env) for kw in node.keywords if kw.arg}
        if not callable(func):
            raise ExpressionError("tried to call non-callable")
        return func(*args, **kwargs)
    if isinstance(node, ast.BinOp):
        left, right = _eval(node.left, env), _eval(node.right, env)
        op = node.op
        if isinstance(op, ast.Add): return left + right
        if isinstance(op, ast.Sub): return left - right
        if isinstance(op, ast.Mult): return left * right
        if isinstance(op, ast.Div): return left / right
        if isinstance(op, ast.Mod): return left % right
    if isinstance(node, ast.UnaryOp):
        v = _eval(node.operand, env)
        if isinstance(node.op, ast.USub): return -v
        if isinstance(node.op, ast.UAdd): return +v
        if isinstance(node.op, ast.Not): return not v
    if isinstance(node, ast.BoolOp):
        vals = [_eval(v, env) for v in node.values]
        if isinstance(node.op, ast.And): return all(vals) and vals[-1]
        return next((v for v in vals if v), vals[-1])
    if isinstance(node, ast.Compare):
        left = _eval(node.left, env)
        for op, comp in zip(node.ops, node.comparators):
            right = _eval(comp, env)
            ok = (
                (isinstance(op, ast.Eq) and left == right)
                or (isinstance(op, ast.NotEq) and left != right)
                or (isinstance(op, ast.Lt) and left < right)
                or (isinstance(op, ast.LtE) and left <= right)
                or (isinstance(op, ast.Gt) and left > right)
                or (isinstance(op, ast.GtE) and left >= right)
            )
            if not ok:
                return False
            left = right
        return True
    if isinstance(node, ast.IfExp):
        return _eval(node.body, env) if _eval(node.test, env) else _eval(node.orelse, env)
    if isinstance(node, ast.List):
        return [_eval(e, env) for e in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_eval(e, env) for e in node.elts)
    if isinstance(node, ast.Dict):
        # JS-style object literals allow unquoted identifier keys: { days: 7 }.
        # Python parses such a key as a bare Name — treat it as the string key
        # (its identifier) rather than a variable lookup. Quoted keys parse as
        # Constants and pass through _eval unchanged, so {'days': 7} still works.
        return {
            (k.id if isinstance(k, ast.Name) else _eval(k, env)): _eval(v, env)
            for k, v in zip(node.keys, node.values)
        }

    raise ExpressionError(f"unsupported expression: {ast.dump(node)}")


# ── Preprocessing: turn $json / $now / $('X') into plain identifiers ──

_NODE_REF_RE = re.compile(r"\$\(\s*(['\"])(.*?)\1\s*\)")


def _preprocess(expr: str, node_refs: dict[str, str]) -> str:
    """Replace `$('Node Name')` with a synthetic identifier and strip leading `$` from helpers."""
    def _sub(match: re.Match) -> str:
        name = match.group(2)
        key = f"__noderef_{len(node_refs)}__"
        node_refs[key] = name
        return key

    out = _NODE_REF_RE.sub(_sub, expr)
    # $json, $now, $today, $itemIndex, $vars, $env → stripped
    out = re.sub(r"\$(json|now|today|itemIndex|vars|env)\b", r"\1", out)
    return out


# ── Main resolver ──

def _build_env(
    ctx_results: dict[str, list[dict[str, Any]]],
    node_labels: dict[str, str],
    item: dict[str, Any] | None,
    item_index: int,
    vars_: dict[str, Any],
    node_refs: dict[str, str],
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    today = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)

    label_to_rows: dict[str, list[dict[str, Any]]] = {}
    for step_id, rows in ctx_results.items():
        label = node_labels.get(step_id, step_id)
        label_to_rows[label] = rows
        label_to_rows[step_id] = rows

    env: dict[str, Any] = {
        "json": item or {},
        "itemIndex": item_index,
        "now": DateHelper(now),
        "today": DateHelper(today),
        "vars": _AttrDict(vars_),
        "env": _AttrDict({k: v for k, v in os.environ.items() if k.startswith("FPULSE_")}),
        # safe built-ins
        "len": len, "str": str, "int": int, "float": float, "bool": bool,
        "list": list, "dict": dict, "min": min, "max": max, "sum": sum,
        "abs": abs, "round": round,
    }

    for synthetic, label in node_refs.items():
        rows = label_to_rows.get(label, [])
        env[synthetic] = NodeRef(rows, label)

    return env


class _AttrDict(dict):
    def __getattr__(self, name):
        if name in self:
            return self[name]
        raise ExpressionError(f"no such key: {name}")


_EXPR_RE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)


def _resolve_string(
    s: str,
    ctx_results: dict[str, list[dict[str, Any]]],
    node_labels: dict[str, str],
    item: dict[str, Any] | None,
    item_index: int,
    vars_: dict[str, Any],
) -> Any:
    """Resolve every {{ ... }} in a string. If the whole string is one expression, return typed value."""
    matches = list(_EXPR_RE.finditer(s))
    if not matches:
        return s

    whole = len(matches) == 1 and matches[0].group(0).strip() == s.strip()

    def eval_one(raw: str) -> Any:
        expr = raw.strip()
        node_refs: dict[str, str] = {}
        processed = _preprocess(expr, node_refs)
        env = _build_env(ctx_results, node_labels, item, item_index, vars_, node_refs)
        try:
            tree = ast.parse(processed, mode="eval")
        except SyntaxError as e:
            raise ExpressionError(f"syntax error: {e.msg}", expression=expr)
        try:
            return _eval(tree, env)
        except ExpressionError as e:
            e.expression = expr
            raise

    if whole:
        return eval_one(matches[0].group(1))

    def _sub(m: re.Match) -> str:
        val = eval_one(m.group(1))
        if isinstance(val, DateHelper):
            return str(val)
        return str(val)

    return _EXPR_RE.sub(_sub, s)


def resolve_expressions(
    params: Any,
    ctx_results: dict[str, list[dict[str, Any]]],
    node_labels: dict[str, str],
    item: dict[str, Any] | None = None,
    item_index: int = 0,
    vars_: dict[str, Any] | None = None,
) -> Any:
    """Recursively resolve {{ ... }} expressions inside params.

    params may be a dict, list, string, or scalar. Keys prefixed with `_`
    are passed through unchanged (they're internal wiring like _input_step_ids).
    """
    vars_ = vars_ or {}

    if isinstance(params, dict):
        return {
            k: v if k.startswith("_") else resolve_expressions(
                v, ctx_results, node_labels, item, item_index, vars_
            )
            for k, v in params.items()
        }
    if isinstance(params, list):
        return [
            resolve_expressions(v, ctx_results, node_labels, item, item_index, vars_)
            for v in params
        ]
    if isinstance(params, str):
        return _resolve_string(params, ctx_results, node_labels, item, item_index, vars_)
    return params
