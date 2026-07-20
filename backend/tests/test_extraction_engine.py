"""End-to-end engine tests against an in-process HTTP server.

This is the proof Phase 2 actually works: a fake API mimics the
'slow + fanout + nested + paginated' shape, the engine drives the
ENDPOINT_CENTRAL_INVENTORY profile against it, and we assert the
projected JSONL output matches expectations.

Two-phase test:
  - List endpoint at /api/1.4/inventory/computers paginates with
    page/pagelimit and returns {computers: [{resource_id, ...}]}
  - Per-resource detail at /api/1.4/inventory/computers/{id} returns
    a deeply nested record (hardware → memory.total_gb, network →
    interfaces[*], etc.)
  - Engine extracts all resources, projects them through the schema
    mapper, streams JSONL.

Single-phase test:
  - Pagination with no enrichment — list returns full records.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from fpulse.extraction import (
    AuthProfile,
    ExtractionEngine,
    PaginationProfile,
    SchemaProfile,
    SourceProfile,
)
from fpulse.extraction.profile import (
    ConcurrencyProfile,
    EnrichmentProfile,
    RateLimitProfile,
)


# Inline fixture replacing the vendor-named sample profile. Mirrors
# the canonical 'slow + fanout + nested' shape so the engine test
# exercises every feature without naming a specific vendor.
def _slow_fanout_profile() -> SourceProfile:
    return SourceProfile(
        name="slow_fanout_test",
        latency_class="very_slow",
        expected_volume="large",
        freshness_interval_seconds=6 * 3600,
        auth=AuthProfile(type="api_token", header="Authorization", prefix=""),
        pagination=PaginationProfile(
            mode="offset", items_path=["computers"],
            page_size=100, offset_param="page", limit_param="pagelimit",
        ),
        enrichment=EnrichmentProfile(
            list_url="/api/1.4/inventory/computers",
            list_id_field="resource_id",
            fetch_url="/api/1.4/inventory/computers/{id}",
            batch_size=1,
        ),
        rate_limit=RateLimitProfile(rps=200.0, burst=50, respect_header="Retry-After"),
        concurrency=ConcurrencyProfile(mode="aimd", initial=4, max=12),
        schema=SchemaProfile(
            field_paths={
                "id":               "resource_id",
                "name":             "computer_name",
                "domain":           "domain_name|default=workgroup",
                "os":               "os_info.platform_name",
                "os_version":       "os_info.version",
                "os_build":         "os_info.build_number",
                "user":             "logged_on_user.user_principal_name",
                "user_email":       "logged_on_user.email",
                "manufacturer":     "hardware.manufacturer",
                "model":            "hardware.model",
                "serial":           "hardware.serial_number",
                "memory_gb":        "hardware.memory.total_gb",
                "cpu_cores":        "hardware.cpu.core_count",
                "primary_ip":       "network.interfaces[0].ip_address",
                "all_ips":          "network.interfaces[*].ip_address",
                "compliance_state": "compliance.state",
                "patch_count":      "patches.installed_count",
                "last_scan":        "scan_history[0].timestamp",
                "agent_version":    "agent.version",
            },
            coercions={
                "memory_gb":   "float",
                "cpu_cores":   "int",
                "patch_count": "int",
                "last_scan":   "iso_datetime",
            },
        ),
    )


# ── Mock server ──────────────────────────────────────────────────────

class _MockHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def _json(self, code, body):
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        url = urlparse(self.path)
        # ── List (paginated) ──
        if url.path == "/api/1.4/inventory/computers":
            self.server.list_calls += 1  # type: ignore[attr-defined]
            params = parse_qs(url.query)
            # `page` here is sent by paginate_offset_limit as an offset
            # (item index), not a 1-indexed page number.
            offset = int((params.get("page") or ["0"])[0])
            limit = int((params.get("pagelimit") or ["100"])[0])
            all_records = self.server.list_records  # type: ignore[attr-defined]
            slice_ = all_records[offset:offset + limit]
            return self._json(200, {"computers": slice_})

        # ── Per-resource detail ──
        if url.path.startswith("/api/1.4/inventory/computers/"):
            rid = url.path.rsplit("/", 1)[-1]
            self.server.detail_calls += 1  # type: ignore[attr-defined]
            record = self.server.detail_for_id(rid)  # type: ignore[attr-defined]
            if record is None:
                return self._json(404, {"error": "not found"})
            return self._json(200, record)

        # ── Single-phase list ──
        if url.path == "/v1/widgets":
            return self._json(200, {"items": self.server.widget_records})  # type: ignore[attr-defined]

        return self._json(404, {"error": "no route"})


@pytest.fixture
def mock_api():
    server = HTTPServer(("127.0.0.1", 0), _MockHandler)
    server.list_calls = 0       # type: ignore[attr-defined]
    server.detail_calls = 0     # type: ignore[attr-defined]
    server.list_records = []    # type: ignore[attr-defined]
    server.widget_records = []  # type: ignore[attr-defined]

    def _detail_for_id(rid: str):
        # Synthesize a deeply nested detail record per ID.
        try:
            n = int(rid.replace("r-", ""))
        except ValueError:
            return None
        return {
            "resource_id": rid,
            "computer_name": f"host-{n:03d}",
            "domain_name": "labs.example.com",
            "os_info": {
                "platform_name": "Windows" if n % 2 == 0 else "Linux",
                "version": "11" if n % 2 == 0 else "Ubuntu 22.04",
                "build_number": str(22000 + n),
            },
            "logged_on_user": {
                "user_principal_name": f"user-{n}@example.com",
                "email": f"user{n}@example.com",
            },
            "hardware": {
                "manufacturer": "Dell",
                "model": "Latitude 7440",
                "serial_number": f"SER-{n:05d}",
                "memory": {"total_gb": str(8 + (n % 4) * 8)},
                "cpu": {"core_count": str(4 + (n % 8))},
            },
            "network": {
                "interfaces": [
                    {"ip_address": f"10.0.{n // 256}.{n % 256}"},
                    {"ip_address": f"192.168.1.{n % 256}"},
                ]
            },
            "compliance": {"state": "compliant" if n % 3 != 0 else "non_compliant"},
            "patches": {"installed_count": str(50 + n)},
            "scan_history": [
                {"timestamp": "2026-05-09T14:30:00Z"},
                {"timestamp": "2026-05-08T14:30:00Z"},
            ],
            "agent": {"version": "11.0.5"},
        }

    server.detail_for_id = _detail_for_id  # type: ignore[attr-defined]

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield server, base
    server.shutdown()


# ── Two-phase end-to-end ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_two_phase_extraction_full_lifecycle(mock_api, tmp_path):
    server, base = mock_api
    # Seed 25 list records — exercises pagination boundaries (page_size=100).
    server.list_records = [{"resource_id": f"r-{i}"} for i in range(25)]

    output = str(tmp_path / "endpoints.jsonl")

    events: list[dict] = []
    engine = ExtractionEngine(
        profile=_slow_fanout_profile(),
        config={"api_token": "fake-token-doesnt-matter"},
        base_url=base,
        output_path=output,
        on_event=events.append,
    )
    result = await engine.run()

    # Engine reports correct numbers.
    assert result.total_listed == 25
    assert result.total_extracted == 25
    assert result.total_failed == 0
    # Server saw the right number of calls.
    assert server.list_calls >= 1
    assert server.detail_calls == 25

    # Lifecycle events fired in order. The engine now emits a
    # 'reachability_probe' pre-event before 'started' so operators can see
    # endpoint health on a stuck-on-DNS run. Assert that 'started' is
    # present and any pre-events are lifecycle probes (not real run state).
    kinds = [e["kind"] for e in events]
    assert "started" in kinds
    started_idx = kinds.index("started")
    assert all(k in ("reachability_probe", "auth_probe") for k in kinds[:started_idx])
    assert "list_phase_start" in kinds
    assert "list_phase_done" in kinds
    assert "enrichment_phase_start" in kinds
    assert "enrichment_phase_done" in kinds
    # The engine emits 'completed' followed by a 'manifest_written'
    # post-event (freshness manifest persisted to disk). Both should
    # appear; assert by membership + relative ordering instead of
    # head/tail equality so future additions don't trip this.
    assert "completed" in kinds
    completed_idx = kinds.index("completed")
    assert all(k in ("manifest_written",) for k in kinds[completed_idx + 1:])

    # Output file is valid JSONL with one row per resource and the
    # SchemaMapper actually flattened the nested response.
    rows = [json.loads(line) for line in open(output)]
    assert len(rows) == 25
    # Every row has the expected projected columns.
    sample = rows[0]
    expected_columns = {
        "id", "name", "domain", "os", "os_version", "os_build",
        "user", "user_email", "manufacturer", "model", "serial",
        "memory_gb", "cpu_cores", "primary_ip", "all_ips",
        "compliance_state", "patch_count", "last_scan", "agent_version",
    }
    assert expected_columns.issubset(sample.keys())
    # Type coercions actually applied.
    assert isinstance(sample["memory_gb"], float)
    assert isinstance(sample["cpu_cores"], int)
    assert isinstance(sample["patch_count"], int)
    # Wildcard array was flattened into a list.
    assert isinstance(sample["all_ips"], list)
    assert len(sample["all_ips"]) == 2
    # iso_datetime was projected as a string in JSON (datetime → str via default=str).
    assert "2026-05-09" in sample["last_scan"]


# ── Single-phase end-to-end ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_single_phase_extraction(mock_api, tmp_path):
    server, base = mock_api
    server.widget_records = [
        {"id": i, "spec": {"size": "L"}, "tags": [{"name": "alpha"}, {"name": "beta"}]}
        for i in range(5)
    ]

    profile = SourceProfile(
        name="test_widgets",
        auth=AuthProfile(type="none"),
        pagination=PaginationProfile(mode="none", items_path=["items"]),
        schema=SchemaProfile(
            field_paths={
                "id":         "id",
                "size":       "spec.size",
                "first_tag":  "tags[0].name",
                "all_tags":   "tags[*].name",
            },
            coercions={"id": "int"},
        ),
    )

    output = str(tmp_path / "widgets.jsonl")
    engine = ExtractionEngine(
        profile=profile,
        config={},
        base_url=base,
        list_url="/v1/widgets",
        output_path=output,
    )
    result = await engine.run()

    assert result.total_extracted == 5
    rows = [json.loads(line) for line in open(output)]
    assert rows[0] == {
        "id": 0, "size": "L",
        "first_tag": "alpha", "all_tags": ["alpha", "beta"],
    }


# ── Validation errors ───────────────────────────────────────────────

def test_engine_rejects_missing_list_url_when_no_enrichment():
    profile = SourceProfile(
        name="no_url",
        auth=AuthProfile(type="none"),
        pagination=PaginationProfile(mode="none"),
        schema=SchemaProfile(field_paths={"id": "id"}),
    )
    with pytest.raises(ValueError, match="list_url required"):
        ExtractionEngine(
            profile=profile, config={}, base_url="http://x",
            output_path="/tmp/out.jsonl",
        )


def test_engine_falls_back_to_enrichment_list_url():
    """When enrichment is configured, the engine uses enrichment.list_url
    even if the constructor doesn't pass list_url explicitly."""
    profile = SourceProfile(
        name="auto_list_url",
        auth=AuthProfile(type="none"),
        pagination=PaginationProfile(mode="none"),
        schema=SchemaProfile(field_paths={"id": "id"}),
        enrichment=EnrichmentProfile(
            list_url="/auto/list",
            list_id_field="id",
            fetch_url="/auto/{id}",
        ),
    )
    eng = ExtractionEngine(
        profile=profile, config={}, base_url="http://x",
        output_path="/tmp/out.jsonl",
    )
    assert eng.list_url == "/auto/list"
