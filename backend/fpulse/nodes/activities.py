"""
Extended activity nodes for common transformation patterns.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
import urllib.error
from typing import Any, TYPE_CHECKING

logger = logging.getLogger(__name__)

# Stage 2.5b: duckdb only used for type annotations on execute() returns
# and a few helpers.
if TYPE_CHECKING:
    import duckdb

from fpulse.ir.schema import StepType
from fpulse.nodes.base import BaseNode, ExecutionContext
from fpulse.nodes.registry import register
# 2026-06-03 (H2 fix) — SSRF guard for every user-supplied URL fetched
# by api_source / http_request / pagination continuation. Without this,
# a pipeline author could point an api_source at
# http://169.254.169.254/latest/meta-data/... (cloud metadata) or
# http://localhost:6379/ (probe internal services). Mirrors the
# longstanding OpenAPI-fetch defence. Operators with internal API
# catalogs can set FPULSE_API_SOURCE_ALLOW_PRIVATE=1 to permit
# private-network targets in trusted deployments.
from fpulse.security.ssrf import (
    check_url as _ssrf_check_url,
    SsrfBlockedError,
    API_SOURCE_ALLOW_PRIVATE_ENV,
)


# The up-front _ssrf_check_url validates the URL the operator configured, but
# urllib.request.urlopen auto-follows 3xx redirects — so a server could pass
# the check, then 302 us to http://169.254.169.254/ (cloud metadata) or an
# internal host that was never validated. Refuse to auto-follow: a redirect
# becomes a hard SSRF error. Mirrors the _NoRedirect pattern in
# connectors/ai_authoring.py.
class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise SsrfBlockedError(
            f"Refusing to follow redirect ({code}) to {newurl!r} — "
            f"redirects are blocked to prevent SSRF/DNS-rebinding bypass."
        )


def _no_redirect_opener() -> "urllib.request.OpenerDirector":
    return urllib.request.build_opener(_NoRedirectHandler())


# ── Sources ──

@register(StepType.API_SOURCE)
class ApiSourceNode(BaseNode):
    """REST/HTTP Source — read rows from a REST endpoint."""
    display_name = "API Source"
    category = "source"
    description = "Read data from a REST API endpoint"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        connection_id = self.params.get("connection_id", "")
        url = self.params.get("url", "")
        file_path = self.params.get("file_path", "")
        path = self.params.get("path", "")
        headers_extra = self.params.get("headers", {})
        # API Source is read-only — always GET. For POST/PUT/PATCH/DELETE
        # use the API Sink (write-side) or HTTP Request (action) node.
        method = "GET"
        body = ""

        pagination = self.params.get("pagination", "none")

        # R1b (2026-05-30) — Incremental sync support. When
        # sync_mode=incremental, substitute the stored watermark for
        # any `{cursor}` placeholder in url / path / headers. The
        # operator embeds {cursor} wherever their API expects the
        # filter (e.g. `/orders?updated_after={cursor}` or
        # `?since={cursor}`); the engine handles the persistence end.
        # First run with no stored cursor substitutes an empty string,
        # which most REST APIs interpret as "give me everything" — the
        # MAX is then stamped after the response materialises.
        sync_mode = (self.params.get("sync_mode") or "full_refresh").lower()
        cursor_value: str = ""
        if sync_mode == "incremental":
            cursor_value = self._load_api_cursor(ctx) or ""
            if "{cursor}" in url:
                url = url.replace("{cursor}", cursor_value)
            if "{cursor}" in path:
                path = path.replace("{cursor}", cursor_value)
            if isinstance(headers_extra, dict):
                headers_extra = {
                    k: (v.replace("{cursor}", cursor_value) if isinstance(v, str) else v)
                    for k, v in headers_extra.items()
                }

        # Priority: connection_id > url > file_path
        if connection_id:
            rows = self._fetch_via_connection(connection_id, method, path, headers_extra, body, pagination)
        elif url:
            rows = self._fetch_url(url, method, headers_extra, body, pagination)
        elif file_path:
            # 2026-05-26: project-CWD fallback for sample-pack pipelines.
            from fpulse.nodes._path_utils import resolve_input_path
            file_path = resolve_input_path(file_path, ctx.data_dir)
            return ctx.conn.read_json(file_path)
        else:
            raise ValueError("API Source: provide a connection, URL, or file_path")

        if not rows:
            return ctx.conn.sql("SELECT NULL AS empty WHERE false")

        # R1b — after a successful fetch, stamp the new watermark.
        # The cursor source field is named by `cursor_response_field`.
        # We look at every row in the response and pick the MAX value
        # for that field. Skipped silently if the field is missing
        # (e.g. operator left it blank) — the next run will simply
        # re-fetch the same window, which is degraded but correct.
        if sync_mode == "incremental":
            cursor_field = (self.params.get("cursor_response_field") or "").strip()
            if cursor_field:
                self._save_api_cursor(ctx, cursor_field, rows)

        # Load rows into DuckDB
        return ctx.conn.sql("SELECT * FROM rows_list", params={"rows_list": rows}) if False else \
            self._rows_to_relation(ctx.conn, rows)

    def _load_api_cursor(self, ctx: ExecutionContext) -> str | None:
        """R1b helper — read the persisted cursor for this step."""
        try:
            from fpulse.engine.sync_state_store import sync_state_store
            workflow_id = getattr(ctx, "workflow_id", None) or ""
            step_id = self.params.get("_step_id", "") or ""
            if not (workflow_id and step_id):
                return None
            state = sync_state_store.get(workflow_id, step_id)
            return state.last_cursor if state else None
        except Exception:  # noqa: BLE001
            return None

    def _save_api_cursor(self, ctx: ExecutionContext, cursor_field: str,
                          rows: list) -> None:
        """R1b helper — stamp MAX(cursor_field) across the rows we
        just fetched. Best-effort: any error keeps the prior cursor."""
        try:
            from fpulse.engine.sync_state_store import sync_state_store, SyncState
            workflow_id = getattr(ctx, "workflow_id", None) or ""
            step_id = self.params.get("_step_id", "") or ""
            if not (workflow_id and step_id and rows):
                return
            values = [r.get(cursor_field) for r in rows
                      if isinstance(r, dict) and r.get(cursor_field) is not None]
            if not values:
                return
            # Stringify because cursors can be int/datetime/string and we
            # store as TEXT. Comparison happens via lexicographic order
            # for strings + numeric for everything castable — matches
            # what db_source does for warehouse cursors.
            new_cursor = str(max(values))
            sync_state_store.upsert(SyncState(
                workflow_id=workflow_id,
                step_id=step_id,
                cursor_column=cursor_field,
                last_cursor=new_cursor,
                rows_last_run=len(rows),
            ))
        except Exception:  # noqa: BLE001
            pass

    def _fetch_via_connection(self, connection_id: str, method: str, path: str,
                              headers_extra: dict, body: str,
                              pagination: str = "none") -> list[dict]:
        """Fetch data using a saved Connection + Credential."""
        from fpulse.nodes.db_source import _get_connection_config
        result = _get_connection_config(connection_id)
        if not result:
            raise ValueError(f"API Source: connection '{connection_id}' not found")

        config, conn_type = result
        # 2026-05-23 (T4 + U1/U2 + V1/V2): the Oracle / SAP product
        # families are HTTP-flavoured connections that route through the
        # same API Source generic node.
        if conn_type not in (
            "rest_api", "graphql",
            "oracle_api", "oracle_fusion", "oracle_bip",
            "sap_s4hana", "sap_successfactors",
        ):
            raise ValueError(f"API Source: connection type '{conn_type}' is not an API connection")

        base_url = config.get("base_url", "").rstrip("/")
        if not base_url:
            raise ValueError("API Source: connection has no base_url configured")

        # Build full URL
        if path:
            full_url = f"{base_url}{path}" if path.startswith("/") else f"{base_url}/{path}"
        else:
            full_url = base_url

        # Build headers with auth from connection/credential
        headers = {"Accept": "application/json", "User-Agent": "F-Pulse/0.6.0"}
        auth_type = config.get("auth_type", "bearer")
        if auth_type == "basic":
            import base64
            user = config.get("username") or config.get("user", "")
            pwd = config.get("password", "")
            b64 = base64.b64encode(f"{user}:{pwd}".encode()).decode()
            headers["Authorization"] = f"Basic {b64}"
        elif config.get("api_key"):
            key_header = config.get("api_key_header", "Authorization")
            key_prefix = config.get("api_key_prefix", "Bearer")
            headers[key_header] = f"{key_prefix} {config['api_key']}"
        elif config.get("token"):
            headers["Authorization"] = f"Bearer {config['token']}"
        headers.update(headers_extra)

        if pagination != "none":
            return self._fetch_paginated(full_url, method, headers, body)
        return self._do_request(full_url, method, headers, body)

    def _fetch_url(self, url: str, method: str, headers_extra: dict, body: str,
                   pagination: str = "none") -> list[dict]:
        """Fetch data from a direct URL (no saved connection)."""
        headers = {"Accept": "application/json", "User-Agent": "F-Pulse/0.6.0"}
        headers.update(headers_extra)
        if pagination != "none":
            return self._fetch_paginated(url, method, headers, body)
        return self._do_request(url, method, headers, body)

    def _do_request(self, url: str, method: str, headers: dict, body: str) -> list[dict]:
        """Execute HTTP request with retry and parse JSON response."""
        import time as _time
        max_retries = int(self.params.get("max_retries", 3))
        retry_delay = float(self.params.get("retry_delay", 1))
        timeout = int(self.params.get("timeout", 30))

        data = body.encode("utf-8") if body else None
        if data:
            headers.setdefault("Content-Type", "application/json")

        # 2026-06-03 (H2) — SSRF guard. Refuse cloud-metadata / loopback /
        # private-network targets unless FPULSE_API_SOURCE_ALLOW_PRIVATE=1.
        # Raised as ValueError to land in the same "API Source: ..." run
        # card error path as other HTTP failures.
        try:
            _ssrf_check_url(url, allow_private_env=API_SOURCE_ALLOW_PRIVATE_ENV)
        except SsrfBlockedError as exc:
            raise ValueError(f"API Source: URL blocked by SSRF policy: {exc}") from exc

        last_error = None
        for attempt in range(max_retries + 1):
            try:
                req = urllib.request.Request(url, method=method, headers=headers, data=data)
                with _no_redirect_opener().open(req, timeout=timeout) as resp:
                    raw = json.loads(resp.read().decode("utf-8"))
                return self._normalize_response(raw)
            except urllib.error.HTTPError as exc:
                last_error = exc
                # Retry on 429 (rate limit) and 5xx (server errors)
                if exc.code == 429 or exc.code >= 500:
                    if attempt < max_retries:
                        # Check for Retry-After header
                        retry_after = exc.headers.get("Retry-After")
                        wait = float(retry_after) if retry_after else retry_delay * (2 ** attempt)
                        _time.sleep(min(wait, 60))
                        continue
                raise ValueError(f"API Source: HTTP {exc.code} from {url}: {exc.reason}") from exc
            except urllib.error.URLError as exc:
                last_error = exc
                if attempt < max_retries:
                    _time.sleep(retry_delay * (2 ** attempt))
                    continue
                raise ValueError(f"API Source: cannot reach {url}: {exc.reason}") from exc

        raise ValueError(f"API Source: failed after {max_retries} retries: {last_error}")

    def _fetch_paginated(self, url: str, method: str, headers: dict,
                         body: str) -> list[dict]:
        """Fetch all pages using configured pagination strategy."""
        pagination = self.params.get("pagination", "none")
        max_pages = int(self.params.get("max_pages", 100))
        data_key = self.params.get("data_key", "")

        if pagination == "none":
            return self._do_request(url, method, headers, body)

        all_rows: list[dict] = []
        current_url = url
        page = 1

        for _ in range(max_pages):
            raw = self._do_request_raw(current_url, method, headers, body)
            if not isinstance(raw, dict):
                all_rows.extend(raw if isinstance(raw, list) else [])
                break

            # Extract data from response
            rows = self._extract_data(raw, data_key)
            if not rows:
                break
            all_rows.extend(rows)

            # Determine next page
            if pagination == "link":
                # Follow next link (Link header or response body)
                next_url = raw.get("next") or raw.get("next_page") or raw.get("nextLink")
                if not next_url:
                    break
                current_url = next_url
            elif pagination == "offset":
                page_size = int(self.params.get("page_size", 100))
                offset_param = self.params.get("offset_param", "offset")
                if len(rows) < page_size:
                    break
                sep = "&" if "?" in url else "?"
                current_url = f"{url}{sep}{offset_param}={page * page_size}"
            elif pagination == "cursor":
                cursor_field = self.params.get("cursor_field", "cursor")
                cursor_param = self.params.get("cursor_param", "cursor")
                cursor_val = raw.get(cursor_field) or raw.get("next_cursor")
                if not cursor_val:
                    break
                sep = "&" if "?" in url else "?"
                current_url = f"{url}{sep}{cursor_param}={cursor_val}"
            elif pagination == "page":
                page_param = self.params.get("page_param", "page")
                page_size = int(self.params.get("page_size", 100))
                if len(rows) < page_size:
                    break
                page += 1
                sep = "&" if "?" in url else "?"
                current_url = f"{url}{sep}{page_param}={page}"
            else:
                break

        return all_rows

    def _do_request_raw(self, url: str, method: str, headers: dict, body: str):
        """Execute request and return raw parsed JSON (not normalized).

        Used by the pagination loop — re-validates every page URL
        through the SSRF guard because pagination ``next`` URLs and
        cursor URLs are also under user/upstream control.
        """
        import time as _time
        max_retries = int(self.params.get("max_retries", 3))
        retry_delay = float(self.params.get("retry_delay", 1))
        timeout = int(self.params.get("timeout", 30))

        data = body.encode("utf-8") if body else None
        if data:
            headers.setdefault("Content-Type", "application/json")

        # 2026-06-03 (H2) — SSRF guard on every page URL. Some APIs
        # return absolute `next_url` values that point at a different
        # host; the check here means a compromised upstream can't
        # redirect us into a private-network target on page 2.
        try:
            _ssrf_check_url(url, allow_private_env=API_SOURCE_ALLOW_PRIVATE_ENV)
        except SsrfBlockedError as exc:
            raise ValueError(f"API Source: pagination URL blocked by SSRF policy: {exc}") from exc

        for attempt in range(max_retries + 1):
            try:
                req = urllib.request.Request(url, method=method, headers=headers, data=data)
                with _no_redirect_opener().open(req, timeout=timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except (urllib.error.HTTPError, urllib.error.URLError):
                if attempt < max_retries:
                    _time.sleep(retry_delay * (2 ** attempt))
                    continue
                raise

    @staticmethod
    def _extract_data(raw: dict, data_key: str = "") -> list[dict]:
        """Extract list of records from a response dict."""
        if data_key and data_key in raw:
            val = raw[data_key]
            return val if isinstance(val, list) else [val]
        for key in ("data", "results", "items", "records", "rows", "values"):
            if key in raw and isinstance(raw[key], list):
                return raw[key]
        return [raw]

    @staticmethod
    def _normalize_response(raw) -> list[dict]:
        """Normalize response to list of dicts."""
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            for key in ("data", "results", "items", "records", "rows", "values"):
                if key in raw and isinstance(raw[key], list):
                    return raw[key]
            return [raw]
        return []

    @staticmethod
    def _rows_to_relation(conn, rows: list[dict]) -> duckdb.DuckDBPyRelation:
        """Convert list of dicts to a DuckDB relation.

        2026-05-22 — rewritten to use ``ctx.conn.from_arrow`` / pandas
        ingestion instead of hand-built ``VALUES`` SQL. The old path
        had two bugs:

          1. The rename SQL referenced ``column0``/``column1`` but
             DuckDB's ``VALUES`` clause auto-names columns
             ``col0``/``col1`` — so every API-source pipeline produced
             a Binder Error: ``Referenced column "column0" not found
             in FROM clause! Candidate bindings: "col0", "col1", ...``
          2. The ``fmt`` helper couldn't represent nested dicts /
             lists (it stringified them with Python repr like
             ``{'lat': '-37.3159', 'lng': '81.1496'}`` which DuckDB
             then rejected as malformed JSON).

        New path: build a pandas DataFrame with the union of all
        keys, let DuckDB's pandas registration handle column naming
        and complex types natively. Falls back to the literal-SQL
        path for pure-scalar rows in case pandas isn't available.
        """
        if not rows:
            return conn.sql("SELECT NULL AS empty WHERE false")

        # Collect all keys across all rows (preserve first-seen order
        # so the relation columns match the source JSON's natural
        # field order).
        all_keys: list[str] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            for k in row:
                if k not in seen:
                    all_keys.append(k)
                    seen.add(k)

        if not all_keys:
            return conn.sql("SELECT NULL AS empty WHERE false")

        # Preferred path — pandas DataFrame. DuckDB ingests it with
        # correct column names and native types (lists/dicts become
        # struct/array columns). Significantly fewer foot-guns than
        # hand-built VALUES SQL.
        try:
            import pandas as _pd  # type: ignore
            df = _pd.DataFrame([
                {k: r.get(k) if isinstance(r, dict) else None for k in all_keys}
                for r in rows
            ])
            # df_relation lifts the local DataFrame into a DuckDB
            # relation; the relation is cached for the duration of
            # the connection so the call is cheap.
            return conn.from_df(df)
        except Exception:
            # Fall through to the literal-SQL path for environments
            # without pandas. Same column-name fix as the bug above:
            # DuckDB names VALUES columns col0/col1/..., NOT
            # column0/column1/... .
            pass

        def fmt(v):
            if v is None:
                return "NULL"
            if isinstance(v, bool):
                return "true" if v else "false"
            if isinstance(v, (int, float)):
                return str(v)
            if isinstance(v, (dict, list)):
                # Serialize nested structures as JSON strings so the
                # column at least lands as TEXT rather than raising a
                # parse error on Python-repr braces.
                import json as _json
                return "'" + _json.dumps(v).replace("'", "''") + "'"
            return "'" + str(v).replace("'", "''") + "'"

        value_rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            vals = ", ".join(fmt(row.get(k)) for k in all_keys)
            value_rows.append(f"({vals})")

        if not value_rows:
            return conn.sql("SELECT NULL AS empty WHERE false")

        values_sql = ", ".join(value_rows)
        # Unique per-call temp-table name so two API-source nodes in one
        # pipeline can't clobber each other's staged rows (the shared
        # "__api_source" name meant whichever node ran last silently won for
        # both — same collision class as the internal-view recursion bug).
        # This staticmethod only receives ``conn`` (no ExecutionContext), so it
        # scopes with a uuid rather than ctx.scoped_name.
        import uuid as _uuid
        tmp = f"__api_source_{_uuid.uuid4().hex[:8]}"
        conn.execute(
            f"CREATE OR REPLACE TEMP TABLE {tmp} AS "
            f"SELECT * FROM (VALUES {values_sql})"
        )
        # FIX: DuckDB names VALUES columns col0/col1/... — not
        # column0/column1. The old code's `column{i}` rename produced
        # the very Binder Error users reported on every api_source
        # run.
        col_renames = ", ".join(
            f'col{i} AS "{k}"' for i, k in enumerate(all_keys)
        )
        return conn.sql(f"SELECT {col_renames} FROM {tmp}")

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "connection_id": "", "url": "", "file_path": "",
            "path": "", "headers": {},
            "pagination": "none", "max_pages": 100,
            "page_size": 100, "data_key": "",
            "offset_param": "offset", "page_param": "page",
            "cursor_field": "cursor", "cursor_param": "cursor",
            "max_retries": 3, "retry_delay": 1, "timeout": 30,
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            # Source tab
            {"name": "connection_id", "type": "connection_picker", "label": "Connection",
             "tab": "Source",
             "description": "Select a saved REST API connection. Or use URL below."},
            {"name": "url", "type": "text", "label": "Direct URL", "tab": "Source",
             "placeholder": "https://api.example.com/data",
             "description": "Use this if you don't have a saved connection."},
            {"name": "path", "type": "text", "label": "API Path", "tab": "Source",
             "placeholder": "/users?limit=100",
             "description": "Appended to the connection's base_url."},
            {"name": "headers", "type": "key_value", "label": "Extra Headers", "tab": "Source"},
            {"name": "data_key", "type": "text", "label": "Data Key", "tab": "Source",
             "placeholder": "data, results, items",
             "description": "JSON key containing the array of records. Auto-detected if empty."},
            {"name": "file_path", "type": "text", "label": "Local JSON (testing)", "tab": "Source",
             "placeholder": "sample.json"},
            # Pagination tab
            {"name": "pagination", "type": "select", "label": "Pagination Strategy",
             "options": ["none", "offset", "page", "cursor", "link"],
             "default": "none", "tab": "Pagination",
             "description": "none = single request, offset = OFFSET param, page = page number, cursor = cursor-based, link = follow next URL."},
            {"name": "max_pages", "type": "number", "label": "Max Pages",
             "default": 100, "tab": "Pagination",
             "description": "Safety limit to prevent infinite pagination."},
            {"name": "page_size", "type": "number", "label": "Page Size",
             "default": 100, "tab": "Pagination",
             "show_when": {"pagination": ["offset", "page"]}},
            {"name": "offset_param", "type": "text", "label": "Offset Parameter",
             "default": "offset", "tab": "Pagination",
             "show_when": {"pagination": ["offset"]}},
            {"name": "page_param", "type": "text", "label": "Page Parameter",
             "default": "page", "tab": "Pagination",
             "show_when": {"pagination": ["page"]}},
            {"name": "cursor_field", "type": "text", "label": "Cursor Field (in response)",
             "default": "cursor", "tab": "Pagination",
             "show_when": {"pagination": ["cursor"]}},
            {"name": "cursor_param", "type": "text", "label": "Cursor Parameter (in URL)",
             "default": "cursor", "tab": "Pagination",
             "show_when": {"pagination": ["cursor"]}},
            # Reliability tab
            {"name": "max_retries", "type": "number", "label": "Max Retries",
             "default": 3, "tab": "Reliability",
             "description": "Retry on 429/5xx errors with exponential backoff."},
            {"name": "retry_delay", "type": "number", "label": "Retry Delay (seconds)",
             "default": 1, "tab": "Reliability"},
            {"name": "timeout", "type": "number", "label": "Request Timeout (seconds)",
             "default": 30, "tab": "Reliability"},
            # 2026-05-30 (R1): incremental sync declaration. REST APIs vary
            # in how they filter by cursor (?since=, ?updated_after=,
            # ?modified[gte]=, etc.), so the actual filtering is the
            # operator's responsibility — either via the connection
            # manifest (declarative cursor_strategy) or by embedding
            # `{cursor}` into the URL/path/headers. The platform records
            # the watermark observed in the response so subsequent runs
            # can substitute it. See R1 scope note for the deferred
            # auto-filter work per source class.
            {"name": "sync_mode", "type": "select", "label": "Sync Mode",
             "options": ["full_refresh", "incremental"], "default": "full_refresh",
             "tab": "Incremental",
             "description": "full_refresh = fetch everything every run; incremental = embed {cursor} placeholder in URL/path and the engine substitutes the last watermark."},
            {"name": "cursor_response_field", "type": "text", "label": "Cursor Response Field",
             "tab": "Incremental",
             "placeholder": "max_updated_at",
             "show_when": {"sync_mode": ["incremental"]},
             "description": "JSON field in the response whose MAX value becomes the next run's cursor. Often a top-level `max_updated_at` or computed via _max of an `updated_at` row field."},
        ]


# ── Row-Level Transforms ──

@register(StepType.SORT)
class SortNode(BaseNode):
    """Sort transformation."""
    display_name = "Sort"
    category = "transform"
    description = "Sort rows by one or more columns"

    @staticmethod
    def expected_output_schema(input_schemas, params):
        # R5: Sort is a row reorder — column shape unchanged.
        if not input_schemas:
            return None
        return list(input_schemas[0])

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        inputs = ctx.get_inputs(self.params.get("_input_step_ids", []))
        if not inputs:
            raise ValueError("Sort node has no input data")

        # 2026-06-11 (node-audit fix): the old implementation quoted each
        # sort_by entry whole — but the UI serialises entries as
        # "amount DESC", so EVERY sort configured through the structured
        # editor produced ORDER BY "amount DESC" (a quoted identifier
        # that doesn't exist) and failed at runtime. Entries are now
        # parsed: plain strings ("amount"), direction tokens
        # ("amount DESC"), NULLS placement ("amount DESC NULLS LAST"),
        # and structured dicts ({column, direction, nulls}) all work.
        raw = self.params.get("sort_rules") or self.params.get("sort_by", [])
        default_dir = str(self.params.get("direction", "ASC")).upper()
        if isinstance(raw, str):
            raw = [t.strip() for t in raw.split(",") if t.strip()]

        rules: list[tuple[str, str, str]] = []
        for item in raw:
            if isinstance(item, dict):
                col = str(item.get("column", "")).strip()
                dirn = str(item.get("direction", default_dir) or default_dir).upper()
                nulls = str(item.get("nulls", "") or "").upper()
            else:
                toks = str(item).split()
                col = toks[0] if toks else ""
                dirn, nulls = default_dir, ""
                rest = [t.upper() for t in toks[1:]]
                if rest and rest[0] in ("ASC", "DESC"):
                    dirn = rest[0]
                    rest = rest[1:]
                if len(rest) >= 2 and rest[0] == "NULLS" and rest[1] in ("FIRST", "LAST"):
                    nulls = rest[1]
                    rest = rest[2:]
                if rest:
                    raise ValueError(
                        f"Sort: invalid sort entry '{item}'. Expected "
                        f"'<column> [ASC|DESC] [NULLS FIRST|LAST]'."
                    )
            if not col:
                continue
            if dirn not in ("ASC", "DESC"):
                raise ValueError(
                    f"Sort: invalid direction '{dirn}' for column '{col}'. Allowed: ASC, DESC."
                )
            if nulls and nulls not in ("FIRST", "LAST"):
                raise ValueError(
                    f"Sort: invalid NULLS placement '{nulls}' for column '{col}'. Allowed: FIRST, LAST."
                )
            rules.append((col, dirn, nulls))

        if not rules:
            return inputs[0]

        seen: set[str] = set()
        for col, _, _ in rules:
            if col in seen:
                raise ValueError(
                    f"Sort: column '{col}' appears more than once. Remove the duplicate sort rule."
                )
            seen.add(col)

        available = list(inputs[0].columns)
        missing = [col for col, _, _ in rules if col not in available]
        if missing:
            raise ValueError(
                f"Sort: column(s) not found: {', '.join(missing)}. "
                f"Available: {', '.join(available)}"
            )

        sort_input = ctx.register_scoped("__sort_input", inputs[0])
        parts = [
            f'"{col}" {dirn}' + (f" NULLS {nulls}" if nulls else "")
            for col, dirn, nulls in rules
        ]
        return ctx.conn.sql(f"SELECT * FROM {sort_input} ORDER BY {', '.join(parts)}")

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"sort_by": [], "direction": "ASC"}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "sort_by", "type": "column_list", "label": "Sort By Columns", "required": True,
             "description": "Each entry: '<column> [ASC|DESC] [NULLS FIRST|LAST]'."},
            {"name": "direction", "type": "select", "label": "Default Direction", "options": ["ASC", "DESC"], "default": "ASC"},
        ]


@register(StepType.RENAME)
class RenameNode(BaseNode):
    """Select/Rename transformation — pick columns or rename them."""
    display_name = "Rename Columns"
    category = "transform"
    description = "Rename columns, or pick which ones to keep"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        inputs = ctx.get_inputs(self.params.get("_input_step_ids", []))
        if not inputs:
            raise ValueError("Rename node has no input data")

        mappings = self.params.get("mappings", {})
        rename_input = ctx.register_scoped("__rename_input", inputs[0])

        if not mappings:
            return inputs[0]

        select_parts = []
        for old_name, new_name in mappings.items():
            select_parts.append(f'"{old_name}" AS "{new_name}"')

        return ctx.conn.sql(f"SELECT {', '.join(select_parts)} FROM {rename_input}")

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"mappings": {}}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "mappings", "type": "key_value", "label": "Column Mappings (old → new)", "required": True},
        ]


@register(StepType.TYPECAST)
class TypecastNode(BaseNode):
    """Data Conversion / Cast transformation."""
    display_name = "Type Cast"
    category = "transform"
    description = "Change column types — text to number, string to date, etc."

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        inputs = ctx.get_inputs(self.params.get("_input_step_ids", []))
        if not inputs:
            raise ValueError("Typecast node has no input data")

        casts = self.params.get("casts", {})
        typecast_input = ctx.register_scoped("__typecast_input", inputs[0])

        if not casts:
            return inputs[0]

        cols = inputs[0].columns
        select_parts = []
        for col in cols:
            if col in casts:
                select_parts.append(f'CAST("{col}" AS {casts[col]}) AS "{col}"')
            else:
                select_parts.append(f'"{col}"')

        return ctx.conn.sql(f"SELECT {', '.join(select_parts)} FROM {typecast_input}")

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"casts": {}}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "casts", "type": "key_value", "label": "Column → Type (e.g. amount → DOUBLE)", "required": True},
        ]


@register(StepType.DERIVED_COLUMN)
class DerivedColumnNode(BaseNode):
    """Derived Column transformation.

    Each entry in ``columns`` is ``{name, expression}`` for row-local
    expressions, or ``{name, expression, window: {partition_by, order_by}}``
    for cross-row expressions (LAG/LEAD/SUM/AVG/etc).

    2026-05-30 (R2): the optional `window` field on each column lets
    users reference adjacent rows without dropping into the full
    Window Function node. The expression is wrapped in ``OVER (...)``
    automatically when `window` is present.

    Examples
    --------

    Row-local (legacy, still works):
        {"name": "total", "expression": "qty * price"}

    Previous row's value (multi-row):
        {"name": "prev_amount",
         "expression": "LAG(amount, 1) OVER ()",
         "window": {"order_by": ["created_at"]}}

    Running total over a partition:
        {"name": "running_total",
         "expression": "SUM(amount)",
         "window": {"partition_by": ["customer_id"], "order_by": ["created_at"]}}
    """
    display_name = "Derived Column"
    category = "transform"
    description = "Add new columns by calculating from existing ones (row-local math/text/dates OR cross-row LAG/LEAD/running totals)"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        inputs = ctx.get_inputs(self.params.get("_input_step_ids", []))
        if not inputs:
            raise ValueError("Derived Column node has no input data")

        columns = self.params.get("columns", [])
        _in = ctx.register_scoped("__derived_input", inputs[0])

        if not columns:
            return inputs[0]

        # 2026-06-11 (node-audit): explicit Add-vs-Replace semantics. A
        # derived name colliding with an input column used to emit a
        # duplicate column name (SELECT *, expr AS existing) — broken
        # downstream. Now: collision + replace=true → the input column
        # is EXCLUDEd and the derived value takes its place; collision
        # without replace → a clear error instead of silent duplication.
        input_cols = set(inputs[0].columns)
        replaced: list[str] = []
        for c in columns:
            name = c.get("name") or "derived"
            if name in input_cols:
                if c.get("replace"):
                    replaced.append(name)
                else:
                    raise ValueError(
                        f"Derived Column: column '{name}' already exists on the input. "
                        f"Enable 'Replace existing column' to overwrite it, or pick a new name."
                    )

        parts: list[str] = []
        for c in columns:
            name = c.get("name") or "derived"
            expr = c.get("expression") or "NULL"
            window = c.get("window") or {}
            # R2: cross-row mode. When `window` is present we wrap the
            # expression in OVER (PARTITION BY ... ORDER BY ...).
            # Expressions that already contain OVER (...) pass through
            # unchanged so power users keep full control.
            if window and "OVER" not in expr.upper():
                partition_by = window.get("partition_by") or []
                order_by = window.get("order_by") or []
                clauses: list[str] = []
                if partition_by:
                    cols_p = ", ".join(f'"{p}"' for p in partition_by)
                    clauses.append(f"PARTITION BY {cols_p}")
                if order_by:
                    cols_o = ", ".join(f'"{o}"' for o in order_by)
                    clauses.append(f"ORDER BY {cols_o}")
                expr = f"{expr} OVER ({' '.join(clauses)})"
            parts.append(f'{expr} AS "{name}"')

        extras = ", ".join(parts)
        if replaced:
            exclude = ", ".join(f'"{c}"' for c in replaced)
            return ctx.conn.sql(f"SELECT * EXCLUDE ({exclude}), {extras} FROM {_in}")
        return ctx.conn.sql(f"SELECT *, {extras} FROM {_in}")

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"columns": [{"name": "new_col", "expression": "1"}]}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "columns", "type": "derived_list", "label": "Derived Columns",
             "required": True,
             "description": (
                 "Each row: {name, expression}. Add optional "
                 "{window: {partition_by: [...], order_by: [...]}} to reference "
                 "adjacent rows via LAG/LEAD/SUM/AVG without the full Window node."
             )},
        ]

    @staticmethod
    def expected_output_schema(input_schemas, params):
        # R5: Derived Column appends one row per `columns` entry to
        # the upstream shape. Type is "UNKNOWN" — we can't predict
        # DuckDB's coercion without binding the expression.
        if not input_schemas:
            return None
        out = list(input_schemas[0])
        for c in (params.get("columns") or []):
            name = c.get("name")
            if name:
                out.append({"name": name, "type": "UNKNOWN"})
        return out


# ── Set-Level Transforms ──

@register(StepType.LOOKUP)
class LookupNode(BaseNode):
    """Lookup transformation — enrich rows by joining a reference dataset.

    2026-06-11 (validation-audit follow-up): promoted from "a LEFT JOIN
    disguised as a Lookup" to a real lookup node. The old implementation
    had four hidden assumptions a user could never see or change:

      * input order WAS semantics (connection #2 = reference) — but users
        rearrange nodes, so this was a silent foot-gun;
      * one shared key name on both sides;
      * always LEFT JOIN, always every reference column (``SELECT
        main.*, ref.*`` — which also emitted a duplicate key column);
      * multiple matches silently fanned out rows.

    Parameters (all optional except lookup_key; defaults reproduce the
    legacy behaviour so existing pipelines keep working):

      lookup_input_id : str   step id of the REFERENCE input. Defaults
                              to the second connection (legacy).
      main_key        : str   key column on the main stream. Defaults
                              to ``lookup_key`` (legacy shared-key).
      lookup_key      : str   key column on the reference dataset.
      no_match        : str   'keep' (LEFT JOIN, default) | 'drop' (INNER).
      multiple_match  : str   'all' (default, row fan-out) | 'first'
                              (one arbitrary match per key).
      return_columns  : list  reference columns to append. Empty = all
                              except the key. Names colliding with a
                              main-stream column get a ``_lookup`` suffix.
    """
    display_name = "Lookup Join"
    category = "transform"
    description = "Enrich rows with columns from a reference dataset matched on a key (lookup join)"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        input_ids = self.params.get("_input_step_ids", [])
        if len(input_ids) < 2:
            raise ValueError("Lookup node requires 2 inputs (main + lookup)")

        # Explicit reference selection — connection order is layout, not
        # semantics. Fall back to "second connection" for old pipelines.
        ref_id = self.params.get("lookup_input_id") or ""
        ref_idx = input_ids.index(ref_id) if ref_id in input_ids else 1
        main_idx = 0 if ref_idx != 0 else 1

        main = ctx.get_input(input_ids[main_idx])
        lookup = ctx.get_input(input_ids[ref_idx])
        if main is None or lookup is None:
            raise ValueError("Lookup node: inputs missing")

        lookup_key = self.params.get("lookup_key", "")
        if not lookup_key:
            raise ValueError("Lookup node requires a lookup_key")
        main_key = self.params.get("main_key") or lookup_key

        main_cols = list(main.columns)
        ref_cols = list(lookup.columns)
        if main_key not in main_cols:
            raise ValueError(
                f"Lookup: main-stream key '{main_key}' not found in main input "
                f"(columns: {', '.join(main_cols)})"
            )
        if lookup_key not in ref_cols:
            raise ValueError(
                f"Lookup: lookup-dataset key '{lookup_key}' not found in reference input "
                f"(columns: {', '.join(ref_cols)})"
            )

        requested = self.params.get("return_columns") or []
        if requested:
            missing = [c for c in requested if c not in ref_cols]
            if missing:
                raise ValueError(
                    f"Lookup: return column(s) not in reference dataset: {', '.join(missing)}"
                )
            selected = [c for c in requested if c != lookup_key]
        else:
            # Default: every reference column except the key — the main
            # stream already carries its own key column.
            selected = [c for c in ref_cols if c != lookup_key]

        lookup_main = ctx.register_scoped("__lookup_main", main)
        lookup_ref = ctx.register_scoped("__lookup_ref", lookup)

        main_col_set = set(main_cols)
        projections = []
        for col in selected:
            alias = f"{col}_lookup" if col in main_col_set else col
            projections.append(f'__ref."{col}" AS "{alias}"')
        select_ref = (", " + ", ".join(projections)) if projections else ""

        # 'first' dedupes the reference on the key before joining so a
        # many-row reference can't fan the main stream out.
        if self.params.get("multiple_match", "all") == "first":
            ref_rel = (
                f'(SELECT * FROM {lookup_ref} '
                f'QUALIFY ROW_NUMBER() OVER (PARTITION BY "{lookup_key}") = 1)'
            )
        else:
            ref_rel = lookup_ref

        join_kw = "JOIN" if self.params.get("no_match", "keep") == "drop" else "LEFT JOIN"

        return ctx.conn.sql(
            f'SELECT {lookup_main}.*{select_ref} '
            f'FROM {lookup_main} '
            f'{join_kw} {ref_rel} AS __ref '
            f'ON {lookup_main}."{main_key}" = __ref."{lookup_key}"'
        )

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "lookup_key": "",
            "main_key": "",
            "lookup_input_id": "",
            "no_match": "keep",
            "multiple_match": "all",
            "return_columns": [],
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "lookup_key", "type": "column", "label": "Lookup Dataset Key", "required": True,
             "description": "Key column on the reference dataset."},
            {"name": "main_key", "type": "column", "label": "Main Stream Key",
             "description": "Key column on the main stream. Defaults to the Lookup Dataset Key."},
            {"name": "lookup_input_id", "type": "text", "label": "Lookup Input (step id)",
             "description": "Which connected input is the reference dataset. Defaults to the second connection."},
            {"name": "no_match", "type": "select", "label": "If No Match",
             "options": ["keep", "drop"], "default": "keep",
             "description": "keep = keep main row with NULLs (left join); drop = drop main row (inner join)."},
            {"name": "multiple_match", "type": "select", "label": "If Multiple Matches",
             "options": ["all", "first"], "default": "all",
             "description": "all = return every match (may duplicate main rows); first = one match per key."},
            {"name": "return_columns", "type": "text", "label": "Return Columns",
             "description": "Reference columns to append. Empty = all except the key."},
        ]


@register(StepType.UNION)
class UnionNode(BaseNode):
    """Union All transformation — stack rows from multiple inputs."""
    display_name = "Union"
    category = "transform"
    description = "Stack rows from two or more datasets into one"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        input_ids = self.params.get("_input_step_ids", [])
        inputs = ctx.get_inputs(input_ids)
        if len(inputs) < 2:
            raise ValueError("Union requires at least 2 inputs")

        # 2026-06-11 (node-audit): three real modes. The frontend offered
        # 'by_name' but the engine treated anything != 'all' as UNION
        # (distinct, positional) — a silent dead option. by_name now maps
        # to DuckDB's UNION ALL BY NAME: columns matched by NAME, missing
        # columns NULL-filled — the "schema union" most ingestion
        # pipelines actually want.
        mode = self.params.get("mode", "all")
        if mode not in ("all", "distinct", "by_name"):
            raise ValueError(
                f"Union: invalid mode '{mode}'. Allowed: all, distinct, by_name."
            )

        for i, inp in enumerate(inputs):
            ctx.conn.register(f"__union_{i}", inp)

        parts = [f"SELECT * FROM __union_{i}" for i in range(len(inputs))]
        op = {
            "all": "UNION ALL",
            "distinct": "UNION",
            "by_name": "UNION ALL BY NAME",
        }[mode]
        return ctx.conn.sql(f" {op} ".join(parts))

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"mode": "all"}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "mode", "type": "select", "label": "Mode",
             "options": ["all", "distinct", "by_name"], "default": "all",
             "description": "all = stack rows (keep duplicates, match columns by position); "
                            "distinct = stack + drop duplicate rows; "
                            "by_name = match columns by NAME, NULL-fill missing columns (schema union)."},
        ]


@register(StepType.PIVOT)
class PivotNode(BaseNode):
    """Pivot transformation."""
    display_name = "Pivot"
    category = "transform"
    description = "Turn distinct values in one column into separate columns (e.g. months → 12 columns)"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        inputs = ctx.get_inputs(self.params.get("_input_step_ids", []))
        if not inputs:
            raise ValueError("Pivot node has no input data")

        pivot_col = self.params.get("pivot_column", "")
        value_col = self.params.get("value_column", "")
        agg_func = self.params.get("agg_function", "SUM")
        group_by = self.params.get("group_by", [])

        if not pivot_col or not value_col:
            raise ValueError("Pivot requires pivot_column and value_column")

        available = list(inputs[0].columns)
        for c in (pivot_col, value_col):
            if c not in available:
                raise ValueError(
                    f"Pivot: column '{c}' not found. Available: {', '.join(available)}"
                )

        # 2026-06-15 (node-audit): freeze + fill.
        #  * pivot_values — explicit IN (...) list so the output columns are
        #    STABLE run-to-run even when a value is missing from this batch
        #    (otherwise the pivoted schema shifts with the data).
        #  * fill_value — replace the NULL cells DuckDB produces for absent
        #    (group × pivot-value) combinations.
        pivot_values = self.params.get("pivot_values") or []
        if isinstance(pivot_values, str):
            pivot_values = [s.strip() for s in pivot_values.split(",") if s.strip()]
        fill_value = self.params.get("fill_value", "")

        pivot_input = ctx.register_scoped("__pivot_input", inputs[0])

        if group_by:
            group_set = list(group_by)
            group_clause = "GROUP BY " + ", ".join('"' + g + '"' for g in group_by)
        else:
            # No explicit Row Columns → group by every remaining column. List
            # them EXPLICITLY: DuckDB's PIVOT *statement* rejects "GROUP BY ALL"
            # ("Parser Error: syntax error at or near ALL"), so the old fallback
            # made every Row-Columns-empty pivot fail. (2026-06-16 in-app find.)
            group_set = [c for c in available if c not in (pivot_col, value_col)]
            group_clause = (
                "GROUP BY " + ", ".join('"' + g + '"' for g in group_set)
                if group_set else ""
            )

        in_clause = ""
        if pivot_values:
            vals = ", ".join("'" + str(v).replace("'", "''") + "'" for v in pivot_values)
            in_clause = f" IN ({vals})"

        pivoted = ctx.conn.sql(
            f'PIVOT {pivot_input} ON "{pivot_col}"{in_clause} '
            f'USING {agg_func}("{value_col}") {group_clause}'
        )

        if fill_value != "" and fill_value is not None:
            result_cols = list(pivoted.columns)
            value_cols = [c for c in result_cols if c not in group_set]
            if value_cols:
                fill_lit = self._fill_literal(fill_value)
                pivot_result = ctx.register_scoped("__pivot_result", pivoted)
                parts = [
                    (f'COALESCE("{c}", {fill_lit}) AS "{c}"' if c in value_cols else f'"{c}"')
                    for c in result_cols
                ]
                pivoted = ctx.conn.sql(f'SELECT {", ".join(parts)} FROM {pivot_result}')

        return pivoted

    @staticmethod
    def _fill_literal(value: Any) -> str:
        """Render the fill value as a SQL literal — numeric as-is (so it
        matches numeric pivot columns), anything else single-quoted."""
        s = str(value).strip()
        try:
            float(s)
            return s
        except ValueError:
            return "'" + s.replace("'", "''") + "'"

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "pivot_column": "", "value_column": "", "agg_function": "SUM",
            "group_by": [], "pivot_values": "", "fill_value": "",
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "pivot_column", "type": "text", "label": "Pivot Column", "required": True},
            {"name": "value_column", "type": "text", "label": "Value Column", "required": True},
            {"name": "agg_function", "type": "select", "label": "Aggregate", "options": ["SUM", "COUNT", "AVG", "MIN", "MAX"]},
            {"name": "group_by", "type": "column_list", "label": "Group By"},
            {"name": "pivot_values", "type": "text", "label": "Pivot Values (freeze)",
             "placeholder": "Jan, Feb, Mar",
             "description": "Optional comma-separated list of pivot-column values to "
                            "produce. Pins the output columns so they don't shift when "
                            "a value is missing from a run. Empty = derive from data."},
            {"name": "fill_value", "type": "text", "label": "Fill Empty Cells",
             "placeholder": "0",
             "description": "Optional value for empty (group × value) cells. Use 0 for "
                            "numeric pivots; text for string aggregates. Empty = leave NULL."},
        ]


@register(StepType.UNPIVOT)
class UnpivotNode(BaseNode):
    """Unpivot transformation."""
    display_name = "Unpivot"
    category = "transform"
    description = "Turn columns back into rows (one row per value — opposite of Pivot)"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        inputs = ctx.get_inputs(self.params.get("_input_step_ids", []))
        if not inputs:
            raise ValueError("Unpivot node has no input data")

        columns = self.params.get("columns", [])
        name_col = self.params.get("name_column", "attribute")
        value_col = self.params.get("value_column", "value")
        # 2026-06-11 (node-audit): explicit identifier columns + null handling.
        id_columns = self.params.get("id_columns") or []
        include_nulls = bool(self.params.get("include_nulls", False))

        if not columns:
            raise ValueError("Unpivot requires columns to unpivot")

        available = list(inputs[0].columns)
        missing = [c for c in columns if c not in available]
        if missing:
            raise ValueError(
                f"Unpivot: column(s) to unpivot not found: {', '.join(missing)}. "
                f"Available: {', '.join(available)}"
            )

        unpivot_input = ctx.register_scoped("__unpivot_input", inputs[0])

        # Identifier columns: which columns are CARRIED on each output row.
        # DuckDB keeps every non-unpivoted column by default; when the user
        # names explicit identifiers we pre-project to just those + the
        # value columns, so unrelated columns are dropped (standard
        # melt semantics). Empty list = keep all others (back-compat).
        if id_columns:
            id_missing = [c for c in id_columns if c not in available]
            if id_missing:
                raise ValueError(
                    f"Unpivot: identifier column(s) not found: {', '.join(id_missing)}. "
                    f"Available: {', '.join(available)}"
                )
            keep = ", ".join('"' + c + '"' for c in [*id_columns, *columns])
            src = f"(SELECT {keep} FROM {unpivot_input})"
        else:
            src = unpivot_input

        col_list = ", ".join('"' + c + '"' for c in columns)
        # DuckDB drops rows whose value is NULL by default; INCLUDE NULLS
        # keeps them (the "keep nulls" option enterprise melts often want).
        # The FROM-expression form is used because INCLUDE NULLS is only
        # accepted there (not in the bare `UNPIVOT tbl ON ...` statement).
        nulls_kw = "INCLUDE NULLS " if include_nulls else ""
        return ctx.conn.sql(
            f'SELECT * FROM {src} UNPIVOT {nulls_kw}'
            f'("{value_col}" FOR "{name_col}" IN ({col_list}))'
        )

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "columns": [], "name_column": "attribute", "value_column": "value",
            "id_columns": [], "include_nulls": False,
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "columns", "type": "column_list", "label": "Columns to Unpivot", "required": True},
            {"name": "id_columns", "type": "column_list", "label": "Identifier Columns (keep)",
             "description": "Columns carried onto every output row. Empty = keep all columns that aren't being unpivoted."},
            {"name": "name_column", "type": "text", "label": "Name Column", "default": "attribute"},
            {"name": "value_column", "type": "text", "label": "Value Column", "default": "value"},
            {"name": "include_nulls", "type": "boolean", "label": "Include Null Values", "default": False,
             "description": "Keep output rows whose value is NULL (DuckDB drops them by default)."},
        ]


@register(StepType.WINDOW)
class WindowNode(BaseNode):
    """Window Function — enterprise-level with multiple functions and frame spec.

    Window transformation.

    Features:
      - Multiple window functions in a single node
      - Frame specification: ROWS/RANGE BETWEEN
      - All functions: ROW_NUMBER, RANK, DENSE_RANK, NTILE, LAG, LEAD,
        FIRST_VALUE, LAST_VALUE, NTH_VALUE, SUM, AVG, MIN, MAX, COUNT
      - Per-function column reference (for LAG/LEAD/SUM etc.)
      - Sort direction per ORDER BY column (ASC/DESC)
    """
    display_name = "Window"
    category = "transform"
    description = "Add running totals, ranks, or values from neighboring rows"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        inputs = ctx.get_inputs(self.params.get("_input_step_ids", []))
        if not inputs:
            raise ValueError("Window node has no input data")

        window_input = ctx.register_scoped("__window_input", inputs[0])

        # Shared window spec
        partition_by = self.params.get("partition_by", [])
        order_by = self.params.get("order_by", [])
        # 2026-05-30 (audit): legacy frontend wrote `sort_direction`;
        # canonical is `order_direction` (matches SQL OVER syntax).
        order_direction = (
            self.params.get("order_direction")
            or self.params.get("sort_direction")
            or "ASC"
        ).upper()
        frame = self.params.get("frame", "").strip()

        partition_clause = ""
        if partition_by:
            partition_cols = ", ".join(f'"{p}"' for p in partition_by)
            partition_clause = f"PARTITION BY {partition_cols}"

        order_clause = ""
        if order_by:
            # 2026-06-11 (node-audit): entries may carry an inline
            # direction ("amount DESC") from the free-text editor — the
            # old code quoted the whole token as an identifier and the
            # window broke at runtime. Inline direction wins; the shared
            # order_direction is the default for plain column names.
            parsed_cols = []
            for o in order_by:
                toks = str(o).split()
                col = toks[0] if toks else ""
                if not col:
                    continue
                inline = toks[1].upper() if len(toks) > 1 else ""
                if inline and inline not in ("ASC", "DESC"):
                    raise ValueError(
                        f"Window: invalid order-by entry '{o}'. Expected '<column> [ASC|DESC]'."
                    )
                parsed_cols.append(f'"{col}" {inline or order_direction}')
            order_clause = f"ORDER BY {', '.join(parsed_cols)}" if parsed_cols else ""

        frame_clause = frame if frame else ""
        window_spec = f"{partition_clause} {order_clause} {frame_clause}".strip()

        # Support multiple window functions.
        # 2026-05-30 (audit): legacy frontend wrote `functions`; canonical
        # is `window_functions` (avoids collision with single inline
        # `function` field below). Read either.
        window_functions = (
            self.params.get("window_functions")
            or self.params.get("functions")
            or []
        )

        # Legacy single function support
        if not window_functions:
            func = self.params.get("function", "ROW_NUMBER()")
            alias = self.params.get("alias", "row_num")
            window_functions = [{"function": func, "alias": alias}]

        window_exprs = []
        for wf in window_functions:
            func_name = wf.get("function", "ROW_NUMBER").upper().rstrip("()")
            col = wf.get("column", "").strip()
            alias = wf.get("alias", f"{func_name.lower()}_result")
            offset = int(wf.get("offset", 1))
            n = int(wf.get("n", 4))

            # Build the function expression
            if func_name in ("ROW_NUMBER", "RANK", "DENSE_RANK", "CUME_DIST", "PERCENT_RANK"):
                expr = f"{func_name}()"
            elif func_name == "NTILE":
                expr = f"NTILE({n})"
            elif func_name in ("LAG", "LEAD"):
                if col:
                    expr = f'{func_name}("{col}", {offset})'
                else:
                    expr = f"{func_name}(*, {offset})"
            elif func_name in ("FIRST_VALUE", "LAST_VALUE"):
                expr = f'{func_name}("{col}")' if col else f"{func_name}(*)"
            elif func_name == "NTH_VALUE":
                expr = f'{func_name}("{col}", {n})' if col else f"NTH_VALUE(*, {n})"
            elif func_name in ("SUM", "AVG", "MIN", "MAX", "COUNT"):
                if col:
                    expr = f'{func_name}("{col}")'
                else:
                    expr = f"{func_name}(*)"
            else:
                # Custom expression (e.g., already has parentheses)
                expr = wf.get("function", "ROW_NUMBER()")

            window_exprs.append(f'{expr} OVER ({window_spec}) AS "{alias}"')

        window_select = ", ".join(window_exprs)
        return ctx.conn.sql(f"SELECT *, {window_select} FROM {window_input}")

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "partition_by": [], "order_by": [], "order_direction": "ASC",
            "frame": "",
            "window_functions": [
                {"function": "ROW_NUMBER", "alias": "row_num"},
            ],
            # Legacy compat
            "function": "ROW_NUMBER()", "alias": "row_num",
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "partition_by", "type": "column_list", "label": "Partition By",
             "tab": "Window",
             "description": "Columns to partition the window. Like GROUP BY but keeps all rows."},
            {"name": "order_by", "type": "column_list", "label": "Order By",
             "tab": "Window",
             "description": "Columns to sort within each partition."},
            {"name": "order_direction", "type": "select", "label": "Sort Direction",
             "options": ["ASC", "DESC"], "default": "ASC", "tab": "Window"},
            {"name": "frame", "type": "text", "label": "Frame Specification",
             "tab": "Window",
             "placeholder": "ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING",
             "description": "Optional frame: ROWS/RANGE BETWEEN ... AND ..."},
            {"name": "window_functions", "type": "window_function_list",
             "label": "Window Functions", "required": True, "tab": "Functions",
             "description": (
                 "Functions: ROW_NUMBER, RANK, DENSE_RANK, NTILE, LAG, LEAD, "
                 "FIRST_VALUE, LAST_VALUE, NTH_VALUE, SUM, AVG, MIN, MAX, COUNT. "
                 "Each has: function, column (for LAG/LEAD/SUM...), alias, offset (LAG/LEAD), n (NTILE/NTH_VALUE)."
             )},
        ]


# ── Quality & Control ──

@register(StepType.SAMPLE)
class SampleNode(BaseNode):
    """Sample — return a subset of rows."""
    display_name = "Sample"
    category = "transform"
    description = "Take a sample of rows"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        inputs = ctx.get_inputs(self.params.get("_input_step_ids", []))
        if not inputs:
            raise ValueError("Sample node has no input data")

        # 2026-06-11 (node-audit fix): the old node read only `count` +
        # `method`, while the UI offered Count AND Fraction — `fraction`
        # was a dead param the engine never read, so a fraction-only
        # config silently sampled the default 100 rows. Sampling is now
        # an explicit, mutually-exclusive mode:
        #
        #   mode   : 'rows' (fixed row count) | 'percent' (proportion)
        #   method : 'first' (deterministic prefix) | 'random'
        #   seed   : reproducible random sampling (optional)
        #
        # Legacy params still resolve: `count` → rows mode; `fraction`
        # (0-1) → percent mode. When both are present, the explicit
        # `mode` wins; without it, `count` wins and we warn — never
        # silently ambiguous.
        params = self.params
        mode = str(params.get("mode") or "").lower()
        method = str(params.get("method", "first")).lower()
        seed = params.get("seed")
        fraction = params.get("fraction")
        percent = params.get("percent")
        count = params.get("count")

        if mode not in ("rows", "percent"):
            if count is not None and (fraction is not None or percent is not None):
                logger.warning(
                    "Sample: both count and fraction/percent set without an explicit "
                    "mode — using rows (count=%s). Set mode='percent' to sample by proportion.",
                    count,
                )
                mode = "rows"
            elif fraction is not None or percent is not None:
                mode = "percent"
            else:
                mode = "rows"

        if method not in ("first", "random"):
            raise ValueError(f"Sample: invalid method '{method}'. Allowed: first, random.")
        if seed is not None:
            try:
                seed = int(seed)
            except (TypeError, ValueError):
                raise ValueError(f"Sample: seed must be an integer, got '{seed}'.")

        sample_input = ctx.register_scoped("__sample_input", inputs[0])

        if mode == "percent":
            if percent is None:
                percent = float(fraction) * 100.0 if fraction is not None else None
            if percent is None:
                raise ValueError("Sample: percent mode requires 'percent' (0-100).")
            try:
                pct = float(percent)
            except (TypeError, ValueError):
                raise ValueError(f"Sample: percent must be numeric, got '{percent}'.")
            if not (0.0 < pct <= 100.0):
                raise ValueError(f"Sample: percent must be between 0 and 100, got {pct}.")
            if method == "first":
                total = ctx.conn.sql(f"SELECT COUNT(*) FROM {sample_input}").fetchone()[0]
                n = max(1, int(round(total * pct / 100.0))) if total else 0
                return ctx.conn.sql(f"SELECT * FROM {sample_input} LIMIT {n}")
            sampler = f"{pct} PERCENT (bernoulli, {seed})" if seed is not None else f"{pct} PERCENT"
            return ctx.conn.sql(f"SELECT * FROM {sample_input} USING SAMPLE {sampler}")

        # rows mode
        if count is None:
            count = 100
        try:
            n = int(count)
        except (TypeError, ValueError):
            raise ValueError(f"Sample: count must be numeric, got '{count}'.")
        if n <= 0:
            raise ValueError(f"Sample: count must be greater than 0, got {n}.")
        if method == "random":
            if seed is not None:
                return ctx.conn.sql(
                    f"SELECT * FROM {sample_input} USING SAMPLE reservoir({n} ROWS) REPEATABLE ({seed})"
                )
            return ctx.conn.sql(f"SELECT * FROM {sample_input} USING SAMPLE {n} ROWS")
        return ctx.conn.sql(f"SELECT * FROM {sample_input} LIMIT {n}")

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"mode": "rows", "count": 100, "method": "first", "percent": None, "seed": None}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "mode", "type": "select", "label": "Sampling Mode",
             "options": ["rows", "percent"], "default": "rows",
             "description": "rows = fixed row count; percent = proportion of input (0-100)."},
            {"name": "count", "type": "number", "label": "Row Count", "default": 100,
             "show_when": {"mode": ["rows"]}},
            {"name": "percent", "type": "number", "label": "Percent (0-100)",
             "show_when": {"mode": ["percent"]}},
            {"name": "method", "type": "select", "label": "Method",
             "options": ["first", "random"], "default": "first",
             "description": "first = deterministic prefix; random = statistical sample."},
            {"name": "seed", "type": "number", "label": "Random Seed (optional)",
             "description": "Makes random sampling reproducible across runs."},
        ]


@register(StepType.VALIDATE)
class ValidateNode(BaseNode):
    """Data Validation — assert row-level rules."""
    display_name = "Validate"
    category = "transform"
    description = "Check rows against rules and mark which ones passed or failed"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        inputs = ctx.get_inputs(self.params.get("_input_step_ids", []))
        if not inputs:
            raise ValueError("Validate node has no input data")

        rules = self.params.get("rules", [])
        validate_input = ctx.register_scoped("__validate_input", inputs[0])

        if not rules:
            return ctx.conn.sql(f"SELECT *, true AS is_valid FROM {validate_input}")

        conditions = " AND ".join(f"({r['condition']})" for r in rules)
        return ctx.conn.sql(
            f"SELECT *, CASE WHEN {conditions} THEN true ELSE false END AS is_valid FROM {validate_input}"
        )

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"rules": [{"name": "not_null", "condition": "id IS NOT NULL"}]}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "rules", "type": "rule_list", "label": "Validation Rules", "required": True},
        ]


@register(StepType.CONDITIONAL_SPLIT)
class ConditionalSplitNode(BaseNode):
    """Conditional Split — route rows based on conditions.

    Each condition is evaluated in order. The first matching condition
    assigns a `_split_output` label to that row. Rows matching no
    condition get the default output label.

    Modes:
      - "first_match" (default): each row gets the FIRST condition it matches
      - "all_match": a row can appear in MULTIPLE outputs (duplicated)
      - "filter": only return rows matching the active_output condition

    The `_split_output` column lets downstream nodes filter by output name.
    """
    display_name = "Switch"
    category = "transform"
    description = "Route each row to a named output branch by condition (multi-output)"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        inputs = ctx.get_inputs(self.params.get("_input_step_ids", []))
        if not inputs:
            raise ValueError("Conditional Split has no input data")

        conditions = self.params.get("conditions", [])
        default_output = self.params.get("default_output", "default")
        # 2026-05-30 (audit): legacy frontend wrote `split_mode` /
        # `filter_output`. Canonical keys are `mode` / `active_output`.
        # Read both so saved pipelines keep using the correct branch.
        mode = self.params.get("mode") or self.params.get("split_mode") or "first_match"
        active_output = (
            self.params.get("active_output")
            or self.params.get("filter_output")
            or ""
        ).strip()

        split_input = ctx.register_scoped("__split_input", inputs[0])

        # 2026-06-15 (security): the output LABELS (name / default_output /
        # active_output) are user-supplied and were interpolated into SQL
        # string literals unescaped — a single quote broke the query and was
        # an injection vector. Escape them. (The `condition` strings are raw
        # SQL expressions the user authors on purpose, like a Filter, so they
        # are not escaped — but they ARE author-only, never row data.)
        def _q(s: Any) -> str:
            return str(s).replace("'", "''")

        if not conditions:
            return ctx.conn.sql(
                f"SELECT *, '{_q(default_output)}' AS _split_output FROM {split_input}"
            )

        if mode == "filter" and active_output:
            # Return only rows matching a specific output
            for cond in conditions:
                if cond.get("name", "") == active_output:
                    return ctx.conn.sql(
                        f"SELECT *, '{_q(active_output)}' AS _split_output "
                        f"FROM {split_input} WHERE {cond['condition']}"
                    )
            # If active_output = default, return unmatched rows
            if active_output == default_output:
                all_conditions = " OR ".join(
                    f"({c['condition']})" for c in conditions if c.get("condition")
                )
                return ctx.conn.sql(
                    f"SELECT *, '{_q(default_output)}' AS _split_output "
                    f"FROM {split_input} WHERE NOT ({all_conditions})"
                )
            return inputs[0]

        # Build CASE WHEN expression for labeling
        case_parts = []
        for cond in conditions:
            name = cond.get("name", "unnamed")
            condition = cond.get("condition", "1=0")
            case_parts.append(f"WHEN {condition} THEN '{_q(name)}'")

        case_expr = f"CASE {' '.join(case_parts)} ELSE '{_q(default_output)}' END"

        return ctx.conn.sql(
            f"SELECT *, {case_expr} AS _split_output FROM {split_input}"
        )

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "conditions": [{"name": "match", "condition": "1=1"}],
            "default_output": "default",
            "mode": "first_match",
            "active_output": "",
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "conditions", "type": "rule_list", "label": "Split Conditions",
             "required": True, "tab": "Conditions",
             "description": "Each condition: {name: 'output_label', condition: 'SQL expression'}. Evaluated in order."},
            {"name": "default_output", "type": "text", "label": "Default Output Name",
             "default": "default", "tab": "Conditions",
             "description": "Label for rows matching no condition."},
            {"name": "mode", "type": "select", "label": "Split Mode",
             "options": ["first_match", "filter"], "default": "first_match",
             "tab": "Behavior",
             "description": "first_match = label all rows with CASE WHEN, filter = only return one output."},
            {"name": "active_output", "type": "text", "label": "Active Output (filter mode)",
             "tab": "Behavior",
             "show_when": {"mode": ["filter"]},
             "description": "Which output name to return when mode=filter."},
        ]


# ── Destinations ──

@register(StepType.DB_SINK)
class DbSinkNode(BaseNode):
    """Database Sink — writes data to a real database via saved Connection."""
    display_name = "Database Sink"
    category = "output"
    description = "Write data to a database table"

    @staticmethod
    def preview_message(params, row_count):
        # X4 — surface table + mode the dry-run WOULD have written.
        table = params.get("table") or params.get("table_name") or "(no table)"
        schema = params.get("schema")
        full = f"{schema}.{table}" if schema else table
        mode = params.get("mode", "append")
        return (
            f"would write {row_count} row{'s' if row_count != 1 else ''} "
            f"to {full} (mode={mode})"
        )

    # ── DuckDB→SQL type mapping ────────────────────────────────────
    _DUCKDB_TO_SQL = {
        "BIGINT": "BIGINT", "INTEGER": "INTEGER", "SMALLINT": "SMALLINT",
        "TINYINT": "SMALLINT", "HUGEINT": "BIGINT",
        "DOUBLE": "DOUBLE PRECISION", "FLOAT": "REAL",
        "DECIMAL": "DECIMAL", "BOOLEAN": "BOOLEAN",
        "DATE": "DATE", "TIME": "TIME", "TIMESTAMP": "TIMESTAMP",
        "TIMESTAMP WITH TIME ZONE": "TIMESTAMPTZ",
        "VARCHAR": "TEXT", "BLOB": "BYTEA",
    }

    @classmethod
    def _map_type(cls, duckdb_type: str, conn_type: str) -> str:
        """Map a DuckDB type string to the target database SQL type."""
        base = str(duckdb_type).upper().split("(")[0].strip()
        mapped = cls._DUCKDB_TO_SQL.get(base, "TEXT")
        if conn_type == "mysql":
            if mapped == "BOOLEAN":
                return "TINYINT(1)"
            if mapped == "BYTEA":
                return "LONGBLOB"
            if mapped == "TIMESTAMPTZ":
                return "DATETIME"
        elif conn_type == "mssql":
            if mapped == "BOOLEAN":
                return "BIT"
            if mapped == "TEXT":
                return "NVARCHAR(MAX)"
            if mapped == "DOUBLE PRECISION":
                return "FLOAT"
            if mapped == "BYTEA":
                return "VARBINARY(MAX)"
            if mapped == "TIMESTAMPTZ":
                return "DATETIMEOFFSET"
        elif conn_type == "sqlite":
            if base in ("BIGINT", "INTEGER", "SMALLINT", "TINYINT", "HUGEINT"):
                return "INTEGER"
            if base in ("DOUBLE", "FLOAT", "DECIMAL"):
                return "REAL"
            return "TEXT"
        return mapped

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        inputs = ctx.get_inputs(self.params.get("_input_step_ids", []))
        if not inputs:
            raise ValueError("DB Sink has no input data")

        table = self.params.get("table", "output_table")
        mode = self.params.get("mode", "create")  # create | append | truncate
        connection_id = self.params.get("connection_id", "")

        # Apply Mapping-tab settings BEFORE we materialize columns/types
        # so the writer sees the rename + skip applied. No-op when the
        # user hasn't touched the Mapping tab.
        from fpulse.nodes._column_mapping import apply_column_mapping
        source = apply_column_mapping(inputs[0], self.params)

        # If no connection specified, write to DuckDB in-memory
        if not connection_id:
            sink_data = ctx.register_scoped("__sink_data", source)
            if mode == "append":
                ctx.conn.sql(f'INSERT INTO "{table}" SELECT * FROM {sink_data}')
            else:
                ctx.conn.sql(f'CREATE OR REPLACE TABLE "{table}" AS SELECT * FROM {sink_data}')
            return source

        # Real database write
        from fpulse.nodes.db_source import _get_connection_config
        result = _get_connection_config(connection_id)
        if not result:
            raise ValueError(f"DB Sink: connection '{connection_id}' not found")

        config, conn_type = result

        # Extract data from DuckDB relation
        sink_export = ctx.register_scoped("__sink_export", source)
        columns = source.columns
        types = source.types  # DuckDB type objects
        rows = ctx.conn.sql(f"SELECT * FROM {sink_export}").fetchall()

        self._write_real(conn_type, config, table, columns, rows, mode, types)
        return source

    def _write_real(self, conn_type: str, config: dict, table: str,
                    columns: list[str], rows: list[tuple], mode: str,
                    types: list | None = None):
        """Write rows to a real database with proper type inference."""
        host = config.get("host")
        port = config.get("port")
        database = config.get("database")
        user = config.get("user") or config.get("username")
        password = config.get("password")
        schema = config.get("schema")

        if conn_type == "sqlite":
            self._write_sqlite(config, table, columns, rows, mode, types)
        elif conn_type == "postgresql":
            self._write_postgresql(host, port, database, user, password, schema, table, columns, rows, mode, types)
        elif conn_type == "mysql":
            self._write_mysql(host, port, database, user, password, table, columns, rows, mode, types)
        elif conn_type == "mssql":
            self._write_mssql(host, port, database, user, password, table, columns, rows, mode, types)
        else:
            raise ValueError(f"DB Sink: unsupported connection type '{conn_type}'")

    def _write_sqlite(self, config, table, columns, rows, mode, types=None):
        import sqlite3
        db_path = config.get("database") or config.get("file")
        if not db_path:
            raise ValueError("SQLite Sink: no database path configured")
        conn = sqlite3.connect(db_path)
        try:
            self._write_with_conn(conn, table, columns, rows, mode, "?", "sqlite", types)
            conn.commit()
        finally:
            conn.close()

    def _write_postgresql(self, host, port, database, user, password, schema, table, columns, rows, mode, types=None):
        import psycopg2  # type: ignore
        conn = psycopg2.connect(
            host=host, port=port or 5432,
            dbname=database, user=user, password=password, connect_timeout=10,
            options=f"-c search_path={schema}" if schema else None,
        )
        try:
            self._write_with_conn(conn, table, columns, rows, mode, "%s", "postgresql", types)
            conn.commit()
        finally:
            conn.close()

    def _write_mysql(self, host, port, database, user, password, table, columns, rows, mode, types=None):
        import pymysql  # type: ignore
        conn = pymysql.connect(
            host=host, port=int(port or 3306),
            database=database, user=user, password=password, connect_timeout=10,
        )
        try:
            self._write_with_conn(conn, table, columns, rows, mode, "%s", "mysql", types)
            conn.commit()
        finally:
            conn.close()

    def _write_mssql(self, host, port, database, user, password, table, columns, rows, mode, types=None):
        import pyodbc  # type: ignore
        conn_str = (
            f"DRIVER={{ODBC Driver 17 for SQL Server}};"
            f"SERVER={host},{port or 1433};DATABASE={database};"
            f"UID={user};PWD={password};Connection Timeout=10;"
        )
        conn = pyodbc.connect(conn_str)
        try:
            self._write_with_conn(conn, table, columns, rows, mode, "?", "mssql", types)
            conn.commit()
        finally:
            conn.close()

    def _write_with_conn(self, conn, table: str, columns: list[str], rows: list[tuple],
                         mode: str, placeholder: str, conn_type: str = "sqlite",
                         types: list | None = None):
        """Generic write using DB-API 2.0 connection with proper type inference."""
        cur = conn.cursor()
        col_names = ", ".join(f'"{c}"' for c in columns)
        placeholders = ", ".join([placeholder] * len(columns))
        insert_sql = f'INSERT INTO "{table}" ({col_names}) VALUES ({placeholders})'

        # Build column definitions with proper types
        if types and len(types) == len(columns):
            col_defs = ", ".join(
                f'"{c}" {self._map_type(str(t), conn_type)}'
                for c, t in zip(columns, types)
            )
        else:
            col_defs = ", ".join(f'"{c}" TEXT' for c in columns)

        if mode == "create":
            try:
                cur.execute(f'DROP TABLE IF EXISTS "{table}"')
            except Exception:
                pass
            cur.execute(f'CREATE TABLE "{table}" ({col_defs})')
            if rows:
                cur.executemany(insert_sql, rows)

        elif mode == "truncate":
            try:
                cur.execute(f'DELETE FROM "{table}"')
            except Exception:
                pass
            if rows:
                cur.executemany(insert_sql, rows)

        elif mode == "append":
            if rows:
                cur.executemany(insert_sql, rows)

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "table": "output_table", "mode": "create", "connection_id": "",
            "pre_sql": "", "post_sql": "", "schema": "",
            "schema_evolution": "fail",
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "connection_id", "type": "connection_picker", "label": "Connection",
             "tab": "Target",
             "description": "Select a saved connection. Leave empty for DuckDB in-memory."},
            {"name": "table", "type": "text", "label": "Table Name", "required": True,
             "tab": "Target"},
            {"name": "schema", "type": "text", "label": "Schema",
             "tab": "Target",
             "placeholder": "public",
             "description": "Database schema. Leave empty for default."},
            {"name": "mode", "type": "select", "label": "Write Mode",
             "options": ["create", "append", "truncate"], "default": "create",
             "tab": "Target",
             "description": (
                 "create = drop & recreate (CREATE OR REPLACE TABLE). "
                 "append = INSERT into existing rows; preserves history. "
                 "truncate = empty target then INSERT; preserves table DDL."
             )},
            {"name": "schema_evolution", "type": "select", "label": "Schema Evolution",
             "options": ["fail", "ignore_extra", "add_column"], "default": "fail",
             "tab": "Advanced",
             "description": (
                 "Behaviour when the upstream relation has columns the target table doesn't. "
                 "fail = raise (default; safest). "
                 "ignore_extra = drop the extra columns before INSERT (current data preserved, no DDL). "
                 "add_column = ALTER TABLE ADD COLUMN per dialect (risky; not all dialects implement)."
             )},
            {"name": "pre_sql", "type": "sql", "label": "Pre-Write SQL",
             "tab": "Advanced",
             "placeholder": "DELETE FROM staging WHERE batch_date = CURRENT_DATE",
             "description": "SQL to execute before writing data."},
            {"name": "post_sql", "type": "sql", "label": "Post-Write SQL",
             "tab": "Advanced",
             "placeholder": "CALL refresh_view('mv_orders')",
             "description": "SQL to execute after writing data."},
        ]
