"""Unit tests for the trust API — Gate 4."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from fpulse.api import trust as trust_mod


# ── Posture endpoint ─────────────────────────────────────────────────


class TestPosture:
    def test_posture_shape_stable(self):
        result = trust_mod.trust_posture()
        # Top-level required fields — compliance scrapers depend on these.
        assert "posture_version" in result
        assert "as_of" in result
        assert "sovereignty" in result
        assert "security_baseline" in result
        assert "supported_models" in result

    def test_sovereignty_defaults_to_local(self):
        result = trust_mod.trust_posture()
        sov = result["sovereignty"]
        # Default policy: data stays local, telemetry off.
        assert sov["data_stays_local_by_default"] is True
        assert sov["telemetry_default_off"] is True
        # active_provider_summary is always present even when no provider configured.
        assert "active_provider_summary" in sov

    def test_security_baseline_is_a_list_of_rows(self):
        result = trust_mod.trust_posture()
        baseline = result["security_baseline"]
        assert isinstance(baseline, list)
        assert len(baseline) > 0
        # Each row must have key/label/status/detail.
        for row in baseline:
            assert "key" in row
            assert "label" in row
            assert "status" in row
            assert "detail" in row

    def test_telemetry_consent_falls_back_to_off_on_error(self, monkeypatch):
        """If is_telemetry_enabled() throws, posture returns False (not crash)."""
        # Force the import inside _read_telemetry_consent to raise.
        import sys

        bad = type(sys)("fake_consent")
        def boom(*_a, **_kw):
            raise RuntimeError("simulated outage")
        bad.is_telemetry_enabled = boom

        monkeypatch.setitem(sys.modules, "fpulse.telemetry.consent", bad)
        result = trust_mod.trust_posture()
        # Privacy-first failure mode: when we can't confirm telemetry is on,
        # we report it as off so the posture page never lies.
        assert result["sovereignty"]["telemetry_currently_enabled"] is False


# ── Supported-models endpoint ────────────────────────────────────────


class TestSupportedModels:
    def test_default_local_cpu_is_qwen25_7b(self):
        result = trust_mod.supported_models()
        # 2026-05-19 tool-use floor revision: sub-7B models advertise tool
        # schemas but fail to drive the agent loop. See
        # project_fpulse_local_llm_floor_revision_2026-05-19.
        assert result["default_local_cpu"]["name"] == "qwen2.5:7b"
        assert result["default_local_cpu"]["provider"] == "ollama"
        assert result["default_local_cpu"]["tool_capable"] is True

    def test_cloud_default_is_off(self):
        result = trust_mod.supported_models()
        assert result["cloud_escape_hatch"]["default"] is False
        assert result["cloud_escape_hatch"]["supported"] is True
        # Listed providers must include the canonical major cloud LLMs.
        providers = set(result["cloud_escape_hatch"]["providers_supported"])
        assert "anthropic" in providers
        assert "openai" in providers

    def test_tier_default_marked(self):
        result = trust_mod.supported_models()
        defaults = [t for t in result["tiers"] if t.get("default")]
        # Exactly one default tier — the CPU laptop pick.
        assert len(defaults) == 1
        assert defaults[0]["model"] == "qwen2.5:7b"

    def test_deprecated_recommendations_includes_llama31_8b(self):
        result = trust_mod.supported_models()
        deprecated_models = {d["model"] for d in result["deprecated_recommendations"]}
        # Per the May 3 local-only lock, llama3.1:8b is documented-deprecated
        # because it's unusable on CPU laptops for tool-use.
        assert "llama3.1:8b" in deprecated_models

    def test_deprecated_recommendations_includes_sub_floor_qwen(self):
        # 2026-05-19 floor revision: qwen2.5:1.5b and :3b were the previous
        # CPU picks but fail to drive the tool-use loop reliably (silent
        # greetings instead of tool calls). Both are documented-deprecated.
        result = trust_mod.supported_models()
        deprecated_models = {d["model"] for d in result["deprecated_recommendations"]}
        assert "qwen2.5:3b" in deprecated_models
        assert "qwen2.5:1.5b" in deprecated_models


# ── Eval-summary endpoint ────────────────────────────────────────────


class TestEvalSummary:
    def test_returns_never_run_sentinel_when_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FPULSE_DATA_DIR", str(tmp_path))
        # Also ensure cwd's data/ is empty by chdir'ing somewhere clean.
        monkeypatch.chdir(tmp_path)
        result = trust_mod.eval_summary()
        assert result["ran"] is False
        assert "message" in result

    def test_reads_latest_json_when_present(self, tmp_path, monkeypatch):
        eval_dir = tmp_path / "eval"
        eval_dir.mkdir()
        payload = {
            "ran": True,
            "ran_at": "2026-05-04T00:00:00+00:00",
            "total": 14,
            "passed": 12,
            "pass_rate": 0.857,
        }
        (eval_dir / "latest.json").write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setenv("FPULSE_DATA_DIR", str(tmp_path))
        # Avoid the fallback `data/eval/latest.json` resolving from cwd.
        monkeypatch.chdir(tmp_path)
        result = trust_mod.eval_summary()
        assert result["ran"] is True
        assert result["total"] == 14
        assert result["passed"] == 12
        assert "source_path" in result

    def test_malformed_latest_json_returns_never_run(self, tmp_path, monkeypatch):
        eval_dir = tmp_path / "eval"
        eval_dir.mkdir()
        (eval_dir / "latest.json").write_text("not json", encoding="utf-8")
        monkeypatch.setenv("FPULSE_DATA_DIR", str(tmp_path))
        monkeypatch.chdir(tmp_path)
        result = trust_mod.eval_summary()
        # Falls through to the never-run sentinel rather than crashing.
        assert result["ran"] is False
