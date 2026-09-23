"""Readable-SQL PREVIEW compiler for F-Pulse pipelines.

Compiles the **SQL-native subset** of a pipeline (a linear source -> filter ->
aggregate -> sort -> limit -> sink chain) into a single annotated, CTE-chained
SQL script a user can read, git-diff, and — for the compiled part — run in
DuckDB without F-Pulse. This is the transparency + anti-lock-in feature: see
exactly what the engine would run, and take it with you.

It is a **PREVIEW, not a guaranteed round-trip.** F-Pulse executes pipelines
imperatively (each node builds a DuckDB relation at runtime), and many nodes
(API/AI sources, flow-control, and complex transforms) cannot be expressed as
one SQL statement. Those are emitted as explicit ``-- [boundary] ...`` comments
so the output never silently misrepresents what runs. Nodes that ARE compiled
mirror the exact SQL their runtime node builds (e.g. the aggregate compiler
below reproduces ``nodes/aggregate.py``).

Contract: input is a workflow dict shaped like ``fpulse.ir.schema.Workflow``
(``steps: [{id, type, params}]`` + ``connections: [{from_step, to_step}]``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class CompiledSql:
    sql: str
    compiled_steps: list[str] = field(default_factory=list)
    boundary_steps: list[dict] = field(default_factory=list)

    @property
    def fully_compiled(self) -> bool:
        """True when every non-sink step compiled to SQL."""
        return not self.boundary_steps


_IDENT_RE = re.compile(r"[^A-Za-z0-9_]+")


def _step_type(step: dict) -> str:
    t = step.get("type", "")
    return getattr(t, "value", t)  # accept enum or str


def _is_sink(t: str) -> bool:
    return t == "destination" or t.endswith("_sink")


def _is_source(t: str) -> bool:
    return t == "source" or t.endswith("_source")


def _cte_name(step: dict) -> str:
    raw = str(step.get("id") or step.get("label") or "step")
    name = _IDENT_RE.sub("_", raw).strip("_").lower() or "step"
    return f"s_{name}"


# ── per-node compilers: (step, input_cte|None) -> sql body str, or None (boundary) ──

def _compile_source(step: dict, _input: Optional[str]) -> Optional[str]:
    p = step.get("params", {}) or {}
    t = _step_type(step)
    query = (p.get("query") or "").strip()
    if query:
        return f"/* source: {t} */\n{query}"
    path = p.get("path") or p.get("file_path") or p.get("file")
    if t in ("csv_source", "file_source") and path:
        return f"/* source: {t} */\nSELECT * FROM read_csv_auto('{path}')"
    if t == "parquet_source" and path:
        return f"/* source: {t} */\nSELECT * FROM read_parquet('{path}')"
    if t == "json_source" and path:
        return f"/* source: {t} */\nSELECT * FROM read_json_auto('{path}')"
    table = p.get("table")
    if table:
        schema = p.get("schema")
        ref = f'"{schema}"."{table}"' if schema else f'"{table}"'
        return f"/* source: {t} */\nSELECT * FROM {ref}"
    return None  # not SQL-expressible (api/kafka/saas/etc. or missing path/table)


def _compile_filter(step: dict, input_cte: str) -> Optional[str]:
    p = step.get("params", {}) or {}
    if p.get("mode", "expression") != "expression":
        return None  # rules-mode filter -> boundary (compound rule groups)
    cond = (p.get("condition") or "TRUE").strip()
    return f"SELECT *\nFROM {input_cte}\nWHERE {cond}"


def _compile_aggregate(step: dict, input_cte: str) -> Optional[str]:
    """Mirror of nodes/aggregate.py so the preview matches what runs."""
    p = step.get("params", {}) or {}
    group_by = p.get("group_by", [])
    if isinstance(group_by, str):
        group_by = [g.strip() for g in group_by.split(",") if g.strip()]

    functions = p.get("functions", [])
    if isinstance(functions, dict):
        functions = [{"column": c, "function": fn} for c, fn in functions.items()]
    norm: list[dict] = []
    for f in (functions or []):
        if isinstance(f, str):
            norm.append({"column": f, "function": "COUNT"})
        elif isinstance(f, dict):
            norm.append(f)
    functions = norm

    having = (p.get("having") or "").strip()
    order_by = (p.get("order_by") or "").strip()
    group_cols = ", ".join(f'"{g}"' for g in group_by) if group_by else ""

    agg_exprs: list[str] = []
    for f in functions:
        col = f.get("column", "*")
        func = str(f.get("function", "COUNT")).upper()
        alias = f.get("alias", "")
        if func == "CUSTOM":
            expr = f.get("expression", "")
            if expr:
                agg_exprs.append(f'{expr} AS "{alias or "custom_agg"}"')
            continue
        if not alias:
            alias = f"{func.lower()}_{col}" if col != "*" else f"{func.lower()}_all"
        if func == "COUNT" and col == "*":
            agg_exprs.append(f'COUNT(*) AS "{alias}"')
        elif func == "COUNT_DISTINCT":
            agg_exprs.append(f'COUNT(DISTINCT "{col}") AS "{alias}"')
        elif func == "MEDIAN":
            agg_exprs.append(f'MEDIAN("{col}") AS "{alias}"')
        elif func in ("PERCENTILE_CONT", "PERCENTILE_DISC"):
            pct = float(f.get("percentile", 0.5))
            agg_exprs.append(f'{func}({pct}) WITHIN GROUP (ORDER BY "{col}") AS "{alias}"')
        elif func == "STRING_AGG":
            sep = f.get("separator", ", ")
            agg_exprs.append(f"STRING_AGG(\"{col}\", '{sep}') AS \"{alias}\"")
        elif func in ("FIRST", "LAST"):
            agg_exprs.append(f'{func}("{col}") AS "{alias}"')
        else:
            agg_exprs.append(f'{func}("{col}") AS "{alias}"')
    if not agg_exprs:
        agg_exprs = ['COUNT(*) AS "count"']

    select_parts = ([group_cols] if group_cols else []) + agg_exprs
    sql = f"SELECT {', '.join(select_parts)}\nFROM {input_cte}"
    if group_cols:
        sql += f"\nGROUP BY {group_cols}"
    if having:
        sql += f"\nHAVING {having}"
    if order_by:
        sql += f"\nORDER BY {order_by}"
    return sql


def _compile_sort(step: dict, input_cte: str) -> Optional[str]:
    p = step.get("params", {}) or {}
    order_by = (p.get("order_by") or "").strip()
    if not order_by:
        keys = p.get("keys") or p.get("columns") or []
        if isinstance(keys, list) and keys:
            order_by = ", ".join(str(k) for k in keys)
    if not order_by:
        return None
    return f"SELECT *\nFROM {input_cte}\nORDER BY {order_by}"


def _compile_sample(step: dict, input_cte: str) -> Optional[str]:
    p = step.get("params", {}) or {}
    n = p.get("sample_rows") or p.get("n") or p.get("limit")
    if not n:
        return None
    try:
        n = int(n)
    except (TypeError, ValueError):
        return None
    return f"SELECT *\nFROM {input_cte}\nLIMIT {n}"


def _compile_deduplicate(step: dict, input_cte: str) -> Optional[str]:
    """Readable equivalent of nodes/deduplicate.py (ROW_NUMBER + keep rn=1),
    expressed as DuckDB QUALIFY. keep_last reverses each order direction."""
    p = step.get("params", {}) or {}
    if p.get("emit_duplicates"):
        return None  # dual-output (unique/duplicate) — not a single SELECT
    keys = p.get("key") or p.get("columns")
    if isinstance(keys, str):
        keys = [keys]
    if not keys:
        return None
    key_cols = ", ".join(f'"{k}"' for k in keys)

    order_rules: list[tuple[str, str]] = []
    for tok in str(p.get("order_by") or "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        bits = tok.split()
        col = bits[0]
        dirn = bits[1].upper() if len(bits) > 1 else "ASC"
        order_rules.append((col, dirn if dirn in ("ASC", "DESC") else "ASC"))
    if p.get("strategy", "keep_first") == "keep_last":
        order_rules = [(c, "ASC" if d == "DESC" else "DESC") for c, d in order_rules]
    order_clause = (" ORDER BY " + ", ".join(f'"{c}" {d}' for c, d in order_rules)) if order_rules else ""

    return (f"SELECT *\nFROM {input_cte}\n"
            f"QUALIFY ROW_NUMBER() OVER (PARTITION BY {key_cols}{order_clause}) = 1")


def _compile_rename(step: dict, input_cte: str) -> Optional[str]:
    """Mirror of nodes/activities.py RenameNode — projects the mapped columns
    (empty mappings = passthrough)."""
    mappings = (step.get("params", {}) or {}).get("mappings", {})
    if not mappings:
        return f"SELECT *\nFROM {input_cte}"
    parts = ", ".join(f'"{old}" AS "{new}"' for old, new in mappings.items())
    return f"SELECT {parts}\nFROM {input_cte}"


def _compile_typecast(step: dict, input_cte: str) -> Optional[str]:
    """Mirror of nodes/activities.py TypecastNode via DuckDB `* REPLACE` (keeps
    every column, casts the mapped ones) — no live column list needed."""
    casts = (step.get("params", {}) or {}).get("casts", {})
    if not casts:
        return f"SELECT *\nFROM {input_cte}"
    replaces = ", ".join(f'CAST("{c}" AS {t}) AS "{c}"' for c, t in casts.items())
    return f"SELECT * REPLACE ({replaces})\nFROM {input_cte}"


def _compile_join(step: dict, input_ids: list[str], cte_of: dict[str, str]) -> Optional[str]:
    """Mirror of nodes/join.py for the 2-input case. The runtime's collision-safe
    default projection needs live column names, so the preview emits `L.*, R.*`
    (with a note) unless explicit select_left/select_right are given."""
    p = step.get("params", {}) or {}
    if len(input_ids) != 2:
        return None
    left_id = p.get("left_input_id") or ""
    li = input_ids.index(left_id) if left_id in input_ids else 0
    ri = 1 - li
    left = cte_of[input_ids[li]]
    right = cte_of[input_ids[ri]]

    def sub(frag: str) -> str:
        return frag.replace("__join_left", left).replace("__join_right", right)

    jt = str(p.get("join_type", "INNER")).upper()
    km = p.get("key_mode", "same_key")

    if jt == "CROSS":
        on_clause = ""
    elif km == "custom":
        on_clause = sub((p.get("custom_on") or "TRUE").strip())
    elif km == "mapped_keys":
        parts = [f'{left}."{pr.get("left","")}" {pr.get("operator","=")} {right}."{pr.get("right","")}"'
                 for pr in (p.get("key_pairs") or []) if pr.get("left") and pr.get("right")]
        if not parts:
            return None
        on_clause = " AND ".join(parts)
    else:
        jk = p.get("join_key", [])
        if isinstance(jk, str):
            jk = [k.strip() for k in jk.split(",") if k.strip()]
        if not jk:
            return None
        on_clause = " AND ".join(f'{left}."{k}" = {right}."{k}"' for k in jk)

    sl = (p.get("select_left") or "").strip()
    sr = (p.get("select_right") or "").strip()
    if sl or sr:
        proj = f"{sub(sl) if sl else f'{left}.*'}, {sub(sr) if sr else f'{right}.*'}"
        note = ""
    else:
        proj = f"{left}.*, {right}.*"
        note = "/* runtime builds a collision-safe projection over the real columns */\n"

    if jt == "SEMI":
        return f"SELECT {left}.*\nFROM {left}\nWHERE EXISTS (SELECT 1 FROM {right} WHERE {on_clause})"
    if jt == "ANTI":
        return f"SELECT {left}.*\nFROM {left}\nWHERE NOT EXISTS (SELECT 1 FROM {right} WHERE {on_clause})"
    if jt == "CROSS":
        return f"{note}SELECT {proj}\nFROM {left}\nCROSS JOIN {right}"
    keyword = "FULL OUTER" if jt == "FULL" else jt
    return f"{note}SELECT {proj}\nFROM {left}\n{keyword} JOIN {right} ON {on_clause}"


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _compile_window(step: dict, input_cte: str) -> Optional[str]:
    """Mirror of nodes/activities.py WindowNode — one or more window functions
    over a shared PARTITION BY / ORDER BY / frame spec."""
    p = step.get("params", {}) or {}
    partition_by = p.get("partition_by", []) or []
    order_by = p.get("order_by", []) or []
    order_direction = str(p.get("order_direction") or p.get("sort_direction") or "ASC").upper()
    frame = str(p.get("frame") or "").strip()

    partition_clause = ("PARTITION BY " + ", ".join(f'"{c}"' for c in partition_by)) if partition_by else ""
    order_parts: list[str] = []
    for o in order_by:
        toks = str(o).split()
        if not toks:
            continue
        col = toks[0]
        inline = toks[1].upper() if len(toks) > 1 else ""
        if inline not in ("ASC", "DESC"):
            inline = ""
        order_parts.append(f'"{col}" {inline or order_direction}')
    order_clause = ("ORDER BY " + ", ".join(order_parts)) if order_parts else ""
    window_spec = f"{partition_clause} {order_clause} {frame}".strip()

    wfs = p.get("window_functions") or p.get("functions") or []
    if not wfs:
        wfs = [{"function": p.get("function", "ROW_NUMBER()"), "alias": p.get("alias", "row_num")}]

    exprs: list[str] = []
    for wf in wfs:
        fn = str(wf.get("function", "ROW_NUMBER")).upper().rstrip("()")
        col = (wf.get("column") or "").strip()
        alias = wf.get("alias", f"{fn.lower()}_result")
        offset = _safe_int(wf.get("offset", 1), 1)
        n = _safe_int(wf.get("n", 4), 4)
        if fn in ("ROW_NUMBER", "RANK", "DENSE_RANK", "CUME_DIST", "PERCENT_RANK"):
            expr = f"{fn}()"
        elif fn == "NTILE":
            expr = f"NTILE({n})"
        elif fn in ("LAG", "LEAD"):
            expr = f'{fn}("{col}", {offset})' if col else f"{fn}(*, {offset})"
        elif fn in ("FIRST_VALUE", "LAST_VALUE"):
            expr = f'{fn}("{col}")' if col else f"{fn}(*)"
        elif fn == "NTH_VALUE":
            expr = f'{fn}("{col}", {n})' if col else f"NTH_VALUE(*, {n})"
        elif fn in ("SUM", "AVG", "MIN", "MAX", "COUNT"):
            expr = f'{fn}("{col}")' if col else f"{fn}(*)"
        else:
            expr = wf.get("function", "ROW_NUMBER()")
        exprs.append(f'{expr} OVER ({window_spec}) AS "{alias}"')

    return f"SELECT *, {', '.join(exprs)}\nFROM {input_cte}"


def _compile_union(step: dict, input_ids: list[str], cte_of: dict[str, str]) -> Optional[str]:
    """Mirror of nodes/activities.py UnionNode — stack N inputs."""
    op = {"all": "UNION ALL", "distinct": "UNION", "by_name": "UNION ALL BY NAME"}.get(
        (step.get("params", {}) or {}).get("mode", "all")
    )
    if op is None or len(input_ids) < 2:
        return None
    parts = [f"SELECT * FROM {cte_of[i]}" for i in input_ids]
    return ("\n" + op + "\n").join(parts)


def _compile_derived_column(step: dict, input_cte: str) -> Optional[str]:
    """Mirror of nodes/activities.py DerivedColumnNode — add computed columns,
    with optional per-column window wrapping and replace-via-EXCLUDE."""
    cols = (step.get("params", {}) or {}).get("columns", [])
    if not cols:
        return f"SELECT *\nFROM {input_cte}"
    replaced: list[str] = []
    parts: list[str] = []
    for c in cols:
        name = c.get("name") or "derived"
        expr = c.get("expression") or "NULL"
        window = c.get("window") or {}
        if window and "OVER" not in expr.upper():
            clauses = []
            if window.get("partition_by"):
                clauses.append("PARTITION BY " + ", ".join(f'"{p}"' for p in window["partition_by"]))
            if window.get("order_by"):
                clauses.append("ORDER BY " + ", ".join(f'"{o}"' for o in window["order_by"]))
            expr = f"{expr} OVER ({' '.join(clauses)})"
        if c.get("replace"):
            replaced.append(name)
        parts.append(f'{expr} AS "{name}"')
    extras = ", ".join(parts)
    if replaced:
        exclude = ", ".join(f'"{c}"' for c in replaced)
        return f"SELECT * EXCLUDE ({exclude}), {extras}\nFROM {input_cte}"
    return f"SELECT *, {extras}\nFROM {input_cte}"


def _compile_data_wrangler(step: dict, input_cte: str) -> Optional[str]:
    """Compile a Data Wrangler node by REUSING its own recipe compiler
    (``compile_wrangle``) — the exact code the engine runs, so the preview is
    faithful and we don't duplicate the sub-op SQL (filter/select/rename/cast/
    derive/group_by/sort/dedupe/sample). A sub-step needing a live connection
    (e.g. flatten's struct probe) falls back to a boundary."""
    steps = (step.get("params", {}) or {}).get("steps") or []
    if not steps:
        return f"SELECT *\nFROM {input_cte}"
    try:
        from fpulse.nodes.data_wrangler import compile_wrangle
        return compile_wrangle(steps, input_cte, conn=None)
    except Exception:  # noqa: BLE001
        return None


def _compile_transform(step: dict, input_cte: str) -> Optional[str]:
    """SQL Transform (nodes/transform.py) — the user's raw SQL, which reads from
    `source_table` / `input`. Rebind those to the upstream CTE. Best-effort: a
    multi-input transform referencing upstream by label falls to a boundary via
    the walker (this handles the common single-input case)."""
    expr = ((step.get("params", {}) or {}).get("expression") or "").strip()
    if not expr:
        return None
    body = re.sub(r"\bsource_table\b", input_cte, expr)
    body = re.sub(r"\binput\b", input_cte, body)
    return f"/* SQL Transform (verbatim; source_table/input -> {input_cte}) */\n{body}"


def _compile_pivot(step: dict, input_cte: str) -> Optional[str]:
    """Mirror of nodes/activities.py PivotNode via DuckDB's PIVOT statement
    (auto-detects values at runtime). The optional fill_value post-pass needs
    live columns, so it's noted rather than reproduced."""
    p = step.get("params", {}) or {}
    pivot_col = p.get("pivot_column", "")
    value_col = p.get("value_column", "")
    if not pivot_col or not value_col:
        return None
    agg = p.get("agg_function", "SUM")
    pivot_values = p.get("pivot_values") or []
    if isinstance(pivot_values, str):
        pivot_values = [s.strip() for s in pivot_values.split(",") if s.strip()]
    in_clause = ""
    if pivot_values:
        vals = ", ".join("'" + str(v).replace("'", "''") + "'" for v in pivot_values)
        in_clause = f" IN ({vals})"
    group_by = p.get("group_by") or []
    group_clause = (" GROUP BY " + ", ".join(f'"{g}"' for g in group_by)) if group_by else ""
    note = ""
    if p.get("fill_value") not in ("", None):
        note = "/* fill_value applied at runtime over the generated pivot columns */\n"
    return (f'{note}PIVOT {input_cte} ON "{pivot_col}"{in_clause} '
            f'USING {agg}("{value_col}"){group_clause}')


def _compile_unpivot(step: dict, input_cte: str) -> Optional[str]:
    """Mirror of nodes/activities.py UnpivotNode via DuckDB's UNPIVOT."""
    p = step.get("params", {}) or {}
    columns = p.get("columns") or []
    if not columns:
        return None
    name_col = p.get("name_column", "attribute")
    value_col = p.get("value_column", "value")
    id_columns = p.get("id_columns") or []
    col_list = ", ".join(f'"{c}"' for c in columns)
    if id_columns:
        keep = ", ".join(f'"{c}"' for c in [*id_columns, *columns])
        src = f"(SELECT {keep} FROM {input_cte})"
    else:
        src = input_cte
    nulls_kw = "INCLUDE NULLS " if p.get("include_nulls") else ""
    return (f'SELECT * FROM {src} UNPIVOT {nulls_kw}'
            f'("{value_col}" FOR "{name_col}" IN ({col_list}))')


# Single-input compilers: (step, input_cte) -> sql body | None
_COMPILERS: dict[str, Callable[[dict, Any], Optional[str]]] = {
    "filter": _compile_filter,
    "aggregate": _compile_aggregate,
    "sort": _compile_sort,
    "sample": _compile_sample,
    "deduplicate": _compile_deduplicate,
    "rename": _compile_rename,
    "typecast": _compile_typecast,
    "window": _compile_window,
    "derived_column": _compile_derived_column,
    "data_wrangler": _compile_data_wrangler,
    "transform": _compile_transform,
    "pivot": _compile_pivot,
    "unpivot": _compile_unpivot,
}

# Multi-input compilers: (step, input_ids_in_edge_order, cte_of) -> sql body | None
_MULTI_COMPILERS: dict[str, Callable[[dict, list[str], dict[str, str]], Optional[str]]] = {
    "join": _compile_join,
    "union": _compile_union,
}


def _toposort(step_ids: list[str], preds: dict[str, list[str]]) -> list[str]:
    """Kahn topological order; falls back to input order on a cycle."""
    indeg = {sid: len(preds.get(sid, [])) for sid in step_ids}
    succ: dict[str, list[str]] = {sid: [] for sid in step_ids}
    for sid in step_ids:
        for p in preds.get(sid, []):
            if p in succ:
                succ[p].append(sid)
    queue = [sid for sid in step_ids if indeg[sid] == 0]
    order: list[str] = []
    while queue:
        n = queue.pop(0)
        order.append(n)
        for m in succ[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)
    if len(order) != len(step_ids):  # cycle — degrade gracefully
        return list(step_ids)
    return order


def compile_workflow_to_sql(workflow: dict) -> CompiledSql:
    steps = {s["id"]: s for s in workflow.get("steps", []) if s.get("id")}
    preds: dict[str, list[str]] = {sid: [] for sid in steps}
    for c in workflow.get("connections", []):
        f, t = c.get("from_step"), c.get("to_step")
        if f in steps and t in steps:
            preds[t].append(f)

    order = _toposort(list(steps), preds)

    cte_of: dict[str, str] = {}
    ctes: list[tuple[str, str]] = []
    boundaries: list[dict] = []
    compiled: list[str] = []
    final_cte: Optional[str] = None

    for sid in order:
        step = steps[sid]
        t = _step_type(step)
        ins = preds[sid]

        if _is_sink(t):
            if len(ins) == 1 and ins[0] in cte_of:
                final_cte = cte_of[ins[0]]
                compiled.append(sid)
            else:
                boundaries.append({"id": sid, "type": t,
                                   "reason": "sink input not SQL-compiled"})
            continue

        # Resolve input + compiler.
        if _is_source(t) or not ins:
            body = _compile_source(step, None)
            reason = "source not SQL-expressible (API/streaming/SaaS or no path/table)"
        elif len(ins) >= 2:
            multi = _MULTI_COMPILERS.get(t)
            if multi is not None and all(i in cte_of for i in ins):
                body = multi(step, ins, cte_of)
                reason = f"'{t}' params not SQL-expressible (missing keys / mode)"
            else:
                body = None
                reason = (f"multi-input '{t}': not all inputs compiled yet"
                          if multi else f"multi-input step ({len(ins)} inputs) — no preview compiler")
        elif ins[0] not in cte_of:
            body, reason = None, "upstream step is a boundary"
        else:
            compiler = _COMPILERS.get(t)
            if compiler is None:
                body, reason = None, f"'{t}' has no SQL preview compiler yet"
            else:
                body = compiler(step, cte_of[ins[0]])
                reason = f"'{t}' params not SQL-expressible (e.g. rules-mode / missing keys)"

        if body is None:
            boundaries.append({"id": sid, "type": t, "reason": reason})
            continue

        name = _cte_name(step)
        ctes.append((name, body))
        cte_of[sid] = name
        compiled.append(sid)

    # ── assemble ────────────────────────────────────────────────────────────
    name = workflow.get("name", "pipeline")
    header = [
        f"-- F-Pulse SQL preview — pipeline: {name}",
        "-- PREVIEW ONLY: the compiled CTEs mirror what the engine runs; steps marked",
        "-- [boundary] below are NOT SQL-expressible and run inside F-Pulse.",
        "",
    ]

    if ctes:
        with_blocks = []
        for cte_name, body in ctes:
            indented = "\n".join("  " + ln for ln in body.splitlines())
            with_blocks.append(f"{cte_name} AS (\n{indented}\n)")
        tail = final_cte or ctes[-1][0]
        body_sql = "WITH " + ",\n".join(with_blocks) + f"\nSELECT *\nFROM {tail};"
    else:
        body_sql = "-- (no SQL-compilable steps in this pipeline)"

    parts = header + [body_sql]
    if boundaries:
        parts.append("")
        parts.append("-- ── Boundaries (run in F-Pulse; not in this SQL) ──────────────")
        for b in boundaries:
            parts.append(f"-- [boundary] step '{b['id']}' ({b['type']}): {b['reason']}")

    return CompiledSql(sql="\n".join(parts) + "\n",
                       compiled_steps=compiled, boundary_steps=boundaries)
