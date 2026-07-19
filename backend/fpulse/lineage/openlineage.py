"""OpenLineage formatter + file exporter (2026-06-08, L2 of lineage-1.2).

Bridges F-Pulse's internal runtime lineage events (recorded in
`lineage_step_runs` via L1) into the OpenLineage 1.0-5 spec format so
they can be ingested by Marquez, DataHub, Airflow's lineage UI, or
any tool that speaks OpenLineage.

# What ships here (foundation)

  * `to_openlineage_run_event()` - converts one step-run row to a
    RunEvent JSON dict (COMPLETE event variant)
  * `OpenLineageJSONLExporter` - append-only file exporter
  * Helpers: `_dataset_uri()`, `_producer()` for the URI conventions
  * Conformance: every emitted event passes shape checks against the
    1.0-5 spec field list

# What's deferred to L2.1 / L2.2 (genuinely separate sessions)

  * HTTP POST to a Marquez / DataHub endpoint (requires network +
    retry handling + auth)
  * START / FAIL / ABORT event variants (need step-start hook + error
    propagation in the executor — load-bearing)
  * Frontend export-config UI
  * Docker-compose worked example with a live Marquez

# Configuration (when L2.1 lands)

The runtime will accept these env vars:

  FPULSE_LINEAGE_EXPORT_FILE       Path to JSONL file for append export
  FPULSE_LINEAGE_OPENLINEAGE_URL   HTTP endpoint for POST export (L2.1)
  FPULSE_LINEAGE_NAMESPACE         Override "f-pulse" job namespace
  FPULSE_LINEAGE_PRODUCER          Override the producer URI
  FPULSE_LINEAGE_REDACT_COLUMNS    "1" => drop column names from
                                    schema facets (privacy mode)

Today: the formatter + JSONL exporter are pure - exported manually
via `OpenLineageJSONLExporter(path).export_run(run_id, store)`.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# OpenLineage spec version we conform to. 1.0-5 is the latest stable
# at time of writing and is accepted by every active Marquez / DataHub.
# Pin in module so the format string flows through every export.
OPENLINEAGE_SCHEMA_URL = "https://openlineage.io/spec/1-0-5/OpenLineage.json"
OPENLINEAGE_PRODUCER_DEFAULT = "https://github.com/hybridyn/fpulse"
OPENLINEAGE_NAMESPACE_DEFAULT = "f-pulse"


_FILE_LOCK = threading.Lock()


def _producer() -> str:
    return os.environ.get("FPULSE_LINEAGE_PRODUCER", OPENLINEAGE_PRODUCER_DEFAULT)


def _namespace() -> str:
    return os.environ.get("FPULSE_LINEAGE_NAMESPACE", OPENLINEAGE_NAMESPACE_DEFAULT)


def _redact_columns() -> bool:
    return os.environ.get("FPULSE_LINEAGE_REDACT_COLUMNS", "0") == "1"


def _epoch_to_iso(epoch_seconds: float | None) -> str:
    """OpenLineage requires eventTime as RFC 3339. Epoch -> UTC ISO."""
    if not epoch_seconds:
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(float(epoch_seconds), tz=timezone.utc).isoformat()


def _dataset_uri(*, kind: str, identifier: str) -> dict[str, str]:
    """Compose an OpenLineage dataset reference. We use a single
    `f-pulse` namespace + the (workflow_id-prefixed) step or source
    identifier as the dataset name. Marquez / DataHub then group
    these into a tree under the namespace."""
    return {
        "namespace": _namespace(),
        "name": identifier or f"unknown-{kind}",
    }


def _schema_facet(columns: list[str]) -> dict[str, Any]:
    """OpenLineage schema facet - a list of column descriptors. The
    formal facet also takes a type per column; we don't track types in
    lineage_step_runs (the design-time graph has them), so we omit."""
    if not columns or _redact_columns():
        return {}
    return {
        "schema": {
            "_producer": _producer(),
            "_schemaURL": "https://openlineage.io/spec/facets/1-0-0/SchemaDatasetFacet.json",
            "fields": [{"name": c} for c in columns],
        }
    }


def to_openlineage_run_event(
    step_run: dict[str, Any],
    *,
    event_type: str = "COMPLETE",
) -> dict[str, Any]:
    """Convert one lineage_step_runs row (as dict) into an
    OpenLineage RunEvent.

    `step_run` is the dict shape `LineageStore.get_runtime_lineage()`
    returns - keys: workflow_id, run_id, step_id, step_label,
    step_type, columns_in, columns_out, rows_in, rows_out,
    started_at, completed_at, error.

    Returns a dict that conforms to the OpenLineage 1.0-5 RunEvent
    schema. Caller serialises to JSON when persisting / POSTing.
    """
    wf_id = step_run.get("workflow_id") or "unknown-workflow"
    run_id = step_run.get("run_id") or "unknown-run"
    step_id = step_run.get("step_id") or "unknown-step"
    step_label = step_run.get("step_label") or step_id

    # eventTime uses the step's completed_at (success) or started_at
    # (start event). Fallback to now() if neither present.
    if event_type == "COMPLETE" and step_run.get("completed_at"):
        ts = _epoch_to_iso(step_run["completed_at"])
    elif step_run.get("started_at"):
        ts = _epoch_to_iso(step_run["started_at"])
    else:
        ts = _epoch_to_iso(None)

    cols_in = step_run.get("columns_in") or []
    cols_out = step_run.get("columns_out") or []

    inputs = []
    outputs = []
    if cols_in:
        inputs.append({
            **_dataset_uri(kind="input", identifier=f"{wf_id}.{step_id}.input"),
            "facets": _schema_facet(cols_in),
        })
    if cols_out:
        outputs.append({
            **_dataset_uri(kind="output", identifier=f"{wf_id}.{step_id}.output"),
            "facets": _schema_facet(cols_out),
        })

    job_facets: dict[str, Any] = {}
    if step_run.get("step_type"):
        # Source-code facet variant - identifies the F-Pulse node type
        # producing the data so consumers know "this came from a
        # csv_source / db_sink / transform / etc."
        job_facets["sourceCode"] = {
            "_producer": _producer(),
            "_schemaURL": "https://openlineage.io/spec/facets/1-0-0/SourceCodeJobFacet.json",
            "language": "fpulse-node",
            "sourceCode": step_run["step_type"],
        }

    run_facets: dict[str, Any] = {}
    # Row-count facet - non-standard but commonly added; many tools
    # render it as "rows produced this run."
    if step_run.get("rows_out", 0) > 0:
        run_facets["fpulse_runtime_stats"] = {
            "_producer": _producer(),
            "_schemaURL": OPENLINEAGE_SCHEMA_URL,
            "rows_in":  step_run.get("rows_in", 0),
            "rows_out": step_run.get("rows_out", 0),
        }

    error = step_run.get("error") or ""
    if event_type in ("FAIL", "ABORT") and error:
        run_facets["errorMessage"] = {
            "_producer": _producer(),
            "_schemaURL": "https://openlineage.io/spec/facets/1-0-0/ErrorMessageRunFacet.json",
            "message": str(error)[:500],
            "programmingLanguage": "python",
        }

    event: dict[str, Any] = {
        "eventType": event_type,
        "eventTime": ts,
        "producer": _producer(),
        "schemaURL": OPENLINEAGE_SCHEMA_URL,
        "run": {"runId": run_id, "facets": run_facets},
        "job": {
            "namespace": _namespace(),
            "name": f"{wf_id}.{step_label}",
            "facets": job_facets,
        },
        "inputs": inputs,
        "outputs": outputs,
    }
    return event


class OpenLineageJSONLExporter:
    """Append-only JSONL exporter. One RunEvent per line; standard
    OpenLineage tooling can `cat *.jsonl` directly into Marquez via
    its bulk-import endpoint."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write_event(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False)
        with _FILE_LOCK:
            with self.path.open("a", encoding="utf-8") as fp:
                fp.write(line + "\n")

    def export_run(self, run_id: str, lineage_store) -> int:
        """Export every step-run for the given run_id. Returns the
        number of events written."""
        out = lineage_store.get_runtime_lineage(run_id)
        n = 0
        for sr in out.get("step_runs", []):
            # Step succeeded if error is empty; otherwise FAIL.
            event_type = "FAIL" if (sr.get("error") or "") else "COMPLETE"
            event = to_openlineage_run_event(sr, event_type=event_type)
            self.write_event(event)
            n += 1
        return n


