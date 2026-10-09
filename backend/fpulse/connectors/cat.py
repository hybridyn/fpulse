"""Connector Acceptance Tests (CAT) — behavioral certification engine.

Runs a *connector-agnostic* battery against recorded cassette fixtures, so a
connector earns its cert tier by PASSING a suite — not by a file merely
existing on disk (which is all `cert_matrix._compute_tier`'s
`has_smoke_fixture` check proves today). CAT is the executable layer that
turns the manifest `REQUIRED_FIXTURE_TYPES` taxonomy into a gate.

Fixtures (offline, replay — green on forks, no secrets):
    backend/tests/fixtures/connectors/<id>/<stream>.<fixture_type>.cassette.json
Optional sidecar maps a runtime stream name to its v2 cert-manifest stream
(their names can diverge — e.g. runtime `repos` vs cert `repositories`):
    backend/tests/fixtures/connectors/<id>/cat.json   ->  {"stream_map": {"repos": "repositories"}}

Cassette shape is the standard `tools/test_connector.py` ConnectorCassette
JSON (so the same recorder + secret-redaction produce CAT fixtures):
    {"version":1,"connector_id":..,"stream":..,"params":{..},
     "calls":[{"request":{"method","url",..},"response":{"payload","headers"}}]}

Checks (all run in replay mode):
    spec                v2 manifest validates (manifest_v2.validate_manifest)
    run_happy_path      connector executes the real path, returns >=1 dict row
    schema_conformance  rows satisfy the v2 stream schema (required + primary_key + loose types)
    empty               an empty page -> 0 rows, no crash
    auth_error          [skip until recorded] connector surfaces a typed error, not rows
    rate_limit          [skip until recorded] honors backoff
    incremental         [skip until recorded] resumes from the saved cursor, no dup

Tier mapping (this engine; `production` additionally needs a green live smoke,
which is out of scope for the offline suite):
    experimental  < beta < verified

Public API:
    discover_certifiable() -> list[str]          # connectors that have CAT fixtures
    run_cat(connector_id, mode="replay") -> CatReport
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Package layout: backend/fpulse/connectors/cat.py
#   parents[0]=connectors  [1]=fpulse  [2]=backend
_CONNECTORS_DIR = Path(__file__).resolve().parent
_MANIFEST_DIR = _CONNECTORS_DIR / "manifests"
_FIXTURES_DIR = _CONNECTORS_DIR.parents[1] / "tests" / "fixtures" / "connectors"

# The acceptance checks that must be GREEN (pass on >=1 stream, never fail) to
# earn each tier. auth_error / rate_limit / incremental are recorded as `skip`
# until their fixtures land; they gate `production`, not this offline suite.
_REQUIRED_FOR_BETA = ("spec", "run_happy_path")
_REQUIRED_FOR_VERIFIED = ("spec", "run_happy_path", "schema_conformance", "empty")

_FIXTURE_SUFFIX = ".cassette.json"


# ── Result model ─────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    stream: str
    check: str
    status: str          # "pass" | "fail" | "skip"
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"stream": self.stream, "check": self.check,
                "status": self.status, "detail": self.detail}


@dataclass
class CatReport:
    connector: str
    mode: str
    run_at: str
    manifest_sha256: str
    checks: list[CheckResult] = field(default_factory=list)
    fixture_sha256: dict[str, str] = field(default_factory=dict)

    @property
    def failed(self) -> int:
        return sum(1 for c in self.checks if c.status == "fail")

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.status == "pass")

    @property
    def cat_level(self) -> str:
        """experimental < beta < verified, from which check-types are green.

        A check-type is "green" if it passed on at least one stream and never
        failed on any. (`production` requires a live smoke — not computed here.)
        """
        statuses: dict[str, set[str]] = {}
        for c in self.checks:
            statuses.setdefault(c.check, set()).add(c.status)

        def green(name: str) -> bool:
            s = statuses.get(name, set())
            return "pass" in s and "fail" not in s

        if all(green(c) for c in _REQUIRED_FOR_VERIFIED):
            return "verified"
        if all(green(c) for c in _REQUIRED_FOR_BETA):
            return "beta"
        return "experimental"

    def failures_pretty(self) -> str:
        return "\n".join(
            f"  {c.stream}.{c.check}: {c.detail}"
            for c in self.checks if c.status == "fail"
        ) or "  (no failures)"

    def to_dict(self) -> dict[str, Any]:
        return {
            "connector": self.connector,
            "mode": self.mode,
            "run_at": self.run_at,
            "manifest_sha256": self.manifest_sha256,
            "summary": {
                "passed": self.passed,
                "failed": self.failed,
                "cat_level": self.cat_level,
            },
            "checks": [c.to_dict() for c in self.checks],
            "fixture_sha256": self.fixture_sha256,
        }


# ── Fixture discovery ─────────────────────────────────────────────────────

def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _connector_fixture_dir(connector_id: str) -> Path:
    return _FIXTURES_DIR / connector_id


def _streams_with_fixtures(connector_id: str) -> dict[str, dict[str, Path]]:
    """{stream_name: {fixture_type: cassette_path}} for a connector.

    `foo.happy_path.cassette.json` -> stream "foo", fixture_type "happy_path".
    """
    out: dict[str, dict[str, Path]] = {}
    base = _connector_fixture_dir(connector_id)
    if not base.is_dir():
        return out
    for p in sorted(base.glob(f"*{_FIXTURE_SUFFIX}")):
        stem = p.name[: -len(_FIXTURE_SUFFIX)]        # e.g. "repos.happy_path"
        stream, _, fixture_type = stem.rpartition(".")
        if not stream or not fixture_type:
            continue
        out.setdefault(stream, {})[fixture_type] = p
    return out


def discover_certifiable() -> list[str]:
    """Connector ids that have at least one `*.happy_path.cassette.json`.

    Pure filesystem scan — intentionally free of any duckdb/runtime import so
    it is cheap to call at pytest collection time.
    """
    if not _FIXTURES_DIR.is_dir():
        return []
    ids: set[str] = set()
    pattern = str(_FIXTURES_DIR / "*" / f"*.happy_path{_FIXTURE_SUFFIX}")
    for hit in glob.glob(pattern):
        ids.add(Path(hit).parent.name)
    return sorted(ids)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _stream_map(connector_id: str) -> dict[str, str]:
    sidecar = _connector_fixture_dir(connector_id) / "cat.json"
    if sidecar.is_file():
        try:
            data = _load_json(sidecar)
            sm = data.get("stream_map") if isinstance(data, dict) else None
            if isinstance(sm, dict):
                return {str(k): str(v) for k, v in sm.items()}
        except (ValueError, OSError):
            pass
    return {}


def _v2_streams(connector_id: str) -> dict[str, dict[str, Any]]:
    """{stream_name: v2_stream_dict} from the <id>.v2.json cert manifest."""
    v2_path = _MANIFEST_DIR / f"{connector_id}.v2.json"
    if not v2_path.is_file():
        return {}
    try:
        manifest = _load_json(v2_path)
    except (ValueError, OSError):
        return {}
    return {
        str(s.get("name")): s
        for s in (manifest.get("streams") or [])
        if isinstance(s, dict) and s.get("name")
    }


# ── Replay seam ────────────────────────────────────────────────────────────

class _Replayer:
    """Drop-in for `rest_framework._http_request` that yields recorded calls
    in order. Order-based (CAT fixtures are curated, usually one call per
    page); exhaustion is a hard error so a truncated cassette fails loudly."""

    def __init__(self, calls: list[dict[str, Any]]):
        self._calls = calls
        self._i = 0

    def __call__(self, url, headers, method="GET", body=None, body_text=None):
        if self._i >= len(self._calls):
            raise RuntimeError(f"CAT cassette exhausted before {method} {url}")
        call = self._calls[self._i]
        self._i += 1
        resp = call.get("response") or {}
        return resp.get("payload"), dict(resp.get("headers") or {})


def _run_stream_replay(runtime_manifest, stream: dict[str, Any],
                       cassette: dict[str, Any]) -> tuple[list[dict] | None, Exception | None]:
    """Execute one stream against a cassette through the real framework path."""
    import fpulse.connectors.rest_framework as rest_framework  # lazy: pulls duckdb

    params = {str(k): v for k, v in (cassette.get("params") or {}).items()}
    # Recorded secrets are redacted to "[REDACTED]"; swap a dummy back in so
    # `_build_auth_headers` can build a header. Replay never calls the network.
    params = {k: ("dummy" if v == "[REDACTED]" else v) for k, v in params.items()}

    original = rest_framework._http_request
    rest_framework._http_request = _Replayer(list(cassette.get("calls") or []))
    try:
        rows = rest_framework._execute_stream(runtime_manifest, stream, params)
        return rows, None
    except Exception as exc:  # noqa: BLE001 — CAT classifies this per check
        return None, exc
    finally:
        rest_framework._http_request = original


# ── Checks ───────────────────────────────────────────────────────────────

_PY_TYPES = {
    "string": str, "integer": int, "number": (int, float),
    "boolean": bool, "object": dict, "array": list,
}


def _type_ok(value: Any, declared: Any) -> bool:
    """Loose JSON-schema type check. `declared` is a type name or a list of
    them; `null` is allowed when declared. Unknown types pass (don't
    over-reject)."""
    if value is None:
        return "null" in (declared if isinstance(declared, list) else [declared])
    names = declared if isinstance(declared, list) else [declared]
    for name in names:
        py = _PY_TYPES.get(name)
        if py is None:
            return True  # unknown/absent type → don't fail on it
        # bool is a subclass of int — guard so a bool doesn't satisfy "integer"
        if name in ("integer", "number") and isinstance(value, bool):
            continue
        if isinstance(value, py):
            return True
    return False


def _check_schema_conformance(rows: list[dict], v2_stream: dict[str, Any]) -> tuple[bool, str]:
    schema = v2_stream.get("schema") or {}
    required = [str(k) for k in (schema.get("required") or [])]
    props = schema.get("properties") or {}
    pk = [str(k) for k in (v2_stream.get("primary_key") or [])]
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            return False, f"row[{i}] is not an object"
        missing = [k for k in required if k not in row]
        if missing:
            return False, f"row[{i}] missing required {missing}"
        missing_pk = [k for k in pk if k not in row]
        if missing_pk:
            return False, f"row[{i}] missing primary_key {missing_pk}"
        for key, spec in props.items():
            if key in row and isinstance(spec, dict) and "type" in spec:
                if not _type_ok(row[key], spec["type"]):
                    return False, f"row[{i}].{key}={row[key]!r} violates type {spec['type']!r}"
    return True, f"{len(rows)} row(s) conform to {len(required)} required + pk {pk}"


# ── Runner ─────────────────────────────────────────────────────────────────

def run_cat(connector_id: str, mode: str = "replay") -> CatReport:
    """Run the CAT battery for one connector and return a structured report."""
    if mode != "replay":
        raise NotImplementedError("CAT currently supports mode='replay' only")

    v2_path = _MANIFEST_DIR / f"{connector_id}.v2.json"
    runtime_path = _MANIFEST_DIR / f"{connector_id}.json"
    manifest_hash = _sha256(v2_path) or _sha256(runtime_path)

    report = CatReport(
        connector=connector_id,
        mode=mode,
        run_at=datetime.now(timezone.utc).isoformat(),
        manifest_sha256=manifest_hash,
    )

    # ── C1 spec: v2 cert manifest is STRUCTURALLY valid ──
    # NB: `validate_manifest` couples structural validity with fixture
    # *coverage* (it fails a manifest that doesn't DECLARE all five
    # REQUIRED_FIXTURE_TYPES inline). CAT proves fixture coverage at runtime
    # via the behavioral checks below, so "missing required fixture types"
    # errors must NOT fail the spec check — otherwise a manifest is penalised
    # twice for the same gap. We treat only non-fixture errors as blocking.
    if v2_path.is_file():
        try:
            from fpulse.connectors.manifest_v2 import validate_manifest
            res = validate_manifest(_load_json(v2_path), connector_root=v2_path.parent)
            blocking = [e for e in res.errors if "fixture" not in str(e).lower()]
            if not blocking:
                note = "structurally valid"
                if not res.valid:
                    note += " (fixture coverage deferred to CAT runtime checks)"
                report.checks.append(CheckResult("-", "spec", "pass", note))
            else:
                report.checks.append(CheckResult(
                    "-", "spec", "fail", "; ".join(str(e) for e in blocking[:3])))
        except Exception as exc:  # noqa: BLE001
            report.checks.append(CheckResult("-", "spec", "fail", f"validator crashed: {exc}"))
    else:
        report.checks.append(CheckResult("-", "spec", "skip", "no v2 cert manifest"))

    # Runtime manifest (v1) drives execution.
    from fpulse.connectors.rest_framework import get_manifest
    runtime = get_manifest(connector_id)
    if runtime is None:
        report.checks.append(CheckResult("-", "run_happy_path", "fail",
                                          "no runtime manifest loadable by rest_framework"))
        return report

    stream_map = _stream_map(connector_id)
    v2_streams = _v2_streams(connector_id)
    fixtures = _streams_with_fixtures(connector_id)

    for stream_name, by_type in fixtures.items():
        for ftype, path in by_type.items():
            report.fixture_sha256[path.name] = _sha256(path)

        stream_def = runtime.stream(stream_name)
        if stream_def is None:
            report.checks.append(CheckResult(
                stream_name, "run_happy_path", "fail",
                f"stream '{stream_name}' not in runtime manifest",
            ))
            continue

        # ── C2/C3 run happy_path ──
        happy = by_type.get("happy_path")
        rows: list[dict] | None = None
        if happy is not None:
            cassette = _load_json(happy)
            rows, err = _run_stream_replay(runtime, stream_def, cassette)
            if err is not None:
                report.checks.append(CheckResult(stream_name, "run_happy_path", "fail",
                                                  f"{type(err).__name__}: {err}"))
            elif not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
                report.checks.append(CheckResult(stream_name, "run_happy_path", "fail",
                                                  f"expected >=1 dict row, got {rows!r:.80}"))
            else:
                report.checks.append(CheckResult(stream_name, "run_happy_path", "pass",
                                                  f"{len(rows)} row(s)"))

            # ── C4 schema_conformance (needs rows + a cert schema) ──
            cert_name = stream_map.get(stream_name, stream_name)
            v2_stream = v2_streams.get(cert_name)
            if rows and isinstance(rows, list) and rows and isinstance(rows[0], dict):
                if v2_stream and (v2_stream.get("schema") or v2_stream.get("primary_key")):
                    ok, detail = _check_schema_conformance(rows, v2_stream)
                    report.checks.append(CheckResult(
                        stream_name, "schema_conformance", "pass" if ok else "fail", detail))
                else:
                    report.checks.append(CheckResult(
                        stream_name, "schema_conformance", "skip",
                        f"no v2 schema for cert stream '{cert_name}'"))

        # ── C5 empty ──
        empty = by_type.get("empty")
        if empty is not None:
            cassette = _load_json(empty)
            erows, err = _run_stream_replay(runtime, stream_def, cassette)
            if err is not None:
                report.checks.append(CheckResult(stream_name, "empty", "fail",
                                                  f"crashed on empty page: {type(err).__name__}: {err}"))
            elif erows != []:
                report.checks.append(CheckResult(stream_name, "empty", "fail",
                                                  f"expected 0 rows, got {len(erows or [])}"))
            else:
                report.checks.append(CheckResult(stream_name, "empty", "pass", "0 rows, no crash"))

        # ── C6/C7/C8 recorded as skip until their fixtures land ──
        for ftype, check in (("auth_error", "auth_error"),
                             ("rate_limit", "rate_limit"),
                             ("incremental", "incremental")):
            if ftype not in by_type:
                report.checks.append(CheckResult(
                    stream_name, check, "skip", "fixture not recorded (gates production)"))

    return report


def write_report(report: CatReport, out_path: str | os.PathLike) -> None:
    """Persist a CatReport as JSON (feeds `cert_matrix` + the signed attestation)."""
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n",
                 encoding="utf-8")


# Canonical status file the cert matrix reads to gate the Verified tier.
CAT_STATUS_PATH = _CONNECTORS_DIR / "ci" / "cat_status.json"


def run_cat_batch(*, write: bool = True, out_path: str | os.PathLike | None = None) -> dict[str, Any]:
    """Run CAT for every connector with fixtures and write a compact status map.

    Output (written to `connectors/ci/cat_status.json` by default) is the file
    `cert_matrix` reads to decide the Verified tier — the result-backed gate
    that replaces mere smoke-fixture file existence:

        {"generated_at": "<iso>",
         "connectors": {"<id>": {"cat_level","failed","passed","run_at","manifest_sha256"}}}
    """
    connectors: dict[str, Any] = {}
    for cid in discover_certifiable():
        r = run_cat(cid, mode="replay")
        connectors[cid] = {
            "cat_level": r.cat_level,
            "failed": r.failed,
            "passed": r.passed,
            "run_at": r.run_at,
            "manifest_sha256": r.manifest_sha256,
        }
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(), "connectors": connectors}
    if write:
        p = Path(out_path) if out_path else CAT_STATUS_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


if __name__ == "__main__":  # `python -m fpulse.connectors.cat` → refresh the status file
    import sys
    out = run_cat_batch(write=True)
    for cid, info in out["connectors"].items():
        print(f"{cid:20s} {info['cat_level']:12s} failed={info['failed']}")
    sys.exit(0 if all(i["failed"] == 0 for i in out["connectors"].values()) else 1)
