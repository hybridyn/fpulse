"""Pinned tests for the user-defined-rules engine (2026-06-07).

The engine is the OSS foundation for what becomes a Plus authoring
experience. These tests pin every contract a future UI / SQL escape
hatch must keep stable: schema validation, YAML parsing tolerance,
matcher semantics, finding-id determinism, suppression integration,
end-to-end API plumbing through /scan and /findings.

If any of these break, the Plus authoring layer above breaks too -
keep these green.
"""
from __future__ import annotations

import pytest

from fpulse.steward import (
    FindingKind,
    FindingLevel,
    FindingSeverity,
    NodeMatch,
    RuleLoadError,
    UserRule,
    WorkflowMatch,
    evaluate_rules,
    load_rules,
)


# ── Schema validation ────────────────────────────────────────────────


class TestSchemaValidation:
    """The rule id is the stable handle the entire pipeline (finding
    ids, evidence references, suppression keys) hangs off. Keep its
    validation strict."""

    def test_minimal_valid_rule(self):
        rule = UserRule.model_validate({
            "id": "minimal",
            "title": "Minimal rule",
            "match": {},  # Empty match = matches every workflow
        })
        assert rule.id == "minimal"
        assert rule.severity == FindingSeverity.P2  # default
        assert rule.level == FindingLevel.PIPELINE  # default
        assert rule.confidence == "medium"  # default
        assert rule.enabled is True  # default

    def test_id_rejects_uppercase(self):
        with pytest.raises(Exception):
            UserRule.model_validate({"id": "BadId", "title": "t", "match": {}})

    def test_id_rejects_spaces(self):
        with pytest.raises(Exception):
            UserRule.model_validate({"id": "bad id", "title": "t", "match": {}})

    def test_id_rejects_path_traversal(self):
        # The rule id ends up in filenames and evidence dicts; refuse
        # anything that could escape the rules dir.
        with pytest.raises(Exception):
            UserRule.model_validate({"id": "../etc/passwd", "title": "t", "match": {}})

    def test_id_accepts_underscores_and_hyphens(self):
        for good in ("ok", "ok-rule", "ok_rule", "ok123", "rule-1_v2"):
            r = UserRule.model_validate({"id": good, "title": "t", "match": {}})
            assert r.id == good

    def test_confidence_must_be_high_medium_low(self):
        for good in ("high", "medium", "low"):
            r = UserRule.model_validate({"id": "x", "title": "t", "match": {}, "confidence": good})
            assert r.confidence == good
        with pytest.raises(Exception):
            UserRule.model_validate({"id": "x", "title": "t", "match": {}, "confidence": "very-high"})

    def test_level_accepts_all_seven_observability_levels(self):
        # User rules can live at ANY of the 7 levels - that's the
        # whole point of bypassing KIND_TO_LEVEL for USER_DEFINED.
        for lvl in FindingLevel:
            r = UserRule.model_validate({
                "id": f"x_{lvl.value}", "title": "t", "match": {}, "level": lvl.value
            })
            assert r.level == lvl


# ── Node + workflow matchers ─────────────────────────────────────────