class OpenLineageHTTPExporter:
    """POST RunEvents to a Marquez / DataHub OpenLineage endpoint
    (L2.1, 2026-06-08).

    Uses the stdlib ``urllib.request`` so we add no new dependency for
    a feature that's off by default. Marquez's ingestion endpoint is
    ``POST <base>/api/v1/lineage``; DataHub's OpenLineage proxy is
    similar. The caller passes the full URL.

    Reliability contract:
      * Retries transient failures (timeouts, connection errors, 5xx)
        with bounded exponential backoff
      * NEVER raises out of ``export_run`` / ``post_event`` - lineage
        is observational; a telemetry endpoint being down must not
        fail a data pipeline. Failures are counted + logged.
      * Returns a summary dict so callers can surface "posted 5,
        failed 1".

    Testability: the constructor accepts an optional ``transport``
    callable ``(url, data_bytes, headers) -> (status_code, body)`` so
    tests inject a fake without real network. Default transport uses
    urllib.

    Deferred (L2.2): the executor calling this automatically on run
    completion when ``FPULSE_LINEAGE_OPENLINEAGE_URL`` is set. That's a
    load-bearing executor hook + needs its own integration test.
    """

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
        max_attempts: int = 3,
        initial_backoff_seconds: float = 0.5,
        backoff_multiplier: float = 2.0,
        transport: Any = None,
        sleep: Any = None,
    ):
        self.url = url
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.timeout = timeout
        self.max_attempts = max(1, int(max_attempts))
        self.initial_backoff_seconds = initial_backoff_seconds
        self.backoff_multiplier = backoff_multiplier
        # Injectable for tests. Default = real urllib transport.
        self._transport = transport or self._urllib_transport
        # Injectable sleep so tests don't actually wait on backoff.
        if sleep is not None:
            self._sleep = sleep
        else:
            import time as _time
            self._sleep = _time.sleep

    # ── Transport ────────────────────────────────────────────────────

    def _urllib_transport(self, url: str, data: bytes,
                            headers: dict[str, str]) -> tuple[int, str]:
        """Default transport: a real HTTP POST via urllib. Returns
        (status_code, body). Raises on network error (caught by the
        retry loop)."""
        import urllib.error
        import urllib.request
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            # HTTPError carries a status code - surface it so the retry
            # loop can decide (5xx retryable, 4xx not).
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            return e.code, body

    @staticmethod
    def _is_retryable_status(status: int) -> bool:
        # 5xx + 429 are worth retrying; 4xx (except 429) are client
        # errors a retry won't fix.
        return status >= 500 or status == 429

    # ── Post one event ───────────────────────────────────────────────

    def post_event(self, event: dict[str, Any]) -> bool:
        """POST a single RunEvent with bounded retry. Returns True on
        success (2xx), False if all attempts failed. Never raises."""
        data = json.dumps(event, ensure_ascii=False).encode("utf-8")
        backoff = self.initial_backoff_seconds
        for attempt in range(1, self.max_attempts + 1):
            try:
                status, _body = self._transport(self.url, data, dict(self.headers))
                if 200 <= status < 300:
                    return True
                if not self._is_retryable_status(status) or attempt >= self.max_attempts:
                    logger.warning(
                        "OpenLineage POST to %s returned %s (attempt %d/%d); giving up",
                        self.url, status, attempt, self.max_attempts,
                    )
                    return False
                # retryable status - fall through to backoff
                logger.info(
                    "OpenLineage POST returned %s (attempt %d/%d); retrying",
                    status, attempt, self.max_attempts,
                )
            except Exception as exc:  # noqa: BLE001 - network errors etc.
                if attempt >= self.max_attempts:
                    logger.warning(
                        "OpenLineage POST to %s failed after %d attempts: %s",
                        self.url, attempt, exc,
                    )
                    return False
                logger.info(
                    "OpenLineage POST error (attempt %d/%d): %s; retrying",
                    attempt, self.max_attempts, exc,
                )
            # Backoff before next attempt
            self._sleep(backoff)
            backoff *= self.backoff_multiplier
        return False

    # ── Export a whole run ───────────────────────────────────────────

    def export_run(self, run_id: str, lineage_store) -> dict[str, int]:
        """POST every step-run for the given run_id. Returns a summary
        ``{"posted": N, "failed": M}``. Never raises."""
        posted = 0
        failed = 0
        try:
            out = lineage_store.get_runtime_lineage(run_id)
            step_runs = out.get("step_runs", [])
        except Exception as exc:  # noqa: BLE001
            logger.warning("OpenLineage export_run could not read lineage for %s: %s",
                           run_id, exc)
            return {"posted": 0, "failed": 0}
        for sr in step_runs:
            event_type = "FAIL" if (sr.get("error") or "") else "COMPLETE"
            event = to_openlineage_run_event(sr, event_type=event_type)
            if self.post_event(event):
                posted += 1
            else:
                failed += 1
        return {"posted": posted, "failed": failed}
