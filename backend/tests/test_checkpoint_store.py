"""Unit tests for CheckpointStore — Sprint 1 / Gate 1.

Covers:
  * Schema migration v23 creates the table on a fresh DB.
  * upsert / mark_success / mark_failed / mark_skipped / mark_in_progress.
  * get / get_run / latest_failed_run / successful_step_ids.
  * delete_run / delete_workflow.
  * Eviction (TTL + orphaned in_progress).
  * Defensive behavior when db is not wired (no exceptions).
"""

from __future__ import annotations

import time

import pytest

from fpulse.engine.checkpoint_store import Checkpoint, CheckpointStore


@pytest.fixture
def checkpoint_store(_fpulse_test_db):
    return CheckpointStore(db=_fpulse_test_db)


# ── Schema migration v23 ─────────────────────────────────────────────


class TestSchemaMigrationV23:
    def test_pipeline_checkpoints_table_exists(self, _fpulse_test_db):
        # Database() in conftest runs migrations to current SCHEMA_VERSION.
        row = _fpulse_test_db.fetchone(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='pipeline_checkpoints'"
        )
        assert row is not None, "pipeline_checkpoints table missing — migration v23 did not run"

    def test_status_check_constraint(self, _fpulse_test_db):
        """Inserting an invalid status string must raise."""
        with pytest.raises(Exception):
            _fpulse_test_db.execute(
                "INSERT INTO pipeline_checkpoints "
                "(workflow_id, run_id, step_id, status) VALUES (?, ?, ?, ?)",
                ("wf", "run", "s1", "garbage"),
            )
            _fpulse_test_db.commit()

    def test_indexes_present(self, _fpulse_test_db):
        rows = _fpulse_test_db.fetchall(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='pipeline_checkpoints'"
        )
        names = {r["name"] for r in rows}
        assert "idx_checkpoints_workflow" in names
        assert "idx_checkpoints_status" in names


# ── Upsert + read ────────────────────────────────────────────────────


class TestUpsert:
    def test_mark_success_persists(self, checkpoint_store):
        checkpoint_store.mark_success(
            workflow_id="wf1", run_id="run1", step_id="s1",
            rows_out=42, duration_ms=120, output_ref="cache/wf1/s1.parquet",
        )
        cp = checkpoint_store.get("run1", "s1")
        assert cp is not None
        assert cp.status == "success"
        assert cp.rows_out == 42
        assert cp.duration_ms == 120
        assert cp.output_ref == "cache/wf1/s1.parquet"
        assert cp.completed_at is not None  # ISO timestamp stamped

    def test_mark_failed_persists_error(self, checkpoint_store):
        checkpoint_store.mark_failed(
            workflow_id="wf1", run_id="run1", step_id="s2",
            error_summary="timeout after 30s",
        )
        cp = checkpoint_store.get("run1", "s2")
        assert cp.status == "failed"
        assert cp.error_summary == "timeout after 30s"

    def test_mark_in_progress_then_success_overwrites(self, checkpoint_store):
        checkpoint_store.mark_in_progress("wf1", "run1", "s1")
        assert checkpoint_store.get("run1", "s1").status == "in_progress"
        checkpoint_store.mark_success("wf1", "run1", "s1", rows_out=10)
        cp = checkpoint_store.get("run1", "s1")
        assert cp.status == "success"
        assert cp.rows_out == 10

    def test_mark_skipped(self, checkpoint_store):
        checkpoint_store.mark_skipped("wf1", "run1", "s3", reason="upstream failed")
        cp = checkpoint_store.get("run1", "s3")
        assert cp.status == "skipped"
        assert cp.error_summary == "upstream failed"

    def test_error_summary_truncated_at_4000_chars(self, checkpoint_store):
        big = "x" * 5000
        checkpoint_store.mark_failed("wf1", "run1", "s1", error_summary=big)
        cp = checkpoint_store.get("run1", "s1")
        assert cp.error_summary is not None
        assert len(cp.error_summary) <= 4000