class TestNodeMatcher:
    """Match semantics: all present fields are AND'd. Empty = matches
    every node. These are the building blocks every user rule composes."""

    def test_type_exact_match(self):
        m = NodeMatch(type="db_sink")
        assert m.matches({"type": "db_sink", "params": {}})
        assert not m.matches({"type": "csv_source", "params": {}})

    def test_type_in_list(self):
        m = NodeMatch(type_in=["db_sink", "jdbc_sink"])
        assert m.matches({"type": "db_sink", "params": {}})
        assert m.matches({"type": "jdbc_sink", "params": {}})
        assert not m.matches({"type": "csv_source", "params": {}})

    def test_type_endswith(self):
        m = NodeMatch(type_endswith="_source")
        assert m.matches({"type": "csv_source", "params": {}})
        assert m.matches({"type": "db_source", "params": {}})
        assert not m.matches({"type": "db_sink", "params": {}})

    def test_params_eq_all_must_match(self):
        m = NodeMatch(params_eq={"environment": "prod", "table": "orders"})
        assert m.matches({"type": "db_sink", "params": {"environment": "prod", "table": "orders"}})
        # One mismatch = no match.
        assert not m.matches({"type": "db_sink", "params": {"environment": "dev", "table": "orders"}})
        # Missing key = no match.
        assert not m.matches({"type": "db_sink", "params": {"environment": "prod"}})

    def test_params_contains_substring(self):
        m = NodeMatch(params_contains={"file_path": "raw/"})
        assert m.matches({"type": "csv_source", "params": {"file_path": "/data/raw/orders.csv"}})
        assert not m.matches({"type": "csv_source", "params": {"file_path": "/data/staging/orders.csv"}})

    def test_params_in_list_membership(self):
        m = NodeMatch(params_in={"environment": ["prod", "production", "live"]})
        assert m.matches({"type": "db_sink", "params": {"environment": "prod"}})
        assert m.matches({"type": "db_sink", "params": {"environment": "live"}})
        assert not m.matches({"type": "db_sink", "params": {"environment": "dev"}})

    def test_handles_react_flow_format(self):
        # The matcher must support BOTH F-Pulse (type+params at top
        # level) AND React Flow (data.stepType + data.params) shapes -
        # same dual-format handling as Archeologist.
        m = NodeMatch(type="db_sink", params_eq={"environment": "prod"})
        rf_node = {"id": "n1", "data": {"stepType": "db_sink", "params": {"environment": "prod"}}}
        assert m.matches(rf_node)


class TestWorkflowMatcher:
    """has_node = existence; lacks_node = absence (the powerful one)."""

    def _wf(self, *nodes):
        return {"id": "w", "name": "test", "nodes": list(nodes)}

    def test_has_node_matches_when_at_least_one_node_satisfies(self):
        wm = WorkflowMatch(has_node=NodeMatch(type="db_sink"))
        wf = self._wf({"type": "csv_source"}, {"type": "db_sink"})
        assert wm.matches(wf)

    def test_has_node_fails_when_no_node_satisfies(self):
        wm = WorkflowMatch(has_node=NodeMatch(type="db_sink"))
        wf = self._wf({"type": "csv_source"}, {"type": "csv_source"})
        assert not wm.matches(wf)

    def test_lacks_node_matches_when_no_node_satisfies(self):
        # ABSENCE detection - the test rule "no db_sink with env=dev".
        wm = WorkflowMatch(lacks_node=NodeMatch(type="db_sink", params_eq={"environment": "dev"}))
        wf = self._wf(
            {"type": "db_sink", "params": {"environment": "prod"}},
        )
        assert wm.matches(wf)

    def test_lacks_node_fails_when_some_node_satisfies(self):
        wm = WorkflowMatch(lacks_node=NodeMatch(type="db_sink", params_eq={"environment": "dev"}))
        wf = self._wf(
            {"type": "db_sink", "params": {"environment": "prod"}},
            {"type": "db_sink", "params": {"environment": "dev"}},
        )
        assert not wm.matches(wf)

    def test_has_and_lacks_combine_as_and(self):
        # The canonical "writes to prod but no dev counterpart" rule.
        wm = WorkflowMatch(
            has_node=NodeMatch(type="db_sink", params_eq={"environment": "prod"}),
            lacks_node=NodeMatch(type="db_sink", params_eq={"environment": "dev"}),
        )
        prod_only = self._wf({"type": "db_sink", "params": {"environment": "prod"}})
        prod_and_dev = self._wf(
            {"type": "db_sink", "params": {"environment": "prod"}},
            {"type": "db_sink", "params": {"environment": "dev"}},
        )
        dev_only = self._wf({"type": "db_sink", "params": {"environment": "dev"}})
        assert wm.matches(prod_only)
        assert not wm.matches(prod_and_dev)
        assert not wm.matches(dev_only)

    def test_node_count_bounds(self):
        wm = WorkflowMatch(node_count_min=3, node_count_max=5)
        assert not wm.matches(self._wf({"type": "a"}, {"type": "b"}))
        assert wm.matches(self._wf({"type": "a"}, {"type": "b"}, {"type": "c"}))
        assert not wm.matches(
            self._wf(*[{"type": str(i)} for i in range(10)])
        )

    def test_empty_match_matches_everything(self):
        # Empty WorkflowMatch must NOT accidentally reject every
        # workflow - useful for "always-on" debug rules.
        wm = WorkflowMatch()
        assert wm.matches(self._wf({"type": "a"}))
        assert wm.matches(self._wf())  # Even empty workflow.


