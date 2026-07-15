"""Unit tests for NotificationService."""

import pytest
from unittest.mock import patch, MagicMock
from fpulse.alerts.notifier import NotificationService
from fpulse.alerts.models import AlertRule, AlertChannel, AlertCondition, AlertLog


@pytest.fixture
def notifier():
    return NotificationService()


@pytest.fixture
def email_rule():
    return AlertRule(
        id="r1", name="Email Alert", workflow_id="wf-001",
        condition=AlertCondition.ON_FAILURE, channel=AlertChannel.EMAIL,
        email_addresses=["user@test.com"],
    )


@pytest.fixture
def slack_rule():
    return AlertRule(
        id="r2", name="Slack Alert", workflow_id="wf-001",
        condition=AlertCondition.ON_FAILURE, channel=AlertChannel.SLACK,
        slack_webhook_url="https://hooks.slack.com/test",
    )


@pytest.fixture
def webhook_rule():
    return AlertRule(
        id="r3", name="Webhook Alert", workflow_id="wf-001",
        condition=AlertCondition.ON_FAILURE, channel=AlertChannel.WEBHOOK,
        webhook_url="https://example.com/webhook",
    )


@pytest.fixture
def context():
    return {
        "workflow_name": "Test Pipeline",
        "status": "error",
        "execution_id": "exe-001",
        "duration_ms": 5000,
        "error_message": "Step s2 failed",
        "triggered_condition": "on_failure",
    }


class TestNotifierMessageBuilding:
    def test_default_message(self, notifier, email_rule, context):
        msg = notifier._build_message(email_rule, context)
        assert "Test Pipeline" in msg
        assert "ERROR" in msg
        assert "5.0s" in msg
        assert "Step s2 failed" in msg

    def test_custom_message_template(self, notifier, context):
        rule = AlertRule(
            id="r1", channel=AlertChannel.EMAIL,
            custom_message="Pipeline {{workflow_name}} ended with {{status}}",
            condition=AlertCondition.ON_ANY,
        )
        msg = notifier._build_message(rule, context)
        assert "Pipeline Test Pipeline ended with error" == msg

    def test_message_without_error(self, notifier, email_rule):
        ctx = {"workflow_name": "OK Pipeline", "status": "success", "triggered_condition": "on_success"}
        msg = notifier._build_message(email_rule, ctx)
        assert "SUCCESS" in msg
        assert "Error" not in msg


class TestNotifierEmailDryRun:
    def test_email_failed_when_no_smtp(self, notifier, email_rule, context):
        """Without SMTP configured, the notifier now correctly returns a
        FAILED log (with a `failed` status + a helpful "SMTP not
        configured" reason) instead of silently dry-running. Dry-run
        masked the fact that nothing was actually being delivered."""
        log = notifier.send(email_rule, context)
        assert isinstance(log, AlertLog)
        assert log.status == "failed"
        assert log.channel == AlertChannel.EMAIL

    def test_email_no_recipients(self, notifier, context):
        rule = AlertRule(
            id="r1", channel=AlertChannel.EMAIL,
            condition=AlertCondition.ON_FAILURE,
            email_addresses=[],
        )
        log = notifier.send(rule, context)
        assert log.status == "failed"
        assert "No email" in log.error


class TestNotifierSlack:
    @patch.object(NotificationService, '_post_json')
    def test_slack_sends_payload(self, mock_post, notifier, slack_rule, context):
        log = notifier.send(slack_rule, context)
        assert log.status == "sent"
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert "https://hooks.slack.com/test" == call_args[0][0]

    def test_slack_no_url(self, notifier, context):
        rule = AlertRule(
            id="r1", channel=AlertChannel.SLACK,
            condition=AlertCondition.ON_FAILURE,
            slack_webhook_url="",
        )
        log = notifier.send(rule, context)
        assert log.status == "failed"


class TestNotifierWebhook:
    @patch.object(NotificationService, '_post_json')
    def test_webhook_sends_payload(self, mock_post, notifier, webhook_rule, context):
        log = notifier.send(webhook_rule, context)
        assert log.status == "sent"
        mock_post.assert_called_once()

    def test_webhook_no_url(self, notifier, context):
        rule = AlertRule(
            id="r1", channel=AlertChannel.WEBHOOK,
            condition=AlertCondition.ON_FAILURE,
            webhook_url="",
        )
        log = notifier.send(rule, context)
        assert log.status == "failed"


