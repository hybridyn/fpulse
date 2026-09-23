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
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

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
import fpulse.connectors.rest_framework as rest_framework  # noqa: E402


_REDACTED = "[REDACTED]"

# Header names (matched as lowercased substrings) whose value is a credential.
_SECRET_HEADER_MARKERS = (
    "authorization", "auth", "api-key", "apikey", "api_key", "token",
    "secret", "password", "cookie", "credential", "session",
    "x-amz-security-token", "private-token",
)

# Key-name substrings (lowercased) marking a secret in params / query / request body.
_SECRET_KEY_MARKERS = (
    "token", "key", "secret", "password", "passwd", "auth", "credential",
    "cookie", "session", "signature", "sig", "bearer", "refresh",
    "private", "access",
)

# Narrower set for RESPONSE bodies: catch returned credentials (OAuth tokens,
# secrets) WITHOUT nuking pagination cursors (next_token / page_token) that
# replay needs — so we match specific credential field names, not bare "token".
_RESPONSE_SECRET_KEYS = (
    "secret", "password", "passwd", "client_secret", "refresh_token",
    "access_token", "id_token", "private_key", "secret_key", "api_key",
    "apikey", "credential", "authorization", "session_token",
)

# Known secret shapes for the write-time self-scan — a fail-closed net if the
# structured redaction above ever misses a spot (e.g. a vendor returns a token
# under an unexpected field name).
_SECRET_PATTERNS = tuple(
    re.compile(pattern) for pattern in (
        r"ghp_[A-Za-z0-9]{20,}", r"gho_[A-Za-z0-9]{20,}",
        r"github_pat_[A-Za-z0-9_]{20,}", r"glpat-[A-Za-z0-9_\-]{16,}",
        r"xox[baprs]-[A-Za-z0-9-]{10,}", r"sk-[A-Za-z0-9]{20,}",
        r"sk_live_[A-Za-z0-9]{16,}", r"AKIA[0-9A-Z]{16}",
        r"AIza[0-9A-Za-z_\-]{35}", r"ya29\.[0-9A-Za-z_\-]{10,}",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}",
        r"fpk_[A-Za-z0-9]{16,}", r"psk_[A-Za-z0-9]{16,}",
    )
)


def _is_secret_key(key: str, markers: tuple[str, ...] = _SECRET_KEY_MARKERS) -> bool:
    key_l = str(key).lower()
    return any(marker in key_l for marker in markers)


def _redact_headers(headers: dict[str, str] | None) -> dict[str, str]:
    return {
        key: (_REDACTED if any(m in str(key).lower() for m in _SECRET_HEADER_MARKERS) else value)
        for key, value in (headers or {}).items()
    }


def _redact_mapping(value: object, markers: tuple[str, ...]) -> object:
    """Recursively redact dict values whose key matches a secret marker."""
    if isinstance(value, dict):
        return {
            str(k): (_REDACTED if _is_secret_key(str(k), markers) else _redact_mapping(v, markers))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_mapping(item, markers) for item in value]
    return value


def _redact_url(url: str) -> str:
    """Redact userinfo passwords and secret query-string params in a URL.

    Applied identically at record time (the stored form) and at replay match
    time, so a redacted request still matches — replay never holds the real
    secret. Non-secret query (page, cursor, ...) is preserved for match fidelity.
    """
    if not isinstance(url, str) or "://" not in url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    netloc = parts.netloc
    if "@" in netloc:
        userinfo, _, host = netloc.rpartition("@")
        user = userinfo.split(":", 1)[0]
        netloc = f"{user}:{_REDACTED}@{host}" if ":" in userinfo else f"{_REDACTED}@{host}"
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    new_query = urlencode([(k, _REDACTED if _is_secret_key(k) else v) for k, v in pairs])
    return urlunsplit((parts.scheme, netloc, parts.path, new_query, parts.fragment))


def _redact_body_text(body_text: str | None) -> str | None:
    """Redact a raw request body string (JSON, form-encoded, or freeform)."""
    if not isinstance(body_text, str) or not body_text:
        return body_text
    stripped = body_text.lstrip()
    if stripped[:1] in "{[":
        try:
            return json.dumps(_redact_mapping(json.loads(body_text), _SECRET_KEY_MARKERS))
        except (ValueError, TypeError):
            pass
    if "=" in body_text and "\n" not in body_text:
        try:
            pairs = parse_qsl(body_text, keep_blank_values=True)
            if pairs:
                return urlencode([(k, _REDACTED if _is_secret_key(k) else v) for k, v in pairs])
        except ValueError:
            pass
    return re.sub(
        r"(?i)(password|passwd|secret|token|api[_-]?key|client_secret|refresh_token|access_token)"
        r"\s*[:=]\s*([^\s,;&\"']+)",
        lambda m: m.group(1) + "=" + _REDACTED,
        body_text,
    )


