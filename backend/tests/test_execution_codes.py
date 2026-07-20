"""Phase 7 — one-time, run-bound execution codes: mint / verify / consume."""
import pytest

from fpulse import runtime_config
from fpulse.security.execution_codes import (
    ExecutionCodeStore,
    enforce_execution_code,
    get_execution_code_store,
)


def _mint(store, **over):
    kw = dict(user_id="u1", workspace_id="w1", pipeline_id="p1", job_run_id="r1", action="run")
    kw.update(over)
    return store.mint(**kw)


def test_valid_code_consumes_once():
    s = ExecutionCodeStore()
    code = _mint(s)
    assert s.verify_and_consume(code, workspace_id="w1", pipeline_id="p1", action="run") is True
    # single-use: the second attempt fails
    assert s.verify_and_consume(code, workspace_id="w1", pipeline_id="p1", action="run") is False


def test_binding_mismatch_rejected():
    s = ExecutionCodeStore()
    code = _mint(s)
    assert s.verify_and_consume(code, workspace_id="OTHER") is False  # wrong workspace
    # code was not consumed by the failed check → still valid for the right binding
    assert s.verify_and_consume(code, workspace_id="w1", pipeline_id="p1") is True


def test_expired_code_rejected():
    s = ExecutionCodeStore()
    code = _mint(s, ttl_seconds=-1)  # already past expiry
    assert s.verify_and_consume(code, workspace_id="w1") is False


def test_unknown_and_empty_code_rejected():
    s = ExecutionCodeStore()
    assert s.verify_and_consume("nope") is False
    assert s.verify_and_consume("") is False


def test_enforce_is_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(runtime_config, "REQUIRE_EXECUTION_CODE", False)
    enforce_execution_code("")  # must not raise even with an empty code


def test_enforce_raises_when_required(monkeypatch):
    monkeypatch.setattr(runtime_config, "REQUIRE_EXECUTION_CODE", True)
    with pytest.raises(PermissionError):
        enforce_execution_code("bad-code", workspace_id="w1", pipeline_id="p1")
    # a freshly minted code passes the gate
    code = get_execution_code_store().mint(
        user_id="u", workspace_id="w1", pipeline_id="p1", job_run_id="r", action="run"
    )
    enforce_execution_code(code, workspace_id="w1", pipeline_id="p1", action="run")  # no raise


def test_executor_gate_blocks_then_allows(monkeypatch, tmp_path):
    """End-to-end: with the flag on, the executor refuses a run with no code
    and runs it with a valid one — proving the gate is actually wired."""
    import os
    os.environ.setdefault("FPULSE_MODE", "dev")
    from fpulse.ir.schema import Workflow, Step, StepType
    from fpulse.engine.executor import WorkflowExecutor

    monkeypatch.setattr(runtime_config, "REQUIRE_EXECUTION_CODE", True)
    wf = Workflow(
        id="p1", workspace_id="w1",
        steps=[Step(id="s", type=StepType.DB_SOURCE,
                    params={"source_mode": "query", "query": "SELECT 1 AS n"})],
    )
    ex = WorkflowExecutor(data_dir=str(tmp_path))

    blocked = ex.execute_workflow(wf)
    assert blocked.status == "error"
    assert "authorization" in blocked.step_results

    code = get_execution_code_store().mint(
        user_id="u", workspace_id="w1", pipeline_id="p1", job_run_id="r", action="run"
    )
    ok = ex.execute_workflow(wf, execution_code=code)
    assert ok.status == "success"