class TestNotifierTeams:
    @patch.object(NotificationService, '_post_json')
    def test_teams_sends_payload(self, mock_post, notifier, context):
        rule = AlertRule(
            id="r1", channel=AlertChannel.TEAMS,
            condition=AlertCondition.ON_FAILURE,
            teams_webhook_url="https://teams.webhook.test",
        )
        log = notifier.send(rule, context)
        assert log.status == "sent"

    def test_teams_no_url(self, notifier, context):
        rule = AlertRule(
            id="r1", channel=AlertChannel.TEAMS,
            condition=AlertCondition.ON_FAILURE,
            teams_webhook_url="",
        )
        log = notifier.send(rule, context)
        assert log.status == "failed"


class TestNotifierHTMLEmail:
    def test_html_email_content(self, notifier):
        """Minimum-context render. Orange 'F-Pulse OSS Alert' banner is
        present (restored 2026-05-21 second pass after operator feedback
        — the visual anchor reads as cleaner than starting cold on the
        pipeline name). What's NOT present: the redundant
        `{workflow} — {STATUS}` sub-line that used to appear underneath
        the banner; the email subject + compact header carry it once."""
        html = notifier._build_html_email("Test Subject", "Line 1\nLine 2")
        assert "F-Pulse OSS Alert" in html  # banner restored
        assert "Sent by F-Pulse OSS" in html
        # The redundant pipeline-name sub-line under the banner must NOT
        # come back — that was the original redundancy the operator flagged.
        assert "Unknown — UNKNOWN" not in html

    def test_html_email_uses_wide_container(self, notifier, context):
        """Layout regression test — the alert template's outer container
        should use the wide (920px) layout, not the cramped 640px one
        the user complained about. Stops the width from drifting back
        when someone tweaks the template."""
        html = notifier._build_html_email("subject", "body", context=context)
        assert "max-width:920px" in html
        # And the prior narrow value MUST NOT appear anywhere else.
        assert "max-width:640px" not in html

    def test_html_email_compact_header_renders(self, notifier, context):
        """2026-05-21 redesign: pipeline name + status pill render in a
        single compact row, not a 5-column table. The fixed-column /
        colgroup pattern from the old design must NOT come back.

        Status text is lower-case in the source — CSS `text-transform:
        uppercase` handles the visual cap; we assert the literal token."""
        html = notifier._build_html_email("subject", "body", context=context)
        # Name + status chip both appear...
        assert "Test Pipeline" in html
        # The status pill carries the raw status word ("error"); CSS
        # uppercases it at render time.
        assert ">error</span>" in html
        # ...and the old 5-column header labels are gone — they were
        # pure redundancy with the email subject + meta-line.
        assert ">Pipeline<" not in html
        assert "table-layout:fixed" not in html
        assert "<colgroup>" not in html

    def test_html_email_contains_structured_sections(self, notifier, context):
        """Post-redesign section list: orange banner / compact header /
        Run Details (when metadata flows) / Lineage (when workflow_steps
        present) / Original Failure (when error_message present) /
        footer. Banner restored; the redundant `{workflow} — {STATUS}`
        sub-line is gone for good."""
        ctx = dict(context)
        ctx["workflow_steps"] = [
            {"name": "Source", "type": "source", "status": "success"},
            {"name": "Destination", "type": "destination", "status": "error"},
        ]
        html = notifier._build_html_email("subject", "body", context=ctx)
        # Banner present
        assert "F-Pulse OSS Alert" in html
        # Compact header
        assert "Test Pipeline" in html
        # Lineage section
        assert "Lineage" in html
        assert "Source" in html and "Destination" in html
        # Per-step status badges are part of the in-app-parity rendering.
        assert "SUCCESS" in html and "ERROR" in html
        # Original failure section
        assert "Original Failure" in html
        assert "Step s2 failed" in html
        # Footer
        assert "Sent by F-Pulse OSS" in html
        # The redundant pieces that the operator flagged stay removed.
        # (banner sub-line + 5-column pipeline table with fixed columns)
        assert "Test Pipeline — ERROR" not in html  # redundant sub-line
        assert "table-layout:fixed" not in html  # old 5-col header

    def test_html_email_run_details_renders_with_metadata(self, notifier):
        """When operational metadata flows in, the Run Details block lists
        Project / Trigger / Started / Steps / Rows / Owner — the fields
        the operator asked for on 2026-05-21.

        Folder name only renders when ``folder_store`` is available in
        ``app_state`` (resolved from folder_id → folder.name). Pytest
        doesn't boot app_state, so the Folder row is intentionally
        skipped here — a dedicated integration test covers the resolved
        path. project_id falls through as the raw id when project_store
        isn't around; we assert presence of the Project label only."""
        ctx = {
            "workflow_name": "Test Pipeline",
            "status": "success",
            "triggered_condition": "on_success",
            "triggered_by": "manual",
            "started_at": "2026-05-21T08:55:01+00:00",
            "rows_processed": 1234,
            "steps_completed": 2,
            "steps_total": 2,
            "owner_email": "alice@example.com",
            "project_id": "proj-1",
            # folder_id intentionally omitted — without folder_store the
            # renderer correctly suppresses the row.
        }
        html = notifier._build_html_email("s", "b", context=ctx)
        assert "Run Details" in html
        assert ">Project<" in html
        assert ">Trigger<" in html
        assert ">Started<" in html
        assert ">Steps<" in html
        assert ">Rows<" in html
        assert ">Owner<" in html
        # The actual values render too.
        assert "1,234" in html
        assert "2 / 2" in html
        assert "alice@example.com" in html
        assert "Manual" in html
        # Folder row not rendered (no folder_store available).
        assert ">Folder<" not in html