class TestRead:
    def test_get_run_returns_all_in_order(self, checkpoint_store):
        checkpoint_store.mark_success("wf1", "run1", "s1", rows_out=1)
        time.sleep(0.01)
        checkpoint_store.mark_success("wf1", "run1", "s2", rows_out=2)
        time.sleep(0.01)
        checkpoint_store.mark_failed("wf1", "run1", "s3", error_summary="bang")
        cps = checkpoint_store.get_run("run1")
        assert [c.step_id for c in cps] == ["s1", "s2", "s3"]
        assert [c.status for c in cps] == ["success", "success", "failed"]

    def test_get_nonexistent_returns_none(self, checkpoint_store):
        assert checkpoint_store.get("missing", "nope") is None

    def test_successful_step_ids_filters_out_failures(self, checkpoint_store):
        checkpoint_store.mark_success("wf1", "run1", "s1")
        checkpoint_store.mark_success("wf1", "run1", "s2")
        checkpoint_store.mark_failed("wf1", "run1", "s3", error_summary="bad")
        ids = checkpoint_store.successful_step_ids("run1")
        assert ids == {"s1", "s2"}

    def test_latest_failed_run_returns_most_recent(self, checkpoint_store):
        checkpoint_store.mark_failed("wf1", "run-old", "s1", error_summary="old")
        time.sleep(0.02)
        checkpoint_store.mark_failed("wf1", "run-new", "s1", error_summary="new")
        assert checkpoint_store.latest_failed_run("wf1") == "run-new"

    def test_latest_failed_run_none_when_no_failures(self, checkpoint_store):
        checkpoint_store.mark_success("wf1", "run1", "s1")
        assert checkpoint_store.latest_failed_run("wf1") is None

    def test_latest_failed_run_filters_by_workflow(self, checkpoint_store):
        checkpoint_store.mark_failed("wf-other", "run-other", "s1", error_summary="x")
        assert checkpoint_store.latest_failed_run("wf1") is None
        assert checkpoint_store.latest_failed_run("wf-other") == "run-other"


# ── Eviction ─────────────────────────────────────────────────────────


class TestEviction:
    def test_delete_run_removes_only_that_run(self, checkpoint_store):
        checkpoint_store.mark_success("wf1", "runA", "s1")
        checkpoint_store.mark_success("wf1", "runA", "s2")
        checkpoint_store.mark_success("wf1", "runB", "s1")
        deleted = checkpoint_store.delete_run("runA")
        assert deleted == 2
        assert checkpoint_store.get_run("runA") == []
        assert len(checkpoint_store.get_run("runB")) == 1

    def test_delete_workflow_removes_all_runs(self, checkpoint_store):
        checkpoint_store.mark_success("wf1", "runA", "s1")
        checkpoint_store.mark_success("wf1", "runB", "s1")
        checkpoint_store.mark_success("wf-other", "runC", "s1")
        deleted = checkpoint_store.delete_workflow("wf1")
        assert deleted == 2
        assert len(checkpoint_store.get_run("runC")) == 1

    def test_evict_older_than_zero_days_clears_completed(self, checkpoint_store):
        checkpoint_store.mark_success("wf1", "run1", "s1")
        # Tiny sleep so the cutoff computed inside evict_older_than is
        # strictly greater than the row's completed_at. Without this the
        # two timestamps can land in the same microsecond on a fast box
        # and the strict `<` comparison would skip the row (correct
        # behavior, just a flaky test setup).
        time.sleep(0.005)
        evicted = checkpoint_store.evict_older_than(ttl_days=0)
        assert evicted >= 1

    def test_evict_older_than_keeps_recent(self, checkpoint_store):
        """Rows inside the TTL window must NOT be evicted."""
        checkpoint_store.mark_success("wf1", "run1", "s1")
        # ttl_days=7 — the row is microseconds old, well inside the window.
        evicted = checkpoint_store.evict_older_than(ttl_days=7)
        assert evicted == 0
        assert checkpoint_store.get("run1", "s1") is not None

    def test_evict_older_than_negative_is_noop(self, checkpoint_store):
        """Negative ttl is treated as a no-op (defensive against caller bugs)."""
        checkpoint_store.mark_success("wf1", "run1", "s1")
        assert checkpoint_store.evict_older_than(ttl_days=-1) == 0
        assert checkpoint_store.get("run1", "s1") is not None


# ── No-DB defensive mode ─────────────────────────────────────────────


class TestNoDbWired:
    def test_unwired_store_does_not_raise(self):
        store = CheckpointStore()  # no db
        # Every public method must be a no-op (or return empty/None).
        store.mark_success("w", "r", "s")
        store.mark_failed("w", "r", "s", error_summary="x")
        store.mark_in_progress("w", "r", "s")
        store.mark_skipped("w", "r", "s", reason="x")
        assert store.get("r", "s") is None
        assert store.get_run("r") == []
        assert store.successful_step_ids("r") == set()
        assert store.latest_failed_run("w") is None
        assert store.delete_run("r") == 0
        assert store.delete_workflow("w") == 0
        assert store.evict_older_than(7) == 0