# ── YAML loader ───────────────────────────────────────────────────────


class TestYAMLLoader:
    """One bad file must not silence the others. Loader returns
    (rules, errors) so the UI can surface what failed and why."""

    def test_missing_directory_returns_empty(self, tmp_path):
        rules, errors = load_rules(tmp_path / "nonexistent")
        assert rules == [] and errors == []

    def test_empty_directory_returns_empty(self, tmp_path):
        rules, errors = load_rules(tmp_path)
        assert rules == [] and errors == []

    def test_loads_valid_rule(self, tmp_path):
        (tmp_path / "prod_only.yaml").write_text("""
id: prod_only
title: "Writes to prod"
description: "Pipeline writes to prod"
level: governance
severity: p2
match:
  has_node:
    type: db_sink
    params_eq:
      environment: prod
""", encoding="utf-8")
        rules, errors = load_rules(tmp_path)
        assert errors == []
        assert len(rules) == 1
        assert rules[0].id == "prod_only"
        assert rules[0].level == FindingLevel.GOVERNANCE

    def test_loads_yml_extension_too(self, tmp_path):
        (tmp_path / "r.yml").write_text(
            "id: ok\ntitle: t\nmatch: {}\n", encoding="utf-8"
        )
        rules, errors = load_rules(tmp_path)
        assert errors == [] and len(rules) == 1

    def test_one_bad_rule_does_not_silence_others(self, tmp_path):
        # A broken rule alongside a good one - the good one MUST still
        # load. Silent total failure was the failure mode we're guarding
        # against.
        (tmp_path / "good.yaml").write_text(
            "id: good\ntitle: ok\nmatch: {}\n", encoding="utf-8"
        )
        (tmp_path / "bad.yaml").write_text(
            "id: bad\nthis is not valid yaml: :", encoding="utf-8"
        )
        rules, errors = load_rules(tmp_path)
        assert len(rules) == 1 and rules[0].id == "good"
        assert len(errors) == 1
        assert isinstance(errors[0], RuleLoadError)
        assert "bad.yaml" in errors[0].path

    def test_empty_yaml_file_is_skipped_not_error(self, tmp_path):
        # An empty file is a half-finished edit, not a parse failure -
        # treat it as a benign skip.
        (tmp_path / "wip.yaml").write_text("", encoding="utf-8")
        rules, errors = load_rules(tmp_path)
        assert rules == [] and errors == []

    def test_duplicate_id_across_files_reports_error(self, tmp_path):
        (tmp_path / "a.yaml").write_text("id: dup\ntitle: a\nmatch: {}\n", encoding="utf-8")
        (tmp_path / "b.yaml").write_text("id: dup\ntitle: b\nmatch: {}\n", encoding="utf-8")
        rules, errors = load_rules(tmp_path)
        assert len(rules) == 1  # First one wins
        assert len(errors) == 1
        assert "duplicate" in errors[0].message.lower()


# ── Evaluator ─────────────────────────────────────────────────────────


