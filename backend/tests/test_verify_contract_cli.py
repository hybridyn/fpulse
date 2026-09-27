"""Smoke test for the tools/verify_contract.py CLI."""

from __future__ import annotations

import importlib.util
import json
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_cli():
    path = os.path.join(_ROOT, "tools", "verify_contract.py")
    spec = importlib.util.spec_from_file_location("verify_contract_cli", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_demo_verifies_and_exits_zero(capsys):
    cli = _load_cli()
    rc = cli.main(["--demo"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "VERIFIED" in out
    assert "resumed_from: 59" in out
    assert "receipt:" in out


def test_contract_and_run_from_files(tmp_path, capsys):
    cli = _load_cli()
    contract = os.path.join(_ROOT, "examples", "execution-contracts", "timesheet-monthly.json")
    run = tmp_path / "run.json"
    run.write_text(json.dumps({
        "workflow_id": "wf-x", "status": "success",
        "step_results": {"sink": {"step_id": "sink", "status": "success",
                                   "row_count": 250314,
                                   "columns": ["TM_REC_ID", "hours", "loaded_at"]}},
    }), encoding="utf-8")
    facts = tmp_path / "facts.json"
    facts.write_text(json.dumps({
        "duplicate_key_counts": {"TM_REC_ID": 0},
        "checkpoint_enabled": True, "destination_verified": True,
    }), encoding="utf-8")
    out_path = tmp_path / "receipt.json"

    rc = cli.main(["--contract", contract, "--run", str(run),
                   "--facts", str(facts), "--resumed-from", "59", "--out", str(out_path)])
    assert rc == 0
    receipt = json.loads(out_path.read_text(encoding="utf-8"))
    assert receipt["verdict"] == "verified"
    assert len(receipt["receipt_hash"]) == 64

    # a duplicate on resume fails the contract
    facts.write_text(json.dumps({
        "duplicate_key_counts": {"TM_REC_ID": 4200},
        "checkpoint_enabled": True, "destination_verified": True,
    }), encoding="utf-8")
    rc2 = cli.main(["--contract", contract, "--run", str(run), "--facts", str(facts)])
    assert rc2 == 1
