"""B3 tests (2026-06-08): resume-from-window backfill.

Pins the orchestrator's from_window slicing + the
first_unfinished_window_index helper that powers auto-detection in
the /resume API endpoint.

We don't drive the full orchestrator end-to-end here (heavy fixture:
real executor + real workflow + real exe_store). Instead:

  * Unit-test first_unfinished_window_index against a fake store with
    plantable child statuses
  * Pin the orchestrator's from_window slicing via a lightweight
    monkeypatched _run_one_window that records which windows were
    processed
  * Source-grep regression guard for the /resume API endpoint
"""
from __future__ import annotations

import pytest

from fpulse.backfills.models import BackfillRun, BackfillStatus
from fpulse.backfills.orchestrator import (
    first_unfinished_window_index,
    run_backfill_sync,
)


# ── Fake stores ──────────────────────────────────────────────────────


class _FakeStore:
    """Minimal in-memory BackfillStore-shape for unit tests. Tracks
    parent + children, update_status calls, and supports the cancel()
    poll the orchestrator does mid-iteration."""

    def __init__(self, parent: BackfillRun, children: list[BackfillRun]):
        self._parent = parent
        self._children = list(children)
        self.status_updates: list[tuple[str, BackfillStatus, bool]] = []

    def get(self, parent_id: str, workspace_id: str | None = None):
        if parent_id == self._parent.id:
            return self._parent
        for c in self._children:
            if c.id == parent_id:
                return c
        return None

    def list_children(self, parent_id: str) -> list[BackfillRun]:
        if parent_id != self._parent.id:
            return []
        return list(self._children)

    def update_status(self, run_id, status, completed=False, **kwargs):
        self.status_updates.append((run_id, status, completed))
        # Reflect on the in-memory parent so cancellation polls see it.
        if run_id == self._parent.id:
            self._parent.status = status

    def cancel(self, parent_id, workspace_id=None) -> bool:
        if parent_id == self._parent.id:
            self._parent.status = BackfillStatus.CANCELLED
            return True
        return False


def _parent(pid: str = "bf-1") -> BackfillRun:
    return BackfillRun(
        id=pid, pipeline_id="p-1",
        window_start="2026-06-01T00:00:00+00:00",
        window_end="2026-06-08T00:00:00+00:00",
    )


def _child(pid: str, parent_id: str, status: BackfillStatus,
            window_start_day: int) -> BackfillRun:
    return BackfillRun(
        id=pid, pipeline_id="p-1",
        parent_backfill_id=parent_id,
        window_start=f"2026-06-{window_start_day:02d}T00:00:00+00:00",
        window_end=f"2026-06-{window_start_day+1:02d}T00:00:00+00:00",
        status=status,
    )


# ── first_unfinished_window_index ───────────────────────────────────


class TestFirstUnfinishedIndex:
    def test_all_success_returns_zero(self):
        # Convention: nothing to resume → returns 0 (caller decides
        # whether to skip the actual orchestrator call)
        parent = _parent()
        children = [
            _child("c1", parent.id, BackfillStatus.SUCCESS, 1),
            _child("c2", parent.id, BackfillStatus.SUCCESS, 2),
            _child("c3", parent.id, BackfillStatus.SUCCESS, 3),
        ]
        store = _FakeStore(parent, children)
        assert first_unfinished_window_index(parent.id, store=store) == 0

    def test_third_window_failed_returns_2(self):
        parent = _parent()
        children = [
            _child("c1", parent.id, BackfillStatus.SUCCESS, 1),
            _child("c2", parent.id, BackfillStatus.SUCCESS, 2),
            _child("c3", parent.id, BackfillStatus.FAILED, 3),
            _child("c4", parent.id, BackfillStatus.PENDING, 4),
        ]
        store = _FakeStore(parent, children)
        assert first_unfinished_window_index(parent.id, store=store) == 2

    def test_first_pending_returns_index(self):
        # SUCCESS, SUCCESS, PENDING, PENDING - resume from index 2
        parent = _parent()
        children = [
            _child("c1", parent.id, BackfillStatus.SUCCESS, 1),
            _child("c2", parent.id, BackfillStatus.SUCCESS, 2),
            _child("c3", parent.id, BackfillStatus.PENDING, 3),
            _child("c4", parent.id, BackfillStatus.PENDING, 4),
        ]
        store = _FakeStore(parent, children)
        assert first_unfinished_window_index(parent.id, store=store) == 2

    def test_first_window_failed_returns_0(self):
        parent = _parent()
        children = [
            _child("c1", parent.id, BackfillStatus.FAILED, 1),
            _child("c2", parent.id, BackfillStatus.PENDING, 2),
        ]
        store = _FakeStore(parent, children)
        assert first_unfinished_window_index(parent.id, store=store) == 0

    def test_no_children_returns_0(self):
        parent = _parent()
        store = _FakeStore(parent, [])
        assert first_unfinished_window_index(parent.id, store=store) == 0


# ── Orchestrator slicing ────────────────────────────────────────────