class TestEvaluator:
    """The whole rule → finding pipeline. Findings must be deterministic,
    carry the rule id back in evidence, and obey enabled=false."""

    def _prod_without_dev_rule(self):
        return UserRule.model_validate({
            "id": "prod_without_dev",
            "title": "Pipeline writes to prod with no dev counterpart",
            "description": "Each prod pipeline should have a dev equivalent.",
            "level": "governance",
            "severity": "p2",
            "confidence": "high",
            "match": {
                "has_node": {"type": "db_sink", "params_eq": {"environment": "prod"}},
                "lacks_node": {"type": "db_sink", "params_eq": {"environment": "dev"}},
            },
            "recommend": [
                "Create a dev counterpart pipeline",
                "Or tag the pipeline `dev_required: false`",
            ],
        })

    def test_emits_finding_per_matching_workflow(self):
        rule = self._prod_without_dev_rule()
        workflows = [
            {"id": "wf-1", "name": "Sales report",
             "nodes": [{"type": "db_sink", "params": {"environment": "prod"}}]},
            {"id": "wf-2", "name": "Sales dev",
             "nodes": [{"type": "db_sink", "params": {"environment": "dev"}}]},
            {"id": "wf-3", "name": "Mixed",
             "nodes": [
                 {"type": "db_sink", "params": {"environment": "prod"}},
                 {"type": "db_sink", "params": {"environment": "dev"}},
             ]},
        ]
        findings = evaluate_rules(workflows, [rule])
        # Only wf-1 (prod, no dev) matches.
        assert len(findings) == 1
        f = findings[0]
        assert f.kind == FindingKind.USER_DEFINED
        assert f.level == FindingLevel.GOVERNANCE
        assert f.severity == FindingSeverity.P2
        assert f.evidence["rule_id"] == "prod_without_dev"
        assert f.evidence["workflow_id"] == "wf-1"
        assert "Recommended actions" in f.body

    def test_finding_ids_are_deterministic_across_runs(self):
        """Re-running the same rule against the same workflow must
        produce the same finding id - that's what lets the memory layer
        track occurrence counts and the notification bridge de-dup."""
        rule = self._prod_without_dev_rule()
        wf = {"id": "wf-x", "name": "x",
              "nodes": [{"type": "db_sink", "params": {"environment": "prod"}}]}
        a = evaluate_rules([wf], [rule])
        b = evaluate_rules([wf], [rule])
        assert a[0].id == b[0].id

    def test_disabled_rule_emits_nothing(self):
        rule = self._prod_without_dev_rule()
        rule.enabled = False
        wf = {"id": "wf-x", "name": "x",
              "nodes": [{"type": "db_sink", "params": {"environment": "prod"}}]}
        assert evaluate_rules([wf], [rule]) == []

    def test_rule_with_no_matches_emits_nothing(self):
        rule = self._prod_without_dev_rule()
        wf = {"id": "wf-x", "name": "x",
              "nodes": [{"type": "csv_source"}]}
        assert evaluate_rules([wf], [rule]) == []

    def test_multiple_rules_emit_independently(self):
        rule_a = UserRule.model_validate({
            "id": "rule_a", "title": "A", "match": {"has_node": {"type": "db_sink"}},
        })
        rule_b = UserRule.model_validate({
            "id": "rule_b", "title": "B", "match": {"has_node": {"type": "csv_source"}},
        })
        wf = {"id": "wf-x", "name": "x", "nodes": [
            {"type": "db_sink"}, {"type": "csv_source"},
        ]}
        findings = evaluate_rules([wf], [rule_a, rule_b])
        # Both rules match → two findings, one per rule.
        assert len(findings) == 2
        rule_ids = {f.evidence["rule_id"] for f in findings}
        assert rule_ids == {"rule_a", "rule_b"}

    def test_source_signature_distinguishes_rule_workflow_pairs(self):
        """Each (rule, workflow) pair gets a unique source_signature so
        users can dismiss just ONE of N matches without silencing the
        whole rule."""
        rule = UserRule.model_validate({
            "id": "rx", "title": "t", "match": {"has_node": {"type": "db_sink"}},
        })
        wfs = [
            {"id": "wf-1", "name": "a", "nodes": [{"type": "db_sink"}]},
            {"id": "wf-2", "name": "b", "nodes": [{"type": "db_sink"}]},
        ]
        findings = evaluate_rules(wfs, [rule])
        sigs = {f.evidence["source_signature"] for f in findings}
        assert len(sigs) == 2
        assert "user_rule:rx:wf-1" in sigs
        assert "user_rule:rx:wf-2" in sigs


# ── End-to-end via the API ───────────────────────────────────────────


