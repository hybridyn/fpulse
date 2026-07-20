"""Smoke-test a single REST connector manifest end-to-end.

Picks a manifest by ID, optionally a stream (defaults to the first),
collects auth + connection params from a CLI mix of `--param k=v` and
environment variables (anything prefixed `FPULSE_TEST_` maps to a lower-
cased param name), and executes one real HTTP round-trip through the
production framework code path.

This is the lightweight verification layer recommended by the 2026-06-01
REST-framework audit — without it, a regression in `_http_request` or
pagination normalisation stays invisible until a user hits it.

Usage
-----
List every manifest the framework can load:

    python tools/test_connector.py --list

Dry-run (prints the resolved method / URL / headers — no network):

    python tools/test_connector.py github --dry-run \
        --param github_token=ghp_xxx

Live smoke test — auth via env, scoped stream:

    $env:FPULSE_TEST_GITHUB_TOKEN = "ghp_..."
    python tools/test_connector.py github --stream user

    $env:FPULSE_TEST_API_KEY = "sk-..."
    python tools/test_connector.py openai --stream models

The goal is "did the HTTP layer send what the manifest declared":
correct method, correct headers, body delivered, response parsed. It
intentionally does NOT validate vendor-side semantics (row counts,
schema drift, rate-limit behaviour) — that's the cert-matrix job.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Make the in-tree framework importable when this script is run from
# anywhere (repo root, tools/, or a CI working dir).
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from fpulse.connectors.rest_framework import (  # noqa: E402
    _build_url,
    _deep_interpolate,
    _execute_stream,
    _interpolate,
    _interpolate_dict,
    _normalize_pagination,
    _build_auth_headers,
    get_manifest,
    list_manifests,
)


def collect_params(cli_params: list[str]) -> dict[str, str]:
    """CLI `--param k=v` + env `FPULSE_TEST_<KEY>=v` → params dict.

    Env wins over CLI when both are set (env is the safer place to keep
    secrets — they don't end up in shell history or the process table).
    """
    params: dict[str, str] = {}
    for kv in cli_params or []:
        key, _, val = kv.partition("=")
        if key:
            params[key.strip()] = val
    for env_key, env_val in os.environ.items():
        if env_key.startswith("FPULSE_TEST_"):
            params[env_key[len("FPULSE_TEST_"):].lower()] = env_val
    return params


def cmd_list() -> int:
    manifests = list_manifests()
    print(f"{len(manifests)} manifest(s) loaded by the framework:\n")
    for m in sorted(manifests, key=lambda x: x.id):
        atype = (m.auth or {}).get("type", "?")
        print(f"  {m.id:25s}  auth={atype:8s}  streams={len(m.streams)}  ({m.name})")
    return 0


def cmd_dry_run(connector_id: str, stream_name: str | None, params: dict) -> int:
    m = get_manifest(connector_id)
    if not m:
        print(f"ERROR: Unknown connector '{connector_id}'. Try --list.")
        return 2
    stream_name = stream_name or (m.streams[0]["name"] if m.streams else None)
    if not stream_name:
        print(f"ERROR: Connector '{connector_id}' has no streams.")
        return 2
    stream = m.stream(stream_name)
    if not stream:
        print(f"ERROR: Stream '{stream_name}' not in '{connector_id}'.")
        return 2

    # Mirror what _execute_stream does, just don't fire the request.
    base = _interpolate(m.base_url, params)
    path = _interpolate(stream.get("path", ""), params)
    query = _interpolate_dict(m.default_query or {}, params)
    query.update(_interpolate_dict(stream.get("query", {}), params))
    query = {k: v for k, v in query.items() if v not in (None, "")}

    headers = {"Accept": "application/json", **(m.headers or {})}
    headers.update(_interpolate_dict(m.default_headers or {}, params))
    headers.update(_build_auth_headers(m, params))
    headers.update(_interpolate_dict(stream.get("headers", {}), params))

    method = (stream.get("method") or "GET").upper()
    body = stream.get("body")
    if body is not None:
        body = _deep_interpolate(body, params)
    body_text = stream.get("body_text")
    if isinstance(body_text, str):
        body_text = _interpolate(body_text, params)

    pagination = _normalize_pagination(stream.get("pagination"))

    print(f"Connector:  {m.id}  ({m.name})")
    print(f"Auth:       {(m.auth or {}).get('type','none')}")
    print(f"Stream:     {stream_name}")
    print(f"Method:     {method}")
    print(f"URL:        {_build_url(base, path, query)}")
    print(f"Pagination: type={pagination.get('type')}  "
          f"max_pages={pagination.get('max_pages')}")
    # Mask Authorization header value — never print full tokens.
    safe_headers = {
        k: ("***" if k.lower() == "authorization" else v)
        for k, v in headers.items()
    }
    print(f"Headers:    {json.dumps(safe_headers, indent=2)}")
    if body is not None and body != {}:
        print(f"Body:       {json.dumps(body, indent=2)[:400]}")
    if body_text:
        print(f"Body text:  {body_text[:400]}")
    print("\nDRY RUN — no network call.")
    return 0


def cmd_run(connector_id: str, stream_name: str | None, params: dict,
            max_rows: int) -> int:
    m = get_manifest(connector_id)
    if not m:
        print(f"ERROR: Unknown connector '{connector_id}'. Try --list.")
        return 2
    stream_name = stream_name or (m.streams[0]["name"] if m.streams else None)
    stream = m.stream(stream_name)
    if not stream:
        print(f"ERROR: Stream '{stream_name}' not in '{connector_id}'.")
        return 2

    method = (stream.get("method") or "GET").upper()
    print(f"-> {m.id}.{stream_name} [{method}]  "
          f"(auth={(m.auth or {}).get('type','none')})")

    try:
        rows = _execute_stream(m, stream, params)
    except Exception as exc:
        # Anything bubbling out is a real failure — surface and exit non-zero.
        print(f"FAIL: {type(exc).__name__}: {exc}")
        return 1

    print(f"OK: {len(rows)} row(s) returned")
    if rows:
        sample = rows[: max(1, max_rows)]
        for i, r in enumerate(sample):
            keys = list(r.keys())[:8] if isinstance(r, dict) else ["(non-dict)"]
            print(f"  row[{i}] keys: {keys}")
    return 0


def cmd_live_batch(allowlist_path: str, status_out: str) -> int:
    """Run live-smoke against every connector in the allow-list.

    For each entry: skip cleanly if any required secret is unset
    (forks; PRs from contributors without secret access). Run the
    connector's first stream with `--live` semantics. Write a
    JSON status file so the cert matrix can auto-demote on red.

    Exit codes:
      0  every attempted connector passed (or was skipped clean)
      1  one or more attempted connectors returned a runtime error
      2  the allow-list file itself is unreadable

    The status file written to `status_out`:
      {
        "ran_at": "<iso>",
        "results": [
          {"id": "github", "status": "pass" | "fail" | "skipped",
           "reason": "...", "duration_ms": <int>}
        ]
      }
    """
    from pathlib import Path
    try:
        import yaml  # pyyaml is already a core dep
    except ImportError:
        print("FAIL: pyyaml required for --live-batch (pip install pyyaml)")
        return 2

    p = Path(allowlist_path)
    if not p.is_file():
        print(f"WARN: allow-list '{allowlist_path}' not found — nothing to do")
        Path(status_out).parent.mkdir(parents=True, exist_ok=True)
        Path(status_out).write_text(
            json.dumps({"ran_at": "", "results": []}, indent=2),
            encoding="utf-8",
        )
        return 0

    try:
        spec = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        print(f"FAIL: allow-list parse error: {exc}")
        return 2

    entries = spec.get("connectors") or []
    if not entries:
        print("INFO: allow-list is empty — no connectors gated for live CI yet")
        Path(status_out).parent.mkdir(parents=True, exist_ok=True)
        Path(status_out).write_text(
            json.dumps({"ran_at": "", "results": []}, indent=2),
            encoding="utf-8",
        )
        return 0

    # Wall-clock per-connector via process_time isn't network-aware;
    # use a coarse monotonic clock for "did this take ages" signal.
    # NOTE: time.time() / monotonic() are not blocked here — only
    # rest_framework's internal use is restricted. Local script is fine.
    import time

    results: list[dict] = []
    any_fail = False
    for entry in entries:
        cid = entry.get("id")
        required_secrets = entry.get("secrets") or []
        if not cid:
            continue

        missing = [s for s in required_secrets if not os.environ.get(s)]
        if missing:
            print(f"SKIP {cid}: missing secrets {missing}")
            results.append({"id": cid, "status": "skipped",
                            "reason": f"missing secrets {missing}",
                            "duration_ms": 0})
            continue

        params = collect_params([])
        start = time.monotonic()
        rc = cmd_run(cid, None, params, max_rows=3)
        dur_ms = int((time.monotonic() - start) * 1000)
        status = "pass" if rc == 0 else "fail"
        if rc != 0:
            any_fail = True
        results.append({"id": cid, "status": status, "duration_ms": dur_ms})

    Path(status_out).parent.mkdir(parents=True, exist_ok=True)
    Path(status_out).write_text(
        json.dumps({"results": results}, indent=2),
        encoding="utf-8",
    )

    print(f"\nLive-batch summary: {len(results)} entries, "
          f"{sum(1 for r in results if r['status']=='pass')} pass, "
          f"{sum(1 for r in results if r['status']=='fail')} fail, "
          f"{sum(1 for r in results if r['status']=='skipped')} skipped")
    return 1 if any_fail else 0


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="test_connector",
        description="Smoke-test a single REST connector manifest end-to-end.",
    )
    ap.add_argument("connector_id", nargs="?",
                    help="Manifest ID (e.g. github, openai). Omit with --list.")
    ap.add_argument("--list", action="store_true",
                    help="List every manifest the framework can load.")
    ap.add_argument("--stream", help="Stream name (default: first stream).")
    ap.add_argument("--param", action="append", default=[],
                    help="Param as key=value. Repeat for multiple. Env vars "
                         "named FPULSE_TEST_<KEY> are also accepted.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the resolved request plan, don't call.")
    ap.add_argument("--max-rows", type=int, default=3,
                    help="How many sample row signatures to print (default 3).")
    # --live-batch mode for CI: runs every allow-listed connector and
    # writes a status file the cert matrix can read for tier demotion.
    ap.add_argument("--live-batch", action="store_true",
                    help="CI mode: run live-smoke for every connector in "
                         "--allowlist; write JSON status to --status-out.")
    ap.add_argument("--allowlist",
                    default="backend/fpulse/connectors/ci/live_smoke.yml",
                    help="Path to live-smoke allow-list YAML (for --live-batch).")
    ap.add_argument("--status-out",
                    default="backend/fpulse/connectors/ci/last_smoke_status.json",
                    help="Where to write the live-batch status JSON.")
    args = ap.parse_args()

    if args.list:
        return cmd_list()
    if args.live_batch:
        return cmd_live_batch(args.allowlist, args.status_out)
    if not args.connector_id:
        ap.print_help()
        return 2

    params = collect_params(args.param)
    if args.dry_run:
        return cmd_dry_run(args.connector_id, args.stream, params)
    return cmd_run(args.connector_id, args.stream, params, args.max_rows)


if __name__ == "__main__":
    sys.exit(main())