class TestNotifierComplexPipelineSections:
    """Locks down the five complex-pipeline sections added for
    foreach-loop / nested-branch failures.

    Each section is conditional on its specific context fields — a
    simple linear pipeline (where none of the new fields are present)
    must NOT render any of these blocks."""

    @pytest.fixture
    def base_ctx(self):
        return {
            "workflow_name": "Daily Sync",
            "status": "error",
            "execution_id": "exe-100",
            "duration_ms": 18000,
            "error_message": "Login failed (18456)",
            "first_failed_step": "DBSink",
            "triggered_condition": "on_failure",
        }

    # ── Step path (failure breadcrumb) ──────────────────────────────

    def test_step_path_renders_breadcrumb(self, notifier, base_ctx):
        ctx = {**base_ctx, "step_path": ["Pipeline", "ForEach (customers)",
                                            "IfBranch (active)", "DBSink"]}
        html = notifier._build_html_email("s", "b", context=ctx)
        assert "Failure Path" in html
        # All breadcrumb segments present.
        assert "ForEach (customers)" in html
        assert "IfBranch (active)" in html
        # The chevron separator is used between segments.
        assert "›" in html

    def test_step_path_absent_for_simple_pipelines(self, notifier, base_ctx):
        """No step_path → no Failure Path section."""
        html = notifier._build_html_email("s", "b", context=base_ctx)
        assert "Failure Path" not in html

    def test_step_path_skipped_when_only_one_segment(self, notifier, base_ctx):
        """A 1-segment 'breadcrumb' is just the step name — no value
        in rendering an empty breadcrumb."""
        ctx = {**base_ctx, "step_path": ["DBSink"]}
        html = notifier._build_html_email("s", "b", context=ctx)
        assert "Failure Path" not in html

    # ── Iteration context (loop failures) ───────────────────────────

    def test_iteration_context_renders_progress_and_key(self, notifier, base_ctx):
        ctx = {
            **base_ctx,
            "failed_step_iteration": {
                "loop_name": "ForEach (customers)",
                "current": 47, "total": 100,
                "key": "customer_id=ACME-2024-0047",
            },
        }
        html = notifier._build_html_email("s", "b", context=ctx)
        assert "Iteration Context" in html
        assert "47 of 100" in html
        assert "47.0%" in html  # progress percentage
        assert "ACME-2024-0047" in html

    def test_iteration_context_handles_missing_total(self, notifier, base_ctx):
        """An untyped while-style loop may know iteration count but not
        total — should render position without percentage."""
        ctx = {**base_ctx, "failed_step_iteration": {"current": 47, "loop_name": "ForEach"}}
        html = notifier._build_html_email("s", "b", context=ctx)
        assert "Iteration Context" in html
        assert "47" in html
        assert "%" not in html.split("Iteration Context")[1].split("Original Failure")[0]

    def test_iteration_context_absent_when_not_in_loop(self, notifier, base_ctx):
        html = notifier._build_html_email("s", "b", context=base_ctx)
        assert "Iteration Context" not in html

    # ── Retry attempts + step input ──────────────────────────────────

    def test_retry_attempts_badge_shows_when_retried(self, notifier, base_ctx):
        """Retry-exhaustion is a different failure class from first-try
        failure — the email must call it out so operators don't
        re-run blindly assuming a transient blip."""
        ctx = {**base_ctx, "failed_step_attempts": 3}
        html = notifier._build_html_email("s", "b", context=ctx)
        assert "Retried 3" in html
        assert "before giving up" in html

    def test_retry_badge_absent_for_first_try_failure(self, notifier, base_ctx):
        ctx = {**base_ctx, "failed_step_attempts": 1}
        html = notifier._build_html_email("s", "b", context=ctx)
        assert "Retried" not in html

    def test_step_input_truncated_when_huge(self, notifier, base_ctx):
        big = "x" * 2000
        ctx = {**base_ctx, "failed_step_input_snippet": big}
        html = notifier._build_html_email("s", "b", context=ctx)
        assert "Step Input" in html
        # Body snippet capped before the truncation marker.
        assert "(truncated)" in html
        # Whole 2000-char string MUST NOT be present in full.
        assert html.count("x" * 1500) == 0

    # ── Loop progress tiles ──────────────────────────────────────────

    def test_loop_progress_renders_three_counts(self, notifier, base_ctx):
        ctx = {
            **base_ctx,
            "loop_progress_summary": {
                "successful_iterations": 46,
                "failed_iterations": 1,
                "remaining_iterations": 53,
            },
        }
        html = notifier._build_html_email("s", "b", context=ctx)
        assert "Loop Progress" in html
        # All three counts visible.
        assert ">46<" in html
        assert ">1<" in html
        assert ">53<" in html
        # Total iterations summary.
        assert "Total iterations" in html and "100" in html

    def test_loop_progress_absent_when_no_summary(self, notifier, base_ctx):
        html = notifier._build_html_email("s", "b", context=base_ctx)
        assert "Loop Progress" not in html

    # ── Execution timeline ───────────────────────────────────────────

    def test_execution_timeline_renders_per_step_metrics(self, notifier, base_ctx):
        ctx = {
            **base_ctx,
            "step_metrics": [
                {"name": "Source", "depth": 0, "status": "success",
                  "duration_ms": 2300, "rows_in": 0, "rows_out": 100},
                {"name": "ForEach (customers)", "depth": 0, "status": "error",
                  "duration_ms": 18000, "rows_in": 100, "rows_out": 46},
                {"name": "DBSink", "depth": 1, "status": "error",
                  "duration_ms": 500, "rows_in": 1, "rows_out": 0},
            ],
        }
        html = notifier._build_html_email("s", "b", context=ctx)
        assert "Execution Timeline" in html
        # Step names and durations rendered.
        assert "Source" in html
        assert "DBSink" in html
        assert "2.3s" in html  # 2300 ms
        assert "18.0s" in html  # 18000 ms
        # Status pills rendered for each step
        assert "success" in html.lower()
        # Rows in/out arrow notation.
        assert "0 →" in html or "0 →" in html

    def test_execution_timeline_uses_depth_for_indent(self, notifier, base_ctx):
        """Nested steps (depth > 0) must indent visually so the tree
        shape is preserved in a flat table."""
        ctx = {
            **base_ctx,
            "step_metrics": [
                {"name": "ForEach", "depth": 0, "status": "error", "duration_ms": 100},
                {"name": "InnerStep", "depth": 1, "status": "error", "duration_ms": 50},
            ],
        }
        html = notifier._build_html_email("s", "b", context=ctx)
        # Indentation rendered as a width-spaced span.
        assert "width:18px" in html  # depth=1 × 18px

    def test_execution_timeline_absent_without_metrics(self, notifier, base_ctx):
        html = notifier._build_html_email("s", "b", context=base_ctx)
        assert "Execution Timeline" not in html

    # ── Resume hint ──────────────────────────────────────────────────

    def test_resume_hint_renders_when_checkpoint_exists(self, notifier, base_ctx):
        ctx = {**base_ctx, "resume_available": True, "resume_token": "ckpt-abc123"}
        html = notifier._build_html_email("s", "b", context=ctx)
        assert "Resume Available" in html
        assert "ckpt-abc123" in html
        assert "skip already-" in html  # phrasing of the suggestion

    def test_resume_hint_absent_when_no_checkpoint(self, notifier, base_ctx):
        html = notifier._build_html_email("s", "b", context=base_ctx)
        assert "Resume Available" not in html

    # ── Backward compatibility ───────────────────────────────────────

    def test_simple_pipeline_email_unchanged_apart_from_layout(self, notifier, base_ctx):
        """A simple linear pipeline with NONE of the new fields must
        render exactly the original five sections — header, pipeline
        table, original failure, footer, AI diagnosis. No new sections
        leak in just because the template knows about them."""
        html = notifier._build_html_email("s", "b", context=base_ctx)
        # New sections all absent.
        for section in ("Failure Path", "Iteration Context",
                          "Loop Progress", "Execution Timeline",
                          "Resume Available", "Step Input",
                          "Retried", "Run Details"):
            assert section not in html, f"{section!r} leaked into simple-pipeline email"


