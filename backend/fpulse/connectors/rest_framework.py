"""
Declarative REST connector framework.

A single node class (RestApiSourceNode) reads a JSON manifest describing
auth, base URL, streams (endpoints), and pagination — then executes the
selected stream and returns rows as a DuckDB relation.

This unlocks ~300 SaaS connectors with one Python class. Each new SaaS
integration is a ~40-line JSON file in `manifests/`.

Manifest schema (top-level):
{
  "id": "salesforce",                    // unique connector id, used as palette item
  "name": "Salesforce",                  // human label
  "description": "Salesforce CRM via REST API",
  "category": "saas",                    // palette grouping
  "base_url": "https://{instance}.salesforce.com",
  "auth": {
    "type": "oauth2" | "bearer" | "api_key" | "basic" | "none",
    // type=oauth2: token_url, client_id_param, client_secret_param, refresh_token_param
    // type=bearer: token_param
    // type=api_key: header_name (or query_param), key_param
    // type=basic: username_param, password_param
    "header_name": "Authorization",
    "header_template": "Bearer {token}"
  },
  "params": [
    {"name": "instance", "label": "Instance", "required": true},
    {"name": "access_token", "label": "Access Token", "required": true, "secret": true}
  ],
  "streams": [
    {
      "name": "accounts",
      "label": "Accounts",
      "path": "/services/data/v59.0/query",
      "method": "GET",
      "query": {"q": "SELECT Id, Name FROM Account LIMIT 200"},
      "data_path": "records",            // dot-path into JSON to find row array
      "pagination": {
        "type": "cursor",                // page_number | offset_limit | cursor | link_header | none
        "next_url_path": "nextRecordsUrl",
        "max_pages": 100
      }
    }
  ]
}
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

# Stage 2.5b: duckdb only used for type annotations on _rows_to_relation
# and execute() returns.
if TYPE_CHECKING:
    import duckdb

from fpulse.ir.schema import StepType
from fpulse.nodes.base import BaseNode, ExecutionContext
from fpulse.nodes.registry import register
from fpulse.security.ssrf import API_SOURCE_ALLOW_PRIVATE_ENV, check_url


class _SsrfGuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate every redirect target against the SSRF policy.

    urllib follows redirects transparently, so checking only the initial
    URL would let a public host 302 us into the private network.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        check_url(newurl, allow_private_env=API_SOURCE_ALLOW_PRIVATE_ENV)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_GUARDED_OPENER = urllib.request.build_opener(_SsrfGuardedRedirectHandler)


_MANIFEST_DIR = os.path.join(os.path.dirname(__file__), "manifests")

# 2026-06-18 — writable, user-added connector store. Lives under the data dir
# (NOT the packaged read-only manifests/), so an admin/lead can add a connector
# in-app and have it persist + load without filesystem access or a restart.
# Resolved the same way Database resolves its path so the two agree.
_USER_DATA_DIR = os.environ.get("FPULSE_DATA_DIR") or os.path.join(os.getcwd(), "data")
_USER_MANIFEST_DIR = os.path.join(_USER_DATA_DIR, "connectors", "manifests")

_MANIFEST_CACHE: dict[str, "RestConnectorManifest"] = {}
# Ids that came from the user store (so the API/UI can mark them deletable and
# the loader never lets a user manifest shadow a shipped connector).
_USER_MANIFEST_IDS: set[str] = set()

# Connector ids are used as filenames + palette keys — keep them safe.
_SAFE_ID_RE = re.compile(r"[^a-z0-9_]+")


# ─────────────────────────── Manifest model ───────────────────────────

@dataclass
class RestConnectorManifest:
    id: str
    name: str
    description: str = ""
    category: str = "saas"
    # Confidence tier shown in the picker (certified | beta | community |
    # generated). Auto-set to "certified" at load when a <id>.v2.json cert
    # manifest ships alongside; an explicit `tier` in the JSON wins.
    tier: str = "beta"
    base_url: str = ""
    auth: dict[str, Any] = field(default_factory=dict)
    params: list[dict[str, Any]] = field(default_factory=list)
    streams: list[dict[str, Any]] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    # 2026-05-23 (T1): manifest-level defaults merged into every stream's
    # query and headers before the per-stream values are applied. Lets
    # an OData manifest set `$format=json` + `sap-client={client}` once
    # instead of repeating it on every stream. Stream-level keys win
    # on conflict — defaults are a fallback, not an override.
    default_query: dict[str, Any] = field(default_factory=dict)
    default_headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RestConnectorManifest":
        return cls(
            id=data["id"],
            name=data.get("name", data["id"]),
            description=data.get("description", ""),
            category=data.get("category", "saas"),
            tier=data.get("tier", "beta"),
            base_url=data.get("base_url", ""),
            auth=data.get("auth", {}),
            params=data.get("params", []),
            streams=data.get("streams", []),
            headers=data.get("headers", {}),
            default_query=data.get("default_query", {}),
            default_headers=data.get("default_headers", {}),
        )

    def stream(self, name: str) -> dict[str, Any] | None:
        for s in self.streams:
            if s.get("name") == name:
                return s
        return None


def load_manifests(force: bool = False) -> dict[str, RestConnectorManifest]:
    """Scan the manifests/ directory and cache parsed manifests.

    Skips `*.v2.json` files — those follow the F0.1 spec consumed by the
    cert matrix endpoint (`fpulse.api.cert_matrix`), not this runtime
    loader. The runtime SaaS Connector node uses the v1 manifest format;
    a connector with both `<name>.json` (v1) and `<name>.v2.json` (v2 cert
    sample) ships only the v1 at runtime today. The v1→v2 runtime
    consumer is the post-1.0 connector roadmap.
    """
    if _MANIFEST_CACHE and not force:
        return _MANIFEST_CACHE
    _MANIFEST_CACHE.clear()
    _USER_MANIFEST_IDS.clear()

    # 1) Packaged (shipped) manifests. A `<id>.v2.json` cert sibling marks the
    #    connector CERTIFIED unless the v1 JSON pins an explicit tier.
    if os.path.isdir(_MANIFEST_DIR):
        certified_ids = {
            fn[: -len(".v2.json")]
            for fn in os.listdir(_MANIFEST_DIR)
            if fn.endswith(".v2.json")
        }
        for fname in sorted(os.listdir(_MANIFEST_DIR)):
            _load_manifest_file(_MANIFEST_DIR, fname, certified_ids, user_added=False)

    # 2) User-added manifests (writable store). Always Beta; never allowed to
    #    shadow a shipped connector id.
    if os.path.isdir(_USER_MANIFEST_DIR):
        for fname in sorted(os.listdir(_USER_MANIFEST_DIR)):
            _load_manifest_file(_USER_MANIFEST_DIR, fname, set(), user_added=True)

    return _MANIFEST_CACHE


def _load_manifest_file(
    directory: str, fname: str, certified_ids: set[str], *, user_added: bool
) -> None:
    """Parse one v1 manifest file into the cache. v2 cert files are skipped
    (different schema, read by the cert-matrix endpoint). Never raises."""
    if not fname.endswith(".json") or fname.endswith(".v2.json"):
        return
    fpath = os.path.join(directory, fname)
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("hidden"):
            return
        manifest = RestConnectorManifest.from_dict(data)
        if user_added:
            # A user manifest must never hijack a built-in connector id.
            if manifest.id in _MANIFEST_CACHE:
                return
            manifest.tier = "beta"  # user-added is always Beta — honest signal
            _USER_MANIFEST_IDS.add(manifest.id)
        elif "tier" not in data and manifest.id in certified_ids:
            manifest.tier = "certified"
        _MANIFEST_CACHE[manifest.id] = manifest
    except Exception as e:  # noqa: BLE001 — one bad file never breaks the rest
        print(f"[connectors] Failed to load {fname}: {e}")


# ─────────────────────── User-added connector store ───────────────────────

def _safe_connector_id(raw: str) -> str:
    return _SAFE_ID_RE.sub("_", str(raw or "").strip().lower()).strip("_")


def is_user_manifest(connector_id: str) -> bool:
    """True if this connector id was added by a user (deletable)."""
    load_manifests()
    return connector_id in _USER_MANIFEST_IDS


def user_manifest_ids() -> set[str]:
    load_manifests()
    return set(_USER_MANIFEST_IDS)


def save_user_manifest(data: dict[str, Any]) -> "RestConnectorManifest":
    """Persist a v1 runtime manifest to the writable user store as a Beta
    connector and refresh the cache so it is usable immediately (no restart).

    Raises ValueError on a malformed manifest or an id that collides with a
    shipped (built-in) connector."""
    if not isinstance(data, dict):
        raise ValueError("manifest must be a JSON object")
    cid = _safe_connector_id(data.get("id") or "")
    if not cid:
        raise ValueError("manifest 'id' is required (letters, digits, underscore)")
    normalized = {**data, "id": cid, "tier": "beta"}
    # Validate shape — RestConnectorManifest.from_dict raises if id is absent.
    manifest = RestConnectorManifest.from_dict(normalized)
    if not manifest.base_url and not manifest.streams:
        raise ValueError("manifest needs a base_url and at least one stream")
    if not manifest.streams:
        raise ValueError("manifest needs at least one stream")
    # Refuse to shadow a built-in connector (but allow overwriting an existing
    # user one — that's an edit).
    load_manifests()
    if cid in _MANIFEST_CACHE and cid not in _USER_MANIFEST_IDS:
        raise ValueError(f"'{cid}' is a built-in connector id — choose a different id")

    os.makedirs(_USER_MANIFEST_DIR, exist_ok=True)
    path = os.path.join(_USER_MANIFEST_DIR, f"{cid}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(normalized, f, indent=2)
    os.replace(tmp, path)
    load_manifests(force=True)  # live — appears in the picker on next fetch
    return manifest


def delete_user_manifest(connector_id: str) -> bool:
    """Remove a user-added connector. Returns False if it wasn't a user one."""
    cid = _safe_connector_id(connector_id)
    path = os.path.join(_USER_MANIFEST_DIR, f"{cid}.json")
    if not os.path.isfile(path):
        return False
    os.remove(path)
    load_manifests(force=True)
    return True