class TestRunBackfillFromWindow:
    """Pin: run_backfill_sync(from_window=N) processes children[N:]
    and leaves children[:N] untouched."""

    def _instrument(self, monkeypatch, processed: list[str]):
        """Replace _run_one_window with a recorder that just marks
        success. We don't actually execute the workflow; we only verify
        which child IDs got fed in."""
        from fpulse.backfills import orchestrator as orch_mod

        def _fake_run_one_window(parent, child, **kwargs):
            processed.append(child.id)
            child.status = BackfillStatus.SUCCESS
            return child

        monkeypatch.setattr(orch_mod, "_run_one_window", _fake_run_one_window)

    def test_from_window_0_processes_all(self, monkeypatch):
        processed: list[str] = []
        self._instrument(monkeypatch, processed)
        parent = _parent()
        children = [
            _child(f"c{i}", parent.id, BackfillStatus.PENDING, i + 1)
            for i in range(5)
        ]
        store = _FakeStore(parent, children)
        run_backfill_sync(
            parent.id, store=store,
            executor=None, workflow=None, exe_store=None,
            from_window=0,
        )
        assert processed == ["c0", "c1", "c2", "c3", "c4"]

    def test_from_window_2_skips_first_two(self, monkeypatch):
        processed: list[str] = []
        self._instrument(monkeypatch, processed)
        parent = _parent()
        children = [
            _child(f"c{i}", parent.id, BackfillStatus.PENDING, i + 1)
            for i in range(5)
        ]
        store = _FakeStore(parent, children)
        run_backfill_sync(
            parent.id, store=store,
            executor=None, workflow=None, exe_store=None,
            from_window=2,
        )
        # Children 0 + 1 untouched (would be SUCCESS in real resume
        # scenario); 2-4 processed by the test recorder
        assert processed == ["c2", "c3", "c4"]

    def test_from_window_past_end_marks_success(self, monkeypatch):
        processed: list[str] = []
        self._instrument(monkeypatch, processed)
        parent = _parent()
        children = [
            _child(f"c{i}", parent.id, BackfillStatus.SUCCESS, i + 1)
            for i in range(3)
        ]
        store = _FakeStore(parent, children)
        run_backfill_sync(
            parent.id, store=store,
            executor=None, workflow=None, exe_store=None,
            from_window=99,
        )
        # Nothing processed; parent marked success directly
        assert processed == []
        # status_updates should include the success-mark
        success_updates = [u for u in store.status_updates
                            if u[1] == BackfillStatus.SUCCESS and u[2] is True]
        assert success_updates, "expected a completed=True SUCCESS update"

    def test_negative_from_window_treated_as_zero(self, monkeypatch):
        processed: list[str] = []
        self._instrument(monkeypatch, processed)
        parent = _parent()
        children = [
            _child(f"c{i}", parent.id, BackfillStatus.PENDING, i + 1)
            for i in range(3)
        ]
        store = _FakeStore(parent, children)
        run_backfill_sync(
            parent.id, store=store,
            executor=None, workflow=None, exe_store=None,
            from_window=-5,
        )
        assert processed == ["c0", "c1", "c2"]

    def test_total_windows_reflects_full_set_not_resumed_set(self, monkeypatch):
        """If a 30-window backfill resumes from 17, the aggregate
        UI still shows total_windows=30, not 13. Pin this so the
        progress bar doesn't reset."""
        processed: list[str] = []
        self._instrument(monkeypatch, processed)
        parent = _parent()
        children = [
            _child(f"c{i}", parent.id, BackfillStatus.PENDING, i + 1)
            for i in range(10)
        ]
        store = _FakeStore(parent, children)
        run_backfill_sync(
            parent.id, store=store,
            executor=None, workflow=None, exe_store=None,
            from_window=7,
        )
        # The orchestrator sets parent.total_windows to len(all_children)
        assert parent.total_windows == 10


# ── API regression guard ────────────────────────────────────────────


class TestResumeAPIWireIn:
    """Pin the /resume endpoint exists + has the right shape. Don't
    spin up the full app (workflow store + executor are heavy); pin
    by source-grep on the API module."""

    def test_resume_endpoint_declared(self):
        from pathlib import Path
        src = (
            Path(__file__).resolve().parents[1]
            / "fpulse" / "api" / "backfills.py"
        ).read_text(encoding="utf-8")
        assert "/{backfill_id}/resume" in src, (
            "B3 regression - the POST /resume endpoint must exist"
        )
        assert "first_unfinished_window_index" in src, (
            "B3 regression - resume endpoint must call the helper "
            "to auto-detect the first unfinished window"
        )

    def test_resume_refuses_running_or_success(self):
        # Document the contract via grep - the endpoint returns 409
        # for both. (Real integration test deferred to a focused
        # session that can spin up the executor.)
        from pathlib import Path
        src = (
            Path(__file__).resolve().parents[1]
            / "fpulse" / "api" / "backfills.py"
        ).read_text(encoding="utf-8")
        # Both refusal messages present
        assert "currently running" in src
        assert "already succeeded" in src

    def test_orchestrator_accepts_from_window_kwarg(self):
        # Pin: run_backfill_async signature includes from_window
        from inspect import signature
        from fpulse.backfills.orchestrator import run_backfill_async
        sig = signature(run_backfill_async)
        assert "from_window" in sig.parameters
        assert sig.parameters["from_window"].default == 0