class TestNotifierRunDetailsAndActions:
    """Run Details key/value table + the action-buttons row.

    Run Details only renders rows for fields that are populated, so
    a partially-populated alert still looks clean — no empty
    'Workspace: —' rows or blank action buttons."""

    @pytest.fixture
    def base_ctx(self):
        return {
            "workflow_name": "Daily Sync",
            "status": "error",
            "duration_ms": 5000,
            "error_message": "Step s2 failed",
            "first_failed_step": "DBSink",
            "triggered_condition": "on_failure",
        }

    def test_run_details_renders_when_metadata_present(self, notifier, base_ctx):
        ctx = {
            **base_ctx,
            "execution_id": "exe-abc-123",
            "workflow_id": "wf-456",
            "schedule_name": "daily-sync",
            "triggered_by": "schedule",
            "started_at": "2026-05-09T11:25:13Z",
            "completed_at": "2026-05-09T11:25:18Z",
            "workspace_id": "default",
            "project_id": "Analytics",
            "environment": "prod",
        }
        html = notifier._build_html_email("s", "b", context=ctx)
        assert "Run Details" in html
        # 2026-05-21: Execution ID / Pipeline ID rows removed — the
        # "View Execution" deep-link button covers navigation, and the
        # raw hex strings were noise the operator complained about.
        assert "Execution ID" not in html
        assert "Pipeline ID" not in html
        # Trigger source combines with schedule name into a readable string.
        # Schedule name is HTML-escaped (`&quot;daily-sync&quot;`) when
        # rendered — assert on the escaped form too so a future _esc()
        # tweak still passes.
        assert ('Schedule "daily-sync"' in html) or ("Schedule &quot;daily-sync&quot;" in html)
        # Timestamp formatted to YYYY-MM-DD HH:MM:SS UTC.
        assert "2026-05-09 11:25:13 UTC" in html
        # Workspace also dropped — same noise rationale as Execution ID.
        assert "Workspace" not in html
        assert "Project" in html and "Analytics" in html
        # Environment is upper-cased for readability.
        assert "PROD" in html

    def test_run_details_skips_unpopulated_rows(self, notifier, base_ctx):
        """Only triggered_by is supplied — Run Details renders, but
        missing rows (Project, Steps, Rows, Owner) don't show as empty
        placeholders. (Execution ID is no longer rendered at all in
        the 2026-05-21 redesign — it's covered by the View Execution
        button.)"""
        ctx = {**base_ctx, "execution_id": "exe-1", "triggered_by": "manual"}
        html = notifier._build_html_email("s", "b", context=ctx)
        assert "Run Details" in html
        assert "Manual" in html  # title-cased trigger source
        # The Execution ID row is gone by design.
        assert "Execution ID" not in html
        # Rows that weren't supplied must NOT appear.
        assert "Workspace" not in html
        assert ">Project<" not in html
        assert ">Steps<" not in html
        assert ">Rows<" not in html
        assert "Environment" not in html

    def test_run_details_absent_when_no_metadata(self, notifier, base_ctx):
        """No metadata keys → no Run Details section at all."""
        html = notifier._build_html_email("s", "b", context=base_ctx)
        assert "Run Details" not in html

    def test_run_details_handles_unparseable_timestamp_gracefully(self, notifier, base_ctx):
        """Bad ISO strings shouldn't crash the email — fall back to
        the raw value rather than dropping the row entirely."""
        ctx = {**base_ctx, "started_at": "not-a-date"}
        html = notifier._build_html_email("s", "b", context=ctx)
        assert "Run Details" in html
        assert "Started" in html
        # Original (un-formatted) string surfaces.
        assert "not-a-date" in html

    # 2026-05-21: Action-button row (View Execution / Open Pipeline /
    # View All Runs) was removed from the alert email per operator
    # feedback — the email is for at-a-glance status, not navigation.
    # These three tests are now regression guards: even when deep-link
    # fields ARE supplied (Slack/Teams paths still use them on the
    # payload), the HTML email body MUST NOT render any of the buttons
    # or carry their URLs.

    def test_action_buttons_never_render_even_with_links(self, notifier, base_ctx):
        """Deep-link fields on the payload feed Slack/Teams — they must
        NOT bleed into the HTML email body as buttons."""
        ctx = {
            **base_ctx,
            "execution_link": "https://app.example.com/#executions/exe-1",
            "workflow_link":  "https://app.example.com/#editor?workflow=wf-1",
            "deep_link":      "https://app.example.com/#executions?workflow=wf-1",
        }
        html = notifier._build_html_email("s", "b", context=ctx)
        assert "View Execution" not in html
        assert "Open Pipeline" not in html
        assert "View All Runs" not in html
        # And no <a href> anywhere referencing those URLs.
        assert "https://app.example.com" not in html

    def test_action_buttons_absent_when_no_links(self, notifier, base_ctx):
        """Same guarantee with no link fields — the row never appears."""
        html = notifier._build_html_email("s", "b", context=base_ctx)
        assert "View Execution" not in html
        assert "Open Pipeline" not in html
        assert "View All Runs" not in html

    def test_no_button_urls_inlined_even_with_malicious_value(self, notifier, base_ctx):
        """The URL-escape path is moot now that no <a> is rendered, but
        we keep this guard: a malicious deep-link value MUST NOT show up
        in the body in any form — escaped OR un-escaped."""
        evil = 'https://app.example.com/x"><script>alert(1)</script>'
        ctx = {**base_ctx, "execution_link": evil}
        html = notifier._build_html_email("s", "b", context=ctx)
        # No raw script tag.
        assert "<script>" not in html
        # And the URL itself doesn't surface anywhere.
        assert "app.example.com" not in html