def list_manifests() -> list[RestConnectorManifest]:
    return list(load_manifests().values())


def get_manifest(connector_id: str) -> RestConnectorManifest | None:
    return load_manifests().get(connector_id)


def _connector_options() -> list[dict]:
    """Connector-picker options carrying a confidence tier. Non-certified tiers
    are suffixed into the label so the signal shows even in a plain <select>
    (a richer card picker can read the `tier` field directly)."""
    opts: list[dict] = []
    for m in list_manifests():
        tier = (getattr(m, "tier", "beta") or "beta").lower()
        label = m.name if tier == "certified" else f"{m.name} · {tier}"
        opts.append({"value": m.id, "label": label, "tier": tier})
    return opts


# ─────────────────────────── Template helpers ───────────────────────────

_TEMPLATE_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _interpolate(s: Any, env: dict[str, Any]) -> Any:
    """Replace `{name}` placeholders in strings using env values."""
    if not isinstance(s, str):
        return s
    def repl(m):
        key = m.group(1)
        val = env.get(key, "")
        return str(val) if val is not None else ""
    return _TEMPLATE_RE.sub(repl, s)


def _interpolate_dict(d: dict[str, Any], env: dict[str, Any]) -> dict[str, Any]:
    return {k: _interpolate(v, env) for k, v in (d or {}).items()}


