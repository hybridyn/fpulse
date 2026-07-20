"""Unit tests for AlertStore."""

import pytest
from fpulse.alerts.models import AlertRule, AlertLog, AlertChannel, AlertCondition
from fpulse.alerts.store import AlertStore


class TestAlertStoreRules:
    def test_create_rule(self, alert_store, sample_alert_rule):
        rule = alert_store.create_rule(sample_alert_rule)
        assert rule.id == "alert-001"
        assert rule.name == "Failure Alert"

    def test_get_rule(self, alert_store, sample_alert_rule):
        alert_store.create_rule(sample_alert_rule)
        rule = alert_store.get_rule("alert-001")
        assert rule is not None
        assert rule.channel == AlertChannel.EMAIL

    def test_get_rule_nonexistent(self, alert_store):
        assert alert_store.get_rule("nope") is None

    def test_list_rules(self, alert_store, sample_alert_rule):
        alert_store.create_rule(sample_alert_rule)
        r2 = AlertRule(id="alert-002", name="Success Alert", workflow_id="wf-002",
                       condition=AlertCondition.ON_SUCCESS, channel=AlertChannel.SLACK)
        alert_store.create_rule(r2)
        result = alert_store.list_rules()
        assert len(result) == 2

    def test_list_rules_by_workflow(self, alert_store, sample_alert_rule):
        alert_store.create_rule(sample_alert_rule)
        r2 = AlertRule(id="alert-002", workflow_id="wf-002")
        alert_store.create_rule(r2)
        result = alert_store.list_rules_by_workflow("test-wf-001")
        assert len(result) == 1

    def test_list_rules_by_project(self, alert_store, sample_alert_rule):
        alert_store.create_rule(sample_alert_rule)
        result = alert_store.list_rules_by_project("default")
        assert len(result) == 1

    def test_update_rule(self, alert_store, sample_alert_rule):
        alert_store.create_rule(sample_alert_rule)
        updated = alert_store.update_rule("alert-001", {"name": "Updated Alert"})
        assert updated is not None
        assert updated.name == "Updated Alert"

    def test_update_rule_nonexistent(self, alert_store):
        assert alert_store.update_rule("nope", {"name": "X"}) is None

    def test_delete_rule(self, alert_store, sample_alert_rule):
        alert_store.create_rule(sample_alert_rule)
        assert alert_store.delete_rule("alert-001") is True
        assert alert_store.get_rule("alert-001") is None

    def test_delete_rule_nonexistent(self, alert_store):
        assert alert_store.delete_rule("nope") is False


class TestAlertStoreLogs:
    def test_add_log(self, alert_store, sample_alert_rule):
        alert_store.create_rule(sample_alert_rule)
        log = AlertLog(
            rule_id="alert-001", workflow_id="test-wf-001",
            channel=AlertChannel.EMAIL, condition=AlertCondition.ON_FAILURE,
            status="sent", message="Test alert",
        )
        result = alert_store.add_log(log)
        assert result.status == "sent"

    def test_add_log_updates_rule_trigger(self, alert_store, sample_alert_rule):
        alert_store.create_rule(sample_alert_rule)
        log = AlertLog(
            rule_id="alert-001", workflow_id="test-wf-001",
            channel=AlertChannel.EMAIL, condition=AlertCondition.ON_FAILURE,
        )
        alert_store.add_log(log)
        rule = alert_store.get_rule("alert-001")
        assert rule.trigger_count == 1
        assert rule.last_triggered_at is not None

    def test_list_logs(self, alert_store, sample_alert_rule):
        alert_store.create_rule(sample_alert_rule)
        for i in range(5):
            alert_store.add_log(AlertLog(
                rule_id="alert-001", workflow_id="test-wf-001",
                channel=AlertChannel.EMAIL, condition=AlertCondition.ON_FAILURE,
            ))
        logs = alert_store.list_logs()
        assert len(logs) == 5

    def test_list_logs_by_workflow(self, alert_store, sample_alert_rule):
        alert_store.create_rule(sample_alert_rule)
        alert_store.add_log(AlertLog(
            rule_id="alert-001", workflow_id="test-wf-001",
            channel=AlertChannel.EMAIL, condition=AlertCondition.ON_FAILURE,
        ))
        alert_store.add_log(AlertLog(
            rule_id="alert-001", workflow_id="other-wf",
            channel=AlertChannel.EMAIL, condition=AlertCondition.ON_FAILURE,
        ))
        logs = alert_store.list_logs_by_workflow("test-wf-001")
        assert len(logs) == 1

    def test_list_logs_respects_limit(self, alert_store, sample_alert_rule):
        alert_store.create_rule(sample_alert_rule)
        for _ in range(10):
            alert_store.add_log(AlertLog(
                rule_id="alert-001", workflow_id="test-wf-001",
                channel=AlertChannel.EMAIL, condition=AlertCondition.ON_FAILURE,
            ))
        logs = alert_store.list_logs(limit=3)
        assert len(logs) == 3