class TestAPIIntegration:
    """The full path: YAML on disk → GET /findings → POST /dismiss.
    Pins that the API plumbing is wired correctly through _run_scan."""

    def _make_client(self, tmp_path, monkeypatch, workflows, rule_yaml=None):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import fpulse.api.steward as steward_mod
        import fpulse.main as main_mod
        monkeypatch.setattr(main_mod, "app_state",
                            {"data_dir": str(tmp_path)}, raising=False)
        monkeypatch.setattr(steward_mod, "_workflows_for_scan", lambda ws: workflows)
        # Seed a rule on disk if one was supplied - the API loads from
        # <data_dir>/steward/default/rules/.
        if rule_yaml:
            rules_dir = tmp_path / "steward" / "default" / "rules"
            rules_dir.mkdir(parents=True, exist_ok=True)
            (rules_dir / "rule.yaml").write_text(rule_yaml, encoding="utf-8")
        app = FastAPI()
        from fpulse.auth.deps import require_auth
        app.dependency_overrides[require_auth] = lambda: None
        app.include_router(steward_mod.router)
        return TestClient(app)

    def test_get_rules_lists_loaded_rules(self, tmp_path, monkeypatch):
        client = self._make_client(
            tmp_path, monkeypatch, workflows=[],
            rule_yaml="id: r1\ntitle: First rule\nmatch: {}\n",
        )
        r = client.get("/api/steward/rules")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        assert body["rules"][0]["id"] == "r1"
        assert body["errors"] == []

    def test_get_rules_surfaces_parse_errors(self, tmp_path, monkeypatch):
        rules_dir = tmp_path / "steward" / "default" / "rules"
        rules_dir.mkdir(parents=True, exist_ok=True)
        (rules_dir / "good.yaml").write_text(
            "id: ok\ntitle: ok\nmatch: {}\n", encoding="utf-8"
        )
        (rules_dir / "bad.yaml").write_text(
            "id: bad\nthis is not valid yaml: :", encoding="utf-8"
        )
        client = self._make_client(tmp_path, monkeypatch, workflows=[])
        body = client.get("/api/steward/rules").json()
        assert body["count"] == 1
        assert len(body["errors"]) == 1
        assert "bad.yaml" in body["errors"][0]["path"]

    def test_findings_endpoint_includes_user_rule_matches(self, tmp_path, monkeypatch):
        workflows = [
            {"id": "wf-1", "name": "prod only", "workspace_id": "default",
             "nodes": [{"type": "db_sink", "params": {"environment": "prod"}}]},
        ]
        rule = """
id: prod_only
title: Pipeline writes to prod with no dev counterpart
level: governance
severity: p2
match:
  has_node:
    type: db_sink
    params_eq: {environment: prod}
  lacks_node:
    type: db_sink
    params_eq: {environment: dev}
"""
        client = self._make_client(tmp_path, monkeypatch, workflows, rule_yaml=rule)
        body = client.get("/api/steward/findings").json()
        ids = [f["evidence"].get("rule_id") for f in body["findings"]]
        assert "prod_only" in ids

    def test_dismissing_a_user_rule_finding_suppresses_it(self, tmp_path, monkeypatch):
        """Dismiss flow works identically for user-rule findings - same
        suppression store, same alert-fatigue guarantees."""
        workflows = [
            {"id": "wf-1", "name": "x", "workspace_id": "default",
             "nodes": [{"type": "db_sink", "params": {"environment": "prod"}}]},
        ]
        rule = (
            "id: prod_only\n"
            "title: t\n"
            "match:\n"
            "  has_node:\n"
            "    type: db_sink\n"
            "    params_eq: {environment: prod}\n"
        )
        client = self._make_client(tmp_path, monkeypatch, workflows, rule_yaml=rule)
        body = client.get("/api/steward/findings").json()
        user_findings = [f for f in body["findings"]
                          if f["evidence"].get("rule_source") == "user_defined"]
        assert len(user_findings) == 1
        finding_id = user_findings[0]["id"]

        # Dismiss it.
        r = client.post(f"/api/steward/findings/{finding_id}/dismiss",
                        json={"reason": "Intentional - we don't have dev for this dataset"})
        assert r.status_code == 200

        # Re-scan: the user-rule finding is now suppressed.
        body2 = client.get("/api/steward/findings").json()
        user_findings2 = [f for f in body2["findings"]
                           if f["evidence"].get("rule_source") == "user_defined"]
        assert user_findings2 == []