def _deep_interpolate(v: Any, env: dict[str, Any]) -> Any:
    """Recursive template substitution — used for request bodies.

    Headers and query strings are flat key→string, so `_interpolate_dict`
    suffices for them. Bodies, however, can be nested: OpenAI's
    `messages: [{"role": "user", "content": "{prompt}"}]` would lose the
    `{prompt}` substitution under shallow interpolation. This walks
    dicts + lists + scalars and substitutes every string leaf.
    """
    if isinstance(v, str):
        return _interpolate(v, env)
    if isinstance(v, dict):
        return {k: _deep_interpolate(x, env) for k, x in v.items()}
    if isinstance(v, list):
        return [_deep_interpolate(x, env) for x in v]
    return v


def _dot_get(obj: Any, path: str) -> Any:
    """Walk dot-path; supports list traversal by returning first match.

    2026-05-25 — also handles keys that *literally contain a dot*. The
    motivating case is OData v4 pagination, whose continuation field is
    ``@odata.nextLink`` (one key, with a dot inside). Previously the
    split-on-dot logic looked for nested ``@odata → nextLink``, didn't
    find it, and v4 stopped after page 1.

    Resolution order at each step:
      1. Literal key lookup against the current dict (handles
         ``@odata.nextLink``, ``schema.field``, etc.).
      2. Fall through to dot-path traversal (handles nested ``d.__next``,
         ``data.cursor.next_id``, ``results.0.id``).

    If the literal key matches, we return immediately — no further
    traversal — so a real nested path like ``d.__next`` (where ``d``
    exists as its own key) keeps working.
    """
    if not path:
        return obj
    # Literal-key fast path: if the full path is itself a key in the
    # current object, return that value verbatim.
    if isinstance(obj, dict) and path in obj:
        return obj[path]
    cur = obj
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if 0 <= idx < len(cur) else None
        else:
            return None
    return cur


