"""Extraction Engine — reads a SourceProfile and runs the full
list → enrich → project → stream pipeline.

Composes the substrate that's already shipped:
  - paginator.py                — list-phase walking (4 patterns)
  - oauth_session.py            — auth refresh + 401 retry
  - fanout_runner.py            — enrichment-phase concurrency / retry
  - schema_mapper.py            — JSON path projection
  - credentials resolution      — Vault > legacy > config

Two flow shapes:

  1. Single-phase (no enrichment):
        list URL → paginate → for each record:
          mapper.flatten(record) → JSONL.append(...)

  2. Two-phase (enrichment present):
        list URL → paginate → collect IDs
        FanoutRunner(fetch_ids, fetch_one):
          fetch_one wraps the per-resource GET + mapper.flatten
        AIMD + token bucket + checkpoint as configured by the profile

Events flow through an `on_event` callback; in Phase 4 this becomes
a proper bus subscribed by the operator UI. For now it's a dict-shape
that's easy to log or assert against in tests.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from fpulse.connections.fanout_runner import FanoutRunner, raise_if_rate_limited
from fpulse.connections.paginator import (
    paginate_cursor_in_body,
    paginate_link_header,
    paginate_offset_limit,
    paginate_page_token,
)
from fpulse.connections.runtime import check_reachability
from fpulse.extraction.events import (
    EventBus,
    ExtractionEvent,
    get_bus,
    make_run_id,
)
from fpulse.extraction.freshness import FreshnessBlocked, FreshnessGate
from fpulse.extraction.manifest import (
    RunManifest,
    schema_fingerprint_from_profile,
)
from fpulse.extraction.profile import SourceProfile
from fpulse.extraction.schema_mapper import SchemaMapper
from fpulse.extraction.session import build_session
from fpulse.extraction.staging import (
    JsonlStagingWriter,
    make_staging_writer,
    output_size_bytes,
)

logger = logging.getLogger(__name__)


# ── Result shape ────────────────────────────────────────────────────

@dataclass
class ExtractionResult:
    profile_name: str
    total_listed: int
    total_extracted: int
    total_failed: int
    total_skipped_resumed: int
    duration_s: float
    output_path: str
    failed_path: str
    final_concurrency: int


# ── Engine ──────────────────────────────────────────────────────────

@dataclass
class ExtractionEngine:
    """Drives a SourceProfile end-to-end.

    Required:
      profile      — declarative descriptor
      config       — already-resolved credentials (Vault > config)
      base_url     — environment-specific root (e.g. https://my-server.local)
      output_path  — JSONL path for projected records

    Optional:
      list_url     — overrides profile.enrichment.list_url; required when
                     no enrichment is configured (single-phase mode)
      writeback    — callback for OAuth refresh persistence
      on_event     — callback receiving lifecycle events (started,
                     list_done, progress, completed, failed)
    """

    profile: SourceProfile
    config: dict[str, Any]
    base_url: str
    output_path: str
    list_url: str | None = None
    writeback: Callable[[dict], None] | None = None
    on_event: Callable[[dict], None] | None = None
    bus: EventBus | None = None         # injected for tests; defaults to module singleton
    run_id: str | None = None           # auto-generated when None
    output_format: str = "jsonl"        # "jsonl" | "parquet"
    manifest_dir: str | None = None     # defaults to dir(output_path)
    force: bool = False                 # bypass freshness gate
    skip_reachability_check: bool = False  # for tests / known-good envs
    reachability_timeout_s: float = 5.0

    def __post_init__(self) -> None:
        self._mapper = SchemaMapper(self.profile.schema)
        self._session = build_session(self.profile.auth, self.config,
                                       writeback=self.writeback)
        # Resolve list URL precedence: explicit arg > profile.enrichment.list_url
        if not self.list_url and self.profile.enrichment:
            self.list_url = self.profile.enrichment.list_url
        if not self.list_url:
            raise ValueError(
                "list_url required when profile has no enrichment configured"
            )
        if self.run_id is None:
            self.run_id = make_run_id()
        if self.bus is None:
            self.bus = get_bus()
        if self.manifest_dir is None:
            self.manifest_dir = os.path.dirname(self.output_path) or "."
        if self.output_format not in ("jsonl", "parquet"):
            raise ValueError(f"output_format must be 'jsonl' or 'parquet', got {self.output_format!r}")

    # ── Event helpers ────────────────────────────────────────────────

    def _emit(self, kind: str, **payload: Any) -> None:
        ts = time.time()
        # Publish on the bus for fan-out subscribers (state aggregator,
        # SSE endpoint, UI). Also call the legacy on_event callback so
        # existing tests / callers keep working unchanged.
        evt = ExtractionEvent(
            run_id=self.run_id, profile=self.profile.name,  # type: ignore[arg-type]
            kind=kind, ts=ts, payload=dict(payload),
        )
        try:
            self.bus.publish(evt)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            logger.exception("event bus publish failed")
        logger.info("extraction.%s %s", kind, payload)
        if self.on_event:
            try:
                self.on_event(evt.to_dict())
            except Exception:  # noqa: BLE001
                logger.warning("on_event handler raised", exc_info=True)

    # ── URL building ─────────────────────────────────────────────────

    def _absolute(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return self.base_url.rstrip("/") + "/" + path.lstrip("/")

    # ── List phase ───────────────────────────────────────────────────

    def _list_iter(self):
        """Returns a sync iterator over list-phase records.

        Picks the right paginator based on profile.pagination.mode.
        Wrapped in asyncio.to_thread by the caller so the event loop
        stays free during blocking HTTP calls.
        """
        url = self._absolute(self.list_url)  # type: ignore[arg-type]
        pag = self.profile.pagination

        if pag.mode == "none":
            r = self._session.get(url, timeout=30)
            r.raise_for_status()
            body = r.json()
            for key in pag.items_path:
                body = body.get(key, []) if isinstance(body, dict) else []
            return iter(body or [])

        if pag.mode == "link_header":
            return paginate_link_header(
                self._session, url,
                extract_items=lambda body: _walk(body, pag.items_path) or [],
            )

        if pag.mode == "cursor":
            if not pag.cursor_path:
                raise ValueError("cursor pagination requires pagination.cursor_path")
            return paginate_cursor_in_body(
                self._session, url,
                cursor_path=pag.cursor_path,
                cursor_param=pag.cursor_param,
                items_path=pag.items_path,
            )

        if pag.mode == "offset":
            return paginate_offset_limit(
                self._session, url,
                items_path=pag.items_path,
                page_size=pag.page_size,
                offset_param=pag.offset_param,
                limit_param=pag.limit_param,
                has_more_path=pag.has_more_path,
            )

        if pag.mode == "page_token":
            if not pag.token_path:
                raise ValueError("page_token pagination requires pagination.token_path")
            return paginate_page_token(
                self._session, url,
                token_path=pag.token_path,
                items_path=pag.items_path,
                token_param=pag.token_param,
            )

        raise ValueError(f"Unsupported pagination.mode: {pag.mode!r}")

    # ── Enrichment phase ─────────────────────────────────────────────

    async def _build_fetch_one(self):
        """Returns an async callable: rid → projected dict.

        Builds the per-resource URL from profile.enrichment.fetch_url,
        runs the schema mapper before returning so the runner only
        ever writes flat records to disk.
        """
        if not self.profile.enrichment:
            raise RuntimeError("_build_fetch_one called without enrichment")
        template = self.profile.enrichment.fetch_url
        loop = asyncio.get_running_loop()

        def _sync_fetch(rid: str) -> dict:
            url = self._absolute(template.format(id=rid))
            r = self._session.get(url, timeout=60)
            raise_if_rate_limited(r)  # 429/503 → typed signal for AIMD
            r.raise_for_status()
            record = r.json()
            return self._mapper.flatten(record)

        async def fetch_one(rid: str) -> dict:
            return await loop.run_in_executor(None, _sync_fetch, rid)

        return fetch_one

    async def _collect_ids(self) -> list[str]:
        """Run the list phase and pull out IDs for the enrichment phase."""
        if not self.profile.enrichment:
            raise RuntimeError("_collect_ids called without enrichment")
        id_field = self.profile.enrichment.list_id_field
        loop = asyncio.get_running_loop()

        def _gather() -> list[str]:
            ids: list[str] = []
            for record in self._list_iter():
                value = _get_nested(record, id_field)
                if value is not None:
                    ids.append(str(value))
            return ids

        return await loop.run_in_executor(None, _gather)

    # ── Single-phase path ────────────────────────────────────────────

    async def _run_single_phase(self) -> ExtractionResult:
        os.makedirs(os.path.dirname(self.output_path) or ".", exist_ok=True)
        loop = asyncio.get_running_loop()
        listed = 0
        extracted = 0
        start = time.monotonic()
        writer = make_staging_writer(self.output_format, self.output_path)

        def _drain() -> tuple[int, int]:
            nonlocal listed, extracted
            try:
                for record in self._list_iter():
                    listed += 1
                    flat = self._mapper.flatten(record)
                    writer.write(flat)
                    extracted += 1
                    if extracted % 100 == 0:
                        self._emit("progress", phase="single", extracted=extracted)
            finally:
                writer.close()
            return listed, extracted

        await loop.run_in_executor(None, _drain)

        return ExtractionResult(
            profile_name=self.profile.name,
            total_listed=listed,
            total_extracted=extracted,
            total_failed=0,
            total_skipped_resumed=0,
            duration_s=round(time.monotonic() - start, 2),
            output_path=self.output_path,
            failed_path="",
            final_concurrency=1,  # single-phase is serial by definition
        )

    # ── Two-phase path ───────────────────────────────────────────────

    async def _run_two_phase(self) -> ExtractionResult:
        start = time.monotonic()
        self._emit("list_phase_start")
        ids = await self._collect_ids()
        self._emit("list_phase_done", id_count=len(ids))

        c = self.profile.concurrency
        rl = self.profile.rate_limit
        fetch_one = await self._build_fetch_one()

        runner = FanoutRunner(
            fetch_ids=lambda: _aiter_list(ids),
            fetch_one=fetch_one,
            output_path=self.output_path,
            initial_concurrency=c.initial,
            max_concurrency=c.max,
            min_concurrency=c.min,
            rate_limit_rps=rl.rps,
            rate_limit_burst=rl.burst,
            success_window=c.success_window,
        )
        self._emit("enrichment_phase_start", target_count=len(ids),
                    initial_concurrency=c.initial, max_concurrency=c.max)
        result = await runner.run()
        self._emit("enrichment_phase_done",
                    succeeded=result.succeeded, failed=result.failed,
                    final_concurrency=result.final_concurrency)

        return ExtractionResult(
            profile_name=self.profile.name,
            total_listed=len(ids),
            total_extracted=result.succeeded,
            total_failed=result.failed,
            total_skipped_resumed=result.skipped_resumed,
            duration_s=round(time.monotonic() - start, 2),
            output_path=result.output_path,
            failed_path=result.failed_path,
            final_concurrency=result.final_concurrency,
        )

    # ── Public entry ─────────────────────────────────────────────────

    async def run(self) -> ExtractionResult:
        # ── Freshness gate (Phase 3) ─────────────────────────────────
        gate = FreshnessGate(self.manifest_dir)  # type: ignore[arg-type]
        decision = gate.check(self.profile, force=self.force)
        if not decision.allowed:
            self._emit("freshness_blocked", reason=decision.reason,
                        next_allowed_at=decision.next_allowed_at)
            raise FreshnessBlocked(decision)
        if decision.forced:
            self._emit("freshness_overridden", reason=decision.reason)

        # ── Reachability preflight (local-network gap fix) ──────────
        # Cheaper to fail in seconds via a TCP probe than to burn 5+
        # minutes of AIMD failure budget discovering the source is
        # unreachable. Skippable for tests / known-good environments.
        if not self.skip_reachability_check:
            probe = check_reachability(self.base_url,
                                          timeout_s=self.reachability_timeout_s)
            self._emit("reachability_probe",
                        target=probe.target, reachable=probe.reachable,
                        detail=probe.detail, latency_ms=probe.latency_ms)
            if not probe.reachable:
                err = (f"source unreachable at {probe.target}: {probe.detail}. "
                        f"Set skip_reachability_check=True to bypass.")
                self._emit("failed", error=err)
                raise RuntimeError(err)

        started_at = time.time()
        self._emit("started", base_url=self.base_url, list_url=self.list_url,
                    has_enrichment=bool(self.profile.enrichment))
        result: ExtractionResult | None = None
        error: str | None = None
        try:
            if self.profile.enrichment:
                result = await self._run_two_phase()
            else:
                result = await self._run_single_phase()

            # Two-phase always emits JSONL via FanoutRunner. If the
            # caller asked for Parquet, convert at end so the staging
            # output matches the requested format. Update the result
            # so the manifest stamps the .parquet path, not the
            # already-removed .jsonl.
            if self.profile.enrichment and self.output_format == "parquet":
                result.output_path = self._convert_jsonl_to_parquet(result.output_path)

            self._emit("completed",
                        listed=result.total_listed,
                        extracted=result.total_extracted,
                        failed=result.total_failed,
                        duration_s=result.duration_s)
            return result
        except FreshnessBlocked:
            raise
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            self._emit("failed", error=error)
            raise
        finally:
            # ── Manifest write (Phase 3) ──────────────────────────────
            # Always emit a manifest, even on failure, so the freshness
            # gate has visibility into the attempt and the operator UI
            # has an audit trail.
            self._write_manifest(started_at, result, error)

    # ── Phase 3 helpers ──────────────────────────────────────────────

    def _write_manifest(self, started_at: float,
                          result: ExtractionResult | None,
                          error: str | None) -> None:
        completed_at = time.time() if (result is not None or error is not None) else None
        row_counts: dict[str, int] = {}
        output_path = self.output_path
        failed_path = ""
        if result is not None:
            row_counts = {
                "listed": result.total_listed,
                "extracted": result.total_extracted,
                "failed": result.total_failed,
                "skipped_resumed": result.total_skipped_resumed,
            }
            output_path = result.output_path
            failed_path = result.failed_path
        manifest = RunManifest(
            run_id=self.run_id,  # type: ignore[arg-type]
            profile_name=self.profile.name,
            started_at=started_at,
            completed_at=completed_at,
            duration_s=(round(completed_at - started_at, 2)
                        if completed_at else None),
            row_counts=row_counts,
            schema_fingerprint=schema_fingerprint_from_profile(self.profile),
            output_format=self.output_format,
            output_path=output_path,
            failed_path=failed_path,
            output_size_bytes=output_size_bytes(output_path),
            error=error,
        )
        try:
            saved = manifest.save(self.manifest_dir)  # type: ignore[arg-type]
            self._emit("manifest_written", path=saved)
        except Exception:  # noqa: BLE001 — never fail a run on manifest issues
            logger.exception("manifest write failed")

    def _convert_jsonl_to_parquet(self, jsonl_path: str) -> str:
        """One-shot post-processing convert when output_format=parquet
        but the two-phase fanout runner emitted JSONL. Returns the
        parquet path. Failures keep the JSONL — it's never removed
        before the Parquet is fully flushed."""
        import json as _json
        parquet_path = jsonl_path.rsplit(".", 1)[0] + ".parquet"
        with make_staging_writer("parquet", parquet_path) as pw:
            with open(jsonl_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        pw.write(_json.loads(line))
                    except _json.JSONDecodeError:
                        continue
        try:
            os.remove(jsonl_path)
        except OSError:
            pass
        self._emit("parquet_converted",
                    parquet_path=parquet_path,
                    bytes=output_size_bytes(parquet_path))
        return parquet_path


# ── Helpers ──────────────────────────────────────────────────────────

def _walk(body: Any, path: list[str]) -> Any:
    cursor = body
    for key in path:
        cursor = cursor.get(key, []) if isinstance(cursor, dict) else []
    return cursor


def _get_nested(record: Any, dotted: str) -> Any:
    """Lightweight nested-key getter used by enrichment ID extraction.
    Doesn't need the full SchemaMapper because list-phase IDs are
    never wildcard-expanded — they're a single dotted path."""
    cursor = record
    for key in dotted.split("."):
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(key)
        if cursor is None:
            return None
    return cursor


async def _aiter_list(items: list[str]):
    return items
