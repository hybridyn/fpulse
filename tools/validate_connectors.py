"""Generate an F-Pulse connector validation report.

This is intentionally local and dependency-light: it inventories the frontend
connector picker, backend connection model, implemented backend testers, and
saved connections in the local SQLite store. With --test-saved it also runs
the implemented testers against saved non-placeholder connections.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SECRET_RE = re.compile(r"(password|secret|token|key|uri|connection_string|client_secret)", re.I)
PLACEHOLDER_RE = re.compile(r"(^$|example\.com|localhost$|^$)", re.I)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def read_backend_types(root: Path) -> list[str]:
    module = ast.parse((root / "backend/fpulse/connections/models.py").read_text(encoding="utf-8"))
    for node in module.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "CONNECTION_TYPES" for t in node.targets):
            return list(ast.literal_eval(node.value))
    return []


def read_backend_testers(root: Path) -> dict[str, str]:
    sys.path.insert(0, str(root / "backend"))
    from fpulse.connections.tester import ConnectionTester

    return dict(ConnectionTester._TESTERS)


def read_frontend_types(root: Path) -> dict[str, dict[str, str]]:
    text = (root / "frontend/src/components/pages/ConnectionsPage.tsx").read_text(encoding="utf-8")
    pattern = re.compile(
        r"\{\s*type:\s*'([^']+)'\s*,\s*label:\s*'([^']+)'.*?category:\s*'([^']+)'",
        re.S,
    )
    return {typ: {"label": label, "category": category} for typ, label, category in pattern.findall(text)}


def mask_config(config: dict[str, Any]) -> dict[str, Any]:
    masked: dict[str, Any] = {}
    for key, value in (config or {}).items():
        if SECRET_RE.search(str(key)):
            masked[key] = "[set]" if value not in (None, "") else ""
        else:
            masked[key] = value
    return masked


def load_saved_connections(db_path: Path) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "select id, name, type, data, updated_at from connections order by updated_at desc"
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        data = json.loads(row["data"] or "{}")
        out.append({
            "id": row["id"],
            "name": row["name"],
            "type": row["type"],
            "config": data.get("config") or {},
            "last_test_ok": data.get("last_test_ok"),
            "last_test_at": data.get("last_test_at"),
            "updated_at": row["updated_at"],
        })
    return out


def looks_placeholder(conn: dict[str, Any]) -> bool:
    config = conn.get("config") or {}
    if not config:
        return True
    name = str(conn.get("name") or "").lower()
    if "stub" in name or "catalog_test" in name:
        return True
    hostish = " ".join(str(config.get(k, "")) for k in ("host", "base_url", "url", "endpoint", "account"))
    return bool(re.search(r"example\.com|abc12345|myworkspace|myaccount|contoso", hostish, re.I))


def test_saved_connections(saved: list[dict[str, Any]], testers: dict[str, str]) -> list[dict[str, Any]]:
    sys.path.insert(0, str(repo_root() / "backend"))
    from fpulse.connections.tester import ConnectionTester

    tester = ConnectionTester()
    results: list[dict[str, Any]] = []
    for conn in saved:
        typ = str(conn["type"]).lower()
        if typ not in testers:
            status = "skipped"
            result = {"success": None, "message": "No backend tester implemented", "error": None}
        elif looks_placeholder(conn):
            status = "skipped"
            result = {"success": None, "message": "Skipped empty/placeholder connection", "error": None}
        else:
            result = tester.test_connection(typ, conn.get("config") or {})
            status = "pass" if result.get("success") else "fail"
        results.append({
            "id": conn["id"],
            "name": conn["name"],
            "type": typ,
            "status": status,
            "message": result.get("message"),
            "error": result.get("error"),
            "suggestion": result.get("suggestion"),
            "last_test_ok": conn.get("last_test_ok"),
            "last_test_at": conn.get("last_test_at"),
            "config": mask_config(conn.get("config") or {}),
        })
    return results


def make_report(
    *,
    backend_types: list[str],
    frontend_types: dict[str, dict[str, str]],
    testers: dict[str, str],
    saved_results: list[dict[str, Any]],
) -> str:
    now = datetime.now(timezone.utc).isoformat()
    lines = [
        "# F-Pulse Connector Validation Report",
        "",
        f"Generated: `{now}`",
        "",
        "## Summary",
        "",
        f"- Backend registered connector types: `{len(backend_types)}`",
        f"- Frontend create-connection types: `{len(frontend_types)}`",
        f"- Backend implemented testers: `{len(testers)}`",
        f"- Saved connections inspected: `{len(saved_results)}`",
        "",
        "## Saved Connection Tests",
        "",
        "| Status | Name | Type | Message | Last UI Test |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in saved_results:
        status = item["status"]
        last = item.get("last_test_at") or "-"
        msg = (item.get("message") or item.get("error") or "").replace("\n", " ")
        lines.append(f"| {status} | `{item['name']}` | `{item['type']}` | {msg[:220]} | {last} |")

    lines += [
        "",
        "## Frontend Connector Coverage",
        "",
        "| Category | Connector | Type | Backend Tester |",
        "| --- | --- | --- | --- |",
    ]
    for typ, meta in sorted(frontend_types.items(), key=lambda kv: (kv[1]["category"], kv[1]["label"])):
        lines.append(
            f"| {meta['category']} | {meta['label']} | `{typ}` | {'yes' if typ in testers else 'no'} |"
        )

    model_only = sorted(set(backend_types) - set(frontend_types))
    ui_no_tester = sorted(set(frontend_types) - set(testers))
    lines += [
        "",
        "## Gaps",
        "",
        "Frontend types without backend tester:",
        "",
        ", ".join(f"`{x}`" for x in ui_no_tester) or "None",
        "",
        "Backend model types not exposed in the create-connection picker:",
        "",
        ", ".join(f"`{x}`" for x in model_only) or "None",
        "",
        "## Saved Connection Details",
        "",
        "```json",
        json.dumps(saved_results, indent=2, default=str),
        "```",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/samples/fpulse.db")
    parser.add_argument("--out", default="docs/connector-validation-report.md")
    parser.add_argument("--test-saved", action="store_true")
    args = parser.parse_args()

    root = repo_root()
    backend_types = read_backend_types(root)
    frontend_types = read_frontend_types(root)
    testers = read_backend_testers(root)
    saved = load_saved_connections(root / args.db)
    saved_results = test_saved_connections(saved, testers) if args.test_saved else [
        {
            "id": c["id"],
            "name": c["name"],
            "type": c["type"],
            "status": "not-run",
            "message": "Use --test-saved to run live tests",
            "last_test_ok": c.get("last_test_ok"),
            "last_test_at": c.get("last_test_at"),
            "config": mask_config(c.get("config") or {}),
        }
        for c in saved
    ]

    report = make_report(
        backend_types=backend_types,
        frontend_types=frontend_types,
        testers=testers,
        saved_results=saved_results,
    )
    out_path = root / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    print(str(out_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