class TestNotifierLayeredDAGLineage:
    """Locks down the layered DAG lineage rendering added 2026-05-28.

    The user complained that the alert email rendered the Sales Pivot +
    Trend Analysis pipeline (2 sources, multiple branches, joins) as a
    misleading linear ``A → B → C → D`` chain. The fix passes
    ``workflow_connections`` through to the notifier, which now groups
    steps into rank-columns and stacks within a column when more than
    one step shares a rank.
    """

    @pytest.fixture
    def notifier(self):
        return NotificationService()

    @pytest.fixture
    def diamond_steps(self):
        """Diamond DAG — two parallel branches that join at a sink.

            A ─┐         ┌─ C
                ├─ B ──┤
            X ─┘         └─ Y

        Two sources (A, X) → one transform (B) → two sinks (C, Y).
        Rank layout should be:
          col0: [A, X]   col1: [B]   col2: [C, Y]
        """
        return {
            "steps": [
                {"id": "a", "name": "Source A", "type": "source", "status": "success"},
                {"id": "x", "name": "Source X", "type": "source", "status": "success"},
                {"id": "b", "name": "Pivot B",  "type": "pivot",  "status": "success"},
                {"id": "c", "name": "Sink C",   "type": "destination", "status": "success"},
                {"id": "y", "name": "Sink Y",   "type": "destination", "status": "error"},
            ],
            "connections": [
                {"from": "a", "to": "b"},
                {"from": "x", "to": "b"},
                {"from": "b", "to": "c"},
                {"from": "b", "to": "y"},
            ],
        }

    def test_dag_lineage_renders_all_steps(self, notifier, diamond_steps):
        """All 5 step names appear in the rendered email — the rank
        layout never drops a node."""
        ctx = {
            "workflow_name": "Diamond",
            "status": "error",
            "workflow_steps": diamond_steps["steps"],
            "workflow_connections": diamond_steps["connections"],
        }
        html = notifier._build_html_email("s", "b", context=ctx)
        for name in ("Source A", "Source X", "Pivot B", "Sink C", "Sink Y"):
            assert name in html, f"step '{name}' missing from DAG render"

    def test_dag_lineage_has_three_columns_not_five(self, notifier, diamond_steps):
        """The diamond has 5 steps but max rank=2 — the renderer should
        produce exactly 2 inter-column arrows (col0→col1, col1→col2),
        NOT the 4 arrows the old flat layout would have produced.
        Counts the literal `→` glyph in the lineage block to verify
        the rank-based collapse actually happened."""
        ctx = {
            "workflow_name": "Diamond",
            "status": "error",
            "workflow_steps": diamond_steps["steps"],
            "workflow_connections": diamond_steps["connections"],
        }
        html = notifier._build_html_email("s", "b", context=ctx)
        # The lineage block uses '→' (U+2192) for inter-column arrows.
        # The Run Details / Failure blocks do not, so a count of the
        # full document is safe. 2 arrows for a 3-rank DAG.
        arrow_count = html.count("→")
        assert arrow_count == 2, (
            f"expected 2 inter-column arrows for a 3-rank diamond, got {arrow_count}. "
            "The renderer may have fallen back to the per-step flat layout."
        )

    def test_dag_falls_back_to_linear_without_connections(self, notifier):
        """Legacy callers (or simple linear pipelines that haven't been
        upgraded) pass only ``workflow_steps`` — the renderer must
        keep producing the original linear layout with N-1 arrows."""
        ctx = {
            "workflow_name": "Linear",
            "status": "success",
            "workflow_steps": [
                {"name": "Read",  "type": "source",       "status": "success"},
                {"name": "Clean", "type": "filter",       "status": "success"},
                {"name": "Save",  "type": "destination",  "status": "success"},
                {"name": "Notify","type": "webhook",      "status": "success"},
            ],
            # No workflow_connections → flat layout
        }
        html = notifier._build_html_email("s", "b", context=ctx)
        # 4 steps → 3 inter-step arrows in flat layout.
        assert html.count("→") == 3

    def test_dag_handles_cycle_safely(self, notifier):
        """Cycle safety: a malformed connections list with a cycle
        must not blow the stack. Renderer should clamp ranks and still
        produce HTML containing every step name."""
        ctx = {
            "workflow_name": "Cycle",
            "status": "error",
            "workflow_steps": [
                {"id": "a", "name": "A", "type": "source",      "status": "success"},
                {"id": "b", "name": "B", "type": "transform",   "status": "success"},
                {"id": "c", "name": "C", "type": "destination", "status": "error"},
            ],
            "workflow_connections": [
                {"from": "a", "to": "b"},
                {"from": "b", "to": "c"},
                {"from": "c", "to": "a"},  # back-edge → cycle
            ],
        }
        html = notifier._build_html_email("s", "b", context=ctx)
        assert "A" in html and "B" in html and "C" in html

    def test_dag_accepts_ir_field_names(self, notifier):
        """Connections may arrive with the IR's ``from_step`` /
        ``to_step`` field names rather than the notifier's canonical
        ``from`` / ``to`` — both shapes should produce the same
        layered layout. Catches a future refactor that drops the
        legacy alias."""
        ctx = {
            "workflow_name": "Y-shape",
            "status": "success",
            "workflow_steps": [
                {"id": "a", "name": "A", "type": "source", "status": "success"},
                {"id": "b", "name": "B", "type": "source", "status": "success"},
                {"id": "c", "name": "C", "type": "destination", "status": "success"},
            ],
            "workflow_connections": [
                {"from_step": "a", "to_step": "c"},
                {"from_step": "b", "to_step": "c"},
            ],
        }
        html = notifier._build_html_email("s", "b", context=ctx)
        # Y-shape collapses to 2 ranks → 1 inter-column arrow.
        assert html.count("→") == 1
        assert "A" in html and "B" in html and "C" in html