def _scan_secrets(text: str, secret_values: tuple[str, ...] = ()) -> list[str]:
    """Return descriptions of any secret still present in ``text``.

    Two nets: (1) literal known secret values — the real params used to record;
    (2) known secret-shaped patterns — tokens a vendor may return in a response.
    An empty list means the redaction held.
    """
    hits: list[str] = []
    for value in secret_values:
        if isinstance(value, str) and len(value) >= 6 and value in text:
            hits.append(f"literal secret value {value[:3]}...")
    for pattern in _SECRET_PATTERNS:
        match = pattern.search(text)
        if match:
            hits.append(f"secret pattern {match.group(0)[:6]}...")
    return hits


def default_cassette_path(connector_id: str, stream_name: str | None) -> Path:
    stream_part = stream_name or "default"
    return (
        REPO_ROOT
        / "backend"
        / "tests"
        / "fixtures"
        / "connectors"
        / connector_id
        / f"{stream_part}.cassette.json"
    )


class ConnectorCassette:
    def __init__(self, path: Path, mode: str):
        self.path = path
        self.mode = mode
        self.calls: list[dict] = []
        self._index = 0
        if mode == "replay":
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.calls = list(payload.get("calls") or [])

    def record_call(
        self,
        *,
        url: str,
        headers: dict[str, str],
        method: str,
        body: object,
        body_text: str | None,
        payload: object,
        response_headers: dict[str, str],
    ) -> None:
        self.calls.append(
            {
                "request": {
                    "method": method,
                    "url": _redact_url(url),
                    "headers": _redact_headers(headers),
                    "body": _redact_mapping(body, _SECRET_KEY_MARKERS),
                    "body_text": _redact_body_text(body_text),
                },
                "response": {
                    "payload": _redact_mapping(payload, _RESPONSE_SECRET_KEYS),
                    "headers": _redact_headers(response_headers),
                },
            }
        )

    def next_response(self, *, url: str, method: str) -> tuple[object, dict[str, str]]:
        if self._index >= len(self.calls):
            raise RuntimeError(f"Replay cassette exhausted before {method} {url}")
        call = self.calls[self._index]
        self._index += 1
        request = call.get("request") or {}
        expected_method = str(request.get("method") or "GET").upper()
        expected_url = str(request.get("url") or "")
        # The stored URL is already redacted; redact the incoming one the same
        # way so a request authenticated via query string still matches on
        # replay (when the real secret is absent).
        redacted_url = _redact_url(url)
        if expected_method != method.upper() or expected_url != redacted_url:
            raise RuntimeError(
                "Replay cassette request mismatch: "
                f"expected {expected_method} {expected_url}, got {method.upper()} {redacted_url}"
            )
        response = call.get("response") or {}
        return response.get("payload"), dict(response.get("headers") or {})

    def write(self, *, connector_id: str, stream_name: str, params: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        safe_params = {
            key: (_REDACTED if _is_secret_key(key) else value)
            for key, value in params.items()
        }
        text = json.dumps(
            {
                "version": 1,
                "connector_id": connector_id,
                "stream": stream_name,
                "params": safe_params,
                "calls": self.calls,
            },
            indent=2,
            sort_keys=True,
        ) + "\n"
        # Fail-closed: never write a cassette that still contains a secret. This
        # catches any gap in the structured redaction above BEFORE the file can
        # be committed to a public repo.
        secret_values = tuple(str(v) for k, v in params.items() if _is_secret_key(k) and v)
        leaked = _scan_secrets(text, secret_values)
        if leaked:
            raise RuntimeError(
                f"Refusing to write cassette {self.path}: possible secret(s) survived "
                f"redaction: {leaked}. This is a redaction bug — fix the _redact_* helpers "
                f"in tools/test_connector.py before recording."
            )
        self.path.write_text(text, encoding="utf-8")


def install_cassette(cassette: ConnectorCassette):
    original = rest_framework._http_request

    def _recording_request(url, headers, method="GET", body=None, body_text=None):
        payload, response_headers = original(
            url, headers, method=method, body=body, body_text=body_text
        )
        cassette.record_call(
            url=url,
            headers=headers,
            method=method,
            body=body,
            body_text=body_text,
            payload=payload,
            response_headers=response_headers,
        )
        return payload, response_headers

    def _replay_request(url, headers, method="GET", body=None, body_text=None):
        return cassette.next_response(url=url, method=method)

    rest_framework._http_request = (
        _recording_request if cassette.mode == "record" else _replay_request
    )
    return original


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


def cmd_scan(target: str | None) -> int:
    """Scan committed cassette(s) for leaked secrets — CI gate.

    Exit 1 if any cassette still contains a known secret shape, so a leaked
    fixture can never merge. With no PATH, scans every committed cassette.
    """
    if target:
        paths = [Path(target)]
    else:
        root = REPO_ROOT / "backend" / "tests" / "fixtures" / "connectors"
        paths = sorted(root.rglob("*.cassette.json")) if root.is_dir() else []
    if not paths:
        print("no cassettes to scan")
        return 0
    bad = False
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"FAIL {path}: {exc}")
            bad = True
            continue
        hits = _scan_secrets(text)
        if hits:
            bad = True
            print(f"LEAK {path}: {hits}")
        else:
            print(f"OK   {path}")
    return 1 if bad else 0


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


