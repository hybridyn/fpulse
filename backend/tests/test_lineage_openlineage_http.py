"""Pinned tests for the OpenLineage HTTP exporter (L2.1, 2026-06-08).

Tests WITHOUT real network by injecting a fake transport. Pins the
reliability contract: posts one event per step-run, retries on 5xx /
network errors then succeeds, gives up after max attempts, never
raises, 4xx is non-retryable.
"""
from __future__ import annotations

import sqlite3

import pytest

from fpulse.lineage import LineageStore
from fpulse.lineage.openlineage import OpenLineageHTTPExporter


# Re-use the SQLite fake
class _FakeDB:
    def __init__(self):
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def execute(self, sql, params=()):
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        return cur

    def fetchone(self, sql, params=()):
        row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def fetchall(self, sql, params=()):
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]


class _FakeTransport:
    """Records POST calls + returns a scripted sequence of
    (status, body) tuples. When the script runs out, repeats the last
    one. A status of None means 'raise a connection error'."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, data, headers):
        self.calls.append({"url": url, "data": data, "headers": headers})
        idx = min(len(self.calls) - 1, len(self.responses) - 1)
        resp = self.responses[idx]
        if resp is None or (isinstance(resp, tuple) and resp[0] is None):
            raise ConnectionError("simulated network failure")
        return resp


def _no_sleep(_seconds):
    pass


def _exporter(transport, **kw):
    return OpenLineageHTTPExporter(
        "https://marquez.example/api/v1/lineage",
        transport=transport, sleep=_no_sleep, **kw,
    )


def _event():
    return {"eventType": "COMPLETE", "run": {"runId": "r1"}}


# ── post_event ──────────────────────────────────────────────────────


class TestPostEvent:
    def test_success_2xx_returns_true_one_call(self):
        t = _FakeTransport([(200, "ok")])
        ex = _exporter(t)
        assert ex.post_event(_event()) is True
        assert len(t.calls) == 1

    def test_201_also_success(self):
        t = _FakeTransport([(201, "created")])
        assert _exporter(t).post_event(_event()) is True

    def test_5xx_retries_then_succeeds(self):
        # 500, 500, 200 -> success on the 3rd attempt
        t = _FakeTransport([(500, "err"), (500, "err"), (200, "ok")])
        ex = _exporter(t, max_attempts=3)
        assert ex.post_event(_event()) is True
        assert len(t.calls) == 3

    def test_gives_up_after_max_attempts(self):
        t = _FakeTransport([(500, "err")])
        ex = _exporter(t, max_attempts=3)
        assert ex.post_event(_event()) is False
        assert len(t.calls) == 3  # exactly max_attempts

    def test_429_is_retryable(self):
        t = _FakeTransport([(429, "slow down"), (200, "ok")])
        ex = _exporter(t, max_attempts=2)
        assert ex.post_event(_event()) is True
        assert len(t.calls) == 2

    def test_4xx_is_not_retryable(self):
        # 400 bad request - retry won't fix; give up immediately
        t = _FakeTransport([(400, "bad")])
        ex = _exporter(t, max_attempts=3)
        assert ex.post_event(_event()) is False
        assert len(t.calls) == 1  # no retry

    def test_network_error_retries_then_succeeds(self):
        t = _FakeTransport([None, (200, "ok")])  # raise, then succeed
        ex = _exporter(t, max_attempts=2)
        assert ex.post_event(_event()) is True
        assert len(t.calls) == 2

    def test_network_error_exhausts_returns_false_never_raises(self):
        t = _FakeTransport([None])  # always raises
        ex = _exporter(t, max_attempts=3)
        # Must NOT raise - returns False
        assert ex.post_event(_event()) is False
        assert len(t.calls) == 3

    def test_sends_json_content_type(self):
        t = _FakeTransport([(200, "ok")])
        _exporter(t).post_event(_event())
        assert t.calls[0]["headers"]["Content-Type"] == "application/json"

    def test_custom_headers_merged(self):
        t = _FakeTransport([(200, "ok")])
        ex = OpenLineageHTTPExporter(
            "https://x/api/v1/lineage",
            headers={"Authorization": "Bearer tok"},
            transport=t, sleep=_no_sleep,
        )
        ex.post_event(_event())
        assert t.calls[0]["headers"]["Authorization"] == "Bearer tok"
        assert t.calls[0]["headers"]["Content-Type"] == "application/json"


# ── export_run ──────────────────────────────────────────────────────


class TestExportRun:
    def _store_with_runs(self, n=3, run_id="run-A"):
        store = LineageStore(_FakeDB())
        for i in range(n):
            store.record_step_run(
                workflow_id="wf-1", run_id=run_id, step_id=f"s{i}",
                step_label=f"Step {i}", step_type="db_source",
                columns_out=["id"], rows_out=10,
                started_at=float(i),
            )
        return store

    def test_posts_one_per_step_run(self):
        store = self._store_with_runs(3)
        t = _FakeTransport([(200, "ok")])
        summary = _exporter(t).export_run("run-A", store)
        assert summary == {"posted": 3, "failed": 0}
        assert len(t.calls) == 3

    def test_partial_failure_counts(self):
        # Transport returns 200 then 400 then 200 across the 3 events.
        # (Each event's post_event makes its own call; 400 is non-retryable.)
        store = self._store_with_runs(3)
        t = _FakeTransport([(200, "ok"), (400, "bad"), (200, "ok")])
        summary = _exporter(t).export_run("run-A", store)
        assert summary["posted"] == 2
        assert summary["failed"] == 1

    def test_empty_run_posts_nothing(self):
        store = LineageStore(_FakeDB())
        t = _FakeTransport([(200, "ok")])
        summary = _exporter(t).export_run("no-such-run", store)
        assert summary == {"posted": 0, "failed": 0}
        assert len(t.calls) == 0

    def test_export_run_never_raises_on_store_error(self):
        class _BrokenStore:
            def get_runtime_lineage(self, run_id):
                raise RuntimeError("db down")
        t = _FakeTransport([(200, "ok")])
        # Must not raise
        summary = _exporter(t).export_run("run-A", _BrokenStore())
        assert summary == {"posted": 0, "failed": 0}

    def test_fail_event_type_for_errored_step(self):
        store = LineageStore(_FakeDB())
        store.record_step_run(
            workflow_id="wf-1", run_id="run-A", step_id="s_bad",
            started_at=1.0, completed_at=2.0, error="ConnectionError",
        )
        captured = []

        def _t(url, data, headers):
            import json
            captured.append(json.loads(data.decode("utf-8")))
            return (200, "ok")

        _exporter(_t).export_run("run-A", store)
        assert captured[0]["eventType"] == "FAIL"
