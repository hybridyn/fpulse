"""Pinned tests for the runtime ownership state (2026-06-07).

These are the contracts the `fpulse open` / `fpulse stop` flow relies
on, mirroring what the PowerShell launcher's launcher-utils.ps1 tests
should but can't easily express in pytest. Specifically:

  * The runtime JSON round-trips losslessly between read and write
  * The schema is forward-compatible with the PS launcher's schema
    (so `start.ps1`-written files load correctly via `fpulse stop`,
    and vice versa)
  * The 3-signal ownership check refuses every non-F-Pulse candidate
    (random PID, dead PID, our own PID, foreign cmdline)
  * stop_owned_process never kills a process that fails ownership

These guarantees are the safety net for the entire deployment story.
If they break, `fpulse stop` could start killing wrong processes -
exactly the reviewer concern that drove this work.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from fpulse.cli.runtime_state import (
    RuntimeInstance,
    is_owned_fpulse,
    make_open_instance,
    read_runtime,
    remove_runtime,
    runtime_file,
    stop_owned_process,
    write_runtime,
)


# ── File round-trip ──────────────────────────────────────────────────


class TestRuntimeFileRoundTrip:
    """The on-disk JSON must round-trip cleanly and be readable by the
    PowerShell sibling (same field names + schema_version)."""

    def test_missing_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert read_runtime() is None

    def test_write_then_read(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        inst = make_open_instance(host="127.0.0.1", port=8001, pid=12345)
        path = write_runtime(inst)
        assert path.exists()
        back = read_runtime()
        assert back is not None
        assert back.instance_id == inst.instance_id
        assert back.backend_port == 8001
        assert back.backend_pid == 12345
        assert back.mode == "open"

    def test_remove_clears_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        write_runtime(make_open_instance(host="127.0.0.1", port=8001, pid=12345))
        assert remove_runtime() is True
        assert read_runtime() is None
        # Second remove is idempotent
        assert remove_runtime() is False

    def test_corrupt_file_returns_none_not_raises(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        path = runtime_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{this is not valid JSON", encoding="utf-8")
        # Must NOT raise - the launcher is supposed to tolerate the
        # file having been hand-edited or partially written.
        assert read_runtime() is None

    def test_open_instance_marks_single_process_mode(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        inst = make_open_instance(host="127.0.0.1", port=8001, pid=99)
        # frontend and backend collapse to the same PID/port in open mode.
        assert inst.frontend_pid == inst.backend_pid == 99
        assert inst.frontend_port == inst.backend_port == 8001
        assert inst.mode == "open"


# ── Schema compatibility with the PS launcher ────────────────────────


class TestSchemaCompatibility:
    """The Python and PowerShell launchers must read each other's files.
    The fields documented in launcher/launcher-utils.ps1 are the
    contract - this test pins them."""

    def test_ps_launcher_format_loads(self, tmp_path, monkeypatch):
        # This is the exact JSON shape Write-RuntimeFile emits, copied
        # from the PowerShell instance hashtable. If we ever break
        # compatibility, this test fails loudly.
        monkeypatch.chdir(tmp_path)
        path = runtime_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schema_version": 1,
            "instance_id":    "fpulse-20260607-103000",
            "frontend_port":  5174,
            "backend_port":   8001,
            "frontend_pid":   12345,
            "backend_pid":    67890,
            "cwd":            "C:\\dev\\hybridyn-f-pulse-oss",
            "started_at":     "2026-06-07T10:30:00+00:00",
            "pid_owner":      99999,
            "mode":           "dev-script",
        }), encoding="utf-8")
        inst = read_runtime()
        assert inst is not None
        assert inst.frontend_pid == 12345
        assert inst.backend_pid == 67890
        assert inst.mode == "dev-script"

    def test_extra_fields_dont_break_load(self, tmp_path, monkeypatch):
        # Forward compatibility - a future launcher might add fields;
        # the current loader must ignore them rather than crash.
        monkeypatch.chdir(tmp_path)
        path = runtime_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schema_version": 1,
            "instance_id": "x", "frontend_port": 1, "backend_port": 2,
            "frontend_pid": 0, "backend_pid": 0, "cwd": "", "started_at": "x",
            "pid_owner": 0, "mode": "open",
            "future_field_we_dont_know_about": ["anything", "at", "all"],
        }), encoding="utf-8")
        inst = read_runtime()
        assert inst is not None


# ── Ownership check safety ───────────────────────────────────────────


class TestOwnershipCheckSafety:
    """The 3-signal check is the entire safety mechanism for `fpulse stop`.
    These tests pin that it REFUSES every wrong-target case."""

    def test_dead_pid_returns_false(self):
        # A PID we are confident is dead (very large positive integer
        # that the OS hasn't issued; psutil should error or return
        # not-running).
        assert is_owned_fpulse(999999999, expected_port=8001, kind="backend") is False

    def test_zero_pid_returns_false(self):
        # Defensive - 0 means "I have no PID recorded"; never kill.
        assert is_owned_fpulse(0, expected_port=8001, kind="backend") is False

    def test_our_own_python_pid_is_not_owned_unless_cmdline_matches(self):
        # The currently-running pytest process is python but is NOT
        # uvicorn-fpulse. The cmdline check must reject it (and the
        # port check would too, since we're not listening on 8001).
        assert is_owned_fpulse(os.getpid(), expected_port=8001, kind="backend") is False

    def test_stop_refuses_when_ownership_check_fails(self):
        # Belt-and-braces: stop_owned_process MUST gate on is_owned_fpulse.
        # We pick a dead PID so even if the gating were broken, no real
        # damage would result (we'd just get an error trying to kill
        # the PID). The assertion is on the False return - the function
        # signals "I refused to touch this."
        assert stop_owned_process(999999999, expected_port=8001, kind="backend") is False


# ── cmd_stop reads what cmd_serve writes ─────────────────────────────


class TestCmdStopEndToEnd:
    """The most important test: cmd_stop must see the runtime file that
    cmd_serve writes. We don't actually run uvicorn here - we simulate
    the runtime-file write and confirm cmd_stop reads it correctly."""

    def test_cmd_stop_no_runtime_file_is_quiet(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        from fpulse.cli import cmd_stop
        cmd_stop(None)
        out = capsys.readouterr().out
        assert "No F-Pulse instance is recorded" in out

    def test_cmd_stop_reads_runtime_file_and_skips_dead_pids(
        self, tmp_path, monkeypatch, capsys,
    ):
        monkeypatch.chdir(tmp_path)
        # Plant a runtime file pointing to a dead PID. cmd_stop must
        # report the recorded instance, then SKIP the kill because the
        # 3-signal check refuses.
        inst = make_open_instance(host="127.0.0.1", port=8001, pid=999999999)
        write_runtime(inst)
        from fpulse.cli import cmd_stop
        cmd_stop(None)
        out = capsys.readouterr().out
        assert "Found recorded instance" in out
        assert "no longer ours" in out
        # And the stale runtime file is cleaned up.
        assert read_runtime() is None