def cmd_replay_batch(status_out: str) -> int:
    """Replay every committed cassette and write a machine-readable status.

    Each cassette carries its connector_id + stream + (redacted) params, so
    replay rebuilds the request identically and matches on the redacted form.
    Writes ``{"results": [{"id","stream","status","cassette"}]}`` for the cert
    matrix (`verified_recorded` requires status == "pass"), and exits non-zero
    if any cassette fails to replay — the CI gate that makes the tier mean
    "replays green", not "file exists".
    """
    root = REPO_ROOT / "backend" / "tests" / "fixtures" / "connectors"
    cassettes = sorted(root.rglob("*.cassette.json")) if root.is_dir() else []
    Path(status_out).parent.mkdir(parents=True, exist_ok=True)
    if not cassettes:
        Path(status_out).write_text(json.dumps({"results": []}, indent=2), encoding="utf-8")
        print("no committed cassettes to replay")
        return 0

    results: list[dict] = []
    any_fail = False
    for path in cassettes:
        stream_label = path.name[: -len(".cassette.json")]
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            results.append({"id": path.parent.name, "stream": stream_label,
                            "status": "fail", "cassette": str(path),
                            "reason": f"unreadable: {exc}"})
            any_fail = True
            continue
        cid = str(payload.get("connector_id") or path.parent.name)
        stream = payload.get("stream")
        params = {str(k): v for k, v in (payload.get("params") or {}).items()}
        cassette = ConnectorCassette(path, "replay")
        original_http = install_cassette(cassette)
        try:
            rc = cmd_run(cid, stream, params, max_rows=3)
        except Exception as exc:  # noqa: BLE001
            rc = 1
            print(f"FAIL {cid}.{stream}: {type(exc).__name__}: {exc}")
        finally:
            rest_framework._http_request = original_http
        status = "pass" if rc == 0 else "fail"
        any_fail = any_fail or rc != 0
        results.append({"id": cid, "stream": stream, "status": status, "cassette": str(path)})

    Path(status_out).write_text(json.dumps({"results": results}, indent=2), encoding="utf-8")
    passed = sum(1 for r in results if r["status"] == "pass")
    print(f"\nReplay-batch: {len(results)} cassette(s), {passed} pass, {len(results) - passed} fail")
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
    ap.add_argument("--record", nargs="?", const="",
                    help="Record HTTP responses to a cassette JSON file. "
                         "Omit PATH to use backend/tests/fixtures/connectors/<id>/<stream>.cassette.json.")
    ap.add_argument("--replay", nargs="?", const="",
                    help="Replay HTTP responses from a cassette JSON file. "
                         "Omit PATH to use backend/tests/fixtures/connectors/<id>/<stream>.cassette.json.")
    ap.add_argument("--scan", nargs="?", const="",
                    help="CI gate: scan committed cassette(s) for leaked secrets "
                         "and exit non-zero if any are found. Omit PATH to scan "
                         "all backend/tests/fixtures/connectors/**/*.cassette.json.")
    ap.add_argument("--replay-batch", action="store_true",
                    help="CI gate: replay every committed cassette and write a "
                         "status file the cert matrix reads (result-backed "
                         "Verified-Recorded). Exit non-zero if any replay fails.")
    ap.add_argument("--replay-status-out",
                    default=str(REPO_ROOT / "backend" / "fpulse" / "connectors" / "ci" / "last_replay_status.json"),
                    help="Where --replay-batch writes its JSON status.")
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
    if args.scan is not None:
        return cmd_scan(args.scan or None)
    if args.replay_batch:
        return cmd_replay_batch(args.replay_status_out)
    if args.live_batch:
        return cmd_live_batch(args.allowlist, args.status_out)
    if not args.connector_id:
        ap.print_help()
        return 2
    if args.record is not None and args.replay is not None:
        print("ERROR: choose only one of --record or --replay.")
        return 2

    params = collect_params(args.param)
    if args.dry_run:
        return cmd_dry_run(args.connector_id, args.stream, params)

    m = get_manifest(args.connector_id)
    stream_name = args.stream or (m.streams[0]["name"] if m and m.streams else None)
    cassette: ConnectorCassette | None = None
    original_http = None
    if args.record is not None or args.replay is not None:
        requested = args.record if args.record is not None else args.replay
        cassette_path = Path(requested) if requested else default_cassette_path(args.connector_id, stream_name)
        mode = "record" if args.record is not None else "replay"
        if mode == "replay" and not cassette_path.is_file():
            print(f"ERROR: replay cassette not found: {cassette_path}")
            return 2
        cassette = ConnectorCassette(cassette_path, mode)
        original_http = install_cassette(cassette)
        print(f"{mode.upper()}: {cassette_path}")

    try:
        rc = cmd_run(args.connector_id, args.stream, params, args.max_rows)
    finally:
        if original_http is not None:
            rest_framework._http_request = original_http
    if cassette is not None and cassette.mode == "record" and rc == 0 and stream_name:
        cassette.write(connector_id=args.connector_id, stream_name=stream_name, params=params)
        print(f"Recorded {len(cassette.calls)} HTTP call(s) to {cassette.path}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