# ─────────────────────────── Auth ───────────────────────────

def _build_auth_headers(manifest: RestConnectorManifest, params: dict[str, Any]) -> dict[str, str]:
    auth = manifest.auth or {}
    atype = (auth.get("type") or "none").lower()
    headers: dict[str, str] = {}

    if atype == "none":
        return headers

    if atype == "bearer":
        token_param = auth.get("token_param", "access_token")
        token = params.get(token_param) or ""
        header_name = auth.get("header_name", "Authorization")
        template = auth.get("header_template", "Bearer {token}")
        headers[header_name] = template.replace("{token}", str(token))
        return headers

    if atype == "api_key":
        key_param = auth.get("key_param", "api_key")
        key_value = params.get(key_param) or ""
        if auth.get("header_name"):
            header_name = auth["header_name"]
            template = auth.get("header_template", "{key}")
            headers[header_name] = template.replace("{key}", str(key_value))
        # query_param handled in URL builder
        return headers

    if atype == "basic":
        u = str(params.get(auth.get("username_param", "username"), ""))
        p = str(params.get(auth.get("password_param", "password"), ""))
        token = base64.b64encode(f"{u}:{p}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
        return headers

    if atype == "oauth2":
        # If access token already provided, use it. Otherwise refresh.
        token = params.get(auth.get("access_token_param", "access_token"))
        if not token and auth.get("token_url"):
            token = _oauth2_refresh(manifest, params)
        headers["Authorization"] = f"Bearer {token or ''}"
        return headers

    return headers


def _oauth2_refresh(manifest: RestConnectorManifest, params: dict[str, Any]) -> str | None:
    """Best-effort refresh-token flow. Returns new access_token or None."""
    auth = manifest.auth or {}
    token_url = _interpolate(auth.get("token_url", ""), params)
    if not token_url:
        return None
    # This flow is grant_type=refresh_token, so it can't run without a refresh
    # token. Bail BEFORE the SSRF check / network so a credential-less call
    # (e.g. `tools/test_connector.py --dry-run`, which builds an auth-header
    # preview with no params) returns cleanly instead of crashing the SSRF
    # gate on an unresolved/templated token_url. A real run that supplies a
    # refresh_token is unaffected and still passes through check_url.
    refresh_token = params.get(auth.get("refresh_token_param", "refresh_token"), "")
    if not refresh_token:
        return None
    # SSRF gate outside the try: a policy block must surface loudly, not
    # degrade into "refresh silently failed".
    check_url(token_url, allow_private_env=API_SOURCE_ALLOW_PRIVATE_ENV)
    body = {
        "grant_type": "refresh_token",
        "client_id": params.get(auth.get("client_id_param", "client_id"), ""),
        "client_secret": params.get(auth.get("client_secret_param", "client_secret"), ""),
        "refresh_token": params.get(auth.get("refresh_token_param", "refresh_token"), ""),
    }
    try:
        data = urllib.parse.urlencode(body).encode()
        req = urllib.request.Request(
            token_url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with _GUARDED_OPENER.open(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode())
        return payload.get("access_token")
    except Exception:
        return None


# ─────────────────────────── HTTP fetch + pagination ───────────────────────────

def _http_request(
    url: str,
    headers: dict[str, str],
    method: str = "GET",
    body: Any = None,
    body_text: str | None = None,
) -> tuple[Any, dict[str, str]]:
    """HTTP request with method + body support.

    2026-06-01 audit upgrade: the original `_http_get` ignored
    `stream.method` entirely (silently sending GET against every
    endpoint) and dropped `stream.body` / `stream.body_text`. That
    broke every manifest stream meant to run as POST/PUT/DELETE —
    OpenAI chat_completions, MongoDB find, Snowflake statements,
    BigQuery query, ClickHouse SQL, Splunk search submit, etc. —
    even though the JSON looked correct.

    This function honors `method` (defaulting to GET) and sends
    exactly one of:
      * `body_text` — raw string payload (text/plain by default; the
        manifest is responsible for setting Content-Type via headers
        when it needs something else like form-encoded).
      * `body` — dict/list → JSON-encoded with
        Content-Type: application/json (only if the manifest didn't
        already set one).

    `_http_get` is preserved as a thin compatibility shim so any
    external caller keeps working.
    """
    method = (method or "GET").upper()
    out_headers = dict(headers)
    data: bytes | None = None

    if body_text is not None and body_text != "":
        data = body_text.encode("utf-8")
        # Don't override Content-Type — caller picks (text/plain,
        # application/x-www-form-urlencoded, etc.). Default only if
        # nothing was set anywhere.
        out_headers.setdefault("Content-Type", "text/plain")
    elif body is not None and body != {} and body != []:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode("utf-8")
            out_headers.setdefault("Content-Type", "application/json")
        else:
            data = str(body).encode("utf-8")

    # Every framework fetch funnels through here — initial stream URLs and
    # every pagination follow (including server-supplied absolute next-URLs),
    # so this is the one SSRF choke point. Redirect targets are re-checked
    # by _SsrfGuardedRedirectHandler.
    check_url(url, allow_private_env=API_SOURCE_ALLOW_PRIVATE_ENV)
    req = urllib.request.Request(url, data=data, headers=out_headers, method=method)
    with _GUARDED_OPENER.open(req, timeout=60) as resp:
        resp_body = resp.read().decode("utf-8", errors="replace")
        resp_headers = dict(resp.headers.items())
    try:
        return json.loads(resp_body), resp_headers
    except json.JSONDecodeError:
        return {"raw": resp_body}, resp_headers


def _http_get(url: str, headers: dict[str, str]) -> tuple[Any, dict[str, str]]:
    """Back-compat alias — always GET, no body. Prefer `_http_request`."""
    return _http_request(url, headers, method="GET")


# Pagination aliases — natural names found in vendor docs and in many
# pre-existing manifests, mapped to the framework's canonical types.
# Keeps manifests declarative-against-the-vendor instead of forcing
# every manifest author to memorise framework-internal naming.
#
# Aliases recognised:
#   page_token  → cursor       (Microsoft Graph $skiptoken, Google nextPageToken)
#   offset      → offset_limit (most SaaS pagination)
#   page        → page_number  (plain N+1 pagination)
#
# Param-name aliasing matches the wild-style names we've seen:
#   `next_field` / `cursor_field`
#   `param`      / `cursor_param` / `offset_param` / `page_param`
#   `count_param`/ `limit_param`  / `page_size_param`
def _normalize_pagination(p: dict[str, Any] | None) -> dict[str, Any]:
    if not p:
        return {"type": "none"}
    ptype = (p.get("type") or "none").lower()
    if ptype == "page_token":
        return {
            "type": "cursor",
            "cursor_field": p.get("cursor_field") or p.get("next_field", "next_cursor"),
            "cursor_param": p.get("cursor_param") or p.get("param", "cursor"),
            "next_url_path": p.get("next_url_path"),
            "max_pages": p.get("max_pages", 50),
        }
    if ptype == "offset":
        return {
            "type": "offset_limit",
            "offset_param": p.get("offset_param") or p.get("param", "offset"),
            "limit_param": p.get("limit_param") or p.get("count_param", "limit"),
            "page_size": p.get("page_size", 100),
            "max_pages": p.get("max_pages", 50),
        }
    if ptype == "page":
        return {
            "type": "page_number",
            "page_param": p.get("page_param") or p.get("param", "page"),
            "page_size_param": p.get("page_size_param") or p.get("count_param", "per_page"),
            "page_size": p.get("page_size", 100),
            "max_pages": p.get("max_pages", 50),
        }
    return p


def _build_url(base: str, path: str, query: dict[str, Any]) -> str:
    base = (base or "").rstrip("/")
    path = path or ""
    if not path.startswith("/") and not path.startswith("http"):
        path = "/" + path
    full = path if path.startswith("http") else base + path
    if query:
        # Drop empty values
        clean = {k: v for k, v in query.items() if v not in (None, "")}
        if clean:
            sep = "&" if "?" in full else "?"
            full = full + sep + urllib.parse.urlencode(clean)
    return full


def _execute_stream(
    manifest: RestConnectorManifest, stream: dict[str, Any], params: dict[str, Any]
) -> list[dict[str, Any]]:
    """Run a stream and return all collected rows across pages."""
    base = _interpolate(manifest.base_url, params)
    # Header layering (later overrides earlier):
    #   1. Accept: application/json default
    #   2. manifest.headers              — top-level static headers
    #   3. manifest.default_headers      — T1 (2026-05-23) shared defaults
    #   4. auth-derived headers          — built from params
    #   5. stream.headers                — per-stream overrides win
    headers = {"Accept": "application/json", **(manifest.headers or {})}
    headers.update(_interpolate_dict(manifest.default_headers or {}, params))
    headers.update(_build_auth_headers(manifest, params))
    headers.update(_interpolate_dict(stream.get("headers", {}), params))

    # api_key in query string
    auth = manifest.auth or {}
    extra_query: dict[str, Any] = {}
    if (auth.get("type") or "").lower() == "api_key" and auth.get("query_param"):
        extra_query[auth["query_param"]] = params.get(auth.get("key_param", "api_key"), "")

    path = _interpolate(stream.get("path", ""), params)
    # Query layering (later overrides earlier):
    #   1. manifest.default_query   — T1 (2026-05-23) shared defaults
    #   2. stream.query             — per-stream overrides win
    #   3. extra_query (api_key)    — auth-derived
    query: dict[str, Any] = _interpolate_dict(manifest.default_query or {}, params)
    query.update(_interpolate_dict(stream.get("query", {}), params))
    query.update(extra_query)
    # Strip empties: SAP rejects `sap-client=` with a 400. Manifest authors
    # set `{client}` in the default_query and leave the param blank when
    # the tenant doesn't use multi-client routing.
    query = {k: v for k, v in query.items() if v not in (None, "")}
    data_path = stream.get("data_path", "")

    # Normalize manifest-author-friendly pagination aliases
    # (page_token / offset / page) into the framework's canonical types
    # BEFORE deriving ptype / page_size below. Pre-2026-06-01 these
    # aliases silently fell through to single-page reads — manifests
    # using `page_token` or `offset` were paginating only page 1.
    pagination = _normalize_pagination(stream.get("pagination"))
    ptype = (pagination.get("type") or "none").lower()
    max_pages = int(pagination.get("max_pages", 50))

    # Resolve method + body for this stream. Bodies are deep-template-
    # interpolated against the same params dict used for path/query so
    # nested `{prompt}` / `{sql}` / `{search_query}` in JSON arrays
    # (OpenAI chat messages, Druid native queries, etc.) get
    # substituted — not just top-level keys.
    method = (stream.get("method") or "GET").upper()
    body: Any = stream.get("body")
    if body is not None:
        body = _deep_interpolate(body, params)
    body_text = stream.get("body_text")
    if isinstance(body_text, str):
        body_text = _interpolate(body_text, params)

    rows: list[dict[str, Any]] = []
    next_url: str | None = _build_url(base, path, query)
    page = 1
    page_size = int(pagination.get("page_size", 100))

    while next_url and page <= max_pages:
        try:
            payload, resp_headers = _http_request(
                next_url, headers, method=method, body=body, body_text=body_text
            )
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code} from {next_url}: {e.read().decode(errors='replace')[:300]}")
        except Exception as e:
            raise RuntimeError(f"Request failed for {next_url}: {e}")

        chunk = _dot_get(payload, data_path) if data_path else payload
        if isinstance(chunk, list):
            rows.extend(x if isinstance(x, dict) else {"value": x} for x in chunk)
        elif isinstance(chunk, dict):
            rows.append(chunk)

        # Compute next page
        if ptype == "none":
            next_url = None
        elif ptype == "page_number":
            page_param = pagination.get("page_param", "page")
            size_param = pagination.get("page_size_param", "per_page")
            if not chunk:
                next_url = None
            else:
                page += 1
                q = dict(query)
                q[page_param] = page
                q[size_param] = page_size
                next_url = _build_url(base, path, q)
        elif ptype == "offset_limit":
            offset_param = pagination.get("offset_param", "offset")
            limit_param = pagination.get("limit_param", "limit")
            if not chunk or (isinstance(chunk, list) and len(chunk) < page_size):
                next_url = None
            else:
                page += 1
                q = dict(query)
                q[offset_param] = (page - 1) * page_size
                q[limit_param] = page_size
                next_url = _build_url(base, path, q)
        elif ptype in ("cursor", "url"):
            # `url`: explicit next-URL follow. The response embeds a full
            # next URL (Twilio's `next_page_uri`, OData v4's
            # `@odata.nextLink`, OData v2's `d.__next`, Salesforce's
            # `nextRecordsUrl`). Same plumbing as cursor-with-next_url_path
            # — the two types differ only in intent: `cursor` may also
            # carry an opaque token while `url` always means a full URL.
            cursor_field = pagination.get("cursor_field", "next_cursor")
            cursor_param = pagination.get("cursor_param", "cursor")
            next_url_path = pagination.get("next_url_path")
            if next_url_path:
                next_relative = _dot_get(payload, next_url_path)
                if next_relative:
                    next_relative = str(next_relative)
                    # Three forms the server can return:
                    #   absolute URL ("https://…")  → follow as-is
                    #   server-relative ("/path")   → replace base's path
                    #   bare path ("entity?…")      → append to base path
                    # urljoin handles all three correctly given a fully-
                    # qualified base. The previous cursor implementation
                    # did string concat which double-prefixed the path
                    # on the server-relative form (T1 fix).
                    if next_relative.startswith(("http://", "https://")):
                        next_url = next_relative
                    else:
                        next_url = urllib.parse.urljoin(
                            base if base.endswith("/") else base + "/",
                            next_relative,
                        )
                else:
                    next_url = None
            elif ptype == "url":
                # url-type without next_url_path is a misconfiguration; stop
                # after first page rather than spin.
                next_url = None
            else:
                cursor = _dot_get(payload, cursor_field)
                if cursor:
                    page += 1
                    q = dict(query)
                    q[cursor_param] = cursor
                    next_url = _build_url(base, path, q)
                else:
                    next_url = None
        elif ptype == "link_header":
            link = resp_headers.get("Link", "") or resp_headers.get("link", "")
            m = re.search(r'<([^>]+)>;\s*rel="next"', link)
            next_url = m.group(1) if m else None
            page += 1
        else:
            next_url = None

    return rows


def _rows_to_relation(conn: duckdb.DuckDBPyConnection, rows: list[dict[str, Any]]) -> duckdb.DuckDBPyRelation:
    if not rows:
        return conn.sql("SELECT NULL AS empty WHERE false")
    # JSON-stringify each row, then read via DuckDB's read_json_auto over a temp file.
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, default=str) + "\n")
        return conn.sql(f"SELECT * FROM read_json_auto('{path}', format='newline_delimited')")
    finally:
        # DuckDB has read the file lazily — keep until relation is materialized.
        # We rely on caller fetching before deletion; safest to leave for OS temp cleanup.
        pass


# ─────────────────────────── Node ───────────────────────────

@register(StepType.SAAS_CONNECTOR)
class SaaSConnectorNode(BaseNode):
    """Universal SaaS node — single palette entry that drives any loaded manifest.

    Replaces the 16 individual `rest:*` virtual palette items. The user picks
    a connector from a dropdown (populated from `list_manifests()`), then a
    stream within that connector, then fills in connector-specific params.
    Execution delegates to RestApiSourceNode.
    """

    display_name = "SaaS Connector"
    category = "source"
    description = "Pick from any of the loaded SaaS manifests (Salesforce, HubSpot, Stripe, ...)"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        # Just delegate — params shape is identical.
        return RestApiSourceNode(self.params).execute(ctx)

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"connector_id": "", "stream": ""}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "connector_id", "type": "string", "label": "SaaS Connector", "required": True,
             "options": _connector_options()},
            {"name": "stream", "type": "string", "label": "Stream", "required": True},
        ]


@register(StepType.REST_CONNECTOR)
class RestApiSourceNode(BaseNode):
    """Manifest-driven REST connector. One class, hundreds of SaaS apps."""

    display_name = "REST Connector"
    category = "source"
    description = "Declarative REST/SaaS connector — selects a manifest and stream"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        connector_id = self.params.get("connector_id")
        if not connector_id:
            raise ValueError("connector_id is required (id of the manifest to run)")
        manifest = get_manifest(connector_id)
        if not manifest:
            raise ValueError(f"Unknown connector '{connector_id}'. Available: {[m.id for m in list_manifests()]}")
        stream_name = self.params.get("stream") or (manifest.streams[0]["name"] if manifest.streams else None)
        if not stream_name:
            raise ValueError(f"Connector '{connector_id}' has no streams defined")
        stream = manifest.stream(stream_name)
        if not stream:
            raise ValueError(f"Stream '{stream_name}' not found in connector '{connector_id}'")

        # Merge connector-level params (excluding internals).
        env = {k: v for k, v in self.params.items() if not k.startswith("_")}
        rows = _execute_stream(manifest, stream, env)
        return _rows_to_relation(ctx.conn, rows)

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"connector_id": "", "stream": ""}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "connector_id", "type": "string", "label": "Connector", "required": True,
             "options": _connector_options()},
            {"name": "stream", "type": "string", "label": "Stream", "required": True},
        ]
